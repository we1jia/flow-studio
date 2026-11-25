#!/usr/bin/env python3
"""基于本机 Ego Lite（ego-browser）封装 Google Flow 生图与生视频的确定性 CLI。

特性：
1. 运行前后强制调用同目录的 ego_lite_window_guard.py，保护用户工作窗口与 user-owned Space。
2. 复用本机已登录的 Google AI Ultra 浏览器态，不导出、不上传任何 Cookie。
3. 页面自动化路径提交前读取页面实时积分消耗，并按 --max-credits 执行硬预算拦截
   （生图直连不读页面预估：Flow 生图当前不扣点数；视频始终走页面自动化与预算拦截）。
4. 默认复用固定专用项目（Agent CLI Studio），防止在 Flow 首页刷出大量空项目。
5. 输出结构化 JSON，供 Antigravity、Claude Code 等 Agent 直接一句命令调用。
6. 为 Agent 省 Token：默认单行精简输出（--verbose 才给全量）；batch 一次跑完整组镜头；
   图片附 512px 缩略图、视频附 4 帧拼图，检查结果时只看预览图。
7. 生图默认直连（--transport auto）：在专用项目页内取 reCAPTCHA 令牌后直接 POST Flow 的 ogiZ0b，
   不模拟填词/点击/轮询，约 18–28 秒一张；参数组合未实测或请求发出前失败时自动回退页面自动化。
   请求格式移植自 gflow-cli（MIT），见 GFLOW_UPSTREAM_REF。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
GUARD_SCRIPT = SCRIPT_DIR / "ego_lite_window_guard.py"
# 本工具建的 Task Space 都以此开头；守卫只复用同前缀空间，其他 Agent 的空间列为禁止触碰
SPACE_PREFIX = "flow studio"
STATE_FILE = Path.home() / ".cache" / "flow_studio" / "state.json"
# 输出目录：环境变量 FLOW_STUDIO_OUT_DIR 优先，否则为当前工作目录下的 outputs/flow_studio
DEFAULT_OUT_DIR = Path(os.environ.get("FLOW_STUDIO_OUT_DIR") or Path.cwd() / "outputs" / "flow_studio").expanduser()
# Flow 里复用的专用项目名：环境变量 FLOW_STUDIO_PROJECT 可改
DEFAULT_PROJECT_NAME = os.environ.get("FLOW_STUDIO_PROJECT") or "Agent CLI Studio"

# 导入同目录的窗口守卫模块，复用官方判定逻辑
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import ego_lite_window_guard as window_guard  # noqa: E402

RESULT_MARKER = "__FLOW_STUDIO_RESULT__"

# 注入到生成脚本中的纯函数（不依赖浏览器），单独抽出便于用 node 做回归测试
PURE_HELPERS_JS = r"""
// 阻断关键词：只有提交后出现次数比提交前基线增加，才算本次生成被阻断。
// 固定复用的专用项目里可能留着历史失败卡片，按全页文本是否包含来判断会导致后续每次都误报。
const BLOCK_KEYWORDS = [
  '验证码', 'CAPTCHA', 'unusual traffic', '请重试', '无法生成',
  '违反了我们的政策', '点数不足', 'Not enough credits', 'Something went wrong',
];

function countKeywords(text, keywords = BLOCK_KEYWORDS) {
  const s = String(text || '');
  const counts = {};
  for (const kw of keywords) counts[kw] = s.split(kw).length - 1;
  return counts;
}

function detectNewBlockers(text, baselineCounts, promptText = '', keywords = BLOCK_KEYWORDS) {
  const current = countKeywords(text, keywords);
  const hits = [];
  for (const kw of keywords) {
    // 提示词本身含关键词时，新卡片会回显提示词，不能据此判定阻断
    if (promptText && String(promptText).includes(kw)) continue;
    if (current[kw] > ((baselineCounts && baselineCounts[kw]) || 0)) hits.push(kw);
  }
  if (!hits.length) return null;
  const lines = String(text || '').split('\n').map(s => s.trim())
    .filter(l => hits.some(kw => l.includes(kw)));
  return { keywords: hits, lines: [...new Set(lines)].slice(0, 3).map(l => l.slice(0, 200)) };
}

function originalImageCandidates(src) {
  // /asb/ 链接末尾的 =s1600-rw 表示缩放并转码为有损 WebP；去掉后缀只会拿到 512px 缩略图。
  // =d 返回 Flow 存储的原始文件字节，=s0 为原尺寸兜底，最后回退到页面上的预览链接。
  const raw = String(src);
  const base = raw.replace(/=[^=\/]*$/, '');
  const list = [
    { variant: 'original', url: base + '=d' },
    { variant: 'full_size', url: base + '=s0' },
    { variant: 'preview', url: raw },
  ];
  const seen = new Set();
  return list.filter(c => (seen.has(c.url) ? false : (seen.add(c.url), true)));
}

function extForContentType(ct) {
  const t = String(ct || '').toLowerCase();
  if (!t.startsWith('image/')) return null;
  if (t.includes('jpeg') || t.includes('jpg')) return '.jpg';
  if (t.includes('webp')) return '.webp';
  if (t.includes('png')) return '.png';
  if (t.includes('gif')) return '.gif';
  return null;
}
"""


def load_state() -> dict[str, Any]:
    """读取本地状态文件（仅保存默认专用项目 URL，不含任何凭证）。"""
    if not STATE_FILE.exists():
        return {}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(patch: dict[str, Any]) -> None:
    """更新本地状态文件。"""
    state = load_state()
    state.update(patch)
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def check_guard_or_fail() -> dict[str, Any]:
    """执行 Ego Lite 窗口守卫检查；若遇到阻断状态则直接抛出异常。"""
    state = window_guard.collect_state(space_prefix=SPACE_PREFIX)
    action = state.get("action", "stop_probe_failed")
    if action.startswith("stop_"):
        raise RuntimeError(f"Ego Lite 窗口守卫阻断（{action}）：{state.get('reason', '')}")
    return state


def run_ego_node(js_code: str, timeout_sec: int = 360) -> tuple[dict[str, Any], list[str]]:
    """通过 ego-browser nodejs 执行自动化脚本并解析末尾结构化结果。"""
    executable = shutil.which("ego-browser")
    if not executable:
        raise RuntimeError("未找到 ego-browser 命令，请确认 Ego Lite CLI 已安装并在 PATH 中。")

    completed = subprocess.run(
        [executable, "nodejs"],
        input=js_code,
        capture_output=True,
        text=True,
        timeout=timeout_sec,
        check=False,
    )

    notices: list[str] = []
    parsed: dict[str, Any] | None = None
    combined_lines = [
        *completed.stdout.splitlines(),
        *completed.stderr.splitlines(),
    ]
    for line in combined_lines:
        if "[ego-browser:notice]" in line:
            notices.append(line.strip())
        if line.startswith(RESULT_MARKER):
            raw = line[len(RESULT_MARKER) :]
            parsed = json.loads(raw)

    if parsed is not None:
        return parsed, notices

    err_tail = "\n".join(combined_lines[-30:]).strip()
    raise RuntimeError(
        f"ego-browser 执行失败（exit={completed.returncode}）：{err_tail or '无输出'}"
    )


def build_space_bootstrap_js(guard_state: dict[str, Any], space_name: str) -> str:
    """生成符合窗口守卫规范的 TaskSpace 获取与清理代码片段。"""
    action = guard_state["action"]
    reusable_ids = guard_state.get("reusable_task_space_ids") or []
    if action == "reuse_existing_task_space" and reusable_ids:
        reuse_id = int(reusable_ids[0])
        return f"""
const __REUSED_SPACE__ = true;
const task = await taskSpace({reuse_id});
const __existingPages = await task.pages();
const page = __existingPages[0] ?? (await task.adopt((await task.tabs())[0].page));
"""
    return f"""
const __REUSED_SPACE__ = false;
const task = await taskSpace({json.dumps(space_name)});
const page = task.page("p1");
"""


def build_credits_js(guard_state: dict[str, Any]) -> str:
    """构建查询 Google Flow 剩余点数与账号状态的 Node.js 脚本。"""
    bootstrap = build_space_bootstrap_js(guard_state, "flow studio credits")
    return f"""
const RESULT_MARKER = {json.dumps(RESULT_MARKER)};
{bootstrap}

let payload = {{ ok: false }};
try {{
  await page.goto("https://flow.google.com/", {{ timeout: 30000 }});
  await page.waitForFunction(
    () => (
      document.body.innerText.includes('新建项目') ||
      document.body.innerText.includes('New project') ||
      document.body.innerText.includes('ULTRA') ||
      location.hostname.includes('accounts.google.com')
    ),
    undefined,
    {{ timeout: 25000 }}
  );
  const currentUrl = await page.url();
  if (currentUrl.includes("accounts.google.com")) {{
    payload = {{
      ok: false,
      error: "not_logged_in",
      message: "Ego Lite 当前 Profile 尚未登录 Google Flow，请先在浏览器中完成登录后重试。",
      url: currentUrl,
    }};
  }} else {{
    await page.click('loc=role:button[name="账号详情"]', {{ label: "open Flow account credits" }});
    await page.waitForTimeout(1000);
    const info = await page.evaluate(() => {{
      const dialog = document.querySelector('[role="dialog"]');
      const text = dialog ? dialog.innerText : document.body.innerText;
      const lines = text.split('\\n').map(s => s.trim()).filter(Boolean);
      const creditsLine = lines.find(l => l.includes('Google Flow 点数') || /credits/i.test(l)) || '';
      const resetLine = lines.find(l => l.includes('重置时间') || /resets?/i.test(l)) || '';
      const emailLine = lines.find(l => l.includes('@')) || '';
      const watermarkSwitch = document.querySelector('button[aria-label="可见水印"]');
      const watermarkChecked = watermarkSwitch ? watermarkSwitch.getAttribute('aria-checked') : null;
      const numMatch = creditsLine.replace(/,/g, '').match(/(\\d+)/);
      const credits = numMatch ? Number(numMatch[1]) : null;
      const tier = document.body.innerText.includes('ULTRA') ? 'ULTRA' : 'UNKNOWN';
      return {{
        tier,
        email: emailLine,
        credits,
        credits_text: creditsLine,
        reset_text: resetLine,
        visible_watermark: watermarkChecked,
      }};
    }});
    payload = {{
      ok: info.credits !== null,
      ...info,
      url: currentUrl,
    }};
  }}
}} catch (err) {{
  payload = {{
    ok: false,
    error: "runtime_error",
    message: String(err && err.stack ? err.stack : err),
  }};
}} finally {{
  if (!__REUSED_SPACE__) {{
    try {{
      await task.finish({{ keep: [] }});
    }} catch (_) {{}}
  }}
}}
console.log(RESULT_MARKER + JSON.stringify(payload));
"""


# 定位或新建专用项目；生图直连与页面自动化两条路径共用（纯 JS，非 f-string，单花括号）
# 视频“帧”模式：把参考图绑定到“开始”（首帧）/“结束”（尾帧）槽位。
# 2026-11-24 事故：点开“开始”后资源列表默认选中项目里最近一张图，旧逻辑只上传不点选，
# 10 条视频全部用了同一张首帧。现在上传（或复用同名已上传素材）后真实点选，并重新打开列表核对选中项。
BIND_FRAMES_JS = r"""
function frameSlotButtons() {
  return page.evaluate(() => {
    const vis = (b) => { const r = b.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
    const btns = [...document.querySelectorAll('button')].filter(vis);
    const info = (b) => {
      if (!b) return null;
      const r = b.getBoundingClientRect();
      const img = b.querySelector('img');
      return { x: r.left + r.width / 2, y: r.top + r.height / 2, empty: b.classList.contains('empty-chip'), text: (b.innerText || '').trim().slice(0, 40), img: img ? img.src : null };
    };
    const swapIdx = btns.findIndex(b => /交换第一帧和最后一帧|swap first and last frame/i.test(b.getAttribute('aria-label') || ''));
    if (swapIdx > 0) return { start: info(btns[swapIdx - 1]), end: info(btns[swapIdx + 1]), via: 'swap' };
    const byText = (re) => btns.find(b => re.test(((b.innerText || '') + ' ' + (b.getAttribute('aria-label') || '')).trim()));
    return { start: info(byText(/^(开始|首帧|Start|First frame)/i)), end: info(byText(/^(结束|尾帧|End|Last frame)/i)), via: 'text' };
  });
}

// 资源列表选项文字带图标前缀（如 “image xxx.jpg”），上传中显示 “正在上传 xxx.jpg”
function readAssetList() {
  return page.evaluate(() => {
    const lb = [...document.querySelectorAll('[role="listbox"]')].find(l => { const r = l.getBoundingClientRect(); return r.width > 0 && r.height > 0; });
    if (!lb) return null;
    const opts = [...lb.querySelectorAll('[role="option"]')].map(o => {
      const r = o.getBoundingClientRect();
      const text = (o.innerText || '').replace(/\s+/g, ' ').trim();
      return { text, uploading: /正在上传|uploading/i.test(text), img: o.querySelector('img')?.src || null, x: r.left + r.width / 2, y: r.top + r.height / 2 };
    });
    return { label: lb.getAttribute('aria-label'), options: opts };
  });
}

const assetBase = (u) => String(u || '').replace(/=[^=\/]*$/, '');

async function openSlotPicker(slot) {
  const slots = await frameSlotButtons();
  const btn = slots[slot];
  if (!btn) throw new Error(`未找到视频“${slot === 'start' ? '开始' : '结束'}”帧入口按钮`);
  await page.keyboard.press("Escape");
  await page.waitForTimeout(300);
  await page.mouse.click(btn.x, btn.y, { label: `open ${slot} frame picker` });
  try {
    await page.waitForFunction(() => [...document.querySelectorAll('[role="listbox"]')].some(l => { const r = l.getBoundingClientRect(); return r.width > 0 && r.height > 0; }), undefined, { timeout: 8000 });
  } catch (_) {
    return null;
  }
  await page.waitForTimeout(500);
  return await readAssetList();
}

async function uploadIntoOpenPicker(imgPath) {
  const uploadRect = await page.evaluate(() => {
    const u = [...document.querySelectorAll('button')].find(b => { const r = b.getBoundingClientRect(); return r.width > 0 && r.height > 0 && (b.innerText.includes('上传媒体') || /upload media/i.test(b.innerText)); });
    if (!u) return null;
    const r = u.getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
  });
  if (!uploadRect) throw new Error("帧选择弹层里未找到可见的“上传媒体”按钮");
  // 真实鼠标点击才算可信用户手势，否则 Chromium 拦截文件选择框
  const [fc] = await Promise.all([
    page.waitForFileChooser({ timeout: 10000 }),
    page.mouse.click(uploadRect.x, uploadRect.y),
  ]);
  await fc.setFiles(imgPath);
}

// 绑定规则（2026-11-24 真机确认）：资源列表里第一次点击只高亮，点击已高亮项才填入槽位并关闭列表；
// 不要点“添加到提示”——它会把列表默认高亮的最近一张图塞进去（首帧事故的直接原因）。
async function bindVideoFrames(refImages) {
  if (refImages.length > 2) throw new Error("帧模式最多 2 张参考图（首帧、尾帧）");
  const results = [];
  for (let i = 0; i < refImages.length; i++) {
    const slot = i === 0 ? 'start' : 'end';
    const name = path.basename(refImages[i]);
    const findOpt = (lst) => lst && lst.options.find(o => !o.uploading && o.text.includes(name));
    let list = await openSlotPicker(slot);
    if (!list) throw new Error(`点开${slot}帧入口后没有出现资源列表`);
    const reused = !!findOpt(list);
    if (!reused) {
      await uploadIntoOpenPicker(refImages[i]);
      const deadline = Date.now() + 60000;
      for (;;) {
        await page.waitForTimeout(1500);
        list = (await readAssetList()) || (await openSlotPicker(slot));
        if (findOpt(list)) break;
        if (Date.now() > deadline) throw new Error(`FRAME_BIND_FAILED:${slot} 上传后 60 秒内资源列表里没出现 ${name}`);
      }
    }
    // 列表有展开动画：等稳定、滚到可见、用最新坐标点击；没填上就重开列表再点一次
    const isFilled = (ms = 6000) => page.waitForFunction((s) => {
      const vis = (b) => b.getBoundingClientRect().width > 0;
      const btns = [...document.querySelectorAll('button')].filter(vis);
      const k = btns.findIndex(b => /交换第一帧和最后一帧|swap first and last frame/i.test(b.getAttribute('aria-label') || ''));
      const b = s === 'start' ? btns[k - 1] : btns[k + 1];
      return !!b && !b.classList.contains('empty-chip') && !!b.querySelector('img');
    }, slot, { timeout: ms }).then(() => true, () => false);
    let target = null;
    const attemptsLog = [];
    for (let attempt = 0; attempt < 2; attempt++) {
      if (attempt > 0 || !(await readAssetList())) list = await openSlotPicker(slot);
      await page.waitForTimeout(900);
      await page.evaluate((nm) => {
        const o = [...document.querySelectorAll('[role="listbox"] [role="option"]')].find(o => !/正在上传|uploading/i.test(o.innerText || '') && (o.innerText || '').includes(nm));
        o?.scrollIntoView({ block: 'center' });
      }, name);
      await page.waitForTimeout(400);
      list = await readAssetList();
      target = findOpt(list);
      if (!target) throw new Error(`FRAME_BIND_FAILED:${slot} 资源列表里找不到 ${name}`);
      // 第一次点击只把该项设为高亮；点“已高亮”的项才真正填入槽位（2026-11-24 真机确认）
      await page.mouse.click(target.x, target.y, { label: `select ${slot} frame` });
      let filledNow = await isFilled(1800);
      if (!filledNow && (await readAssetList())) {
        const again = findOpt(await readAssetList());
        if (again) {
          await page.mouse.click(again.x, again.y, { label: `confirm ${slot} frame` });
          filledNow = await isFilled(6000);
        }
      }
      attemptsLog.push(await page.evaluate((nm) => ({
        lb: [...document.querySelectorAll('[role="listbox"]')].some(l => l.getBoundingClientRect().width > 0),
        sel: [...document.querySelectorAll('[role="listbox"] [role="option"][aria-selected="true"]')].map(o => (o.innerText || '').trim().slice(0, 30)),
        btns: [...document.querySelectorAll('.cdk-overlay-pane button')].filter(b => b.getBoundingClientRect().width > 0).map(b => (b.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 20)).slice(0, 12),
      }), name));
      if (filledNow) break;
    }
    await page.waitForTimeout(500);
    const slots = await frameSlotButtons();
    const chip = slots[slot];
    const filled = !!chip && !chip.empty && !!chip.img;
    const sameAsset = !!target.img && !!chip?.img && assetBase(chip.img) === assetBase(target.img);
    const ok = filled && (sameAsset || !target.img);
    results.push({ slot, name, reused, filled, same_asset: sameAsset, verified: ok });
    if (!ok) {
      const diag = await page.evaluate(() => ({
        listbox: [...document.querySelectorAll('[role="listbox"] [role="option"]')].slice(0, 4).map(o => { const r = o.getBoundingClientRect(); return [(o.innerText || '').trim().slice(0, 30), o.getAttribute('aria-selected'), Math.round(r.left), Math.round(r.top), Math.round(r.width)]; }),
        overlays: [...document.querySelectorAll('.cdk-overlay-pane, [role="dialog"], [role="menu"]')].filter(e => e.getBoundingClientRect().width > 0).map(e => (e.getAttribute('role') || e.className).toString().slice(0, 40) + ':' + (e.innerText || '').replace(/\s+/g, ' ').slice(0, 80)),
      }));
      throw new Error(`FRAME_BIND_FAILED:${slot} 帧没有绑定到 ${name}（filled=${filled}, same_asset=${sameAsset}） target=${JSON.stringify(target)} chip=${JSON.stringify(chip)} attempts=${JSON.stringify(attemptsLog)} diag=${JSON.stringify(diag)}`);
    }
  }
  return results;
}

async function setDuration(wantSeconds) {
  const read = () => page.evaluate(() => {
    const s = document.querySelector('input[type="range"][aria-label="时长"], input[type="range"][aria-label="Duration"]');
    if (!s) return null;
    const txt = (s.closest('div')?.parentElement?.innerText || '').replace(/\s+/g, ' ');
    const m = txt.match(/(\d+)\s*(秒|s\b)/);
    return { value: Number(s.value), min: Number(s.min || 0), max: Number(s.max || 0), seconds: m ? Number(m[1]) : null };
  });
  const write = (v) => page.evaluate((v) => {
    const s = document.querySelector('input[type="range"][aria-label="时长"], input[type="range"][aria-label="Duration"]');
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
    setter.call(s, String(v));
    s.dispatchEvent(new Event('input', { bubbles: true }));
    s.dispatchEvent(new Event('change', { bubbles: true }));
  }, v);
  const cur = await read();
  if (!cur) return { found: false, optionText: `${wantSeconds} 秒`, reason: 'no_duration_slider' };
  if (cur.seconds === wantSeconds) return { found: true, selected: `${wantSeconds} 秒` };
  const tried = [];
  const guess = Math.min(cur.max, Math.max(cur.min, cur.value + (wantSeconds - (cur.seconds ?? wantSeconds))));
  const order = [guess, ...Array.from({ length: cur.max - cur.min + 1 }, (_, k) => cur.min + k).filter(v => v !== guess)];
  for (const v of order) {
    await write(v);
    await page.waitForTimeout(200);
    const now = await read();
    tried.push(now?.seconds);
    if (now?.seconds === wantSeconds) return { found: true, selected: `${wantSeconds} 秒` };
  }
  await write(cur.value);
  return { found: false, optionText: `${wantSeconds} 秒`, available: [...new Set(tried.filter(x => x != null))] };
}
"""

ENSURE_PROJECT_JS = r"""
async function ensureProject() {
  if (CFG.project_url) {
    await page.goto(CFG.project_url, { timeout: 30000 });
    await page.waitForFunction(
      () => !!document.querySelector('button[aria-label="设置触发器"]') || location.hostname.includes('accounts.google.com'),
      undefined,
      { timeout: 30000 }
    );
    const u = await page.url();
    if (u.includes('accounts.google.com')) throw new Error("NOT_LOGGED_IN:" + u);
    return u;
  }

  // 访问首页，查找是否已存在同名专用项目；无则新建并重命名
  await page.goto("https://flow.google.com/", { timeout: 30000 });
  await page.waitForFunction(
    () => document.body.innerText.includes('新建项目') || document.body.innerText.includes('New project') || location.hostname.includes('accounts.google.com'),
    undefined,
    { timeout: 30000 }
  );
  const homeUrl = await page.url();
  if (homeUrl.includes('accounts.google.com')) throw new Error("NOT_LOGGED_IN:" + homeUrl);
  await page.waitForTimeout(1200);

  const existingHref = await page.evaluate((targetName) => {
    const anchors = [...document.querySelectorAll('a[href*="/project/"]')];
    for (const a of anchors) {
      const card = a.parentElement;
      const text = card ? card.innerText : '';
      if (text.includes(targetName)) {
        return a.href;
      }
    }
    return null;
  }, CFG.project_name);

  if (existingHref) {
    await page.goto(existingHref, { timeout: 30000 });
    await page.waitForFunction(() => !!document.querySelector('button[aria-label="设置触发器"]'), undefined, { timeout: 30000 });
    return await page.url();
  }

  // 新建专用项目
  const created = await page.evaluate(() => {
    const btns = [...document.querySelectorAll('button')];
    const target = btns.find(b => b.innerText.includes('新建项目') || b.innerText.includes('New project'));
    if (target) {
      target.click();
      return true;
    }
    return false;
  });
  if (!created) throw new Error("未在 Flow 首页找到“新建项目”按钮");

  await page.waitForFunction(
    () => location.pathname.includes('/project/') && !!document.querySelector('button[aria-label="设置触发器"]'),
    undefined,
    { timeout: 30000 }
  );
  await page.waitForTimeout(800);

  // 重命名为专用项目名，方便后续复用
  const hasTitleInput = await page.evaluate(() => !!document.querySelector('input[aria-label="可编辑文本"]'));
  if (hasTitleInput) {
    await page.click('css=input[aria-label="可编辑文本"]', { label: "rename studio project" });
    await page.keyboard.press("ControlOrMeta+A");
    await page.keyboard.insertText(CFG.project_name);
    await page.keyboard.press("Enter");
    await page.waitForTimeout(600);
  }
  return await page.url();
}
"""


def build_generate_js(guard_state: dict[str, Any], cfg: dict[str, Any]) -> str:
    """构建执行生图或生视频并自动下载原件的 Node.js 脚本。"""
    bootstrap = build_space_bootstrap_js(guard_state, f"flow studio {cfg['mode']}")
    cfg_json = json.dumps(cfg, ensure_ascii=False)
    return f"""
const fs = await import("node:fs/promises");
const path = await import("node:path");
const RESULT_MARKER = {json.dumps(RESULT_MARKER)};
const CFG = {cfg_json};
{PURE_HELPERS_JS}
{bootstrap}

async function readCreditsQuick() {{
  try {{
    await page.click('loc=role:button[name="账号详情"]', {{ label: "read Flow credits balance", timeout: 5000 }});
    await page.waitForTimeout(800);
    const val = await page.evaluate(() => {{
      const dialog = document.querySelector('[role="dialog"]');
      const text = dialog ? dialog.innerText : '';
      const line = text.split('\\n').map(s => s.trim()).find(l => l.includes('Google Flow 点数') || /credits/i.test(l)) || '';
      const m = line.replace(/,/g, '').match(/(\\d+)/);
      return m ? Number(m[1]) : null;
    }});
    const closed = await page.evaluate(() => {{
      const closeBtn = document.querySelector('button[aria-label="关闭账号面板"]') || document.querySelector('[role="dialog"] button[aria-label*="关闭"]');
      if (closeBtn) {{ closeBtn.click(); return true; }}
      return false;
    }});
    if (!closed) {{
      await page.keyboard.press("Escape");
    }}
    await page.waitForFunction(() => !document.querySelector('[role="dialog"]'), undefined, {{ timeout: 5000 }}).catch(() => {{}});
    await page.waitForTimeout(300);
    return val;
  }} catch (_) {{
    return null;
  }}
}}

{ENSURE_PROJECT_JS}
{BIND_FRAMES_JS}
// Flow 新版项目页默认开启“Flow 智能体”，此时经典输入区的“设置触发器”被隐藏（display:none）。
// 打开设置前先确认按钮可见，不可见就关掉智能体开关；结束时在 finally 里尽量恢复原状。
let __agentModeTurnedOff = false;
async function ensureClassicComposer() {{
  const settingsVisible = () => {{
    const b = document.querySelector('button[aria-label="设置触发器"]');
    if (!b) return false;
    const r = b.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }};
  if (await page.evaluate(settingsVisible)) return;
  const clicked = await page.evaluate(() => {{
    const t = document.querySelector('button[aria-label="Flow 智能体"]') || document.querySelector('button[aria-label*="智能体"]');
    if (t && t.getAttribute('aria-pressed') === 'true') {{
      t.click();
      return true;
    }}
    return false;
  }});
  if (clicked) __agentModeTurnedOff = true;
  await page.waitForFunction(settingsVisible, undefined, {{ timeout: 10000 }});
  await page.waitForTimeout(400);
}}

async function restoreAgentMode() {{
  if (!__agentModeTurnedOff) return;
  try {{
    await page.evaluate(() => {{
      const t = document.querySelector('button[aria-label="Flow 智能体"]') || document.querySelector('button[aria-label*="智能体"]');
      if (t && t.getAttribute('aria-pressed') === 'false') t.click();
    }});
    await page.waitForTimeout(300);
  }} catch (_) {{}}
}}

async function configureSettings() {{
  await ensureClassicComposer();
  await page.click('css=button[aria-label="设置触发器"]', {{ label: "open Flow generation settings" }});
  await page.waitForFunction(() => document.querySelectorAll('[role="radiogroup"]').length > 0, undefined, {{ timeout: 10000 }});
  await page.waitForTimeout(400);

  const clickRadioInGroup = async (groupHint, optionText) => {{
    if (!optionText) return {{ skipped: true }};
    return await page.evaluate(({{ groupHint, optionText }}) => {{
      const groups = [...document.querySelectorAll('[role="radiogroup"]')];
      let targetGroup = groups.find(g => {{
        const label = (g.getAttribute('aria-label') || '') + ' ' + (g.parentElement?.getAttribute('aria-label') || '') + ' ' + (g.parentElement?.innerText || '');
        return label.includes(groupHint);
      }});
      if (!targetGroup) {{
        // 兜底：遍历全部 radiogroup 找包含该选项文本的组
        targetGroup = groups.find(g => [...g.querySelectorAll('[role="radio"]')].some(r => r.innerText.includes(optionText)));
      }}
      if (!targetGroup) return {{ found: false, groupHint, optionText }};
      const radios = [...targetGroup.querySelectorAll('[role="radio"]')];
      const match = radios.find(r => {{
        const t = r.innerText.replace(/\\s+/g, ' ').trim();
        return t === optionText || t.endsWith(' ' + optionText) || t.includes(optionText);
      }});
      if (!match) return {{ found: false, groupHint, optionText, available: radios.map(r => r.innerText.trim()) }};
      if (match.getAttribute('aria-checked') !== 'true') {{
        match.click();
      }}
      return {{ found: true, selected: match.innerText.replace(/\\s+/g, ' ').trim() }};
    }}, {{ groupHint, optionText }});
  }};

  const modeLabel = CFG.mode === 'video' ? '视频' : '图片';
  const rMode = await clickRadioInGroup('模式', modeLabel);
  await page.waitForTimeout(500);

  let rVideoType = null;
  let rResolution = null;
  let rThinking = null;
  if (CFG.mode === 'video' && CFG.video_type) {{
    rVideoType = await clickRadioInGroup('视频类型', CFG.video_type);
    await page.waitForTimeout(300);
  }}

  // 先切模型，再设分辨率/宽高比/数量：可选项随模型变化，反过来会按上一次的模型去设置
  let selectedModel = null;
  if (CFG.model) {{
    const currentModelText = await page.evaluate(() => document.querySelector('button[aria-label="选择模型系列"]')?.innerText?.trim() || '');
    if (!currentModelText.toLowerCase().includes(CFG.model.toLowerCase())) {{
      await page.click('css=button[aria-label="选择模型系列"]', {{ label: "open model selector" }});
      await page.waitForTimeout(500);
      const picked = await page.evaluate((want) => {{
        const items = [...document.querySelectorAll('[role="menuitem"], [role="option"], [role="menuitemradio"]')];
        const m = items.find(el => el.innerText.toLowerCase().includes(want.toLowerCase()));
        if (m) {{
          const txt = m.innerText.replace(/\\s+/g, ' ').trim();
          m.click();
          return {{ found: true, text: txt }};
        }}
        return {{ found: false, available: items.map(el => el.innerText.replace(/\\s+/g, ' ').trim()) }};
      }}, CFG.model);
      await page.waitForTimeout(500);
      if (!picked.found) {{
        throw new Error("未找到指定模型 '" + CFG.model + "'，当前可选：" + JSON.stringify(picked.available));
      }}
      selectedModel = picked.text;
    }} else {{
      selectedModel = currentModelText.replace(/arrow_drop_down/g, '').replace(/\\s+/g, ' ').trim();
    }}
  }} else {{
    selectedModel = await page.evaluate(() => (document.querySelector('button[aria-label="选择模型系列"]')?.innerText || '').replace(/arrow_drop_down/g, '').replace(/\\s+/g, ' ').trim());
  }}

  // 选模型的菜单可能把设置弹层一起关掉，需要时重新打开
  const settingsOpen = await page.evaluate(() => document.querySelectorAll('[role="radiogroup"]').length > 0);
  if (!settingsOpen) {{
    await page.click('css=button[aria-label="设置触发器"]', {{ label: "reopen Flow generation settings" }});
    await page.waitForFunction(() => document.querySelectorAll('[role="radiogroup"]').length > 0, undefined, {{ timeout: 10000 }});
    await page.waitForTimeout(400);
  }}

  let rDuration = null;
  if (CFG.mode === 'video') {{
    if (CFG.resolution) {{
      rResolution = await clickRadioInGroup('视频分辨率', CFG.resolution);
      await page.waitForTimeout(300);
    }}
    if (CFG.duration) {{
      rDuration = await setDuration(Number(CFG.duration));
      await page.waitForTimeout(300);
    }}
  }} else if (CFG.thinking) {{
    rThinking = await clickRadioInGroup('思考等级', CFG.thinking);
    await page.waitForTimeout(300);
  }}

  const rAspect = await clickRadioInGroup('宽高比', CFG.aspect);
  await page.waitForTimeout(300);
  const rCount = await clickRadioInGroup('输出数量', 'x' + String(CFG.count));
  await page.waitForTimeout(400);

  // 读取预估消耗点数
  const costInfo = await page.evaluate(() => {{
    const link = document.querySelector('a[href*="g1_ai_credit_menu"]');
    const containerText = link?.parentElement?.innerText?.replace(/\\s+/g, ' ').trim() || '';
    const linkText = link?.innerText?.trim() || '';
    const m = (linkText || containerText).replace(/,/g, '').match(/(\\d+)/);
    return {{
      cost_text: containerText,
      estimated_credits: m ? Number(m[1]) : null,
    }};
  }});

  // 关闭设置弹层
  await page.keyboard.press("Escape");
  await page.waitForTimeout(300);

  return {{
    mode: rMode,
    aspect: rAspect,
    count: rCount,
    video_type: rVideoType,
    resolution: rResolution,
    duration: rDuration,
    thinking: rThinking,
    model: selectedModel,
    ...costInfo,
  }};
}}

async function uploadRefImages(refImages) {{
  const uploaded = [];
  for (const imgPath of refImages) {{
    const beforeAsbCount = await page.evaluate(() => new Set([...document.querySelectorAll('img')].map(i => i.src || '').filter(s => s.includes('/asb/'))).size);
    // 确保任何旧弹层已关闭，先按一次 Escape
    await page.keyboard.press("Escape");
    await page.waitForTimeout(300);

    // 兼容图片/素材模式的“在提示框中添加素材”按钮与视频帧模式的首帧入口
    const openBtnRect = await page.evaluate(() => {{
      const direct = document.querySelector('button[aria-label="在提示框中添加素材"], button[aria-label*="Add media"], button[aria-label*="添加素材"]');
      if (direct) {{
        const r = direct.getBoundingClientRect();
        if (r.width > 0 && r.height > 0) return {{ x: r.left + r.width / 2, y: r.top + r.height / 2, kind: "direct" }};
      }}
      const btns = [...document.querySelectorAll('button')];
      const frameBtn = btns.find(b => {{
        const r = b.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) return false;
        const txt = ((b.innerText || '') + ' ' + (b.getAttribute('aria-label') || '')).trim();
        return /^(开始|首帧|Start|First frame)/i.test(txt) || /添加首帧|Start frame/i.test(txt);
      }});
      if (frameBtn) {{
        const r = frameBtn.getBoundingClientRect();
        return {{ x: r.left + r.width / 2, y: r.top + r.height / 2, kind: "frame" }};
      }}
      return null;
    }});
    if (!openBtnRect) {{
      throw new Error("未找到“在提示框中添加素材”或视频首帧入口按钮");
    }}
    await page.mouse.click(openBtnRect.x, openBtnRect.y);

    // 等待可见的“上传媒体”按钮出现并稳定
    await page.waitForFunction(() => {{
      return [...document.querySelectorAll('button')].some(b => {{
        const r = b.getBoundingClientRect();
        return r.width > 0 && r.height > 0 && (b.innerText.includes('上传媒体') || /upload media/i.test(b.innerText));
      }});
    }}, undefined, {{ timeout: 8000 }});
    await page.waitForTimeout(400);

    let chooser = null;
    for (let attempt = 0; attempt < 2; attempt++) {{
      const uploadRect = await page.evaluate(() => {{
        const btns = [...document.querySelectorAll('button')];
        const u = btns.find(b => {{
          const r = b.getBoundingClientRect();
          return r.width > 0 && r.height > 0 && (b.innerText.includes('上传媒体') || /upload media/i.test(b.innerText));
        }});
        if (!u) return null;
        const r = u.getBoundingClientRect();
        return {{ x: r.left + r.width / 2, y: r.top + r.height / 2 }};
      }});
      if (!uploadRect) {{
        throw new Error("在添加素材弹层中未找到可见的“上传媒体”按钮");
      }}
      try {{
        // 必须使用真实鼠标点击触发可信用户手势（Trusted User Activation），否则 Chromium 会拦截文件选择框
        const [fc] = await Promise.all([
          page.waitForFileChooser({{ timeout: 10000 }}),
          page.mouse.click(uploadRect.x, uploadRect.y),
        ]);
        chooser = fc;
        break;
      }} catch (e) {{
        if (attempt === 1) throw e;
        await page.waitForTimeout(800);
      }}
    }}

    await chooser.setFiles(imgPath);

    // 等待参考图真正上传完毕（页面出现新的 /asb/ 资源或进度消失，最多等 20s）
    const uploadDeadline = Date.now() + 20000;
    while (Date.now() < uploadDeadline) {{
      await page.waitForTimeout(1000);
      const status = await page.evaluate((prevCnt) => {{
        const urls = new Set([...document.querySelectorAll('img')].map(i => i.src || '').filter(s => s.includes('/asb/')));
        const uploading = /正在上传|uploading/i.test(document.body.innerText || '');
        const promptBox = document.querySelector('[role="textbox"]')?.closest('div')?.parentElement;
        const promptImgs = promptBox ? [...promptBox.querySelectorAll('img')].filter(i => (i.src || '').includes('/asb/') || (i.src || '').startsWith('blob:') || (i.src || '').startsWith('data:')).length : 0;
        return {{ curCnt: urls.size, uploading, promptImgs }};
      }}, beforeAsbCount);
      if (!status.uploading && (status.curCnt > beforeAsbCount || status.promptImgs > uploaded.length)) {{
        break;
      }}
    }}
    await page.waitForTimeout(1500);

    // 若弹层仍打开且存在可点击的“添加到提示”按钮则真实点击
    const addRect = await page.evaluate(() => {{
      const btns = [...document.querySelectorAll('button')];
      const addBtn = btns.find(b => {{
        const r = b.getBoundingClientRect();
        return r.width > 0 && r.height > 0 && !b.disabled && (b.innerText.trim() === '添加到提示' || b.innerText.trim() === 'Add to prompt');
      }});
      if (!addBtn) return null;
      const r = addBtn.getBoundingClientRect();
      return {{ x: r.left + r.width / 2, y: r.top + r.height / 2 }};
    }});
    if (addRect) {{
      await page.mouse.click(addRect.x, addRect.y);
      await page.waitForTimeout(800);
    }}
    await page.keyboard.press("Escape");
    await page.waitForTimeout(500);
    uploaded.push(imgPath);
  }}
  return uploaded;
}}

async function fillPrompt(promptText) {{
  // 精准定位 Flow 底部富文本提示框（div[role="textbox"][aria-label="您希望创作什么？"]）
  const exactSelector = 'css=[role="textbox"][aria-label="您希望创作什么？"]';
  const hasExact = await page.evaluate(() => !!document.querySelector('[role="textbox"][aria-label="您希望创作什么？"]'));
  if (hasExact) {{
    await page.click(exactSelector, {{ label: "focus Flow prompt editor" }});
  }} else {{
    const target = await page.evaluate(() => {{
      const candidates = [...document.querySelectorAll('[role="textbox"][contenteditable="true"], [contenteditable="true"]')]
        .filter(el => {{
          const r = el.getBoundingClientRect();
          return r.width > 120 && r.height >= 20 && r.bottom > window.innerHeight * 0.5;
        }});
      if (!candidates.length) return null;
      const r = candidates[0].getBoundingClientRect();
      return {{ x: Math.round(r.left + Math.min(80, r.width / 2)), y: Math.round(r.top + r.height / 2) }};
    }});
    if (!target) throw new Error("未找到 Flow 底部的提示词输入框");
    await page.mouse.click(target.x, target.y, {{ label: "focus Flow prompt editor" }});
  }}

  await page.waitForTimeout(200);
  await page.keyboard.press("ControlOrMeta+A");
  await page.keyboard.press("Backspace");
  await page.waitForTimeout(150);
  // 使用原生 paste() 以触发 ProseMirror/Angular 状态同步并完美支持中文与长文本
  await page.keyboard.paste(promptText);
  await page.waitForTimeout(350);

  // 核验文本已写入且“开始生成”按钮已激活；若仍 disabled 则补发一次轻量键入触发变更检测
  const editorState = await page.evaluate(() => {{
    const el = document.querySelector('[role="textbox"][aria-label="您希望创作什么？"]') ||
      document.querySelector('[role="textbox"][contenteditable="true"]');
    const btn = document.querySelector('button[aria-label="开始生成"]');
    return {{
      text: (el?.innerText || '').trim(),
      btnDisabled: btn ? (btn.disabled || btn.getAttribute('aria-disabled') === 'true') : null,
    }};
  }});
  if (!editorState.text) {{
    await page.keyboard.type(promptText);
    await page.waitForTimeout(350);
  }} else if (editorState.btnDisabled) {{
    await page.keyboard.type(" ");
    await page.keyboard.press("Backspace");
    await page.waitForTimeout(250);
  }}

  const finalCheck = await page.evaluate(() => {{
    const el = document.querySelector('[role="textbox"][aria-label="您希望创作什么？"]') ||
      document.querySelector('[role="textbox"][contenteditable="true"]');
    const btn = document.querySelector('button[aria-label="开始生成"]');
    return {{
      text: (el?.innerText || '').trim(),
      btnDisabled: btn ? (btn.disabled || btn.getAttribute('aria-disabled') === 'true') : null,
    }};
  }});
  if (!finalCheck.text) {{
    throw new Error("提示词写入 Flow 富文本编辑器失败（读取仍为空）");
  }}
  if (finalCheck.btnDisabled) {{
    throw new Error("提示词已写入但“开始生成”按钮仍为禁用状态");
  }}
}}

async function snapshotGridMedia() {{
  return await page.evaluate(() => {{
    // 同一素材的缩略图地址会在末尾带/不带 “=mm,18,15”“=s1600-rw” 这类参数之间切换，用去掉参数的地址当主键
    const mediaKey = (s) => String(s || '').split('?')[0].replace(/=[^/=]*$/, '');
    // 卡片容器的 aria-label（如 "Anime girl reacting to whale"），只用于诊断哪张卡被选中
    const cardLabel = (el) => {{
      let cur = el.parentElement;
      for (let d = 0; d < 6 && cur; d++) {{
        const a = cur.getAttribute && cur.getAttribute('aria-label');
        if (a) return a.slice(0, 80);
        cur = cur.parentElement;
      }}
      return '';
    }};
    const checkCardIsVideo = (el) => {{
      let cur = el;
      for (let d = 0; d < 6 && cur; d++) {{
        if (cur.tagName === 'VIDEO') return true;
        if (cur.querySelector && cur.querySelector('video')) return true;
        const txt = (cur.innerText || '').trim();
        // 新版网格的视频卡片用 play_circle 图标，旧版用 play_arrow
        if (/\\b\\d+:\\d{{2}}\\b/.test(txt) || /play_arrow|play_circle|videocam/i.test(txt)) return true;
        const aria = (cur.getAttribute && (cur.getAttribute('aria-label') || '')) || '';
        if (/video|视频/i.test(aria)) return true;
        cur = cur.parentElement;
      }}
      return false;
    }};
    const imgs = [...document.querySelectorAll('img')]
      .filter(el => {{
        const src = el.src || '';
        const r = el.getBoundingClientRect();
        return src.includes('/asb/') && r.width >= 80 && r.height >= 60;
      }})
      .map(el => ({{
        src: el.src,
        alt: el.alt || '',
        w: el.naturalWidth,
        h: el.naturalHeight,
        top: Math.round(el.getBoundingClientRect().top),
        left: Math.round(el.getBoundingClientRect().left),
        isVideo: checkCardIsVideo(el),
        label: cardLabel(el),
      }}));
    const vids = [...document.querySelectorAll('video')]
      .filter(el => {{
        const src = el.currentSrc || el.src || el.poster || '';
        const r = el.getBoundingClientRect();
        return Boolean(src) && r.width >= 80 && r.height >= 60;
      }})
      .map(el => ({{
        src: el.currentSrc || el.src || el.poster || '',
        alt: el.getAttribute('aria-label') || '',
        w: el.videoWidth || Math.round(el.getBoundingClientRect().width),
        h: el.videoHeight || Math.round(el.getBoundingClientRect().height),
        top: Math.round(el.getBoundingClientRect().top),
        left: Math.round(el.getBoundingClientRect().left),
        isVideo: true,
        label: cardLabel(el),
      }}));
    const unique = [];
    const seen = new Set();
    for (const item of [...vids, ...imgs]) {{
      item.key = mediaKey(item.src);
      if (!seen.has(item.key)) {{
        seen.add(item.key);
        unique.push(item);
      }}
    }}
    return unique;
  }});
}}

async function readBodyText() {{
  return await page.evaluate(() => document.body.innerText || '');
}}

// 切到对应类型导航（视频/图片），网格只剩同类素材
async function selectMediaTab() {{
  await page.evaluate((mode) => {{
    const navLabel = mode === 'video' ? '视频' : '图片';
    const btn = [...document.querySelectorAll('nav button, [role="navigation"] button')].find(b => b.innerText.includes(navLabel));
    if (btn) btn.click();
  }}, CFG.mode);
  await page.waitForTimeout(800);
}}

// 生成前已有媒体基线；上传过参考图时多等一会，让网格把参考图渲染完并纳入基线
async function captureBaseline(hadUploads) {{
  await selectMediaTab();
  if (hadUploads) await page.waitForTimeout(3000);
  let baseline = await snapshotGridMedia();
  if (hadUploads) {{
    await page.waitForTimeout(1500);
    const baseline2 = await snapshotGridMedia();
    if (baseline2.length > baseline.length) baseline = baseline2;
  }}
  return baseline;
}}

// 诊断用：网格前几张卡的标签、src 尾部与位置
function gridPreview(list, n = 3) {{
  return list.slice(0, n).map(x => ({{ label: x.label || '', src: String(x.src || '').slice(-24), video: !!x.isVideo, top: x.top, left: x.left }}));
}}

// 视频卡片按网格阅读顺序排列（同一行 top 相差 ≤12px 视为一行，行内按 left）；新成品总插在左上角
const gridOrder = (list) => {{ const v = list.filter(x => x.isVideo).sort((a, b) => a.top - b.top); let row = 0, rowTop = null; for (const x of v) {{ if (rowTop === null || x.top - rowTop > 12) {{ row++; rowTop = x.top; }} x._row = row; }} return v.sort((a, b) => (a._row - b._row) || (a.left - b.left)); }};

async function downloadNewItems(newItems, mode, outDir, prefix) {{
  await fs.mkdir(outDir, {{ recursive: true }});
  const saved = [];

  for (let i = 0; i < newItems.length; i++) {{
    const item = newItems[i];
    const indexSuffix = newItems.length > 1 ? `_${{i + 1}}` : '';

    if (mode === 'image') {{
      // 通过同源已登录的 page.fetch 下载：优先 =d 原始文件，失败再依次回退原尺寸、预览图
      const tmpPath = path.join(outDir, `${{prefix}}${{indexSuffix}}.part`);
      const attempts = [];
      let savedItem = null;
      for (const cand of originalImageCandidates(item.src)) {{
        try {{
          const resp = await page.fetch(cand.url, {{ saveAs: tmpPath, timeout: 60000 }});
          const ct = String(resp.headers?.['content-type'] || resp.headers?.['Content-Type'] || '').toLowerCase();
          const stat = await fs.stat(tmpPath).catch(() => null);
          const ext = extForContentType(ct);
          if (resp.ok && stat && stat.size > 0 && ext) {{
            const finalPath = path.join(outDir, `${{prefix}}${{indexSuffix}}${{ext}}`);
            await fs.rename(tmpPath, finalPath);
            savedItem = {{
              path: finalPath,
              bytes: stat.size,
              content_type: ct,
              variant: cand.variant,
              preview_width: item.w,
              preview_height: item.h,
              source_url: cand.url,
            }};
            break;
          }}
          attempts.push({{ variant: cand.variant, status: resp.status, content_type: ct, bytes: stat ? stat.size : 0 }});
        }} catch (e) {{
          attempts.push({{ variant: cand.variant, error: String(e).slice(0, 200) }});
        }}
        await fs.rm(tmpPath, {{ force: true }}).catch(() => {{}});
      }}
      if (!savedItem) {{
        throw new Error("下载图片失败，所有候选地址均不可用：" + JSON.stringify(attempts) + " src=" + item.src);
      }}
      if (attempts.length) savedItem.fallback_attempts = attempts;
      saved.push(savedItem);
      continue;
    }}

    // 视频模式：定位对应卡片（兼容 <video> 与 <img>），进入详情页后点击“下载媒体内容”并捕获浏览器下载事件
    // 点击前后都核对：点击点下确实是目标卡片，详情页标题与卡片标签一致（防止网格刚刷新时点到相邻的旧视频）
    const locateCard = () => page.evaluate((targetKey) => {{
      const keyOf = (s) => String(s || '').split('?')[0].replace(/=[^/=]*$/, '');
      const el = [...document.querySelectorAll('video, img')].find(node => keyOf(node.currentSrc || node.src || node.poster) === targetKey);
      if (!el) return null;
      el.scrollIntoView({{ block: 'center' }});
      const r = el.getBoundingClientRect();
      const x = Math.round(r.left + r.width / 2), y = Math.round(r.top + r.height / 2);
      const cardOf = (n) => {{
        let c = n;
        for (let d = 0; d < 8 && c; d++) {{
          if (c.getAttribute && c.getAttribute('aria-label')) return c;
          c = c.parentElement;
        }}
        return null;
      }};
      const card = cardOf(el.parentElement);
      const hit = document.elementFromPoint(x, y);
      // 命中元素属于目标卡：本身/互为祖先后代，或在目标卡容器内（容器须是单张卡大小，不能是整个网格）
      const cardIsSingle = !!card && card.getBoundingClientRect().width < window.innerWidth * 0.6;
      const onTarget = !!hit && (hit === el || el.contains(hit) || hit.contains(el) || (cardIsSingle && card.contains(hit)));
      return {{ x, y, onTarget, label: ((card && card.getAttribute('aria-label')) || '').slice(0, 80) }};
    }}, item.key || item.src);
    const readDetailHeading = () => page.evaluate(() => {{
      const h = [...document.querySelectorAll('h1,h2,h3,[role="heading"]')].map(e => (e.innerText || '').trim()).find(Boolean);
      return (h || '').slice(0, 80);
    }});
    let opened = null;
    const openAttempts = [];
    for (let attempt = 0; attempt < 3 && !opened; attempt++) {{
      if (attempt > 0) {{
        // 重试前回到网格并切回对应类型栏
        if ((await page.evaluate(() => location.pathname)).includes('/edit/')) {{
          await page.evaluate(() => history.back());
          await page.waitForTimeout(1500);
        }}
        await selectMediaTab();
      }}
      let pos = await locateCard();
      if (pos) {{
        await page.waitForTimeout(400);
        pos = await locateCard();  // 滚动、布局稳定后再量一次，以这次坐标为准
      }}
      if (!pos || !pos.onTarget) {{
        openAttempts.push({{ attempt, error: pos ? 'not_on_target' : 'card_not_found' }});
        await page.waitForTimeout(1500);
        continue;
      }}
      await page.mouse.click(pos.x, pos.y, {{ label: "open generated video detail" }});
      try {{
        await page.waitForFunction(
          () => !!document.querySelector('button[aria-label="下载媒体内容"]') || !!document.querySelector('button[aria-label="下载"]'),
          undefined,
          {{ timeout: 15000 }}
        );
      }} catch (_) {{
        openAttempts.push({{ attempt, error: 'detail_not_opened' }});
        continue;
      }}
      await page.waitForTimeout(500);
      const heading = await readDetailHeading();
      const expectLabel = item.label || pos.label;
      if (expectLabel && heading && heading !== expectLabel) {{
        openAttempts.push({{ attempt, error: 'heading_mismatch', heading, expect: expectLabel }});
        continue;
      }}
      opened = {{ heading, label: expectLabel }};
    }}
    if (!opened) {{
      throw new Error("RESULT_PICK_MISMATCH: 没能打开新生成视频的详情页（点数已扣，不要重跑）：" + JSON.stringify(openAttempts));
    }}

    const downloadPromise = page.waitForEvent("download", {{ timeout: 90000 }});
    const clickedDl = await page.evaluate(() => {{
      const btn = document.querySelector('button[aria-label="下载媒体内容"]') || document.querySelector('button[aria-label="下载"]');
      if (btn) {{ btn.click(); return true; }}
      return false;
    }});
    if (!clickedDl) throw new Error("视频详情页未找到下载按钮");

    // 某些情况下点击下载会弹出分辨率菜单（如 720p / 1080p），等待短暂时间检查是否有菜单项
    let downloadObj = null;
    try {{
      downloadObj = await Promise.race([
        downloadPromise,
        new Promise((_, reject) => setTimeout(() => reject(new Error("CHECK_SUBMENU")), 3000)),
      ]);
    }} catch (e) {{
      if (String(e.message) === "CHECK_SUBMENU") {{
        const subClicked = await page.evaluate(() => {{
          const items = [...document.querySelectorAll('[role="menuitem"]')];
          const preferred = items.find(el => el.innerText.includes('原始') || el.innerText.includes('720p') || el.innerText.includes('下载')) || items[0];
          if (preferred) {{ preferred.click(); return preferred.innerText.trim(); }}
          return null;
        }});
        if (!subClicked) {{
          // 没有子菜单则继续等待原 downloadPromise
        }}
        downloadObj = await downloadPromise;
      }} else {{
        throw e;
      }}
    }}

    const suggested = downloadObj.suggestedFilename() || `${{prefix}}${{indexSuffix}}.mp4`;
    const ext = (path.extname(suggested) || '.mp4').toLowerCase();
    if (!['.mp4', '.webm', '.mov', '.m4v'].includes(ext)) {{
      throw new Error(`视频模式误下载了非视频文件（${{suggested}}），已拦截以避免冒充视频成片`);
    }}
    const finalPath = path.join(outDir, `${{prefix}}${{indexSuffix}}${{ext}}`);
    await downloadObj.saveAs(finalPath);
    const stat = await fs.stat(finalPath);
    saved.push({{
      path: finalPath,
      bytes: stat.size,
      suggested_filename: suggested,
      source_url: item.src,
      card_label: opened.label || '',
      detail_heading: opened.heading || '',
    }});

    // 返回项目网格页，准备下载下一条（如有）
    if (i < newItems.length - 1) {{
      await page.goto(CFG.resolved_project_url, {{ timeout: 30000 }});
      await page.waitForFunction(() => !!document.querySelector('button[aria-label="设置触发器"]'), undefined, {{ timeout: 25000 }});
      await page.waitForTimeout(1000);
    }}
  }}

  return saved;
}}

let payload = {{ ok: false }};
try {{
  const projectUrl = await ensureProject();
  CFG.resolved_project_url = projectUrl;

  const creditsBefore = await readCreditsQuick();
  const settings = await configureSettings();

  if (settings.estimated_credits === null && !CFG.allow_unknown_cost) {{
    payload = {{
      ok: false,
      error: "unknown_cost",
      message: "未能识别页面上的预估积分消耗，出于安全保护已中止生成。可加 --allow-unknown-cost 显式放行。",
      project_url: projectUrl,
      credits_before: creditsBefore,
      settings,
    }};
  }} else if (settings.estimated_credits !== null && settings.estimated_credits > CFG.max_credits) {{
    payload = {{
      ok: false,
      error: "budget_exceeded",
      message: `预估消耗 ${{settings.estimated_credits}} 点数，超过允许上限 --max-credits ${{CFG.max_credits}}，已自动拦截未提交生成。`,
      project_url: projectUrl,
      credits_before: creditsBefore,
      estimated_credits: settings.estimated_credits,
      max_credits: CFG.max_credits,
      settings,
    }};
  }} else if (CFG.dry_run) {{
    // 帧模式干跑也真实上传并核对首/尾帧绑定（不提交、不扣点），提前暴露绑定失败
    const frameBinding = (CFG.mode === 'video' && CFG.video_type === '帧' && CFG.ref_images.length)
      ? await bindVideoFrames(CFG.ref_images) : null;
    // 帧模式干跑同时拍一次生成前基线，核对网格最新几张卡都已纳入（真跑靠它判断哪张是新成品）
    let baselinePreview = null;
    if (frameBinding) {{
      const bl = await captureBaseline(true);
      baselinePreview = {{ count: bl.length, first: gridPreview(bl) }};
    }}
    payload = {{
      ok: true,
      dry_run: true,
      project_url: projectUrl,
      credits_before: creditsBefore,
      estimated_credits: settings.estimated_credits,
      max_credits: CFG.max_credits,
      settings,
      frame_binding: frameBinding,
      baseline_preview: baselinePreview,
    }};
  }} else {{
    // 若需要上传参考图，先上传
    const isFrameMode = CFG.mode === 'video' && CFG.video_type === '帧';
    const frameBinding = (isFrameMode && CFG.ref_images.length) ? await bindVideoFrames(CFG.ref_images) : null;
    const uploadedRefs = isFrameMode ? CFG.ref_images.slice() : (CFG.ref_images.length ? await uploadRefImages(CFG.ref_images) : []);

    // 切换到对应类型导航，获取生成前已有媒体基线（避免把参考图或旧素材误判为新生成成品）
    const baseline = await captureBaseline(uploadedRefs.length > 0);
    const beforeSet = new Set(baseline.map(x => x.key));

    await fillPrompt(CFG.prompt);

    // 提交前记录阻断关键词与进度百分比基线，历史失败卡片和页脚 reCAPTCHA 声明都会计入基线而不再误报
    const preSubmitText = await readBodyText();
    const blockBaseline = countKeywords(preSubmitText);
    // 生成中判定：页面上“NN%”进度比提交前多，或出现“正在生成”
    // （旧正则用单词边界包住“NN%”，% 后接空格/换行时永远不命中，曾导致没生成完就收集）
    const countProgress = (txt) => (txt.match(/(^|[^0-9])[0-9]{{1,2}}%/gm) || []).length;
    const progressBaseline = countProgress(preSubmitText);
    const isGenerating = (txt) => countProgress(txt) > progressBaseline || txt.includes('正在生成');

    // 点击开始生成（优先精确 aria-label 真实点击）
    await page.click('css=button[aria-label="开始生成"]', {{ label: "submit Flow generation", timeout: 8000 }});
    const submittedAt = Date.now();
    // 提交后先短暂缓冲，让生成任务卡片进入队列并避免误捕瞬时渲染的参考图
    await page.waitForTimeout(4000);
    // 新版提交后会自动跳回“所有媒体”，视频封面会被误当成新图；切回对应类型栏再找新素材
    await selectMediaTab();

    // 轮询等待新素材生成完成
    const deadline = Date.now() + CFG.timeout_sec * 1000;
    let newItems = [];
    let lastProgress = null;
    let pickDiag = null;
    const isExpectedAspect = (item) => {{
      if (CFG.mode !== 'image') return true;
      if (CFG.aspect === '16:9' || CFG.aspect === '4:3') return item.w > item.h;
      if (CFG.aspect === '9:16' || CFG.aspect === '3:4') return item.h > item.w;
      return true;
    }};
    const filterFresh = (list) => {{
      const candidates = list.filter(item => {{
        if (beforeSet.has(item.key) || item.w < 256 || item.h < 256) return false;
        if (CFG.mode === 'video' && !item.isVideo) return false;
        if (CFG.mode === 'image' && item.isVideo) return false;
        return isExpectedAspect(item);
      }});
      // 历史项目网格多行虚拟滚动时，新生成的素材永远插在顶部区域，过滤掉底部懒加载冒出来的老图
      if (baseline.length >= 6) {{
        const topOnly = candidates.filter(item => item.top >= -50 && item.top <= 520);
        if (topOnly.length) return topOnly;
        return [];
      }}
      return candidates;
    }};
    // 视频卡片要“落定”才收集：位置稳定 ≥5 秒且已有标签（成品才有 AI 标题）；标签迟迟不出现时稳定 60 秒也放行
    const firstSeen = new Map();
    const settledFresh = (fresh) => {{
      const now = Date.now();
      for (const it of fresh) {{
        const seen = firstSeen.get(it.key);
        if (!seen || seen.top !== it.top || seen.left !== it.left) firstSeen.set(it.key, {{ at: now, top: it.top, left: it.left }});
      }}
      if (CFG.mode !== 'video') return fresh;
      return fresh.filter(it => {{
        const age = now - firstSeen.get(it.key).at;
        return age >= 5000 && (Boolean(it.label) || age >= 60000);
      }});
    }};
    while (Date.now() < deadline) {{
      await page.waitForTimeout(2500);
      const bodyText = await readBodyText();
      const blocker = detectNewBlockers(bodyText, blockBaseline, CFG.prompt);
      if (blocker) {{
        throw new Error("页面出现新的阻断/错误提示：" + JSON.stringify(blocker));
      }}
      const current = await snapshotGridMedia();
      const fresh = filterFresh(current);
      let ready = settledFresh(fresh);
      if (CFG.mode === 'video') {{
        // 第二道防护：新成品必须是视频网格最前 N 张；排在后面的“新卡”只可能是主键没对上的旧视频
        const topKeys = new Set(gridOrder(current).slice(0, CFG.count).map(x => x.key));
        ready = ready.filter(x => topKeys.has(x.key));
      }}
      const stillGenerating = isGenerating(bodyText);
      lastProgress = {{ freshCount: fresh.length, readyCount: ready.length, stillGenerating }};
      if (ready.length >= CFG.count && !stillGenerating) {{
        newItems = ready.slice(0, CFG.count);
        pickDiag = {{
          after_submit_s: Math.round((Date.now() - submittedAt) / 1000),
          picked: gridPreview(newItems, CFG.count),
          grid_first: gridPreview(CFG.mode === 'video' ? gridOrder(current) : current),
          baseline_first: gridPreview(CFG.mode === 'video' ? gridOrder(baseline) : baseline),
          // 选中卡片的 AI 标题在生成前就出现过：可能是旧视频（同一提示词重跑也可能撞标题，只提醒不判失败）
          label_seen_before: newItems.some(n => n.label && baseline.some(b => b.label === n.label)),
        }};
        break;
      }}
    }}

    if (!newItems.length) {{
      throw new Error(`等待生成超时（${{CFG.timeout_sec}}s），最后观测状态：${{JSON.stringify(lastProgress)}}`);
    }}

    const files = await downloadNewItems(newItems, CFG.mode, CFG.out_dir, CFG.file_prefix);
    const creditsAfter = await readCreditsQuick();

    payload = {{
      ok: true,
      dry_run: false,
      mode: CFG.mode,
      project_url: projectUrl,
      prompt: CFG.prompt,
      uploaded_refs: uploadedRefs,
      frame_binding: frameBinding,
      pick_diag: pickDiag,
      credits_before: creditsBefore,
      estimated_credits: settings.estimated_credits,
      credits_after: creditsAfter,
      settings,
      files,
    }};
  }}
}} catch (err) {{
  const msg = String(err && err.stack ? err.stack : err);
  payload = {{
    ok: false,
    error: msg.includes("NOT_LOGGED_IN:") ? "not_logged_in"
      : msg.includes("FRAME_BIND_FAILED:") ? "frame_bind_failed"
      : msg.includes("RESULT_PICK_MISMATCH:") ? "result_pick_mismatch"
      : "runtime_error",
    message: msg,
  }};
}} finally {{
  await restoreAgentMode();
  if (!__REUSED_SPACE__) {{
    try {{
      await task.finish({{ keep: [] }});
    }} catch (_) {{}}
  }}
}}
console.log(RESULT_MARKER + JSON.stringify(payload));
"""


# ---------- 生图直连：页面内取 reCAPTCHA 令牌后直接 POST ogiZ0b，不再模拟填词/点击/轮询 ----------
# 请求线格式与回包解析移植自 gflow-cli（MIT，https://github.com/ffroliva/gflow-cli，提交 6eadc3d / v0.106.0）：
#   src/gflow_cli/api/transports/frame_builder.py（build_ogiz0b_body）
#   src/gflow_cli/api/transports/batchexecute.py（_wrb_rows / parse_frames / RpcError）
#   src/gflow_cli/api/transports/direct_http.py（模型线名 BELUGA、reCAPTCHA action、401/403 才可回退）
# Flow 改版导致直连失效时，先对照上游这几个文件的更新再改这里。
GFLOW_UPSTREAM_REF = "ffroliva/gflow-cli@6eadc3d (v0.106.0)"

# 直连只放行已实测过的组合；其余一律走页面自动化
DIRECT_IMAGE_MODELS: dict[str, str] = {"nano banana 2.1": "BELUGA"}
DIRECT_ASPECT_WIRE: dict[str, int] = {"1:1": 1, "9:16": 2, "16:9": 3, "3:4": 4, "4:3": 5}
TRANSPORTS = ("auto", "direct", "ui")

PURE_DIRECT_JS = r"""
const DIRECT_UNUSUAL_ACTIVITY = 'PUBLIC_ERROR_UNUSUAL_ACTIVITY';
const DIRECT_SAFETY_REASONS = [
  'PUBLIC_ERROR_UNSAFE_GENERATION', 'PUBLIC_ERROR_UNSAFE_CONTENT',
  'PUBLIC_ERROR_UNSAFE_FACE', 'PUBLIC_ERROR_UNSAFE_IDENTITY',
];

// ogiZ0b 请求体：每个 seed 对应一张图，一次 POST 出 count 张
function buildOgiz0bBody({ prompt, model, projectId, token, at, batchId, seeds, aspectWire }) {
  if (!Array.isArray(seeds) || !seeds.length) throw new Error('seeds 至少要有一个');
  const ctx = [null, 22, null, null, null, projectId, null, null, null, null, [token, 1]];
  const entries = seeds.map((seed) => [
    null, null, null, seed, aspectWire, model, null, ctx.slice(), [[[prompt]]], null, null, null,
  ]);
  const inner = [null, entries, 1, ctx, [batchId]];
  const fReq = JSON.stringify([[['ogiZ0b', JSON.stringify(inner), null, 'generic']]]);
  return new URLSearchParams({ 'f.req': fReq, at }).toString();
}

// batchexecute 回包里所有 ["wrb.fr", rpcid, ...] 行；长度行跳过，每个以 [ 开头的行单独解析
function wrbRows(text) {
  let body = String(text || '').trimStart();
  if (body.startsWith(")]}'")) body = body.slice(4);
  const rows = [];
  for (const raw of body.split('\n')) {
    const line = raw.trim();
    if (!line.startsWith('[')) continue;
    let decoded;
    try { decoded = JSON.parse(line); } catch (_) { continue; }
    if (!Array.isArray(decoded)) continue;
    for (const item of decoded) {
      if (Array.isArray(item) && item[0] === 'wrb.fr') rows.push(item);
    }
  }
  return rows;
}

function collectFifeUrls(node, out) {
  if (typeof node === 'string') {
    if (/^https:\/\/flow-content\.google\/image\/[A-Za-z0-9-]+/.test(node)) out.push(node);
  } else if (Array.isArray(node)) {
    for (const n of node) collectFifeUrls(n, out);
  } else if (node && typeof node === 'object') {
    for (const n of Object.values(node)) collectFifeUrls(n, out);
  }
}

// 解析 ogiZ0b 回包：成功时给出每张图的 media_id 与签名原图链接；payload 为 null 的是错误信封
function parseOgiz0bReply(text, rpcid = 'ogiZ0b') {
  const images = [];
  const errors = [];
  for (const row of wrbRows(text)) {
    if (row[1] !== rpcid) continue;
    if (typeof row[2] === 'string') {
      let payload = null;
      try { payload = JSON.parse(row[2]); } catch (_) {}
      const urls = [];
      collectFifeUrls(payload, urls);
      for (const url of urls) {
        const m = url.match(/\/image\/([A-Za-z0-9-]+)/);
        images.push({ media_id: m ? m[1] : null, url });
      }
    } else {
      const status = Array.isArray(row[5]) ? row[5] : [];
      const reasons = [];
      for (const d of (Array.isArray(status[2]) ? status[2] : [])) {
        if (Array.isArray(d) && Array.isArray(d[1])) reasons.push(...d[1].filter((r) => typeof r === 'string'));
      }
      errors.push({ code: status[0] ?? null, reasons });
    }
  }
  const seen = new Set();
  const uniq = images.filter((img) => {
    const key = img.media_id || img.url;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
  let refusal = null;
  for (const e of errors) {
    if (e.reasons.includes(DIRECT_UNUSUAL_ACTIVITY)) { refusal = { kind: 'waf', ...e }; break; }
    const safety = e.reasons.find((r) => DIRECT_SAFETY_REASONS.includes(r));
    if (safety) { refusal = { kind: 'content_policy', reason: safety, ...e }; break; }
  }
  return { images: uniq, errors, refusal };
}
"""

DIRECT_IMAGE_JS_TEMPLATE = r"""
const RESULT_MARKER = __RESULT_MARKER__;
const CFG = __CFG__;
__PURE_DIRECT_JS__
__BOOTSTRAP__
__ENSURE_PROJECT_JS__

// 发出请求前的失败：Flow 一定没收到，可以安全回退到页面自动化
function directFallback(reason) {
  const e = new Error(reason);
  e.directFallback = true;
  return e;
}

const T0 = Date.now();
const timings = {};
let payload = { ok: false };
try {
  const projectUrl = await ensureProject();
  CFG.resolved_project_url = projectUrl;
  timings.project_ms = Date.now() - T0;
  const parsedUrl = new URL(projectUrl);
  const projectId = (parsedUrl.pathname.match(/\/project\/([0-9a-fA-F-]{36})/) || [])[1];
  if (parsedUrl.host !== 'flow.google.com') throw directFallback('served_on_' + parsedUrl.host);
  if (!projectId) throw directFallback('no_project_id');

  try {
    await page.waitForFunction(
      () => !!(window.grecaptcha && window.grecaptcha.enterprise && window.WIZ_global_data && window.WIZ_global_data.SNlM0e),
      undefined, { timeout: 20000 });
  } catch (_) {
    throw directFallback('page_not_ready');
  }

  // 取 reCAPTCHA 令牌（不花费任何东西，页面脚本刚加载时允许重试 10 秒）与会话令牌
  const prep = await page.evaluate(async (action) => {
    const m = [...document.querySelectorAll('script[src*="recaptcha/enterprise.js"]')]
      .map((s) => (s.getAttribute('src') || '').match(/[?&]render=([^&]+)/)).find(Boolean);
    if (!m) return { error: 'no_site_key' };
    const w = window.WIZ_global_data || {};
    if (!w.SNlM0e) return { error: 'no_at_token' };
    const deadline = Date.now() + 10000;
    let lastErr = '';
    while (Date.now() < deadline) {
      try {
        const started = performance.now();
        const token = await new Promise((resolve, reject) => {
          const timer = setTimeout(() => reject(new Error('mint_timeout')), 8000);
          grecaptcha.enterprise.ready(() => {
            grecaptcha.enterprise.execute(m[1], { action })
              .then((t) => { clearTimeout(timer); resolve(t); })
              .catch((err) => { clearTimeout(timer); reject(err); });
          });
        });
        if (token) {
          return { token, at: w.SNlM0e, fsid: w.FdrFJe || '', bl: w.cfb2h || '', mint_ms: Math.round(performance.now() - started) };
        }
      } catch (err) {
        lastErr = String((err && err.message) || err);
      }
      await new Promise((r) => setTimeout(r, 500));
    }
    return { error: 'mint_failure:' + lastErr.slice(0, 120) };
  }, 'IMAGE_GENERATION');
  if (prep.error) throw directFallback(prep.error);
  timings.mint_ms = prep.mint_ms;

  const seeds = Array.from({ length: CFG.count }, () => 1 + Math.floor(Math.random() * 2147483646));
  const body = buildOgiz0bBody({
    prompt: CFG.prompt, model: CFG.model_wire, projectId, token: prep.token, at: prep.at,
    batchId: crypto.randomUUID().toUpperCase(), seeds, aspectWire: CFG.aspect_wire,
  });
  const qs = new URLSearchParams({
    rpcids: 'ogiZ0b', 'source-path': '/project/' + projectId, bl: prep.bl, 'f.sid': prep.fsid,
    hl: 'en', _reqid: String(1000 + Math.floor(Math.random() * 9000)), rt: 'c',
  }).toString();

  // 只发一次，绝不重发：请求一旦送出，Flow 可能已经生成
  const postStart = Date.now();
  let resp;
  try {
    resp = await page.fetch('/_/AiSandboxAngularFrontend/data/batchexecute?' + qs, {
      method: 'POST',
      headers: { 'content-type': 'application/x-www-form-urlencoded;charset=UTF-8', 'x-same-domain': '1' },
      body,
      timeout: CFG.post_timeout_ms,
    });
  } catch (err) {
    const e = new Error('请求已发出但未拿到回包（Flow 可能已生成，请先到项目里核对，不要直接重跑）：' + String((err && err.message) || err).slice(0, 200));
    e.directSent = true;
    throw e;
  }
  timings.post_ms = Date.now() - postStart;
  const status = resp.status;
  const finalUrl = String(resp.url || '');
  // 401/403/重定向：Flow 拒收了会话或令牌，没有生成任何东西，可以回退
  if (status === 401 || status === 403 || (status >= 300 && status < 400)) throw directFallback('http_' + status);
  if (finalUrl && !finalUrl.includes('/batchexecute')) throw directFallback('redirected');
  const text = String(resp.body || '');
  const parsed = status === 200 ? parseOgiz0bReply(text) : { images: [], errors: [], refusal: null };
  const base = {
    transport: 'direct', mode: 'image', project_url: projectUrl, prompt: CFG.prompt,
    settings: { model: CFG.model, aspect: CFG.aspect, count: CFG.count }, timings,
  };
  if (status !== 200) {
    payload = { ...base, ok: false, error: 'direct_http_error',
      message: `Flow 返回 HTTP ${status}（请求已发出，未自动重试；请先到项目里核对）`, resp_head: text.slice(0, 300) };
  } else if (parsed.refusal) {
    const waf = parsed.refusal.kind === 'waf';
    payload = { ...base, ok: false, error: waf ? 'flow_unusual_activity' : 'content_policy',
      message: waf
        ? 'Flow 判定异常活动（PUBLIC_ERROR_UNUSUAL_ACTIVITY），未生成也未扣费；与提示词无关，过一段时间再试。'
        : `Flow 内容安全拒绝：${parsed.refusal.reason}，请修改提示词。`,
      refusal: parsed.refusal };
  } else if (!parsed.images.length) {
    payload = { ...base, ok: false, error: 'direct_unparsed_reply',
      message: 'Flow 返回 200 但没解析出图片（可能已生成，请先到项目里核对，不要直接重跑）', resp_head: text.slice(0, 300) };
  } else {
    payload = { ...base, ok: true, dry_run: false, images: parsed.images };
    if (parsed.images.length < CFG.count) payload.warning = `请求 ${CFG.count} 张，只返回 ${parsed.images.length} 张`;
  }
} catch (err) {
  const msg = String(err && err.stack ? err.stack : err);
  if (err && err.directFallback) {
    payload = { ok: false, transport: 'direct', fallback: true, fallback_reason: err.message, timings };
  } else {
    payload = {
      ok: false, transport: 'direct',
      error: msg.includes('NOT_LOGGED_IN:') ? 'not_logged_in' : (err && err.directSent ? 'direct_post_unknown' : 'runtime_error'),
      message: (err && err.directSent) ? err.message : msg, timings,
    };
  }
} finally {
  if (!__REUSED_SPACE__) {
    try { await task.finish({ keep: [] }); } catch (_) {}
  }
}
timings.total_ms = Date.now() - T0;
console.log(RESULT_MARKER + JSON.stringify(payload));
"""


def build_direct_image_js(guard_state: dict[str, Any], cfg: dict[str, Any]) -> str:
    """构建生图直连脚本：定位专用项目 → 页面内取令牌 → 直接 POST ogiZ0b → 返回原图签名链接。"""
    # CFG 放最后替换：提示词里即使碰巧含占位符文本，也不会被再次展开
    replacements = {
        "__RESULT_MARKER__": json.dumps(RESULT_MARKER),
        "__PURE_DIRECT_JS__": PURE_DIRECT_JS,
        "__BOOTSTRAP__": build_space_bootstrap_js(guard_state, "flow studio image direct"),
        "__ENSURE_PROJECT_JS__": ENSURE_PROJECT_JS,
        "__CFG__": json.dumps(cfg, ensure_ascii=False),
    }
    js = DIRECT_IMAGE_JS_TEMPLATE
    for key, value in replacements.items():
        js = js.replace(key, value)
    return js


MODE_DEFAULTS: dict[str, dict[str, Any]] = {
    "image": {"model": "Nano Banana 2.1", "aspect": "16:9", "max_credits": 0, "timeout": 180},
    "video": {
        "model": "Omni 1.2 Flash",
        "aspect": "16:9",
        "max_credits": 100,
        "timeout": 360,
        "video_type": "素材",
        "resolution": "720p",
    },
}
BASE_PARAMS: dict[str, Any] = {
    "count": 1,
    "thinking": None,
    "video_type": None,
    "resolution": None,
    "duration": None,
    "ref_image": [],
    "name": None,
    "project_url": None,
    "project_name": DEFAULT_PROJECT_NAME,
    "allow_unknown_cost": False,
    "dry_run": False,
    "transport": "auto",
}
PLAN_KEYS = {
    "id", "mode", "prompt", "aspect", "count", "model", "thinking", "video_type",
    "resolution", "duration", "ref_image", "name", "max_credits", "timeout", "project_url",
    "project_name", "allow_unknown_cost", "transport",
}
PREVIEW_DIRNAME = "_preview"
IMAGE_PREVIEW_EDGE = 512
MESSAGE_LIMIT = 500


# ---------- 落盘文件后处理：真实尺寸 + 低成本预览图 ----------


def run_quiet(cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess[str] | None:
    """执行外部命令，失败或超时返回 None，不抛异常。"""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except Exception:
        return None


def probe_image_size(path: str) -> tuple[int | None, int | None]:
    """用 macOS 自带 sips 读取已下载图片的真实像素尺寸；不可用时返回 (None, None)。"""
    sips = shutil.which("sips")
    if not sips:
        return None, None
    proc = run_quiet([sips, "-g", "pixelWidth", "-g", "pixelHeight", path], timeout=15)
    out = proc.stdout if proc else ""
    w = re.search(r"pixelWidth:\s*(\d+)", out)
    h = re.search(r"pixelHeight:\s*(\d+)", out)
    return (int(w.group(1)) if w else None, int(h.group(1)) if h else None)


def probe_video_meta(path: str) -> dict[str, Any]:
    """用 ffprobe 读取视频分辨率与时长；不可用时返回空字典。"""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return {}
    proc = run_quiet(
        [
            ffprobe, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height:format=duration", "-of", "json", path,
        ],
        timeout=30,
    )
    if not proc or proc.returncode != 0:
        return {}
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}
    stream = (data.get("streams") or [{}])[0]
    meta: dict[str, Any] = {}
    if stream.get("width"):
        meta["width"] = int(stream["width"])
    if stream.get("height"):
        meta["height"] = int(stream["height"])
    duration = (data.get("format") or {}).get("duration")
    if duration:
        meta["duration"] = round(float(duration), 2)
    return meta


def make_image_preview(path: str) -> str | None:
    """生成长边 512px 的 JPEG 缩略图（约 200 Token），Agent 需要检查时只看这张。"""
    src = Path(path)
    out = src.parent / PREVIEW_DIRNAME / f"{src.stem}.jpg"
    out.parent.mkdir(parents=True, exist_ok=True)
    sips = shutil.which("sips")
    ffmpeg = shutil.which("ffmpeg")
    if sips:
        proc = run_quiet(
            [sips, "-Z", str(IMAGE_PREVIEW_EDGE), "-s", "format", "jpeg",
             "-s", "formatOptions", "70", str(src), "--out", str(out)],
        )
    elif ffmpeg:
        edge = IMAGE_PREVIEW_EDGE
        proc = run_quiet(
            [ffmpeg, "-v", "error", "-y", "-i", str(src), "-vf",
             f"scale={edge}:{edge}:force_original_aspect_ratio=decrease", "-q:v", "5", str(out)],
        )
    else:
        return None
    ok = proc is not None and proc.returncode == 0 and out.exists() and out.stat().st_size > 0
    return str(out) if ok else None


def make_video_sheet(path: str, meta: dict[str, Any]) -> str | None:
    """均匀抽 4 帧拼成一张小图（横版 2x2 共 640px 宽、竖版 4x1 共 360px 高，约 300~400 Token）。"""
    ffmpeg = shutil.which("ffmpeg")
    duration = meta.get("duration")
    if not ffmpeg or not duration:
        return None
    src = Path(path)
    out = src.parent / PREVIEW_DIRNAME / f"{src.stem}_sheet.jpg"
    out.parent.mkdir(parents=True, exist_ok=True)
    portrait = (meta.get("height") or 0) > (meta.get("width") or 0)
    scale, tile = ("-2:360", "4x1") if portrait else ("320:-2", "2x2")
    fps = 4 / max(float(duration), 0.5)
    proc = run_quiet(
        [ffmpeg, "-v", "error", "-y", "-i", str(src), "-vf",
         f"fps={fps:.5f},scale={scale},tile={tile}", "-frames:v", "1", "-q:v", "5", str(out)],
        timeout=120,
    )
    ok = proc is not None and proc.returncode == 0 and out.exists() and out.stat().st_size > 0
    return str(out) if ok else None


def postprocess_files(result: dict[str, Any], mode: str) -> None:
    """补全落盘文件的真实尺寸/时长，并生成低成本预览图。"""
    for item in result.get("files") or []:
        file_path = item.get("path")
        if not file_path or not Path(file_path).is_file():
            continue
        if mode == "image":
            item["width"], item["height"] = probe_image_size(file_path)
            item["preview"] = make_image_preview(file_path)
        else:
            meta = probe_video_meta(file_path)
            item.update(meta)
            item["preview"] = make_video_sheet(file_path, meta)


# ---------- 精简输出：默认只给 Agent 必要字段，--verbose 才输出全量 ----------


def truncate(text: Any, limit: int = MESSAGE_LIMIT) -> str:
    s = str(text or "")
    return s if len(s) <= limit else s[:limit] + f"…（已截断，共 {len(s)} 字，--verbose 查看全文）"


def clean_model_name(text: Any) -> str:
    """去掉模型按钮里的 emoji 与图标文字，如“🍌 Nano Banana 2.1”。"""
    s = str(text or "").replace("arrow_drop_down", "")
    return re.sub(r"^[^\w]+", "", s).strip()


def settings_warnings(settings: dict[str, Any]) -> list[str]:
    """列出请求了但页面上没找到、因此未生效的设置项。"""
    warnings = []
    for key in ("mode", "aspect", "count", "video_type", "resolution", "duration", "thinking"):
        value = settings.get(key)
        if not (isinstance(value, dict) and value.get("found") is False):
            continue
        # 模型本身没有分辨率选项（如 Veo 3.1 - Lite）且请求的是默认 720p 时不算异常
        if (
            key == "resolution"
            and "available" not in value
            and value.get("optionText") == MODE_DEFAULTS["video"]["resolution"]
        ):
            continue
        warnings.append(f"{key}={value.get('optionText')} 未生效（当前模型可能不支持）")
    return warnings


def compact_result(result: dict[str, Any]) -> dict[str, Any]:
    """把生成结果压缩成 Agent 需要的最小字段集。"""
    ok = bool(result.get("ok"))
    out: dict[str, Any] = {"ok": ok}
    if result.get("dry_run"):
        out["dry_run"] = True
    if not ok:
        out["error"] = result.get("error") or "unknown"
        out["message"] = truncate(result.get("message"))
    settings = result.get("settings") or {}
    model = clean_model_name(settings.get("model"))
    if model:
        out["model"] = model
    estimated = result.get("estimated_credits", settings.get("estimated_credits"))
    credits = {
        k: v
        for k, v in (
            ("before", result.get("credits_before")),
            ("estimated", estimated),
            ("after", result.get("credits_after")),
        )
        if v is not None
    }
    if credits:
        out["credits"] = credits
    files = []
    for item in result.get("files") or []:
        entry = {k: item.get(k) for k in ("path", "preview", "variant", "width", "height", "duration", "first_frame_ssim", "last_frame_ssim")}
        files.append({k: v for k, v in entry.items() if v is not None})
    if files:
        out["files"] = files
    warnings = settings_warnings(settings)
    if result.get("warning"):
        warnings.append(str(result["warning"]))
    if warnings:
        out["warnings"] = warnings
    # 帧模式绑定核对、生成前基线、选中成品的诊断：字段很小，直接透传
    for key in ("frame_binding", "baseline_preview", "pick_diag"):
        if result.get(key):
            out[key] = result[key]
    if result.get("transport"):
        out["transport"] = result["transport"]
    if result.get("direct_fallback_reason"):
        out["direct_fallback"] = result["direct_fallback_reason"]
    seconds = (result.get("timings") or {}).get("total_ms")
    if seconds:
        out["seconds"] = round(seconds / 1000, 1)
    guard = (result.get("window_guard_after") or {}).get("action")
    if guard:
        out["guard_after"] = guard
    if result.get("notices"):
        out["notices"] = [truncate(n, 200) for n in result["notices"]]
    return out


def compact_credits(result: dict[str, Any]) -> dict[str, Any]:
    """点数查询的精简输出（不回显邮箱）。"""
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error") or "unknown", "message": truncate(result.get("message"))}
    out = {
        "ok": True,
        "tier": result.get("tier"),
        "credits": result.get("credits"),
        "reset": str(result.get("reset_text") or "").replace("重置时间：", "") or None,
        "visible_watermark": result.get("visible_watermark"),
    }
    guard = (result.get("window_guard_after") or {}).get("action")
    if guard:
        out["guard_after"] = guard
    if result.get("notices"):
        out["notices"] = [truncate(n, 200) for n in result["notices"]]
    return {k: v for k, v in out.items() if v is not None}


def emit(obj: dict[str, Any], verbose: bool = False) -> None:
    """默认输出单行紧凑 JSON 以减少 Token；--verbose 时缩进输出。"""
    if verbose:
        print(json.dumps(obj, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))


# ---------- 单次生成 ----------


def sanitize_slug(text: str, max_len: int = 28) -> str:
    """将提示词转成适合文件名的安全前缀。"""
    cleaned = re.sub(r"[^a-zA-Z0-9一-鿿]+", "_", text).strip("_")
    return (cleaned[:max_len] or "flow_asset").lower()


UPLOAD_CACHE_DIR = Path.home() / ".cache" / "flow_studio" / "uploads"
# 实测校准：首帧/尾帧正确时 720p 约 0.88、360p 约 0.57~0.65（分辨率低拉低分数）；用错图 0.10~0.21（相近的码头场景也只有 0.21）
FRAME_SSIM_MIN = 0.4
DURATION_TOLERANCE_S = 1.5  # 实测请求 4 秒下载为 4.03 秒


def unique_upload_copy(src: Path) -> str:
    """复制成 `<原名>-<内容哈希8位><扩展名>`：文件名唯一对应内容，避免点选到同名旧图。"""
    digest = hashlib.sha256(src.read_bytes()).hexdigest()[:8]
    UPLOAD_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    dst = UPLOAD_CACHE_DIR / f"{src.stem}-{digest}{src.suffix.lower()}"
    if not dst.exists():
        shutil.copyfile(src, dst)
    return str(dst)


def frame_similarity(video: str, image: str, at_end: bool = False) -> float | None:
    """视频首帧（或末帧）与图片的灰度 SSIM；ffmpeg 不可用或失败时返回 None。"""
    seek = ["-sseof", "-0.2"] if at_end else []
    graph = "[0:v]trim=end_frame=1,scale=320:180,format=gray[a];[1:v]scale=320:180,format=gray[b];[a][b]ssim=stats_file=-"
    proc = run_quiet(["ffmpeg", "-v", "error", *seek, "-i", video, "-i", image, "-filter_complex", graph, "-f", "null", "-"], timeout=60)
    if not proc:
        return None
    m = re.search(r"All:([0-9.]+)", proc.stdout or "")
    return round(float(m.group(1)), 3) if m else None


def verify_frames(result: dict[str, Any], ref_images: list[str]) -> None:
    """帧模式生成后核对：首帧必须像传入的首帧图，否则判失败（批量会就此停下，避免继续扣点）。"""
    mismatched = []
    for item in result.get("files") or []:
        path = item.get("path")
        if not path:
            continue
        item["first_frame_ssim"] = frame_similarity(path, ref_images[0])
        if item["first_frame_ssim"] is not None and item["first_frame_ssim"] < FRAME_SSIM_MIN:
            mismatched.append(f"{Path(path).name} 首帧 SSIM={item['first_frame_ssim']}")
        if len(ref_images) > 1:
            item["last_frame_ssim"] = frame_similarity(path, ref_images[1], at_end=True)
            if item["last_frame_ssim"] is not None and item["last_frame_ssim"] < FRAME_SSIM_MIN:
                result["warning"] = f"{Path(path).name} 末帧与尾帧图差异较大（SSIM={item['last_frame_ssim']}）"
    if mismatched:
        result.update(
            ok=False,
            error="first_frame_mismatch",
            message="生成的视频首帧不是传入的首帧图（点数已扣）：" + "；".join(mismatched) + "。批量已停，先排查再续跑。",
        )


def flag_reused_label(result: dict[str, Any]) -> None:
    """选中卡片的标题在生成前就存在时加警告：首帧比对和时长核对分不出同首尾帧、同时长的旧视频。"""
    if not result.get("ok") or not (result.get("pick_diag") or {}).get("label_seen_before"):
        return
    note = f"选中的视频标题在生成前就已存在，可能拿到了旧视频：到专用项目“{DEFAULT_PROJECT_NAME}”视频栏核对第一张"
    result["warning"] = f"{result['warning']}；{note}" if result.get("warning") else note


def verify_duration(result: dict[str, Any], wanted: Any) -> None:
    """视频时长核对：与请求时长相差过大，多半是拿错了项目里的旧视频（点数已扣，不要重跑）。"""
    try:
        want = float(wanted)
    except (TypeError, ValueError):
        return
    bad = [
        f"{Path(item['path']).name} 时长 {item['duration']} 秒"
        for item in result.get("files") or []
        if item.get("path") and isinstance(item.get("duration"), (int, float)) and abs(item["duration"] - want) > DURATION_TOLERANCE_S
    ]
    if bad:
        result.update(
            ok=False,
            error="result_mismatch",
            message=f"下载到的视频与请求的 {want:g} 秒不符（点数已扣）：" + "；".join(bad)
            + f"。多半拿错了项目里的旧视频，不要重跑；到 Flow 专用项目“{DEFAULT_PROJECT_NAME}”视频栏取最新一条。批量已停。",
        )


def direct_model_wire(model: Any) -> str | None:
    """模型名（如“Nano Banana 2.1”）→ 直连线名；未实测过的模型返回 None。"""
    return DIRECT_IMAGE_MODELS.get(clean_model_name(model).lower())


def direct_ineligible_reason(cfg: dict[str, Any]) -> str | None:
    """判断本次请求能否走生图直连；不能时返回原因（只放行已实测过的组合）。"""
    if cfg.get("mode") != "image":
        return "mode_not_image"
    if cfg.get("dry_run"):
        return "dry_run"  # 干跑要读页面上的预估点数，走页面自动化
    if cfg.get("ref_images"):
        return "ref_image"
    if cfg.get("thinking"):
        return "thinking"
    if direct_model_wire(cfg.get("model")) is None:
        return "model_unmeasured"
    if cfg.get("aspect") not in DIRECT_ASPECT_WIRE:
        return "aspect_unmeasured"
    if not 1 <= int(cfg.get("count") or 1) <= 4:
        return "count_out_of_range"
    return None


def download_direct_images(images: list[dict[str, Any]], out_dir: Path, prefix: str) -> list[dict[str, Any]]:
    """下载直连返回的签名原图链接（无需 Cookie；urllib 自动走环境变量或 macOS 系统代理）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    ext_map = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
    files: list[dict[str, Any]] = []
    for i, img in enumerate(images):
        suffix = f"_{i + 1}" if len(images) > 1 else ""
        req = urllib.request.Request(img["url"], headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=90) as resp:
            content_type = str(resp.headers.get("Content-Type") or "").lower()
            data = resp.read()
        ext = ext_map.get(content_type.split(";")[0].strip())
        if not ext or not data:
            raise RuntimeError(f"原图下载结果异常：content-type={content_type!r} bytes={len(data)}")
        final = out_dir / f"{prefix}{suffix}{ext}"
        tmp = final.with_name(final.name + ".part")
        tmp.write_bytes(data)
        tmp.replace(final)
        files.append(
            {
                "path": str(final),
                "bytes": len(data),
                "content_type": content_type,
                "variant": "original",
                "media_id": img.get("media_id"),
            }
        )
    return files


def run_direct_image(guard_state: dict[str, Any], cfg: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """执行一次生图直连并下载原图；返回结果里 fallback=True 表示请求未发出，可安全改走页面自动化。"""
    direct_cfg = {
        **cfg,
        "model_wire": direct_model_wire(cfg["model"]),
        "aspect_wire": DIRECT_ASPECT_WIRE[cfg["aspect"]],
        "post_timeout_ms": max(60, min(int(cfg["timeout_sec"]), 170)) * 1000,
    }
    result, notices = run_ego_node(build_direct_image_js(guard_state, direct_cfg), timeout_sec=max(150, cfg["timeout_sec"] + 90))
    if result.get("ok"):
        try:
            result["files"] = download_direct_images(result.get("images") or [], Path(cfg["out_dir"]), cfg["file_prefix"])
        except Exception as exc:  # 图已在 Flow 项目里生成，只是下载失败：不重新生成
            result.update(
                ok=False,
                error="download_failed",
                message=f"图片已在 Flow 生成，但下载原图失败：{exc}（media_id 见 images，不要重跑生成）",
            )
    return result, notices


def generate_once(params: dict[str, Any]) -> dict[str, Any]:
    """执行一次生图或生视频（含窗口守卫、下载、预览图），返回完整结果。"""
    mode = params["mode"]
    transport = params.get("transport") or "auto"
    if transport not in TRANSPORTS:
        return {"ok": False, "error": "invalid_transport", "message": f"transport 只能是 {'/'.join(TRANSPORTS)}"}
    # 校验参考图路径存在并转为绝对路径
    ref_images: list[str] = []
    for raw_path in params.get("ref_image") or []:
        p = Path(raw_path).expanduser().resolve()
        if not p.is_file():
            return {"ok": False, "error": "invalid_ref_image", "message": f"参考图不存在：{p}"}
        ref_images.append(str(p))
    frame_mode = mode == "video" and params.get("video_type") == "帧"
    if frame_mode:
        if len(ref_images) > 2:
            return {"ok": False, "error": "invalid_ref_image", "message": "帧模式最多 2 张参考图：第 1 张首帧，第 2 张尾帧"}
        # 资源列表按文件名点选：换成按内容哈希命名的副本，同名即同内容，可安全复用已上传素材
        ref_images = [unique_upload_copy(Path(r)) for r in ref_images]

    guard_before = check_guard_or_fail()
    state = load_state()
    project_url = params.get("project_url") or state.get("default_project_url")

    out_dir = Path(params.get("out_dir") or DEFAULT_OUT_DIR).expanduser().resolve()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = params.get("name") or f"flow_{mode}_{stamp}_{sanitize_slug(params['prompt'])}"
    timeout = int(params["timeout"])

    cfg: dict[str, Any] = {
        "mode": mode,
        "prompt": params["prompt"],
        "aspect": params["aspect"],
        "count": int(params.get("count") or 1),
        "model": params.get("model"),
        "thinking": params.get("thinking"),
        "video_type": params.get("video_type"),
        "resolution": params.get("resolution"),
        "duration": int(params["duration"]) if params.get("duration") else None,
        "ref_images": ref_images,
        "project_url": project_url,
        "project_name": params.get("project_name") or DEFAULT_PROJECT_NAME,
        "out_dir": str(out_dir),
        "file_prefix": prefix,
        "max_credits": int(params["max_credits"]),
        "allow_unknown_cost": bool(params.get("allow_unknown_cost")),
        "dry_run": bool(params.get("dry_run")),
        "timeout_sec": timeout,
    }

    # 生图优先直连；参数组合未实测、或请求发出前就失败时，回退到页面自动化
    result: dict[str, Any] | None = None
    notices: list[str] = []
    fallback_reason: str | None = None
    skip_reason = direct_ineligible_reason(cfg) if mode == "image" else "mode_not_image"
    if transport == "direct" and skip_reason:
        return {"ok": False, "error": "direct_unsupported", "message": f"该参数组合不支持直连（{skip_reason}），去掉 --transport direct 即可自动走页面自动化"}
    if transport != "ui" and skip_reason is None:
        result, notices = run_direct_image(guard_before, cfg)
        if result.get("fallback"):
            fallback_reason = str(result.get("fallback_reason") or "unknown")
            if transport == "direct":
                result.update(error="direct_unavailable", message=f"直连在发出请求前失败（{fallback_reason}），未生成任何内容")
            else:
                result = None
                guard_before = check_guard_or_fail()

    if result is None:
        js = build_generate_js(guard_before, cfg)
        result, notices = run_ego_node(js, timeout_sec=max(120, timeout + 90))
        result["transport"] = "ui"
        if fallback_reason:
            result["direct_fallback_reason"] = fallback_reason
        elif mode == "image" and transport == "auto":
            result["direct_skipped_reason"] = skip_reason
    postprocess_files(result, mode)
    if mode == "video" and params.get("duration") and result.get("ok") and not result.get("dry_run"):
        verify_duration(result, params.get("duration"))
    if frame_mode and ref_images and result.get("ok") and not result.get("dry_run"):
        verify_frames(result, ref_images)
    flag_reused_label(result)

    # 自动记住专用项目 URL（仅在未显式覆盖 --project-url 时）
    resolved_url = result.get("project_url")
    if resolved_url and not params.get("project_url") and "/project/" in str(resolved_url):
        save_state(
            {
                "default_project_url": resolved_url,
                "project_name": cfg["project_name"],
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            }
        )

    guard_after = window_guard.collect_state(space_prefix=SPACE_PREFIX)
    result["window_guard_after"] = {
        "action": guard_after.get("action"),
        "single_window_enforced": guard_after.get("single_window_enforced"),
        "work_window_titles": guard_after.get("work_window_titles"),
    }
    result["notices"] = notices
    return result


def cmd_credits(args: argparse.Namespace) -> int:
    """执行点数查询命令。"""
    guard_before = check_guard_or_fail()
    js = build_credits_js(guard_before)
    result, notices = run_ego_node(js, timeout_sec=90)
    guard_after = window_guard.collect_state(space_prefix=SPACE_PREFIX)
    result["window_guard_after"] = {
        "action": guard_after.get("action"),
        "single_window_enforced": guard_after.get("single_window_enforced"),
        "work_window_titles": guard_after.get("work_window_titles"),
    }
    result["notices"] = notices
    emit(result if args.verbose else compact_credits(result), args.verbose)
    return 0 if result.get("ok") else 1


def cmd_generate(args: argparse.Namespace, mode: str) -> int:
    """执行单次生图或生视频命令。"""
    params = {
        **BASE_PARAMS,
        "mode": mode,
        "prompt": args.prompt,
        "aspect": args.aspect,
        "count": args.count,
        "model": args.model,
        "thinking": getattr(args, "thinking", None),
        "video_type": getattr(args, "video_type", None),
        "resolution": getattr(args, "resolution", None),
        "duration": getattr(args, "duration", None),
        "ref_image": args.ref_image or [],
        "out_dir": args.out_dir,
        "name": args.name,
        "project_url": args.project_url,
        "project_name": args.project_name,
        "max_credits": args.max_credits,
        "allow_unknown_cost": args.allow_unknown_cost,
        "dry_run": args.dry_run,
        "timeout": args.timeout,
        "transport": getattr(args, "transport", "auto"),
    }
    result = generate_once(params)
    emit(result if args.verbose else compact_result(result), args.verbose)
    if result.get("error") == "invalid_ref_image":
        return 2
    return 0 if result.get("ok") else 1


# ---------- 批量生成：一次调用跑完整组镜头 ----------


def load_plan(plan_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """读取并校验镜头计划 JSON；拼错的字段直接报错，避免静默按默认值生成。"""
    data = json.loads(plan_path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        data = {"shots": data}
    if not isinstance(data, dict):
        raise ValueError("plan 顶层必须是对象 {defaults, shots} 或镜头数组")
    defaults = data.get("defaults") or {}
    shots = data.get("shots") or []
    if not isinstance(defaults, dict) or not isinstance(shots, list) or not shots:
        raise ValueError("plan 需要非空的 shots 数组，defaults 必须是对象")
    bad_default = set(defaults) - (PLAN_KEYS - {"id"}) - {"out_dir", "image", "video"}
    if bad_default:
        raise ValueError(f"defaults 含未知字段：{sorted(bad_default)}")
    # defaults.image / defaults.video 为按模式的默认值，避免视频模型等设置串到图片镜头
    for mode_key in ("image", "video"):
        section = defaults.get(mode_key)
        if section is None:
            continue
        if not isinstance(section, dict):
            raise ValueError(f"defaults.{mode_key} 必须是对象")
        bad = set(section) - (PLAN_KEYS - {"id", "mode", "prompt"})
        if bad:
            raise ValueError(f"defaults.{mode_key} 含未知字段：{sorted(bad)}")
    seen: set[str] = set()
    for idx, shot in enumerate(shots):
        if not isinstance(shot, dict):
            raise ValueError(f"第 {idx + 1} 个镜头不是对象")
        bad = set(shot) - PLAN_KEYS
        if bad:
            hint = "（out_dir 只能写在 defaults）" if "out_dir" in bad else ""
            raise ValueError(f"镜头 {shot.get('id', idx + 1)} 含未知字段：{sorted(bad)}{hint}")
        sid = str(shot.get("id") or f"s{idx + 1:02d}")
        if sid in seen:
            raise ValueError(f"镜头 id 重复：{sid}")
        seen.add(sid)
    return defaults, shots


def resolve_shot_params(
    defaults: dict[str, Any],
    shot: dict[str, Any],
    idx: int,
    plan_path: Path,
    out_dir: Path,
    dry_run: bool,
) -> dict[str, Any]:
    """合并 脚本模式默认值 < defaults 通用 < defaults.<mode> < 镜头字段，得到单个镜头的生成参数。"""
    common = {k: v for k, v in defaults.items() if k not in ("out_dir", "image", "video")}
    mode = shot.get("mode") or common.get("mode")
    if mode not in MODE_DEFAULTS:
        raise ValueError(f"镜头 {shot.get('id', idx + 1)} 的 mode 必须是 image 或 video")
    merged = {**common, **(defaults.get(mode) or {}), **shot, "mode": mode}
    if not str(merged.get("prompt") or "").strip():
        raise ValueError(f"镜头 {shot.get('id', idx + 1)} 缺少 prompt")
    sid = str(merged.pop("id", None) or f"s{idx + 1:02d}")
    params: dict[str, Any] = {**BASE_PARAMS, **MODE_DEFAULTS[mode], **merged}
    refs = merged.get("ref_image") or []
    if isinstance(refs, str):
        refs = [refs]
    # 相对路径按 plan 文件所在目录解析
    params["ref_image"] = [str((plan_path.parent / Path(r).expanduser()).resolve()) for r in refs]
    params["out_dir"] = str(out_dir)
    params["name"] = merged.get("name") or f"{plan_path.stem}_{sid}"
    params["dry_run"] = dry_run
    params["id"] = sid
    return params


def existing_outputs(out_dir: Path, name: str) -> list[str]:
    """查找某镜头已落盘的成品文件（name.ext 或 name_N.ext），用于断点续跑。"""
    pattern = re.compile(re.escape(name) + r"(_\d+)?")
    hits = []
    if out_dir.is_dir():
        for p in out_dir.iterdir():
            if p.is_file() and p.suffix != ".part" and pattern.fullmatch(p.stem):
                hits.append(str(p))
    return sorted(hits)


def credits_spent(result: dict[str, Any]) -> int:
    """保守计算单个镜头实际消耗：取预估值与前后余额差中的较大者（余额可能延迟刷新）。"""
    est = result.get("estimated_credits")
    before, after = result.get("credits_before"), result.get("credits_after")
    diff = before - after if isinstance(before, int) and isinstance(after, int) else 0
    return max(int(est) if isinstance(est, int) else 0, diff, 0)


def cmd_batch(args: argparse.Namespace) -> int:
    """按 JSON 计划顺序生成多个镜头；任一镜头失败即停，只输出一份精简汇总。"""
    plan_path = Path(args.plan).expanduser().resolve()
    try:
        defaults, shots = load_plan(plan_path)
        if args.out_dir:
            out_dir = Path(args.out_dir).expanduser().resolve()
        elif defaults.get("out_dir"):
            out_dir = (plan_path.parent / Path(defaults["out_dir"]).expanduser()).resolve()
        else:
            out_dir = DEFAULT_OUT_DIR / plan_path.stem
        all_params = [
            resolve_shot_params(defaults, shot, idx, plan_path, out_dir, args.dry_run)
            for idx, shot in enumerate(shots)
        ]
    except (OSError, ValueError) as exc:
        emit({"ok": False, "error": "invalid_plan", "message": truncate(exc)}, args.verbose)
        return 2

    if args.only:
        wanted = {s.strip() for s in args.only.split(",") if s.strip()}
        unknown = wanted - {p["id"] for p in all_params}
        if unknown:
            emit({"ok": False, "error": "invalid_plan", "message": f"--only 中的镜头不存在：{sorted(unknown)}"}, args.verbose)
            return 2
        all_params = [p for p in all_params if p["id"] in wanted]

    has_video = any(p["mode"] == "video" for p in all_params)
    if has_video and not args.dry_run and args.max_total_credits is None:
        emit(
            {
                "ok": False,
                "error": "max_total_credits_required",
                "message": "计划包含视频镜头，必须传 --max-total-credits；可先加 --dry-run 拿到总预估点数（0 消耗）。",
            },
            args.verbose,
        )
        return 2

    remaining = args.max_total_credits
    result_file = out_dir / f"{plan_path.stem}_batch_result.json"
    entries: list[dict[str, Any]] = []
    raw_results: list[dict[str, Any]] = []
    notices: list[str] = []
    total_estimated = 0
    total_spent = 0
    credits_before = None
    credits_after = None
    guard_after = None
    stopped = False

    def build_summary() -> dict[str, Any]:
        counts = {s: sum(1 for e in entries if e["status"] == s) for s in ("ok", "failed", "skipped", "pending")}
        credits: dict[str, Any] = {"before": credits_before, "after": credits_after, "estimated_total": total_estimated}
        if not args.dry_run:
            credits["spent"] = total_spent
            credits["max_total"] = args.max_total_credits
        summary: dict[str, Any] = {
            "ok": counts["failed"] == 0 and counts["pending"] == 0,
            "dry_run": bool(args.dry_run),
            "out_dir": str(out_dir),
            "counts": counts,
            "credits": {k: v for k, v in credits.items() if v is not None},
            "shots": entries,
            "result_file": str(result_file),
        }
        if guard_after:
            summary["guard_after"] = guard_after
        if notices:
            summary["notices"] = notices
        return summary

    def write_result_file() -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        payload = {"plan": str(plan_path), "summary": build_summary(), "raw": raw_results}
        result_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    for params in all_params:
        sid = params.pop("id")
        entry: dict[str, Any] = {"id": sid, "mode": params["mode"]}
        if stopped:
            entry["status"] = "pending"
            entries.append(entry)
            continue
        if args.skip_existing and not args.dry_run:
            hits = existing_outputs(Path(params["out_dir"]), params["name"])
            if hits:
                entry.update(status="skipped", files=hits)
                entries.append(entry)
                continue
        if remaining is not None and not args.dry_run:
            # 单镜头上限不能超过整批剩余预算
            params["max_credits"] = min(int(params["max_credits"]), max(int(remaining), 0))

        try:
            result = generate_once(params)
        except Exception as exc:  # 守卫阻断、ego-browser 异常等
            result = {"ok": False, "error": "exception", "message": str(exc)}
        raw_results.append({"id": sid, "params": params, "result": result})

        compact = compact_result(result)
        entry["status"] = "ok" if result.get("ok") else "failed"
        for key in ("error", "message", "model", "files", "warnings", "transport", "direct_fallback", "seconds"):
            if key in compact:
                entry[key] = compact[key]
        est = (compact.get("credits") or {}).get("estimated")
        if est is not None:
            entry["estimated"] = est
            if isinstance(est, int):
                total_estimated += est
        if credits_before is None and isinstance(result.get("credits_before"), int):
            credits_before = result["credits_before"]
        if isinstance(result.get("credits_after"), int):
            credits_after = result["credits_after"]
        guard_after = compact.get("guard_after") or guard_after
        for n in compact.get("notices") or []:
            if n not in notices:
                notices.append(n)
        if result.get("ok") and not args.dry_run:
            spent = credits_spent(result)
            entry["spent"] = spent
            total_spent += spent
            if remaining is not None:
                remaining -= spent
        entries.append(entry)
        write_result_file()

        if not result.get("ok"):
            # 干跑时超预算只是信息，继续收集其余镜头的预估；其他错误一律停止
            if not (args.dry_run and result.get("error") == "budget_exceeded"):
                stopped = True

    write_result_file()
    summary = build_summary()
    if args.verbose:
        summary["raw"] = raw_results
    emit(summary, args.verbose)
    return 0 if summary["ok"] else 1


# ---------- 命令行 ----------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="基于本机 Ego Lite 调用 Google Flow 生成图片/视频并自动下载到本地。"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_credits = sub.add_parser("credits", help="查询当前 Google Flow 剩余点数与重置时间")
    p_credits.add_argument("--verbose", action="store_true", help="输出完整 JSON（排障用）")

    def add_common_gen_args(p: argparse.ArgumentParser, mode: str) -> None:
        d = MODE_DEFAULTS[mode]
        p.add_argument("--prompt", required=True, help="生成提示词")
        p.add_argument("--aspect", default=d["aspect"], help=f"宽高比（默认 {d['aspect']}）")
        p.add_argument("--count", type=int, default=1, choices=[1, 2, 3, 4], help="输出数量 1~4（默认 1）")
        p.add_argument("--model", default=d["model"], help=f"模型名称子串（默认 {d['model']}）")
        p.add_argument("--ref-image", action="append", default=[], help="本地参考图路径（可重复传多次）")
        p.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help=f"下载目录（默认 {DEFAULT_OUT_DIR}）")
        p.add_argument("--name", default=None, help="自定义输出文件名前缀（不含扩展名）")
        p.add_argument("--project-url", default=None, help="指定现有 Flow 项目 URL（不传则自动复用专用项目）")
        p.add_argument("--project-name", default=DEFAULT_PROJECT_NAME, help=f"专用项目名称（默认 {DEFAULT_PROJECT_NAME}）")
        p.add_argument("--max-credits", type=int, default=d["max_credits"], help=f"允许消耗的最大点数上限（默认 {d['max_credits']}）")
        p.add_argument("--allow-unknown-cost", action="store_true", help="无法解析页面点数预估时是否继续（默认拦截）")
        p.add_argument("--dry-run", action="store_true", help="仅验证参数与预估点数消耗，不实际点击生成")
        p.add_argument("--timeout", type=int, default=d["timeout"], help=f"生成等待超时秒数（默认 {d['timeout']}）")
        p.add_argument("--verbose", action="store_true", help="输出完整 JSON（排障用）")

    p_img = sub.add_parser("image", help="通过 Google Flow 生成图片并下载到本地")
    add_common_gen_args(p_img, "image")
    p_img.add_argument("--thinking", choices=["低", "中", "高"], default=None, help="图片模型思考等级（低/中/高）")
    p_img.add_argument(
        "--transport",
        choices=list(TRANSPORTS),
        default="auto",
        help="auto（默认）：已实测的组合走直连，其余或直连发请求前失败时走页面自动化；direct：只走直连；ui：只走页面自动化",
    )

    p_vid = sub.add_parser("video", help="通过 Google Flow 生成视频并下载到本地")
    add_common_gen_args(p_vid, "video")
    p_vid.add_argument("--video-type", choices=["素材", "帧"], default=MODE_DEFAULTS["video"]["video_type"], help="视频类型（默认 素材）")
    p_vid.add_argument("--resolution", choices=["360p", "720p"], default=MODE_DEFAULTS["video"]["resolution"], help="视频分辨率（默认 720p）")
    p_vid.add_argument("--duration", type=int, default=None, help="视频时长（秒）。不传则沿用编辑器当前值（常为 15 秒）；Omni 1.2 Flash 支持 3~15")

    p_batch = sub.add_parser("batch", help="按 JSON 镜头计划批量生成，只返回一份精简汇总")
    p_batch.add_argument("--plan", required=True, help="镜头计划 JSON 路径")
    p_batch.add_argument("--max-total-credits", type=int, default=None, help="整批允许消耗的点数上限（含视频时必填）")
    p_batch.add_argument("--out-dir", default=None, help="输出目录（默认 outputs/flow_studio/<plan 文件名>）")
    p_batch.add_argument("--only", default=None, help="只跑指定镜头 id，逗号分隔")
    p_batch.add_argument("--skip-existing", action="store_true", help="跳过已有成品文件的镜头（断点续跑）")
    p_batch.add_argument("--dry-run", action="store_true", help="逐条干跑，只汇总预估点数（0 消耗）")
    p_batch.add_argument("--verbose", action="store_true", help="附带每个镜头的完整结果（排障用）")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "credits":
            return cmd_credits(args)
        if args.command == "image":
            return cmd_generate(args, "image")
        if args.command == "video":
            return cmd_generate(args, "video")
        if args.command == "batch":
            return cmd_batch(args)
        parser.error(f"未知命令：{args.command}")
        return 2
    except Exception as exc:
        emit({"ok": False, "error": "exception", "message": truncate(exc)}, getattr(args, "verbose", False))
        return 1


if __name__ == "__main__":
    sys.exit(main())

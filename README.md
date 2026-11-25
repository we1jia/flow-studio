# flow-studio

> A deterministic CLI + agent skill that drives Google Flow (Nano Banana / Omni / Veo) through your logged-in Ego Lite browser, with hard credit budgets and machine-readable JSON output. Unofficial; not affiliated with Google.

让 Claude Code、Codex、Antigravity 等 Agent 用**一条命令**在 Google Flow 里生图、生视频，并把成品下载到本地。它复用你在 Ego Lite 浏览器里已经登录的 Google 账号，不导出、不上传任何 Cookie。

## 能做什么

- **生图**：Nano Banana 2.1（默认，走页面内直连，约 20 秒一次）、Nano Banana Pro 等；支持参考图。
- **生视频**：Omni 1.2 Flash（默认）、Veo 3.1；支持首帧、首帧 + 尾帧、素材参考图，可设时长和分辨率。
- **批量**：一份 `shots.json` 串行跑完一组镜头，支持干跑估价、只跑指定镜头、跳过已完成的镜头。
- **查点数**：`credits` 读取剩余点数和重置时间。

## 为什么不是普通脚本

- **硬预算**：提交视频前读取页面上的实时扣点，超过 `--max-credits` / `--max-total-credits` 就在提交前拦截，不扣点。
- **结果核对**：
  - 帧模式干跑会真实上传并核对首/尾帧绑定；
  - 真跑后核对成片时长，并用 SSIM 比对成片首帧和你给的首帧图；
  - 按去掉缩略图尺寸后缀的媒体 key 区分新旧卡片，防止拿到项目里的旧视频。
- **不抢窗口**：运行前后都跑窗口守卫 `ego_lite_window_guard.py`。它只复用名字以 `flow studio` 开头的 Task Space，不接管用户窗口，也不碰其他 Agent 正在用的 Space。
- **省 Token**：默认只输出一行精简 JSON；图片附 512px 缩略图，视频附 4 帧拼图，Agent 只需看预览图。

## 环境要求

- macOS（窗口守卫用 `osascript`，缩略图用 `sips`）
- [Ego Lite](https://lite.ego.app/) 浏览器，提供 `ego-browser` 命令；在其中登录可以使用 Google Flow 的 Google 账号
- Python 3（开发与测试用 3.13），只用标准库
- `ffmpeg` / `ffprobe`：视频预览、时长核对和首帧 SSIM 需要
- Node.js：只有跑测试时需要

## 安装

作为 Agent skill 安装（仓库根目录就是 skill 目录）：

```bash
git clone https://github.com/we1jia/flow-studio.git ~/flow-studio
mkdir -p ~/.agents/skills && ln -s ~/flow-studio ~/.agents/skills/flow-studio
```

Claude Code 再加一条链接：

```bash
mkdir -p ~/.claude/skills && ln -s ~/.agents/skills/flow-studio ~/.claude/skills/flow-studio
```

Agent 会按 [`SKILL.md`](SKILL.md) 调用脚本：先干跑估价，拿到你同意的点数再真跑，成品发回对话。

## 直接用命令行

```bash
S=~/flow-studio/flow_studio.py

# 生图（Nano Banana 2.1 不扣点）
python3 $S image --prompt "a red fox in snow, cinematic" --aspect 16:9 --count 2

# 视频：先干跑看报价，再按你接受的上限真跑
python3 $S video --prompt "..." --duration 4 --resolution 360p --dry-run
python3 $S video --prompt "..." --duration 4 --resolution 360p --max-credits 4

# 首帧 + 尾帧
python3 $S video --prompt "..." --video-type 帧 --ref-image /abs/start.png --ref-image /abs/end.png --duration 4 --max-credits 4

# 批量
python3 $S batch --plan /abs/shots.json --dry-run
python3 $S batch --plan /abs/shots.json --max-total-credits 40 --skip-existing

python3 $S credits
```

全部参数看 `python3 flow_studio.py <子命令> --help`。返回字段与错误码的处理方式见 [`SKILL.md`](SKILL.md)。

## 配置

| 环境变量 | 作用 | 默认 |
|---|---|---|
| `FLOW_STUDIO_OUT_DIR` | 成品下载目录 | 当前工作目录下的 `outputs/flow_studio` |
| `FLOW_STUDIO_PROJECT` | Flow 里复用的专用项目名 | `Agent CLI Studio` |

运行状态和上传缓存放在 `~/.cache/flow_studio/`。

## 测试

```bash
python3 -m pytest -q tests
```

测试不访问网络、不打开浏览器、不扣点。

## 声明

- 本项目是非官方工具，与 Google 无关。“Google Flow”“Nano Banana”“Veo”等名称归各自权利人所有。
- 它通过网页自动化和 Flow 网页内部接口工作，Flow 改版后可能失效；自动化使用可能不符合相关服务条款，请自行评估风险。
- 生成视频会消耗你账号的点数，扣掉的点数无法退回。脚本的预算拦截只能减少误扣，不能替你做决定。

## 致谢与许可

生图直连的请求格式移植自 [gflow-cli](https://github.com/ffroliva/gflow-cli)（MIT），见 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。

本项目以 [MIT](LICENSE) 许可发布。

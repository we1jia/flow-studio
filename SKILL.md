---
name: flow-studio
description: Use when the user wants images or videos from Google Flow (Nano Banana 2.1, Nano Banana Pro, Omni 1.2 Flash, Veo 3.1) — 用 Flow 生图、生视频、跑一组镜头、查 Flow 点数 — or mentions flow_studio, flow-studio, or generating with the logged-in Ego Lite profile and Google AI Ultra credits.
---

# Flow Studio

脚本：本 skill 目录下的 `flow_studio.py`（按 README 安装后是 `~/.agents/skills/flow-studio/flow_studio.py`）。一条命令完成，读取单行 JSON 即可。脚本内部已执行 Ego Lite 窗口守卫（同目录 `ego_lite_window_guard.py`）并按其 action 处理，调用前不必另跑；还会复用 Flow 里的专用项目（默认“Agent CLI Studio”，环境变量 `FLOW_STUDIO_PROJECT` 可改）、下载原图、生成预览图。

## 省 Token 规则（必须遵守）

- 不读脚本源码，不截图操控 Flow 网页，不自己写 `ego-browser` 脚本去点 Flow。
- Nano Banana 2.1 生图 0 点：不单独跑 `credits`，不做 `--dry-run`。
- 自己检查效果只用 Read 看 `files[].preview`（绝对路径；图片 512px 缩略图，视频 4 帧拼图），不逐张 Read 原图/原视频。
- **成品必须发到对话里给用户看**：生成成功后，把 `files[].path`（原图/原视频，视频再附 preview 拼图）用当前 agent 的发送文件工具发给用户，例如 Claude Code 的 `SendUserFile`（`display: "render"`，同一批放一次调用）；没有发送工具时列出可点击的绝对路径。只写“我看过了”不算交付。
- 两个及以上镜头用 `batch`（内部串行）一次跑完；不要逐条调用，也不要并发开多个 flow_studio。
- 工具超时：图片前台 300 秒；单条视频 3~7 分钟，设 600 秒以上；`batch` 放后台，等完成通知，中途不轮询。`--timeout` 是脚本等页面出结果的上限，页面自动化一次出 3~4 张图时调到 300。
- 只在排障时加 `--verbose`；全部参数看 `--help`。

## 命令

```bash
S=~/.agents/skills/flow-studio/flow_studio.py
python3 $S image --prompt "..." [--aspect 16:9] [--count 1] [--model "Nano Banana Pro"] [--ref-image /abs/a.png] [--name 前缀]
python3 $S video --prompt "..." [--model "Veo 3.1 - Lite"] [--aspect 16:9|9:16] [--duration 4] [--video-type 帧 --ref-image /abs/首帧.png [--ref-image /abs/尾帧.png]] [--resolution 360p] --max-credits <用户同意的上限>
python3 $S batch --plan /abs/shots.json --dry-run                 # 含视频时先干跑：0 点，汇总每条与总预估
python3 $S batch --plan /abs/shots.json --max-total-credits <用户同意的总额> [--skip-existing] [--only s01,s03]
python3 $S credits
```

- 视频不写 `--model` 时默认 Omni 1.2 Flash；`--max-credits` 默认 100。
- 输出默认落在当前工作目录的 `outputs/flow_studio/`，环境变量 `FLOW_STUDIO_OUT_DIR` 或 `--out-dir` 可改。
- **视频参考图有两种用法**：`--video-type 帧` + `--ref-image` = 首帧，再加第 2 张 = 尾帧；默认 `素材` + `--ref-image` = 素材参考图（不是首帧）。
- 帧模式的 `--dry-run` 会真实上传并核对首/尾帧绑定（结果 `frame_binding[].verified`），不提交不扣点。真跑后自动核对：时长与 `--duration` 相差超过 1.5 秒判 `result_mismatch`；成片首帧与首帧图 SSIM 低于 0.4 判 `first_frame_mismatch`（`files[].first_frame_ssim`；尾帧只给 `last_frame_ssim` 和警告）。两者都会停批量。这两项核对分不出首尾帧和时长都相同的旧视频：同一组首尾帧重跑后，看 `pick_diag.label_seen_before` 或警告“可能拿到了旧视频”，出现就先核对再交付。

`shots.json`：`defaults` 放通用字段，`defaults.image` / `defaults.video` 放按模式的默认值，镜头字段覆盖默认值。相对路径按 plan 所在目录解析；输出默认 `outputs/flow_studio/<plan 文件名>/`。镜头可用字段：`id prompt mode aspect count model thinking video_type resolution duration ref_image name max_credits timeout transport project_url project_name allow_unknown_cost`，拼错会直接报错。

```json
{"defaults": {"mode": "video", "aspect": "16:9", "video": {"model": "Veo 3.1 - Lite"}},
 "shots": [{"id": "s01", "prompt": "..."},
           {"id": "s02", "prompt": "...", "video_type": "帧", "ref_image": "/abs/s02_首帧.png"}]}
```

## 点数与预算（硬规则）

- 视频会扣点。单条 `video` 和 `batch` 都先 `--dry-run`（走页面自动化，前台、超时 600 秒），把预估（每条 + 总额）报给用户，拿到明确数字，或用户对你报出的具体数字回复“可以/按这个跑”，再真跑；“生成这一组”“别花太多”“急着发”都不算同意花多少。
- `--max-total-credits` 会同时压住每条镜头的上限；单条视频真跑可前台（超时 600 秒）。
- **金丝雀**：预估总额 ≥ 60 点或 ≥ 3 条视频时，先 `--only <第一条>` 真跑 1 条，Read 它的 preview 核对：参考图/首帧是否生效、画面是否符合提示词、有无意外对白；没问题再 `--skip-existing` 跑其余。素材模式的干跑不上传图片，验证不了参考是否生效。
- 用 `--duration` 按镜头实际需要设时长（Omni 1.2 Flash 支持 3~15 秒）；不传会沿用编辑器当前值（常为 15 秒）。实测报价：4 秒 360p 4 点、720p 12 点；15 秒 360p 20 点、720p 60 点。
- 视频提示词写明声音：要人声时写明语言和原句，如 `characters speak Mandarin Chinese:「中文原句」`（旁白同理）；不要人声时写 `no dialogue, ambient sound only`。什么都不写，模型会随机生成对白（多为英文）；用户对语言有要求时，成片人声不符就不合格。
- `budget_exceeded` / `unknown_cost` 都在提交前拦截、未扣点：不要自己调高上限，也不要自己加 `--allow-unknown-cost`，先问用户。Nano Banana Pro 等其他图片模型若页面显示要扣点，默认上限 0 会触发 `budget_exceeded`，同样先问。

## 生图走哪条路径（`--transport`，默认 auto，不用手动指定）

| 请求 | 路径 | 耗时 |
|---|---|---|
| Nano Banana 2.1 文生图，1:1/9:16/16:9/3:4/4:3，1~4 张 | `direct`：页面内直连，不弹窗 | 约 18~32 秒/次 |
| 参考图、`--thinking`、其他模型、`--dry-run` | `ui`：页面自动化 | 约 30~50 秒/张 |
| 直连在发请求前失败 | 自动改走 `ui`，结果带 `direct_fallback`（无需处理，可顺带告知） | — |

视频始终走 `ui`，并受 `--max-credits` 拦截。

## 结果处理

| 字段 / 错误 | 怎么做 |
|---|---|
| `not_logged_in`、验证码、`guard_after` 为 `stop_*` | 立即停止，交给用户 |
| `runtime_error` 含 `page label not found` / `Task space does not exist` | 多半是别的 agent 或定时任务同时在用 Ego Lite。先跑 `credits` 看点数有没有扣：没扣说明没提交，等对方结束后再跑；扣了按 `result_mismatch` 处理 |
| `direct_post_unknown`、`direct_unparsed_reply`、`direct_http_error` | 请求已发出、可能已生成：**不要重跑，也不要换 `--transport ui` 绕过**。请用户到 Flow 专用项目看最新一张是否就是这次的提示词；用户确认没有、并同意后再跑一次 |
| `download_failed` | 图已生成，只是下载失败：不要重新生成，把 `images[].media_id` 告诉用户 |
| `flow_unusual_activity` | Flow 风控，未扣费：停下，至少等几小时再试；不要换提示词或换路径硬刷 |
| `content_policy` | 改提示词后再试 |
| `warnings` | 某项设置没生效（如该模型不支持 360p）或张数不足，告诉用户 |
| `batch` 中途失败 | 已完成的镜头不会重跑；修复后加 `--skip-existing` 续跑 |
| `result_mismatch`、`result_pick_mismatch`、`first_frame_mismatch` | 已提交、**点数已扣**：不要重跑。把 `pick_diag`（选中了哪张卡）报给用户；成品多半就在专用项目视频栏第一张，先取回核对，再决定是否续跑 |

## 本工具没有的功能 → gflow-cli

续写已有视频、镜头接力、服务端拼接、角色一致性（`@Name`）、超分：本工具做不到，可改用 [gflow-cli](https://github.com/ffroliva/gflow-cli)（需另装）。对应命令：`gflow image upscale`（2K/4K）、`gflow video upscale`（1080p）、`gflow video extend`（续写，扣点）、`gflow video i2v --initial-frame ... --end-frame ...`（首尾帧，扣点）、`gflow video chain`、`gflow scene`、`gflow character`；参数看各自 `--help`。gflow 每次会弹出独立 Chrome 约 2 秒，**先征得用户同意**；扣点命令先用 `--max-credits 0` 拿报价（超上限会在提交前拒绝、不扣点，错误里带报价；`extend`/`chain` 也可 `--dry-run`），报给用户拿到数字后再带 `--max-credits N` 真跑。

## 角色与分镜

- 用户要的是网上流行的角色/IP 形象时，先搜 ≥ 3 张参考，或先出 2~3 个方向的设定图让用户选，再做分镜；不得擅自改用户指定的名称或形象。
- 分镜默认写清：剧情节点（开场→事件→冲突→低谷→转折→高潮→结局）、景别、画面、转场方式、时长；全部镜头用同一张设定图作 `ref_image` 保持人物一致。

## 事实边界

不得用生成图伪装产品截图、终端输出或真实证据；logo、文字可能被模型改形，不保证像素级还原。

# Changelog

## 0.2.0 — 2026-11-25

- Installable as an agent skill with `npx skills add we1jia/flow-studio`, as a Claude Code plugin, and as an Antigravity plugin. The skill now lives in `skills/flow-studio/`.
- Each run uses its own Task Space name (`flow studio #<pid>: …`), so concurrent runs from different sessions no longer pick up each other's spaces.
- New `unsupported_ui_language` error when Flow's UI isn't Simplified Chinese, instead of a generic "control not found".
- English aliases: `--video-type frames|ingredients`, `--thinking low|medium|high` (also in `shots.json`).
- `--version` flag, CI, bilingual README with examples.

## 0.1.0 — 2026-11-25

- First public release: `image`, `video`, `batch` and `credits` commands; hard credit budgets; start and end frames; duration and first-frame SSIM checks; window guard.

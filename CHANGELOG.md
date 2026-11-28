# Changelog

## 0.2.1 — 2026-11-27

- Fix: image and ingredients-mode reference images could attach the wrong picture. After uploading, the old code clicked "Add to prompt", which adds whatever asset the picker has highlighted (the most recent one in the project, not the upload). On top of that, once Flow finishes processing an upload it auto-attaches it to the prompt box of the next page that opens the project. Reference images are now uploaded first and the run waits until Flow has processed them. Each is then picked by a unique file name, the highlight is confirmed before adding, and the attachment's asset URL is checked against the picked asset. Any attachment that isn't one of this run's references is removed, and the prompt box is cleaned and checked again right before submitting. Dry runs do the same check at no cost.
- Fix: a cached project URL silently overrode `--project-name` / `FLOW_STUDIO_PROJECT`. The cache is now keyed by project name, and every run reads the project title from the page after opening it. A mismatch drops the cached URL and falls back to search-by-name, then create. An explicit `--project-url` plus `--project-name` that disagree is a `project_mismatch` error.
- New: results carry `project` (name read from the page, and how it was found) and `ref_attach`. Image results with references carry `files[].ref_ssim`. `--keep-layout` / `keep_layout` turns a very low similarity into a `ref_mismatch` error that stops the batch; without it, a low score is only a warning.
- New errors, all raised before submitting: `project_mismatch`, `ref_attach_failed`, `composer_not_clean`.

## 0.2.0 — 2026-11-25

- Installable as an agent skill with `npx skills add we1jia/flow-studio`, as a Claude Code plugin, and as an Antigravity plugin. The skill now lives in `skills/flow-studio/`.
- Each run uses its own Task Space name (`flow studio #<pid>: …`), so concurrent runs from different sessions no longer pick up each other's spaces.
- New `unsupported_ui_language` error when Flow's UI isn't Simplified Chinese, instead of a generic "control not found".
- English aliases: `--video-type frames|ingredients`, `--thinking low|medium|high` (also in `shots.json`).
- `--version` flag, CI, bilingual README with examples.

## 0.1.0 — 2026-11-25

- First public release: `image`, `video`, `batch` and `credits` commands; hard credit budgets; start and end frames; duration and first-frame SSIM checks; window guard.

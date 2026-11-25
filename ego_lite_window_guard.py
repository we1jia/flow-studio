#!/usr/bin/env python3
"""Fail closed unless Ego Lite can safely reuse or attach one agent window instance."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from typing import Any


WINDOW_PROBE = r'''
if application "ego lite" is running then
  tell application "ego lite"
    set output to "1" & linefeed & ((count of windows) as text)
    repeat with currentWindow in windows
      set windowTitle to ""
      try
        set windowTitle to name of currentWindow as text
      end try
      set output to output & linefeed & windowTitle
    end repeat
    return output
  end tell
else
  return "0" & linefeed & "0"
end if
'''


TASK_SPACE_PROBE = r'''
const spaces = await listTaskSpaces()
cliLog('__EGO_WINDOW_GUARD__' + JSON.stringify(spaces))
'''

EMPTY_WINDOW_TITLES = {"", "about:blank", "about:blank/"}


def _normalize_title(title: Any) -> str:
    return str(title or "").strip()


def is_empty_window_title(title: Any) -> bool:
    return _normalize_title(title).lower() in EMPTY_WINDOW_TITLES


def classify_window_titles(
    window_titles: list[Any] | None, window_count: int
) -> tuple[list[str], list[str]]:
    titles = [_normalize_title(title) for title in (window_titles or [])]
    while len(titles) < window_count:
        titles.append("")
    if window_titles is None:
        work = [f"window-{index + 1}" for index in range(window_count)]
        return work, []
    work = [title for title in titles[:window_count] if not is_empty_window_title(title)]
    empty = [title or "about:blank" for title in titles[:window_count] if is_empty_window_title(title)]
    return work, empty


def _space_id(space: dict[str, Any]) -> int | None:
    space_id = space.get("id")
    return space_id if isinstance(space_id, int) else None


def _is_agent_owned(space: dict[str, Any]) -> bool:
    return (
        _space_id(space) is not None
        and space.get("ownership") not in ("user", "agentDelegatedToUser")
    )


def _matches_space_prefix(space: dict[str, Any], space_prefix: str) -> bool:
    prefix = space_prefix.strip().lower()
    if not prefix:
        return True
    name = _normalize_title(space.get("name") or space.get("taskId")).lower()
    return name.startswith(prefix)


def agent_owned_ids(
    task_spaces: list[dict[str, Any]], *, space_prefix: str = ""
) -> list[int]:
    ids: list[int] = []
    for space in task_spaces:
        if not _is_agent_owned(space):
            continue
        if not _matches_space_prefix(space, space_prefix):
            continue
        space_id = _space_id(space)
        if space_id is not None:
            ids.append(space_id)
    return ids


def foreign_agent_owned_spaces(
    task_spaces: list[dict[str, Any]], *, space_prefix: str = ""
) -> list[dict[str, Any]]:
    """当指定任务前缀时，识别正在运行的其他 Agent Space，避免跨任务误抢。"""
    prefix = space_prefix.strip().lower()
    if not prefix:
        return []
    foreign: list[dict[str, Any]] = []
    for space in task_spaces:
        if not _is_agent_owned(space):
            continue
        if _matches_space_prefix(space, prefix):
            continue
        foreign.append(
            {
                "id": _space_id(space),
                "name": _normalize_title(space.get("name") or space.get("taskId")),
                "ownership": space.get("ownership"),
            }
        )
    return foreign


def decide(
    window_count: int,
    task_spaces: list[dict[str, Any]],
    window_titles: list[str] | None = None,
    *,
    space_prefix: str = "",
) -> dict[str, Any]:
    """Return the safety decision for one work window plus reclaimable empty windows."""
    work_titles, empty_titles = classify_window_titles(window_titles, window_count)
    reusable_ids = agent_owned_ids(task_spaces, space_prefix=space_prefix)
    foreign_spaces = foreign_agent_owned_spaces(task_spaces, space_prefix=space_prefix)
    has_user_owned = any(
        _space_id(space) is not None
        and space.get("ownership") in ("user", "agentDelegatedToUser")
        for space in task_spaces
    )

    if len(work_titles) > 1:
        action = "stop_duplicate_windows"
        reason = "Ego Lite 已有多个工作窗口，禁止继续任何页面操作。"
        can_create = False
    elif reusable_ids:
        action = "reuse_existing_task_space"
        reason = "复用已有 agent-owned 数字 Task Space ID；禁止接管 user-owned Space。"
        can_create = False
    elif empty_titles:
        action = "attach_empty_window"
        if foreign_spaces:
            names = ", ".join(s["name"] or f"#{s['id']}" for s in foreign_spaces)
            reason = (
                "把空窗口收成 Agent 窗口实例，并在其上新建本任务专属隔离 Space；"
                f"禁止触碰或关闭其他 Agent 的 Space（{names}）。"
            )
        else:
            reason = "把空窗口收成 Agent 窗口实例，并在其上创建或复用本任务 Space。"
        can_create = True
    elif has_user_owned or foreign_spaces:
        action = "create_agent_task_space"
        if foreign_spaces:
            names = ", ".join(s["name"] or f"#{s['id']}" for s in foreign_spaces)
            reason = (
                "允许在单窗口内新建本任务专属隔离 Space；"
                f"禁止接管或关闭用户或其他 Agent 的 Space（{names}）。"
            )
        else:
            reason = "仅有 user-owned Space，允许新建本任务 agent-owned Space；禁止接管用户窗口。"
        can_create = True
    elif window_count == 1:
        action = "stop_existing_window_without_task_space"
        reason = "已有 Ego Lite 窗口但未发现可复用 Space，禁止在未知状态下创建。"
        can_create = False
    else:
        action = "create_first_task_space"
        reason = "当前没有窗口和可复用 Space，允许创建首个任务 Space。"
        can_create = True

    return {
        "action": action,
        "reason": reason,
        "can_create_task_space": can_create,
        "single_window_enforced": len(work_titles) <= 1,
        "reusable_task_space_ids": reusable_ids,
        "foreign_agent_spaces": foreign_spaces,
        "work_window_titles": work_titles,
        "empty_window_titles": empty_titles,
    }


def probe_windows() -> dict[str, Any]:
    completed = subprocess.run(
        ["osascript", "-e", WINDOW_PROBE],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "Ego Lite 窗口检查失败")

    lines = completed.stdout.rstrip("\n").splitlines()
    if len(lines) < 2:
        raise RuntimeError("Ego Lite 窗口检查返回格式异常")

    return {
        "app_running": lines[0] == "1",
        "window_count": int(lines[1]),
        "window_titles": lines[2:],
    }


def parse_task_spaces_output(stdout: str, stderr: str) -> list[dict[str, Any]]:
    marker = "__EGO_WINDOW_GUARD__"
    for line in (*stdout.splitlines(), *stderr.splitlines()):
        if line.startswith(marker):
            spaces = json.loads(line[len(marker) :])
            if not isinstance(spaces, list):
                raise RuntimeError("listTaskSpaces() 未返回数组")
            return spaces
    raise RuntimeError(stderr.strip() or stdout.strip() or "无法读取 Ego Lite Task Space")


def probe_task_spaces(app_running: bool) -> list[dict[str, Any]]:
    if not app_running:
        return []

    executable = shutil.which("ego-browser")
    if executable is None:
        raise RuntimeError("找不到 ego-browser，禁止在未知状态下创建窗口")

    completed = subprocess.run(
        [executable, "nodejs"],
        input=TASK_SPACE_PROBE,
        check=False,
        capture_output=True,
        text=True,
    )
    return parse_task_spaces_output(completed.stdout, completed.stderr)


def collect_state(*, space_prefix: str = "") -> dict[str, Any]:
    windows = probe_windows()
    work_titles, empty_titles = classify_window_titles(
        windows["window_titles"], windows["window_count"]
    )
    if len(work_titles) > 1:
        spaces: list[dict[str, Any]] = []
    else:
        spaces = probe_task_spaces(windows["app_running"])
    return {
        **windows,
        "task_spaces": spaces,
        **decide(
            windows["window_count"],
            spaces,
            windows["window_titles"],
            space_prefix=space_prefix,
        ),
        "work_window_titles": work_titles,
        "empty_window_titles": empty_titles,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="检查 Ego Lite 窗口与 Task Space 归属，防止双窗口与跨任务误抢。"
    )
    parser.add_argument(
        "--space-prefix",
        default="",
        help="限定只复用名称以该前缀开头的 agent-owned Space；存在其他活跃 Agent Space 时放行新建本任务隔离 Space，并在 foreign_agent_spaces 中列出禁止触碰的对象。",
    )
    args = parser.parse_args(argv)

    try:
        state = collect_state(space_prefix=args.space_prefix)
    except Exception as exc:  # Fail closed: unknown browser state is unsafe.
        print(
            json.dumps(
                {"action": "stop_probe_failed", "reason": str(exc)},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 4

    print(json.dumps(state, ensure_ascii=False, indent=2))
    return {
        "stop_duplicate_windows": 2,
        "stop_existing_window_without_task_space": 3,
    }.get(state["action"], 0)


if __name__ == "__main__":
    sys.exit(main())

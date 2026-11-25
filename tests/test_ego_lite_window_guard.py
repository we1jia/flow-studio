import importlib.util
from pathlib import Path
import unittest


MODULE_PATH = Path(__file__).parents[1] / "ego_lite_window_guard.py"
SPEC = importlib.util.spec_from_file_location("ego_lite_window_guard", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)



class EgoLiteWindowGuardDecisionTest(unittest.TestCase):
    def test_allows_first_task_space_only_when_no_window_exists(self):
        decision = MODULE.decide(window_count=0, task_spaces=[])

        self.assertEqual("create_first_task_space", decision["action"])
        self.assertTrue(decision["can_create_task_space"])

    def test_blocks_named_task_space_creation_when_normal_window_exists(self):
        decision = MODULE.decide(window_count=1, task_spaces=[])

        self.assertEqual("stop_existing_window_without_task_space", decision["action"])
        self.assertFalse(decision["can_create_task_space"])

    def test_requires_numeric_reuse_when_agent_task_space_exists(self):
        decision = MODULE.decide(
            window_count=1,
            task_spaces=[{"id": 13, "name": "X 评论", "ownership": "agent"}],
        )

        self.assertEqual("reuse_existing_task_space", decision["action"])
        self.assertFalse(decision["can_create_task_space"])
        self.assertEqual([13], decision["reusable_task_space_ids"])

    def test_reuses_background_task_space_even_when_no_window_is_visible(self):
        decision = MODULE.decide(
            window_count=0,
            task_spaces=[{"id": 1, "name": "抖音诊断", "ownership": "agent"}],
        )

        self.assertEqual("reuse_existing_task_space", decision["action"])
        self.assertFalse(decision["can_create_task_space"])
        self.assertEqual([1], decision["reusable_task_space_ids"])

    def test_blocks_all_browser_work_when_multiple_work_windows_exist(self):
        decision = MODULE.decide(
            window_count=2,
            task_spaces=[{"id": 13, "name": "X 评论", "ownership": "agent"}],
        )

        self.assertEqual("stop_duplicate_windows", decision["action"])
        self.assertFalse(decision["can_create_task_space"])

    def test_does_not_reuse_user_owned_space(self):
        decision = MODULE.decide(
            window_count=1,
            task_spaces=[{"id": 13, "name": "tweet-inspect", "ownership": "user"}],
            window_titles=["tweet-inspect"],
        )

        self.assertEqual("create_agent_task_space", decision["action"])
        self.assertTrue(decision["can_create_task_space"])
        self.assertEqual([], decision["reusable_task_space_ids"])

    def test_does_not_reuse_agent_delegated_to_user_space(self):
        decision = MODULE.decide(
            window_count=1,
            task_spaces=[{"id": 7, "name": "muse", "ownership": "agentDelegatedToUser"}],
            window_titles=["muse"],
        )

        self.assertEqual("create_agent_task_space", decision["action"])
        self.assertTrue(decision["can_create_task_space"])
        self.assertEqual([], decision["reusable_task_space_ids"])

    def test_attaches_empty_window_instead_of_stopping(self):
        decision = MODULE.decide(
            window_count=2,
            task_spaces=[{"id": 13, "name": "tweet-inspect", "ownership": "user"}],
            window_titles=["tweet-inspect", "about:blank"],
        )

        self.assertEqual("attach_empty_window", decision["action"])
        self.assertTrue(decision["can_create_task_space"])
        self.assertEqual([], decision["reusable_task_space_ids"])
        self.assertEqual(["about:blank"], decision["empty_window_titles"])

    def test_still_stops_two_real_work_windows(self):
        decision = MODULE.decide(
            window_count=2,
            task_spaces=[],
            window_titles=["tweet-inspect", "Notifications / X"],
        )

        self.assertEqual("stop_duplicate_windows", decision["action"])
        self.assertFalse(decision["can_create_task_space"])

    def test_prefers_agent_owned_id_over_user_owned(self):
        decision = MODULE.decide(
            window_count=1,
            task_spaces=[
                {"id": 13, "name": "tweet-inspect", "ownership": "user"},
                {"id": 20, "name": "agent-window", "ownership": "agent"},
            ],
            window_titles=["tweet-inspect"],
        )

        self.assertEqual("reuse_existing_task_space", decision["action"])
        self.assertEqual([20], decision["reusable_task_space_ids"])

    def test_space_prefix_creates_isolated_space_instead_of_blocking_or_hijacking(self):
        decision = MODULE.decide(
            window_count=1,
            task_spaces=[
                {"id": 4, "name": "O2T Console", "ownership": "user"},
                {"id": 80, "name": "flow studio video", "ownership": "agent"},
            ],
            window_titles=["O2T Console"],
            space_prefix="news-radar",
        )

        self.assertEqual("create_agent_task_space", decision["action"])
        self.assertTrue(decision["can_create_task_space"])
        self.assertEqual([], decision["reusable_task_space_ids"])
        self.assertEqual(1, len(decision["foreign_agent_spaces"]))
        self.assertEqual(80, decision["foreign_agent_spaces"][0]["id"])

    def test_space_prefix_attaches_empty_window_without_blocking_foreign_space(self):
        decision = MODULE.decide(
            window_count=2,
            task_spaces=[
                {"id": 4, "name": "O2T Console", "ownership": "user"},
                {"id": 80, "name": "flow studio video", "ownership": "agent"},
            ],
            window_titles=["O2T Console", "about:blank"],
            space_prefix="mail-task",
        )

        self.assertEqual("attach_empty_window", decision["action"])
        self.assertTrue(decision["can_create_task_space"])
        self.assertEqual([], decision["reusable_task_space_ids"])
        self.assertEqual(["about:blank"], decision["empty_window_titles"])
        self.assertEqual(1, len(decision["foreign_agent_spaces"]))
        self.assertEqual(80, decision["foreign_agent_spaces"][0]["id"])

    def test_space_prefix_creates_space_when_only_foreign_agent_space_exists(self):
        decision = MODULE.decide(
            window_count=1,
            task_spaces=[
                {"id": 80, "name": "flow studio video", "ownership": "agent"},
            ],
            window_titles=["flow studio video"],
            space_prefix="mail-task",
        )

        self.assertEqual("create_agent_task_space", decision["action"])
        self.assertTrue(decision["can_create_task_space"])
        self.assertEqual([], decision["reusable_task_space_ids"])
        self.assertEqual(1, len(decision["foreign_agent_spaces"]))
        self.assertEqual(80, decision["foreign_agent_spaces"][0]["id"])

    def test_space_prefix_reuses_matching_prefix_space_only(self):
        decision = MODULE.decide(
            window_count=1,
            task_spaces=[
                {"id": 80, "name": "flow studio video", "ownership": "agent"},
                {"id": 91, "name": "news-radar-collect", "ownership": "agent"},
            ],
            window_titles=["O2T Console"],
            space_prefix="news-radar",
        )

        self.assertEqual("reuse_existing_task_space", decision["action"])
        self.assertEqual([91], decision["reusable_task_space_ids"])
        self.assertEqual(1, len(decision["foreign_agent_spaces"]))


class EgoLiteTaskSpaceProbeOutputTest(unittest.TestCase):
    def test_reads_cli_log_marker_from_stderr(self):
        spaces = MODULE.parse_task_spaces_output(
            stdout="",
            stderr=(
                '__EGO_WINDOW_GUARD__'
                '[{"id":13,"name":"X 评论","ownership":"agent"}]\n'
            ),
        )

        self.assertEqual(13, spaces[0]["id"])


if __name__ == "__main__":
    unittest.main()

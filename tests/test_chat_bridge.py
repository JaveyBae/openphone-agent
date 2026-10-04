import sys
import unittest
import json
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from chat_bridge import failure_message, progress_message, result_summary


class ProgressMessageTest(unittest.TestCase):
    def test_maps_agent_action(self) -> None:
        self.assertEqual(
            progress_message("[3/10] swipe: inspect more content"),
            "正在查看更多内容…",
        )

    def test_ignores_non_step_output(self) -> None:
        self.assertIsNone(progress_message("Run log: runs/example"))

    def test_maps_workspace_initialization_progress(self):
        self.assertEqual(
            "正在准备独立工作区，确认不会读取主屏…",
            progress_message("[0/15] initialize_workspace: verify content"),
        )

    def test_startup_error_is_explained_in_chinese(self):
        message = failure_message("Agent error: private workspace has no verified content; refusing a mirror")
        self.assertIn("保护主屏", message)
        self.assertNotIn("Agent error", message)
        self.assertEqual("unrelated error", failure_message("unrelated error"))

    def test_preserves_multiline_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "one"
            run.mkdir()
            (run / "run.json").write_text(json.dumps({"summary": "one\ntwo"}))
            self.assertEqual(result_summary([f"Run log: {run}"], root), "one\ntwo")

    def test_rejects_result_outside_runs(self):
        self.assertIsNone(result_summary(["Run log: /tmp/elsewhere"], Path("runs")))


if __name__ == "__main__":
    unittest.main()

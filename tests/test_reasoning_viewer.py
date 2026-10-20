import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import reasoning_viewer as rv


class DescribeActionTest(unittest.TestCase):
    def test_actions_read_in_plain_chinese(self):
        self.assertEqual("点击 (360,211)", rv.describe_action({"action": "tap", "arguments": {"x": 360, "y": 211}}))
        self.assertEqual("输入「李华」", rv.describe_action({"action": "input_text", "arguments": {"text": "李华"}}))
        self.assertEqual("交给主人处理 (Take_over)", rv.describe_action({"action": "request_user"}))
        self.assertEqual("返回键", rv.describe_action({"action": "key", "arguments": {"keycode": 4}}))


class RenderStepTest(unittest.TestCase):
    def test_a_model_step_shows_reason_action_and_result(self):
        block = rv.render_step({"step": 0, "result": "blocked_unknown_control",
                                "action": {"action": "tap", "arguments": {"x": 1, "y": 2}, "reason": "打开对话"}})
        self.assertIn("[1]", block)            # steps are shown 1-indexed
        self.assertIn("打开对话", block)         # the chain-of-thought
        self.assertIn("点击 (1,2)", block)       # the action, in words
        self.assertIn("安全守卫拦下", block)      # the result label

    def test_feedback_is_shown_when_present(self):
        block = rv.render_step({"step": 3, "result": "sms_body_missing", "told": "先填正文再交接",
                                "action": {"action": "request_user", "reason": "交接"}})
        self.assertIn("反馈：先填正文再交接", block)

    def test_non_step_log_lines_are_skipped(self):
        self.assertIsNone(rv.render_step({"stage": "workspace_reset", "result": "executed"}))
        self.assertIsNone(rv.render_step({"action": "not-a-dict"}))


class LatestRunTest(unittest.TestCase):
    def test_picks_the_most_recently_touched_run(self):
        import tempfile
        import os
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("20261020-010000", "20261020-020000"):
                (root / name).mkdir()
                (root / name / "events.jsonl").write_text("")
            os.utime(root / "20261020-020000" / "events.jsonl", None)  # make it newest
            self.assertEqual("20261020-020000", rv.latest_run(root).name)
            self.assertIsNone(rv.latest_run(root / "empty-missing"))


if __name__ == "__main__":
    unittest.main()

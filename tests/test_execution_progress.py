import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from execution_progress import PREFIX, parse_progress, progress_text


class ExecutionProgressTest(unittest.TestCase):
    def test_structured_steps_have_safe_public_copy(self):
        event = {"stage": "model", "step": 2, "total": 15,
                 "reason": "private chain of thought", "title": "do not display me"}
        message = progress_text(parse_progress(PREFIX + json.dumps(event)))
        self.assertIn("第 2/15 步", message)
        self.assertIn("模型处理中", message)
        self.assertNotIn("thought", message)
        self.assertNotIn("display me", message)

    def test_unknown_and_malformed_progress_rejected(self):
        for event in ({"stage": "thought", "step": 1, "total": 10},
                      {"stage": "model", "step": True, "total": 10},
                      {"stage": "model", "step": 2, "total": 1},
                      {"stage": "model", "step": 1, "total": 51},
                      {"stage": [], "step": 1, "total": 10},
                      {"stage": "model", "step": 1, "total": 10, "action": []}):
            self.assertIsNone(parse_progress(PREFIX + json.dumps(event)))
        self.assertIsNone(parse_progress(PREFIX + "not json"))
        self.assertIsNone(parse_progress("<think>reasoning</think>"))

    def test_input_contents_and_model_reason_are_not_progress(self):
        message = progress_text({"stage": "execute", "step": 3, "total": 15,
                                 "action": "input_text", "text": "private message body"})
        self.assertIn("填写输入框", message)
        self.assertNotIn("private message", message)

    def test_sms_has_explicit_app_label(self):
        self.assertIn("Messages（短信）", progress_text({"stage": "launch", "step": 0,
                      "total": 15, "package": "com.google.android.apps.messaging"}))

    def test_replan_does_not_claim_click_success(self):
        message = progress_text({"stage": "replan", "step": 2, "total": 15})
        self.assertIn("未执行点击", message)
        self.assertIn("其他路径", message)

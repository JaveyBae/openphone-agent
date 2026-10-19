import itertools
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import skill_runner as runner
from message_params import SmsParams, parse_sms

M = "com.google.android.apps.messaging:id/"


class SmsParamsTest(unittest.TestCase):
    def test_explicit_number_and_body_only(self):
        self.assertEqual(SmsParams("183492", "晚会儿到。"), parse_sms("给183492发短信，内容是：晚会儿到。"))
        self.assertEqual(SmsParams("13800138000", "明天见"), parse_sms("发短信给 138 0013 8000，内容是「明天见」"))

    def test_unclear_requests_fall_back(self):
        for goal in ("给老王发短信说我晚点到", "给183492和183493发短信，内容是好的",
                     "发消息给183492说好的", "给183492发短信", "给183492发微信，内容是好的"):
            with self.subTest(goal=goal):
                self.assertIsNone(parse_sms(goal))


class FakeMessages:
    def __init__(self, existing_draft="Text message", picker="Send to 555-0100"):
        self.calls, self.draft, self.picker, self.screen = [], existing_draft, picker, "home"

    def call(self, mode, selector=None, text=None):
        self.calls.append((mode, selector, text))
        if mode == "click" and selector == {"id": M + "start_chat_fab"}:
            self.screen = "new"
        elif mode == "click" and selector == {"text": self.picker}:
            self.screen = "conversation"
        elif mode == "text" and selector == {"id": M + "compose_message_text"}:
            self.draft = text
        return {"ok": True}

    def snapshot(self):
        if self.screen == "home":
            return [{"id": M + "start_chat_fab"}]
        if self.screen == "new":
            return [{"class": "android.widget.EditText"}, {"text": self.picker}]
        return [{"id": M + "compose_message_text", "text": self.draft}, {"text": "555-0100"}]

    def close(self):
        pass


class SmsSkillTest(unittest.TestCase):
    PARAMS = SmsParams("5550100", "明天见")

    def run_with(self, ui):
        ui.c = Mock()  # the scoped key-input channel used for the digits
        with patch.object(runner, "PrivateUiClient", return_value=ui), patch.object(runner.time, "sleep"), \
                patch.object(runner.time, "monotonic", side_effect=itertools.count(0, 0.5)):
            return runner.run_skill(Mock(), runner.load_skill("messages_sms_draft"), self.PARAMS, lambda _: None)

    def test_draft_is_written_and_handed_to_the_owner(self):
        ui = FakeMessages()
        message = self.run_with(ui)
        self.assertIn("未发送", message)
        self.assertIn("由你手动发送", message)
        self.assertIn(("click", {"text": "Send to 555-0100"}, None), ui.calls)  # matched by digits
        ui.c.execute.assert_any_call({"action": "input_text", "arguments": {"text": "5550100"}})
        self.assertEqual("明天见", ui.draft)
        self.assertFalse(any("Send SMS" in str(c[1]) for c in ui.calls))

    def test_an_existing_different_draft_is_never_overwritten(self):
        ui = FakeMessages(existing_draft="晚会儿到。")
        with self.assertRaises(runner.SkillPaused) as raised:
            self.run_with(ui)
        self.assertIn("没有覆盖", str(raised.exception))
        self.assertEqual("晚会儿到。", ui.draft)

    def test_the_same_draft_is_not_retyped(self):
        ui = FakeMessages(existing_draft="明天见")
        self.run_with(ui)
        self.assertFalse(any(c[0] == "text" and c[1] == {"id": M + "compose_message_text"} for c in ui.calls))

    def test_a_different_number_in_the_picker_is_not_chosen(self):
        ui = FakeMessages(picker="Send to 555-0199")
        with self.assertRaises(runner.SkillFallback):
            self.run_with(ui)
        self.assertFalse(any(c[1] == {"text": "Send to 555-0199"} for c in ui.calls))


if __name__ == "__main__":
    unittest.main()

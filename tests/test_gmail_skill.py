import itertools
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import skill_runner as runner
from message_params import MailParams, parse_mail, subject_from_body

G = "com.google.android.gm:id/"


class MailParamsTest(unittest.TestCase):
    def test_address_subject_and_body_from_the_owner(self):
        self.assertEqual(MailParams("friend@example.com", "", "周五的读书会改到七点"),
                         parse_mail("尝试写个邮件发送给friend@example.com 告诉他周五的读书会改到七点"))
        self.assertEqual(MailParams("test@example.com", "周会通知", "明天上午10点开周会"),
                         parse_mail("给 test@example.com 写封邮件，主题是周会通知，内容是明天上午10点开周会"))

    def test_incomplete_or_ambiguous_addresses_fall_back(self):
        for goal in ("尝试写个邮件发送给friendexample.com 告诉他读书会改时间了", "给 a@b.com 和 c@d.com 写邮件，内容是好的",
                     "给 a@b.com 写封邮件", "打开邮箱看看最新邮件"):
            with self.subTest(goal=goal):
                self.assertIsNone(parse_mail(goal))

    def test_subject_is_the_first_clause(self):
        self.assertEqual("读书会改时间了", subject_from_body("读书会改时间了，周五七点开始"))
        self.assertEqual("Please review the attached budget before",
                         subject_from_body("Please review the attached budget before Friday afternoon, thanks"))


class FakeGmail:
    def __init__(self):
        self.calls, self.screen, self.fields = [], "inbox", {}
        self.c = Mock()

    def call(self, mode, selector=None, text=None):
        self.calls.append((mode, selector, text))
        if selector == {"id": G + "compose_button"}:
            self.screen = "compose"
        elif selector == {"class": "android.widget.TextView", "text": "a@example.com"}:
            self.fields["to"] = "a@example.com"
        elif mode == "text":
            self.fields["subject" if selector == {"id": G + "subject"} else "body"] = text
        return {"ok": True}

    def snapshot(self):
        if self.screen == "inbox":
            return [{"id": G + "compose_button"}]
        nodes = [{"id": G + "to_prefix_text"}, {"id": G + "subject", "text": self.fields.get("subject", "Subject")},
                 {"class": "android.widget.EditText", "text": self.fields.get("body", "Compose email")}]
        if "to" in self.fields:
            nodes.append({"description": "Recipients: a@example.com,"})
        return nodes

    def close(self):
        pass


class GmailSkillTest(unittest.TestCase):
    def test_draft_is_filled_verified_and_handed_over_never_sent(self):
        ui = FakeGmail()
        with patch.object(runner, "PrivateUiClient", return_value=ui), patch.object(runner.time, "sleep"), \
                patch.object(runner.time, "monotonic", side_effect=itertools.count(0, 0.5)):
            message = runner.run_skill(Mock(), runner.load_skill("gmail_draft"),
                                       MailParams("a@example.com", "周会通知", "明天上午10点开周会"), lambda _: None)
        ui.c.execute.assert_called_once_with({"action": "input_text", "arguments": {"text": "a@example.com"}})
        self.assertEqual({"to": "a@example.com", "subject": "周会通知", "body": "明天上午10点开周会"}, ui.fields)
        self.assertIn("未发送", message)
        self.assertFalse(any(c[1] == {"id": G + "send"} for c in ui.calls))


if __name__ == "__main__":
    unittest.main()

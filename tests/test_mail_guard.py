import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import agent_loop as agent
import agent_session
from app_targets import (APP_PACKAGES, GMAIL_PACKAGE, MAIL_DRAFT_PREFIX, is_mail_compose_goal,
                         mail_completion_handoff, mail_draft_goal)
from test_workspace_isolation import FOCUS, SESSION, STATE

GMAIL_RESUMED = ("Display #5 (activities from top to bottom):\n"
                 f"  topResumedActivity=ActivityRecord{{abc u0 {GMAIL_PACKAGE}/.ComposeActivityGmail t80}}\n")


class MailGoalTest(unittest.TestCase):
    def test_compose_goals_are_draft_only_but_reading_is_not(self):
        for goal in ("给 test@example.com 写封邮件，主题周会通知", "帮我发邮件给老王",
                     "回复这封邮件", "Send an email to a@b.com"):
            with self.subTest(goal=goal):
                self.assertTrue(is_mail_compose_goal(goal))
                self.assertTrue(mail_draft_goal(goal).startswith(MAIL_DRAFT_PREFIX))
                self.assertTrue(mail_draft_goal(goal).endswith(goal))
        for goal in ("打开邮箱看看最新邮件", "查看 Gmail 收件箱", "帮我建一个10月18日的会议"):
            with self.subTest(goal=goal):
                self.assertFalse(is_mail_compose_goal(goal))
                self.assertEqual(goal, mail_draft_goal(goal))

    def test_draft_finish_is_never_reported_as_sent(self):
        finish = {"action": "finish", "arguments": {"summary": "邮件已发送"}}
        handed = mail_completion_handoff("给 a@b.com 写封邮件", finish)
        self.assertEqual("request_user", handed["action"])
        self.assertIn("未自动发送", handed["arguments"]["message"])
        self.assertIs(finish, mail_completion_handoff("打开邮箱看看最新邮件", finish))

    def test_gmail_is_launchable_and_accepts_private_unicode(self):
        self.assertEqual(GMAIL_PACKAGE, APP_PACKAGES["gmail"])
        self.assertEqual(GMAIL_PACKAGE, APP_PACKAGES["邮件"])
        self.assertIn(GMAIL_PACKAGE, agent.TEXT_PACKAGES)


class MailControlGuardTest(unittest.TestCase):
    def controller(self, check_result):
        controller = agent.AndroidController("unused")
        controller.require_display = Mock(return_value=5)
        controller.require_isolation = Mock()
        controller.state = Mock(return_value=STATE)
        controller._text_helper_jar = "/data/local/tmp/openphone-workspace/private-text.jar"

        def run(*args, **kwargs):
            if args[-1] == "input" and "dumpsys" in args:
                return FOCUS
            if "activities" in args:
                return GMAIL_RESUMED
            if args[0] == "shell" and len(args) == 2 and "mail-check" in args[1]:
                return json.dumps(check_result)
            return ""
        controller._run = Mock(side_effect=run)
        return controller

    @staticmethod
    def check(**override):
        return {"ok": True, "verified": True, "executed": False, "allowed": True, "displayId": 5,
                "session": SESSION, "package": GMAIL_PACKAGE, "method": "mail_control_check", **override}

    @staticmethod
    def raw_inputs(controller, gesture):
        return [c.args for c in controller._run.call_args_list if "input" in c.args and gesture in c.args]

    def test_send_or_delete_control_is_not_tapped(self):
        controller = self.controller(self.check(allowed=False))
        with self.assertRaises(agent.MailControlBlocked):
            controller.execute({"action": "tap", "arguments": {"x": 640, "y": 60}})
        helper = [c.args[1] for c in controller._run.call_args_list if "mail-check" in str(c.args)]
        self.assertEqual(1, len(helper))
        self.assertIn(f"5 {SESSION} {GMAIL_PACKAGE} mail-check 640 60", helper[0])
        self.assertEqual([], self.raw_inputs(controller, "tap"))

    def test_ordinary_control_is_tapped_on_private_display_only(self):
        controller = self.controller(self.check())
        controller.execute({"action": "tap", "arguments": {"x": 300, "y": 400}})
        self.assertEqual([("shell", "input", "-d", "5", "tap", "300", "400")], self.raw_inputs(controller, "tap"))

    def test_unverified_check_fails_closed_without_tapping(self):
        for override in ({"displayId": 0}, {"session": "stale"}, {"executed": True}, {"verified": False},
                         {"method": "raw_tap"}, {"allowed": "yes"}, {"ok": False}):
            with self.subTest(override=override):
                controller = self.controller(self.check(**override))
                with self.assertRaisesRegex(agent.AgentError, "Mail safety guard"):
                    controller.execute({"action": "tap", "arguments": {"x": 300, "y": 400}})
                self.assertEqual([], self.raw_inputs(controller, "tap"))

    def test_sideways_swipe_in_gmail_is_refused_vertical_scroll_allowed(self):
        controller = self.controller(self.check())
        with self.assertRaises(agent.MailControlBlocked):
            controller.execute({"action": "swipe", "arguments": {
                "x1": 100, "y1": 500, "x2": 650, "y2": 520, "duration_ms": 300}})
        self.assertEqual([], self.raw_inputs(controller, "swipe"))
        controller.execute({"action": "swipe", "arguments": {
            "x1": 360, "y1": 900, "x2": 360, "y2": 300, "duration_ms": 300}})
        self.assertEqual(1, len(self.raw_inputs(controller, "swipe")))


class MailLoopTest(unittest.TestCase):
    def run_loop(self, goal, actions, gmail_resumed=True):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        args = agent.build_parser().parse_args([
            "--goal", goal, "--provider", "mock", "--dry-run", "--runs-dir", directory.name, "--max-steps", "6"])
        controller = Mock()
        controller.require_display.return_value = 7
        controller.prepare_workspace.return_value = False
        controller.capture.side_effect = lambda p: agent.Observation(p, "private-frame", 7)
        controller.mail_is_resumed.return_value = gmail_resumed
        planner = Mock()
        planner.next_action.side_effect = actions
        with patch.object(agent, "locate_adb", return_value="unused"), \
             patch.object(agent, "AndroidController", return_value=controller), \
             patch.object(agent, "MockPlanner", return_value=planner), \
             patch.object(agent_session, "log_event"), patch.object(agent.time, "sleep"):
            result = agent.run(args)
        run = json.loads(next(Path(directory.name).glob("*/run.json")).read_text())
        return result, controller, run

    TAP_TO = {"action": "tap", "arguments": {"x": 359, "y": 294}, "risk": "none",
              "reason": "The task asks me to send email. First tap the To field."}
    PAUSE = {"action": "request_user", "arguments": {"message": "草稿已写好"}}

    def test_send_wording_in_gmail_draft_is_left_to_control_guard(self):
        result, controller, run = self.run_loop("给 a@b.com 写封邮件，主题周会", [self.TAP_TO, self.PAUSE])
        self.assertEqual(2, result)
        executed = [c.args[0] for c in controller.execute.call_args_list]
        self.assertIn(self.TAP_TO, executed)
        self.assertNotIn("safety_reason", run)

    def test_other_sensitive_words_or_other_contexts_still_block(self):
        password = {**self.TAP_TO, "reason": "send email after entering the password"}
        for goal, action, resumed in (("给 a@b.com 写封邮件", password, True),
                                      ("给 a@b.com 写封邮件", self.TAP_TO, False),
                                      ("打开京东搜索 5090", self.TAP_TO, True)):
            with self.subTest(goal=goal, reason=action["reason"], resumed=resumed):
                result, controller, run = self.run_loop(goal, [action], gmail_resumed=resumed)
                self.assertEqual(2, result)
                self.assertNotIn(action, [c.args[0] for c in controller.execute.call_args_list])
                self.assertIn("safety_reason", run)

    def test_permission_dialog_pauses_without_any_tap(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        args = agent.build_parser().parse_args([
            "--goal", "帮我在日历里加一个明天上午10点的站会", "--provider", "mock", "--dry-run",
            "--runs-dir", directory.name, "--max-steps", "6"])
        controller = Mock()
        controller.require_display.return_value = 7
        controller.prepare_workspace.return_value = False
        controller.capture.side_effect = lambda p: agent.Observation(p, "private-frame", 7)
        controller.permission_prompt_open.return_value = True
        planner = Mock()
        deny = {"action": "tap", "arguments": {"x": 360, "y": 900}, "reason": "Tap Don't allow"}
        planner.next_action.side_effect = [deny]
        with patch.object(agent, "locate_adb", return_value="unused"), \
             patch.object(agent, "AndroidController", return_value=controller), \
             patch.object(agent, "MockPlanner", return_value=planner), \
             patch.object(agent_session, "log_event"), patch.object(agent.time, "sleep"):
            self.assertEqual(2, agent.run(args))
        self.assertNotIn(deny, [c.args[0] for c in controller.execute.call_args_list])
        run = json.loads(next(Path(directory.name).glob("*/run.json")).read_text())
        self.assertIn("没有替你选择", run["message"])

    def test_blocked_mail_control_pauses_for_owner_without_replanning(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        args = agent.build_parser().parse_args([
            "--goal", "给 test@example.com 写封邮件，主题周会通知", "--provider", "mock",
            "--dry-run", "--runs-dir", directory.name, "--max-steps", "6"])
        controller = Mock()
        controller.require_display.return_value = 7
        controller.prepare_workspace.return_value = False
        controller.capture.side_effect = lambda p: agent.Observation(p, "private-frame", 7)
        # The draft task first opens Gmail itself, then the model's tap is rejected.
        controller.execute.side_effect = [None, agent.MailControlBlocked("send rejected")]
        planner = Mock()
        planner.next_action.side_effect = [{"action": "tap", "arguments": {"x": 640, "y": 60}}]
        with patch.object(agent, "locate_adb", return_value="unused"), \
             patch.object(agent, "AndroidController", return_value=controller), \
             patch.object(agent, "MockPlanner", return_value=planner), \
             patch.object(agent_session, "log_event"), patch.object(agent.time, "sleep"):
            self.assertEqual(2, agent.run(args))
        self.assertEqual(1, planner.next_action.call_count)
        self.assertTrue(planner.next_action.call_args.args[0].startswith(MAIL_DRAFT_PREFIX))
        run = json.loads(next(Path(directory.name).glob("*/run.json")).read_text())
        self.assertEqual("awaiting_user", run["status"])
        self.assertIn("没有执行", run["message"])


if __name__ == "__main__":
    unittest.main()

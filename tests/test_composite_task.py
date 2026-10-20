"""A "draft THEN another task" request runs in two legs: the draft pauses for the
owner to take it to the main screen, then the bridge continues with the rest on the
private display (now freed by the takeover)."""
import io
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import agent_session
from app_targets import continuation_goal, draft_phase_goal, expected_sms_body, split_composite_goal
from chat_bridge import Bridge


class SplitCompositeGoalTest(unittest.TestCase):
    def test_sms_draft_then_another_task_is_split(self):
        first, rest = split_composite_goal(
            "给联系人李华发一条短信『我下周要出差，需要请个假』，然后再去 12306 预订一张下周一北京到广州的高铁票")
        self.assertEqual(first, "给联系人李华发一条短信『我下周要出差，需要请个假』")
        self.assertEqual(rest, "12306 预订一张下周一北京到广州的高铁票")

    def test_mail_draft_then_another_task_is_split(self):
        first, rest = split_composite_goal("写一封邮件给张三说项目延期，接着去日历上建个会议")
        self.assertEqual(first, "写一封邮件给张三说项目延期")
        self.assertEqual(rest, "日历上建个会议")

    def test_non_draft_first_part_is_left_whole(self):
        # A query that merely contains "然后" is one task for the model, not a boundary.
        self.assertEqual(
            split_composite_goal("打开12306查明天北京到上海的高铁然后告诉我最早一班"),
            ("打开12306查明天北京到上海的高铁然后告诉我最早一班", None))

    def test_connector_inside_the_quoted_body_is_not_a_boundary(self):
        self.assertEqual(
            split_composite_goal("给李华发短信『先开会然后再吃饭』"),
            ("给李华发短信『先开会然后再吃饭』", None))

    def test_plain_sms_draft_has_no_continuation(self):
        self.assertEqual(
            split_composite_goal("给联系人李华发一条短信：周一的会我参加不了"),
            ("给联系人李华发一条短信：周一的会我参加不了", None))


def _bridge() -> Bridge:
    bridge = Bridge.__new__(Bridge)
    bridge.args = SimpleNamespace(provider="mock", model="", runs_dir=Path("runs"),
                                  max_steps=40, initial_url="https://example.com", api_key="")
    bridge.save_state = Mock()
    bridge.update = Mock()
    bridge.last_task_id = ""
    bridge.continuation = None
    return bridge


class ContinuationStateTest(unittest.TestCase):
    def test_paused_draft_leg_stores_the_rest_and_runs_only_the_first_leg(self):
        bridge = _bridge()
        process = Mock(stdout=io.StringIO("Paused for user: 草稿已准备，未发送\n"))
        process.wait.return_value = 2  # PAUSED: the draft is handed to the owner
        process.poll.return_value = 2
        goal = "给李华发短信『周一的会我参加不了』，然后再去12306订一张下周一北京到广州的高铁票"
        with patch("chat_bridge.subprocess.Popen", return_value=process) as popen:
            bridge.run_task({"id": "task-1", "goal": goal, "mode": "experience"})
        command = popen.call_args.args[0]
        passed_goal = command[command.index("--goal") + 1]
        # This leg's effective goal is only the SMS part (routing/guards act on Messages)...
        self.assertEqual(passed_goal, "给李华发短信『周一的会我参加不了』")
        self.assertNotIn("12306", passed_goal)
        # ...but the whole connected request travels along and the phase is marked, so the task
        # stays connected rather than split.
        self.assertEqual(command[command.index("--full-goal") + 1], goal)
        self.assertEqual(command[command.index("--phase") + 1], "draft")
        self.assertIsNotNone(bridge.continuation)
        self.assertEqual(bridge.continuation["id"], "task-1")
        self.assertIn("12306", bridge.continuation["goal"])
        self.assertEqual(bridge.continuation["full_goal"], goal)
        self.assertEqual(bridge.continuation["phase"], "continue")
        self.assertEqual(bridge.continuation["mode"], "experience")

    def test_a_plain_draft_leaves_no_continuation(self):
        bridge = _bridge()
        process = Mock(stdout=io.StringIO("Paused for user: 草稿已准备\n"))
        process.wait.return_value = 2
        process.poll.return_value = 2
        with patch("chat_bridge.subprocess.Popen", return_value=process):
            bridge.run_task({"id": "task-2", "goal": "给李华发短信：周一的会我参加不了"})
        self.assertIsNone(bridge.continuation)

    def test_a_failed_draft_leg_does_not_continue(self):
        bridge = _bridge()
        process = Mock(stdout=io.StringIO("Agent error: something broke\n"))
        process.wait.return_value = 1  # not PAUSED: do not carry on to the second app
        process.poll.return_value = 1
        goal = "给李华发短信『请假』，然后再去12306订票"
        with patch("chat_bridge.subprocess.Popen", return_value=process):
            bridge.run_task({"id": "task-3", "goal": goal})
        self.assertIsNone(bridge.continuation)

    def test_continue_after_takeover_runs_the_rest_and_clears_state(self):
        bridge = _bridge()
        bridge.run_task = Mock()
        full = "给李华发短信『请假』，然后再去12306订一张下周一北京到广州的高铁票"
        bridge.continuation = {"id": "task-1", "goal": "12306 订一张下周一北京到广州的高铁票",
                               "full_goal": full, "phase": "continue",
                               "mode": "experience", "observeOnly": None}
        bridge.continue_after_takeover(bridge.continuation)
        self.assertIsNone(bridge.continuation)
        bridge.run_task.assert_called_once()
        task = bridge.run_task.call_args.args[0]
        self.assertEqual(task["id"], "task-1")
        self.assertIn("12306", task["goal"])
        self.assertEqual(task["phase"], "continue")  # the second leg is routed as a continuation
        self.assertEqual(task["full_goal"], full)    # and still carries the whole request
        self.assertEqual(task["mode"], "experience")
        self.assertNotIn("observeOnly", task)  # a falsey value is not forwarded


class ContinuationGoalTest(unittest.TestCase):
    def test_second_leg_prompt_keeps_the_whole_request_and_forbids_reopening(self):
        full = "给李华发短信『请假』，然后再去12306订一张下周一北京到广州的高铁票"
        rest = "12306订一张下周一北京到广州的高铁票"
        prompt = continuation_goal(full, rest)
        self.assertIn(full, prompt)          # the connected description stays in view
        self.assertIn(rest, prompt)          # the remaining step is named
        self.assertIn("不要再打开短信", prompt)  # the draft was handed off; do not redo it

    def test_first_leg_leads_with_the_draft_but_still_shows_the_whole_task(self):
        # Leading with the full request made the model skip the body to rush ahead; the draft leg
        # must be the primary instruction (body prominent), with the whole task as context.
        full = "给联系人李华发一条短信『我下周要出差，需要请个假』，然后再去12306预订一张下周一北京到广州的高铁票"
        leg = "给联系人李华发一条短信『我下周要出差，需要请个假』"
        prompt = draft_phase_goal(full, leg)
        self.assertIn("我下周要出差，需要请个假", prompt)  # the body stays prominent (from the leg)
        self.assertIn(full, prompt)          # the whole request is still shown as context
        self.assertIn("正文", prompt)         # finish the body, do not skip it
        self.assertIn("提前交接", prompt)      # do not hand off early


class SmsBodyGuardTest(unittest.TestCase):
    def test_body_is_taken_from_the_quoted_text_only(self):
        self.assertEqual("我下周要出差，需要请个假",
                         expected_sms_body("给联系人李华发一条短信『我下周要出差，需要请个假』"))
        self.assertIsNone(expected_sms_body("打开短信"))  # nothing quoted: no guard

    def _session(self, history, replans=0):
        session = agent_session.AgentSession.__new__(agent_session.AgentSession)
        session.args = SimpleNamespace(goal="给联系人李华发一条短信『我下周要出差，需要请个假』", max_steps=40)
        session.history = history
        session.sms_body_replans = replans
        session.loops = agent_session.LoopGuard()
        session.log = Mock()
        session.log.pause = Mock(return_value=2)
        return session

    def test_handoff_with_an_empty_body_is_sent_back_to_type_it(self):
        session = self._session([{"action": {"action": "input_text", "arguments": {"text": "李华"}}}])
        action = {"action": "request_user", "arguments": {"message": "草稿已准备"}}
        self.assertIsNone(session.handle_request_user(action, {}, 3, False))  # replan, not pause
        self.assertEqual(session.sms_body_replans, 1)
        self.assertIn("正文还没写进去", session.history[-1]["told"])
        session.log.pause.assert_not_called()

    def test_handoff_after_the_body_is_typed_pauses_for_the_owner(self):
        session = self._session([
            {"action": {"action": "input_text", "arguments": {"text": "李华"}}},
            {"action": {"action": "input_text", "arguments": {"text": "我下周要出差，需要请个假"}}}])
        action = {"action": "request_user", "arguments": {"message": "草稿已准备"}}
        self.assertEqual(2, session.handle_request_user(action, {}, 4, False))
        session.log.pause.assert_called_once()

    def test_a_model_that_never_types_the_body_hands_off_with_an_honest_message(self):
        # After its turns, a model that insists the empty draft is complete must not claim success.
        session = self._session([{"action": {"action": "input_text", "arguments": {"text": "李华"}}}], replans=2)
        action = {"action": "request_user", "arguments": {"message": "草稿已准备，未发送"}}
        self.assertEqual(2, session.handle_request_user(action, {}, 6, False))
        message = session.log.pause.call_args.args[0]
        self.assertIn("正文", message)
        self.assertIn("手动补上", message)


if __name__ == "__main__":
    unittest.main()

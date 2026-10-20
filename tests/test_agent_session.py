import http.client
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import agent_session  # noqa: E402
from agent_common import AgentError  # noqa: E402


class ModelRetryTest(unittest.TestCase):
    def session(self, planner):
        args = SimpleNamespace(goal="定个闹钟", max_steps=10, no_image=False)
        session = agent_session.AgentSession.__new__(agent_session.AgentSession)
        session.args, session.planner, session.replay, session.history = args, planner, None, []
        session.planner_goal, session.controller, session.log = args.goal, Mock(order_guard=False), Mock()
        return session

    def decide(self, session):
        timer = Mock(timing={})
        with patch.object(agent_session.time, "sleep"), patch.object(agent_session, "emit_progress"):
            return session.decide(Mock(display_id=2), None, None, 0, timer)

    def test_a_dropped_connection_is_retried_once(self):
        action = {"action": "tap", "arguments": {"x": 1, "y": 1}}
        planner = Mock(last_timing=None)
        planner.next_action.side_effect = [http.client.RemoteDisconnected("closed"), action]
        self.assertEqual(action, self.decide(self.session(planner))[0])
        self.assertEqual(2, planner.next_action.call_count)

    def test_a_server_that_stays_down_stops_with_an_agent_error(self):
        planner = Mock(last_timing=None)
        planner.next_action.side_effect = ConnectionRefusedError("refused")
        with self.assertRaises(AgentError):
            self.decide(self.session(planner))
        self.assertEqual(2, planner.next_action.call_count)


class CalendarFinishTest(unittest.TestCase):
    def test_wrong_event_time_is_sent_back_to_the_model(self):
        from app_targets import CALENDAR_PACKAGE
        args = SimpleNamespace(goal="帮我在日历里建一个10月27日下午3点到4点的会议，叫基准测试34", max_steps=10)
        session = agent_session.AgentSession.__new__(agent_session.AgentSession)
        session.args, session.target, session.recorder, session.corrections = args, CALENDAR_PACKAGE, None, 0
        session.history, session.log = [], Mock(metadata={})
        wrong = "Row: 0 title=基准测试34, dtstart=1793113200000, dtend=1793116800000"  # 16:00-17:00

        def run(*command):
            return "Europe/Zurich" if "getprop" in command else wrong
        session.controller = Mock(order_guard=False, _run=Mock(side_effect=run))
        session.read_ui = Mock(return_value=(None, None))
        action = {"action": "finish", "arguments": {"summary": "已创建"}}
        with patch.object(agent_session.time, "sleep"), patch.object(agent_session, "emit_progress"), \
                patch.object(agent_session, "datetime", Mock(now=Mock(return_value=__import__("datetime").datetime(2026, 10, 19)))):
            outcome = session.handle_finish(action, {"step": 3}, 3, None)
        self.assertIsNone(outcome)  # not finished: the model gets one correction
        self.assertEqual(["10月27日 15:00–16:00 的「基准测试34」"], session.history[-1]["missing"])

class AlternationTest(unittest.TestCase):
    def test_two_actions_in_turn_stop_after_three_rounds(self):
        guard = agent_session.LoopGuard()
        a = {"action": "tap", "arguments": {"x": 622, "y": 508}}
        b = {"action": "tap", "arguments": {"x": 605, "y": 1088}}
        for index in range(5):
            guard.check_action(a if index % 2 == 0 else b, str(index))
        with self.assertRaisesRegex(AgentError, "alternated"):
            guard.check_action(b, "5")

    def test_filling_a_form_is_not_alternation(self):
        guard = agent_session.LoopGuard()
        for index in range(8):
            guard.check_action({"action": "tap", "arguments": {"x": 1, "y": 1}} if index % 2 == 0 else
                               {"action": "input_text", "arguments": {"text": f"v{index}"}}, str(index))

class AlternationTest(unittest.TestCase):
    def test_two_actions_in_turn_stop_after_three_rounds(self):
        guard = agent_session.LoopGuard()
        a = {"action": "tap", "arguments": {"x": 622, "y": 508}}
        b = {"action": "tap", "arguments": {"x": 605, "y": 1088}}
        for index in range(5):
            guard.check_action(a if index % 2 == 0 else b, str(index))
        with self.assertRaisesRegex(AgentError, "alternated"):
            guard.check_action(b, "5")

    def test_filling_a_form_is_not_alternation(self):
        guard = agent_session.LoopGuard()
        for index in range(8):
            guard.check_action({"action": "tap", "arguments": {"x": 1, "y": 1}} if index % 2 == 0 else
                               {"action": "input_text", "arguments": {"text": f"v{index}"}}, str(index))

class ControlCharacterTest(unittest.TestCase):
    def test_a_line_break_in_typed_text_is_told_to_the_model(self):
        session = agent_session.AgentSession.__new__(agent_session.AgentSession)
        session.args = SimpleNamespace(goal="日历", observe_only=False, max_steps=10)
        session.controller, session.log, session.history = Mock(), Mock(), []
        action = {"action": "input_text", "arguments": {"text": "三楼大会议室\n"}}
        with patch.object(agent_session, "emit_progress"):
            self.assertEqual(agent_session.REPLANNED, session.guard(action, {}, Mock(display_id=2), 3))
        self.assertIn("Enter key", session.history[-1]["result"])

class TappedTextFieldTest(unittest.TestCase):
    def test_the_model_is_told_it_tapped_a_text_field(self):
        from qwen_planner import user_text
        history = [{"action": {"action": "tap", "arguments": {"x": 1, "y": 1}}, "tapped_text_field": True}]
        self.assertIn("do not tap it again", user_text("写邮件", history, 720, 1280))
        self.assertNotIn("do not tap it again", user_text("写邮件", [{**history[0], "tapped_text_field": False}], 720, 1280))

    def test_a_blocked_action_feedback_reaches_the_prompt(self):
        # Without this, the model only saw the action list and repeated the same rejected tap.
        from qwen_planner import user_text
        told = "That tap was NOT performed: tap the 'Start chat' button to open the composer."
        history = [{"action": {"action": "tap", "arguments": {"x": 1, "y": 1}}, "executed": False, "told": told}]
        self.assertIn("Feedback: " + told, user_text("给李华发短信", history, 720, 1280))
        # A normal executed action carries no such feedback line.
        self.assertNotIn("Feedback:", user_text("给李华发短信",
                         [{"action": {"action": "tap", "arguments": {"x": 1, "y": 1}}}], 720, 1280))

class ReaderLeakTest(unittest.TestCase):
    def test_a_failed_read_closes_the_reader(self):
        session = agent_session.AgentSession.__new__(agent_session.AgentSession)
        session.controller, session.log = Mock(), Mock()
        session.controller.require_display.return_value = 2
        reader = Mock()
        reader.snapshot.side_effect = AgentError("private display has no focused window")
        session.ui_readers = {"com.google.android.gm": reader}
        with patch.object(agent_session, "resumed_package", return_value="com.google.android.gm"):
            self.assertEqual(("com.google.android.gm", None), session.read_ui())
        reader.close.assert_called_once()
        self.assertEqual({}, session.ui_readers)


if __name__ == "__main__":
    unittest.main()

import importlib.util
import sys
import unittest
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "agent_loop.py"
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("agent_loop", MODULE_PATH)
agent_loop = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = agent_loop
SPEC.loader.exec_module(agent_loop)
import agent_session  # noqa: E402  (the run loop; tests patch its log_event)


class ParseActionTest(unittest.TestCase):
    def test_parses_plain_json(self):
        action = agent_loop.parse_action(
            '{"action":"tap","arguments":{"x":12,"y":34},"reason":"visible"}'
        )
        self.assertEqual("tap", action["action"])
        self.assertEqual(12, action["arguments"]["x"])
        self.assertEqual("none", action["risk"])

    def test_parses_fenced_json(self):
        action = agent_loop.parse_action(
            '```json\n{"action":"finish","arguments":{"summary":"done"}}\n```'
        )
        self.assertEqual("finish", action["action"])

    def test_rejects_unknown_tool(self):
        with self.assertRaises(agent_loop.AgentError):
            agent_loop.parse_action('{"action":"shell","arguments":{}}')

    def test_coordinate_bounds(self):
        with self.assertRaises(agent_loop.AgentError):
            agent_loop.bounded_int({"x": 721}, "x", 0, 719)

    def test_sensitive_reason(self):
        action = {
            "action": "tap",
            "arguments": {"x": 4, "y": 5},
            "reason": "confirm payment",
            "risk": "none",
        }
        self.assertIsNotNone(agent_loop.sensitive_reason(action))

    def test_owner_display_zero_is_rejected(self):
        controller = agent_loop.AndroidController("unused")
        controller.state = lambda: {"displayId": 0}
        with self.assertRaises(agent_loop.AgentError):
            controller.require_display()

    def test_private_content_flag_is_display_scoped(self):
        dump = """Logical Displays: size=2
  Display 0:
    mHasContent=true
  Display 3:
    mDisplayId=3
    mHasContent=false
"""
        self.assertIs(False, agent_loop.display_has_content(dump, 3))
        self.assertIsNone(agent_loop.display_has_content(dump, 0))
        self.assertIsNone(agent_loop.display_has_content(dump, 2))
        self.assertIs(True, agent_loop.display_has_content(dump.replace("mHasContent=false", "mHasContent=true"), 3))

    def test_mirrored_or_unknown_content_is_rejected(self):
        controller = agent_loop.AndroidController("unused")
        controller._run = lambda *args: "Logical Displays:\n  Display 2:\n    mHasContent=false\n"
        with self.assertRaises(agent_loop.AgentError):
            controller.require_private_content(2)

    def test_package_display_parser_ignores_historical_references(self):
        dump = """Display #0 (activities from top to bottom):
  * Task{abc #1 A=example.owner}
Display #2 (activities from top to bottom):
  * Task{def #2 A=com.android.chrome}
ActivityTaskSupervisor state:
  mLastFocusedRootTask=Task{old A=com.android.chrome displayId=0}
"""
        self.assertEqual(
            {2}, agent_loop.parse_package_displays(dump, "com.android.chrome")
        )


class WorkspaceInitializationTest(unittest.TestCase):
    def controller(self, contents):
        controller = agent_loop.AndroidController("unused")
        controller.require_display = Mock(return_value=4)
        controller.private_content_state = Mock(side_effect=contents)
        controller.execute = Mock()
        return controller

    def test_empty_display_is_initialized_and_verified(self):
        controller = self.controller([False, True])
        self.assertTrue(controller.prepare_workspace("https://example.com", allow_initialize=True))
        controller.execute.assert_called_once_with({
            "action": "open_url", "arguments": {"url": "https://example.com"},
        })

    def test_existing_page_is_not_navigated(self):
        controller = self.controller([True])
        self.assertFalse(controller.prepare_workspace("https://example.com", allow_initialize=True))
        controller.execute.assert_not_called()


    def test_observe_only_never_initializes_empty_display(self):
        controller = self.controller([False, False])
        with self.assertRaises(agent_loop.AgentError):
            controller.prepare_workspace("https://example.com", allow_initialize=False)
        controller.execute.assert_not_called()

    def test_unknown_display_metadata_fails_closed(self):
        controller = self.controller([None, None])
        with self.assertRaises(agent_loop.AgentError):
            controller.prepare_workspace("https://example.com", allow_initialize=True)
        controller.execute.assert_not_called()

    def test_recreated_display_interrupts_initialization(self):
        controller = self.controller([False])
        controller.require_display.side_effect = [4, 5]
        with self.assertRaisesRegex(agent_loop.AgentError, "changed during initialization"):
            controller.prepare_workspace("https://example.com", allow_initialize=True)

    def test_browser_launch_failure_does_not_read_a_frame(self):
        controller = self.controller([False])
        controller.execute.side_effect = agent_loop.AgentError("launch denied")
        controller.capture = Mock()
        with self.assertRaisesRegex(agent_loop.AgentError, "launch denied"):
            controller.prepare_workspace("https://example.com", allow_initialize=True)
        controller.capture.assert_not_called()

    @patch.object(agent_loop.time, "sleep")
    def test_initialization_times_out_without_independent_content(self, sleep):
        controller = self.controller([False] * 21)
        with self.assertRaisesRegex(agent_loop.AgentError, "initialization failed"):
            controller.prepare_workspace("https://example.com", allow_initialize=True)
        self.assertEqual(20, sleep.call_count)

    def test_owner_display_cannot_be_bootstrapped(self):
        controller = agent_loop.AndroidController("unused")
        controller.state = lambda: {"displayId": 0}
        controller.execute = Mock()
        with self.assertRaises(agent_loop.AgentError):
            controller.prepare_workspace("https://example.com", allow_initialize=True)
        controller.execute.assert_not_called()


class SMSReplanTest(unittest.TestCase):
    def exercise(self, actions, outcomes):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        args = agent_loop.build_parser().parse_args([
            "--goal", "给183492发短信，内容是：晚会儿到。", "--provider", "mock",
            "--dry-run", "--runs-dir", directory.name, "--max-steps", "6"])
        controller = Mock()
        controller.require_display.return_value = 7
        controller.prepare_workspace.return_value = False
        controller.capture.side_effect = lambda p: agent_loop.Observation(p, "private-frame", 7)
        controller.execute.side_effect = [None, *outcomes]
        planner = Mock()
        planner.next_action.side_effect = actions
        with patch.object(agent_loop, "locate_adb", return_value="unused"), \
             patch.object(agent_loop, "AndroidController", return_value=controller), \
             patch.object(agent_loop, "MockPlanner", return_value=planner), \
             patch.object(agent_session, "log_event"), patch.object(agent_loop.time, "sleep"):
            return agent_loop.run(args), planner, controller

    def test_unknown_control_replans_then_can_take_safe_back_route(self):
        result, planner, controller = self.exercise([
            {"action": "tap", "arguments": {"x": 661, "y": 188}},
            {"action": "key", "arguments": {"keycode": 4}},
            {"action": "request_user", "arguments": {"message": "请核对号码"}},
        ], [agent_loop.SMSControlBlocked("rejected without click"), None])
        self.assertEqual(2, result)
        history = planner.next_action.call_args_list[-1].args[2]
        self.assertEqual("blocked_unknown_control", history[0]["result"])
        self.assertIs(False, history[0]["executed"])
        self.assertEqual(3, controller.execute.call_count)

    def test_send_or_focus_failures_never_replan(self):
        with self.assertRaisesRegex(agent_loop.AgentError, "send control"):
            self.exercise([{"action": "tap", "arguments": {"x": 650, "y": 1200}}],
                          [agent_loop.AgentError("send control")])

    def test_different_unknown_controls_still_have_two_replan_limit(self):
        with self.assertRaisesRegex(agent_loop.AgentError, "replan limit"):
            self.exercise([{"action": "tap", "arguments": {"x": x, "y": 188}} for x in (660, 661, 662)],
                          [agent_loop.SMSControlBlocked("rejected")] * 3)


if __name__ == "__main__":
    unittest.main()

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "agent_loop.py"
SPEC = importlib.util.spec_from_file_location("agent_loop", MODULE_PATH)
agent_loop = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = agent_loop
SPEC.loader.exec_module(agent_loop)


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


if __name__ == "__main__":
    unittest.main()

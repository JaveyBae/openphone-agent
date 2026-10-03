import importlib.util
import sys
import unittest
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from qwen_planner import QwenPlanner, to_json_action, user_text  # noqa: E402


class TeacherDatasetTest(unittest.TestCase):
    def test_json_action_round_trips_through_the_planner_parser(self):
        planner = QwenPlanner("http://x/v1", "m", "", 1, 720, 1280)
        for action in ({"action": "tap", "arguments": {"x": 360, "y": 640}},
                       {"action": "input_text", "arguments": {"text": "Test"}},
                       {"action": "launch_app", "arguments": {"package": "com.google.android.contacts"}},
                       {"action": "key", "arguments": {"keycode": 4}}):
            native = to_json_action(action, 720, 1280)
            parsed = planner.parse("why\n" + __import__("json").dumps(native), {})
            self.assertEqual(parsed["action"], action["action"])
            self.assertEqual(parsed["arguments"], action["arguments"])

    def test_tap_with_a_point_list_and_malformed_values(self):
        planner = QwenPlanner("http://x/v1", "m", "", 1, 720, 1280)
        action = planner.parse('tap the date\n{"action": "tap", "x": [500, 500]}', {})
        self.assertEqual({"x": 360, "y": 640}, action["arguments"])
        with self.assertRaises(ValueError):
            planner.parse('{"action": "tap", "x": [1, 2, 3]}', {})
        with self.assertRaises(ValueError):
            planner.parse('{"action": "tap", "x": {"a": 1}, "y": 2}', {})

    def test_user_text_reports_focus(self):
        history = [{"action": {"action": "tap", "arguments": {"x": 360, "y": 640}}, "private_input_focused": True}]
        text = user_text("goal", history, 720, 1280)
        self.assertIn("1. tap at (500,500)", text)
        self.assertIn("Do NOT tap it again", text)
        named = user_text("goal", [{**history[0], "focused_field": "subject"}], 720, 1280)
        self.assertIn("focused the 'subject' text field", named)
        self.assertIn("(none)", user_text("goal", [], 720, 1280))


if __name__ == "__main__":
    unittest.main()

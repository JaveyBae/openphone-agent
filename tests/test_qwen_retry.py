import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from qwen_planner import QwenPlanner  # noqa: E402


class CorrectionTurnTest(unittest.TestCase):
    def test_an_element_number_without_a_list_gets_one_correction_turn(self):
        planner = QwenPlanner("http://x/v1", "m", "", 5, 720, 1280)
        replies = ['Tap the suggestion.\n{"action": "tap", "element": 1}',
                   'Tap the suggestion.\n{"action": "tap", "x": 350, "y": 400}']
        observation = SimpleNamespace(path=Path(__file__), nodes=None)
        with patch.object(QwenPlanner, "ask", side_effect=replies) as ask, \
                patch("qwen_planner.encode", return_value="data:"):
            action = planner.next_action("写邮件", observation, [], True)
        self.assertEqual({"x": 252, "y": 512}, action["arguments"])
        self.assertEqual(2, ask.call_count)
        self.assertIn("cannot be executed", ask.call_args.args[0][-1]["content"])

    def test_a_second_malformed_reply_still_stops(self):
        planner = QwenPlanner("http://x/v1", "m", "", 5, 720, 1280)
        observation = SimpleNamespace(path=Path(__file__), nodes=None)
        with patch.object(QwenPlanner, "ask", return_value='{"action": "tap", "element": 1}'), \
                patch("qwen_planner.encode", return_value="data:"), self.assertRaises(ValueError):
            planner.next_action("写邮件", observation, [], True)

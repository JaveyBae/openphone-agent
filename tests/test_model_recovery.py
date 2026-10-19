import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from autoglm_adapter import alternating_taps, parse_native


def tap(x, y):
    return {"action": {"action": "tap", "arguments": {"x": x, "y": y}}}


class ModelRecoveryTest(unittest.TestCase):
    def test_back_and_forth_between_two_taps_is_noticed(self):
        self.assertTrue(alternating_taps([tap(844, 925), tap(499, 823), tap(846, 925), tap(499, 820)]))
        self.assertFalse(alternating_taps([tap(1, 1)] * 4))  # the same tap repeated is a different check
        self.assertFalse(alternating_taps([tap(844, 925), tap(499, 823), tap(100, 100), tap(499, 823)]))

    def test_travel_apps_open_the_flight_search_page_and_unknown_apps_ask_to_replan(self):
        action = parse_native('do(action="Launch", app="携程")', 1080, 2400)
        self.assertEqual("open_url", action["action"])
        self.assertTrue(action["arguments"]["url"].startswith("https://www.google.com/travel/flights"))
        action = parse_native('do(action="Launch", app="微信")', 1080, 2400)
        self.assertEqual(("request_user", "微信"), (action["action"], action["arguments"]["unavailable_app"]))
        self.assertEqual("com.google.android.deskclock",
                         parse_native('do(action="Launch", app="时钟")', 1080, 2400)["arguments"]["package"])


if __name__ == "__main__":
    unittest.main()

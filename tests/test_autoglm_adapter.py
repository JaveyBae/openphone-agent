import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from autoglm_adapter import parse_native


class NativeActionTest(unittest.TestCase):
    def test_coordinates_are_normalized_and_clamped(self):
        action = parse_native('do(action="Tap", element=[500,1000])', 720, 1280)
        self.assertEqual(action["arguments"], {"x": 360, "y": 1279})

    def test_accepts_answer_tag_and_reason(self):
        action = parse_native('<think>visible link</think><answer>do(action="Tap", element=[285,763])</answer>', 720, 1280)
        self.assertEqual(action["arguments"], {"x": 205, "y": 976})
        self.assertIn("visible link", action["reason"])

    def test_swipe(self):
        action = parse_native('do(action="Swipe", start=[500,900], end=[500,300])', 720, 1280)
        self.assertEqual(action["arguments"]["y1"], 1152)

    def test_launch_allowlist_and_browser_isolation(self):
        self.assertEqual(parse_native('do(action="Launch", app="淘宝")', 720, 1280)["arguments"]["package"], "com.taobao.taobao")
        self.assertEqual(parse_native('do(action="Launch", app="Chrome")', 720, 1280)["action"], "open_url")
        self.assertEqual(parse_native('do(action="Launch", app="Unknown")', 720, 1280)["action"], "request_user")

    def test_human_and_unicode_input_pause(self):
        self.assertEqual(parse_native('do(action="Take_over", message="login required")', 720, 1280)["action"], "request_user")
        self.assertEqual(parse_native('do(action="Type", text="手机")', 720, 1280)["action"], "request_user")

    def test_finish(self):
        action = parse_native('finish(message="Observed result")', 720, 1280)
        self.assertEqual(action["arguments"]["summary"], "Observed result")

    def test_multiline_finish(self):
        action = parse_native('Screen summary\nfinish(\nmessage="Observed result"\n)', 720, 1280)
        self.assertEqual(action["action"], "finish")

    def test_literal_newlines_in_finish_message(self):
        action = parse_native('finish(message="中文总结\n\nCommons\nWikibooks")', 720, 1280)
        self.assertEqual(action["arguments"]["summary"], "中文总结\n\nCommons\nWikibooks")

    def test_rejects_code_multiple_calls_and_bad_coordinates(self):
        for raw in [
            'do(action="Type", text=__import__("os").getcwd())',
            '__import__("os").system("echo unsafe")',
            'do(action="Tap", element=[1001,0])',
            'do(action="Tap", element=[True,3])',
            'do(action="Tap", element=[1.2,3])',
            'do(action="Tap", element=[1,2])\nfinish(message="done")',
            '<answer>do(action="Tap", element=[1,2]);print("x")</answer>',
        ]:
            with self.subTest(raw=raw), self.assertRaises((ValueError, SyntaxError)):
                parse_native(raw, 720, 1280)


if __name__ == "__main__":
    unittest.main()

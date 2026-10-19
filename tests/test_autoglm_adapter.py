import sys
import unittest
import io
import json
import tempfile
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from autoglm_adapter import PROMPT, AutoGLMPlanner, parse_native


class NativeActionTest(unittest.TestCase):
    def test_regression_labeled_jd_launch_from_failed_response(self):
        raw = '\n\nLaunch: do(action="Launch", app="京东")\n'
        action = parse_native(raw, 720, 1280)
        self.assertEqual("launch_app", action["action"])
        self.assertEqual("com.jingdong.app.mall", action["arguments"]["package"])
        self.assertEqual('do(action="Launch", app="京东")', action["native_action"])

    def test_known_label_inside_answer_tags(self):
        action = parse_native('<think>open JD</think><answer>Launch: do(action="Launch", app="JD")</answer>', 720, 1280)
        self.assertEqual("launch_app", action["action"])
        self.assertIn("open JD", action["reason"])

    def test_supported_labels_keep_original_action_semantics(self):
        cases = [
            ('Tap: do(action="Tap", element=[500,500])', "tap"),
            ('Type: do(action="Type", text="iPhone")', "input_text"),
            ('Swipe: do(action="Swipe", start=[500,900], end=[500,300])', "swipe"),
            ('Back: do(action="Back")', "key"),
            ('Wait: do(action="Wait", seconds=1)', "wait"),
            ('Finish: finish(message="done")', "finish"),
            ('Human assistance: do(action="Take_over", message="login")', "request_user"),
            ('Take_over: do(action="Take_over", message="login")', "request_user"),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(expected, parse_native(raw, 720, 1280)["action"])

    def test_labels_do_not_allow_ambiguous_or_executable_content(self):
        for raw in [
            'Unknown: do(action="Launch", app="京东")',
            'Launch: do(action="Tap", element=[500,500])',
            'Launch: finish(message="done")',
            'Finish: do(action="Launch", app="JD")',
            'Launch: Launch: do(action="Launch", app="JD")',
            'Launch: do(action="Launch", app="JD")\nTap: do(action="Tap", element=[500,500])',
            '<answer>Launch: do(action="Launch", app="JD"); finish(message="done")</answer>',
            '<answer>Launch: do(action="Launch", app="JD")</answer><answer>finish(message="done")</answer>',
            'Type: do(action="Type", text=__import__("os").getcwd())',
            'Tap: do(action="Tap", element=[-1,500])',
            'Launch: do(action="Launch", action="Tap", app="JD")',
            'Launch: do(**{"action":"Launch", "app":"JD"})',
            'Launch: do("Launch", app="JD")',
        ]:
            with self.subTest(raw=raw), self.assertRaises((ValueError, SyntaxError)):
                parse_native(raw, 720, 1280)


    def test_prompt_uses_bare_calls_not_labeled_examples(self):
        self.assertIn("write ONLY a single do(...) or finish(...) call", PROMPT)
        self.assertNotIn('Launch: do(', PROMPT)

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

    def test_sms_aliases_are_messages_not_wechat(self):
        for name in ("Messages", "Message", "Google Messages", "SMS", "短信", "信息"):
            with self.subTest(name=name):
                action = parse_native(f'do(action="Launch", app="{name}")', 720, 1280)
                self.assertEqual("launch_app", action["action"])
                self.assertEqual("com.google.android.apps.messaging", action["arguments"]["package"])
        self.assertEqual("request_user", parse_native('do(action="Launch", app="微信")', 720, 1280)["action"])
        self.assertIn("NOT WeChat", PROMPT)

    def test_human_takeover_and_unicode_input(self):
        self.assertEqual(parse_native('do(action="Take_over", message="login required")', 720, 1280)["action"], "request_user")
        action = parse_native('do(action="Type", text="5090显卡")', 720, 1280)
        self.assertEqual(action["action"], "input_text")
        self.assertEqual(action["arguments"]["text"], "5090显卡")

    def test_unicode_control_characters_rejected(self):
        for text in ("\n", "手机\n", "\x00", "\x7f", "\ud800"):
            with self.subTest(text=repr(text)), self.assertRaises(ValueError):
                parse_native('do(action="Type", text=' + repr(text) + ')', 720, 1280)

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
            '<answer>do(action="Tap", element=[1,2]);print("x")</answer>',
            'do(action="Tap", element=[314,886])\n\ndo(action="Tap", element=[315,886])',
            'Tap: do(action="Tap", element=[1,2])\ndo(action="Tap", element=[1,2])',
            'finish(message="done")\nfinish(message="done")',
        ]:
            with self.subTest(raw=raw), self.assertRaises((ValueError, SyntaxError)):
                parse_native(raw, 720, 1280)

    def test_identical_repeated_ui_call_runs_once(self):
        raw = 'Tap the 30 minutes option.\n\ndo(action="Tap", element=[314,886])\n\n\ndo(action="Tap", element=[314,886])\n'
        action = parse_native(raw, 720, 1280)
        self.assertEqual("tap", action["action"])
        self.assertEqual('do(action="Tap", element=[314,886])', action["native_action"])


class CompletionFormattingTest(unittest.TestCase):
    def test_malformed_sms_finish_pauses_without_success_claim_or_replay(self):
        from app_targets import sms_draft_goal
        with tempfile.TemporaryDirectory() as directory:
            frame = Path(directory) / "private.png"
            frame.write_bytes(b"private-test-fixture")
            planner = AutoGLMPlanner("http://localhost/v1", "test", "", 1, 720, 1280)
            bad = 'finish(message="SMS已发送，内容"晚会儿到。"。")'
            response = io.StringIO(json.dumps({"choices": [{"message": {"content": bad}}]}))
            with patch("autoglm_adapter.urllib.request.urlopen", return_value=response) as api:
                action = planner.next_action(sms_draft_goal("给183492发短信，内容是：晚会儿到。"),
                                             SimpleNamespace(path=frame, display_id=7), [], True)
            self.assertEqual(1, api.call_count)
            self.assertEqual("request_user", action['action'])
            self.assertIn("未自动发送", action['arguments']['message'])
            self.assertNotIn("已发送", action['arguments']['message'])
            self.assertEqual("sms_unparsed_completion_handoff", action['policy_override'])

    def test_malformed_sms_ui_action_is_never_converted_to_completion_pause(self):
        with tempfile.TemporaryDirectory() as directory:
            frame = Path(directory) / "private.png"
            frame.write_bytes(b"private-test-fixture")
            planner = AutoGLMPlanner("http://localhost/v1", "test", "", 1, 720, 1280)
            bad = 'do(action="Type", text="内容"晚会儿到。"")'
            response = io.StringIO(json.dumps({"choices": [{"message": {"content": bad}}]}))
            with patch("autoglm_adapter.urllib.request.urlopen", return_value=response) as api:
                with self.assertRaises((ValueError, SyntaxError)):
                    planner.next_action("短信草稿", SimpleNamespace(path=frame, display_id=7), [], True)
            self.assertEqual(1, api.call_count)

    def test_verified_focus_feedback_does_not_wait_for_private_keyboard(self):
        with tempfile.TemporaryDirectory() as directory:
            frame = Path(directory) / "private.png"
            frame.write_bytes(b"private-test-fixture")
            planner = AutoGLMPlanner("http://localhost/v1", "test", "", 1, 720, 1280)
            history = [{"action": {"action": "tap", "arguments": {"x": 359, "y": 1209}},
                        "private_input_focused": True}]
            response = io.StringIO(json.dumps({"choices": [{"message": {
                "content": 'do(action="Type", text="晚会儿到")'}}]}))
            with patch("autoglm_adapter.urllib.request.urlopen", return_value=response) as api:
                action = planner.next_action("短信草稿", SimpleNamespace(path=frame, display_id=7), history, True)
            content = json.loads(api.call_args.args[0].data)["messages"][-1]["content"][0]["text"]
            self.assertIn("VERIFIED FOCUSED", content)
            self.assertIn("No on-screen keyboard", content)
            self.assertIn("Never tap Send", content)
            self.assertEqual({"text": "晚会儿到"}, action["arguments"])

    def test_guard_feedback_is_sent_without_replaying_failed_click(self):
        with tempfile.TemporaryDirectory() as directory:
            frame = Path(directory) / "private.png"
            frame.write_bytes(b"private-test-fixture")
            planner = AutoGLMPlanner("http://localhost/v1", "test", "", 1, 720, 1280)
            history = [{"action": {"action": "tap", "arguments": {"x": 661, "y": 188}},
                        "result": "blocked_unknown_control", "executed": False}]
            response = io.StringIO(json.dumps({"choices": [{"message": {
                "content": 'do(action="Back")'}}]}))
            with patch("autoglm_adapter.urllib.request.urlopen", return_value=response) as api:
                action = planner.next_action("短信草稿", SimpleNamespace(path=frame, display_id=7), history, True)
            content = json.loads(api.call_args.args[0].data)["messages"][-1]["content"][0]["text"]
            self.assertIn("NO click occurred", content)
            self.assertIn("Do not retry", content)
            self.assertIn("preserving its draft", content)
            self.assertEqual({"keycode": 4}, action["arguments"])

    def run_planner(self, outputs, expected_error=None):
        with tempfile.TemporaryDirectory() as directory:
            frame = Path(directory) / "private.png"
            frame.write_bytes(b"private-test-fixture")
            observation = SimpleNamespace(path=frame, display_id=7)
            planner = AutoGLMPlanner("http://localhost/v1", "test", "", 1, 720, 1280)
            responses = [io.StringIO(json.dumps({"choices": [{"message": {"content": output}}]}))
                         for output in outputs]
            with patch("autoglm_adapter.urllib.request.urlopen", side_effect=responses) as api:
                if expected_error:
                    with self.assertRaises(expected_error):
                        planner.next_action("验证中文输入", observation, [], True)
                    action = None
                else:
                    action = planner.next_action("验证中文输入", observation, [], True)
                if api.call_count > 1:
                    repair = json.loads(api.call_args.args[0].data)
                    self.assertEqual(["assistant", "user"], [m['role'] for m in repair['messages'][-2:]])
                    self.assertIn("SINGLE QUOTES", repair['messages'][-1]['content'])
                return action, api.call_count, json.loads(frame.with_suffix(".model.json").read_text())

    def test_duplicate_completion_is_reformatted_not_deduplicated(self):
        bad = 'finish(message="完成")\nfinish(message="完成")'
        action, calls, record = self.run_planner([bad, "finish(message='中文输入验证成功')"])
        self.assertEqual("finish", action["action"])
        self.assertEqual(2, calls)
        self.assertEqual(bad, record["raw_output"])
        self.assertIn("completion_format_retry", record)

    def test_unescaped_completion_quotes_are_read_without_a_retry(self):
        action, calls, record = self.run_planner(['finish(message="已输入"显卡"。")'])
        self.assertEqual("finish", action["action"])
        self.assertEqual('已输入"显卡"。', action["arguments"]["summary"])
        self.assertEqual(1, calls)
        self.assertEqual("lenient_completion_message", record["policy_override"])

    def test_real_calendar_completion_with_quoted_title(self):
        raw = ('I should finish the task now.\n\nfinish(message="任务完成！已添加团队站会。\n\n'
               '日历显示"团队站会 10:00 - 10:30 AM"。底部提示"Saved \'团队站会\' on Fri, Oct 16"。")')
        action, calls, _ = self.run_planner([raw])
        self.assertEqual(1, calls)
        self.assertTrue(action["arguments"]["summary"].startswith("任务完成！已添加团队站会。"))
        self.assertIn('"团队站会 10:00 - 10:30 AM"', action["arguments"]["summary"])

    def test_completion_retry_cannot_introduce_tap(self):
        action, calls, _ = self.run_planner(['finish(message="完成")\nfinish(message="完成")',
                                           'do(action="Tap", element=[1,1])'])
        self.assertEqual(2, calls)
        self.assertEqual("request_user", action["action"])  # the Tap is never returned

    def test_premature_finish_after_an_action_is_dropped(self):
        # "do(...) finish('done')": only the action runs; success is judged on the next screen.
        action, calls, record = self.run_planner(['do(action="Type", text="显卡")\nfinish(message="完成")'])
        self.assertEqual(1, calls)
        self.assertEqual(("input_text", "显卡"), (action["action"], action["arguments"]["text"]))
        self.assertNotIn("completion_format_retry", record)

    def test_completion_retry_is_bounded_to_one(self):
        bad = 'finish(message="完成")\nfinish(message="完成")'
        action, calls, record = self.run_planner([bad, bad])
        self.assertEqual(2, calls)
        # Earlier steps may have taken effect: pause for inspection, never claim success.
        self.assertEqual("request_user", action["action"])
        self.assertIn("不要直接重复提交", action["arguments"]["message"])
        self.assertEqual("unparsed_completion_handoff", record["policy_override"])

class ElementBoxTest(unittest.TestCase):
    def test_tap_on_a_box_taps_its_centre(self):
        action = parse_native('do(action="Tap", element=[678,928,824,972])', 720, 1280)
        self.assertEqual({"x": 751 * 720 // 1000, "y": 950 * 1280 // 1000}, action["arguments"])

    def test_malformed_boxes_and_box_swipes_are_rejected(self):
        for raw in ('do(action="Tap", element=[824,928,678,972])',
                    'do(action="Tap", element=[678,928,678,972])',
                    'do(action="Tap", element=[678,928,824,1972])',
                    'do(action="Tap", element=[678,928,824])',
                    'do(action="Swipe", start=[1,2,3,4], end=[5,6])'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_native(raw, 720, 1280)


class TrailingFragmentTest(unittest.TestCase):
    def test_first_complete_call_runs_and_broken_tail_is_ignored(self):
        raw = ('Now type the title.\n\ndo(action="Type", text="产品周会")\n\n[Screen analysis]\nThe title field '
               'is active.\n\n\nType", text="产品周会")')
        action = parse_native(raw, 720, 1280)
        self.assertEqual({"text": "产品周会"}, action["arguments"])
        self.assertEqual('do(action="Type", text="产品周会")', action["native_action"])

    def test_text_containing_a_parenthesis_is_kept_whole(self):
        action = parse_native('do(action="Type", text="会议(线上)")', 720, 1280)
        self.assertEqual({"text": "会议(线上)"}, action["arguments"])


class MultiActionRetryTest(unittest.TestCase):
    PLAN = 'Title first.\ndo(action="Tap", element=[499,156])\ndo(action="Type", text="项目复盘")'

    def run_planner(self, outputs, goal="帮我建一个10月20日的会议，叫项目复盘"):
        with tempfile.TemporaryDirectory() as directory:
            frame = Path(directory) / "private.png"
            frame.write_bytes(b"private-test-fixture")
            planner = AutoGLMPlanner("http://localhost/v1", "test", "", 1, 720, 1280)
            responses = [io.StringIO(json.dumps({"choices": [{"message": {"content": output}}]}))
                         for output in outputs]
            with patch("autoglm_adapter.urllib.request.urlopen", side_effect=responses) as api:
                try:
                    action = planner.next_action(goal, SimpleNamespace(path=frame, display_id=7), [], True)
                except ValueError as error:
                    action = error
                retry = json.loads(api.call_args.args[0].data) if api.call_count > 1 else None
                return action, api.call_count, retry, json.loads(frame.with_suffix(".model.json").read_text())

    def test_plan_of_several_steps_runs_only_its_first_step(self):
        # The next screenshot re-decides the rest; no extra model call is needed.
        action, calls, _, record = self.run_planner([self.PLAN])
        self.assertEqual(1, calls)
        self.assertEqual(("tap", {"x": 359, "y": 199}), (action["action"], action["arguments"]))
        self.assertEqual("first_step_of_plan", record["policy_override"])
        self.assertEqual(self.PLAN, record["raw_output"])

    def test_sms_and_mixed_outputs_are_not_reasked(self):
        for goal, output in (("给183492发短信，内容是：晚会儿到。", self.PLAN),
                             ("帮我建一个会议", 'finish(message="done")\ndo(action="Tap", element=[1,2])')):
            with self.subTest(goal=goal):
                action, calls, _, _ = self.run_planner([output], goal=goal)
                self.assertEqual(1, calls)
                self.assertIsInstance(action, ValueError)

if __name__ == "__main__":
    unittest.main()

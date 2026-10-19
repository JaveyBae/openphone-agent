import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import agent_loop as agent
import workspace_runtime as runtime

SESSION = "3a54f8aa-a9dc-47a5-bf64-93f5b9645f03"
STATE = {"backend": "adb-shell", "displayId": 5, "session": SESSION,
         "displayName": "OpenPhone Isolated Workspace " + SESSION}
DISPLAYS = f'Display id 5: DisplayInfo{{"{STATE["displayName"]}", owner com.android.shell (uid 2000), removeMode 1}}'
DEVICES = f'DisplayDeviceInfo{{"{STATE["displayName"]}": owner com.android.shell (uid 2000), ' + ", ".join(agent.ISOLATION_FLAGS) + "}"
FOCUS = """  FocusedDisplayId: 0
  FocusedApplications:
    displayId=9, name='not a window'
  FocusedWindows:
    displayId=5, name='private/browser'
    displayId=0, name='owner/chat'
  FocusRequests:
    displayId=7, name='not a focused window' result='NO_WINDOW'
"""


class IsolationTest(unittest.TestCase):
    def test_live_flags_and_shell_identity_required(self):
        self.assertTrue(agent.workspace_is_isolated(STATE, DISPLAYS, DEVICES))
        for flag in agent.ISOLATION_FLAGS:
            self.assertFalse(agent.workspace_is_isolated(STATE, DISPLAYS, DEVICES.replace(flag, "")))
        self.assertFalse(agent.workspace_is_isolated(STATE, DISPLAYS.replace("2000", "10001"), DEVICES))
        self.assertFalse(agent.workspace_is_isolated(STATE, DISPLAYS.replace("id 5:", "id 0:"), DEVICES))
        self.assertFalse(agent.workspace_is_isolated(STATE, DISPLAYS.replace("removeMode 1", "removeMode 0"), DEVICES))
        self.assertFalse(agent.workspace_is_isolated(STATE, DISPLAYS, DEVICES.replace(SESSION, "stale")))

    def test_malformed_and_owner_state_never_used(self):
        for changes in ({"displayId": 0}, {"displayId": True}, {"displayId": "5"},
                        {"session": "../../owner"}, {"displayName": None}):
            with self.subTest(changes=changes):
                state = {**STATE, **changes}
                self.assertFalse(agent.workspace_is_isolated(state, DISPLAYS, DEVICES))
                with self.assertRaises(agent.AgentError):
                    agent.private_frame_command(state)

    def test_frame_path_is_session_scoped_never_owner(self):
        self.assertEqual(("exec-out", "cat", agent.WORKSPACE_DIRECTORY + "/frame-" + SESSION + ".png"),
                         agent.private_frame_command(STATE))

    def test_focus_parser_ignores_apps_and_requests(self):
        self.assertEqual((0, {0, 5}), agent.input_focus(FOCUS))
        self.assertEqual((None, set()), agent.input_focus("unknown format"))

    def controller(self):
        controller = agent.AndroidController("unused")
        controller.require_display = Mock(return_value=5)
        controller.require_isolation = Mock()
        controller._run = Mock(return_value=FOCUS)
        return controller

    def test_ascii_input_keeps_owner_global_focus(self):
        controller = self.controller()
        controller.execute({"action": "input_text", "arguments": {"text": "5090"}})
        controller._run.assert_any_call("shell", "input", "-d", "5", "text", "5090")
        self.assertEqual(0, controller.last_focus_evidence["after"]["top_display"])

    def test_legacy_display_rejected_before_any_input(self):
        controller = self.controller()
        controller.require_isolation.side_effect = agent.AgentError("focus isolation")
        with self.assertRaises(agent.AgentError):
            controller.execute({"action": "tap", "arguments": {"x": 1, "y": 2}})
        controller._run.assert_not_called()

    def test_keyboard_without_private_window_fails_closed(self):
        controller = self.controller()
        controller._run.return_value = FOCUS.replace("    displayId=5, name='private/browser'\n", "")
        with self.assertRaisesRegex(agent.AgentError, "no focused window"):
            controller.execute({"action": "input_text", "arguments": {"text": "5090"}})
        self.assertEqual(1, controller._run.call_count)

    def test_touch_without_private_window_fails_closed(self):
        for action in ({"action": "tap", "arguments": {"x": 1, "y": 2}},
                       {"action": "swipe", "arguments": {"x1": 1, "y1": 2, "x2": 3, "y2": 4}}):
            controller = self.controller()
            controller._run.return_value = FOCUS.replace("    displayId=5, name='private/browser'\n", "")
            with self.assertRaisesRegex(agent.AgentError, "no focused window"):
                controller.execute(action)
            self.assertEqual(1, controller._run.call_count)

    def test_sms_task_never_falls_back_when_messages_leaves_private_display(self):
        controller = self.controller()
        controller.sms_task = True
        controller._run.side_effect = [FOCUS, "Display #0 (activities from top to bottom):\n"]
        with self.assertRaisesRegex(agent.AgentError, "Messages is no longer resumed"):
            controller.execute({"action": "tap", "arguments": {"x": 1, "y": 2}})
        self.assertFalse(any("tap" in call.args for call in controller._run.call_args_list))

    def test_foreign_global_focus_stops_without_restoring_owner(self):
        controller = self.controller()
        controller._run.return_value = FOCUS.replace("FocusedDisplayId: 0", "FocusedDisplayId: 5")
        with self.assertRaisesRegex(agent.AgentError, "owner input focus"):
            controller.execute({"action": "tap", "arguments": {"x": 1, "y": 2}})
        self.assertEqual(1, controller._run.call_count)

    def test_focus_change_after_action_stops_no_automatic_switch(self):
        controller = self.controller()
        controller._run.side_effect = [FOCUS, "", "", FOCUS.replace("FocusedDisplayId: 0", "FocusedDisplayId: 5")]
        with self.assertRaisesRegex(agent.AgentError, "owner input focus"):
            controller.execute({"action": "tap", "arguments": {"x": 1, "y": 2}})
        self.assertEqual(4, controller._run.call_count)

    def test_global_keys_are_not_injected(self):
        for action in ({"action": "key", "arguments": {"keycode": 3}},
                       {"action": "key", "arguments": {"keycode": 26}}):
            controller = self.controller()
            with self.assertRaises(agent.AgentError):
                controller.execute(action)
            self.assertEqual(1, controller._run.call_count)

    def unicode_controller(self, overrides=None):
        import json
        controller = self.controller()
        controller.state = Mock(return_value=STATE)
        controller._text_helper_jar = "/data/local/tmp/openphone-workspace/private-text.jar"
        result = {"ok": True, "verified": True, "displayId": 5, "session": SESSION,
                  "package": "com.jingdong.app.mall", "method": "accessibility_set_text", "characters": 6}
        result.update(overrides or {})
        dump = "Display #5 (activities from top to bottom):\n  topResumedActivity=ActivityRecord{abc u0 com.jingdong.app.mall/.Search t63}\n"
        def run(*args):
            if args == ("shell", "dumpsys", "input"):
                return FOCUS
            if args == ("shell", "dumpsys", "activity", "activities"):
                return dump
            if len(args) == 2 and "PrivateTextInput" in args[1]:
                return json.dumps(result)
            raise AssertionError(f"Unexpected command: {args}")
        controller._run.side_effect = run
        return controller

    def test_unicode_uses_scoped_helper_with_verified_readback(self):
        import base64
        controller = self.unicode_controller()
        controller.execute({"action": "input_text", "arguments": {"text": "5090显卡"}})
        commands = controller._run.call_args_list
        helper = next(call.args[1] for call in commands if "PrivateTextInput" in call.args[-1])
        self.assertIn(" 5 " + SESSION + " com.jingdong.app.mall ", helper)
        self.assertTrue(helper.endswith(base64.b64encode("5090显卡".encode()).decode()))
        self.assertFalse(any("input" in call.args and "text" in call.args for call in commands))
        self.assertEqual(0, controller.last_focus_evidence["after"]["top_display"])

    def test_unicode_failure_and_wrong_identity_never_fall_back(self):
        for result in ({"ok": False, "error": "no editable field"}, {"displayId": 0},
                       {"verified": False}, {"session": "stale"}, {"package": "owner.app"},
                       {"characters": 3}, {"method": "clipboard"}):
            with self.subTest(result=result):
                controller = self.unicode_controller(result)
                with self.assertRaisesRegex(agent.AgentError, "Unicode input failed"):
                    controller.execute({"action": "input_text", "arguments": {"text": "5090显卡"}})
                self.assertFalse(any("text" in call.args or "ime" in call.args
                                     for call in controller._run.call_args_list))

    def test_unicode_never_selects_resumed_owner_app(self):
        controller = self.unicode_controller()
        controller._run.side_effect = [FOCUS, "Display #0 (activities from top to bottom):\n  topResumedActivity=ActivityRecord{abc u0 com.jingdong.app.mall/.Search t63}\n"]
        with self.assertRaisesRegex(agent.AgentError, "one supported app"):
            controller.execute({"action": "input_text", "arguments": {"text": "5090显卡"}})
        self.assertEqual(2, controller._run.call_count)

    def test_unicode_rejects_workspace_changes_before_helper(self):
        controller = self.unicode_controller()
        controller.state.side_effect = [STATE, {**STATE, "session": "stale"}]
        with self.assertRaisesRegex(agent.AgentError, "workspace changed"):
            controller.execute({"action": "input_text", "arguments": {"text": "5090显卡"}})
        self.assertFalse(any("PrivateTextInput" in call.args[-1] for call in controller._run.call_args_list))

    def test_text_control_characters_fail_before_mutation(self):
        for text in ("hello\n", "手机\x00", "\ud800", "\x7f"):
            controller = self.controller()
            with self.assertRaisesRegex(agent.AgentError, "control characters"):
                controller.execute({"action": "input_text", "arguments": {"text": text}})
            self.assertEqual(1, controller._run.call_count)

    def test_jd_verification_tap_is_blocked_despite_model_risk_none(self):
        action = {"action": "tap", "arguments": {"x": 359, "y": 702},
                  "reason": "Tap 快速验证 to continue", "risk": "none"}
        self.assertIsNotNone(agent.sensitive_reason(action))

    def test_target_routing_requires_explicit_single_app(self):
        for goal in ("打开京东 找最便宜的5090", "你能启动一下京东吗", "open JD find cheapest 5090"):
            self.assertEqual("com.jingdong.app.mall", agent.explicit_target_package(goal))
        for goal in ("京东和淘宝比价", "打开京东和淘宝", "京东多少钱", "不要打开京东", "open JDK"):
            self.assertIsNone(agent.explicit_target_package(goal))

    def test_sms_requests_route_before_observation(self):
        from app_targets import sms_draft_goal
        for goal in ("发短信，告诉18347294，我今天有事", "打开 Messages", "prepare an SMS draft", "send a text message",
                     "准备短信草稿，不要发送", "Open Messages. Do not send anything."):
            self.assertEqual(agent.SMS_PACKAGE, agent.explicit_target_package(goal))
            self.assertIn("NEVER tap Send", sms_draft_goal(goal))
        for goal in ("微信发消息", "发消息给小张", "不要打开短信", "在微信发短信"):
            self.assertIsNone(agent.explicit_target_package(goal))
            self.assertEqual(goal, sms_draft_goal(goal))
        self.assertIn(agent.SMS_PACKAGE, agent.TEXT_PACKAGES)
        self.assertIn("org.fossify.calendar", agent.TEXT_PACKAGES)  # offline calendar titles

    def test_sms_draft_finish_is_manual_handoff_not_sent_success(self):
        from app_targets import sms_completion_handoff
        action = {"action": "finish", "arguments": {"summary": "SMS delivered"}}
        result = sms_completion_handoff("发短信说你好", action)
        self.assertEqual("request_user", result["action"])
        self.assertIn("未自动发送", result["arguments"]["message"])
        self.assertNotIn("delivered", result["arguments"]["message"])
        self.assertIs(action, sms_completion_handoff("Open Messages", action))
        self.assertIs(action, sms_completion_handoff("打开京东", action))

    def test_sms_enter_and_space_are_never_injected(self):
        dump = f"Display #5 (activities from top to bottom):\n  topResumedActivity=ActivityRecord{{abc u0 {agent.SMS_PACKAGE}/.Compose t72}}\n"
        for keycode in (62, 66):
            controller = self.controller()
            controller._run.side_effect = [FOCUS, dump]
            with self.assertRaisesRegex(agent.AgentError, "SMS safety guard"):
                controller.execute({"action": "key", "arguments": {"keycode": keycode}})
            self.assertFalse(any("keyevent" in call.args for call in controller._run.call_args_list))

    def test_sms_click_guard_failure_never_falls_back_to_coordinates(self):
        import json
        controller = self.controller()
        controller.state = Mock(return_value=STATE)
        controller._text_helper_jar = "/data/local/tmp/openphone-workspace/private-text.jar"
        dump = f"Display #5 (activities from top to bottom):\n  topResumedActivity=ActivityRecord{{abc u0 {agent.SMS_PACKAGE}/.Compose t72}}\n"
        controller._run.side_effect = [FOCUS, dump, FOCUS, json.dumps({"ok": False, "error": "send control"})]
        with self.assertRaisesRegex(agent.AgentError, "SMS safety guard"):
            controller.execute({"action": "tap", "arguments": {"x": 600, "y": 1100}})
        helper = controller._run.call_args_list[-1].args[1]
        self.assertIn("sms-tap 600 1100", helper)
        self.assertFalse(any("tap" in call.args for call in controller._run.call_args_list))

    def test_sms_click_verification_is_private_and_session_scoped(self):
        import json
        dump = f"Display #5 (activities from top to bottom):\n  topResumedActivity=ActivityRecord{{abc u0 {agent.SMS_PACKAGE}/.Compose t72}}\n"
        for override in ({}, {"displayId": 0}, {"session": "stale"}, {"method": "raw_tap"}):
            controller = self.controller()
            controller.state = Mock(return_value=STATE)
            controller._text_helper_jar = "/data/local/tmp/openphone-workspace/private-text.jar"
            result = {"ok": True, "verified": True, "displayId": 5, "session": SESSION,
                      "package": agent.SMS_PACKAGE, "method": "guarded_sms_click",
                      "privateEditableFocused": True, **override}
            controller._run.side_effect = [FOCUS, dump, FOCUS, json.dumps(result), FOCUS, FOCUS]
            if override:
                with self.assertRaisesRegex(agent.AgentError, "SMS safety guard"):
                    controller.execute({"action": "tap", "arguments": {"x": 359, "y": 176}})
            else:
                controller.execute({"action": "tap", "arguments": {"x": 359, "y": 176}})
                self.assertEqual(0, controller.last_focus_evidence["after"]["top_display"])
                self.assertTrue(controller.last_private_input_focused)

    def test_only_verified_unexecuted_unknown_control_can_replan(self):
        import json
        dump = f"Display #5 (activities from top to bottom):\n  topResumedActivity=ActivityRecord{{abc u0 {agent.SMS_PACKAGE}/.Compose t72}}\n"
        for override in ({}, {"displayId": 0}, {"session": "stale"}, {"executed": True},
                         {"verified": False}, {"blockedControl": "send_control"},
                         {"blockedControl": "no_target"}):
            controller = self.controller()
            controller.state = Mock(return_value=STATE)
            controller._text_helper_jar = "/private.jar"
            result = {"ok": False, "verified": True, "executed": False, "displayId": 5,
                      "session": SESSION, "package": agent.SMS_PACKAGE,
                      "method": "sms_control_check", "blockedControl": "unknown_control", **override}
            controller._run.side_effect = [FOCUS, json.dumps(result), FOCUS, dump]
            try:
                controller.guarded_sms_tap(661, 188, 5)
                self.fail("Rejected control must never execute")
            except agent.AgentError as error:
                self.assertEqual(not override, isinstance(error, agent.SMSControlBlocked))
            self.assertFalse(any("tap" in call.args for call in controller._run.call_args_list))

    def test_runtime_rechecks_actual_live_flags(self):
        with patch.object(runtime, "read_state", return_value=STATE), patch.object(runtime, "adb_command") as adb:
            adb.side_effect = [Mock(stdout=DISPLAYS), Mock(stdout=DEVICES)]
            self.assertEqual(STATE, runtime.live_state())
            adb.side_effect = [Mock(stdout=DISPLAYS), Mock(stdout=DEVICES.replace("FLAG_OWN_FOCUS", ""))]
            self.assertIsNone(runtime.live_state())

    def test_workspace_process_is_detached_on_device(self):
        command = runtime.device_launch_command(runtime.DEVICE_DIRECTORY + "/workspace-server.jar")
        self.assertIn("nohup app_process / ai.openphone.workspace.WorkspaceServer", command)
        self.assertTrue(command.endswith("2>&1 </dev/null &"))

    def test_install_text_helper_does_not_restart_display(self):
        import hashlib, tempfile
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "workspace-server.jar"
            target.write_bytes(b"helper-jar")
            same = hashlib.sha256(b"helper-jar").hexdigest()
            for remote, pushed in (("0" * 64, True), (same, False), ("", True)):
                with self.subTest(remote=remote), patch.object(runtime, "build_server", return_value=target), \
                        patch.object(runtime, "adb_command") as adb:
                    adb.return_value = Mock(stdout=(remote + "  /data/local/tmp/x.jar\n") if remote else "")
                    installed = runtime.install_text_helper()
                    self.assertEqual(runtime.DEVICE_DIRECTORY + "/private-text.jar", installed)
                    calls = [c.args for c in adb.call_args_list]
                    # Only a checksum read and, if needed, a push: never a display restart.
                    self.assertEqual(("shell", "sha256sum", installed), calls[0])
                    self.assertEqual(pushed, ("push", str(target), installed) in calls)
                    self.assertEqual(2 if pushed else 1, len(calls))

    def test_only_actual_private_tasks_can_be_resumed(self):
        dump = """Display #0 (activities from top to bottom):
  * Hist  #0: ActivityRecord{abc u0 com.jingdong.app.mall/.Main t1}
Display #5 (activities from top to bottom):
  mLastPausedActivity: ActivityRecord{old u0 com.jingdong.app.mall/.Main t99}
  topResumedActivity=ActivityRecord{abc u0 com.jingdong.app.mall/.Verify t63}
  * Hist  #0: ActivityRecord{abc u0 com.jingdong.app.mall/.Verify t63}
ActivityTaskSupervisor state:
  * Hist  #0: ActivityRecord{old u0 com.jingdong.app.mall/.Main t98}
"""
        self.assertEqual([63], agent.private_package_tasks(dump, "com.jingdong.app.mall", 5))
        self.assertEqual([], agent.private_package_tasks(dump, "com.jingdong.app.mall", 0))
        self.assertTrue(agent.package_is_resumed(dump, "com.jingdong.app.mall", 5))
        self.assertFalse(agent.package_is_resumed(dump, "com.jingdong.app", 5))

    def test_existing_private_app_is_resumed_not_restarted(self):
        controller = self.controller()
        controller.require_package_off_owner_display = Mock()
        controller.verify_package_on_agent_display = Mock()
        dump = "Display #5 (activities from top to bottom):\n  * Hist  #0: ActivityRecord{abc u0 com.jingdong.app.mall/.Verify t63}\n"
        controller._run.side_effect = [FOCUS, dump, "", FOCUS]
        controller.execute({"action": "launch_app", "arguments": {"package": "com.jingdong.app.mall"}})
        controller._run.assert_any_call("shell", "am", "task", "focus", "63")
        self.assertFalse(any("start" in call.args for call in controller._run.call_args_list))


if __name__ == "__main__":
    unittest.main()

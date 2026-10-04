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
DISPLAYS = f'Display id 5: DisplayInfo{{"{STATE["displayName"]}", owner com.android.shell (uid 2000)}}'
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

    def test_foreign_global_focus_stops_without_restoring_owner(self):
        controller = self.controller()
        controller._run.return_value = FOCUS.replace("FocusedDisplayId: 0", "FocusedDisplayId: 5")
        with self.assertRaisesRegex(agent.AgentError, "owner input focus"):
            controller.execute({"action": "tap", "arguments": {"x": 1, "y": 2}})
        self.assertEqual(1, controller._run.call_count)

    def test_focus_change_after_action_stops_no_automatic_switch(self):
        controller = self.controller()
        controller._run.side_effect = [FOCUS, "", FOCUS.replace("FocusedDisplayId: 0", "FocusedDisplayId: 5")]
        with self.assertRaisesRegex(agent.AgentError, "owner input focus"):
            controller.execute({"action": "tap", "arguments": {"x": 1, "y": 2}})
        self.assertEqual(3, controller._run.call_count)

    def test_unicode_and_global_keys_are_not_injected(self):
        for action in ({"action": "input_text", "arguments": {"text": "你好"}},
                       {"action": "key", "arguments": {"keycode": 3}},
                       {"action": "key", "arguments": {"keycode": 26}}):
            controller = self.controller()
            with self.assertRaises(agent.AgentError):
                controller.execute(action)
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

    def test_runtime_rechecks_actual_live_flags(self):
        with patch.object(runtime, "read_state", return_value=STATE), patch.object(runtime, "adb_command") as adb:
            adb.side_effect = [Mock(stdout=DISPLAYS), Mock(stdout=DEVICES)]
            self.assertEqual(STATE, runtime.live_state())
            adb.side_effect = [Mock(stdout=DISPLAYS), Mock(stdout=DEVICES.replace("FLAG_OWN_FOCUS", ""))]
            self.assertIsNone(runtime.live_state())

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

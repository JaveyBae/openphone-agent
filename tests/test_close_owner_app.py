import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import chat_bridge
from agent_loop import AgentError
from chat_bridge import Bridge, close_offer

GM = "com.google.android.gm"
BLOCKED = (f"model request/action failed: refusing to launch {GM}: it already has a task on the owner's "
           "display 0 and Android may reuse that task")


CAL = "com.google.android.calendar"


def dump(top_on_main: str, gmail_on_main: bool, calendar_on_main: bool = False) -> str:
    lines = ["Display #0 (activities from top to bottom):"]
    if gmail_on_main:
        lines += [f"  * Task{{abc #181 type=standard A=10171:{GM} U=0 visible=false}}",
                  f"    * Hist  #0: ActivityRecord{{a1 u0 {GM}/.ConversationListActivityGmail t181}}"]
    if calendar_on_main:  # the task's name differs from the package
        lines += ["  * Task{6ad #197 type=standard A=10163:google.android.task.calendar U=0 visible=false}",
                  f"    * Hist  #0: ActivityRecord{{dcb u0 {CAL}/.AllInOneCalendarActivity t197}}"]
    lines.append(f"    topResumedActivity=ActivityRecord{{1 u0 {top_on_main}/.Main t175}}")
    lines += ["Display #2 (activities from top to bottom):",
              "  * Task{p #200 type=standard A=10171:com.google.android.gm U=0 visible=true}",
              f"    * Hist  #0: ActivityRecord{{p1 u0 {GM}/.ConversationListActivityGmail t200}}"]
    return "\n".join(lines)


class CloseOfferTest(unittest.TestCase):
    def test_offer_names_the_app_and_keeps_the_request(self):
        offer = close_offer(BLOCKED, "把「航班行程确认」那封邮件里的行程加到日历")
        self.assertIn("[[action:关闭主屏的Gmail并继续|把「航班行程确认」那封邮件里的行程加到日历||close:" + GM + "]]", offer)

    def test_no_offer_for_other_failures_or_unknown_apps(self):
        self.assertEqual("", close_offer("owner input focus is not on display 0", "x"))
        self.assertEqual("", close_offer(BLOCKED.replace(GM, "com.example.bank"), "x"))
        self.assertEqual("", close_offer(BLOCKED, "goal with ] bracket"))


class CloseOwnerAppTest(unittest.TestCase):
    def bridge(self, dumps):
        bridge = Bridge.__new__(Bridge)
        bridge.adb = "adb"
        bridge.adb_run = Mock()
        controller = Mock()
        controller._run.side_effect = dumps
        return bridge, controller

    def run_close(self, package, dumps):
        bridge, controller = self.bridge(dumps)
        with patch.object(chat_bridge, "AndroidController", return_value=controller), patch.object(chat_bridge.time, "sleep"):
            return bridge.close_owner_app(package), bridge

    def test_closes_only_that_apps_main_screen_task(self):
        message, bridge = self.run_close(GM, [dump("ai.openphone.agent", True), dump("ai.openphone.agent", False)])
        self.assertIn("已按你的确认关掉主屏上的Gmail", message)
        bridge.adb_run.assert_called_once_with("shell", "am", "stack", "remove", "181", check=False)

    def test_finds_a_task_named_differently_from_its_package(self):
        message, bridge = self.run_close(CAL, [dump("ai.openphone.agent", False, True), dump("ai.openphone.agent", False)])
        self.assertIn("Google 日历", message)
        bridge.adb_run.assert_called_once_with("shell", "am", "stack", "remove", "197", check=False)

    def test_never_closes_a_window_in_use_or_an_unlisted_app(self):
        with self.assertRaises(AgentError):
            self.run_close(GM, [dump(GM, True)])
        for package in ("com.example.bank", "ai.openphone.agent", "com.google.android.permissioncontroller"):
            with self.subTest(package=package), self.assertRaises(AgentError):
                self.run_close(package, [])


if __name__ == "__main__":
    unittest.main()

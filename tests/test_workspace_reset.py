import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import agent_loop as agent
from test_workspace_isolation import FOCUS

BEFORE = """ACTIVITY MANAGER ACTIVITIES (dumpsys activity activities)
Display #0 (activities from top to bottom):
  * Task{40a4c6b #1 type=home U=0 visible=true}
    * Task{9c8303d #53 type=home I=com.google.android.apps.nexuslauncher/.NexusLauncherActivity U=0 rootTaskId=1}
  * Task{c7c6e6a #89 type=standard A=10166:com.google.android.gm U=0 visible=false}
  * Task{1cc1c81 #87 type=standard A=10208:ai.openphone.agent U=0 visible=false}
Display #5 (activities from top to bottom):
  * Task{7ab349 #88 type=standard A=10166:com.google.android.gm U=0 visible=true}
      rootOfTask=true task=Task{7ab349 #88 type=standard A=10166:com.google.android.gm}
  * Task{3058498 #84 type=standard A=10212:org.fossify.calendar U=0 visible=false}
ActivityTaskSupervisor state:
      * Task{c7c6e6a #89 type=standard A=10166:com.google.android.gm U=0}
"""
AFTER = BEFORE.split("Display #5")[0] + "Display #5 (activities from top to bottom):\nActivityTaskSupervisor state:\n"


class DisplayTasksTest(unittest.TestCase):
    def test_only_root_app_tasks_of_the_requested_display(self):
        self.assertEqual([(88, "com.google.android.gm"), (84, "org.fossify.calendar")],
                         agent.display_root_tasks(BEFORE, 5))
        self.assertEqual([(89, "com.google.android.gm"), (87, "ai.openphone.agent")],
                         agent.display_root_tasks(BEFORE, 0))
        self.assertEqual([], agent.display_root_tasks(AFTER, 5))


class ResetTest(unittest.TestCase):
    def controller(self, dumps):
        controller = agent.AndroidController("unused")
        controller.require_isolation = Mock()
        dumps = iter(dumps)
        controller._run = Mock(side_effect=lambda *args, **kw: next(dumps) if "activities" in args
                               else FOCUS if "input" in args else "")
        return controller

    @staticmethod
    def removed_ids(controller):
        return [c.args[-1] for c in controller._run.call_args_list if c.args[1:4] == ("am", "stack", "remove")]

    def test_closes_private_tasks_only(self):
        controller = self.controller([BEFORE, AFTER])
        removed = controller.reset_private_workspace(5)
        self.assertEqual(["88", "84"], self.removed_ids(controller))
        self.assertEqual([{"task": 88, "package": "com.google.android.gm"},
                          {"task": 84, "package": "org.fossify.calendar"}], removed)
        self.assertEqual(0, controller.last_focus_evidence["after"]["top_display"])

    def test_owner_display_is_never_reset(self):
        controller = self.controller([BEFORE])
        with self.assertRaisesRegex(agent.AgentError, "owner"):
            controller.reset_private_workspace(0)
        self.assertEqual([], self.removed_ids(controller))

    def test_task_also_listed_on_owner_display_stops_before_removal(self):
        overlap = BEFORE.replace("#84 type=standard A=10212:org.fossify.calendar",
                                 "#89 type=standard A=10166:com.google.android.gm")
        controller = self.controller([overlap.replace("#88", "#89")])
        with self.assertRaisesRegex(agent.AgentError, "owner's display"):
            controller.reset_private_workspace(5)
        self.assertEqual([], self.removed_ids(controller))

    @patch.object(agent.time, "sleep")
    def test_slow_removal_is_awaited_without_removing_again(self, sleep):
        controller = self.controller([BEFORE, BEFORE, BEFORE, AFTER])
        controller.reset_private_workspace(5)
        self.assertEqual(["88", "84"], self.removed_ids(controller))
        self.assertEqual(2, sleep.call_count)

    @patch.object(agent.time, "sleep")
    def test_incomplete_reset_stops_the_task(self, sleep):
        controller = self.controller([BEFORE] * 11)
        with self.assertRaisesRegex(agent.AgentError, "incomplete"):
            controller.reset_private_workspace(5)
        self.assertEqual(["88", "84"], self.removed_ids(controller))


NO_PRIVATE_WINDOW = FOCUS.replace("    displayId=5, name='private/browser'\n", "")
OWNER_LOST = FOCUS.replace("FocusedDisplayId: 0", "FocusedDisplayId: 5")


class SettledFocusTest(unittest.TestCase):
    def controller(self, dumps):
        controller = agent.AndroidController("unused")
        controller._run = Mock(side_effect=dumps)
        return controller

    @patch.object(agent.time, "sleep")
    def test_private_window_switch_is_awaited_briefly(self, sleep):
        controller = self.controller([NO_PRIVATE_WINDOW, NO_PRIVATE_WINDOW, FOCUS])
        self.assertEqual(0, controller.settled_owner_focus(5, private_window=True)["top_display"])
        self.assertEqual(2, sleep.call_count)

    @patch.object(agent.time, "sleep")
    def test_owner_focus_loss_or_lasting_gap_still_stops(self, sleep):
        with self.assertRaisesRegex(agent.AgentError, "owner input focus"):
            self.controller([OWNER_LOST]).settled_owner_focus(5, private_window=True)
        self.assertEqual(0, sleep.call_count)
        with self.assertRaisesRegex(agent.AgentError, "no focused window"):
            self.controller([NO_PRIVATE_WINDOW] * 6).settled_owner_focus(5, private_window=True)


class TargetRoutingTest(unittest.TestCase):
    def test_draft_mail_calendar_and_sms_open_their_app(self):
        cases = {"给 a@b.com 写封邮件，主题周会": agent.GMAIL_PACKAGE,
                 "帮我建一个10月18日上午10点的会议，叫周会": agent.CALENDAR_PACKAGE,
                 "在日历里看看明天有什么安排": agent.CALENDAR_PACKAGE,
                 "给183492发短信，内容是：晚会儿到。": agent.SMS_PACKAGE}
        for goal, package in cases.items():
            with self.subTest(goal=goal):
                self.assertEqual(package, agent.explicit_target_package(goal))

    def test_unrelated_or_negated_goals_do_not_route(self):
        for goal in ("在吗", "打开邮箱看看最新邮件", "不要打开日历", "京东和淘宝哪个便宜"):
            with self.subTest(goal=goal):
                self.assertIsNone(agent.explicit_target_package(goal))


if __name__ == "__main__":
    unittest.main()

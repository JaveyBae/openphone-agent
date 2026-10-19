import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import agent_loop
from agent_loop import AndroidController, OrderPageBlocked, explicit_target_package
from app_targets import MEITUAN_PACKAGE


class MeituanRoutingTest(unittest.TestCase):
    def test_takeout_requests_open_meituan(self):
        for goal in ("用美团点个外卖", "帮我点外卖，看看附近的麻辣烫", "打开美团搜一下奶茶"):
            with self.subTest(goal=goal):
                self.assertEqual(MEITUAN_PACKAGE, explicit_target_package(goal))


class OrderGuardTest(unittest.TestCase):
    def controller(self):
        controller = AndroidController.__new__(AndroidController)
        controller.dry_run = False
        controller.sms_task = False
        controller.require_display = Mock(return_value=2)
        controller.require_isolation = Mock()
        controller.require_owner_focus = Mock(return_value={})
        resumed = f"Display #2 (activities)\n    topResumedActivity=ActivityRecord{{1 u0 {MEITUAN_PACKAGE}/.Main t9}}"
        controller._run = Mock(return_value=resumed)
        return controller

    def test_no_check_before_meituan_was_opened(self):
        controller = self.controller()
        controller.check_order_page = Mock()
        controller.execute({"action": "tap", "arguments": {"x": 10, "y": 10}})
        controller.check_order_page.assert_not_called()

    def test_an_order_page_stops_the_tap_before_it_happens(self):
        controller = self.controller()
        controller.order_guard = True
        controller.check_order_page = Mock(side_effect=OrderPageBlocked("page shows '提交订单'"))
        with self.assertRaises(OrderPageBlocked):
            controller.execute({"action": "tap", "arguments": {"x": 10, "y": 10}})
        self.assertFalse(any("tap" in call.args for call in controller._run.call_args_list))


class OrderCheckOncePerPageTest(unittest.TestCase):
    """The ~2 s page check runs once per frame; a changed frame is checked again."""

    def controller(self):
        from test_workspace_isolation import STATE
        controller = AndroidController.__new__(AndroidController)
        controller.dry_run = False
        controller.order_guard = True
        controller.state = Mock(return_value=STATE)
        controller.frame = "a" * 64
        resumed = (f"Display #{STATE['displayId']} (activities)\n"
                   f"    topResumedActivity=ActivityRecord{{1 u0 {MEITUAN_PACKAGE}/.Main t9}}")

        def run(*args, **kwargs):
            return f"{controller.frame}  /frame.png" if "sha256sum" in args else resumed
        controller._run = Mock(side_effect=run)
        controller.check_order_page = Mock()
        return controller, STATE["displayId"]

    def test_same_frame_is_checked_once(self):
        controller, display = self.controller()
        controller.before_action("tap", display)
        controller.before_action("swipe", display)
        self.assertEqual(1, controller.check_order_page.call_count)

    def test_changed_frame_is_checked_again(self):
        controller, display = self.controller()
        controller.before_action("tap", display)
        controller.frame = "b" * 64
        controller.before_action("tap", display)
        self.assertEqual(2, controller.check_order_page.call_count)

    def test_blocked_page_stays_blocked_without_a_new_check(self):
        controller, display = self.controller()
        controller.check_order_page.side_effect = OrderPageBlocked("page shows '去支付'")
        for _ in range(2):
            with self.assertRaises(OrderPageBlocked):
                controller.before_action("tap", display)
        self.assertEqual(1, controller.check_order_page.call_count)

    def test_unknown_frame_is_always_checked(self):
        controller, display = self.controller()
        controller.frame = "not a hash"
        controller.before_action("tap", display)
        controller.before_action("tap", display)
        self.assertEqual(2, controller.check_order_page.call_count)

    def test_a_new_page_is_prefetched_after_a_wait_step(self):
        controller, display = self.controller()
        controller.prefetch_order_check(display)  # step 1 ends with a wait: no action joins it
        controller.frame = "b" * 64
        controller.prefetch_order_check(display)
        controller.before_action("tap", display)
        self.assertEqual(2, controller.check_order_page.call_count)

    def test_prefetched_check_is_reused_by_the_action(self):
        controller, display = self.controller()
        controller.prefetch_order_check(display)
        controller.before_action("tap", display)
        self.assertEqual(1, controller.check_order_page.call_count)
        self.assertIsNone(controller._order_prefetch)


if __name__ == "__main__":
    unittest.main()

import sys
import unittest
from datetime import date, datetime, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from alarm_params import AlarmParams, AlarmUnclear, alarm_scheduled, parse_alarm
from late_params import LateNotice, LateUnclear, message_body, parse_late, pick_event

NOW = datetime(2026, 10, 16, 15, 30)  # a Friday afternoon


class AlarmTest(unittest.TestCase):
    def test_times_and_repeats_come_from_rules(self):
        cases = {"明天早上7点半叫我起床": ("10月17日 07:30", "起床"),
                 "定一个晚上10点的闹钟": ("10月16日 22:00", "OpenPhone 闹钟"),
                 "设个闹钟今天下午5点，叫「开会」": ("10月16日 17:00", "开会"),
                 "每天早上6点45的闹钟": ("每天 06:45", "OpenPhone 闹钟"),
                 "工作日早上7点闹钟": ("工作日 07:00", "OpenPhone 闹钟"),
                 "每周六上午9点闹钟": ("每周六 09:00", "OpenPhone 闹钟")}
        for goal, expected in cases.items():
            with self.subTest(goal=goal):
                alarm = parse_alarm(goal, NOW)
                self.assertEqual(expected, (alarm.describe(), alarm.label))
        self.assertEqual(datetime(2026, 10, 19, 7, 0), parse_alarm("工作日早上7点闹钟", NOW).next_ring(NOW))

    def test_unclear_alarms_ask_and_others_are_not_alarms(self):
        for goal in ("7点的闹钟", "设个闹钟，明天下午5点"):
            with self.subTest(goal=goal), self.assertRaises(AlarmUnclear):
                parse_alarm(goal, NOW)
        for goal in ("取消明天的闹钟", "帮我在日历里加明天7点的日程", "给5550100发短信说我7点到"):
            with self.subTest(goal=goal):
                self.assertIsNone(parse_alarm(goal, NOW))

    def test_command_is_quoted_and_checked_in_the_system_list(self):
        command = AlarmParams(time(7, 30), "起 床", (2, 3), None).command(2)
        self.assertIn("--display 2 -a android.intent.action.SET_ALARM", command)
        self.assertIn("android.intent.extra.alarm.MESSAGE '起 床'", command)
        self.assertIn("--eial android.intent.extra.alarm.DAYS 2,3", command)
        dump = "  Next alarm clock information:\n    user:0 time:1 = 2026-10-17 07:30:00.000 = +15h"
        self.assertTrue(alarm_scheduled(dump, datetime(2026, 10, 17, 7, 30)))
        self.assertFalse(alarm_scheduled(dump, datetime(2026, 10, 17, 7, 31)))


class LateNoticeTest(unittest.TestCase):
    TODAY = date(2026, 10, 16)

    def test_recipient_day_and_delay(self):
        self.assertEqual(LateNotice("sms", "5550123", date(2026, 10, 22), 10),
                         parse_late("给5550123发短信，说10月22日的设计评审我晚10分钟到", self.TODAY))
        self.assertEqual(LateNotice("mail", "a@b.com", date(2026, 10, 17), 15),
                         parse_late("给 a@b.com 发邮件，说明天开会我晚十五分钟到", self.TODAY))
        with self.assertRaises(LateUnclear):
            parse_late("给小王发短信说明天开会我晚10分钟到", self.TODAY)
        self.assertIsNone(parse_late("给5550100发短信，内容是明天见", self.TODAY))

    def test_the_meeting_comes_from_the_calendar(self):
        events = ["10:00–10:30 团队站会", "15:00–16:00 需求评审会"]
        self.assertEqual(("10:00", "团队站会"), pick_event("团队站会我晚10分钟", events, NOW, date(2026, 10, 17)))
        self.assertEqual(("15:00", "需求评审会"), pick_event("开会我晚10分钟", events, datetime(2026, 10, 16, 12), self.TODAY))
        with self.assertRaises(LateUnclear):  # two meetings ahead and none named
            pick_event("开会我晚10分钟", events, NOW, date(2026, 10, 17))
        with self.assertRaises(LateUnclear):  # nothing left today
            pick_event("开会我晚10分钟", events, datetime(2026, 10, 16, 17), self.TODAY)
        self.assertEqual("抱歉，设计评审（11:00 开始）我会晚 10 分钟到，大约 11:10 到。", message_body("设计评审", "11:00", 10))


if __name__ == "__main__":
    unittest.main()

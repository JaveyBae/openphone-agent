import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from completion_check import alarm_missing, missing_values, required_values

CONTACT = ("在通讯录里新建一个联系人：名字 Test，姓氏 OpenPhone，电话 +41 44 555 01 23，"
           "邮箱 openphone.test@example.com，公司 OpenPhone Demo，然后保存")


class CompletionCheckTest(unittest.TestCase):
    def test_values_of_the_request(self):
        self.assertEqual(["openphone.test@example.com", "+41 44 555 01 23", "Test", "OpenPhone", "Demo"],
                         required_values(CONTACT))
        self.assertEqual(["LX188 苏黎世→巴黎"], required_values("10月25日8点30到11点45的日程，叫「LX188 苏黎世→巴黎」"))
        self.assertEqual(["Zürich", "Uetliberg"], required_values("在 Google 地图里查从（Zürich HB）到 Uetliberg 的路线"))

    def test_a_false_claim_is_caught(self):
        # The model said the e-mail was saved; the saved contact shows no e-mail.
        screen = ["Test OpenPhone", "OpenPhone Demo", "+41 44 555 01 23", "Mobile"]
        self.assertEqual(["openphone.test@example.com"], missing_values(CONTACT, screen))
        self.assertEqual([], missing_values(CONTACT, screen + ["openphone.test@example.com"]))
        # Phone numbers match by digits, whatever the grouping on screen.
        self.assertEqual([], missing_values("电话 +41 44 555 01 23", ["+41445550123"]))

    def test_alarm_requests_are_checked_in_the_system_list(self):
        now = datetime(2026, 10, 16, 15, 30)
        dump = "Next alarm clock information:\n user:0 time:1 = 2026-10-17 07:35:00.000"
        self.assertIsNone(alarm_missing("明天早上7点35叫我起床", dump, now))
        self.assertEqual("08:30 的闹钟", alarm_missing("明天早上8点30叫我起床", dump, now))
        self.assertIsNone(alarm_missing("查一下明天的天气", dump, now))


class AlarmSwitchTest(unittest.TestCase):
    def test_a_later_alarm_switched_on_in_clock_counts(self):
        from datetime import datetime
        from completion_check import alarm_missing
        goal, now = "早上9点05分叫我起床，帮我定个闹钟", datetime(2026, 10, 19, 2, 0)
        dump = "origWhen=2026-10-19 04:05:00.000"  # only the NEXT alarm is registered
        self.assertEqual("09:05 的闹钟", alarm_missing(goal, dump, now))
        on = [{"description": "9:05 AM alarm", "checked": True}]
        self.assertIsNone(alarm_missing(goal, dump, now, on))
        off = [{"description": "9:05 AM alarm", "checked": False}]
        self.assertEqual("09:05 的闹钟", alarm_missing(goal, dump, now, off))
        self.assertIsNone(alarm_missing(goal, "origWhen=2026-10-19 09:05:00.000", now))


if __name__ == "__main__":
    unittest.main()


class CalendarReadBackTest(unittest.TestCase):
    GOAL = "帮我在日历里建一个10月27日下午3点到4点的会议，叫基准测试34"

    def test_title_date_and_times_must_all_match(self):
        from datetime import date
        from zoneinfo import ZoneInfo
        from completion_check import calendar_missing, calendar_rows
        zone, today = ZoneInfo("Europe/Zurich"), date(2026, 10, 19)
        right = "Row: 0 title=基准测试34, dtstart=1793109600000, dtend=1793113200000"   # 15:00-16:00 (UTC+1 after DST)
        wrong = "Row: 0 title=基准测试34, dtstart=1793113200000, dtend=1793116800000"   # 16:00-17:00
        self.assertIsNone(calendar_missing(self.GOAL, calendar_rows(right), zone, today))
        self.assertEqual("10月27日 15:00–16:00 的「基准测试34」",
                         calendar_missing(self.GOAL, calendar_rows(wrong), zone, today))
        self.assertIsNotNone(calendar_missing(self.GOAL, [], zone, today))
        self.assertIsNone(calendar_missing("10月22日有什么安排？", [], zone, today))

    def test_a_named_place_must_be_saved_too(self):
        from datetime import date
        from zoneinfo import ZoneInfo
        from completion_check import calendar_missing, calendar_rows
        zone, today = ZoneInfo("Europe/Zurich"), date(2026, 10, 19)
        goal = "帮我在日历里加一个10月20日下午3点到4点的需求评审会，地点三楼大会议室"
        row = "Row: 0 title=需求评审会, dtstart=1792501200000, dtend=1792504800000, eventLocation="
        self.assertIn("三楼大会议室", calendar_missing(goal, calendar_rows(row), zone, today))
        self.assertIsNone(calendar_missing(goal, calendar_rows(row + "三楼大会议室"), zone, today))

import sys
import unittest
from datetime import date, time
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import skill_runner as runner
from event_params import EventParams, parse_day, parse_times, resolve
from execution_progress import progress_text

TODAY = date(2026, 10, 15)  # a Thursday


class EventParamsTest(unittest.TestCase):
    def test_dates_and_times_come_from_rules(self):
        cases = {
            "帮我在日历里加一个10月16日上午10点到10点半的团队站会": ("团队站会", date(2026, 10, 16), time(10), time(10, 30)),
            "后天晚上7点跟老王吃饭，记到日历上": ("跟老王吃饭", date(2026, 10, 17), time(19), time(20)),
            "下周一上午9点半开周会": ("开周会", date(2026, 10, 19), time(9, 30), time(10, 30)),
            "周五下午两点到三点半面试候选人": ("面试候选人", date(2026, 10, 16), time(14), time(15, 30)),
            "帮我建一个10月18日上午10点的会议，叫周会": ("周会", date(2026, 10, 18), time(10), time(11)),
        }
        for goal, expected in cases.items():
            with self.subTest(goal=goal):
                params = resolve(goal, "", TODAY)
                self.assertEqual(expected, (params.title, params.day, params.start, params.end))

    def test_model_title_only_when_it_is_the_owners_words(self):
        goal = "后天晚上7点跟老王吃饭，记到日历上"
        self.assertEqual("跟老王吃饭", resolve(goal, "event(title='Meeting with 老王', date='2026-10-20')", TODAY).title)
        self.assertEqual("老王吃饭", resolve(goal, "...\nevent(title='老王吃饭', date='2026-10-20')", TODAY).title)
        # A wrong model date never matters: the rules own the date.
        self.assertEqual(date(2026, 10, 17), resolve(goal, "event(title='老王吃饭', date='2026-10-20')", TODAY).day)

    def test_questions_and_unsupported_requests_fall_back(self):
        for goal in ("打开日历，告诉我明天有什么安排", "明天上午10点有会吗", "明天下午3点到4点开会，地点三楼会议室",
                     "明天上午10点开会，邀请小李", "每周一上午9点开周会", "明天开会", "10月16日和17日下午3点开会"):
            with self.subTest(goal=goal):
                self.assertIsNone(resolve(goal, "", TODAY))

    def test_words_inside_a_quoted_title_are_not_instructions(self):
        params = resolve("帮我在日历里加一个10月25日上午8点30到上午11点45的日程，叫「重复检查测试」", "", TODAY)
        self.assertEqual(("重复检查测试", date(2026, 10, 25)), (params.title, params.day))
        self.assertIsNone(resolve("每周一上午9点开会，叫「周会」", "", TODAY))

    def test_rule_details(self):
        self.assertEqual(date(2027, 1, 5), parse_day("1月5日上午10点开会", TODAY))
        self.assertEqual(date(2026, 10, 22), parse_day("周四下午开会", date(2026, 10, 16)))
        self.assertEqual((time(12, 30), None), parse_times("中午12点半吃饭"))
        self.assertIsNone(parse_times("上午10点和下午3点"))  # two times without 到 is not a range


class FakeUi:
    """A tiny Google Calendar stand-in driven by the same selectors as the device."""

    def __init__(self, existing=(), fail_on=None, saved=True):
        self.calls, self.fail_on, self.saved, self.events = [], fail_on, saved, list(existing)
        self.fields = {"title": "", "date": "Thu, Oct 15, 2026", "start": "8:30 AM", "end": "9:30 AM"}

    def call(self, mode, selector=None, text=None):
        self.calls.append((mode, selector, text))
        if self.fail_on and self.fail_on in str(selector):
            raise runner.AgentError("Selector matches 0 nodes")
        if mode == "text" and selector == {"id": runner.GCAL + "title"}:
            self.fields["title"] = text
        if mode == "click" and selector == {"id": runner.GCAL + "save"} and self.saved:
            self.events.append(f"{self.fields['title']}, 10:00 AM – 10:30 AM, me@example.com")
        return {"ok": True}

    def close(self):
        self.closed = True

    def snapshot(self):
        nodes = [{"description": desc} for desc in self.events]
        nodes += [{"id": runner.GCAL + "floating_action_button"},
                  {"id": runner.GCAL + "month_navigation_fragment_toggle", "text": "October 2026"},
                  {"id": runner.GCAL + "title", "text": self.fields["title"]},
                  {"description": "Start date: Fri, Oct 16, 2026"}, {"description": "End date: Fri, Oct 16, 2026"},
                  {"description": "Start time: 10:00 AM"}, {"description": "End time: 10:30 AM"},
                  {"description": "Switch to text input mode for the time input."}]
        return nodes


class SkillRunnerTest(unittest.TestCase):
    PARAMS = EventParams("团队站会", date(2026, 10, 16), time(10), time(10, 30))

    def run_with(self, ui):
        progress = []
        with patch.object(runner, "PrivateUiClient", return_value=ui), patch.object(runner.time, "sleep"):
            summary = runner.run_skill(Mock(), runner.load_skill("google_calendar_create_event"), self.PARAMS, progress.append)
        return summary, progress

    def test_full_path_by_control_identity_and_verified(self):
        ui = FakeUi()
        summary, progress = self.run_with(ui)
        self.assertIn("已在日历列表中核对", summary)
        self.assertIn(("text", {"id": runner.GCAL + "title"}, "团队站会"), ui.calls)
        self.assertIn(("click", {"descriptionEndsWith": " October 16"}, None), ui.calls)
        self.assertEqual(1, sum(1 for c in ui.calls if c[1] == {"id": runner.GCAL + "save"}))
        # Every operation names a control; none carries screen coordinates.
        self.assertTrue(all(c[1] is None or not {"x", "y", "bounds"} & set(c[1]) for c in ui.calls))
        self.assertIn("点 Save 保存", progress)

    def test_existing_event_is_not_created_again(self):
        ui = FakeUi(existing=["团队站会, 10:00 AM – 10:30 AM, me@example.com"])
        summary, _ = self.run_with(ui)
        self.assertIn("没有重复创建", summary)
        clicked = [c[1] for c in ui.calls if c[0] == "click"]
        self.assertNotIn({"id": runner.GCAL + "floating_action_button"}, clicked)
        self.assertNotIn({"id": runner.GCAL + "save"}, clicked)

    def test_same_time_under_another_title_pauses_without_creating(self):
        ui = FakeUi(existing=["Friday 16 October 2026, Open Day View",
                              "站会, 10:00\u202fAM – 10:30\u202fAM, me@example.com"])
        with self.assertRaises(runner.SkillPaused) as raised:
            self.run_with(ui)
        self.assertIn("已经有「站会」", str(raised.exception))
        clicked = [c[1] for c in ui.calls if c[0] == "click"]
        self.assertNotIn({"id": runner.GCAL + "floating_action_button"}, clicked)
        # A different time on that day is no reason to stop.
        ui = FakeUi(existing=["Friday 16 October 2026, Open Day View", "站会, 9:00\u202fAM – 9:30\u202fAM, me@example.com"])
        self.assertIn("已在日历列表中核对", self.run_with(ui)[0])

    def test_failure_before_save_falls_back_without_saving(self):
        ui = FakeUi(fail_on="Event button")
        with self.assertRaises(runner.SkillFallback):
            self.run_with(ui)
        self.assertFalse(any(c[1] == {"id": runner.GCAL + "save"} for c in ui.calls))

    def test_uncertain_save_pauses_and_never_saves_twice(self):
        ui = FakeUi(saved=False)
        with self.assertRaises(runner.SkillPaused):
            self.run_with(ui)
        self.assertEqual(1, sum(1 for c in ui.calls if c[1] == {"id": runner.GCAL + "save"}))

    def test_labels_and_formats(self):
        self.assertEqual("10:00 AM", runner.clock_label(time(10)))
        self.assertEqual("12:30 PM", runner.clock_label(time(12, 30)))
        self.assertEqual("Fri, Oct 16, 2026", runner.placeholders(self.PARAMS)["date_ui"])


class AgendaSkillTest(unittest.TestCase):
    LIST = ["Thursday 15 October 2026, Open Day View", "Nothing planned. Tap to create.",
            "Friday 16 October 2026, Open Day View", "团队站会, 10:00\u202fAM – 10:30\u202fAM, me@example.com",
            "Oct 18\u2009–\u200924", "Thursday 22 October 2026, Open Day View",
            "设计评审, 11:00\u202fAM – 11:30\u202fAM, me@example.com", "November 2026"]

    def run_agenda(self, day, snapshots):
        from event_params import DayParams
        ui = Mock()
        ui.snapshot.side_effect = [[{"id": runner.GCAL + "floating_action_button"}]] + snapshots
        class Today(runner.datetime):  # the skill reads the clock: pin it to the fixture's week
            @classmethod
            def now(cls, tz=None):
                return runner.datetime(2026, 10, 16, 9, 0, tzinfo=tz)
        with patch.object(runner, "PrivateUiClient", return_value=ui), patch.object(runner.time, "sleep"), \
                patch.object(runner, "datetime", Today):
            return runner.run_skill(Mock(), runner.load_skill("google_calendar_read_day"), DayParams(day), lambda _: None), ui

    def nodes(self, items):
        return [{"description": d} for d in items]

    def test_answers_from_a_settled_list_in_24_hour_time(self):
        reply, _ = self.run_agenda(date(2026, 10, 16), [self.nodes(self.LIST)] * 2)
        self.assertEqual("10月16日（五）有 1 项安排：10:00–10:30 团队站会。", reply)
        reply, _ = self.run_agenda(date(2026, 10, 19), [self.nodes(self.LIST)] * 2)
        self.assertEqual("10月19日（一）没有安排。", reply)

    def test_next_week_is_answered_once_the_list_passes_sunday(self):
        from event_params import DayParams, agenda_day
        self.assertEqual(DayParams(date(2026, 10, 19), date(2026, 10, 25)), agenda_day("告诉我我下周的日程呢", TODAY))
        self.assertEqual(DayParams(TODAY, date(2026, 10, 18)), agenda_day("这周还有什么安排", TODAY))
        week = DayParams(date(2026, 10, 19), date(2026, 10, 25))
        listed = self.nodes(self.LIST)  # passes Oct 25 via the "November 2026" banner
        reply, _ = self.run_agenda_params(week, [listed] * 2)
        self.assertEqual("10月19日–10月25日共 1 项安排：\n10月22日（四）：11:00–11:30 设计评审", reply)
        short = self.nodes(self.LIST[:7])  # stops at Oct 22: the week is not over yet, so scroll
        reply, ui = self.run_agenda_params(week, [short, short, listed, listed])
        ui.call.assert_any_call("scroll", {"scrollable": True})

    def run_agenda_params(self, params, snapshots):
        ui = Mock()
        ui.snapshot.side_effect = [[{"id": runner.GCAL + "floating_action_button"}]] + snapshots
        with patch.object(runner, "PrivateUiClient", return_value=ui), patch.object(runner.time, "sleep"):
            return runner.run_skill(Mock(), runner.load_skill("google_calendar_read_day"), params, lambda _: None), ui

    def test_a_list_still_loading_never_yields_no_events(self):
        loading = [self.nodes(self.LIST[:4 + i % 3]) for i in range(8)]  # keeps changing
        with self.assertRaises(runner.SkillFallback):
            self.run_agenda(date(2026, 10, 19), loading)


class SkillProgressTest(unittest.TestCase):
    def test_only_skill_file_labels_are_shown(self):
        self.assertIn("选择日期", progress_text({"stage": "skill", "step": 0, "total": 25, "label": "选择日期"}))
        self.assertIsNone(progress_text({"stage": "skill", "step": 0, "total": 25, "label": "任意模型文字"}))


if __name__ == "__main__":
    unittest.main()

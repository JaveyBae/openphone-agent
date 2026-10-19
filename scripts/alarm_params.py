"""Alarm requests ("明天早上7点半叫我起床") -> Android's standard set-alarm intent.

The time comes from rules only (event_params.parse_times). The alarm is set with
AlarmClock.ACTION_SET_ALARM + SKIP_UI on the private display, then checked in the
system's own alarm list; no model step and nothing on the owner's screen.
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from event_params import parse_day, parse_times, unquoted

ALARM = re.compile(r"闹钟|闹铃|叫我起床|叫醒我|叫我|alarm", re.I)
# Calendar.SUNDAY = 1 ... SATURDAY = 7, as AlarmClock.EXTRA_DAYS expects.
WEEKDAYS = {"一": 2, "二": 3, "三": 4, "四": 5, "五": 6, "六": 7, "日": 1, "天": 1}
EXTRA = "android.intent.extra.alarm."


class AlarmUnclear(ValueError):
    """A clear alarm request that cannot be set as asked: the owner is told why, nothing is set."""


@dataclass(frozen=True)
class AlarmParams:
    at: time
    label: str
    days: tuple[int, ...] = ()  # repeating days; empty = once, at the next occurrence
    when: datetime | None = None  # the single occurrence (None when repeating)

    def command(self, display_id: int) -> str:
        """The device shell command; values are quoted for the device shell."""
        parts = ["am", "start", "--display", str(display_id), "-a", "android.intent.action.SET_ALARM",
                 "--ei", EXTRA + "HOUR", str(self.at.hour), "--ei", EXTRA + "MINUTES", str(self.at.minute),
                 "--es", EXTRA + "MESSAGE", self.label, "--ez", EXTRA + "SKIP_UI", "true"]
        if self.days:
            parts += ["--eial", EXTRA + "DAYS", ",".join(map(str, self.days))]
        return " ".join(shlex.quote(part) for part in parts)

    def next_ring(self, now: datetime) -> datetime:
        """When the alarm rings next (Calendar day numbers: Sunday = 1)."""
        if self.when is not None:
            return self.when
        for ahead in range(8):
            moment = datetime.combine(now.date() + timedelta(days=ahead), self.at)
            if moment > now and (moment.isoweekday() % 7) + 1 in self.days:
                return moment
        raise ValueError("no ring day")

    def describe(self) -> str:
        clock = f"{self.at.hour:02d}:{self.at.minute:02d}"
        if self.days:
            names = {v: k for k, v in WEEKDAYS.items() if k != "天"}
            if self.days == (2, 3, 4, 5, 6):
                repeat = "工作日"
            elif len(self.days) == 7:
                repeat = "每天"
            else:
                repeat = "每周" + "、".join(names[d] for d in self.days)
            return f"{repeat} {clock}"
        assert self.when is not None
        return f"{self.when.month}月{self.when.day}日 {clock}"


def parse_alarm(goal: str, now: datetime) -> AlarmParams | None:
    """One alarm time from the owner's words, or None (unclear requests go to the model)."""
    text = unquoted(goal)
    if not ALARM.search(text) or re.search(r"取消|删除|关掉|关闭|不要|别|日历|日程|短信|邮件", text):
        return None
    times = parse_times(text)
    if times is None or times[1] is not None:
        return None
    at = times[0]
    if not re.search(r"凌晨|早上|上午|中午|下午|傍晚|晚上|\d{1,2}[:：]\d{2}", text) and 1 <= at.hour < 12:
        # "7点" alone: an alarm without 上午/下午 would be a guess (起床 means morning).
        if not re.search(r"起床|叫醒", text):
            raise AlarmUnclear(f"闹钟要定在早上 {at.hour} 点还是晚上 {at.hour} 点？请说「早上{at.hour}点」或「晚上{at.hour}点」，我再设。")
    quoted = re.search(r"「([^「」\n]{1,30})」", goal)
    label = quoted.group(1).strip() if quoted else ("起床" if re.search(r"起床|叫醒", text) else "OpenPhone 闹钟")
    if re.search(r"每天|天天", text):
        return AlarmParams(at, label, (1, 2, 3, 4, 5, 6, 7))
    if re.search(r"工作日|周一到周五|星期一到星期五", text):
        return AlarmParams(at, label, (2, 3, 4, 5, 6))
    weekly = re.findall(r"每(?:周|星期|个星期)([一二三四五六日天])", text)
    if weekly:
        return AlarmParams(at, label, tuple(sorted({WEEKDAYS[d] for d in weekly})))
    # Once: the requested day, or the next occurrence of that time.
    day = parse_day(text, now.date())
    if day is None:
        day = now.date() if datetime.combine(now.date(), at) > now else now.date() + timedelta(days=1)
    when = datetime.combine(day, at)
    # SET_ALARM always rings at the NEXT occurrence: any other day cannot be set this way.
    next_ring = datetime.combine(now.date(), at)
    if next_ring <= now:
        next_ring += timedelta(days=1)
    if when != next_ring:
        raise AlarmUnclear(f"系统的设闹钟指令只能设在下一次到 {at.hour:02d}:{at.minute:02d} 的时候"
                           f"（{next_ring.month}月{next_ring.day}日），没法直接设到 {day.month}月{day.day}日。"
                           "没有设任何闹钟；可以晚点再让我设，或在时钟应用里手动设置。")
    return AlarmParams(at, label, (), when)


def alarm_scheduled(alarm_dump: str, when: datetime) -> bool:
    """The system alarm list holds an alarm clock entry at exactly this local time."""
    stamp = when.strftime("%Y-%m-%d %H:%M:00")
    return stamp in alarm_dump


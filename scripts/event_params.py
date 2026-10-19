"""Parameters for the calendar skill: model-extracted, rule-checked.

Dates and clock times in the owner's Chinese request are computed by fixed
rules (never guessed); the title must be words the owner actually wrote.
Anything unsupported returns None so the agent falls back to step-by-step
model control instead of creating an event on a wrong day.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
          "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
WEEKDAYS = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
# Only these extras are safe for the skill; anything else goes to the model loop.
# A question about the calendar is never a request to create an event.
QUESTION = re.compile(r"什么|吗|？|\?|查一?下|看看|告诉我|有没有|几点|哪天|是否")
UNSUPPORTED = re.compile(r"地点|地址|在.{1,12}(?:开|见)|会议室|邀请|参会|叫上|和.{1,8}一起|提醒|重复|每天|每周|每月|全天|视频")


def unquoted(goal: str) -> str:
    """The request without its 「quoted title」: words inside a title are not instructions."""
    return re.sub(r"「[^「」]*」", "", goal)


@dataclass(frozen=True)
class DayParams:
    day: date
    last: date | None = None  # a range (e.g. next week), inclusive; None for one day


CREATE = re.compile(r"(?:加|添加|建|新建|创建|安排一个|约|记到|记一下|放到)")
AGENDA = re.compile(r"安排|日程|行程|会议|有什么事|忙不忙")


def agenda_day(goal: str, today: date) -> DayParams | None:
    """A question about one upcoming day's schedule, answered read-only."""
    if not QUESTION.search(goal) or not AGENDA.search(goal) or CREATE.search(goal):
        return None
    day = parse_day(goal, today)
    if day is None:
        # A whole week: 下周 = next Monday to Sunday, 这周/本周 = today to Sunday.
        monday = today - timedelta(days=today.weekday())
        if re.search(r"下(?:周|星期|礼拜)", goal) and not re.search(r"下下", goal):
            return DayParams(monday + timedelta(days=7), monday + timedelta(days=13))
        if re.search(r"(?:这|本)(?:周|星期|礼拜)", goal):
            return DayParams(today, monday + timedelta(days=6))
        return None
    return DayParams(day) if day >= today else None


@dataclass(frozen=True)
class EventParams:
    title: str
    day: date
    start: time
    end: time


def number(text: str) -> int | None:
    if text.isdigit():
        return int(text)
    if text and all(c in DIGITS for c in text):
        if text == "十":
            return 10
        if text.startswith("十"):
            return 10 + DIGITS[text[1]]
        if text.endswith("十"):
            return DIGITS[text[0]] * 10
        if len(text) == 3 and text[1] == "十":
            return DIGITS[text[0]] * 10 + DIGITS[text[2]]
        if len(text) == 1:
            return DIGITS[text]
    return None


def parse_day(goal: str, today: date) -> date | None:
    found = []
    for match in re.finditer(r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})[日号]", goal):
        year = int(match.group(1)) if match.group(1) else today.year
        try:
            day = date(year, int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
        if not match.group(1) and day < today:
            day = day.replace(year=today.year + 1)  # "1月5日" said in October means next year
        found.append(day)
    for word, offset in (("大后天", 3), ("后天", 2), ("明天", 1), ("今天", 0)):
        if word in goal:
            found.append(today + timedelta(days=offset))
            goal = goal.replace(word, "")
    for match in re.finditer(r"(下下|下|这|本)?(?:周|星期|礼拜)([一二三四五六日天])", goal):
        target = WEEKDAYS[match.group(2)]
        monday = today - timedelta(days=today.weekday())
        weeks = {"下下": 2, "下": 1}.get(match.group(1) or "", 0)
        day = monday + timedelta(weeks=weeks, days=target)
        if not match.group(1) and day < today:
            day += timedelta(weeks=1)  # a bare "周一" means the next one
        found.append(day)
    return found[0] if len(set(found)) == 1 else None


def parse_times(goal: str) -> tuple[time, time | None] | None:
    clock = r"(\d{1,2}|[零〇一二两三四五六七八九十]{1,3})(?:点|:|：)(半|一刻|三刻|(\d{1,2}|[零〇一二三四五六七八九十]{1,3})分?)?"
    period = r"(凌晨|早上|上午|中午|下午|傍晚|晚上)?"
    matches = list(re.finditer(period + clock, goal))
    if not 1 <= len(matches) <= 2:
        return None
    values, last_period = [], None
    for match in matches:
        hour = number(match.group(2))
        tail = match.group(3)
        minute = {"半": 30, "一刻": 15, "三刻": 45}.get(tail, number(match.group(4)) if match.group(4) else 0)
        part = match.group(1) or last_period
        last_period = part
        if hour is None or minute is None or not 0 <= hour <= 23 or not 0 <= minute <= 59:
            return None
        if part in ("下午", "傍晚", "晚上") and hour < 12:
            hour += 12
        if part == "中午" and hour < 6:
            hour += 12
        values.append(time(hour, minute))
    if len(values) == 2 and not re.search(r"(?:到|至|~|-|—)\s*$", goal[matches[0].end():matches[1].start()]):
        return None
    return values[0], (values[1] if len(values) == 2 else None)


def quoted_title(goal: str) -> str | None:
    """叫「…」: the owner quoted the whole title, spaces and arrows included."""
    quoted = re.search(r"(?:叫|叫做|名为|标题是|主题是)「([^「」\n]{1,30})」", goal)
    return quoted.group(1).strip() if quoted and quoted.group(1).strip() else None


def rule_title(goal: str) -> str | None:
    called = re.search(r"(?:叫|叫做|名为|标题是|主题是)[「“\"]?([^，。,.\s「」“”\"]{1,30})", goal)
    if called:
        return called.group(1)
    tail = re.search(r"的([^，。,.\s的]{2,20})[。！!]?$", goal)
    return tail.group(1) if tail else None


FILLER = re.compile(r"帮我|请|麻烦|在日历[里上中]?|到日历[里上中]?|日历[里上中]|(?:加|添加|建|新建|创建|安排|约|记)(?:一个|一下|个)?"
                    r"|记到|放到|[，。,.！!]|(?:下下|下|这|本)?(?:周|星期|礼拜)[一二三四五六日天]|大后天|后天|明天|今天"
                    r"|(?:\d{4}年)?\d{1,2}月\d{1,2}[日号]|(?:到|至|~|-|—)")


def residual_title(goal: str) -> str | None:
    """What remains after removing dates, times and request phrasing."""
    clock = r"(?:凌晨|早上|上午|中午|下午|傍晚|晚上)?(?:\d{1,2}|[零〇一二两三四五六七八九十]{1,3})(?:点|:|：)(?:半|一刻|三刻|(?:\d{1,2}|[零〇一二三四五六七八九十]{1,3})分?)?"
    rest = FILLER.sub(" ", re.sub(clock, " ", goal))
    rest = re.sub(r"^\s*的|的\s*$", "", re.sub(r"\s+", " ", rest).strip()).strip()
    return rest if 2 <= len(rest) <= 20 and " " not in rest else None


def extract_model_fields(raw: str) -> dict[str, str]:
    """Take the LAST event(...)-like call in a verbose model reply."""
    calls = re.findall(r"\b\w*event\w*\s*\((.*?)\)", raw, re.I | re.S)
    if not calls:
        return {}
    return {key.lower(): value for key, value in
            re.findall(r"(title|date|start|end)\s*[=:]\s*[\"']([^\"']*)[\"']", calls[-1], re.I)}


def resolve(goal: str, model_reply: str, today: date) -> EventParams | None:
    """Rule-checked parameters, or None to fall back to step-by-step control."""
    if UNSUPPORTED.search(unquoted(goal)) or QUESTION.search(unquoted(goal)):
        return None
    day, times = parse_day(goal, today), parse_times(goal)
    if day is None or times is None:
        return None
    start, end = times
    if end is None:
        end = (datetime.combine(day, start) + timedelta(hours=1)).time()
    if end <= start:
        return None
    # Dates and times come from the rules above, never from the model, which
    # miscounts relative days. The model only proposes the title, and that
    # title must be the owner's own words: never a translation or invention.
    model_title = extract_model_fields(model_reply).get("title", "").strip()
    title = quoted_title(goal) or (model_title if model_title and model_title in goal else None) \
        or rule_title(goal) or residual_title(goal)
    if not title or len(title) > 30:
        return None
    return EventParams(title, day, start, end)

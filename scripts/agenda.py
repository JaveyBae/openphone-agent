"""Read one day's events from Google Calendar's schedule list (read-only).

The schedule list reads top to bottom: a day header ("Friday 16 October 2026,
Open Day View") followed by that day's events ("团队站会, 10:00 AM – 10:30 AM,
account"), week separators ("Oct 18 – 24") and month banners. A day without
events has no header, so "no events" is only concluded once the list has
visibly passed that day; otherwise the caller scrolls or falls back.
"""
from __future__ import annotations

import re
from datetime import date, datetime

DAY_HEADER = re.compile(r"^(?:Today )?\w+day (\d{1,2}) (\w+) (\d{4})(?:, Open Day View)?$")
WEEK = re.compile(r"^([A-Z][a-z]{2}) (\d{1,2})\s*[–-]\s*(?:([A-Z][a-z]{2}) )?(\d{1,2})$")
MONTH = re.compile(r"^([A-Z][a-z]+) (\d{4})$")
EVENT = re.compile(r"^(.+?), (All day|\d{1,2}:\d{2}\s?[AP]M\s*[–-]\s*\d{1,2}:\d{2}\s?[AP]M)")


def _clean(text: str) -> str:
    return text.replace(" ", " ").replace(" ", " ")


def _clock(text: str) -> str:
    """'3:00 PM – 4:00 PM' -> '15:00–16:00'; 'All day' -> '全天'."""
    if text == "All day":
        return "全天"
    times = [datetime.strptime(part.strip().replace(" ", ""), "%I:%M%p").strftime("%H:%M")
             for part in re.split(r"[–-]", text)]
    return "–".join(times)


def read_schedule(nodes: list[dict], today: date) -> tuple[dict[date, list[str]], date | None]:
    """Events per day and the latest day the visible list has reached."""
    events: dict[date, list[str]] = {}
    current, reached = None, None
    for node in nodes:
        desc = _clean(str(node.get("description") or ""))
        if desc == "Nothing planned. Tap to create." and current:
            events.setdefault(current, [])
            continue
        header = DAY_HEADER.match(desc)
        if header:
            current = datetime.strptime(" ".join(header.groups()), "%d %B %Y").date()
            events.setdefault(current, [])
            reached = max(filter(None, (reached, current)))
            continue
        week = WEEK.match(desc)
        if week:
            year = (current or today).year
            month = week.group(3) or week.group(1)
            end = datetime.strptime(f"{week.group(4)} {month} {year}", "%d %b %Y").date()
            if end < (current or today):
                end = end.replace(year=end.year + 1)
            # A separator means every day before the week's start had nothing more to show.
            start = datetime.strptime(f"{week.group(2)} {week.group(1)} {year}", "%d %b %Y").date()
            reached = max(filter(None, (reached, start)))
            current = None
            continue
        month = MONTH.match(desc)
        if month:
            start = datetime.strptime(desc, "%B %Y").date()
            reached = max(filter(None, (reached, start)))
            current = None
            continue
        event = EVENT.match(desc)
        if event and current:
            events[current].append(f"{_clock(event.group(2))} {event.group(1)}")
    return events, reached


def answer_range(first: date, last: date, events: dict[date, list[str]], reached: date | None) -> str | None:
    """Chinese answer for several days, once the visible list has passed the last one."""
    if reached is None or reached <= last:
        return None
    days = sorted(day for day, items in events.items() if first <= day <= last and items)
    label = f"{first.month}月{first.day}日–{last.month}月{last.day}日"
    if not days:
        return f"{label}没有安排。"
    count = sum(len(events[day]) for day in days)
    lines = [f"{day.month}月{day.day}日（{'一二三四五六日'[day.weekday()]}）：" + "；".join(events[day]) for day in days]
    return f"{label}共 {count} 项安排：\n" + "\n".join(lines)


def answer(day: date, events: dict[date, list[str]], reached: date | None, today: date) -> str | None:
    """Chinese answer, or None if the visible list cannot settle it yet."""
    label = f"{day.month}月{day.day}日（{'一二三四五六日'[day.weekday()]}）"
    if day in events:
        items = events[day]
        if not items:
            return f"{label}没有安排。"
        return f"{label}有 {len(items)} 项安排：" + "；".join(items) + "。"
    if day >= today and reached is not None and reached > day:
        return f"{label}没有安排。"
    return None

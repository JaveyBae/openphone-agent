"""Check a model's "done" against the app itself before telling the owner.

The model's finish message is a claim. Here the concrete values of the request
(e-mail addresses, phone numbers, Latin names, 「quoted」 text) must appear in the
private app's final UI tree, and an alarm request must be in the system's alarm
list. Anything missing turns "done" into "please check": a claim is never shown
as a result it is not.
"""
from __future__ import annotations

import re
from datetime import datetime

# Words naming an app or a place in the phone, not data the result must contain.
APP_WORDS = {"google", "maps", "gmail", "chrome", "browser", "calendar", "contacts", "messages", "sms",
             "clock", "youtube", "openphone agent", "agent"}


def _squash(text: str) -> str:
    return re.sub(r"[\s  \-–()]+", "", text).casefold()


def required_values(goal: str) -> list[str]:
    """The literal values a finished request must show."""
    values: list[str] = []
    emails = re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", goal)
    values += emails
    rest = goal
    for email in emails:
        rest = rest.replace(email, " ")
    values += [q.strip() for q in re.findall(r"「([^「」]{1,40})」", rest) if q.strip()]
    values += [p.strip() for p in re.findall(r"\+?\d[\d \-]{5,}\d", rest)]  # phone-like numbers
    for word in re.findall(r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'.]{2,}", rest):
        if word.casefold() not in APP_WORDS and word not in values:
            values.append(word)
    return values


def missing_values(goal: str, ui_texts: list[str]) -> list[str]:
    """Values of the request not found anywhere in the final UI tree."""
    screen = _squash(" ".join(ui_texts))
    missing = []
    for value in required_values(goal):
        key = _squash(value)
        if re.fullmatch(r"\+?[\d]+", key):  # phone numbers: digits only, any grouping
            if re.sub(r"\D", "", key) not in re.sub(r"\D", "", screen):
                missing.append(value)
        elif key not in screen:
            missing.append(value)
    return missing


def alarm_missing(goal: str, alarm_dump: str, now: datetime, nodes: list[dict] | None = None) -> str | None:
    """An alarm request that is neither scheduled nor switched on in Clock (None when fine or not an alarm).

    The Clock app registers only its NEXT alarm with the system, so with an earlier alarm
    already on, a new one is not in the dump yet; its on/off switch in the Clock list is.
    """
    from alarm_params import AlarmUnclear, parse_alarm
    try:
        alarm = parse_alarm(goal, now)
    except AlarmUnclear:
        return None
    if alarm is None:
        return None
    if f"{alarm.at.hour:02d}:{alarm.at.minute:02d}:00" in alarm_dump:
        return None
    hour = alarm.at.hour % 12 or 12
    label = f"{hour}:{alarm.at.minute:02d} {'AM' if alarm.at.hour < 12 else 'PM'} alarm"
    if any(n.get("description") == label and n.get("checked") is True for n in nodes or []):
        return None
    return f"{alarm.at.hour:02d}:{alarm.at.minute:02d} 的闹钟"


def ui_texts(nodes: list[dict] | None) -> list[str]:
    return [str(n.get(k) or "") for n in nodes or [] for k in ("text", "description") if n.get(k)]


def calendar_expected(goal: str, today) -> tuple[str, datetime, datetime | None] | None:
    """Title, local start and end of an event the goal asks to create (None when it names no title/day/time)."""
    from event_params import parse_day, parse_times, quoted_title, rule_title
    import re
    # "…的需求评审会，地点三楼大会议室": the title is the last "的…" before the place clause.
    title = quoted_title(goal) or rule_title(re.sub(r"[，,；;]\s*(?:地点|地址|位置)[^，,。；;]*[。！!]?$", "", goal))
    day, times = parse_day(goal, today), parse_times(goal)
    if not title or not day or not times:
        return None
    start, end = times
    return title, datetime.combine(day, start), datetime.combine(day, end) if end else None


def calendar_rows(query_output: str) -> list[tuple]:
    """`content query` lines of the calendar provider: (title, dtstart ms, dtend ms[, location])."""
    import re
    rows = []
    for line in query_output.splitlines():
        found = re.search(r"title=(.*), dtstart=(\d+), dtend=(\d+)(?:, eventLocation=(.*))?", line)
        if found:
            row = (found.group(1), int(found.group(2)), int(found.group(3)))
            rows.append(row + (found.group(4).strip(),) if found.group(4) is not None else row)
    return rows


def calendar_location(goal: str) -> str | None:
    """The place a request names for the event ("地点三楼大会议室", "地点：A301")."""
    import re
    found = re.search(r"(?:地点|地址|位置|location)\s*(?:是|在|为|:|：)?\s*([^，,。；;！!？?\s]+)", goal, re.I)
    return found.group(1) if found else None


def calendar_missing(goal: str, rows: list[tuple[str, int, int]], zone, today) -> str | None:
    """The requested event is not in the calendar with this title AT the requested date and times."""
    expected = calendar_expected(goal, today)
    if expected is None:
        return None
    title, start, end = expected
    place = calendar_location(goal)
    want = f"{start.month}月{start.day}日 {start:%H:%M}" + (f"–{end:%H:%M}" if end else "") + f" 的「{title}」"
    for name, dtstart, dtend, *location in rows:
        if name != title:
            continue
        if dtstart == int(start.replace(tzinfo=zone).timestamp() * 1000) and (
                end is None or dtend == int(end.replace(tzinfo=zone).timestamp() * 1000)):
            if place and location and place not in location[0]:
                return want + f"（地点要是「{place}」，现在是「{location[0] or '空'}」）"
            return None
    return want + (f"，地点「{place}」" if place else "")


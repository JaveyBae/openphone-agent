"""Calendar -> message: "给5550100发短信，说明天的站会我晚10分钟到".

Recipient, day and delay come from rules; the meeting is found in the owner's
calendar (read-only skill); the message is only ever a draft the owner sends.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from event_params import number, parse_day
from message_params import EMAIL, NUMBER, digits

LATE = re.compile(r"(?:晚|迟到?)\s*(\d{1,3}|[一二两三四五六七八九十]{1,3})\s*分钟")


@dataclass(frozen=True)
class LateNotice:
    channel: str  # "sms" or "mail"
    to: str
    day: date
    minutes: int


class LateUnclear(ValueError):
    """A clear late-notice request that needs one more answer from the owner; nothing was drafted."""


def parse_late(goal: str, today: date) -> LateNotice | None:
    """A request to tell someone the owner will be late to a meeting, or None."""
    late = LATE.search(goal)
    if not late:
        return None
    minutes = number(late.group(1))
    if not minutes or not 1 <= minutes <= 180:
        return None
    emails = set(EMAIL.findall(goal))
    numbers = {digits(n) for n in NUMBER.findall(EMAIL.sub(" ", goal[:late.start()]))}
    numbers = {n for n in numbers if 3 <= len(n.lstrip("+")) <= 15}
    if re.search(r"邮件|email|mail", goal, re.I) and len(emails) == 1:
        channel, to = "mail", emails.pop()
    elif re.search(r"短信|信息|sms", goal, re.I) and len(numbers) == 1:
        channel, to = "sms", numbers.pop()
    elif re.search(r"短信|信息|邮件|sms|mail", goal, re.I):
        raise LateUnclear("要发给谁？请写上对方的手机号（短信）或邮箱地址（邮件），比如「给 555-0100 发短信」。"
                          "目前还不能按联系人名字查号码。")
    else:
        return None
    day = parse_day(goal, today) or today
    if day < today:
        return None
    return LateNotice(channel, to, day, minutes)


def pick_event(goal: str, events: list[str], now: datetime, day: date) -> tuple[str, str]:
    """(start "HH:MM", title) of the meeting meant, from items like "10:00–10:30 团队站会".

    A title named in the request wins; otherwise the only meeting still ahead that day.
    """
    parsed = []
    for item in events:
        match = re.match(r"(\d{2}:\d{2})–\d{2}:\d{2} (.+)$", item)
        if match:  # all-day items have no start time and are never "late"
            parsed.append((match.group(1), match.group(2)))
    named = [e for e in parsed if e[1] in goal]
    if len(named) == 1:
        return named[0]
    ahead = [e for e in parsed if day > now.date() or e[0] >= now.strftime("%H:%M")]
    label = f"{day.month}月{day.day}日"
    if not ahead:
        raise LateUnclear(f"{label}日历里没有还没开始的安排，所以没有写消息。")
    if len(ahead) > 1:
        listing = "；".join(f"{start} {title}" for start, title in ahead)
        raise LateUnclear(f"{label}有好几个安排：{listing}。是哪一个？请在请求里写上名字，比如「{ahead[0][1]}」。")
    return ahead[0]


def message_body(title: str, start: str, minutes: int) -> str:
    hour, minute = map(int, start.split(":"))
    arrive = (datetime(2000, 1, 1, hour, minute) + timedelta(minutes=minutes)).strftime("%H:%M")
    return f"抱歉，{title}（{start} 开始）我会晚 {minutes} 分钟到，大约 {arrive} 到。"

"""Turn one email into a calendar proposal: model reads, rules verify.

The model proposes title/date/start/end from the email text. Every value must
be traceable to the email itself (date as 10月25日, times as 8:30, title words),
otherwise there is no proposal. Nothing is written to the calendar here: the
owner confirms first, and the confirmation is an ordinary calendar request
handled by the calendar skill.
"""
from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time

from event_params import extract_model_fields

PROMPT = """The current date: {today}. Read this email and extract the one calendar event it
describes (for a flight: start = departure, end = arrival). End your reply with exactly one line:
event(title='...', date='YYYY-MM-DD', start='HH:MM', end='HH:MM')
Use 24-hour times, and words from the email for the title (e.g. flight number and cities).
Email subject: {subject}
Email: {body}"""


@dataclass(frozen=True)
class Proposal:
    title: str
    day: date
    start: time
    end: time

    def request(self) -> str:
        """The confirmation, phrased as a request the calendar skill understands."""
        def clock(value: time) -> str:
            period = "上午" if value.hour < 12 else "下午"
            hour = value.hour if value.hour <= 12 else value.hour - 12
            return f"{period}{hour}点" + (f"{value.minute:02d}" if value.minute else "")
        title = re.sub(r"\s+", " ", self.title).strip()
        return (f"帮我在日历里加一个{self.day.month}月{self.day.day}日{clock(self.start)}到{clock(self.end)}"
                f"的日程，叫「{title}」")


def ask(base_url: str, model: str, api_key: str, subject: str, body: str, today: date, timeout: int = 60) -> str:
    payload = {"model": model, "temperature": 0, "max_tokens": 500, "messages": [
        {"role": "user", "content": PROMPT.format(today=today.isoformat(), subject=subject, body=body)}]}
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    request = urllib.request.Request(base_url.rstrip("/") + "/chat/completions",
                                     data=json.dumps(payload).encode(), headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)["choices"][0]["message"]["content"]


def _times_in(text: str) -> set[str]:
    found = set()
    for hour, minute in re.findall(r"(\d{1,2})\s*[:：点]\s*(\d{2})", text):
        found.add(f"{int(hour):02d}:{minute}")
        found.add(f"{(int(hour) + 12) % 24:02d}:{minute}")  # "3:00" may be 15:00 in context
    return found


def verify(subject: str, body: str, model_reply: str, today: date) -> Proposal | None:
    fields = extract_model_fields(model_reply)
    text = subject + "\n" + body
    try:
        day = date.fromisoformat(fields["date"])
        start = datetime.strptime(fields["start"], "%H:%M").time()
        end = datetime.strptime(fields["end"], "%H:%M").time()
    except (KeyError, ValueError):
        return None
    # Times belong in the event's time fields, not in its title.
    title = re.sub(r"\s+", " ", re.sub(r"\d{1,2}\s*[:：]\s*\d{2}", " ", fields.get("title", ""))).strip()
    if not title or len(title) > 30 or end <= start or day < today:
        return None
    # The date and both times must literally appear in the email.
    if f"{day.month}月{day.day}日" not in text and f"{day.month}/{day.day}" not in text \
            and day.isoformat() not in text:
        return None
    if not {fields["start"], fields["end"]} <= _times_in(text):
        return None
    # Nothing invented: every Latin/number token appears verbatim in the email and
    # every Chinese character appears in it too (the model may reorder words).
    tokens = re.findall(r"[A-Za-z0-9]+", title)
    hanzi = re.findall(r"[\u4e00-\u9fff]", title)
    joiners = set("到至从起飞达往")
    if not (tokens or hanzi) or any(t not in text for t in tokens) \
            or any(h not in text and h not in joiners for h in hanzi):
        return None
    return Proposal(flight_title(title, text) or title, day, start, end)


FLIGHT = re.compile(r"(?<![A-Za-z0-9])([A-Z]{2}|[A-Z][0-9]|[0-9][A-Z])\s?(\d{2,4})(?![0-9])")
PLACE = r"([\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z ]{0,11}?)\s*(?:国际)?(?:机场)?"


def flight_title(title: str, text: str) -> str | None:
    """A flight reads best as "LX188 苏黎世→巴黎": number and both cities taken
    verbatim from the email, only when the model's title names that flight."""
    flight = FLIGHT.search(text)
    if not flight or flight.group(1) + flight.group(2) not in title.replace(" ", ""):
        return None
    origin = re.search(r"从\s*" + PLACE + r"\s*(?:起飞|出发)", text)
    destination = re.search(r"(?:到达|抵达|飞抵|飞往|前往)\s*" + PLACE + r"(?=[。，,.;；!！\s]|$)", text)
    if not origin or not destination or origin.group(1) == destination.group(1):
        return None
    return f"{flight.group(1)}{flight.group(2)} {origin.group(1).strip()}→{destination.group(1).strip()}"

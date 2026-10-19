"""Rule-only parameters for the SMS draft skill: an explicit number and body.

The number is copied exactly as the owner wrote it (never completed or
guessed); the body is the text after an explicit marker such as 内容是.
Anything ambiguous returns None and the agent works step by step instead.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app_targets import is_sms_goal

NUMBER = re.compile(r"(?<![\d+])(\+?\d[\d -]{1,18}\d)(?!\d)")
BODY = re.compile(r"(?:内容是|内容为|内容[:：]|正文是|正文[:：]|告诉(?:他|她|对方)|跟(?:他|她|对方)说|说)\s*[:：]?\s*(.+)$", re.S)
QUOTES = "「」“”\"'‘’『』"


@dataclass(frozen=True)
class SmsParams:
    number: str
    body: str


def digits(text: str) -> str:
    return re.sub(r"[^\d+]", "", text)


def parse_sms(goal: str) -> SmsParams | None:
    if not is_sms_goal(goal):
        return None
    head, _, _ = goal.partition("内容")
    numbers = {digits(n) for n in NUMBER.findall(head or goal)}
    if len(numbers) != 1:
        return None
    number = numbers.pop()
    if not 3 <= len(number.lstrip("+")) <= 15:
        return None
    body = BODY.search(goal)
    if not body:
        return None
    text = body.group(1).strip().strip(QUOTES).strip()
    if not text or len(text) > 300 or number in digits(text):
        return None
    return SmsParams(number, text)


EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
SUBJECT = re.compile(r"(?:主题|标题)(?:是|为|叫)?\s*[:：]?\s*[「“\"]?([^，。,\n「」“”\"]{1,40})")
MAIL_BODY = re.compile(r"(?:内容是|内容为|内容[:：]|正文是|正文[:：]|告诉(?:他|她|对方)|跟(?:他|她|对方)说|说)\s*[:：]?\s*(.+)$", re.S)


@dataclass(frozen=True)
class MailParams:
    to: str
    subject: str
    body: str


def parse_mail(goal: str) -> MailParams | None:
    """Exactly one complete address and an explicit body; the subject may be empty."""
    from app_targets import is_mail_compose_goal
    if not is_mail_compose_goal(goal):
        return None
    addresses = set(EMAIL.findall(goal))
    if len(addresses) != 1:
        return None
    to = addresses.pop()
    body = MAIL_BODY.search(goal.replace(to, " "))
    if not body:
        return None
    text = body.group(1).strip().strip(QUOTES).strip()
    subject = SUBJECT.search(goal)
    subject_text = subject.group(1).strip() if subject else ""
    if subject and subject_text in text:
        text = text.split("主题")[0].rstrip("，,。 ") or text
    if not text or len(text) > 2000:
        return None
    return MailParams(to, subject_text, text)


def subject_from_body(body: str) -> str:
    """No subject given: the body's first clause, in the owner's own words."""
    first = re.split(r"[，,。！!？?；;\n]", body.strip(), maxsplit=1)[0].strip()
    if first.isascii() and len(first) > 40:
        return first[:41].rsplit(" ", 1)[0]  # whole words only, at most 40 characters
    return first if first.isascii() else first[:15]


@dataclass(frozen=True)
class MailQuery:
    query: str


def mail_query(goal: str) -> MailQuery:
    """Search the named email (all folders, including spam), else recent itineraries."""
    named = re.search(r"[「『“\"]([^」』”\"]{2,40})[」』”\"]", goal) \
        or re.search(r"(?:标题|主题)(?:是|为|叫)\s*([^，。,\s]{2,40})", goal) \
        or re.search(r"([^，。,\s把读看]{2,40})那封(?:邮件|信)", goal)
    if named:
        return MailQuery(f'in:anywhere "{named.group(1)}"')
    return MailQuery("in:anywhere newer_than:7d (航班 OR 行程 OR flight OR itinerary)")

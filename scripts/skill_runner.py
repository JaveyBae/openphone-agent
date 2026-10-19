"""Replay a learned UI skill on the private display, step by verified step.

A skill (skills/*.json) lists controls by identity (id / text / description),
never by screen coordinates. Before its commit step any mismatch raises
SkillFallback: the caller discards the draft and lets the model work step by
step. After the commit step nothing is retried; problems become SkillPaused so
the owner checks the result instead of risking a duplicate event.
"""
from __future__ import annotations

import json
import re
import time
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from agent_common import AgentError
from android_device import AndroidController
from event_params import EventParams
from message_params import MailParams, MailQuery, SmsParams, digits
from private_ui import PrivateUiClient

SKILLS = Path(__file__).resolve().parents[1] / "skills"
GCAL = "com.google.android.calendar:id/"


class SkillFallback(AgentError):
    """Nothing was committed: hand the task back to step-by-step model control."""


class SkillPaused(AgentError):
    """The commit step ran (or its result is uncertain): the owner must check."""


def load_skill(name: str) -> dict:
    return json.loads((SKILLS / (name + ".json")).read_text(encoding="utf-8"))


def clock_label(value) -> str:
    """'10:00 AM' as Google Calendar shows it in the editor."""
    return f"{value.hour % 12 or 12}:{value.minute:02d} {'AM' if value.hour < 12 else 'PM'}"


def placeholders(params) -> dict[str, str]:
    if isinstance(params, SmsParams):
        return {"number": params.number, "body": params.body}
    if isinstance(params, MailQuery):
        return {"query": params.query}
    if isinstance(params, MailParams):
        return {"to": params.to, "subject": params.subject or "（无主题）", "subject_raw": params.subject,
                "body": params.body}
    day = params.day
    values = {"date_ui": f"{day:%a}, {day:%b} {day.day}, {day.year}"}
    if isinstance(params, EventParams):
        values.update({"title": params.title,
                       "start_label": clock_label(params.start), "end_label": clock_label(params.end),
                       # The schedule list joins time and AM/PM with a narrow no-break space.
                       "start_ui": clock_label(params.start).replace(" ", "\u202f"),
                       "end_ui": clock_label(params.end).replace(" ", "\u202f")})
    return values


def fill(value, values: dict[str, str]):
    if isinstance(value, str):
        return value.format(**values)
    if isinstance(value, dict):
        return {key: fill(item, values) for key, item in value.items()}
    if isinstance(value, list):
        return [fill(item, values) for item in value]
    return value


def matches(node: dict, selector: dict) -> bool:
    """Same selector semantics as the device-side PrivateUi helper."""
    text, desc = str(node.get("text")), str(node.get("description"))
    return all((
        "scrollable" not in selector or selector["scrollable"] == bool(node.get("scrollable")),
        "id" not in selector or selector["id"] == str(node.get("id")),
        "class" not in selector or selector["class"] == str(node.get("class")),
        "text" not in selector or selector["text"] == text,
        "description" not in selector or selector["description"] == desc,
        "textStartsWith" not in selector or text.startswith(selector["textStartsWith"]),
        "descriptionStartsWith" not in selector or desc.startswith(selector["descriptionStartsWith"]),
        "descriptionEndsWith" not in selector or desc.endswith(selector["descriptionEndsWith"]),
    ))


def find(ui: PrivateUiClient, selector: dict, wait: float = 0.0) -> list[dict]:
    """Snapshot until the selector matches, for at most `wait` seconds of real time."""
    deadline = time.monotonic() + wait
    for attempt in range(1 + int(wait / 0.3)):
        if attempt:
            if time.monotonic() >= deadline:
                break
            time.sleep(0.3)
        try:
            nodes = ui.snapshot()
        except AgentError:
            nodes = []  # e.g. the app is still on its launch screen: keep waiting
        found = [node for node in nodes if matches(node, selector)]
        if found:
            return found
    return []


GM = "com.google.android.gm:id/"


def read_results(ui: PrivateUiClient, seconds: float) -> list[dict]:
    """Search result rows once the list stops loading and two reads agree."""
    deadline, previous = time.monotonic() + seconds, None
    while time.monotonic() < deadline:
        nodes = ui.snapshot()
        loading = any("ProgressBar" in str(n.get("class")) for n in nodes)
        rows, row = [], None
        for node in nodes:
            name = str(node.get("id")).split(":id/")[-1]
            if name == "viewified_conversation_item_view":
                # The row's full text (subject included) is reported as its text or its description.
                row = {"_row": str(node.get("description") or node.get("text") or "")}
                rows.append(row)
            elif row is not None and name in ("senders", "subject", "snippet", "date"):
                row.setdefault(name, str(node.get("text") or ""))
        if any("No Connection" in str(n.get("text")) for n in nodes):
            raise SkillFallback("the phone has no internet connection for Gmail search")
        if not loading and rows == previous:
            return [full_subject(row) for row in rows]
        previous = rows
        time.sleep(1.0)
    raise SkillFallback("Gmail search results did not settle")


INVISIBLE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")


def full_subject(row: dict) -> dict:
    """The subject view may be cut at its start around search highlights
    ("班行程确认"); the row's own description carries the full subject as one of
    its ", "-separated fields (senders, subject, snippet, time, labels)."""
    described = INVISIBLE.sub("", row.pop("_row", ""))
    subject = INVISIBLE.sub("", row.get("subject", "")).strip()
    if not subject:
        return row
    for field in (part.strip() for part in described.split(", ")):
        # The shortest field that ends with the visible part, a few characters longer at most.
        if field.endswith(subject) and len(field) <= len(subject) + 10:
            row["subject"] = field
            break
    return row


def pick_date(ui: PrivateUiClient, field: str, day: date) -> None:
    ui.call("click", {"descriptionStartsWith": field})
    target = f"{day:%B} {day.year}"
    for _ in range(13):
        header = find(ui, {"id": GCAL + "month_navigation_fragment_toggle"}, wait=2)
        if len(header) != 1:
            raise SkillFallback("date picker did not open")
        shown = datetime.strptime(header[0]["text"], "%B %Y").date()
        if header[0]["text"] == target:
            break
        step = "next" if shown < day.replace(day=1) else "previous"
        ui.call("click", {"id": GCAL + "month_navigation_" + step})
    else:
        raise SkillFallback("date picker never reached " + target)
    ui.call("click", {"descriptionEndsWith": f" {day:%B} {day.day}"})
    ui.call("click", {"id": GCAL + "confirm_button"})


def set_time(ui: PrivateUiClient, field: str, value) -> None:
    ui.call("click", {"descriptionStartsWith": field})
    # One read tells the picker's mode; never wait for a button that is not there.
    mode = find(ui, {"descriptionStartsWith": "Switch to ", "descriptionEndsWith": " mode for the time input."}, wait=2)
    if len(mode) != 1:
        raise SkillFallback("time picker did not open")
    if mode[0].get("description") == "Switch to text input mode for the time input.":
        ui.call("click", {"description": "Switch to text input mode for the time input."})
    ui.call("text", {"class": "android.widget.EditText"}, str(value.hour % 12 or 12))
    ui.call("click", {"class": "android.view.View", "descriptionEndsWith": " minutes"})
    ui.call("text", {"class": "android.widget.EditText"}, f"{value.minute:02d}")
    period = "am" if value.hour < 12 else "pm"
    ui.call("click", {"id": GCAL + f"material_clock_period_{period}_button"})
    ui.call("click", {"id": GCAL + "material_timepicker_ok_button"})


def run_skill(controller: AndroidController, skill: dict, params: EventParams,
              progress: Callable[[str], None], timings: list | None = None, rehearse: bool = False,
              outputs: dict | None = None) -> str:
    """rehearse=True stops right before the commit step (nothing is saved).
    Read-only skills put what they read into `outputs`."""
    ui = PrivateUiClient(controller, skill["app"], resident=True)
    try:
        return replay(ui, skill, params, progress, timings, rehearse, outputs if outputs is not None else {})
    finally:
        ui.close()  # the device-side process exits with its adb connection


def replay(ui: PrivateUiClient, skill: dict, params: EventParams, progress: Callable[[str], None],
           timings: list | None, rehearse: bool, outputs: dict) -> str:
    values = placeholders(params)
    committed = False
    for step in skill["steps"]:
        op, label = step["op"], step["label"]
        is_commit = skill.get("commit_step") is not None and step.get("id") == skill["commit_step"]
        if rehearse and is_commit:
            return "演练结束：已走到保存前一步，没有保存。"
        progress(label)
        started = time.perf_counter()
        try:
            if op == "wait_for":
                if not find(ui, fill(step["select"], values), wait=step.get("seconds", 10)):
                    raise SkillFallback("app screen not ready: " + json.dumps(step["select"]))
                time.sleep(step.get("settle", 0))  # some screens reset fields typed too early
            elif op == "pick_recipient":
                # Type the digits with the private display's scoped key input: this
                # Compose field drops accessibility SET_TEXT. Keys append, so the
                # number is typed once and never retried. A known number may show as an
                # existing conversation row instead of "Send to …"; either row must
                # carry exactly the requested digits.
                ui.c.execute({"action": "input_text", "arguments": {"text": params.number}})
                rows = []
                deadline = time.monotonic() + 4
                while time.monotonic() < deadline:
                    nodes = [n for n in ui.snapshot() if n.get("text") and not n.get("editable")]
                    rows = [n for n in nodes if digits(str(n["text"]).removeprefix("Send to ")) == params.number]
                    if rows:
                        break
                    time.sleep(0.3)
                choice = [n for n in rows if str(n["text"]).startswith("Send to ")] or rows
                if len({str(n["text"]) for n in choice}) != 1:
                    raise SkillFallback("recipient picker did not offer exactly this number")
                ui.call("click", {"text": choice[0]["text"]})
            elif op == "write_draft":
                field_nodes = find(ui, step["select"], wait=5)
                if len(field_nodes) != 1:
                    raise SkillFallback("message field not found")
                current = str(field_nodes[0].get("text") or "")
                if current == params.body:
                    pass  # the requested draft is already there: do not retype
                elif current in step["empty_texts"] or not current:
                    ui.call("text", step["select"], params.body)
                else:
                    raise SkillPaused(f"这个号码已有一条未发送的草稿「{current}」，Agent 没有覆盖它。"
                                      "请你决定保留还是改写；Agent 不会发送任何短信。")
            elif op == "verify_recipient":
                if not any(digits(str(n.get("text") or "")) == params.number for n in ui.snapshot()):
                    raise SkillFallback("conversation is not with the requested number")
            elif op == "check_absent":
                # The home screen is already loaded (wait_for): one read is enough.
                if find(ui, fill(step["select"], values)):
                    return f"日历里已经有「{params.title}」{values['start_label']}–{values['end_label']}，没有重复创建。"
                # Same day and exact same time under another title is most likely the same
                # event worded differently: ask instead of creating a near-duplicate.
                from agenda import read_schedule
                span = f"{params.start:%H:%M}–{params.end:%H:%M} "
                same_time = [item[len(span):] for item in read_schedule(ui.snapshot(), datetime.now().date())[0]
                             .get(params.day, []) if item.startswith(span)]
                if same_time:
                    raise SkillPaused(f"{params.day.month}月{params.day.day}日 {span.strip()} 已经有「{same_time[0]}」，"
                                      f"可能就是同一个安排，所以没有再建「{params.title}」。"
                                      "如果确实要另建，请在 Google 日历里手动添加。")
            elif op == "click":
                if is_commit:
                    committed = True  # Recorded BEFORE the click: never replayed if uncertain.
                ui.call("click", fill(step["select"], values))
            elif op == "text":
                value = fill(step["value"], values)
                if step.get("skip_if_empty") and (not value or (step["value"] == "{subject}" and not values.get("subject_raw"))):
                    pass
                else:
                    ui.call("text", fill(step["select"], values), value)
            elif op == "key":
                ui.c.execute({"action": "key", "arguments": {"keycode": step["keycode"]}})
            elif op == "read_mail":
                outputs["emails"] = read_results(ui, step.get("seconds", 25))
            elif op == "type_keys":
                # ASCII through the private display's scoped key input (fields that drop SET_TEXT).
                value = fill(step["value"], values)
                if not value.isascii():
                    raise SkillFallback("key input is ASCII only")
                ui.c.execute({"action": "input_text", "arguments": {"text": value}})
            elif op == "pick_date":
                pick_date(ui, step["field"], params.day)
            elif op == "set_time":
                set_time(ui, step["field"], getattr(params, step["value"]))
            elif op == "verify":
                # All fields from one read; re-read only while something is still missing.
                for attempt in range(4):
                    nodes = ui.snapshot()
                    missing = [expect for expect in fill(step["expect"], values)
                               if len(hits := [n for n in nodes if matches(n, expect["select"])]) != 1
                               or ("text" in expect and hits[0].get("text") != expect["text"])]
                    if not missing:
                        break
                    time.sleep(0.3)
                else:
                    raise SkillFallback("editor field check failed: " + json.dumps(missing[0], ensure_ascii=False))
            elif op == "read_agenda":
                from agenda import answer, answer_range, read_schedule
                today = datetime.now().date()
                for _ in range(1 + step.get("max_scrolls", 5)):
                    # Answer only from a settled list: two identical reads in a row,
                    # so a half-loaded list can never yield a false "no events".
                    previous, seen = None, None
                    for _ in range(8):
                        seen = read_schedule(ui.snapshot(), today)
                        if seen == previous and seen[1] is not None:
                            break
                        previous = seen
                        time.sleep(0.4)
                    else:
                        raise SkillFallback("schedule list did not settle")
                    reply = (answer_range(params.day, params.last, seen[0], seen[1]) if params.last
                             else answer(params.day, seen[0], seen[1], today))
                    if reply is not None:
                        if outputs is not None and not params.last:
                            outputs["events"] = list(seen[0].get(params.day, []))  # e.g. "10:00–10:30 团队站会"
                        return reply
                    ui.call("scroll", {"scrollable": True})
                raise SkillFallback("schedule list never reached the requested day")
            elif op == "verify_saved":
                if not find(ui, fill(step["select"], values), wait=4):
                    raise SkillPaused("已点保存，但没在日历列表里看到这条日程，请核对；Agent 不会再次保存。")
            else:
                raise SkillFallback("unknown skill step " + op)
            if timings is not None:
                timings.append({"label": label, "seconds": round(time.perf_counter() - started, 2)})
        except SkillPaused:
            raise
        except AgentError as error:
            if committed:
                raise SkillPaused("保存这一步之后出现异常，请核对日历；Agent 不会再次保存。（" + str(error) + "）") from error
            raise SkillFallback(f"{label}: {error}") from error
    if skill.get("handoff"):
        return fill(skill["handoff"], values)
    if skill.get("read_only"):
        return "已读取。"  # the caller uses what landed in `outputs`
    return (f"已按技能「{skill['title']}」建好：{params.title}，{params.day.month}月{params.day.day}日 "
            f"{params.start:%H:%M}–{params.end:%H:%M}，已在日历列表中核对。")


TITLE_PROMPT = """The current date: {today}. Extract the calendar event the owner asks for.
Reply with EXACTLY one line: event(title='...', date='YYYY-MM-DD', start='HH:MM', end='HH:MM')
The title must use the owner's own words, not a translation.
Owner request: {goal}"""


def ask_model(base_url: str, model: str, api_key: str, goal: str, today: date, timeout: int = 60) -> str:
    """One text-only inference; its reply is only a title proposal, checked by rules."""
    body = {"model": model, "temperature": 0, "max_tokens": 400,
            "messages": [{"role": "user", "content": TITLE_PROMPT.format(today=today.isoformat(), goal=goal)}]}
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    request = urllib.request.Request(base_url.rstrip("/") + "/chat/completions",
                                     data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)["choices"][0]["message"]["content"]


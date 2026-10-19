"""Facts read from the private app's UI tree and told to the model with the screenshot.

A model that only sees pixels confuses look-alike screens: Google Calendar's time dialog
("Select time") looks the same for the start and the end time, and a 27B model typed
hours into the title or opened and confirmed the dialog in turn without noticing that
nothing changed. The note names what the screen cannot show: which time the open dialog
is for, and the values the event form holds now.
"""
from __future__ import annotations

import re

CALENDAR = "com.google.android.calendar"
FORM_FIELDS = ("Start date", "Start time", "End date", "End time")


def _id(node: dict) -> str:
    return str(node.get("id") or "").split(":id/")[-1]


def _text(node: dict) -> str:
    return str(node.get("text") or "").strip()


def calendar_note(nodes: list[dict], opened_from: dict | None) -> str | None:
    """What the Calendar screen holds: the open time dialog or the event form's values."""
    ids = {_id(n): n for n in nodes if _id(n)}
    if "material_timepicker_ok_button" in ids:
        which = re.match(r"(Start|End) time", str((opened_from or {}).get("description") or ""))
        # Clock mode: material_hour_tv / material_minute_tv; text mode: an EditText for the hour and
        # a view described "30 minutes" for the minute.
        if "material_hour_tv" in ids:
            hour, minute = _text(ids["material_hour_tv"]), _text(ids.get("material_minute_tv", {}))
        else:
            # Text mode: the focused field is an EditText, the other a view described "3 o'clock" /
            # "30 minutes"; hour comes first in the tree.
            fields = [_text(n) for n in nodes if re.fullmatch(r"\d{1,2}", _text(n)) and (
                str(n.get("class", "")).endswith("EditText")
                or re.search(r"o'clock|minutes?$", str(n.get("description") or "")))]
            hour, minute = (fields + ["", ""])[:2]
        period = next((_text(n) for n in nodes if _id(n) in ("material_clock_period_am_button",
                                                             "material_clock_period_pm_button") and n.get("checked")), "")
        shown = f"{hour}:{minute} {period}".strip(" :") if hour or minute else ""
        note = "The open dialog sets the " + (which.group(1).upper() + " time" if which else "time")
        note += f" (it shows {shown})" if shown else ""
        note += (". Set hour, minute and AM/PM here, then tap OK once. Typing goes into these hour/minute "
                 "fields, never into the event title.")
        if which and which.group(1) == "Start":
            note += " Changing the start time moves the end time along (same duration)."
        return note
    search = next((n for n in nodes if _id(n) == "search_text"), None)
    if search is not None:
        typed = _text(search)
        if not typed or typed == "Add location":
            return "This is the location page: Type the place named in the request."
        return (f"The location field holds {typed!r}. Press the Enter key (a separate action, not a line break in typed text) to keep exactly this text; "
                "do not tap a suggestion (they are other places) and do not press Back (it discards it).")
    values = {}
    for node in nodes:
        match = re.match(r"(Start date|Start time|End date|End time):\s*(.+)", str(node.get("description") or ""))
        if match:
            values.setdefault(match.group(1), match.group(2).strip())
    if not values:
        return None
    title = next((_text(n) for n in nodes if _id(n) == "title" and str(n.get("class", "")).endswith("EditText")), None)
    form = ", ".join(f"{name.lower()} {values[name]}" for name in FORM_FIELDS if name in values)
    title_text = "" if title is None else "title " + ("(empty)" if not title or title == "Add title" else repr(title)) + ", "
    if any(_text(n) == "Add location" for n in nodes):
        form += ", location (empty)"
    return f"The event form now holds: {title_text}{form}. Change only the fields that differ from the request."


def screen_note(package: str | None, nodes: list[dict] | None, opened_from: dict | None) -> str | None:
    if not nodes:
        return None
    if package == CALENDAR:
        return calendar_note(nodes, opened_from)
    return None


def opener_after(nodes: list[dict] | None, action: dict, previous: dict | None) -> dict | None:
    """The control that opened the dialog now on screen: the last tap made outside a dialog."""
    import experience
    if not nodes or action.get("action") != "tap":
        return previous
    if any(_id(n) == "material_timepicker_ok_button" for n in nodes):
        return previous  # a tap inside the dialog does not change what the dialog is for
    args = action.get("arguments", {})
    return experience.target_at(nodes, int(args.get("x", -1)), int(args.get("y", -1)))

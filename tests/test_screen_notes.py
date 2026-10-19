import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from screen_notes import CALENDAR, opener_after, screen_note  # noqa: E402

G = CALENDAR + ":id/"


def node(ident="", text=None, desc=None, cls="android.widget.TextView", checked=False, bounds="[0,0][10,10]"):
    return {"id": G + ident if ident else "", "text": text, "description": desc, "class": cls, "checked": checked,
            "bounds": bounds, "clickable": True}


# Read from Google Calendar on the emulator (Oct 2026): the event form and the time dialog in both modes.
FORM = [node("title", "Add title", cls="android.widget.EditText", bounds="[128,94][672,196]"),
        node(text="Mon, Oct 19, 2026", desc="Start date: Mon, Oct 19, 2026", bounds="[128,462][568,558]"),
        node(text="3:30 PM", desc="Start time: 3:30 PM", bounds="[568,462][688,558]"),
        node(text="4:30 PM", desc="End time: 4:30 PM", bounds="[568,558][688,654]")]
PERIOD = [node("material_clock_period_am_button", "AM"), node("material_clock_period_pm_button", "PM", checked=True)]
CLOCK = [node("header_title", "Select time"), node("material_hour_tv", "03", "3 o'clock", checked=True),
         node("material_minute_tv", "30", "30 minutes"), *PERIOD, node("material_timepicker_ok_button", "OK")]
TEXT = [node("header_title", "Select time"), node(text="03", cls="android.widget.EditText"),
        node("material_label", "Hour"), node(text="30", desc="30 minutes"), *PERIOD,
        node("material_timepicker_ok_button", "OK")]


class CalendarNoteTest(unittest.TestCase):
    def test_the_form_names_its_current_values(self):
        note = screen_note(CALENDAR, FORM, None)
        self.assertIn("title (empty)", note)
        self.assertIn("start time 3:30 PM", note)
        self.assertIn("end time 4:30 PM", note)

    def test_the_time_dialog_says_which_time_it_sets(self):
        opened = opener_after(FORM, {"action": "tap", "arguments": {"x": 620, "y": 500}}, None)
        self.assertEqual("Start time: 3:30 PM", opened["description"])
        for dialog in (CLOCK, TEXT):
            note = screen_note(CALENDAR, dialog, opened)
            self.assertIn("START time (it shows 03:30 PM)", note)
            self.assertIn("never into the event title", note)

    def test_taps_inside_the_dialog_keep_what_it_is_for(self):
        opened = {"description": "End time: 4:30 PM"}
        self.assertIs(opened, opener_after(TEXT, {"action": "tap", "arguments": {"x": 5, "y": 5}}, opened))
        self.assertIn("END time", screen_note(CALENDAR, TEXT, opened))

    def test_other_apps_get_no_note(self):
        self.assertIsNone(screen_note("com.google.android.deskclock", FORM, None))
        self.assertIsNone(screen_note(CALENDAR, None, None))

    def test_the_location_page_says_how_to_keep_the_text(self):
        typed = [node("search_text", "三楼大会议室", cls="android.widget.EditText"),
                 node("first_line_text", "Japan, Nagano, Okaya, 田中町")]
        self.assertIn("Enter key", screen_note(CALENDAR, typed, None))

    def test_text_mode_with_the_minute_focused(self):
        dialog = [node("header_title", "Select time"), node(text="03", desc="3 o'clock"),
                  node(text="30", cls="android.widget.EditText"), *PERIOD, node("material_timepicker_ok_button", "OK")]
        self.assertIn("it shows 03:30 PM", screen_note(CALENDAR, dialog, None))

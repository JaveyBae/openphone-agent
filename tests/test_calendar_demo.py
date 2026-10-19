import json
from datetime import date
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
sys.path.insert(0,str(Path(__file__).parents[1] / "scripts"))
from calendar_demo import (preset, validate_spec, verify_editor, verify_local_calendar,
                           CalendarUiClient, node_id, TITLE, LOCATION, DESCRIPTION, AgentError, display_title)
from chat_bridge import Bridge

class CalendarPresetTest(unittest.TestCase):
    def test_explicit_timezone_and_half_hour(self):
        start,end=validate_spec(preset(date(2026,10,14)))
        self.assertEqual(1800,(end-start).total_seconds())
        self.assertEqual("Europe/Zurich",str(start.tzinfo))
        self.assertEqual(15,start.hour)

    def test_dst_uses_date_not_current_offset(self):
        summer,_=validate_spec(preset(date(2026,10,14)))
        winter,_=validate_spec(preset(date(2026,11,14)))
        self.assertEqual(7200,summer.utcoffset().total_seconds())
        self.assertEqual(3600,winter.utcoffset().total_seconds())

    def test_invalid_or_expanded_preset_is_rejected(self):
        good=preset(date(2026,10,14))
        for bad in (None,{}, {**good,"title":"other"}, {**good,"attendees":["someone"]},
                    {**good,"timezone":"UTC"}, {**good,"date":"2026-02-30"},
                    {**good,"reminderMinutes":True}, {**good,"reminderMinutes":5}, {**good,"end":"14:30"}):
            with self.subTest(bad=bad),self.assertRaises(AgentError):validate_spec(bad)

    def editor(self):
        vals={"event_title":display_title(preset(date(2026,10,14))),"event_location":LOCATION,"event_description":DESCRIPTION,
              "event_start_date":"October 14 (Wed)","event_end_date":"October 14 (Wed)",
              "event_start_time":"03:00 PM","event_end_time":"03:30 PM",
              "event_reminder_1":"No reminder"}
        return [{**node_id(k),"text":v} for k,v in vals.items()] + [{**node_id("event_all_day"),"checked":False}]

    def test_every_visible_field_must_match_before_success(self):
        spec=preset(date(2026,10,14));verify_editor(self.editor(),spec)
        for i in range(len(self.editor())):
            nodes=self.editor();nodes[i]={**nodes[i],"text":"wrong","checked":True}
            with self.subTest(i=i),self.assertRaises(AgentError):verify_editor(nodes,spec)
        with self.assertRaises(AgentError):verify_editor(self.editor()+[self.editor()[0]],spec)

    def test_synced_or_repeating_calendar_is_rejected(self):
        nodes=[{**node_id(k),"text":v} for k,v in {
            "event_calendar_title":"Local calendar","event_calendar_subtitle":"Offline (never synced)",
            "event_repetition":"No repetition"}.items()]
        verify_local_calendar(nodes)
        nodes[1]["text"]="Google account"
        with self.assertRaises(AgentError):verify_local_calendar(nodes)

    def test_helper_rejects_owner_stale_and_bad_readback_identity(self):
        state={"displayId":7,"session":"demo"}
        for override in ({"ok":False},{"displayId":0},{"session":"stale"},{"package":"owner.app"},{"verified":False}):
            c=Mock();c.state.return_value=state;c.require_display.return_value=7
            c._run.return_value=json.dumps({"ok":True,"verified":True,"displayId":7,"session":"demo",
                                           "package":"org.fossify.calendar",**override})
            client=CalendarUiClient(c,"/private.jar")
            with self.subTest(override=override),self.assertRaises(AgentError):client.snapshot()

    def test_calendar_task_never_checks_model_api_or_spawns_agent(self):
        b=Bridge.__new__(Bridge);b.args=SimpleNamespace(provider="autoglm",runs_dir=Path("runs"))
        b.save_state=Mock();b.update=Mock()
        with patch("calendar_demo.run_calendar_demo",return_value="verified") as runner, \
             patch("chat_bridge.urllib.request.urlopen") as api,patch("chat_bridge.subprocess.Popen") as spawn:
            b.run_task({"id":"preset","goal":"calendar","kind":"calendar_demo","calendarSpec":preset()})
        runner.assert_called_once();api.assert_not_called();spawn.assert_not_called()
        self.assertEqual("completed",b.update.call_args.args[1])

    def test_pause_is_not_completed_and_regular_tasks_are_not_mocked(self):
        b=Bridge.__new__(Bridge);b.args=SimpleNamespace(provider="preset",runs_dir=Path("runs"))
        b.save_state=Mock();b.update=Mock()
        with patch("calendar_demo.run_calendar_demo",side_effect=AgentError("readback failed")):
            b.run_task({"id":"preset","goal":"calendar","kind":"calendar_demo","calendarSpec":preset()})
        self.assertEqual("needs_user",b.update.call_args.args[1])
        b.run_task({"id":"normal","goal":"打开京东"})
        self.assertEqual("failed",b.update.call_args.args[1])

if __name__=="__main__":unittest.main()

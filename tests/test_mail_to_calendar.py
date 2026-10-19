import sys
import unittest
from datetime import date, time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import itinerary
import skill_runner as runner
from app_targets import is_mail_to_calendar
from event_params import resolve
from message_params import mail_query

SUBJECT, BODY = "航班行程确认", "您的航班LX188将于10月25日8:30从苏黎世起飞，11:45到达巴黎。 获取 Outlook for iOS"
TODAY = date(2026, 10, 16)
GOOD = "...\nevent(title=\"LX188 苏黎世到巴黎\", date=\"2026-10-25\", start=\"08:30\", end=\"11:45\")"


class RoutingTest(unittest.TestCase):
    def test_mail_to_calendar_requests_and_queries(self):
        self.assertTrue(is_mail_to_calendar("把「航班行程确认」那封邮件里的行程加到日历"))
        self.assertFalse(is_mail_to_calendar("给 a@b.com 写封邮件，内容是好的"))
        self.assertEqual('in:anywhere "航班行程确认"', mail_query("把「航班行程确认」那封邮件里的行程加到日历").query)
        self.assertEqual('in:anywhere "航班行程确认"', mail_query("把航班行程确认那封邮件的行程加进日历").query)
        self.assertIn("in:anywhere newer_than:7d", mail_query("看看邮件里有没有航班，加到日历").query)


class VerifyTest(unittest.TestCase):
    def test_model_values_must_come_from_the_email(self):
        proposal = itinerary.verify(SUBJECT, BODY, GOOD, TODAY)
        self.assertEqual(("LX188 苏黎世→巴黎", date(2026, 10, 25), time(8, 30), time(11, 45)),
                         (proposal.title, proposal.day, proposal.start, proposal.end))
        for bad in (GOOD.replace("10-25", "10-26"), GOOD.replace("巴黎", "伦敦"), GOOD.replace("08:30", "09:30"),
                    GOOD.replace("11:45", "08:00"), "no event here"):
            with self.subTest(bad=bad):
                self.assertIsNone(itinerary.verify(SUBJECT, BODY, bad, TODAY))

    def test_reworded_title_is_fine_but_invented_words_are_not(self):
        reworded = GOOD.replace("LX188 苏黎世到巴黎", "LX188 8:30苏黎世起飞 11:45到达巴黎")
        self.assertEqual("LX188 苏黎世→巴黎", itinerary.verify(SUBJECT, BODY, reworded, TODAY).title)
        self.assertIsNone(itinerary.verify(SUBJECT, BODY, GOOD.replace("LX188", "LX999"), TODAY))

    def test_highlight_cut_subject_is_restored_from_the_row(self):
        described = ", , , Demo Sender, , 航班行程确认, " + BODY + ", ,  at 9:10 AM, labels: Spam, "
        for subject, snippet, row_text in (
                ("班行程确认", BODY, described),
                ("班行程确认", BODY[3:], described),  # the snippet is cut too
                ("\u200b班行程确认", "", described.replace("航班", "\u200b航\u200b班"))):  # invisible marks
            with self.subTest(snippet=snippet[:6]):
                row = {"_row": row_text, "subject": subject, "snippet": snippet}
                self.assertEqual(SUBJECT, runner.full_subject(row)["subject"])
        self.assertEqual("周会", runner.full_subject({"_row": ", 王, 周会, 好", "subject": "周会"})["subject"])

    def test_flight_titles_only_from_the_email(self):
        self.assertEqual("LX188 苏黎世→巴黎", itinerary.flight_title("LX188", BODY))
        self.assertEqual("CA931 北京首都→法兰克福",
                         itinerary.flight_title("CA931航班", "航班CA 931从北京首都国际机场出发，飞抵法兰克福。"))
        self.assertIsNone(itinerary.flight_title("产品周会", BODY))  # the title does not name the flight
        self.assertIsNone(itinerary.flight_title("LX188", "LX188 起飞时间 8:30"))  # no cities

    def test_confirmation_is_an_ordinary_calendar_request(self):
        request = itinerary.verify(SUBJECT, BODY, GOOD, TODAY).request()
        params = resolve(request, "", TODAY)
        self.assertEqual(("LX188 苏黎世→巴黎", date(2026, 10, 25), time(8, 30), time(11, 45)),
                         (params.title, params.day, params.start, params.end))
        # A model title that is only part of the quoted one never wins.
        self.assertEqual("LX188 苏黎世→巴黎", resolve(request, "event(title='LX188')", TODAY).title)


class FindEmailSkillTest(unittest.TestCase):
    def test_read_only_search_skill_returns_its_rows(self):
        from unittest.mock import Mock
        from message_params import MailQuery
        row = [{"id": "com.google.android.gm:id/open_search"}, {"id": "com.google.android.gm:id/open_search_view_edit_text"},
               {"id": "com.google.android.gm:id/viewified_conversation_item_view"},
               {"id": "com.google.android.gm:id/subject", "text": SUBJECT}]
        ui = Mock()
        ui.snapshot.return_value = row
        outputs = {}
        with patch.object(runner, "PrivateUiClient", return_value=ui), patch.object(runner.time, "sleep"):
            runner.run_skill(Mock(), runner.load_skill("gmail_find_email"), MailQuery('in:anywhere "x"'),
                             lambda _: None, outputs=outputs)
        self.assertEqual([{"subject": SUBJECT}], outputs["emails"])
        ui.call.assert_any_call("text", {"id": "com.google.android.gm:id/open_search_view_edit_text"}, 'in:anywhere "x"')


class ReadResultsTest(unittest.TestCase):
    ROW = [{"id": "com.google.android.gm:id/viewified_conversation_item_view"},
           {"id": "com.google.android.gm:id/senders", "text": "Demo Sender"},
           {"id": "com.google.android.gm:id/subject", "text": SUBJECT},
           {"id": "com.google.android.gm:id/snippet", "text": BODY}]

    def read(self, snapshots):
        class Ui:
            def snapshot(self_inner):
                return snapshots.pop(0)
        with patch.object(runner.time, "sleep"):
            return runner.read_results(Ui(), 25)

    def test_rows_are_read_once_the_list_is_settled(self):
        loading = [{"class": "android.widget.ProgressBar"}]
        rows = self.read([loading, self.ROW, self.ROW])
        self.assertEqual([{"senders": "Demo Sender", "subject": SUBJECT, "snippet": BODY}], rows)

    def test_cut_subject_is_restored_from_the_rows_text_as_on_the_device(self):
        # Shape read from the emulator: the full row text sits in "text", the subject view is cut.
        row = [{"id": "com.google.android.gm:id/viewified_conversation_item_view",
                "text": ", , , Demo Sender, , 航班行程确认, " + BODY + ", ,  at 9:10\u202fAM, labels: Spam, "},
               {"id": "com.google.android.gm:id/subject", "text": "班行程确认"}]
        self.assertEqual([{"subject": SUBJECT}], self.read([row, row]))

    def test_no_connection_is_reported_not_treated_as_no_mail(self):
        with self.assertRaises(runner.SkillFallback):
            self.read([[{"text": "No Connection"}]])


if __name__ == "__main__":
    unittest.main()

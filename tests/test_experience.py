import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import experience as ex

CAL = "com.google.android.calendar"
G = CAL + ":id/"


def node(ident=None, text=None, desc=None, bounds="[0,0][10,10]", clickable=True, cls="android.widget.Button"):
    return {"id": G + ident if ident else None, "text": text, "description": desc, "bounds": bounds,
            "clickable": clickable, "class": cls}


HOME = [node("floating_action_button", desc="Create new event", bounds="[600,1100][700,1200]"),
        node("toolbar", text="October", clickable=False, cls="android.widget.TextView")]
MENU = [node(text="Event", bounds="[500,900][700,960]"), node(text="Task", bounds="[500,980][700,1040]")]
FORM = [node("title", text="", bounds="[0,200][720,260]", cls="android.widget.EditText"),
        node(desc="Start date: Fri, Oct 16, 2026", bounds="[0,300][360,360]"),
        node("save", text="Save", bounds="[600,40][700,100]")]
TIME = [node(text="10", bounds="[300,500][340,540]", cls="android.widget.TextView"),
        node(text="OK", bounds="[600,1000][700,1060]")]


def tap_on(nodes, index):
    b = ex.bounds(nodes[index])
    return {"action": "tap", "arguments": {"x": (b[0] + b[2]) // 2, "y": (b[1] + b[3]) // 2}}


class ExperienceTest(unittest.TestCase):
    GOAL = "帮我在日历里加一个10月27日下午3点到4点的日程，叫「经验测试A」"

    def recorded(self):
        rec = ex.Recorder(self.GOAL, "run-1")
        rec.add(CAL, HOME, tap_on(HOME, 0), "model")                       # + (structural)
        rec.add(CAL, MENU, tap_on(MENU, 1), "model")                       # Task: a detour...
        rec.add(CAL, HOME, tap_on(HOME, 0), "model")                       # ...back to + again
        rec.add(CAL, MENU, tap_on(MENU, 0), "model")                       # Event (structural)
        rec.add(CAL, FORM, tap_on(FORM, 0), "model")                       # focus the title field (structural)
        rec.add(CAL, FORM, {"action": "input_text", "arguments": {"text": "经验测试A"}}, "model")
        rec.add(CAL, TIME, tap_on(TIME, 0), "model")                       # hour "10" (value)
        rec.add(CAL, TIME, tap_on(TIME, 1), "model")                       # OK (commit)
        rec.add(CAL, FORM, tap_on(FORM, 2), "model")                       # Save (commit)
        rec.add(CAL, FORM, {"action": "wait", "arguments": {"seconds": 1}}, "model")  # not recorded
        return rec

    def test_learns_from_a_finished_run_and_erases_the_detour(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.recorded().learn(Path(tmp))
            record = __import__("json").loads(path.read_text())
        kinds = [(s["action"], ex.target_label(s.get("target")), s["kind"]) for s in record["steps"]]
        self.assertEqual([("tap", "Create new event", "structural"), ("tap", "Event", "structural"),
                          ("tap", "", "structural"), ("input_text", "", "value"), ("tap", "10", "value"),
                          ("tap", "OK", "commit"), ("tap", "Save", "commit")], kinds)
        self.assertNotIn("Task", [k[1] for k in kinds])  # the detour is gone
        self.assertEqual("run-1", record["learned_from"])

    def test_replay_predicts_only_structural_steps_on_matching_screens(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.recorded().learn(Path(tmp))
            record = ex.find("帮我在日历里加一个10月28日上午10点到11点的日程，叫「经验测试B」", Path(tmp))
            self.assertIsNotNone(record)  # same kind of request, different values
            replay = ex.Replay(record)
            # The + button moved: it is found by identity, not by the learned coordinates.
            moved = [node("floating_action_button", desc="Create new event", bounds="[100,1100][200,1200]"), HOME[1]]
            action, what = replay.predict(CAL, moved)
            self.assertEqual(({"x": 150, "y": 1150}, "Create new event"), (action["arguments"], what))
            self.assertEqual("tap", replay.predict(CAL, MENU)[0]["action"])
            self.assertEqual(150 + 80, replay.predict(CAL, FORM)[0]["arguments"]["y"])  # focus the title field
            self.assertIsNone(replay.predict(CAL, FORM))   # what to type belongs to this request: model
            self.assertTrue(replay.hint.startswith("type this request's own value into the focused field"))
            self.assertIsNone(replay.predict(CAL, TIME))   # an hour: model
            self.assertIn('tap "10"', replay.hint)         # only a hint: the model decides
            self.assertIsNone(replay.predict(CAL, [node(text="Allow notifications?"), node(text="Allow")]))  # pop-up
            replay.finish()
            self.assertEqual(1, __import__("json").loads(Path(record["path"]).read_text())["replays"])

    def test_other_requests_and_unreadable_runs_teach_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.recorded().learn(Path(tmp))
            self.assertIsNone(ex.find("给5550100发短信，内容是明天见", Path(tmp)))
            rec = ex.Recorder("查机票", "run-2")
            rec.add(CAL, HOME, tap_on(HOME, 0), "model")
            rec.add("com.example.unreadable", None, {"action": "tap", "arguments": {"x": 1, "y": 1}}, "model")
            self.assertIsNone(rec.learn(Path(tmp)))

    def test_values_from_the_request_are_never_structural(self):
        goal = "查10月25日苏黎世到巴黎的单程机票"
        for text in ("巴黎", "10月25日周日", "单程", "PM", "35"):
            with self.subTest(text=text):
                step = {"action": "tap", "target": {"text": text, "class": "x"}}
                self.assertEqual("value", ex.classify(step, goal))
        self.assertEqual("commit", ex.classify({"action": "tap", "target": {"text": "搜索", "class": "x"}}, goal))
        self.assertEqual("structural", ex.classify({"action": "tap", "target": {"text": "航班", "class": "x"}}, goal))


if __name__ == "__main__":
    unittest.main()


class VolatileLabelTest(unittest.TestCase):
    def test_date_and_time_rows_are_found_on_another_day(self):
        from experience import locate
        learned = {"class": "android.widget.TextView", "text": "5:30 AM", "description": "Start time: 5:30 AM",
                   "bounds": "[568,462][688,558]"}
        today = [{"class": "android.widget.TextView", "text": "2:00 PM", "description": "Start time: 2:00 PM",
                  "bounds": "[568,462][688,558]"},
                 {"class": "android.widget.TextView", "text": "3:00 PM", "description": "End time: 3:00 PM",
                  "bounds": "[568,558][688,654]"}]
        self.assertEqual("Start time: 2:00 PM", locate(today, learned)["description"])
        fixed = {"class": "android.widget.Button", "text": "OK", "bounds": "[544,768][672,864]"}
        self.assertIsNone(locate([{"class": "android.widget.Button", "text": "Cancel", "bounds": "[1,1][2,2]"}], fixed))


class TaskKindTest(unittest.TestCase):
    def store(self, directory, name, goal):
        import json
        (directory / f"exp-{name}.json").write_text(json.dumps(
            {"goal": goal, "goal_key": ex.goal_key(goal), "steps": []}, ensure_ascii=False))

    def test_a_differently_worded_request_of_the_same_kind_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.store(directory, "1", "帮我在日历里建一个10月28日下午3点到4点的会议，叫教师示范")
            self.store(directory, "2", "早上9点25分叫我起床，帮我定个闹钟")
            found = ex.find("帮我在日历里加一个10月20日下午3点到4点的需求评审会，地点三楼大会议室", directory)
            self.assertEqual("calendar.create", ex.task_kind(found["goal"]))
            self.assertIsNone(ex.find("用12306查10月20日上海到北京的高铁", directory))

    def test_another_kind_is_never_replayed(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.store(directory, "1", "帮我在日历里建一个10月28日下午3点到4点的会议，叫教师示范")
            self.assertIsNone(ex.find("帮我定个早上7点的闹钟", directory))


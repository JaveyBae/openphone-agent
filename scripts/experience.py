"""Experience learned from the model's own successful runs (nothing hand-written).

Recording (every model-driven run): before each action the private app's UI tree
is read; the action is stored with the screen's *fingerprint* (ids and short
labels on screen) and, for a tap, the *identity* of the control under the point.

Learning (only when the model finished the task): loops are erased (a screen the
run came back to removes the detour) and steps are classified:
  * structural - navigation the task type always needs ("+", "Event", a tab, Back);
  * value      - depends on this request (typed text, a date/time/number, a label
                 taken from the request, AM/PM, one way/round trip, a URL);
  * commit     - saves or submits (Save, OK, Done, Send, Search ...).

Replay ("experience mode"): at each step the live screen is compared with the
learned states. Only a *structural* step whose state matches and whose control is
found on the live screen is executed without asking the model. Value and commit
steps, unknown screens and pop-ups always go to the model, which also decides
when the task is finished. Every executed action still passes the same guards.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPERIENCE_DIR = ROOT / "experiences"  # local, like runs/: may contain on-screen labels

# Apps whose UI tree the private reader may read (see tools/workspace PrivateUi.ALLOWED).
READABLE = {"com.google.android.calendar", "org.fossify.calendar", "com.google.android.gm",
            "com.google.android.apps.messaging", "ai.openphone.agent", "com.google.android.deskclock",
            "com.google.android.apps.maps", "com.google.android.contacts"}
CHOICES = {"am", "pm", "上午", "下午", "单程", "往返", "one way", "round trip", "one-way", "round-trip"}
COMMIT = re.compile(r"^(save|ok|done|send|search|submit|confirm|apply|保存|完成|确定|确认|发送|搜索|提交|好)$", re.I)
SAME_STATE = 0.9   # fingerprints this similar are "the same screen" (loop erasure)
MATCH_STATE = 0.6  # replay: the live screen must be at least this similar to the learned one


def bounds(node: dict) -> tuple[int, int, int, int] | None:
    match = re.match(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", str(node.get("bounds") or ""))
    return tuple(map(int, match.groups())) if match else None  # type: ignore[return-value]


def label(node: dict) -> str:
    return str(node.get("text") or node.get("description") or "").strip()


def fingerprint(package: str, nodes: list[dict]) -> list[str]:
    """Stable tokens of a screen: control ids and short labels (long texts are content)."""
    tokens = {"pkg:" + package}
    for node in nodes:
        ident = str(node.get("id") or "").split(":id/")[-1]
        if ident:
            tokens.add("id:" + ident)
        text = label(node)
        if text and len(text) <= 24 and not re.fullmatch(r"[\d\s:.,/\-–]+", text):
            tokens.add("t:" + text)
    return sorted(tokens)


def similarity(a: list[str] | set[str], b: list[str] | set[str]) -> float:
    a, b = set(a), set(b)
    return len(a & b) / len(a | b) if a | b else 1.0


def identity(node: dict) -> dict:
    """How to find the same control again: id, label and class, never coordinates."""
    found = {"class": node.get("class")}
    if node.get("id"):
        found["id"] = node["id"]
    if node.get("text"):
        found["text"] = node["text"]
    if node.get("description"):
        found["description"] = node["description"]
    return found


def target_at(nodes: list[dict], x: int, y: int) -> dict | None:
    """The smallest clickable control under (x, y), else the smallest labelled one."""
    def area(node):
        box = bounds(node)
        return (box[2] - box[0]) * (box[3] - box[1])
    under = [n for n in nodes if (b := bounds(n)) and b[0] <= x <= b[2] and b[1] <= y <= b[3]]
    clickable = [n for n in under if n.get("clickable")]
    for group in (clickable, [n for n in under if label(n) or n.get("id")]):
        if group:
            node = min(group, key=area)
            if not label(node) and not node.get("id"):
                # A bare container: borrow the label of its labelled child under the point.
                inner = [n for n in under if label(n) and area(n) <= area(node)]
                if inner:
                    return {**identity(node), "child_label": label(min(inner, key=area)), "bounds": node.get("bounds")}
            return {**identity(node), "bounds": node.get("bounds")}
    return None


def target_label(target: dict | None) -> str:
    if not target:
        return ""
    return str(target.get("text") or target.get("description") or target.get("child_label") or "").strip()


def classify(step: dict, goal: str) -> str:
    action = step["action"]
    if action in {"input_text", "open_url"}:
        return "value"
    if action in {"launch_app", "key", "swipe"}:
        return "structural"
    if action != "tap":
        return "value"
    text = target_label(step.get("target"))
    if not step.get("target"):
        return "value"  # no identity: cannot be found again safely
    if COMMIT.match(text):
        return "commit"
    if not text and not step["target"].get("id"):
        return "value"
    lowered = text.lower()
    if lowered in CHOICES or re.fullmatch(r"[\d\s:.,/\-–]+", text):
        return "value"
    # A label taken from the request (a city, a title) or carrying its numbers (a date, a time).
    # Latin labels must be a whole word of the request ("Phone" is not taken from "OpenPhone").
    in_goal = (re.search(r"(?<![A-Za-z])" + re.escape(text) + r"(?![A-Za-z])", goal, re.I) if text.isascii()
               else text in goal) if text else False
    if in_goal or set(re.findall(r"\d+", text)) & set(re.findall(r"\d+", goal)):
        return "value"
    return "structural"


def erase_loops(steps: list[dict]) -> list[dict]:
    """When a run comes back to a screen it saw before, the steps in between were a detour."""
    def same_move(a: dict, b: dict) -> bool:
        if a["action"] == "input_text":
            return False  # typed values differ from step to step: never a detour
        return (a["package"] == b["package"] and a["action"] == b["action"]
                and similarity(a["fp"], b["fp"]) >= SAME_STATE
                and (target_label(a.get("target")), (a.get("target") or {}).get("id"), a.get("args"))
                == (target_label(b.get("target")), (b.get("target") or {}).get("id"), b.get("args")))
    kept: list[dict] = []
    for step in steps:
        # The same move on the same screen again: everything since the first time was a detour.
        for index, earlier in enumerate(kept):
            if same_move(earlier, step):
                del kept[index:]
                break
        kept.append(step)
    return kept


def goal_key(goal: str) -> str:
    """The request without its values: what kind of task it is."""
    key = re.sub(r"「[^「」]*」|\d+|[一二两三四五六七八九十半]+点|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+", " ", goal)
    return re.sub(r"\s+", "", key)


# Kinds of task whose path through the app does not depend on how the request is worded:
# a matching kind beats wording similarity, a different kind is never replayed.
TASK_KINDS = (
    ("calendar.create", re.compile(r"(日历|日程|calendar).*(建|加|添加|新增|安排|创建|记|create|add)"
                                   r"|(建|加|添加|新增|安排|创建|记|create|add).*(日历|日程|会议|会|meeting|event)", re.I)),
    ("alarm.create", re.compile(r"闹钟|叫我起床|alarm|点.{0,6}叫(醒)?我", re.I)),
    ("contact.create", re.compile(r"(通讯录|联系人|contact).*(新建|添加|加|建|保存|create|add)"
                                  r"|(新建|添加|create|add).*(联系人|contact)", re.I)),
)


def task_kind(goal: str) -> str | None:
    """The kind of task a request is (see TASK_KINDS), None when it is not one of them."""
    return next((kind for kind, pattern in TASK_KINDS if pattern.search(goal)), None)


def bigrams(text: str) -> set[str]:
    return {text[i:i + 2] for i in range(len(text) - 1)} or {text}


class Recorder:
    """Collects (screen, action) pairs during a run; learn() keeps only a successful run."""

    def __init__(self, goal: str, run_id: str):
        self.goal, self.run_id, self.steps = goal, run_id, []

    def add(self, package: str | None, nodes: list[dict] | None, action: dict, source: str) -> None:
        name = action.get("action")
        if name in {"wait", "finish", "request_user"} or not package or nodes is None:
            if name not in {"wait", "finish", "request_user"}:
                self.steps.append({"package": package, "fp": [], "action": name, "unreadable": True})
            return
        args = action.get("arguments", {})
        step = {"package": package, "fp": fingerprint(package, nodes), "action": name, "source": source}
        if name == "tap":
            step["target"] = target_at(nodes, int(args.get("x", -1)), int(args.get("y", -1)))
        elif name in {"launch_app", "key", "swipe"}:
            step["args"] = args
        self.steps.append(step)

    def save(self, run_dir: Path) -> None:
        """Keep the raw recording with the run, so experience can be re-learned later."""
        (run_dir / "recording.json").write_text(json.dumps({"goal": self.goal, "run_id": self.run_id,
                                                            "steps": self.steps}, ensure_ascii=False) + "\n",
                                                encoding="utf-8")

    @classmethod
    def load(cls, run_dir: Path) -> "Recorder":
        data = json.loads((run_dir / "recording.json").read_text())
        recorder = cls(data["goal"], data["run_id"])
        recorder.steps = data["steps"]
        return recorder

    def learn(self, directory: Path = EXPERIENCE_DIR) -> Path | None:
        """Store the run as experience; None if it taught nothing reusable."""
        steps = [s for s in self.steps if not s.get("unreadable")]
        if len(steps) != len(self.steps) or not steps:
            return None  # an unreadable step means the path cannot be replayed faithfully
        steps = erase_loops(steps)
        for step in steps:
            step["kind"] = classify(step, self.goal)
        if not any(s["kind"] == "structural" for s in steps):
            return None
        directory.mkdir(parents=True, exist_ok=True)
        key = goal_key(self.goal)
        existing = [p for p in directory.glob("*.json")
                    if similarity(bigrams(json.loads(p.read_text())["goal_key"]), bigrams(key)) >= 0.8]
        path = existing[0] if existing else directory / f"exp-{int(time.time())}.json"
        previous = json.loads(path.read_text()) if path.exists() else {}
        record = {"goal": self.goal, "goal_key": key, "learned_from": self.run_id, "learned_at": int(time.time()),
                  "replays": previous.get("replays", 0), "steps": steps}
        path.write_text(json.dumps(record, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        return path


def find(goal: str, directory: Path = EXPERIENCE_DIR, threshold: float = 0.45) -> dict | None:
    """The learned experience for the same kind of request, if any.

    Requests of a known kind (task_kind) match experience of that kind however they are worded;
    other requests match by the wording of their goal_key.
    """
    key, kind, best, best_score = goal_key(goal), task_kind(goal), None, threshold
    for path in sorted(directory.glob("*.json")) if directory.exists() else []:
        record = json.loads(path.read_text())
        learned_kind = task_kind(record["goal"])
        if kind and learned_kind and kind != learned_kind:
            continue
        score = similarity(bigrams(record["goal_key"]), bigrams(key)) + (1 if kind and kind == learned_kind else 0)
        if score >= best_score:
            best, best_score = {**record, "path": str(path)}, score
    return best


LABELLED_VALUE = re.compile(r"^([^:：\d]{2,40})[:：]\s*\S")


def same_label(key: str, live: str | None, learned: str, target: dict) -> bool:
    """Labels that carry today's date or time ("Start date: Mon, Oct 19") match by their stable part.

    A description "Name: value" with digits matches on "Name:"; the control's text is then that
    changing value itself (e.g. "5:30 AM") and is not compared.
    """
    labelled = LABELLED_VALUE.match(target.get("description") or "")
    volatile = labelled and re.search(r"\d", target.get("description") or "")
    if volatile and key == "description":
        return (live or "").startswith(labelled.group(1)) and (live or "")[len(labelled.group(1)):][:1] in ":："
    if volatile and key == "text":
        return True
    return live == learned


def locate(nodes: list[dict], target: dict) -> dict | None:
    """The live control with the learned identity; the closest one if several match."""
    keys = [k for k in ("id", "text", "description") if target.get(k)]
    if not keys and not target.get("child_label"):
        return None
    matches = [n for n in nodes if all(same_label(k, n.get(k), target[k], target) for k in keys)
               and n.get("class") == target.get("class")]
    if target.get("child_label") and not keys:
        matches = []
        for node in nodes:
            box = bounds(node)
            if node.get("clickable") and box and any(
                    label(child) == target["child_label"] and (cb := bounds(child)) and box[0] <= cb[0] and cb[2] <= box[2]
                    and box[1] <= cb[1] and cb[3] <= box[3] for child in nodes):
                matches.append(node)
    learned = bounds({"bounds": target.get("bounds")})
    if not matches:
        return None
    if learned is None or len(matches) == 1:
        return matches[0] if len(matches) == 1 else None
    cx, cy = (learned[0] + learned[2]) / 2, (learned[1] + learned[3]) / 2
    return min(matches, key=lambda n: abs((bounds(n)[0] + bounds(n)[2]) / 2 - cx) + abs((bounds(n)[1] + bounds(n)[3]) / 2 - cy))


class Replay:
    """Predicts structural steps of a learned experience on matching live screens."""

    def __init__(self, record: dict):
        self.record, self.position, self.hits, self.misses = record, 0, 0, 0
        self.hint: str | None = None  # what the successful run did here, when the model must decide

    def predict(self, package: str | None, nodes: list[dict] | None) -> tuple[dict, str] | None:
        """(action, description) to execute without the model, or None to ask the model."""
        if not package or nodes is None:
            return None
        live = fingerprint(package, nodes)
        steps = self.record["steps"]
        best, best_score = None, MATCH_STATE
        for index in range(self.position, len(steps)):
            step = steps[index]
            if step["package"] != package:
                continue
            score = similarity(step["fp"], live)
            # On a tie (the same form for several steps) the earliest one is the next step.
            if score > best_score or (best is None and score >= MATCH_STATE):
                best, best_score = index, score
        if best is None:
            self.misses += 1
            return None
        step = steps[best]
        self.position = best + 1
        self.hint = None
        if step["kind"] != "structural":
            # The model decides; it is only told what the successful run did on this screen.
            if step["action"] == "input_text":
                field = target_label(steps[best - 1].get("target")) if best > 0 else ""
                self.hint = ("type this request's own value into the focused field"
                             + (f' "{field}" (the value the request gives for {field})' if field else ""))
            elif step["action"] == "tap" and target_label(step.get("target")):
                self.hint = f'tap "{target_label(step["target"])}"' + (
                    " (this request's value may differ)" if step["kind"] == "value" else "")
            return None  # this state needs a decision about the request itself: the model's job
        if step["action"] == "tap":
            node = locate(nodes, step["target"])
            box = bounds(node) if node else None
            if not box:
                self.misses += 1
                self.position = best  # not found (yet): stay on this step, the screen may still be loading
                return None
            self.hits += 1
            return ({"action": "tap", "arguments": {"x": (box[0] + box[2]) // 2, "y": (box[1] + box[3]) // 2},
                     "reason": "experience: " + target_label(step["target"]), "risk": "none", "source": "experience"},
                    target_label(step["target"]) or "control")
        self.hits += 1
        return ({"action": step["action"], "arguments": dict(step.get("args") or {}),
                 "reason": "experience: " + step["action"], "risk": "none", "source": "experience"}, step["action"])

    def finish(self) -> None:
        path = Path(self.record["path"])
        record = json.loads(path.read_text())
        record["replays"] = record.get("replays", 0) + 1
        record["last_hits"], record["last_misses"] = self.hits, self.misses
        path.write_text(json.dumps(record, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    # Re-learn from saved runs: python3 scripts/experience.py runs/<id> [...]
    import sys
    for run in sys.argv[1:]:
        print(run, "->", Recorder.load(Path(run)).learn())

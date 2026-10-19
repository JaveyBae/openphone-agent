"""A general vision-language model (Qwen3.8-27B via an OpenAI-compatible API) as the planner.

Same contract as AutoGLMPlanner.next_action: one private-display screenshot in,
one validated action out (tap/type/swipe/launch/back/enter/wait/finish/request_user),
in display pixels. Optionally the UI element list read from the private app is
added, so the model can name an element instead of estimating coordinates.
"""
from __future__ import annotations

import base64
from datetime import datetime
import json
import re
import time
import urllib.request
from pathlib import Path

from app_notes import APP_NOTES
from app_targets import APP_PACKAGES

PROMPT = """You operate an Android phone app for the owner, on a separate private screen.
Each turn you get the task, the actions already taken and the current screenshot.
Choose exactly ONE next action. Reply with one short sentence of reasoning, then a JSON object:
{"action": "tap", "x": 0-1000, "y": 0-1000}            coordinates relative to the screenshot (0-1000)
{"action": "type", "text": "..."}                        replaces the text of the focused field
{"action": "swipe", "direction": "up" | "down"}          up = scroll content further down the page
{"action": "launch", "app": "Contacts" | "Clock" | "Calendar" | "Gmail" | "Messages" | "Maps" | "12306" | "Chrome"}
{"action": "back"}   {"action": "enter"} (the Enter key)   {"action": "wait"}
{"action": "finish", "message": "result for the owner"}  only when the requested result is visible
{"action": "take_over", "message": "why the owner must act"}  payment, login, consent, permissions
Rules: no on-screen keyboard ever appears; after tapping a text field, type directly. Fill every
requested field before Save. Never send messages or pay. Answer in the language of the task.

How the apps work (Type = the "type" action, Take_over = "take_over", Launch = "launch"):
""" + APP_NOTES


def encode(path: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def element_list(nodes: list[dict], width: int, height: int) -> tuple[str, dict[int, tuple[int, int]]]:
    """Numbered clickable/editable elements with labels, and their centres in display pixels."""
    lines, centres = [], {}
    for node in nodes:
        box = re.findall(r"-?\d+", str(node.get("bounds") or ""))
        label = str(node.get("text") or node.get("description") or "").strip()
        if len(box) != 4 or not (node.get("clickable") or node.get("editable")) or not label:
            continue
        x1, y1, x2, y2 = map(int, box)
        index = len(centres) + 1
        centres[index] = ((x1 + x2) // 2, (y1 + y2) // 2)
        kind = "field" if node.get("editable") else "button"
        lines.append(f"[{index}] {kind} \"{label[:40]}\" at ({centres[index][0] * 1000 // width},"
                     f"{centres[index][1] * 1000 // height})")
    return "\n".join(lines[:60]), centres


def user_text(goal: str, history: list[dict], width: int, height: int) -> str:
    """The per-turn text the model sees; shared with the teacher dataset so training matches use."""
    taken = "\n".join(f"{i + 1}. {describe(h['action'], width, height)}"
                      for i, h in enumerate(history) if isinstance(h.get("action"), dict)) or "(none)"
    text = f"Task: {goal}\nActions already taken:\n{taken}"
    if history and history[-1].get("private_input_focused") is True:
        field = history[-1].get("focused_field")
        # A soft "type if it is the right field" left the 27B model tapping Gmail's Subject again and
        # again; naming the field and the next action works.
        text += (f"\nFeedback: your last tap focused the {repr(field) + ' ' if field else ''}text field (read from "
                 "the app). No keyboard or cursor ever appears on this screen, so it looks unchanged. Do NOT tap "
                 "it again: if it is the right field, your next action is type with its text.")
    elif history and history[-1].get("tapped_text_field") is True:
        text += ("\nFeedback: your last tap hit a text field. No keyboard or cursor ever appears on this "
                 "screen, so it looks unchanged: do not tap it again; type into it now if it is the right field.")
    return text


def to_json_action(action: dict, width: int, height: int) -> dict | None:
    """Our executed action in the model's JSON form (inverse of QwenPlanner.parse)."""
    a, args = action.get("action"), action.get("arguments", {})
    if a == "tap":
        return {"action": "tap", "x": args["x"] * 1000 // width, "y": args["y"] * 1000 // height}
    if a == "input_text":
        return {"action": "type", "text": str(args.get("text", ""))}
    if a == "swipe":
        return {"action": "swipe", "direction": "up" if args.get("y2", 0) < args.get("y1", 0) else "down"}
    if a == "launch_app":
        names = {APP_PACKAGES[name.lower()]: name for name in ("Contacts", "Clock", "Calendar", "Maps")}
        return {"action": "launch", "app": names.get(args.get("package"), args.get("package"))}
    if a == "open_url":
        return {"action": "launch", "app": "Chrome"}
    if a == "key" and args.get("keycode") == 4:
        return {"action": "back"}
    if a == "key" and args.get("keycode") == 66:
        return {"action": "enter"}
    if a == "finish":
        return {"action": "finish", "message": str(args.get("summary", ""))}
    if a == "request_user":
        return {"action": "take_over", "message": str(args.get("message", ""))}
    return None


class QwenPlanner:
    def __init__(self, base_url: str, model: str, api_key: str, timeout: int, width: int, height: int,
                 use_elements: bool = False):
        self.endpoint = base_url.rstrip("/") + "/chat/completions"
        self.model, self.api_key, self.timeout = model, api_key, timeout
        self.width, self.height, self.use_elements = width, height, use_elements
        self.last_timing: dict = {}
        self.experience_hint: str | None = None

    def next_action(self, goal, observation, history, include_image):
        today = datetime.now()
        # Without the date the model guesses what "明天" is (it once picked the 21st on the 19th).
        text = (f"Today is {today:%Y-%m-%d} (星期{'一二三四五六日'[today.weekday()]}).\n"
                + user_text(goal, history, self.width, self.height))
        if getattr(self, "experience_hint", None):
            # Set by the session when a learned run matches this screen but the step is the model's to take.
            text += ("\nExperience: on this same screen, a previous successful run of this kind of task did: "
                     + self.experience_hint + ". Decide yourself.")
        if getattr(self, "screen_note", None):
            text += "\nScreen facts (read from the app): " + self.screen_note
        centres: dict[int, tuple[int, int]] = {}
        nodes = getattr(observation, "nodes", None)
        if self.use_elements and nodes:
            listing, centres = element_list(nodes, self.width, self.height)
            text += ("\nElements on screen (you may tap one by number: {\"action\": \"tap\", \"element\": N}):\n"
                     + listing)
        messages = [{"role": "system", "content": PROMPT},
                    {"role": "user", "content": [{"type": "text", "text": text},
                                                 {"type": "image_url", "image_url": {"url": encode(Path(observation.path))}}]}]
        answer = self.ask(messages, observation)
        try:
            return self.parse(answer, centres)
        except ValueError as error:
            # One correction turn: a malformed reply (e.g. an element number without an element
            # list) should not end the task.
            messages += [{"role": "assistant", "content": answer},
                         {"role": "user", "content": f"That reply cannot be executed ({error}). Reply again: one "
                                                     "short sentence and ONE JSON action from the list; a tap "
                                                     "needs \"x\" and \"y\" (0-1000)."}]
            return self.parse(self.ask(messages, observation, retry=True), centres)

    def ask(self, messages: list[dict], observation, retry: bool = False) -> str:
        payload = {"model": self.model, "temperature": 0, "max_tokens": 400, "messages": messages,
                   # Qwen3.x thinks before answering by default; one step needs no long deliberation.
                   "chat_template_kwargs": {"enable_thinking": False}}
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        started = time.perf_counter()
        request = urllib.request.Request(self.endpoint, data=json.dumps(payload).encode(), headers=headers)
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            body = json.load(response)
        answer = body["choices"][0]["message"]["content"] or ""
        timing = {"http_s": round(time.perf_counter() - started, 3), "usage": body.get("usage")}
        log = Path(observation.path).with_suffix(".qwen.json")
        if retry:
            first = json.loads(log.read_text()) if log.exists() else {}
            first["retry"] = {"raw_output": answer, "timing": timing}
            log.write_text(json.dumps(first, ensure_ascii=False, indent=1))
            self.last_timing = {**self.last_timing, "retry_http_s": timing["http_s"]}
        else:
            self.last_timing = timing
            log.write_text(json.dumps({"raw_output": answer, "timing": timing}, ensure_ascii=False, indent=1))
        return answer

    def parse(self, answer: str, centres: dict[int, tuple[int, int]]) -> dict:
        try:
            return self.parse_action(answer, centres)
        except (TypeError, IndexError, KeyError) as error:  # a malformed value is a model error, not a crash
            raise ValueError(f"malformed action: {error}") from error

    def parse_action(self, answer: str, centres: dict[int, tuple[int, int]]) -> dict:
        objects = re.findall(r"\{[^{}]*\}", answer)
        if not objects:
            raise ValueError("no JSON action in the reply")
        data = json.loads(objects[-1])
        reason = answer[:answer.rfind(objects[-1])].strip()[-500:]
        kind = str(data.get("action", "")).lower()
        result = lambda name, args: {"action": name, "arguments": args, "reason": reason, "risk": "none",  # noqa: E731
                                     "native_action": objects[-1]}
        if kind == "tap":
            if "element" in data and int(data["element"]) in centres:
                x, y = centres[int(data["element"])]
            else:
                point = data["x"] if isinstance(data.get("x"), list) else [data["x"], data["y"]]
                if len(point) != 2:  # also accepts {"x": [x, y]}, which the model sometimes writes
                    raise ValueError("tap needs x and y")
                x = int(float(point[0]) * self.width / 1000)
                y = int(float(point[1]) * self.height / 1000)
            return result("tap", {"x": max(0, min(self.width - 1, x)), "y": max(0, min(self.height - 1, y))})
        if kind == "type":
            if str(data["text"]) in ("\n", "\r\n", "\r"):  # a bare line break means the Enter key
                return result("key", {"keycode": 66})
            return result("input_text", {"text": str(data["text"])})
        if kind == "swipe":
            up = str(data.get("direction", "up")).lower() == "up"
            x = self.width // 2
            return result("swipe", {"x1": x, "y1": int(self.height * (0.78 if up else 0.34)), "x2": x,
                                    "y2": int(self.height * (0.34 if up else 0.78)), "duration_ms": 500})
        if kind == "launch":
            if str(data.get("app", "")).lower() in {"chrome", "browser", "浏览器"}:
                return result("open_url", {"url": "https://www.wikipedia.org/"})
            package = APP_PACKAGES.get(str(data.get("app", "")).lower())
            if not package:
                return result("request_user", {"message": f"尚未适配应用：{data.get('app')}",
                                                "unavailable_app": str(data.get("app"))})
            return result("launch_app", {"package": package})
        if kind == "back":
            return result("key", {"keycode": 4})
        if kind == "enter":
            return result("key", {"keycode": 66})
        if kind == "wait":
            return result("wait", {"seconds": 1})
        if kind == "finish":
            return result("finish", {"summary": str(data.get("message", "完成"))})
        if kind == "take_over":
            return result("request_user", {"message": str(data.get("message", "需要你处理"))})
        raise ValueError("unknown action " + kind)


def describe(action: dict, width: int, height: int) -> str:
    a, args = action.get("action"), action.get("arguments", {})
    if a == "tap":
        return f"tap at ({args['x'] * 1000 // width},{args['y'] * 1000 // height})"
    if a == "input_text":
        return f"type {args.get('text')!r}"
    if a == "swipe":
        return "swipe " + ("up" if args.get("y2", 0) < args.get("y1", 0) else "down")
    if a == "launch_app":
        return f"launch {args.get('package')}"
    if a == "key":
        return "enter" if args.get("keycode") == 66 else "back"
    return str(a)

#!/usr/bin/env python3
"""Live dual-display regression: owner keeps typing/scrolling on display 0 while
an APP-queued model task (the "建日程" tool) runs on the private display.

Requires one running `chat_bridge.py` with a real model provider. Owner-screen dumps and
screenshots stay local as evidence; they are never given to a model. Typed text
is ASCII injected with `input text` WITHOUT a display id, so it follows the real
top-focused display: if the agent ever stole focus, the readback would differ.
The chat draft is never submitted and is cleared at the end.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import re
import time
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree

from agent_common import AgentError
from android_device import AndroidController, input_focus, locate_adb
from workspace_runtime import ensure_running

APP = "ai.openphone.agent"
CALENDAR = "com.google.android.calendar"


def bounds_center(node: ElementTree.Element) -> tuple[int, int]:
    x1, y1, x2, y2 = map(int, re.findall(r"\d+", node.get("bounds", "")))
    return (x1 + x2) // 2, (y1 + y2) // 2


class Owner:
    def __init__(self, c: AndroidController):
        self.c = c

    def dump(self) -> ElementTree.Element:
        self.c._run("shell", "uiautomator", "dump", "--windows", "/sdcard/openphone-regression.xml")
        raw = str(self.c._run("shell", "cat", "/sdcard/openphone-regression.xml"))
        return ElementTree.fromstring(raw[raw.index("<"):])

    def nodes(self, display: int) -> list[ElementTree.Element]:
        for item in self.dump().iter("display"):
            if item.get("id") == str(display):
                return list(item.iter("node"))
        return []

    def find(self, display: int, **attrs) -> ElementTree.Element:
        found = [n for n in self.nodes(display) if all(n.get(k) == v for k, v in attrs.items())]
        if len(found) != 1:
            raise AgentError(f"owner control {attrs} found {len(found)} times on display {display}")
        return found[0]

    def tap(self, node: ElementTree.Element) -> None:
        x, y = bounds_center(node)
        self.c._run("shell", "input", "-d", "0", "tap", str(x), str(y))


def focus_windows(c: AndroidController) -> dict[str, str]:
    raw = str(c._run("shell", "dumpsys", "window", "displays"))
    result, current = {}, None
    for line in raw.splitlines():
        match = re.search(r"Display: mDisplayId=(\d+)", line)
        if match:
            current = match.group(1)
        match = re.search(r"mCurrentFocus=Window\{\S+ \S+ ([^ }]+)", line)
        if match and current is not None and current not in result:
            result[current] = match.group(1)
    return result


def app_task(c: AndroidController) -> tuple[str, str, str]:
    """(task id, status, message) as the APP itself recorded them."""
    raw = str(c._run("shell", "run-as", APP, "cat", "shared_prefs/agent_task_updates.xml"))
    def value(key: str) -> str:
        match = re.search(r'<string name="' + key + r'">([^<]*)</string>', raw)
        return match.group(1) if match else ""
    return value("task_id"), value("status"), value("message")


def clear_owner_draft(c: AndroidController) -> None:
    # Move to the end and delete; never press send/Enter.
    c._run("shell", "input", "-d", "0", "keyevent", "123")
    c._run("shell", "input", "-d", "0", "keyevent", *(["67"] * 160))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-dir", type=Path, default=Path("runs"))
    parser.add_argument("--timeout", type=float, default=480)
    args = parser.parse_args()
    with (args.runs_dir / "chat-bridge.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            raise SystemExit("no chat bridge is running; start `chat_bridge.py` first")
        except BlockingIOError:
            pass
    state = ensure_running()
    private = state["displayId"]
    c = AndroidController(locate_adb())
    c.require_package_off_owner_display(CALENDAR, private)
    owner = Owner(c)
    target = args.runs_dir / ("concurrency-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    target.mkdir(parents=True)
    evidence: dict = {"privateDisplay": private, "session": state["session"], "samples": []}

    c._run("shell", "am", "start", "--display", "0", "-n", APP + "/.MainActivity")
    time.sleep(1.5)
    # Locate the composer first so typing starts right after the task is queued.
    composer = bounds_center(owner.find(0, package=APP, **{"class": "android.widget.EditText"}))
    previous_task = app_task(c)[0]
    owner.tap(owner.find(0, **{"content-desc": "工具"}))
    time.sleep(0.8)
    chip = owner.find(0, **{"content-desc": "建日程"})
    started = time.time()
    owner.tap(chip)
    time.sleep(0.8)
    # A paused earlier task asks before the new task cleans the private screen.
    proceed = [n for n in owner.nodes(0) if n.get("text") == "继续新任务"]
    if proceed:
        owner.tap(proceed[0])
        evidence["confirmedCleanStart"] = True
    evidence["queuedAt"] = datetime.now().isoformat()
    time.sleep(0.4)
    c._run("shell", "input", "-d", "0", "tap", str(composer[0]), str(composer[1]))
    time.sleep(0.4)
    clear_owner_draft(c)

    typed, i, finished_at, running_at = "", 0, None, None
    while True:
        i += 1
        chunk = f"{i:03d}"  # digits only: no autocorrect or full-width punctuation
        c._run("shell", "input", "text", chunk)  # no -d: follows real top focus
        typed += chunk
        if i % 4 == 0:  # owner scrolls the chat history between keystrokes
            # Scroll the chat history well above the composer, never across the input box.
            c._run("shell", "input", "-d", "0", "swipe", "540", "500" if i % 8 else "900",
                   "540", "900" if i % 8 else "500", "250")
        top, windows = input_focus(str(c._run("shell", "dumpsys", "input")))
        evidence["samples"].append({"t": round(time.time() - started, 2), "chunk": chunk, "topDisplay": top,
                                    "focusedWindowDisplays": sorted(windows), "windows": focus_windows(c)})
        task_id, status, message = app_task(c)
        if task_id and task_id != previous_task:
            if running_at is None and status == "running":
                running_at = round(time.time() - started, 2)
            if finished_at is None and status in ("completed", "needs_user", "failed"):
                finished_at = i
                evidence.update({"taskId": task_id, "taskStatus": status, "taskMessage": message})
        if finished_at is not None and i >= finished_at + 2:
            break
        if time.time() - started > args.timeout:
            evidence["timeout"] = True
            break
        time.sleep(0.3)

    time.sleep(0.5)
    draft = owner.find(0, package=APP, **{"class": "android.widget.EditText"}).get("text")
    evidence.update({"typed": typed, "readback": draft, "chunks": i})
    if running_at is not None and evidence["samples"]:
        # Negative: the owner was already typing before the agent started acting.
        evidence["typingGapSeconds"] = round(evidence["samples"][0]["t"] - running_at, 2)
    c.capture(target / "private-final.png")
    # Local evidence only (default display); never a planner input.
    (target / "owner-final.png").write_bytes(c._run("exec-out", "screencap", "-p", binary=True))
    checks = {
        "owner_text_exact": draft == typed,
        "top_focus_always_owner": all(s["topDisplay"] == 0 for s in evidence["samples"]),
        "owner_window_always_app": all(s["windows"].get("0", "").startswith(APP + "/") for s in evidence["samples"]),
        "calendar_never_on_owner": all(CALENDAR not in s["windows"].get("0", "") for s in evidence["samples"]),
        # Isolation is what this regression proves; whether the model finished the
        # calendar task is reported separately in taskStatus / taskMessage.
        "task_ended_without_timeout": "taskStatus" in evidence,
        "typing_starts_within_2s": evidence.get("typingGapSeconds", 99) <= 2.0,
    }
    evidence["checks"] = checks
    clear_owner_draft(c)
    (target / "evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"evidence": str(target), **checks, "taskStatus": evidence.get("taskStatus"),
                      "taskMessage": evidence.get("taskMessage", "")[:200]}, ensure_ascii=False, indent=2))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())

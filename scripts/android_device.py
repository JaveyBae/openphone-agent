"""The Android device layer: the private display, its isolation checks and action execution.

Everything that talks to adb lives here; per-app guards (Messages, Gmail, order pages,
permission prompts) are in app_guards.py and run inside execute(). The agent loop only asks for a capture,
an action to be executed, or a fact about the display (focus, resumed app, prompts).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

from action_safety import bounded_int, require_string, validate_input_text
from agent_common import (HEIGHT, ISOLATION_FLAGS, PACKAGE, WIDTH, WORKSPACE_DIRECTORY, WORKSPACE_STATE, AgentError,
                          Observation)
from app_guards import AppGuards
from app_targets import (CALENDAR_PACKAGE, CLOCK_PACKAGE, CONTACTS_PACKAGE, GMAIL_PACKAGE, MAPS_PACKAGE,
                         MEITUAN_PACKAGE, RAILWAY_PACKAGE, SMS_PACKAGE)


TEXT_PACKAGES = (PACKAGE, "com.jingdong.app.mall", "com.taobao.taobao", "com.xunmeng.pinduoduo", SMS_PACKAGE,
                 "org.fossify.calendar", CALENDAR_PACKAGE, GMAIL_PACKAGE, MEITUAN_PACKAGE, CLOCK_PACKAGE,
                 MAPS_PACKAGE, CONTACTS_PACKAGE, RAILWAY_PACKAGE)


def workspace_identity(state: dict) -> bool:
    session = state.get("session", "")
    return (state.get("backend") == "adb-shell"
            and type(state.get("displayId")) is int and state["displayId"] > 0
            and isinstance(session, str)
            and re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", session) is not None
            and state.get("displayName") == "OpenPhone Isolated Workspace " + session)


def workspace_is_isolated(state: dict, displays: str, dump: str) -> bool:
    """Check live identity and device flags, not a cached readiness boolean.

    Android 15's DisplayInfo.toString omits the two focus flags. The underlying
    DisplayDeviceInfo line in dumpsys display includes them.
    """
    if not workspace_identity(state):
        return False
    name = state["displayName"]
    live = any(line.startswith(f"Display id {state['displayId']}:")
               and f'"{name}"' in line and "owner com.android.shell (uid 2000)" in line
               and re.search(r"\bremoveMode 1\b", line)
               for line in displays.splitlines())
    return live and any(line.strip().startswith(f'DisplayDeviceInfo{{"{name}":')
                        and "owner com.android.shell (uid 2000)" in line
                        and all(re.search(rf"\b{flag}\b", line) for flag in ISOLATION_FLAGS)
                        for line in dump.splitlines())


def private_frame_command(state: dict) -> tuple[str, ...]:
    if state.get("backend") == "adb-shell":
        if not workspace_identity(state):
            raise AgentError("Invalid isolated workspace identity")
        return ("exec-out", "cat", WORKSPACE_DIRECTORY + "/frame-" + state["session"] + ".png")
    return ("exec-out", "run-as", PACKAGE, "cat", "files/display/latest.png")


def input_focus(dump: str) -> tuple[int | None, set[int]]:
    global_focus = re.search(r"(?:m?FocusedDisplayId|FocusedDisplay)\s*[:=]\s*(\d+)\b", dump)
    windows = re.search(r"FocusedWindows:[ \t]*\n((?:[ \t]+displayId=[^\n]*\n)*)", dump)
    focused = set(map(int, re.findall(r"displayId=(\d+),\s*name=", windows.group(1)))) if windows else set()
    return (int(global_focus.group(1)) if global_focus else None, focused)


def display_root_tasks(activity_dump: str, display_id: int) -> list[tuple[int, str]]:
    """Standard app root tasks on one display, as (task id, package)."""
    current, tasks = None, []
    for line in activity_dump.splitlines():
        if line.startswith("ActivityTaskSupervisor state:"):
            break
        match = re.match(r"Display #(\d+) ", line)
        if match:
            current = int(match.group(1))
            continue
        task = re.match(r"  \* Task\{\S+ #(\d+) type=standard (?:A|I)=(?:\d+:)?([A-Za-z0-9_.]+)", line)
        if current == display_id and task:
            tasks.append((int(task.group(1)), task.group(2)))
    return tasks


def locate_adb() -> str:
    candidates = [
        os.environ.get("ADB"),
        str(Path.home() / "Library/Android/sdk/platform-tools/adb"),
        str(Path(os.environ.get("ANDROID_HOME", "")) / "platform-tools/adb")
        if os.environ.get("ANDROID_HOME")
        else None,
        "adb",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        if candidate == "adb":
            try:
                subprocess.run(
                    [candidate, "version"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=True,
                )
                return candidate
            except (FileNotFoundError, subprocess.CalledProcessError):
                continue
        if Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise AgentError("adb was not found; set ADB or ANDROID_HOME")


class AndroidController(AppGuards):
    def __init__(self, adb: str, dry_run: bool = False) -> None:
        self.adb = adb
        self.dry_run = dry_run
        self.adb_timing: dict[str, list] = {}

    def drain_adb_timing(self) -> dict[str, dict[str, float]]:
        timing = {key: {"calls": count, "seconds": round(seconds, 3)}
                  for key, (count, seconds) in sorted(self.adb_timing.items(), key=lambda kv: -kv[1][1])}
        self.adb_timing = {}
        return timing

    def _run(self, *args: str, binary: bool = False) -> bytes | str:
        command = [self.adb, *args]
        if self.dry_run:
            print("DRY-RUN", " ".join(command))
            return b"" if binary else ""
        started = time.perf_counter()
        try:
            result = subprocess.run(command, capture_output=True, check=False, timeout=20)
        finally:
            # Timing only: per-command adb cost, drained once per step by the loop.
            entry = self.adb_timing.setdefault(adb_timing_key(args), [0, 0.0])
            entry[0] += 1
            entry[1] += time.perf_counter() - started
        if result.returncode != 0:
            message = result.stderr.decode("utf-8", "replace").strip()
            raise AgentError(f"adb command failed: {message or command}")
        return result.stdout if binary else result.stdout.decode("utf-8", "replace")

    def state(self) -> dict[str, Any]:
        # A stale shell state must fail closed, never fall back to an old mirror.
        try:
            raw = self._run("shell", "cat", WORKSPACE_STATE)
        except AgentError:
            raw = None
        if raw is not None:
            try:
                state = json.loads(str(raw))
            except json.JSONDecodeError as error:
                raise AgentError("Invalid isolated workspace state") from error
            if not isinstance(state, dict) or not workspace_identity(state):
                raise AgentError("Invalid isolated workspace identity")
            return state
        raw = self._run("shell", "run-as", PACKAGE, "cat", "files/display-state.json")
        try:
            return json.loads(str(raw))
        except json.JSONDecodeError as error:
            raise AgentError(
                "agent display state is unavailable; open the app and tap Start agent display"
            ) from error

    def require_display(self) -> int:
        state = self.state()
        display_id = state.get("displayId", -1)
        if type(display_id) is not int or display_id <= 0:
            raise AgentError("agent display is stopped; tap Start agent display in the app")
        raw = self._run("shell", "cmd", "display", "get-displays")
        lines = [line for line in str(raw).splitlines() if line.startswith(f"Display id {display_id}:")]
        if not lines or (state.get("backend") == "adb-shell" and
                         (f'"{state["displayName"]}"' not in lines[0]
                          or "owner com.android.shell (uid 2000)" not in lines[0])):
            raise AgentError(
                f"agent display {display_id} no longer exists; "
                "check the authorized ADB workspace connection"
            )
        return display_id

    def require_isolation(self, display_id: int) -> None:
        state = self.state()
        displays = str(self._run("shell", "cmd", "display", "get-displays"))
        dump = str(self._run("shell", "dumpsys", "display"))
        if state.get("displayId") != display_id or not workspace_is_isolated(state, displays, dump):
            raise AgentError("focus isolation is unavailable; refusing to control a legacy display")

    def require_owner_focus(self, private_id: int, *, private_window: bool = False) -> dict:
        top, windows = input_focus(str(self._run("shell", "dumpsys", "input")))
        # The owner's display must keep the global focus. It may briefly have no focused
        # window of its own (Overview/recents, the notification shade, the lock screen):
        # that is the owner using the phone, not the agent taking focus.
        if top != 0:
            raise AgentError(f"owner input focus is not on display 0 (focused display {top}); "
                             "execution stopped without switching focus")
        if private_window and private_id not in windows:
            raise AgentError("private display has no focused window; refusing keyboard input")
        return {"top_display": top, "focused_window_displays": sorted(windows)}

    def package_displays(self, package: str) -> set[int]:
        raw = self._run("shell", "dumpsys", "activity", "activities")
        return parse_package_displays(str(raw), package)

    def require_package_off_owner_display(self, package: str, agent_display: int) -> None:
        displays = self.package_displays(package)
        if 0 in displays and agent_display != 0:
            raise AgentError(
                f"refusing to launch {package}: it already has a task on the owner's "
                "display 0 and Android may reuse that task; close it on display 0 or "
                "choose a different agent app"
            )

    def verify_package_on_agent_display(self, package: str, agent_display: int) -> None:
        self.note_launch(package)
        if self.dry_run:
            return
        for _ in range(20):
            time.sleep(0.25)
            dump = str(self._run("shell", "dumpsys", "activity", "activities"))
            if package_is_resumed(dump, package, agent_display):
                return
        raise AgentError(
            f"Android did not resume {package} on agent display {agent_display}; "
            "execution stopped to protect the owner's screen"
        )

    def capture(self, target: Path) -> Observation:
        display_id = self.require_display()
        self.require_private_content(display_id)
        if self.state().get("backend") == "adb-shell":
            self.require_isolation(display_id)
        if getattr(self, "_last_capture_display", None) != display_id:
            # The producer writes at most every 750 ms. Let the first frame
            # after workspace initialization replace any startup mirror frame.
            time.sleep(0.8)
            self._last_capture_display = display_id
        state = self.state()
        if state.get("displayId") != display_id:
            raise AgentError("workspace changed before capture")
        payload = self._run(*private_frame_command(state), binary=True)
        assert isinstance(payload, bytes)
        if len(payload) < 128:
            raise AgentError("no agent-display frame is available yet")
        self.require_private_content(display_id)
        if self.state() != state:
            raise AgentError("workspace changed during capture")
        target.write_bytes(payload)
        return Observation(target, hashlib.sha256(payload).hexdigest(), display_id)

    def reset_private_workspace(self, display_id: int) -> list[dict[str, Any]]:
        """Close every app task left on the PRIVATE display for a clean start.

        Tasks on the owner's display are never touched; a task id seen there is
        a hard stop. Afterwards the private display must hold no app task.
        """
        if display_id <= 0:
            raise AgentError("refusing to reset the owner's display")
        self.require_isolation(display_id)
        dump = str(self._run("shell", "dumpsys", "activity", "activities"))
        owner = {task for task, _ in display_root_tasks(dump, 0)}
        removed = []
        for task, package in display_root_tasks(dump, display_id):
            if task in owner:
                raise AgentError("task appears on the owner's display; workspace reset stopped")
            self._run("shell", "am", "stack", "remove", str(task))
            removed.append({"task": task, "package": package})
        # Task removal is asynchronous; wait briefly, never retry removal blindly.
        for attempt in range(10):
            if not display_root_tasks(str(self._run("shell", "dumpsys", "activity", "activities")), display_id):
                break
            time.sleep(0.3)
        else:
            raise AgentError("private workspace reset incomplete; execution stopped")
        self.require_isolation(display_id)
        self.last_focus_evidence = {"after": self.require_owner_focus(display_id)}
        return removed

    def wait_private_content(self, display_id: int, seconds: float = 6.0) -> bool:
        """After launching an app, give it time to draw before treating the display as empty."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.private_content_state(display_id) is True:
                return True
            time.sleep(0.25)
        return False

    def require_private_content(self, display_id: int) -> None:
        if self.private_content_state(display_id) is not True:
            raise AgentError("private workspace has no verified content; refusing a possible owner-screen mirror. Initialize it with scripts/agentctl.sh open-browser")

    def private_content_state(self, display_id: int) -> bool | None:
        dump = str(self._run("shell", "dumpsys", "display"))
        return display_has_content(dump, display_id)

    def prepare_workspace(self, initial_url: str, *, allow_initialize: bool) -> bool:
        """Bootstrap a known-empty workspace, never navigate existing content.

        A MediaProjection display can exist before it hosts an activity. On some
        Android versions its empty capture mirrors the owner display. Launch our
        separate browser through the authorized host before reading any frame.
        Unknown display metadata and read-only runs must still fail closed.
        """
        display_id = self.require_display()

        content = self.private_content_state(display_id)
        if content is True:
            return False
        if content is None or not allow_initialize:
            self.require_private_content(display_id)
            return False

        self.execute({"action": "open_url", "arguments": {"url": initial_url}})
        for _ in range(20):
            if self.require_display() != display_id:
                raise AgentError("private workspace changed during initialization; submit the task again")
            content = self.private_content_state(display_id)
            if content is True:
                return True
            if content is None:
                break
            time.sleep(0.25)
        raise AgentError("private workspace initialization failed: independent content was not verified")

    def execute(self, action: dict[str, Any]) -> None:
        self.last_focus_evidence = None
        self.last_private_input_focused = False
        name = action["action"]
        arguments = action.get("arguments") or {}
        display_id = self.require_display()
        if name != "wait":
            self.require_isolation(display_id)
            before_focus = self.require_owner_focus(display_id, private_window=name in {"tap", "swipe", "input_text", "key"})
        self.before_action(name, display_id)  # per-app guards (app_guards.py)

        if name == "open_url":
            url = require_string(arguments, "url")
            if not re.match(r"^https?://", url):
                raise AgentError("open_url accepts only http/https URLs")
            self.note_url(url)
            self._run(
                "shell",
                "am",
                "start",
                "--display",
                str(display_id),
                "--activity-clear-top",
                "-n",
                f"{PACKAGE}/.AgentBrowserActivity",
                "-a",
                "android.intent.action.VIEW",
                "-d",
                # adb joins arguments into one device shell command: quote it, or "&" splits the URL.
                shlex.quote(url),
            )
            self.verify_package_on_agent_display(PACKAGE, display_id)
        elif name == "launch_app":
            package = require_string(arguments, "package")
            if not re.fullmatch(r"[A-Za-z0-9_.]+", package):
                raise AgentError("invalid Android package name")
            self.require_package_off_owner_display(package, display_id)
            tasks = private_package_tasks(str(self._run("shell", "dumpsys", "activity", "activities")), package, display_id)
            if tasks:
                # Resume the existing private task, preserving its login / risk
                # screen. Relaunching JD's entry activity can immediately finish
                # and reveal the browser instead of restoring the existing task.
                self._run("shell", "am", "task", "focus", str(tasks[0]))
                self.verify_package_on_agent_display(package, display_id)
                self.last_focus_evidence = {"before": before_focus,
                                            "after": self.require_owner_focus(display_id)}
                return
            component_raw = self._run(
                "shell", "cmd", "package", "resolve-activity", "--brief", package
            )
            component_lines = [
                line.strip()
                for line in str(component_raw).splitlines()
                if "/" in line and not line.startswith("priority=")
            ]
            if not component_lines:
                raise AgentError(f"no launchable activity found for {package}")
            component = component_lines[-1]
            self._run(
                "shell",
                "am",
                "start",
                "--display",
                str(display_id),
                "--activity-multiple-task",
                "-n",
                component,
            )
            self.verify_package_on_agent_display(package, display_id)
        elif name == "tap":
            x = bounded_int(arguments, "x", 0, WIDTH - 1)
            y = bounded_int(arguments, "y", 0, HEIGHT - 1)
            self.tap(x, y, display_id)
        elif name == "swipe":
            values = [
                bounded_int(arguments, "x1", 0, WIDTH - 1),
                bounded_int(arguments, "y1", 0, HEIGHT - 1),
                bounded_int(arguments, "x2", 0, WIDTH - 1),
                bounded_int(arguments, "y2", 0, HEIGHT - 1),
                bounded_int(arguments, "duration_ms", 100, 2000),
            ]
            self.check_swipe(*values[:4], display_id)
            self._run(
                "shell", "input", "-d", str(display_id), "swipe", *map(str, values)
            )
        elif name == "input_text":
            value = require_string(arguments, "text")
            validate_input_text(value)
            if not value.isascii():
                self.input_unicode(value, display_id)
            elif any(package_is_resumed(str(self._run("shell", "dumpsys", "activity", "activities")), p, display_id)
                     for p in (SMS_PACKAGE, GMAIL_PACKAGE)):
                # Recipient fields need real key events to show contact suggestions.
                self.type_keys(value, display_id)
            else:
                # Replace the field like Chinese input does, so typing twice never duplicates text.
                try:
                    self.input_unicode(value, display_id)
                except AgentError:
                    self.type_keys(value, display_id)
        elif name == "key":
            keycode = bounded_int(arguments, "keycode", 0, 1000)
            if keycode not in {4, 19, 20, 21, 22, 61, 62, 66, 67, 112, 122, 123}:
                raise AgentError("key is not a private navigation/edit key; global keys are forbidden")
            self.check_key(keycode, display_id)
            self._run(
                "shell", "input", "-d", str(display_id), "keyevent", str(keycode)
            )
        elif name == "wait":
            seconds = float(arguments.get("seconds", 1.0))
            time.sleep(max(0.2, min(5.0, seconds)))
        else:
            raise AgentError(f"{name} is not an executable action")
        if name != "wait":
            self.last_focus_evidence = {"before": before_focus,
                                        "after": self.settled_owner_focus(display_id, private_window=name in {"tap", "swipe", "input_text", "key"})}

    def settled_owner_focus(self, display_id: int, *, private_window: bool) -> dict:
        """Post-action check. A dialog closing on the private display can leave it
        without a focused window for a moment; wait for that briefly (never act).
        Owner focus leaving display 0 still stops immediately."""
        for attempt in range(6):
            try:
                return self.require_owner_focus(display_id, private_window=private_window)
            except AgentError as error:
                if "private display has no focused window" not in str(error) or attempt == 5:
                    raise
                time.sleep(0.2)

    def type_keys(self, value: str, display_id: int) -> None:
        adb_text = value.replace("%", "%25").replace(" ", "%s")
        self._run("shell", "input", "-d", str(display_id), "text", shlex.quote(adb_text))

    def input_unicode(self, value: str, display_id: int) -> None:
        """Write Unicode only to the verified private app's focused editable node.

        No IME switching, clipboard mutation, owner-window root or ASCII fallback.
        The short-lived helper rechecks the session/focus and reads text back.
        """
        from workspace_runtime import install_text_helper
        state = self.state()
        if not workspace_identity(state) or state["displayId"] != display_id:
            raise AgentError("Invalid Unicode workspace identity")
        dump = str(self._run("shell", "dumpsys", "activity", "activities"))
        packages = [p for p in TEXT_PACKAGES if package_is_resumed(dump, p, display_id)]
        if len(packages) != 1:
            raise AgentError("Unicode input requires one supported app resumed on the private display")
        if not getattr(self, "_text_helper_jar", None):
            self._text_helper_jar = install_text_helper()
        self.require_isolation(display_id)
        self.require_owner_focus(display_id, private_window=True)
        if self.state() != state:
            raise AgentError("Private workspace changed before Unicode input")
        encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
        command = ("CLASSPATH=" + shlex.quote(self._text_helper_jar)
                   + " app_process / ai.openphone.workspace.PrivateTextInput "
                   + " ".join(shlex.quote(x) for x in (str(display_id), state["session"], packages[0], encoded)))
        raw = str(self._run("shell", command))
        # Surface helper failure, never retry with a different input mechanism.
        try:
            result = json.loads(raw.strip())
        except json.JSONDecodeError as error:
            raise AgentError("Private Unicode input returned invalid verification") from error
        if (not isinstance(result, dict) or result.get("ok") is not True
                or result.get("verified") is not True or result.get("displayId") != display_id
                or result.get("session") != state["session"] or result.get("package") != packages[0]
                or result.get("method") != "accessibility_set_text"
                or result.get("characters") != len(value)):
            detail = result.get("error", "identity/readback mismatch") if isinstance(result, dict) else "invalid result"
            raise AgentError(f"Private Unicode input failed: {detail}; owner IME/clipboard will not be used")
        self.require_isolation(display_id)
        self.require_owner_focus(display_id, private_window=True)
        if self.state() != state:
            raise AgentError("Private workspace changed during Unicode input")


def display_has_content(display_dump: str, display_id: int) -> bool | None:
    """Read a logical display's content flag; unknown formats fail closed."""
    if display_id <= 0 or "Logical Displays:" not in display_dump:
        return None
    logical = display_dump.split("Logical Displays:", 1)[1]
    block = re.search(rf"(?ms)^[ \t]*Display {display_id}:[ \t]*\n(.*?)(?=^[ \t]*Display \d+:[ \t]*$|\Z)", logical)
    if not block:
        return None
    flag = re.search(r"\bmHasContent=(true|false)\b", block.group(1))
    return flag.group(1) == "true" if flag else None


def parse_package_displays(activity_dump: str, package: str) -> set[int]:
    """Return displays containing real tasks/activities for a package.

    dumpsys repeats historical task references later in its output, so only the
    initial per-display activity list is considered.
    """
    result: set[int] = set()
    current_display: int | None = None
    for line in activity_dump.splitlines():
        if line.startswith("ActivityTaskSupervisor state:"):
            break
        display_match = re.match(r"\s*Display #(\d+) ", line)
        if display_match:
            current_display = int(display_match.group(1))
            continue
        if (
            current_display is not None
            and package in line
            and ("* Task{" in line or "ActivityRecord{" in line)
        ):
            result.add(current_display)
    return result


def private_package_tasks(activity_dump: str, package: str, display_id: int) -> list[int]:
    """Actual Hist records only: never focus display 0 or historical task IDs."""
    if display_id <= 0:
        return []
    tasks = []
    current = None
    for line in activity_dump.splitlines():
        if line.startswith("ActivityTaskSupervisor state:"):
            break
        match = re.match(r"\s*Display #(\d+) ", line)
        if match:
            current = int(match.group(1))
        if current == display_id and re.search(r"\* Hist\s", line):
            record = re.search(r"ActivityRecord\{[^}]*\bu\d+\s+" + re.escape(package) + r"/\S+\s+t(\d+)\b", line)
            if record and int(record.group(1)) not in tasks:
                tasks.append(int(record.group(1)))
    return tasks


def resumed_package(activity_dump: str, display_id: int) -> str | None:
    """The package of the resumed activity on one display."""
    current = None
    for line in activity_dump.splitlines():
        if line.startswith("ActivityTaskSupervisor state:"):
            break
        match = re.match(r"\s*Display #(\d+) ", line)
        if match:
            current = int(match.group(1))
        found = re.search(r"topResumedActivity=ActivityRecord\{\S+ u\d+ ([A-Za-z0-9_.]+)/", line)
        if current == display_id and found:
            return found.group(1)
    return None


def package_is_resumed(activity_dump: str, package: str, display_id: int) -> bool:
    current = None
    for line in activity_dump.splitlines():
        if line.startswith("ActivityTaskSupervisor state:"):
            break
        match = re.match(r"\s*Display #(\d+) ", line)
        if match:
            current = int(match.group(1))
        if current == display_id and re.search(r"topResumedActivity=ActivityRecord\{[^}]*\bu\d+\s+" + re.escape(package) + r"/", line):
            return True
    return False


def adb_timing_key(args: tuple[str, ...]) -> str:
    """Group adb calls by what they do, e.g. 'shell dumpsys activity' or 'exec-out cat'."""
    parts = list(args)
    if len(parts) >= 3 and parts[1] == "run-as":
        parts = [parts[0], *parts[3:]]
    if len(parts) >= 2 and " " in parts[1]:
        parts = [parts[0], *parts[1].split()]
    depth = 3 if len(parts) >= 2 and parts[1] in {"dumpsys", "cmd", "am"} else 2
    return " ".join(parts[:depth])[:60]

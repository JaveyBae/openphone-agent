#!/usr/bin/env python3
"""Host-side agent loop for the private Android display.

The model can see only the private display capture and can emit one bounded JSON
action per turn. ADB performs the action against that display ID, then the loop
captures fresh visual evidence before asking for another action.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE = "ai.openphone.agent"
WIDTH = 720
HEIGHT = 1280
WORKSPACE_DIRECTORY = "/data/local/tmp/openphone-workspace"
WORKSPACE_STATE = WORKSPACE_DIRECTORY + "/state.json"
ISOLATION_FLAGS = ("FLAG_TRUSTED", "FLAG_OWN_FOCUS", "FLAG_STEAL_TOP_FOCUS_DISABLED", "FLAG_OWN_CONTENT_ONLY")
ALLOWED_ACTIONS = {
    "open_url",
    "launch_app",
    "tap",
    "swipe",
    "input_text",
    "key",
    "wait",
    "request_user",
    "finish",
}
SENSITIVE_TERMS = (
    "pay",
    "payment",
    "purchase",
    "checkout",
    "password",
    "passcode",
    "verification code",
    "otp",
    "delete",
    "uninstall",
    "send message",
    "send email",
    "agree to terms",
    "付款",
    "支付",
    "购买",
    "密码",
    "验证码",
    "删除",
    "卸载",
    "发送",
    "同意条款",
    "captcha",
    "login",
    "登录",
    "快速验证",
    "京东验证",
    "安全验证",
    "security verification",
    "人机验证",
)

SYSTEM_PROMPT = f"""You are the action planner for a private Android display.
The phone owner is using display 0. You control only the separate {WIDTH}x{HEIGHT}
agent display shown in the attached screenshot.

Return exactly one JSON object and no markdown. Its shape is:
{{
  "action": "open_url|launch_app|tap|swipe|input_text|key|wait|request_user|finish",
  "arguments": {{}},
  "reason": "short explanation based on visible evidence",
  "risk": "none|sensitive"
}}

Arguments:
- open_url: {{"url":"https://..."}}
- launch_app: {{"package":"com.example.app"}}
- tap: {{"x":0..{WIDTH - 1},"y":0..{HEIGHT - 1}}}
- swipe: {{"x1":...,"y1":...,"x2":...,"y2":...,"duration_ms":100..2000}}
- input_text: {{"text":"..."}}
- key: {{"keycode": integer Android key code}}
- wait: {{"seconds":0.2..5}}
- request_user: {{"message":"what the user must confirm or do"}}
- finish: {{"summary":"what visible evidence proves completion"}}

Rules:
1. Perform one action at a time; a new screenshot arrives after every action.
2. Never act on display 0 and never assume an action succeeded without evidence.
3. Before payment, purchase, sending a message/email, deleting data, entering a
   password/code, installing software, or agreeing to terms, use request_user and
   mark risk sensitive. Do not perform the sensitive action yourself.
4. Prefer UI interaction over shortcuts after the target app/page is opened.
5. If blocked or uncertain, request_user instead of guessing.
"""


class AgentError(RuntimeError):
    pass


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


def explicit_target_package(goal: str) -> str | None:
    # Route only an explicit, single-app open request; merely mentioning a brand
    # or asking for a comparison must not navigate the existing workspace.
    names = {"京东": "com.jingdong.app.mall", "jd": "com.jingdong.app.mall",
             "淘宝": "com.taobao.taobao", "taobao": "com.taobao.taobao",
             "拼多多": "com.xunmeng.pinduoduo", "pinduoduo": "com.xunmeng.pinduoduo"}
    mentioned = {package for name, package in names.items()
                 if re.search(re.escape(name) if not name.isascii() else rf"\b{name}\b", goal, re.I)}
    if len(mentioned) != 1 or re.search(r"不要|别|不需|don't|do not", goal, re.I):
        return None
    match = re.search(r"(?:打开|启动|进入|\bopen\b|\blaunch\b)\s*(?:一下\s*)?(京东|淘宝|拼多多|JD\b|Taobao\b|Pinduoduo\b)", goal, re.I)
    return names[match.group(1).lower()] if match else None


@dataclass(frozen=True)
class Observation:
    path: Path
    sha256: str
    display_id: int


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


class AndroidController:
    def __init__(self, adb: str, dry_run: bool = False) -> None:
        self.adb = adb
        self.dry_run = dry_run

    def _run(self, *args: str, binary: bool = False) -> bytes | str:
        command = [self.adb, *args]
        if self.dry_run:
            print("DRY-RUN", " ".join(command))
            return b"" if binary else ""
        result = subprocess.run(command, capture_output=True, check=False, timeout=20)
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
        if top != 0 or 0 not in windows:
            raise AgentError("owner input focus is not on display 0; execution stopped without switching focus")
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
        name = action["action"]
        arguments = action.get("arguments") or {}
        display_id = self.require_display()
        if name != "wait":
            self.require_isolation(display_id)
            before_focus = self.require_owner_focus(display_id, private_window=name in {"input_text", "key"})

        if name == "open_url":
            url = require_string(arguments, "url")
            if not re.match(r"^https?://", url):
                raise AgentError("open_url accepts only http/https URLs")
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
                url,
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
            self._run("shell", "input", "-d", str(display_id), "tap", str(x), str(y))
        elif name == "swipe":
            values = [
                bounded_int(arguments, "x1", 0, WIDTH - 1),
                bounded_int(arguments, "y1", 0, HEIGHT - 1),
                bounded_int(arguments, "x2", 0, WIDTH - 1),
                bounded_int(arguments, "y2", 0, HEIGHT - 1),
                bounded_int(arguments, "duration_ms", 100, 2000),
            ]
            self._run(
                "shell", "input", "-d", str(display_id), "swipe", *map(str, values)
            )
        elif name == "input_text":
            value = require_string(arguments, "text")
            if len(value) > 500:
                raise AgentError("input_text is limited to 500 characters")
            if not value.isascii():
                raise AgentError("private text input currently supports ASCII only; owner IME will not be used")
            adb_text = value.replace("%", "%25").replace(" ", "%s")
            self._run("shell", "input", "-d", str(display_id), "text", shlex.quote(adb_text))
        elif name == "key":
            keycode = bounded_int(arguments, "keycode", 0, 1000)
            if keycode not in {4, 19, 20, 21, 22, 61, 62, 66, 67, 112, 122, 123}:
                raise AgentError("key is not a private navigation/edit key; global keys are forbidden")
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
                                        "after": self.require_owner_focus(display_id)}


class OpenAICompatiblePlanner:
    def __init__(self, base_url: str, model: str, api_key: str, timeout: int) -> None:
        if not model:
            raise AgentError("AGENT_MODEL or --model is required for the openai provider")
        cleaned = base_url.rstrip("/")
        self.endpoint = (
            cleaned + "/chat/completions"
            if cleaned.endswith("/v1")
            else cleaned + "/v1/chat/completions"
        )
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    def next_action(
        self,
        goal: str,
        observation: Observation,
        history: list[dict[str, Any]],
        include_image: bool,
    ) -> dict[str, Any]:
        compact_history = [
            {
                "step": item["step"],
                "action": item["action"].get("action"),
                "reason": item["action"].get("reason", ""),
                "screen_changed": item.get("screen_changed"),
            }
            for item in history[-8:]
        ]
        text = (
            f"Goal: {goal}\n"
            f"Current step: {len(history)}\n"
            f"Display: {observation.display_id}, {WIDTH}x{HEIGHT}\n"
            f"Recent actions: {json.dumps(compact_history, ensure_ascii=False)}\n"
            "Choose the single safest next action from the current screenshot."
        )
        if include_image:
            image_b64 = base64.b64encode(observation.path.read_bytes()).decode("ascii")
            user_content: Any = [
                {"type": "text", "text": text},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{image_b64}",
                        "detail": "low",
                    },
                },
            ]
        else:
            user_content = text + "\nNo image was supplied; request_user if visual evidence is needed."

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0,
            "stream": False,
        }
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                **(
                    {"Authorization": f"Bearer {self.api_key}"}
                    if self.api_key
                    else {}
                ),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:1000]
            raise AgentError(f"model API returned HTTP {error.code}: {detail}") from error
        except urllib.error.URLError as error:
            raise AgentError(f"could not reach model API at {self.endpoint}: {error.reason}") from error
        try:
            content = result["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise AgentError("model response did not contain choices[0].message.content") from error
        if isinstance(content, list):
            content = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        return parse_action(str(content))


class MockPlanner:
    """Deterministic no-model planner used to prove the full control loop."""

    def __init__(self, initial_url: str) -> None:
        self.initial_url = initial_url

    def next_action(
        self,
        goal: str,
        observation: Observation,
        history: list[dict[str, Any]],
        include_image: bool,
    ) -> dict[str, Any]:
        del goal, observation, include_image
        if not history:
            return {
                "action": "open_url",
                "arguments": {"url": self.initial_url},
                "reason": "Open the harmless smoke-test page on the private display",
                "risk": "none",
            }
        if len(history) == 1:
            return {
                "action": "wait",
                "arguments": {"seconds": 3},
                "reason": "Allow the page to finish rendering before verification",
                "risk": "none",
            }
        if len(history) == 2:
            return {
                "action": "swipe",
                "arguments": {
                    "x1": WIDTH // 2,
                    "y1": 1050,
                    "x2": WIDTH // 2,
                    "y2": 430,
                    "duration_ms": 500,
                },
                "reason": "Exercise display-scoped touch input on the rendered page",
                "risk": "none",
            }
        if len(history) == 3:
            return {
                "action": "wait",
                "arguments": {"seconds": 1},
                "reason": "Capture stable post-swipe evidence",
                "risk": "none",
            }
        return {
            "action": "finish",
            "arguments": {"summary": "Private-display launch and observation loop completed"},
            "reason": "The smoke-test loop executed and captured post-action evidence",
            "risk": "none",
        }


def parse_action(raw: str) -> dict[str, Any]:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        action = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        if start < 0:
            raise AgentError("model did not return a JSON action")
        try:
            action, _ = json.JSONDecoder().raw_decode(cleaned[start:])
        except json.JSONDecodeError as error:
            raise AgentError("model returned malformed JSON") from error
    if not isinstance(action, dict):
        raise AgentError("model action must be a JSON object")
    name = action.get("action")
    if name not in ALLOWED_ACTIONS:
        raise AgentError(f"model requested unsupported action: {name!r}")
    if not isinstance(action.get("arguments", {}), dict):
        raise AgentError("action arguments must be a JSON object")
    action.setdefault("arguments", {})
    action.setdefault("reason", "")
    action.setdefault("risk", "none")
    return action


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


def require_string(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AgentError(f"{key} must be a non-empty string")
    return value


def bounded_int(arguments: dict[str, Any], key: str, low: int, high: int) -> int:
    try:
        value = int(arguments[key])
    except (KeyError, TypeError, ValueError) as error:
        raise AgentError(f"{key} must be an integer") from error
    if not low <= value <= high:
        raise AgentError(f"{key} must be between {low} and {high}")
    return value


def sensitive_reason(action: dict[str, Any]) -> str | None:
    if str(action.get("risk", "none")).lower() == "sensitive":
        return "the model marked this action as sensitive"
    searchable = " ".join(
        [
            str(action.get("reason", "")),
            json.dumps(action.get("arguments", {}), ensure_ascii=False),
        ]
    ).lower()
    for term in SENSITIVE_TERMS:
        if term in searchable:
            return f"the action mentions sensitive operation {term!r}"
    return None


def log_event(path: Path, event: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False) + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the private-display agent loop")
    parser.add_argument("--goal", required=True, help="Natural-language task for the agent")
    parser.add_argument(
        "--provider",
        choices=("mock", "openai", "autoglm"),
        default=os.environ.get("AGENT_PROVIDER", "mock"),
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("AGENT_BASE_URL", "http://127.0.0.1:11434/v1"),
    )
    parser.add_argument("--model", default=os.environ.get("AGENT_MODEL", ""))
    parser.add_argument("--api-key", default=os.environ.get("AGENT_API_KEY", ""))
    parser.add_argument("--max-steps", type=int, default=15)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--no-image", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--observe-only", action="store_true", help="Allow only finish/request_user; never execute UI actions")
    parser.add_argument("--initial-url", default="https://example.com")
    parser.add_argument("--runs-dir", type=Path, default=Path("runs"))
    return parser


def run(args: argparse.Namespace) -> int:
    if args.max_steps < 1 or args.max_steps > 50:
        raise AgentError("--max-steps must be between 1 and 50")
    controller = AndroidController(locate_adb(), dry_run=args.dry_run)
    if args.provider == "mock":
        planner: Any = MockPlanner(args.initial_url)
    elif args.provider == "autoglm":
        from autoglm_adapter import AutoGLMPlanner
        if not args.model:
            raise AgentError("--model is required for AutoGLM")
        planner = AutoGLMPlanner(args.base_url, args.model, args.api_key, args.timeout, WIDTH, HEIGHT)
    else:
        planner = OpenAICompatiblePlanner(
            args.base_url, args.model, args.api_key, args.timeout
        )

    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = args.runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    events_path = run_dir / "events.jsonl"
    metadata = {
        "started_at": utc_now(),
        "goal": args.goal,
        "provider": args.provider,
        "model": args.model or None,
        "base_url": args.base_url if args.provider != "mock" else None,
        "max_steps": args.max_steps,
    }
    (run_dir / "run.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Run log: {run_dir}")

    print(f"[0/{args.max_steps}] initialize_workspace: verify independent content before capture")
    try:
        if not args.observe_only and not args.dry_run:
            from workspace_runtime import ensure_running
            try:
                ensure_running()
            except RuntimeError as error:
                raise AgentError(str(error)) from error
        target = explicit_target_package(args.goal) if not args.observe_only else None
        if target:
            controller.execute({"action": "launch_app", "arguments": {"package": target}})
            log_event(events_path, {"timestamp": utc_now(), "stage": "explicit_app_setup",
                                   "package": target, "display_id": controller.require_display(),
                                   "result": "executed", "focus": controller.last_focus_evidence})
        initialized = controller.prepare_workspace(
            args.initial_url, allow_initialize=not args.observe_only
        )
    except AgentError as error:
        log_event(events_path, {
            "timestamp": utc_now(), "stage": "workspace_setup",
            "result": "blocked", "error": str(error),
        })
        raise
    log_event(events_path, {
        "timestamp": utc_now(), "stage": "workspace_setup",
        "display_id": controller.require_display(),
        "result": "initialized" if initialized else "existing_content",
        "action": {"action": "open_url", "arguments": {"url": args.initial_url}}
        if initialized else None,
    })

    history: list[dict[str, Any]] = []
    prior_hash: str | None = None
    stuck_count = 0
    repeated_action_count = 0
    prior_action_fingerprint: str | None = None

    for step in range(args.max_steps):
        observation = controller.capture(run_dir / f"step-{step:02d}.png")
        try:
            action = planner.next_action(
                args.goal, observation, history, include_image=not args.no_image
            )
        except (ValueError, SyntaxError, KeyError, urllib.error.URLError) as error:
            raise AgentError(f"model request/action failed: {error}") from error
        fingerprint = json.dumps({"action": action["action"], "arguments": action.get("arguments", {})}, ensure_ascii=False, sort_keys=True)
        repeated_action_count = (
            repeated_action_count + 1 if fingerprint == prior_action_fingerprint else 0
        )
        if repeated_action_count >= 2:
            raise AgentError("stopped after the model repeated the same action three times")
        prior_action_fingerprint = fingerprint

        event = {
            "timestamp": utc_now(),
            "step": step,
            "display_id": observation.display_id,
            "screenshot": observation.path.name,
            "screenshot_sha256": observation.sha256,
            "action": action,
        }
        print(
            f"[{step + 1}/{args.max_steps}] {action['action']}: "
            f"{action.get('reason', '')}"
        )

        if action["action"] == "request_user":
            event["result"] = "awaiting_user"
            log_event(events_path, event)
            message = action["arguments"].get("message", "User confirmation required")
            metadata.update({"status": "awaiting_user", "paused_at": utc_now(), "message": message})
            (run_dir / "run.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"Paused for user: {message}")
            return 2
        if action["action"] == "finish":
            event["result"] = "finished"
            log_event(events_path, event)
            summary = action["arguments"].get("summary", "Task finished")
            metadata.update({"status": "finished", "finished_at": utc_now(), "summary": summary})
            (run_dir / "run.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(summary)
            return 0

        safety_block = sensitive_reason(action)
        if safety_block:
            event["result"] = "blocked_sensitive"
            event["safety_reason"] = safety_block
            log_event(events_path, event)
            message = "模型尝试执行需要人工确认的操作，已被安全检查拦截，本步没有执行。请人工处理后重新提交任务。"
            metadata.update({"status": "awaiting_user", "paused_at": utc_now(), "message": message,
                             "safety_reason": safety_block})
            (run_dir / "run.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"Paused for user: {message}")
            return 2

        if args.observe_only:
            event["result"] = "blocked_observe_only"
            log_event(events_path, event)
            raise AgentError("observe-only verification refused a UI action")

        controller.execute(action)
        event["focus"] = getattr(controller, "last_focus_evidence", None)
        if action["action"] not in {"wait"}:
            time.sleep(1.0)
        changed = prior_hash is None or observation.sha256 != prior_hash
        event["screen_changed_since_previous_observation"] = changed
        event["result"] = "executed"
        log_event(events_path, event)
        history.append(
            {
                "step": step,
                "action": action,
                "screen_changed": changed,
            }
        )
        stuck_count = stuck_count + 1 if prior_hash == observation.sha256 else 0
        if stuck_count >= 3:
            raise AgentError("stopped because the private display did not change for four steps")
        prior_hash = observation.sha256

    raise AgentError(f"stopped at the configured {args.max_steps}-step limit")


def main() -> int:
    try:
        return run(build_parser().parse_args())
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130
    except (AgentError, subprocess.TimeoutExpired) as error:
        print(f"Agent error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

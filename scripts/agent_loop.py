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
        result = subprocess.run(command, capture_output=True, check=False)
        if result.returncode != 0:
            message = result.stderr.decode("utf-8", "replace").strip()
            raise AgentError(f"adb command failed: {message or command}")
        return result.stdout if binary else result.stdout.decode("utf-8", "replace")

    def state(self) -> dict[str, Any]:
        raw = self._run("shell", "run-as", PACKAGE, "cat", "files/display-state.json")
        try:
            return json.loads(str(raw))
        except json.JSONDecodeError as error:
            raise AgentError(
                "agent display state is unavailable; open the app and tap Start agent display"
            ) from error

    def require_display(self) -> int:
        display_id = int(self.state().get("displayId", -1))
        if display_id < 0:
            raise AgentError("agent display is stopped; tap Start agent display in the app")
        raw = self._run("shell", "cmd", "display", "get-displays")
        if f"Display id {display_id}:" not in str(raw):
            raise AgentError(
                f"agent display {display_id} no longer exists; "
                "tap Start agent display in the app again"
            )
        return display_id

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
        time.sleep(0.5)
        displays = self.package_displays(package)
        if agent_display not in displays:
            raise AgentError(
                f"Android did not place {package} on agent display {agent_display}; "
                "execution stopped to protect the owner's screen"
            )

    def capture(self, target: Path) -> Observation:
        display_id = self.require_display()
        payload = self._run(
            "exec-out",
            "run-as",
            PACKAGE,
            "cat",
            "files/display/latest.png",
            binary=True,
        )
        assert isinstance(payload, bytes)
        if len(payload) < 128:
            raise AgentError("no agent-display frame is available yet")
        target.write_bytes(payload)
        return Observation(target, hashlib.sha256(payload).hexdigest(), display_id)

    def execute(self, action: dict[str, Any]) -> None:
        name = action["action"]
        arguments = action.get("arguments") or {}
        display_id = self.require_display()

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
            adb_text = value.replace("%", "%25").replace(" ", "%s")
            self._run("shell", "input", "-d", str(display_id), "text", adb_text)
        elif name == "key":
            keycode = bounded_int(arguments, "keycode", 0, 1000)
            self._run(
                "shell", "input", "-d", str(display_id), "keyevent", str(keycode)
            )
        elif name == "wait":
            seconds = float(arguments.get("seconds", 1.0))
            time.sleep(max(0.2, min(5.0, seconds)))
        else:
            raise AgentError(f"{name} is not an executable action")


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
        choices=("mock", "openai"),
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
    parser.add_argument("--initial-url", default="https://example.com")
    parser.add_argument("--runs-dir", type=Path, default=Path("runs"))
    return parser


def run(args: argparse.Namespace) -> int:
    if args.max_steps < 1 or args.max_steps > 50:
        raise AgentError("--max-steps must be between 1 and 50")
    controller = AndroidController(locate_adb(), dry_run=args.dry_run)
    if args.provider == "mock":
        planner: Any = MockPlanner(args.initial_url)
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
        "base_url": args.base_url if args.provider == "openai" else None,
        "max_steps": args.max_steps,
    }
    (run_dir / "run.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Run log: {run_dir}")

    history: list[dict[str, Any]] = []
    prior_hash: str | None = None
    stuck_count = 0
    repeated_action_count = 0
    prior_action_fingerprint: str | None = None

    for step in range(args.max_steps):
        observation = controller.capture(run_dir / f"step-{step:02d}.png")
        action = planner.next_action(
            args.goal, observation, history, include_image=not args.no_image
        )
        fingerprint = json.dumps(action, ensure_ascii=False, sort_keys=True)
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
            print(f"Paused for user: {message}")
            return 2
        if action["action"] == "finish":
            event["result"] = "finished"
            log_event(events_path, event)
            print(action["arguments"].get("summary", "Task finished"))
            return 0

        safety_block = sensitive_reason(action)
        if safety_block:
            event["result"] = "blocked_sensitive"
            event["safety_reason"] = safety_block
            log_event(events_path, event)
            print(f"Paused by safety guard: {safety_block}")
            return 2

        controller.execute(action)
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
    except AgentError as error:
        print(f"Agent error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

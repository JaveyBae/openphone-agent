"""Built-in planners: an OpenAI-compatible JSON planner and the offline mock.

Model-specific planners live in autoglm_adapter.py and qwen_planner.py; all share
next_action(goal, observation, history, include_image) -> one validated action.
"""
from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.request
from typing import Any

from action_safety import ALLOWED_ACTIONS
from agent_common import HEIGHT, WIDTH, AgentError, Observation


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


def note_external_step(planner: Any, goal: str, step: int, display_id: int, action: dict) -> None:
    """Keep the model's own step history consistent when a step was taken from experience."""
    turns = getattr(planner, "turns", None)
    if turns is None:
        return
    planner.previous_sha = None  # the model did not see this step's screen
    args = action.get("arguments", {})
    if action["action"] == "tap":
        native = f'do(action="Tap", element=[{args["x"] * 1000 // WIDTH},{args["y"] * 1000 // HEIGHT}])'
    elif action["action"] == "key" and args.get("keycode") == 4:
        native = 'do(action="Back")'
    elif action["action"] == "launch_app":
        native = f'do(action="Launch", app="{args.get("package")}")'
    else:
        native = f'do(action="{action["action"]}")'
    turns.extend([{"role": "user", "content": f"Task: {goal}\nStep: {step}\nPrivate display: {display_id}."},
                  {"role": "assistant", "content": "<think>Same screen as a learned successful run: "
                   "taking its next step.</think>\n<answer>" + native + "</answer>"}])

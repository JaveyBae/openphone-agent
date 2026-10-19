#!/usr/bin/env python3
"""Host-side agent loop for the private Android display: command line and wiring.

The model sees only the private display capture and emits one bounded action per
turn; ADB performs it on that display, then a fresh capture is taken. The parts:

  agent_common.py     constants, errors, the run log line format
  android_device.py   AndroidController: adb, the private display, isolation, execution
  app_guards.py       per-app guards on the device: Messages, Gmail, order pages, permission prompts
  action_safety.py    action validation and word-level safety checks
  planners.py         OpenAI-compatible and mock planners (autoglm_adapter.py, qwen_planner.py)
  task_routing.py     which app an explicit request opens first
  agent_session.py    one run: observe -> decide -> guard -> execute -> record
  legacy_presets.py   hand-written skills, only in --mode legacy
  experience.py       learning from verified model runs; completion_check.py checks results

Other scripts import the device and error names from here as well, so they stay
available under this module's name.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

# Run as a script, this module is "__main__"; skills import it as "agent_loop".
# Register one module under both names so AgentError and friends are the same
# classes everywhere (otherwise a skill's fallback error escapes every handler).
if __name__ == "__main__":
    sys.modules.setdefault("agent_loop", sys.modules[__name__])

from action_safety import (ALLOWED_ACTIONS, SEND_TERMS, SENSITIVE_TERMS, bounded_int,
                           order_block_message, require_string, sensitive_reason, validate_input_text)
from agent_common import (HEIGHT, ISOLATION_FLAGS, PACKAGE, WIDTH, WORKSPACE_DIRECTORY,
                          WORKSPACE_STATE, AgentError, MailControlBlocked, Observation, OrderPageBlocked,
                          SMSControlBlocked, log_event, utc_now)
from agent_session import AgentSession, RunLog
from android_device import (TEXT_PACKAGES, AndroidController, adb_timing_key, display_has_content,
                            display_root_tasks, input_focus, locate_adb, package_is_resumed,
                            parse_package_displays, private_frame_command, private_package_tasks,
                            resumed_package, workspace_identity, workspace_is_isolated)
from app_targets import CALENDAR_PACKAGE, GMAIL_PACKAGE, SMS_PACKAGE, is_sms_goal
from planners import SYSTEM_PROMPT, MockPlanner, OpenAICompatiblePlanner, note_external_step, parse_action
from task_routing import CALENDAR_GOAL, RAILWAY_GOAL, explicit_target_package


# Names other scripts and the tests import from here.
__all__ = [
    "ALLOWED_ACTIONS", "SEND_TERMS", "SENSITIVE_TERMS", "bounded_int", "order_block_message", "require_string",
    "sensitive_reason", "validate_input_text", "HEIGHT", "ISOLATION_FLAGS", "PACKAGE", "WIDTH", "WORKSPACE_DIRECTORY",
    "WORKSPACE_STATE", "AgentError", "MailControlBlocked", "Observation", "OrderPageBlocked", "SMSControlBlocked",
    "log_event", "utc_now", "TEXT_PACKAGES", "AndroidController", "adb_timing_key", "display_has_content",
    "display_root_tasks", "input_focus", "locate_adb", "package_is_resumed", "parse_package_displays",
    "private_frame_command", "private_package_tasks", "resumed_package", "workspace_identity",
    "workspace_is_isolated", "CALENDAR_PACKAGE", "GMAIL_PACKAGE", "SMS_PACKAGE", "SYSTEM_PROMPT", "MockPlanner",
    "OpenAICompatiblePlanner", "note_external_step", "parse_action", "CALENDAR_GOAL", "RAILWAY_GOAL",
    "explicit_target_package", "time", "build_parser", "make_planner", "run", "main",
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the private-display agent loop")
    parser.add_argument("--goal", required=True, help="Natural-language task for the agent")
    parser.add_argument(
        "--provider",
        choices=("mock", "openai", "autoglm", "qwen"),
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
    parser.add_argument("--no-skills", action="store_true",
                        help="Same as --mode agent")
    parser.add_argument("--mode", choices=("experience", "agent", "legacy"), default="experience",
                        help="experience: model + experience learned from its own successful runs; "
                             "agent: the model decides every step; legacy: old hand-written skills")
    parser.add_argument("--keep-workspace", action="store_true",
                        help="Do not close apps left on the private display before the task")
    parser.add_argument("--runs-dir", type=Path, default=Path("runs"))
    return parser


def make_planner(args: argparse.Namespace) -> Any:
    if args.provider == "mock":
        return MockPlanner(args.initial_url)
    if args.provider == "autoglm":
        from autoglm_adapter import AutoGLMPlanner
        if not args.model:
            raise AgentError("--model is required for AutoGLM")
        return AutoGLMPlanner(args.base_url, args.model, args.api_key, args.timeout, WIDTH, HEIGHT)
    if args.provider == "qwen":
        from qwen_planner import QwenPlanner  # a general vision model (e.g. Qwen3.8-27B), JSON actions
        return QwenPlanner(args.base_url, args.model or "qwen3.8-27b", args.api_key, args.timeout, WIDTH, HEIGHT)
    return OpenAICompatiblePlanner(args.base_url, args.model, args.api_key, args.timeout)


def run(args: argparse.Namespace) -> int:
    if args.max_steps < 1 or args.max_steps > 50:
        raise AgentError("--max-steps must be between 1 and 50")
    mode = getattr(args, "mode", "experience")
    if args.no_skills and mode == "experience":
        mode = "agent"
    args.mode = mode
    # Hand-written skills and rule shortcuts only in the legacy comparison mode.
    args.no_skills = mode != "legacy"
    controller = AndroidController(locate_adb(), dry_run=args.dry_run)
    controller.sms_task = is_sms_goal(args.goal)
    controller.sms_allows_calendar = controller.sms_task and bool(CALENDAR_GOAL.search(args.goal))
    planner = make_planner(args)

    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = args.runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    log = RunLog(run_dir, {
        "started_at": utc_now(),
        "goal": args.goal,
        "provider": args.provider,
        "model": args.model or None,
        "base_url": args.base_url if args.provider != "mock" else None,
        "max_steps": args.max_steps,
        "mode": args.mode,
    })
    print(f"Run log: {run_dir}")
    try:
        return AgentSession(args, controller, planner, log, run_id).run()
    except AgentError as error:
        log.metadata.update({"status": "failed", "failed_at": utc_now(), "error": str(error)})
        log.save()
        raise


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
    from agent_common import use_certifi_if_needed
    use_certifi_if_needed()  # HTTPS model APIs on a python.org Python
    raise SystemExit(main())

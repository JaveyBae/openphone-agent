#!/usr/bin/env python3
"""Manual diagnostics share exactly the agent loop's isolation guards."""
import argparse
import json
import os
from datetime import datetime
from pathlib import Path

from agent_loop import AgentError, AndroidController, locate_adb
from workspace_runtime import ensure_running


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("state", "open-browser", "open-chrome", "tap", "swipe", "text", "key", "capture"))
    parser.add_argument("arguments", nargs="*")
    args = parser.parse_args()
    controller = AndroidController(locate_adb())
    values = args.arguments
    if args.command == "state":
        print(json.dumps(controller.state(), ensure_ascii=False, indent=2))
        return
    if args.command == "capture":
        directory = Path(os.environ.get("CAPTURE_DIR", "captures"))
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / ("agent-display-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".png")
        controller.capture(target)
        print(target.resolve())
        return
    if args.command in {"open-browser", "open-chrome"}:
        ensure_running()
        action, arguments = "open_url", {"url": values[0] if values else "https://example.com"}
    elif args.command == "tap" and len(values) == 2:
        action, arguments = "tap", dict(zip(("x", "y"), map(int, values)))
    elif args.command == "swipe" and len(values) in {4, 5}:
        action = "swipe"
        arguments = dict(zip(("x1", "y1", "x2", "y2", "duration_ms"), map(int, values + (["300"] if len(values) == 4 else []))))
    elif args.command == "text" and len(values) == 1:
        action, arguments = "input_text", {"text": values[0]}
    elif args.command == "key" and len(values) == 1:
        action, arguments = "key", {"keycode": int(values[0])}
    else:
        parser.error("Incorrect arguments for command")
    controller.execute({"action": action, "arguments": arguments})
    print(json.dumps(controller.last_focus_evidence, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (AgentError, RuntimeError, ValueError) as error:
        raise SystemExit(str(error))

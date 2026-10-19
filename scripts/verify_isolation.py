#!/usr/bin/env python3
"""Bounded, live UI regression: ASCII editing must leave owner focus unchanged.

Temporarily opens the APP-owned private browser, never sends its edited address
to a website. With --restore-jd, returns the private screen to JD afterward.
Owner screenshots are not captured or sent to any model.
"""
import argparse
import json
import time
from datetime import datetime
from pathlib import Path

from android_device import AndroidController, locate_adb
from workspace_runtime import ensure_running


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--restore-jd", action="store_true")
    args = parser.parse_args()
    ensure_running()
    controller = AndroidController(locate_adb())
    target = Path("runs") / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-focus-check")
    target.mkdir(parents=True, exist_ok=False)
    actions = [
        {"action": "open_url", "arguments": {"url": "https://example.com"}},
        {"action": "tap", "arguments": {"x": 310, "y": 125}},
        {"action": "key", "arguments": {"keycode": 123}},
        {"action": "input_text", "arguments": {"text": "5090"}},
    ]
    evidence = []
    for action in actions:
        controller.execute(action)
        evidence.append({"action": action, "focus": controller.last_focus_evidence})
        if action["action"] == "open_url":
            controller.capture(target / "private-before-text.png")
        time.sleep(0.3)
    observation = controller.capture(target / "private-after-text.png")
    evidence.append({"display_id": observation.display_id, "screenshot_sha256": observation.sha256})
    if args.restore_jd:
        action = {"action": "launch_app", "arguments": {"package": "com.jingdong.app.mall"}}
        controller.execute(action)
        evidence.append({"action": action, "focus": controller.last_focus_evidence})
    (target / "verification.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(target.resolve())


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Human-in-the-loop dual-display check: observe only, never touch the owner.

While a real person uses the main screen, sample global focus and each display's
focused window about twice a second until the APP's newest task ends. Nothing is
typed, tapped or captured on the owner's screen; only window/focus metadata and
the agent's run log are recorded. Start it, then submit a task from the APP.
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path

from android_device import AndroidController, input_focus, locate_adb
from concurrency_regression import APP, app_task, focus_windows
from workspace_runtime import ensure_running

AGENT_APPS = ("com.google.android.calendar", "org.fossify.calendar", "com.google.android.gm",
              "com.google.android.apps.messaging", "com.jingdong.app.mall", "com.google.android.contacts",
              "com.google.android.deskclock", "com.google.android.apps.maps")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-dir", type=Path, default=Path("runs"))
    parser.add_argument("--timeout", type=float, default=900)
    args = parser.parse_args()
    private = ensure_running()["displayId"]
    c = AndroidController(locate_adb())
    target = args.runs_dir / ("human-concurrency-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    target.mkdir(parents=True)
    previous_task = app_task(c)[0]
    started = time.time()
    samples, task_id, status, message = [], "", "", ""
    print("观察中：请在 APP 里提交任务，然后正常使用主屏。", flush=True)
    while time.time() - started < args.timeout:
        task_id, status, message = app_task(c)
        top, focused = input_focus(str(c._run("shell", "dumpsys", "input")))
        windows = focus_windows(c)
        samples.append({"t": round(time.time() - started, 2), "topDisplay": top,
                        "focusedWindowDisplays": sorted(focused), "owner": windows.get("0", ""),
                        "private": windows.get(str(private), ""), "taskStatus": status})
        if task_id and task_id != previous_task and status in ("completed", "needs_user", "failed"):
            break
        time.sleep(0.4)
    active = [s for s in samples if s["taskStatus"] in ("queued", "running")]
    owner_apps = sorted({s["owner"].split("/")[0] for s in active if s["owner"]})
    checks = {
        "task_ran_while_observed": bool(active),
        "top_focus_always_owner": all(s["topDisplay"] == 0 for s in active),
        "agent_app_never_on_owner": not any(s["owner"].startswith(AGENT_APPS) for s in active),
    }
    evidence = {"privateDisplay": private, "taskId": task_id, "taskStatus": status,
                "taskMessage": message, "ownerAppsWhileAgentWorked": owner_apps,
                "checks": checks, "samples": samples}
    (target / "evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
                                          encoding="utf-8")
    print(json.dumps({"evidence": str(target), **checks, "samplesWhileAgentWorked": len(active),
                      "ownerAppsWhileAgentWorked": owner_apps, "taskStatus": status,
                      "taskMessage": message[:200]}, ensure_ascii=False, indent=2))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())

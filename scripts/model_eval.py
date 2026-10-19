"""Offline next-step evaluation of phone models on our own successful runs.

Each item is one step of a successful run: the goal, the actions taken so far,
the private-display screenshot of that moment, and the action the run actually
took (the reference). A model sees the same goal, history and screenshot and
proposes the next action; it scores when it does what the reference did:
  tap     - inside the reference control's bounds (runs with recording.json),
            else within 60/1000 of the reference point;
  type    - same text, spaces ignored;   swipe - same direction;
  launch  - same app;                    finish - finishes.
No phone is touched. Usage:
  python3 scripts/model_eval.py --model autoglm   (AutoGLM-Phone-9B on :18085)
  python3 scripts/model_eval.py --model qwen      (Qwen3.8-27B on :18086)
  python3 scripts/model_eval.py --model qwen --elements   (plus the UI element list)
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from experience import bounds  # noqa: E402

WIDTH, HEIGHT = 720, 1280
DEFAULT_RUNS = ["20261017-003407", "20261018-185532", "20261018-184340", "20261016-164253"]


def describe(action: dict) -> str:
    a, args = action["action"], action.get("arguments", {})
    if a == "tap":
        return f"tap({args['x'] * 1000 // WIDTH},{args['y'] * 1000 // HEIGHT})"
    if a == "input_text":
        return f"type({args.get('text')!r})"
    if a == "swipe":
        return "swipe(" + ("up" if args["y2"] < args["y1"] else "down") + ")"
    if a == "launch_app":
        return f"launch({args.get('package')})"
    if a == "key":
        return "back" if args.get("keycode") == 4 else f"key({args.get('keycode')})"
    return a


def build_items(run_ids: list[str]) -> list[dict]:
    items = []
    for run_id in run_ids:
        run = ROOT / "runs" / run_id
        meta = json.loads((run / "run.json").read_text())
        recording = json.loads((run / "recording.json").read_text())["steps"] if (run / "recording.json").exists() else None
        events = [json.loads(l) for l in (run / "events.jsonl").read_text().splitlines()]
        steps = [e for e in events if e.get("step") is not None and e.get("result") in ("executed", "finished")
                 and e["action"]["action"] != "wait"]
        history: list[str] = []
        taken: list[dict] = []
        recorded = iter(recording or [])
        previous = None
        for event in steps:
            action = event["action"]
            target = None
            if recording is not None and action["action"] != "finish":
                target = (next(recorded, {}) or {}).get("target")
            key = describe(action)
            if key == previous:  # a repeated identical step is not a new decision
                continue
            previous = key
            items.append({"run": run_id, "goal": meta["goal"], "step": event["step"],
                          "screenshot": str(run / event["screenshot"]), "history": list(history),
                          "reference": action, "target": target, "taken": list(taken)})
            history.append(key)
            # What the real loop told the model after this step (e.g. "the field is focused").
            taken.append({"action": action, "private_input_focused": bool(event.get("private_input_focused"))})
    return items


def score(item: dict, predicted: dict) -> bool:
    ref, a = item["reference"], predicted.get("action")
    rargs, pargs = ref.get("arguments", {}), predicted.get("arguments", {})
    if ref["action"] == "finish":
        return a == "finish"
    if a != ref["action"]:
        return False
    if a == "tap":
        box = bounds({"bounds": (item.get("target") or {}).get("bounds")})
        if box:
            return box[0] <= pargs["x"] <= box[2] and box[1] <= pargs["y"] <= box[3]
        return (abs(pargs["x"] - rargs["x"]) * 1000 / WIDTH <= 60
                and abs(pargs["y"] - rargs["y"]) * 1000 / HEIGHT <= 60)
    if a == "input_text":
        return str(pargs.get("text", "")).replace(" ", "") == str(rargs.get("text", "")).replace(" ", "")
    if a == "swipe":
        return (pargs["y2"] < pargs["y1"]) == (rargs["y2"] < rargs["y1"])
    if a == "launch_app":
        return pargs.get("package") == rargs.get("package")
    if a == "key":
        return pargs.get("keycode") == rargs.get("keycode")
    return True  # open_url: opening the browser at all is the step


def planner_for(name: str, elements: bool):
    if name == "autoglm":
        from autoglm_adapter import AutoGLMPlanner
        return AutoGLMPlanner("http://127.0.0.1:18085/v1", "autoglm-phone-9b-multilingual", "", 120, WIDTH, HEIGHT)
    from qwen_planner import QwenPlanner
    return QwenPlanner("http://127.0.0.1:18086/v1", "qwen3.8-27b", "", 180, WIDTH, HEIGHT, use_elements=elements)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", choices=("autoglm", "qwen"), required=True)
    parser.add_argument("--elements", action="store_true", help="also give the model the UI element list")
    parser.add_argument("--runs", nargs="*", default=DEFAULT_RUNS)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    items = build_items(args.runs)
    results = []
    for item in items:
        planner = planner_for(args.model, args.elements)
        # Both models know the steps already taken, each in its own native form.
        if args.model == "autoglm":
            from planners import note_external_step
            for index, entry in enumerate(item["taken"]):
                note_external_step(planner, item["goal"], index, 2, entry["action"])
            planner.previous_sha = None
        history = [dict(entry) for entry in item["taken"]]
        observation = SimpleNamespace(path=Path(item["screenshot"]), display_id=2, sha256=item["screenshot"],
                                      nodes=None)
        started = time.perf_counter()
        try:
            predicted = planner.next_action(item["goal"], observation, history, True)
        except Exception as error:  # a format failure is a wrong answer, not a crash
            predicted = {"action": "error", "arguments": {}, "reason": str(error)[:200]}
        seconds = time.perf_counter() - started
        ok = score(item, predicted)
        results.append({"run": item["run"], "step": item["step"], "ok": ok, "seconds": round(seconds, 2),
                        "reference": describe(item["reference"]),
                        "predicted": describe(predicted) if predicted.get("action") in
                        ("tap", "input_text", "swipe", "launch_app", "key") else predicted.get("action")})
        print(f"{'✓' if ok else '✗'} {item['run']} #{item['step']:>2} ref={results[-1]['reference']:<28} "
              f"got={results[-1]['predicted']:<28} {seconds:.1f}s", flush=True)
    total = len(results)
    correct = sum(r["ok"] for r in results)
    summary = {"model": args.model + ("+elements" if args.elements else ""), "items": total, "correct": correct,
               "accuracy": round(correct / total, 3) if total else 0,
               "median_seconds": statistics.median(r["seconds"] for r in results) if results else 0}
    print(json.dumps(summary, ensure_ascii=False))
    if args.out:
        args.out.write_text(json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

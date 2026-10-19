#!/usr/bin/env python3
"""Summarize where each agent step spends its time, from a run's events.jsonl.

Usage: python3 scripts/timing_report.py runs/<run-id> [runs/<run-id> ...]
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

PHASES = ("capture", "model", "guards", "execute", "settle")


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))]


def report(run_dir: Path) -> None:
    steps = [json.loads(line) for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
             if '"timing"' in line]
    steps = [event for event in steps if event.get("timing", {}).get("phases")]
    if not steps:
        print(f"{run_dir}: no timing data (run predates instrumentation?)")
        return
    phases: dict[str, list[float]] = defaultdict(list)
    model: dict[str, list[float]] = defaultdict(list)
    adb: dict[str, list[float]] = defaultdict(lambda: [0, 0.0])
    totals = []
    for event in steps:
        timing = event["timing"]
        totals.append(sum(timing["phases"].values()))
        for name in PHASES:
            phases[name].append(timing["phases"].get(name, 0.0))
        for key, value in (timing.get("model") or {}).items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                model[key].append(value)
        for calls in timing.get("adb", {}).values():
            for key, entry in calls.items():
                adb[key][0] += entry["calls"]
                adb[key][1] += entry["seconds"]
        usage = json.loads((run_dir / event["screenshot"]).with_suffix(".model.json").read_text()).get("usage") \
            if (run_dir / event["screenshot"]).with_suffix(".model.json").exists() else None
        if usage:
            model["prompt_tokens"].append(usage.get("prompt_tokens", 0))
            model["completion_tokens"].append(usage.get("completion_tokens", 0))
            model["cached_tokens"].append((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)

    count = len(steps)
    mean_total = sum(totals) / count
    print(f"== {run_dir}  ({count} steps, mean {mean_total:.2f}s/step, p90 {percentile(totals, .9):.2f}s)")
    print(f"{'phase':<10}{'mean':>8}{'p90':>8}{'share':>8}")
    for name in PHASES:
        values = phases[name]
        mean = sum(values) / count
        print(f"{name:<10}{mean:>7.2f}s{percentile(values, .9):>7.2f}s{mean / mean_total:>8.0%}")
    if model:
        print("model detail (mean):")
        for key in ("encode_s", "http_s", "ttft_s", "decode_s", "request_bytes", "image_bytes",
                    "prompt_tokens", "cached_tokens", "completion_tokens"):
            if model.get(key):
                mean = sum(model[key]) / len(model[key])
                print(f"  {key:<18}{mean:>10.3f}" if key.endswith("_s") else f"  {key:<18}{mean:>10.0f}")
        if model.get("decode_s") and model.get("completion_tokens"):
            rate = sum(model["completion_tokens"]) / max(sum(model["decode_s"]), 1e-6)
            print(f"  {'decode tok/s':<18}{rate:>10.1f}")
        model_phase = sum(phases["model"])
        if model.get("http_s"):
            print(f"  {'retries/other_s':<18}{(model_phase - sum(model['http_s'])) / count:>10.3f}")
    print("adb per step (mean calls / seconds), top 10:")
    for key, (calls, seconds) in sorted(adb.items(), key=lambda kv: -kv[1][1])[:10]:
        print(f"  {key:<34}{calls / count:>6.1f} {seconds / count:>7.3f}s")
    print()


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__.strip())
        return 1
    for name in sys.argv[1:]:
        report(Path(name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

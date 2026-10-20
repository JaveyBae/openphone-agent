#!/usr/bin/env python3
"""A third window beside the two display previews: the model's reasoning as a scrolling terminal.

It follows the newest run's events.jsonl and prints, per model step, the chain-of-thought
(`reason`), the action taken, the result, and any feedback the guards sent back — so you can watch
*why* the agent did each thing while the two display viewers show *what* happened on screen.

    python3 scripts/reasoning_viewer.py                 # follow the live run under ./runs
    python3 scripts/reasoning_viewer.py --runs-dir runs --once   # replay the latest run and exit
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

# ANSI styling; kept minimal so a plain terminal stays readable.
DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"
CYAN, GREEN, YELLOW, RED, GREY = "\033[36m", "\033[32m", "\033[33m", "\033[31m", "\033[90m"
RESULT_STYLE = {
    "executed": (GREEN, "✓"), "finished": (GREEN, "✓ 完成"), "awaiting_user": (YELLOW, "⏸ 交给主人"),
    "blocked_unknown_control": (RED, "✗ 安全守卫拦下"), "blocked_order_page": (YELLOW, "⏸ 下单页拦下"),
    "blocked_mail_control": (RED, "✗ 邮件守卫拦下"), "blocked_permission_prompt": (YELLOW, "⏸ 权限弹窗"),
    "sms_body_missing": (YELLOW, "↻ 正文未填，退回"), "app_unavailable": (YELLOW, "↻ 应用缺失，改道"),
    "REPLANNED": (YELLOW, "↻ 退回改道"),
}


def describe_action(action: dict) -> str:
    name = action.get("action", "?")
    args = action.get("arguments", {}) or {}
    if name == "tap":
        return f"点击 ({args.get('x')},{args.get('y')})"
    if name == "input_text":
        return f"输入「{args.get('text', '')}」"
    if name == "swipe":
        return "滑动"
    if name == "key":
        code = args.get("keycode")
        return {4: "返回键", 66: "回车键"}.get(code, f"按键 {code}")
    if name == "launch_app":
        return f"打开应用 {args.get('package', '')}"
    if name == "open_url":
        return f"打开网页 {args.get('url', '')}"
    if name == "request_user":
        return "交给主人处理 (Take_over)"
    if name == "finish":
        return "声明完成"
    if name == "wait":
        return "等待"
    return name


def render_step(entry: dict) -> str | None:
    """One printable block for a model step, or None for a non-step log line."""
    action = entry.get("action")
    if not isinstance(action, dict) or "action" not in action:
        return None
    step = entry.get("step")
    header = f"{BOLD}[{(step + 1) if isinstance(step, int) else '?'}]{RESET}"
    reason = (action.get("reason") or "").strip() or f"{GREY}(本步无文字推理){RESET}"
    lines = [f"{header} {CYAN}想法：{RESET}{reason}"]
    color, label = RESULT_STYLE.get(str(entry.get("result")), (GREY, str(entry.get("result") or "")))
    lines.append(f"      {DIM}→ {describe_action(action)}{RESET}   {color}{label}{RESET}")
    told = entry.get("told")
    if isinstance(told, str) and told.strip():
        lines.append(f"      {YELLOW}⤷ 反馈：{told.strip()}{RESET}")
    return "\n".join(lines)


def latest_run(runs_dir: Path) -> Path | None:
    runs = [p for p in runs_dir.glob("20*") if (p / "events.jsonl").exists() or (p / "run.json").exists()]
    return max(runs, key=lambda p: p.stat().st_mtime, default=None)


def run_header(run_dir: Path) -> str:
    goal, mode = "(未知任务)", ""
    try:
        meta = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        goal, mode = meta.get("goal", goal), meta.get("phase") or meta.get("mode") or ""
    except (OSError, json.JSONDecodeError):
        pass
    tag = f" · {mode}" if mode else ""
    return f"\n{BOLD}━━━ {run_dir.name}{tag} ━━━{RESET}\n{GREY}任务：{goal}{RESET}\n"


def follow(runs_dir: Path, once: bool, poll: float) -> None:
    print(f"{BOLD}模型思维链窗口{RESET} {GREY}· 跟随 {runs_dir}/ 的最新任务（Ctrl-C 退出）{RESET}")
    current: Path | None = None
    offset = 0
    seen_header = False
    while True:
        run_dir = latest_run(runs_dir)
        if run_dir is None:
            if once:
                print(f"{GREY}没有可显示的任务。{RESET}")
                return
            time.sleep(poll)
            continue
        if run_dir != current:
            current, offset, seen_header = run_dir, 0, False
        events = run_dir / "events.jsonl"
        if events.exists():
            with events.open("r", encoding="utf-8") as handle:
                handle.seek(offset)
                for line in handle:
                    if not line.endswith("\n"):  # a half-written final line; read it next poll
                        break
                    offset += len(line.encode("utf-8"))
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    block = render_step(entry)
                    if block is None:
                        continue
                    if not seen_header:
                        print(run_header(run_dir))
                        seen_header = True
                    print(block)
        if once and run_dir == current and events.exists():
            # Replay mode: stop once the run has reached a terminal state.
            try:
                status = json.loads((run_dir / "run.json").read_text(encoding="utf-8")).get("status")
            except (OSError, json.JSONDecodeError):
                status = None
            if status in ("completed", "failed", "awaiting_user"):
                return
        time.sleep(poll)


def main() -> None:
    parser = argparse.ArgumentParser(description="Live view of the model's reasoning for the newest run")
    parser.add_argument("--runs-dir", default="runs", type=Path)
    parser.add_argument("--poll", default=0.5, type=float, help="seconds between checks")
    parser.add_argument("--once", action="store_true", help="replay the latest run and exit")
    args = parser.parse_args()
    try:
        follow(args.runs_dir, args.once, args.poll)
    except KeyboardInterrupt:
        print(f"\n{GREY}已退出思维链窗口。{RESET}")


if __name__ == "__main__":
    main()

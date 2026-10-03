#!/usr/bin/env python3
"""Bridge Android chat tasks to the existing private-display agent loop."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from agent_loop import PACKAGE, locate_adb


ACTION = "ai.openphone.agent.TASK_UPDATE"
RECEIVER = f"{PACKAGE}/.AgentResultReceiver"
TASK_PATH = "files/chat/pending.json"


class Bridge:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.adb = locate_adb()
        self.state_path = args.runs_dir / "chat-bridge-state.json"
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.last_task_id = self.read_state()

    def adb_run(self, *parts: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [self.adb, *parts], capture_output=True, check=check, timeout=10
        )

    def read_state(self) -> str:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            return str(payload.get("lastTaskId", ""))
        except (FileNotFoundError, json.JSONDecodeError):
            return ""

    def save_state(self, task_id: str) -> None:
        self.state_path.write_text(
            json.dumps({"lastTaskId": task_id}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def read_pending(self) -> dict[str, Any] | None:
        result = self.adb_run(
            "exec-out", "run-as", PACKAGE, "cat", TASK_PATH, check=False
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        try:
            task = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(task, dict) or not task.get("id") or not task.get("goal"):
            return None
        return task

    def update(self, task_id: str, status: str, message: str) -> None:
        encoded_message = base64.b64encode(
            message[:800].encode("utf-8")
        ).decode("ascii")
        result = self.adb_run(
            "shell",
            "am",
            "broadcast",
            "--receiver-foreground",
            "-n",
            RECEIVER,
            "-a",
            ACTION,
            "--es",
            "task_id",
            task_id,
            "--es",
            "status",
            status,
            "--es",
            "message_b64",
            encoded_message,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", "replace").strip()
            raise RuntimeError(f"Could not update Android chat: {detail}")

    def run_task(self, task: dict[str, Any]) -> None:
        task_id = str(task["id"])
        goal = str(task["goal"])
        self.update(task_id, "running", "Agent 已在私有屏幕开始执行…")

        command = [
            sys.executable,
            str(Path(__file__).with_name("agent_loop.py")),
            "--goal",
            goal,
            "--provider",
            self.args.provider,
            "--runs-dir",
            str(self.args.runs_dir),
            "--max-steps",
            str(self.args.max_steps),
            "--initial-url",
            self.args.initial_url,
        ]
        if self.args.provider == "openai":
            command += [
                "--base-url", self.args.base_url,
                "--model", self.args.model,
                "--api-key", self.args.api_key,
            ]

        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        lines: list[str] = []
        assert process.stdout is not None
        for raw in process.stdout:
            line = raw.strip()
            if not line:
                continue
            print(line, flush=True)
            lines.append(line)
            progress = progress_message(line)
            if progress:
                self.update(task_id, "running", progress)
        return_code = process.wait()
        final_line = lines[-1] if lines else "Agent 未返回详细信息"
        if return_code == 0:
            if self.args.provider == "mock":
                message = (
                    "演示任务已完成：私有屏已打开目标网页并完成独立滚动，"
                    "主屏没有被 Agent 操作。"
                )
            else:
                message = "任务已完成。" + final_line
            self.update(task_id, "completed", message)
        elif return_code == 2:
            self.update(task_id, "needs_user", final_line)
        else:
            self.update(task_id, "failed", final_line)
        self.last_task_id = task_id
        self.save_state(task_id)

    def loop(self) -> int:
        print("Chat bridge is ready. Send a task from the Android chat UI.")
        while True:
            task = self.read_pending()
            if task and str(task["id"]) != self.last_task_id:
                self.run_task(task)
                if self.args.once:
                    return 0
            elif self.args.once:
                print("No new Android chat task is pending.")
                return 0
            time.sleep(self.args.poll_interval)


def progress_message(line: str) -> str | None:
    match = re.search(r"\[\d+/\d+\]\s+(\w+):", line)
    if not match:
        return None
    labels = {
        "open_url": "正在打开目标网页…",
        "launch_app": "正在打开所需应用…",
        "tap": "正在操作页面…",
        "swipe": "正在查看更多内容…",
        "input_text": "正在输入信息…",
        "wait": "正在等待页面完成…",
        "finish": "正在整理结果…",
    }
    return labels.get(match.group(1), "Agent 正在执行…")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Run Android chat tasks on the private display"
    )
    result.add_argument(
        "--provider",
        choices=("mock", "openai"),
        default=os.environ.get("AGENT_PROVIDER", "mock"),
    )
    result.add_argument(
        "--base-url",
        default=os.environ.get("AGENT_BASE_URL", "http://127.0.0.1:11434/v1"),
    )
    result.add_argument("--model", default=os.environ.get("AGENT_MODEL", ""))
    result.add_argument("--api-key", default=os.environ.get("AGENT_API_KEY", ""))
    result.add_argument("--initial-url", default="https://www.wikipedia.org/")
    result.add_argument("--max-steps", type=int, default=10)
    result.add_argument("--poll-interval", type=float, default=0.7)
    result.add_argument("--runs-dir", type=Path, default=Path("runs"))
    result.add_argument("--once", action="store_true")
    return result


if __name__ == "__main__":
    raise SystemExit(Bridge(parser().parse_args()).loop())

#!/usr/bin/env python3
"""Bridge Android chat tasks to the existing private-display agent loop."""

from __future__ import annotations

import argparse
import base64
import fcntl
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
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
        self.last_task_id = task_id
        self.save_state(task_id)  # Never automatically replay an interrupted task after restart.
        self.update(task_id, "running", f"正在连接 {self.args.model or self.args.provider}…")

        if self.args.provider != "mock":
            for attempt in range(10):
                try:
                    request = urllib.request.Request(self.args.base_url.rstrip("/") + "/models")
                    if self.args.api_key:
                        request.add_header("Authorization", "Bearer " + self.args.api_key)
                    with urllib.request.urlopen(request, timeout=3):
                        pass
                    break
                except OSError:
                    if attempt == 0:
                        self.update(task_id, "running", "正在等待模型连接，SSH 隧道会自动重连…")
                    time.sleep(3)
            else:
                self.update(task_id, "failed", "模型连接尚未恢复，请稍后重新提交；没有执行手机操作。")
                self.last_task_id = task_id
                self.save_state(task_id)
                return

        self.update(task_id, "running", f"已连接 {self.args.model or self.args.provider}，正在读取私有屏幕…")

        command = [
            sys.executable,
            "-u",
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
        if self.args.provider != "mock":
            command += [
                "--base-url", self.args.base_url,
                "--model", self.args.model,
            ]
        if task.get("observeOnly") is True:
            command += ["--observe-only"]

        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env={**os.environ, "AGENT_API_KEY": self.args.api_key},
        )
        lines: list[str] = []
        assert process.stdout is not None
        try:
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
        finally:
            # Do not leave an orphan controller issuing ADB actions when the
            # device disappears or returning progress to the APP fails.
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            process.stdout.close()
        final_line = lines[-1] if lines else "Agent 未返回详细信息"
        summary = result_summary(lines, self.args.runs_dir) or final_line
        if return_code == 0:
            if self.args.provider == "mock":
                message = (
                    "演示任务已完成：私有屏已打开目标网页并完成独立滚动，"
                    "主屏没有被 Agent 操作。"
                )
            else:
                message = "任务已完成。\n" + summary
            self.update(task_id, "completed", message)
        elif return_code == 2:
            self.update(task_id, "needs_user", final_line)
        else:
            self.update(task_id, "failed", failure_message(final_line))
        self.last_task_id = task_id
        self.save_state(task_id)

    def loop(self) -> int:
        lock = (self.args.runs_dir / "chat-bridge.lock").open("a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another chat bridge already owns this runs directory", flush=True)
            return 1
        # Keep the descriptor alive throughout the loop.
        self.lock = lock
        print(f"Chat bridge ready: provider={self.args.provider}, model={self.args.model}. Send a task from the Android chat UI.", flush=True)
        while True:
            try:
                task = self.read_pending()
                if task and str(task["id"]) != self.last_task_id:
                    self.run_task(task)
                    if self.args.once:
                        return 0
                elif self.args.once:
                    print("No new Android chat task is pending.")
                    return 0
            except (subprocess.TimeoutExpired, subprocess.CalledProcessError, RuntimeError, OSError) as error:
                print(f"Bridge waiting for device: {error}", flush=True)
                if self.args.once:
                    return 1
                time.sleep(5)
            time.sleep(self.args.poll_interval)


def progress_message(line: str) -> str | None:
    match = re.search(r"\[\d+/\d+\]\s+(\w+):", line)
    if not match:
        return None
    labels = {
        "initialize_workspace": "正在准备独立工作区，确认不会读取主屏…",
        "open_url": "正在打开目标网页…",
        "launch_app": "正在打开所需应用…",
        "tap": "正在操作页面…",
        "swipe": "正在查看更多内容…",
        "input_text": "正在输入信息…",
        "wait": "正在等待页面完成…",
        "finish": "正在整理结果…",
    }
    return labels.get(match.group(1), "Agent 正在执行…")


def failure_message(line: str) -> str:
    """Keep startup diagnostics in the host log, with actionable chat copy."""
    if "model request/action failed:" in line:
        return "模型请求或操作格式校验失败，本步操作未执行。请重新提交任务；详细错误已保留在电脑日志。"
    if "private workspace has no verified content" in line:
        return "后台工作区没有可验证的独立内容，任务已停止以保护主屏。请检查电脑端控制器；只读任务不会自动打开页面。"
    if "private workspace initialization failed" in line:
        return "后台工作区初始化失败，尚未读取屏幕或执行任务。请检查电脑端控制器后重新提交。"
    if "private workspace changed during initialization" in line:
        return "后台工作区在初始化时被重新创建，任务已暂停。请重新提交任务。"
    if "agent display is stopped" in line or "no longer exists" in line:
        return "后台工作区已关闭，请点顶部「私有屏」重新授权，然后重新提交任务。"
    return line


def result_summary(lines: list[str], runs_dir: Path) -> str | None:
    """Read the controller's structured result instead of dropping multiline text."""
    for line in lines:
        if not line.startswith("Run log: "):
            continue
        run_path = Path(line.removeprefix("Run log: ")).resolve()
        if not run_path.is_relative_to(runs_dir.resolve()):
            return None
        try:
            payload = json.loads((run_path / "run.json").read_text(encoding="utf-8"))
            value = payload.get("summary")
            return value if isinstance(value, str) else None
        except (OSError, json.JSONDecodeError):
            return None
    return None


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Run Android chat tasks on the private display"
    )
    result.add_argument(
        "--provider",
        choices=("mock", "openai", "autoglm"),
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

#!/usr/bin/env python3
"""Bridge Android chat tasks to the existing private-display agent loop."""

from __future__ import annotations

import argparse
import base64
import fcntl
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import uuid
import urllib.request
from pathlib import Path
from typing import Any

from agent_common import PACKAGE, WORKSPACE_DIRECTORY, WORKSPACE_STATE, AgentError
from android_device import (AndroidController, locate_adb, package_is_resumed, parse_package_displays,
                            workspace_identity)
from execution_progress import parse_progress, progress_text


ACTION = "ai.openphone.agent.TASK_UPDATE"
RECEIVER = f"{PACKAGE}/.AgentResultReceiver"
TASK_PATH = "files/chat/pending.json"
TAKEOVER_PATH = "files/chat/takeover.json"
TAKEOVER_RESULT_PATH = "files/chat/takeover-result.json"
TAKEOVER_MAX_AGE_MS = 120_000
APP_LABELS = {"com.google.android.gm": "Gmail", "com.google.android.apps.messaging": "短信",
              "com.google.android.calendar": "Google 日历", "org.fossify.calendar": "离线日历",
              "com.google.android.permissioncontroller": "系统权限申请", "com.jingdong.app.mall": "京东",
              "com.taobao.taobao": "淘宝", "com.xunmeng.pinduoduo": "拼多多",
              "com.sankuai.meituan": "美团", "com.google.android.deskclock": "时钟",
              "com.google.android.apps.maps": "Google 地图", "com.google.android.contacts": "通讯录",
              PACKAGE: "Agent 浏览器"}
# Apps whose main-screen window the owner may ask the Agent to close (one tap per request).
CLOSABLE = {name for name in APP_LABELS if name not in (PACKAGE, "com.google.android.permissioncontroller")}
OWNER_BLOCK = re.compile(r"refusing to launch ([A-Za-z0-9_.]+): it already has a task on the owner's")


def package_tasks(activity_dump: str, display_id: int, package: str) -> list[int]:
    """Tasks on one display that hold an activity of this package. A task's own name
    (affinity) can differ from the package, e.g. google.android.task.calendar."""
    current, task, found = None, None, []
    for line in activity_dump.splitlines():
        if line.startswith("ActivityTaskSupervisor state:"):
            break
        display = re.match(r"\s*Display #(\d+) ", line)
        if display:
            current, task = int(display.group(1)), None
            continue
        header = re.match(r"\s*\* Task\{\S+ #(\d+) ", line)
        if header:
            task = int(header.group(1))
            continue
        activity = re.search(r"\* Hist\s+#\d+: ActivityRecord\{\S+ u\d+ ([A-Za-z0-9_.]+)/", line)
        if current == display_id and task is not None and activity and activity.group(1) == package \
                and task not in found:
            found.append(task)
    return found


def close_offer(line: str, goal: str) -> str:
    """When the target app's window sits on the main screen, offer to close it (owner's tap only)."""
    blocked = OWNER_BLOCK.search(line)
    if not blocked or blocked.group(1) not in CLOSABLE or len(goal) > 280 or re.search(r"[\]|]", goal):
        return ""
    package = blocked.group(1)
    label = APP_LABELS[package]
    return (f"\n点下面的按钮，我会关掉主屏上的{label}窗口，再继续这个任务。那个窗口里没保存的内容会丢"
            f"（Gmail 草稿通常已自动存进草稿箱）；你正在用的窗口我不会关。"
            f"[[action:关闭主屏的{label}并继续|{goal}||close:{package}]]")


def private_top_task(activity_dump: str, display_id: int) -> tuple[str, int] | None:
    """The resumed app task on the private display, as (package, task id)."""
    current = None
    for line in activity_dump.splitlines():
        if line.startswith("ActivityTaskSupervisor state:"):
            break
        match = re.match(r"\s*Display #(\d+) ", line)
        if match:
            current = int(match.group(1))
        found = re.search(r"topResumedActivity=ActivityRecord\{\S+ u\d+ ([A-Za-z0-9_.]+)/\S+ t(\d+)\}", line)
        if current == display_id and found:
            return found.group(1), int(found.group(2))
    return None


def preview_shell_command(state: dict) -> str:
    """Copy the private frame into the APP's own storage, entirely on the device.

    Only the session-scoped private frame is read; an empty read never replaces
    the last good frame. The owner's screen is never captured.
    """
    if not workspace_identity(state):
        raise AgentError("Invalid isolated workspace identity")
    frame = WORKSPACE_DIRECTORY + "/frame-" + state["session"] + ".png"
    store = ("umask 077 && mkdir -p files/preview && cat > files/preview/frame.tmp"
             " && [ -s files/preview/frame.tmp ] && mv files/preview/frame.tmp files/preview/frame.png")
    return "cat " + shlex.quote(frame) + " | run-as " + PACKAGE + " sh -c " + shlex.quote(store)


class NothingToTakeOver(AgentError):
    """The private display has no app window left to hand over."""


def valid_takeover(request: Any, handled: set[str], now_ms: int) -> bool:
    """A fresh, unhandled request written by the APP itself."""
    if not isinstance(request, dict):
        return False
    request_id, requested_at = request.get("id"), request.get("requestedAt")
    try:
        uuid.UUID(str(request_id))
    except ValueError:
        return False
    return (request_id not in handled and type(requested_at) is int
            and 0 <= now_ms - requested_at <= TAKEOVER_MAX_AGE_MS)


def stale_app_task(prefs_xml: str) -> tuple[str | None, str | None]:
    """(task id, status) when the App's last task is still queued or running."""
    task = re.search(r'<string name="task_id">([^<]+)</string>', prefs_xml)
    status = re.search(r'<string name="status">([^<]+)</string>', prefs_xml)
    if task and status and status.group(1) in ("queued", "running"):
        return task.group(1), status.group(1)
    return None, None


class Bridge:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.adb = locate_adb()
        self.state_path = args.runs_dir / "chat-bridge-state.json"
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.last_task_id = self.read_state()
        self.handled_takeovers: set[str] = set()
        self.stop_preview = threading.Event()

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

    def recover_stale_task(self) -> None:
        """A task the App still shows as queued/running while no bridge runs it (the last bridge died,
        or its final update never reached the phone) blocks every new task: close it in the App."""
        try:
            prefs = self.adb_run("shell", "run-as", PACKAGE, "cat", "shared_prefs/agent_task_updates.xml",
                                 check=False).stdout.decode("utf-8", "replace")
        except Exception:
            return
        task_id, status = stale_app_task(prefs)
        if not task_id:
            return
        pending = self.read_pending()
        if status == "queued" and pending and pending.get("id") == task_id and task_id != self.last_task_id:
            return  # a real queued task: the loop runs it next
        self.update(task_id, "failed", "上一个任务在电脑端中断了（连接断开或程序退出），没有完成。"
                                      "后台屏上可能停在中途，请看一下再决定是否重新发送。")
        print(f"Closed stale App task {task_id} ({status})", flush=True)

    def update(self, task_id: str, status: str, message: str, stage: str = "") -> None:
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
            "stage",
            stage or status,  # Empty argv is lost by adb shell's argument joining.
            "--es",
            "message_b64",
            encoded_message,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", "replace").strip()
            raise RuntimeError(f"Could not update Android chat: {detail}")

    def preview_loop(self) -> None:
        """Mirror the private frame into the APP about once per second."""
        state, checked = None, 0.0
        controller = AndroidController(self.adb)
        while not self.stop_preview.wait(1.0):
            try:
                if state is None or time.monotonic() - checked > 5:
                    raw = self.adb_run("exec-out", "cat", WORKSPACE_STATE, check=False).stdout
                    state, checked = json.loads(raw.decode("utf-8")), time.monotonic()
                # An empty private display may capture as an owner-screen mirror:
                # copy only frames of a display with verified independent content.
                if controller.private_content_state(state["displayId"]) is not True:
                    continue
                self.adb_run("shell", preview_shell_command(state), check=False)
            except (ValueError, AgentError, OSError, subprocess.SubprocessError):
                state = None  # Workspace missing or restarting: retry quietly.

    def check_takeover(self) -> None:
        raw = self.adb_run("exec-out", "run-as", PACKAGE, "cat", TAKEOVER_PATH, check=False)
        if raw.returncode != 0 or not raw.stdout.strip():
            return
        try:
            request = json.loads(raw.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not valid_takeover(request, self.handled_takeovers, int(time.time() * 1000)):
            return
        self.handled_takeovers.add(request["id"])
        empty = False
        try:
            ok, message = True, self.takeover()
        except NothingToTakeOver as error:
            ok, empty, message = False, True, "后台屏幕上已经没有页面了：" + str(error)
        except (AgentError, RuntimeError, subprocess.SubprocessError) as error:
            ok, message = False, "接管失败：" + str(error)
        print(("Takeover: " if ok else "Takeover refused: ") + message, flush=True)
        result = json.dumps({"id": request["id"], "ok": ok, "empty": empty, "message": message}, ensure_ascii=False)
        store = ("umask 077 && cat > files/chat/takeover-result.tmp && mv files/chat/takeover-result.tmp "
                 + TAKEOVER_RESULT_PATH)
        subprocess.run([self.adb, "shell", "run-as " + PACKAGE + " sh -c " + shlex.quote(store)],
                       input=result.encode("utf-8"), capture_output=True, timeout=10, check=False)

    def takeover(self) -> str:
        """Move the paused private app task to the owner's screen, on request only."""
        controller = AndroidController(self.adb)
        display = controller.require_display()
        controller.require_isolation(display)
        top = private_top_task(str(controller._run("shell", "dumpsys", "activity", "activities")), display)
        if top is None:
            raise NothingToTakeOver("可能已经接管到主屏，或任务结束时关闭了；可以在最近任务里找找。")
        package, task = top
        self.adb_run("shell", "am", "display", "move-stack", str(task), "0")
        for _ in range(10):
            time.sleep(0.3)
            if package_is_resumed(str(controller._run("shell", "dumpsys", "activity", "activities")), package, 0):
                label = APP_LABELS.get(package, package)
                return f"已把{label}交到主屏，你可以直接核对和操作；Agent 不会再碰它。"
        raise AgentError("系统没有把窗口切到主屏，请在最近任务中查看。")

    def close_owner_app(self, package: str) -> str:
        """Close the named app's tasks on the main screen, only because the owner tapped for it."""
        if package not in CLOSABLE:
            raise AgentError("这个应用不能由 Agent 关闭。")
        label = APP_LABELS[package]
        controller = AndroidController(self.adb)
        dump = str(controller._run("shell", "dumpsys", "activity", "activities"))
        if package_is_resumed(dump, package, 0):
            raise AgentError(f"{label}正在主屏前台使用，我不会关掉你正在用的窗口。")
        tasks = package_tasks(dump, 0, package)
        for task in tasks:
            self.adb_run("shell", "am", "stack", "remove", str(task), check=False)
        for _ in range(10):
            if 0 not in parse_package_displays(str(controller._run("shell", "dumpsys", "activity", "activities")), package):
                print(f"Closed {package} on display 0 at the owner's request (tasks {tasks})", flush=True)
                return f"已按你的确认关掉主屏上的{label}窗口，继续执行…"
            time.sleep(0.3)
        raise AgentError(f"主屏上的{label}窗口没能关掉，请从最近任务里手动关闭。")

    def run_task(self, task: dict[str, Any]) -> None:
        task_id = str(task["id"])
        goal = str(task["goal"])
        self.last_task_id = task_id
        self.save_state(task_id)  # Never automatically replay an interrupted task after restart.
        if task.get("kind") == "calendar_demo":
            try:
                from calendar_demo import run_calendar_demo
                summary = run_calendar_demo(task.get("calendarSpec"), self.args.runs_dir,
                    lambda message: self.update(task_id, "running", message, "calendar_preset"))
                self.update(task_id, "completed", summary)
            except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as error:
                self.update(task_id, "needs_user", "日程演示已暂停：" + str(error)
                            + "\n没有发送邀请。请核对私有屏；不要反复点击保存。")
            return
        close = task.get("closeOwnerPackage")
        if close:
            try:
                self.update(task_id, "running", self.close_owner_app(str(close)))
            except (AgentError, RuntimeError, subprocess.SubprocessError) as error:
                self.update(task_id, "failed", "没有关闭主屏窗口：" + str(error))
                return
        if self.args.provider == "preset":
            self.update(task_id, "failed", "当前为本地日程预设模式，不调用模型。请点击顶部「日程演示」；普通任务需恢复模型 API。")
            return
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
        mode = task.get("mode")
        if task.get("noSkills") is True:
            mode = "agent"
        if mode in ("experience", "agent", "legacy"):
            # experience: model + experience learned from its own runs; agent: model every step.
            command += ["--mode", mode]

        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env={**os.environ, "AGENT_API_KEY": self.args.api_key},
        )
        lines: list[str] = []
        structured_progress = False
        assert process.stdout is not None
        try:
            for raw in process.stdout:
                line = raw.strip()
                if not line:
                    continue
                print(line, flush=True)
                lines.append(line)
                event = parse_progress(line)
                if event:
                    structured_progress = True
                    self.update(task_id, "running", progress_text(event), event["stage"])
                    continue
                # Modern controllers use structured events. Never forward the
                # raw reason following an action or show it as a thought stream.
                if structured_progress:
                    continue
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
            # The full multi-line message (with any action marker) lives in run.json.
            self.update(task_id, "needs_user", run_message(lines, self.args.runs_dir)
                        or final_line.removeprefix("Paused for user: "))
        else:
            self.update(task_id, "failed", failure_message(final_line) + close_offer(final_line, goal))
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
        self.recover_stale_task()
        print(f"Chat bridge ready: provider={self.args.provider}, model={self.args.model}. Send a task from the Android chat UI.", flush=True)
        if not self.args.once:
            threading.Thread(target=self.preview_loop, name="private-preview", daemon=True).start()
        while True:
            try:
                # Between tasks only: never move a window while the agent is acting.
                self.check_takeover()
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
    if "focus isolation" in line or "isolated focus" in line or "Android 15+" in line:
        return "设备未提供独立焦点工作区，已停止操作以保护主屏。当前方案需要 Android 15+ 和已授权的 ADB 电脑连接。"
    if "owner input focus" in line or "private display has no focused window" in line:
        return "主屏的输入焦点被切到了别的屏幕，为保护主屏已停下；Agent 没有操作你的屏幕或键盘。请重新提交。"
    if "model request/action failed:" in line:
        return "模型请求或操作格式校验失败，本步操作未执行。请重新提交任务；详细错误已保留在电脑日志。"
    if "already has a task on the owner's" in line:
        return "目标应用还在主屏的最近任务中。为避免抢走你的窗口，已停止启动；请从最近任务中关闭它后重新提交。"
    if "SMS task refused" in line:
        return "模型为短信任务选错了应用，已停止切换；短信只能在 Messages 中准备草稿。"
    if "SMS safety guard" in line:
        return "短信操作遇到发送按钮或尚未识别的控件，已安全暂停；Agent 不会尝试点击发送，请人工处理后重新提交。"
    if "private workspace has no verified content" in line:
        return "后台工作区没有可验证的独立内容，为保护主屏已停下。请检查电脑端控制器；只读任务不会自动打开页面。"
    if "private workspace initialization failed" in line:
        return "后台工作区初始化失败，尚未读取屏幕或执行任务。请检查电脑端控制器后重新提交。"
    if "private workspace changed during initialization" in line:
        return "后台工作区在初始化时被重新创建，任务已暂停。请重新提交任务。"
    if "agent display is stopped" in line or "no longer exists" in line:
        return "后台工作区已关闭，请检查已授权的 ADB 电脑连接，然后重新提交任务；不需要录制主屏。"
    if "alternated between the same two actions" in line:
        return ("Agent 在两个操作之间来回重复了三轮，没有进展，已提前停下。后台页面保留在当前状态，"
                "你可以点「接管到主屏」自己完成，或换个说法重新提交。")
    if "repeated the same action" in line or "scrolled the same way" in line:
        return "Agent 连续重复同一个操作没有进展，已停下。后台页面保留在当前状态，你可以接管到主屏继续。"
    if "did not change for four steps" in line:
        return "后台页面连续几步都没有变化，Agent 已停下。你可以接管到主屏查看。"
    limit = re.search(r"configured (\d+)-step limit", line)
    if limit:
        return (f"已经用完 {limit.group(1)} 步还没完成，Agent 已停下。后台页面保留在当前状态，"
                "你可以点「接管到主屏」自己完成。")
    if "Missing private focused window" in line or "Private Unicode input failed" in line:
        return "后台输入框暂时没有就绪，这一步文字没有写进去，已停下。可以接管到主屏继续，或重新提交。"
    if "UiAutomation" in line and "already registered" in line:
        return "读取后台屏界面的通道被占用，为安全起见已停下，没有执行这一步。请重新提交任务。"
    if "model server unreachable" in line:
        return "连不上模型服务器（网络或隧道断开），本步没有执行。请稍后重新提交。"
    return re.sub(r"^Agent error:\s*", "Agent 出错：", line)


def run_message(lines: list[str], runs_dir: Path) -> str | None:
    for line in lines:
        if line.startswith("Run log: "):
            run_path = Path(line.removeprefix("Run log: ")).resolve()
            if not run_path.is_relative_to(runs_dir.resolve()):
                return None
            try:
                value = json.loads((run_path / "run.json").read_text(encoding="utf-8")).get("message")
            except (OSError, json.JSONDecodeError):
                return None
            return value if isinstance(value, str) else None
    return None


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
        choices=("mock", "openai", "autoglm", "qwen", "preset"),
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

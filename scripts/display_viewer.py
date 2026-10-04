#!/usr/bin/env python3
"""Read-only local viewer for the agent display, optionally beside the owner screen."""

from __future__ import annotations

import argparse
import io
import json
import queue
import subprocess
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from typing import Any

from PIL import Image, ImageTk

from agent_loop import PACKAGE, display_has_content, locate_adb


@dataclass(frozen=True)
class Frame:
    payload: bytes
    captured_at: float
    display_id: int


class AgentDisplayViewer:
    def __init__(
        self,
        root: tk.Tk,
        adb: str,
        scale: float,
        interval: float,
        always_on_top: bool,
        owner_screen: bool = False,
        embedded: bool = False,
    ) -> None:
        self.root = root
        self.adb = adb
        self.scale = scale
        self.interval = interval
        self.owner_screen = owner_screen
        self.running = threading.Event()
        self.running.set()
        self.frames: queue.Queue[Frame | Exception] = queue.Queue(maxsize=1)
        self.photo: ImageTk.PhotoImage | None = None
        self.source_image: Image.Image | None = None
        self.last_digest: int | None = None
        self.last_frame_time: float | None = None
        self.redraw_pending = False

        target_width = max(216, round(720 * scale))
        target_height = max(384, round(1280 * scale))

        root.configure(background="#0f172a")
        if not embedded:
            root.title("OpenPhone · Agent Display")
            root.geometry(f"{target_width}x{target_height + 36}")
            root.minsize(270, 516)
            root.resizable(True, True)
            root.attributes("-topmost", always_on_top)
            root.protocol("WM_DELETE_WINDOW", self.close)

        self.status = tk.StringVar(value="Connecting…")
        status_bar = tk.Label(
            root,
            textvariable=self.status,
            anchor="w",
            padx=12,
            pady=8,
            foreground="#e2e8f0",
            background="#1e293b",
            font=("Menlo", 11),
        )
        status_bar.pack(side="bottom", fill="x")

        self.canvas = tk.Canvas(
            root,
            background="#0f172a",
            highlightthickness=0,
            borderwidth=0,
        )
        self.canvas.pack(side="top", fill="both", expand=True)
        self.canvas.create_text(
            target_width / 2,
            target_height / 2,
            text="Connecting to owner screen…" if owner_screen else "Waiting for the private display…",
            fill="#cbd5e1",
            font=("Helvetica", 15),
            tags=("waiting",),
        )
        root.bind("<Configure>", self.schedule_redraw)

        self.worker = threading.Thread(target=self.pull_frames, daemon=True)
        self.worker.start()
        self.root.after(50, self.render_latest)

    def adb_run(self, *args: str) -> bytes:
        result = subprocess.run(
            [self.adb, *args], capture_output=True, check=False, timeout=5
        )
        if result.returncode != 0:
            message = result.stderr.decode("utf-8", "replace").strip()
            raise RuntimeError(message or "ADB request failed")
        return result.stdout

    def read_display_id(self) -> int:
        payload = self.adb_run(
            "shell", "run-as", PACKAGE, "cat", "files/display-state.json"
        )
        state: dict[str, Any] = json.loads(payload.decode("utf-8"))
        display_id = int(state.get("displayId", -1))
        if display_id <= 0:
            raise RuntimeError("Agent display is stopped; tap Start agent display")
        displays = self.adb_run("shell", "cmd", "display", "get-displays")
        if f"Display id {display_id}:".encode() not in displays:
            raise RuntimeError(
                f"Agent display {display_id} expired; tap Start agent display again"
            )
        dump = self.adb_run("shell", "dumpsys", "display").decode("utf-8", "replace")
        if display_has_content(dump, display_id) is not True:
            raise RuntimeError("私有屏尚未初始化；请运行 scripts/agentctl.sh open-browser")
        return display_id

    def pull_frames(self) -> None:
        while self.running.is_set():
            try:
                item: Frame | Exception = self.read_frame()
            except Exception as error:  # surfaced in the viewer status bar
                item = error
            try:
                if self.frames.full():
                    self.frames.get_nowait()
                self.frames.put_nowait(item)
            except queue.Empty:
                pass
            time.sleep(self.interval)

    def read_frame(self) -> Frame:
        if self.owner_screen:
            display_id = 0
            # Local preview only: this frame is never passed to a planner.
            payload = self.adb_run("exec-out", "screencap", "-p")
        else:
            display_id = self.read_display_id()
            payload = self.adb_run(
                "exec-out", "run-as", PACKAGE, "cat", "files/display/latest.png",
            )
        if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
            raise RuntimeError("The display has not produced a frame yet")
        return Frame(payload, time.monotonic(), display_id)

    def render_latest(self) -> None:
        latest: Frame | Exception | None = None
        try:
            while True:
                latest = self.frames.get_nowait()
        except queue.Empty:
            pass

        if isinstance(latest, Exception):
            self.status.set(f"Disconnected · {latest}")
            self.source_image = None
            self.last_digest = None
            self.photo = None
            self.canvas.delete("frame")
            self.canvas.delete("waiting")
            self.canvas.create_text(
                self.canvas.winfo_width() / 2, self.canvas.winfo_height() / 2,
                text="工作区未连接\n请在 APP 中点击「私有屏」重新授权",
                width=max(100, self.canvas.winfo_width() - 30), fill="#cbd5e1",
                font=("Helvetica", 16), justify="center", tags=("waiting",),
            )
        elif isinstance(latest, Frame):
            digest = hash(latest.payload)
            if digest != self.last_digest:
                self.source_image = Image.open(io.BytesIO(latest.payload)).convert("RGB")
                self.last_digest = digest
                self.draw_frame()
            self.last_frame_time = latest.captured_at
            age = (
                0.0
                if self.last_frame_time is None
                else max(0.0, time.monotonic() - self.last_frame_time)
            )
            dimensions = f"{self.source_image.width}×{self.source_image.height}"
            self.status.set(f"Display {latest.display_id} · {dimensions} · read only · received {age:.1f}s ago")

        if self.running.is_set():
            self.root.after(100, self.render_latest)

    def schedule_redraw(self, _event: tk.Event[Any] | None = None) -> None:
        if self.redraw_pending:
            return
        self.redraw_pending = True
        self.root.after_idle(self.draw_frame)

    def draw_frame(self) -> None:
        self.redraw_pending = False
        if self.source_image is None:
            return
        canvas_width = max(1, self.canvas.winfo_width())
        canvas_height = max(1, self.canvas.winfo_height())
        fit_scale = min(
            canvas_width / self.source_image.width,
            canvas_height / self.source_image.height,
        )
        target = (
            max(1, round(self.source_image.width * fit_scale)),
            max(1, round(self.source_image.height * fit_scale)),
        )
        rendered = self.source_image.resize(target, Image.Resampling.LANCZOS)
        self.photo = ImageTk.PhotoImage(rendered)
        self.canvas.delete("frame")
        self.canvas.delete("waiting")
        self.canvas.create_image(
            canvas_width / 2,
            canvas_height / 2,
            image=self.photo,
            anchor="center",
            tags=("frame",),
        )

    def close(self) -> None:
        self.running.clear()
        self.root.destroy()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Open a read-only live window for the private agent display"
    )
    parser.add_argument(
        "--scale",
        default="auto",
        help="Initial window scale (0.2–1.5) or auto; default auto",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=0.5,
        help="ADB frame polling interval in seconds; default 0.5",
    )
    parser.add_argument("--always-on-top", action="store_true")
    parser.add_argument("--both", action="store_true", help="Show owner screen and private agent screen side by side; local read-only previews")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not 0.2 <= args.interval <= 5:
        raise SystemExit("--interval must be between 0.2 and 5")
    root = tk.Tk()
    if args.scale == "auto":
        scale = min(0.8, max(0.4, (root.winfo_screenheight() - 140) / 1280))
    else:
        try:
            scale = float(args.scale)
        except ValueError as error:
            raise SystemExit("--scale must be 'auto' or a number") from error
        if not 0.2 <= scale <= 1.5:
            raise SystemExit("--scale must be between 0.2 and 1.5")
    adb = locate_adb()
    if args.both:
        root.title("OpenPhone · 主屏 + Agent 私有屏")
        root.configure(background="#0f172a")
        height = min(root.winfo_screenheight() - 120, round(1280 * scale) + 100)
        width = min(root.winfo_screenwidth() - 80, round(height * 1.05))
        root.geometry(f"{width}x{height}+40+40")
        root.minsize(640, 500)
        root.attributes("-topmost", args.always_on_top)
        panels = tk.Frame(root, background="#0f172a")
        panels.pack(fill="both", expand=True, padx=12, pady=12)
        viewers = []
        for column, (title, owner) in enumerate((("主屏 · Display 0 · 用户使用", True), ("Agent 私有屏 · 后台工作区", False))):
            panels.columnconfigure(column, weight=1, uniform="phone")
            pane = tk.Frame(panels, background="#0f172a")
            pane.grid(row=0, column=column, sticky="nsew", padx=6)
            tk.Label(pane, text=title, background="#0f172a", foreground="#f8fafc", font=("Helvetica", 15, "bold"), pady=10).pack(fill="x")
            body = tk.Frame(pane, background="#0f172a")
            body.pack(fill="both", expand=True)
            viewers.append(AgentDisplayViewer(body, adb, scale, args.interval, False, owner_screen=owner, embedded=True))
        panels.rowconfigure(0, weight=1)
        tk.Label(root, text="两侧均为实时只读预览；请在 Android Studio 中操作主屏。主屏画面仅在本机显示，不发送给模型。", background="#0f172a", foreground="#94a3b8", pady=8).pack(fill="x")
        def close_both():
            for viewer in viewers:
                viewer.running.clear()
            root.destroy()
        root.protocol("WM_DELETE_WINDOW", close_both)
    else:
        AgentDisplayViewer(root, adb, scale, args.interval, args.always_on_top)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

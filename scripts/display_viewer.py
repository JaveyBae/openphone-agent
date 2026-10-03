#!/usr/bin/env python3
"""Read-only live viewer for the private Android agent display."""

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

from agent_loop import PACKAGE, locate_adb


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
    ) -> None:
        self.root = root
        self.adb = adb
        self.scale = scale
        self.interval = interval
        self.running = threading.Event()
        self.running.set()
        self.frames: queue.Queue[Frame | Exception] = queue.Queue(maxsize=1)
        self.photo: ImageTk.PhotoImage | None = None
        self.last_digest: int | None = None
        self.last_frame_time: float | None = None

        root.title("OpenPhone · Agent Display")
        root.configure(background="#0f172a")
        root.resizable(False, False)
        root.attributes("-topmost", always_on_top)
        root.protocol("WM_DELETE_WINDOW", self.close)

        self.canvas = tk.Label(
            root,
            text="Waiting for the private display…",
            foreground="#cbd5e1",
            background="#0f172a",
            font=("Helvetica", 15),
            width=max(1, int(720 * scale / 8)),
            height=max(1, int(1280 * scale / 16)),
        )
        self.canvas.pack()

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
        status_bar.pack(fill="x")

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
        if display_id < 0:
            raise RuntimeError("Agent display is stopped; tap Start agent display")
        return display_id

    def pull_frames(self) -> None:
        while self.running.is_set():
            try:
                display_id = self.read_display_id()
                payload = self.adb_run(
                    "exec-out",
                    "run-as",
                    PACKAGE,
                    "cat",
                    "files/display/latest.png",
                )
                if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
                    raise RuntimeError("The agent display has not produced a frame yet")
                item: Frame | Exception = Frame(payload, time.monotonic(), display_id)
            except Exception as error:  # surfaced in the viewer status bar
                item = error
            try:
                if self.frames.full():
                    self.frames.get_nowait()
                self.frames.put_nowait(item)
            except queue.Empty:
                pass
            time.sleep(self.interval)

    def render_latest(self) -> None:
        latest: Frame | Exception | None = None
        try:
            while True:
                latest = self.frames.get_nowait()
        except queue.Empty:
            pass

        if isinstance(latest, Exception):
            self.status.set(f"Disconnected · {latest}")
        elif isinstance(latest, Frame):
            digest = hash(latest.payload)
            if digest != self.last_digest:
                image = Image.open(io.BytesIO(latest.payload)).convert("RGB")
                target = (
                    max(1, round(image.width * self.scale)),
                    max(1, round(image.height * self.scale)),
                )
                image = image.resize(target, Image.Resampling.LANCZOS)
                self.photo = ImageTk.PhotoImage(image)
                self.canvas.configure(image=self.photo, text="", width=target[0], height=target[1])
                self.last_digest = digest
                self.last_frame_time = latest.captured_at
            age = (
                0.0
                if self.last_frame_time is None
                else max(0.0, time.monotonic() - self.last_frame_time)
            )
            self.status.set(
                f"Display {latest.display_id} · 720×1280 · read only · frame age {age:.1f}s"
            )

        if self.running.is_set():
            self.root.after(100, self.render_latest)

    def close(self) -> None:
        self.running.clear()
        self.root.destroy()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Open a read-only live window for the private agent display"
    )
    parser.add_argument(
        "--scale", type=float, default=0.8, help="Window scale; default 0.8"
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=0.5,
        help="ADB frame polling interval in seconds; default 0.5",
    )
    parser.add_argument("--always-on-top", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not 0.2 <= args.scale <= 1.5:
        raise SystemExit("--scale must be between 0.2 and 1.5")
    if not 0.2 <= args.interval <= 5:
        raise SystemExit("--interval must be between 0.2 and 5")
    root = tk.Tk()
    AgentDisplayViewer(
        root,
        locate_adb(),
        args.scale,
        args.interval,
        args.always_on_top,
    )
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

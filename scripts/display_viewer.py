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
        self.source_image: Image.Image | None = None
        self.last_digest: int | None = None
        self.last_frame_time: float | None = None
        self.redraw_pending = False

        target_width = max(216, round(720 * scale))
        target_height = max(384, round(1280 * scale))

        root.title("OpenPhone · Agent Display")
        root.configure(background="#0f172a")
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
            text="Waiting for the private display…",
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
                self.source_image = Image.open(io.BytesIO(latest.payload)).convert("RGB")
                self.last_digest = digest
                self.last_frame_time = latest.captured_at
                self.draw_frame()
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
    AgentDisplayViewer(
        root,
        locate_adb(),
        scale,
        args.interval,
        args.always_on_top,
    )
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

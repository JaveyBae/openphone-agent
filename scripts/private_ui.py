"""Identity-based control access on the private display, for skill replay.

Wraps the device-side PrivateUi helper: every call re-checks the private
session, isolation and owner focus before and after, like CalendarUiClient.
"""
from __future__ import annotations

import base64
import json
import select
import shlex
import subprocess
import sys

from agent_common import AgentError
from android_device import AndroidController, locate_adb
from workspace_runtime import install_text_helper


class ResidentChannel:
    """One long-lived PrivateUi process on the device, fed over this adb session only.

    Closing stdin (or killing the adb process) makes the device side exit, so
    nothing keeps running after the task. A command whose reply never arrives
    is NOT resent: its effect is unknown, so the caller treats it as failed.
    """

    def __init__(self, adb: str, jar: str, display: int, session: str, timeout: float = 20):
        command = ("CLASSPATH=" + shlex.quote(jar) + " app_process / ai.openphone.workspace.PrivateUi "
                   + " ".join(map(shlex.quote, (str(display), session, "serve"))))
        self.timeout = timeout
        self.process = subprocess.Popen([adb, "shell", command], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1)
        ready = self._read()
        if ready.get("ready") is not True or ready.get("displayId") != display or ready.get("session") != session:
            self.close()
            raise AgentError("resident private UI did not start: " + str(ready.get("error", "identity mismatch")))

    def _read(self) -> dict:
        while True:
            readable, _, _ = select.select([self.process.stdout], [], [], self.timeout)
            if not readable:
                self.close()
                raise AgentError("resident private UI timed out")
            line = self.process.stdout.readline()
            if not line:
                self.close()
                raise AgentError("resident private UI exited")
            if line.lstrip().startswith("{"):  # skip any runtime noise before the JSON reply
                try:
                    return json.loads(line)
                except json.JSONDecodeError as error:
                    self.close()
                    raise AgentError("resident private UI sent invalid output") from error

    def send(self, command: dict) -> dict:
        if self.process.poll() is not None:
            raise AgentError("resident private UI is not running")
        try:
            self.process.stdin.write(json.dumps(command, ensure_ascii=False) + "\n")
            self.process.stdin.flush()
        except OSError as error:
            self.close()
            raise AgentError("resident private UI connection lost") from error
        return self._read()

    def close(self) -> None:
        if self.process.poll() is None:
            try:
                self.process.stdin.close()  # EOF: the device side exits by itself
                self.process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                self.process.kill()
                self.process.wait()


class PrivateUiClient:
    def __init__(self, controller: AndroidController, package: str, jar: str | None = None,
                 resident: bool = False):
        self.c, self.package = controller, package
        self.jar = jar or install_text_helper()
        self.state = controller.state()
        self.display = controller.require_display()
        self.channel = None
        if resident:
            try:
                self.channel = ResidentChannel(controller.adb, self.jar, self.display, self.state["session"])
            except (AgentError, OSError):
                self.channel = None  # fall back to one process per operation

    def close(self) -> None:
        if self.channel is not None:
            self.channel.close()
            self.channel = None

    def call(self, mode: str, selector: dict | None = None, text: str | None = None) -> dict:
        if self.c.state() != self.state:
            raise AgentError("private workspace changed; skill stopped")
        self.c.require_isolation(self.display)
        before = self.c.require_owner_focus(self.display, private_window=True)
        if self.channel is not None:
            try:
                result = self.channel.send({"app": self.package, "mode": mode, "selector": selector, "text": text})
            except AgentError:
                # Unknown whether it ran: never resend; later calls use one-shot processes.
                self.close()
                raise
        else:
            result = self.one_shot(mode, selector, text)
        return self.check(mode, result, before)

    def one_shot(self, mode: str, selector: dict | None, text: str | None) -> dict:
        parts = [str(self.display), self.state["session"], self.package, mode]
        for value in ([json.dumps(selector, ensure_ascii=False)] if selector is not None else []) \
                + ([text] if text is not None else []):
            parts.append(base64.b64encode(value.encode("utf-8")).decode("ascii"))
        command = ("CLASSPATH=" + shlex.quote(self.jar) + " app_process / ai.openphone.workspace.PrivateUi "
                   + " ".join(map(shlex.quote, parts)))
        return json.loads(str(self.c._run("shell", command)))

    def check(self, mode: str, result: dict, before: dict) -> dict:
        if (not isinstance(result, dict) or result.get("ok") is not True or result.get("verified") is not True
                or result.get("displayId") != self.display or result.get("session") != self.state["session"]
                or result.get("package") != self.package):
            raise AgentError("private UI step failed: " + str(result.get("error", "identity mismatch")
                             if isinstance(result, dict) else "invalid response"))
        if mode == "text" and result.get("readback") is not True:
            raise AgentError("private text readback failed")
        result["focus"] = {"before": before,
                           "after": self.c.settled_owner_focus(self.display, private_window=True)}
        return result

    def snapshot(self) -> list[dict]:
        return self.call("snapshot")["nodes"]


if __name__ == "__main__":
    # Exploration aid: print the labelled controls of the private app window.
    client = PrivateUiClient(AndroidController(locate_adb()), sys.argv[1])
    for node in client.snapshot():
        label = node.get("text") or node.get("description")
        if label or node.get("editable") or node.get("clickable"):
            flags = "".join(f for f, k in (("C", "clickable"), ("E", "editable"), ("F", "focused")) if node.get(k))
            print(f"{flags:3} {node['bounds']:22} id={str(node.get('id')).split(':id/')[-1]:28} "
                  f"text={node.get('text')!r} desc={node.get('description')!r}")

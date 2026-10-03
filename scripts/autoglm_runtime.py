#!/usr/bin/env python3
"""Manage durable macOS user services for the SSH tunnel and Android chat bridge."""
from __future__ import annotations

import argparse
import os
import plistlib
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LABELS = ("ai.openphone.autoglm.tunnel", "ai.openphone.autoglm.bridge")


def definitions():
    logs = ROOT / "runs" / "runtime"
    python = str(Path(sys.executable).resolve())
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
        "ADB": str(Path.home() / "Library/Android/sdk/platform-tools/adb"),
        "ANDROID_SERIAL": "emulator-5554",
        "PYTHONUNBUFFERED": "1",
    }
    commands = [
        ["/usr/bin/ssh", "-N", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
         "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=15",
         "-o", "ServerAliveCountMax=2", "-L", "127.0.0.1:18085:127.0.0.1:18085", "n30285"],
        [python, "-u", str(ROOT / "scripts/chat_bridge.py"), "--provider", "autoglm",
         "--base-url", "http://127.0.0.1:18085/v1", "--model", "autoglm-phone-9b-multilingual",
         "--runs-dir", str(ROOT / "runs"), "--max-steps", "15"],
    ]
    for label, command in zip(LABELS, commands):
        yield label, {
            "Label": label, "ProgramArguments": command, "WorkingDirectory": str(ROOT),
            "EnvironmentVariables": environment, "RunAtLoad": True, "KeepAlive": True,
            "ThrottleInterval": 10, "ExitTimeOut": 10,
            "StandardOutPath": str(logs / (label + ".log")),
            "StandardErrorPath": str(logs / (label + ".error.log")),
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("start", "stop", "status"))
    args = parser.parse_args()
    domain = f"gui/{os.getuid()}"
    directory = Path.home() / "Library/LaunchAgents"
    if args.command == "start":
        # macOS may deny background ssh access to a Desktop identity file.
        # Load it through the initiating terminal's authorized SSH agent instead.
        config = subprocess.run(["ssh", "-G", "n30285"], capture_output=True, text=True, check=True)
        for line in config.stdout.splitlines():
            if line.startswith("identityfile "):
                identity = Path(line.split(" ", 1)[1]).expanduser()
                if identity.is_file():
                    subprocess.run(["ssh-add", str(identity)], check=True)
                    break
        directory.mkdir(parents=True, exist_ok=True)
        (ROOT / "runs/runtime").mkdir(parents=True, exist_ok=True)
        for label, definition in definitions():
            subprocess.run(["launchctl", "enable", domain + "/" + label], check=True)
            target = directory / (label + ".plist")
            existing = subprocess.run(["launchctl", "print", domain + "/" + label], capture_output=True)
            if existing.returncode == 0:
                print(f"Already loaded: {label}; use stop then start to reload configuration")
                continue
            if target.exists() and plistlib.loads(target.read_bytes()).get("WorkingDirectory") != str(ROOT):
                raise RuntimeError(f"Refusing to overwrite another workspace's service: {target}")
            target.write_bytes(plistlib.dumps(definition))
            subprocess.run(["launchctl", "bootstrap", domain, str(target)], check=True)
            print(f"Started {label}")
    elif args.command == "stop":
        for label in reversed(LABELS):
            subprocess.run(["launchctl", "disable", domain + "/" + label], check=True)
            subprocess.run(["launchctl", "bootout", domain + "/" + label], check=False)
        print("Services stopped and disabled until the next explicit start; saved plists remain.")
    else:
        for label in LABELS:
            status = subprocess.run(["launchctl", "print", domain + "/" + label], capture_output=True, text=True)
            print(label)
            if status.returncode:
                print("  not loaded")
            else:
                for line in status.stdout.splitlines():
                    if any(marker in line for marker in ("state =", "pid =", "last exit code =")):
                        print(" ", line.strip())
        try:
            with urllib.request.urlopen("http://127.0.0.1:18085/v1/models", timeout=10) as response:
                print("Model API reachable:", response.read().decode())
        except OSError as error:
            print("Model API unavailable:", error)


if __name__ == "__main__":
    main()

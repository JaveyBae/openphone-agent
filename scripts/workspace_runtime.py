#!/usr/bin/env python3
"""Build/start the private-focus workspace using the existing authorized ADB shell."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shlex
import subprocess
import time
from pathlib import Path

from android_device import locate_adb, workspace_is_isolated

ROOT = Path(__file__).resolve().parents[1]
DEVICE_DIRECTORY = "/data/local/tmp/openphone-workspace"
STATE_PATH = DEVICE_DIRECTORY + "/state.json"
SERVER_CLASS = "ai.openphone.workspace.WorkspaceServer"


def adb_command(*args: str, check: bool = True):
    return subprocess.run([locate_adb(), *args], capture_output=True, text=True,
                          timeout=20, check=check)


def read_state() -> dict | None:
    result = adb_command("shell", "cat", STATE_PATH, check=False)
    try:
        return json.loads(result.stdout) if result.returncode == 0 else None
    except json.JSONDecodeError:
        return None


def live_state() -> dict | None:
    state = read_state()
    if not isinstance(state, dict):
        return None
    displays = adb_command("shell", "cmd", "display", "get-displays").stdout
    dump = adb_command("shell", "dumpsys", "display").stdout
    return state if workspace_is_isolated(state, displays, dump) else None


def build_server() -> Path:
    sdk = Path(os.environ.get("ANDROID_HOME", str(Path.home() / "Library/Android/sdk")))
    jdk = Path(os.environ.get("JAVA_HOME", "/Applications/Android Studio.app/Contents/jbr/Contents/Home"))
    android_jar = sdk / "platforms/android-35/android.jar"
    d8 = sdk / "build-tools/35.0.0/d8"
    if not android_jar.is_file() or not d8.is_file() or not (jdk / "bin/javac").is_file():
        raise RuntimeError("Workspace build needs Android SDK 35/build-tools 35.0.0 and a JDK")
    build = ROOT / "runs/workspace-build"
    classes, dex = build / "classes", build / "dex"
    classes.mkdir(parents=True, exist_ok=True)
    dex.mkdir(parents=True, exist_ok=True)
    sources = sorted((ROOT / "tools/workspace/src/ai/openphone/workspace").glob("*.java"))
    target = build / "workspace-server.jar"
    digest = hashlib.sha256(b"".join(p.name.encode() + b"\0" + p.read_bytes() for p in sources)).hexdigest()
    stamp = build / "sources.sha256"
    if target.is_file() and stamp.is_file() and stamp.read_text().strip() == digest:
        return target  # sources unchanged since the last successful build
    subprocess.run([str(jdk / "bin/javac"), "-source", "8", "-target", "8",
                    "-classpath", str(android_jar), "-d", str(classes), *map(str, sources)], check=True)
    compiled = build / "classes.jar"
    subprocess.run([str(jdk / "bin/jar"), "cf", str(compiled), "-C", str(classes), "."], check=True)
    subprocess.run([str(d8), "--min-api", "35", "--lib", str(android_jar),
                    "--output", str(dex), str(compiled)], check=True,
                   env={**os.environ, "JAVA_HOME": str(jdk)})
    subprocess.run([str(jdk / "bin/jar"), "cf", str(target), "-C", str(dex), "classes.dex"], check=True)
    stamp.write_text(digest + "\n")
    return target


def install_text_helper() -> str:
    """Update the JAR without restarting the live display or its app tasks."""
    target = build_server()
    device_jar = DEVICE_DIRECTORY + "/private-text.jar"
    # Re-push unless the device copy is byte-identical (checked every time).
    local = hashlib.sha256(target.read_bytes()).hexdigest()
    remote = adb_command("shell", "sha256sum", device_jar, check=False).stdout.split()
    if not remote or remote[0] != local:
        adb_command("push", str(target), device_jar)
    return device_jar


def notify_app(state: dict) -> None:
    adb_command("shell", "am", "broadcast", "--receiver-foreground",
                "-n", "ai.openphone.agent/.AgentResultReceiver",
                "-a", "ai.openphone.agent.WORKSPACE_UPDATE",
                "--ei", "display_id", str(state["displayId"]), check=False)


def ensure_running() -> dict:
    lock_path = ROOT / "runs/workspace-runtime.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = live_state()
        if state:
            notify_app(state)
            return state
        sdk = adb_command("shell", "getprop", "ro.build.version.sdk").stdout.strip()
        if not sdk.isdecimal() or int(sdk) < 35:
            raise RuntimeError("Isolated workspace currently requires Android 15+; legacy displays are read-only")
        target = build_server()
        adb_command("shell", "mkdir", "-p", DEVICE_DIRECTORY)
        adb_command("shell", "chmod", "700", DEVICE_DIRECTORY)
        device_jar = DEVICE_DIRECTORY + "/workspace-server.jar"
        adb_command("push", str(target), device_jar)
        # Detach on the device, not merely on the host. An attached adb shell
        # receives SIGHUP when its host process/session exits and loses the VD.
        adb_command("shell", device_launch_command(device_jar))
        for _ in range(30):
            time.sleep(0.2)
            state = live_state()
            if state:
                notify_app(state)
                return state
        raise RuntimeError("Workspace did not start with isolated focus; inspect " + DEVICE_DIRECTORY + "/workspace.log")


def device_launch_command(device_jar: str) -> str:
    return ("CLASSPATH=" + shlex.quote(device_jar)
            + " nohup app_process / " + shlex.quote(SERVER_CLASS)
            + " >" + shlex.quote(DEVICE_DIRECTORY + "/workspace.log")
            + " 2>&1 </dev/null &")


def stop() -> None:
    state = live_state()
    if not state:
        print("No live isolated workspace")
        return
    pid = state.get("pid")
    if type(pid) is not int or pid <= 1:
        raise RuntimeError("Invalid workspace process ID")
    cmdline = adb_command("shell", "cat", f"/proc/{pid}/cmdline").stdout
    if SERVER_CLASS not in cmdline:
        raise RuntimeError("Refusing to stop a different process")
    adb_command("shell", "kill", "-TERM", str(pid))
    print("Stopped isolated workspace; source/build artifacts and logs remain")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("start", "status", "stop"))
    args = parser.parse_args()
    if args.command == "start":
        print(json.dumps(ensure_running(), ensure_ascii=False, indent=2))
    elif args.command == "status":
        print(json.dumps(live_state(), ensure_ascii=False, indent=2))
    else:
        stop()

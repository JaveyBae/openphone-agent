"""Shared constants, errors and the run-log helpers of the agent."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE = "ai.openphone.agent"
WIDTH = 720
HEIGHT = 1280
WORKSPACE_DIRECTORY = "/data/local/tmp/openphone-workspace"
WORKSPACE_STATE = WORKSPACE_DIRECTORY + "/state.json"
ISOLATION_FLAGS = ("FLAG_TRUSTED", "FLAG_OWN_FOCUS", "FLAG_STEAL_TOP_FOCUS_DISABLED", "FLAG_OWN_CONTENT_ONLY", "FLAG_DESTROY_CONTENT_ON_REMOVAL")


class AgentError(RuntimeError):
    pass


class SMSControlBlocked(AgentError):
    """A verified private unknown control was rejected WITHOUT executing it."""
    pass


class MailControlBlocked(AgentError):
    """A Gmail send/delete/archive gesture was rejected WITHOUT executing it."""
    pass


class OrderPageBlocked(AgentError):
    """A 美团 page that can place or pay for an order: nothing on it is executed."""
    pass


@dataclass(frozen=True)
class Observation:
    path: Path
    sha256: str
    display_id: int


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_event(path: Path, event: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False) + "\n")

"""Per-app safety guards that run on the device before an action executes.

Word checks (action_safety.py) read what the model SAYS; these guards read what is
really on the private display. They ask the shell helper (PrivateTextInput) about the
control under a tap or the whole page, and stop with a dedicated error before anything
happens:

  Messages   only inspected, allowlisted controls are clicked (never Send); Space/Enter refused
  Gmail      send/delete/archive controls and sideways swipes refused
  orders     美团 / 12306 / the Agent browser on booking sites: any page showing order, pay,
             agreement, login or cookie-consent wording stops every action. The page check
             (~2 s) runs once per page: the session starts it while the model is thinking
             (prefetch_order_check) and an action reuses it only if the frame is unchanged
  permissions  a system permission dialog is never answered (allow or deny is the owner's call)

AndroidController inherits these; they use its _run, state, require_isolation and
require_owner_focus, and keep the order of those device calls fixed (the tests pin it).
"""

from __future__ import annotations

import json
import re
import shlex
import threading
from typing import Any

from agent_common import (PACKAGE, WORKSPACE_DIRECTORY, AgentError, MailControlBlocked, OrderPageBlocked,
                          SMSControlBlocked)
from app_targets import CALENDAR_PACKAGE, GMAIL_PACKAGE, MEITUAN_PACKAGE, RAILWAY_PACKAGE, SMS_PACKAGE

TOUCH_ACTIONS = {"tap", "swipe", "input_text", "key"}
# Apps whose launch turns on the order-page check for the rest of the task.
ORDER_PAGE_APPS = (MEITUAN_PACKAGE, RAILWAY_PACKAGE)
# Booking sites opened in the Agent browser get the same page check.
ORDER_PAGE_URLS = re.compile(r"^https://www\.google\.com/travel/")
PERMISSION_PACKAGES = ("com.google.android.permissioncontroller", "com.android.permissioncontroller")
HELPER_CLASS = "ai.openphone.workspace.PrivateTextInput"


class AppGuards:
    """Mixed into AndroidController: guards selected by the app resumed on the private display."""

    sms_task = False
    sms_allows_calendar = False  # "look up the meeting, then text": Calendar may be read too
    order_guard = False
    PERMISSION_PACKAGES = PERMISSION_PACKAGES
    # (package, frame fingerprint) -> None (page allowed) or the OrderPageBlocked it raised
    _order_checks: dict | None = None
    _order_prefetch: threading.Thread | None = None

    # ---- facts about the private display ------------------------------------------

    def resumed_on(self, package: str, display_id: int) -> bool:
        from android_device import package_is_resumed
        return package_is_resumed(str(self._run("shell", "dumpsys", "activity", "activities")), package, display_id)

    def sms_is_resumed(self, display_id: int) -> bool:
        return self.resumed_on(SMS_PACKAGE, display_id)

    def mail_is_resumed(self, display_id: int) -> bool:
        return self.resumed_on(GMAIL_PACKAGE, display_id)

    def permission_prompt_open(self, display_id: int) -> bool:
        """A system permission dialog on the private display (allow OR deny is the owner's call)."""
        from android_device import package_is_resumed
        dump = str(self._run("shell", "dumpsys", "activity", "activities"))
        return any(package_is_resumed(dump, package, display_id) for package in self.PERMISSION_PACKAGES)

    # ---- hooks called by AndroidController.execute ------------------------------------

    def note_launch(self, package: str) -> None:
        if package in ORDER_PAGE_APPS:
            self.order_guard = True  # from now on every action first checks for an order/pay page

    def note_url(self, url: str) -> None:
        if ORDER_PAGE_URLS.match(url):
            self.order_guard = True  # cookie, sign-in and booking pages stop every later action

    def before_action(self, name: str, display_id: int) -> None:
        """Checks every touch action passes first: SMS tasks stay in Messages; order pages stop."""
        from android_device import package_is_resumed
        if (self.sms_task and name in TOUCH_ACTIONS and not self.sms_is_resumed(display_id)
                and not (self.sms_allows_calendar and package_is_resumed(
                    str(self._run("shell", "dumpsys", "activity", "activities")), CALENDAR_PACKAGE, display_id))):
            raise AgentError("SMS safety guard: Messages is no longer resumed on the private display")
        # Only tasks that opened an order-page app pay for this check (see note_launch / note_url).
        if self.order_guard and name in TOUCH_ACTIONS:
            self.wait_order_prefetch()
            for guarded in self.order_page_packages(display_id):
                self.check_order_page_once(display_id, guarded)

    # ---- order page: once per page ---------------------------------------------------

    def order_page_packages(self, display_id: int) -> list[str]:
        from android_device import package_is_resumed
        activities = str(self._run("shell", "dumpsys", "activity", "activities"))
        return [p for p in (*ORDER_PAGE_APPS, PACKAGE) if package_is_resumed(activities, p, display_id)]

    def frame_fingerprint(self, display_id: int) -> str | None:
        """Hash of the private display's current frame file; None when unknown (then always check)."""
        from android_device import workspace_identity
        try:
            state = self.state()
            if state.get("backend") != "adb-shell" or not workspace_identity(state) or state["displayId"] != display_id:
                return None
            out = str(self._run("shell", "sha256sum", f"{WORKSPACE_DIRECTORY}/frame-{state['session']}.png")).split()
        except (AgentError, KeyError, TypeError, ValueError):
            return None
        return f"{state['session']}:{out[0]}" if out and len(out[0]) == 64 else None

    def check_order_page_once(self, display_id: int, package: str) -> None:
        """The page check, reused while the frame is unchanged (same page, same verdict)."""
        fingerprint = self.frame_fingerprint(display_id)
        checks = self._order_checks if self._order_checks is not None else {}
        if fingerprint is not None and (package, fingerprint) in checks:
            self.order_checks_reused = getattr(self, "order_checks_reused", 0) + 1
            if checks[(package, fingerprint)] is not None:
                raise checks[(package, fingerprint)]
            return
        try:
            self.check_order_page(display_id, package)
        except OrderPageBlocked as error:
            self.remember_order_check(package, fingerprint, error)
            raise
        self.remember_order_check(package, fingerprint, None)

    def remember_order_check(self, package: str, fingerprint: str | None, verdict: OrderPageBlocked | None) -> None:
        if fingerprint is None:
            return  # unknown frame: never reused
        if self._order_checks is None or len(self._order_checks) > 64:
            self._order_checks = {}
        self._order_checks[(package, fingerprint)] = verdict

    def prefetch_order_check(self, display_id: int) -> None:
        """Check the page just observed in the background, while the model decides the next action."""
        if not self.order_guard:
            return
        self.wait_order_prefetch()  # e.g. the last step was a wait: its check has finished by now

        stop = threading.Event()

        def check() -> None:
            # The screen may still be settling from the last action: while the model thinks,
            # check again whenever the frame moved on during a check (at most three times).
            for _ in range(3):
                before = self.frame_fingerprint(display_id)
                try:
                    for guarded in self.order_page_packages(display_id):
                        self.check_order_page_once(display_id, guarded)
                except AgentError:
                    return  # the verdict is cached (or not); before_action decides again on the live frame
                if stop.is_set() or before is None or self.frame_fingerprint(display_id) == before:
                    return

        self._order_prefetch_stop = stop
        self._order_prefetch = threading.Thread(target=check, name="order-page-check", daemon=True)
        self._order_prefetch.start()

    def wait_order_prefetch(self) -> None:
        # One UiAutomation connection at a time: never run a second check beside the prefetch.
        if self._order_prefetch is not None:
            self._order_prefetch_stop.set()  # the action is due: finish the current check, start no other
            self._order_prefetch.join()
            self._order_prefetch = None

    def tap(self, x: int, y: int, display_id: int) -> None:
        """Messages: only an inspected allowlisted control. Gmail: inspect, then tap. Others: tap."""
        from android_device import package_is_resumed
        activities = str(self._run("shell", "dumpsys", "activity", "activities"))
        if package_is_resumed(activities, SMS_PACKAGE, display_id):
            self.guarded_sms_tap(x, y, display_id)
            return
        if package_is_resumed(activities, GMAIL_PACKAGE, display_id):
            self.check_mail_tap(x, y, display_id)
        self._run("shell", "input", "-d", str(display_id), "tap", str(x), str(y))

    def check_swipe(self, x1: int, y1: int, x2: int, y2: int, display_id: int) -> None:
        # A sideways swipe on a Gmail conversation archives or deletes it.
        if abs(x2 - x1) > abs(y2 - y1) and self.mail_is_resumed(display_id):
            raise MailControlBlocked("Mail safety guard: sideways swipe may archive/delete mail; not executed")

    def check_key(self, keycode: int, display_id: int) -> None:
        if keycode in {62, 66} and self.sms_is_resumed(display_id):
            raise AgentError("SMS safety guard: Space/Enter may send a message; owner must act")

    # ---- control and page checks through the shell helper -----------------------------

    def helper_jar(self) -> str:
        if not getattr(self, "_text_helper_jar", None):
            from workspace_runtime import install_text_helper
            self._text_helper_jar = install_text_helper()
        return self._text_helper_jar

    def helper_command(self, *arguments: str) -> str:
        return ("CLASSPATH=" + shlex.quote(self.helper_jar()) + f" app_process / {HELPER_CLASS} "
                + " ".join(shlex.quote(value) for value in arguments))

    def guard_state(self, label: str, display_id: int) -> dict[str, Any]:
        from android_device import workspace_identity
        state = self.state()
        if not workspace_identity(state) or state["displayId"] != display_id:
            raise AgentError(f"{label} safety guard: invalid workspace identity")
        self.helper_jar()
        return state

    @staticmethod
    def check_reply(label: str, result: Any, state: dict, display_id: int, package: str, method: str) -> None:
        if (not isinstance(result, dict) or result.get("ok") is not True or result.get("verified") is not True
                or result.get("executed") is not False or result.get("displayId") != display_id
                or result.get("session") != state["session"] or result.get("package") != package
                or result.get("method") != method or type(result.get("allowed")) is not bool):
            raise AgentError(f"{label} safety guard: " + str(result.get("error", "verification mismatch")
                                                             if isinstance(result, dict) else "invalid response"))

    def check_mail_tap(self, x: int, y: int, display_id: int) -> None:
        """Inspect the private Gmail control under (x, y); refuse send/delete/archive.

        Check-only helper; the caller then taps. Unlike SMS this is a denylist,
        because composing mail needs many ordinary fields and dialogs.
        """
        state = self.guard_state("Mail", display_id)
        self.require_isolation(display_id)
        self.require_owner_focus(display_id, private_window=True)
        raw = str(self._run("shell", self.helper_command(
            str(display_id), state["session"], GMAIL_PACKAGE, "mail-check", str(x), str(y))))
        try:
            result = json.loads(raw.strip())
        except json.JSONDecodeError as error:
            raise AgentError("Mail safety guard: invalid control check") from error
        self.check_reply("Mail", result, state, display_id, GMAIL_PACKAGE, "mail_control_check")
        if self.state() != state:
            raise AgentError("Mail safety guard: workspace changed during check")
        if not result["allowed"]:
            raise MailControlBlocked("Mail safety guard: send/delete/archive control rejected; no click performed")

    def check_order_page(self, display_id: int, package: str = MEITUAN_PACKAGE) -> None:
        """Refuse any action on a page that shows order/pay, agreement, login or cookie-consent wording."""
        state = self.guard_state("Order", display_id)
        raw = str(self._run("shell", self.helper_command(
            str(display_id), state["session"], package, "order-check", "0", "0")))
        try:
            result = json.loads(raw.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError) as error:
            raise AgentError("Order safety guard: invalid page check") from error
        self.check_reply("Order", result, state, display_id, package, "order_page_check")
        if not result["allowed"]:
            raise OrderPageBlocked(f"Order safety guard: page shows {result.get('word')!r}; nothing executed")

    def guarded_sms_tap(self, x: int, y: int, display_id: int) -> None:
        """Click only an inspected, allowlisted private SMS control; never Send."""
        state = self.guard_state("SMS", display_id)
        self.require_isolation(display_id)
        self.require_owner_focus(display_id, private_window=True)
        if self.state() != state:
            raise AgentError("SMS safety guard: workspace changed before click")
        raw = str(self._run("shell", self.helper_command(
            str(display_id), state["session"], SMS_PACKAGE, "sms-tap", str(x), str(y))))
        try:
            result = json.loads(raw.strip())
        except json.JSONDecodeError as error:
            raise AgentError("SMS safety guard: invalid click verification") from error
        if (isinstance(result, dict) and result.get("ok") is False
                and result.get("verified") is True and result.get("executed") is False
                and result.get("displayId") == display_id and result.get("session") == state["session"]
                and result.get("package") == SMS_PACKAGE and result.get("method") == "sms_control_check"
                and result.get("blockedControl") == "unknown_control"):
            self.require_isolation(display_id)
            self.require_owner_focus(display_id, private_window=True)
            if self.state() != state or not self.sms_is_resumed(display_id):
                raise AgentError("SMS safety guard: workspace changed after rejected click")
            raise SMSControlBlocked("Unknown private SMS control rejected; no click performed")
        if (not isinstance(result, dict) or result.get("ok") is not True
                or result.get("verified") is not True or result.get("displayId") != display_id
                or result.get("session") != state["session"] or result.get("package") != SMS_PACKAGE
                or result.get("method") != "guarded_sms_click"):
            raise AgentError("SMS safety guard: " + str(result.get("error", "verification mismatch")
                                                         if isinstance(result, dict) else "invalid response"))
        self.require_isolation(display_id)
        self.require_owner_focus(display_id, private_window=True)
        if self.state() != state:
            raise AgentError("SMS safety guard: workspace changed during click")
        self.last_private_input_focused = result.get("privateEditableFocused") is True

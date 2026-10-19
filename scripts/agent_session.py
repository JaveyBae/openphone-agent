"""One agent run: observe -> decide -> guard -> execute -> record, until finish or pause.

The session owns the per-run state (history, loop counters, experience recorder and
replay, the UI reader) and turns every way a step can end into one of three outcomes:
continue with the next step, finish (exit 0) or pause for the owner (exit 2). Every
other stop is an AgentError. The device, the planner and the run log are passed in,
so the same loop drives AutoGLM, Qwen, an OpenAI-compatible model or the mock.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from action_safety import SEND_TERMS, order_block_message, sensitive_reason, validate_input_text
from agent_common import PACKAGE, AgentError, MailControlBlocked, OrderPageBlocked, SMSControlBlocked, log_event, utc_now
from android_device import resumed_package
from app_targets import (CALENDAR_PACKAGE, GMAIL_PACKAGE, SMS_PACKAGE, is_mail_compose_goal, is_sms_goal,
                         mail_completion_handoff, mail_draft_goal, sms_completion_handoff, sms_draft_goal)
from execution_progress import emit_progress
from flight_params import parse_flight
from planners import note_external_step
from screen_notes import opener_after, screen_note
from task_routing import explicit_target_package

MODEL_PROVIDERS = ("autoglm", "openai", "qwen")
PAUSED = 2  # exit code: the owner must act (the bridge shows the message and a takeover button)
FINISHED = 0


class RunLog:
    """run.json (status and result) plus events.jsonl (one line per stage or step)."""

    def __init__(self, run_dir: Path, metadata: dict[str, Any]):
        self.run_dir = run_dir
        self.events_path = run_dir / "events.jsonl"
        self.metadata = metadata
        self.save()

    def save(self) -> None:
        (self.run_dir / "run.json").write_text(
            json.dumps(self.metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def event(self, event: dict[str, Any]) -> None:
        log_event(self.events_path, event)

    def stage(self, stage: str, **fields: Any) -> None:
        self.event({"timestamp": utc_now(), "stage": stage, **fields})

    def pause(self, message: str, **extra: Any) -> int:
        self.metadata.update({"status": "awaiting_user", "paused_at": utc_now(), "message": message, **extra})
        self.save()
        print("Paused for user: " + str(message).replace("\n", " ").replace("\r", " "))
        return PAUSED

    def finish(self, summary: str) -> int:
        self.metadata.update({"status": "finished", "finished_at": utc_now(), "summary": summary})
        self.save()
        print("Task finished; result saved in run.json")
        return FINISHED


REPLANNED = -1  # guard(): the step was not executed and the model was told why


class LoopGuard:
    """Stops a model that repeats itself: same action three times, six same scrolls, a frozen screen,
    or two actions taken in turn (A, B, A, B, A, B), e.g. opening a dialog and confirming it unchanged."""

    ALTERNATIONS = 6

    def __init__(self) -> None:
        self.prior_hash: str | None = None
        self.prior_fingerprint: str | None = None
        self.recent: list[str] = []
        self.repeated = 0
        self.same_swipes = 0
        self.stuck = 0

    def check_action(self, action: dict, screen_sha: str) -> None:
        fingerprint = json.dumps({"action": action["action"], "arguments": action.get("arguments", {})},
                                 ensure_ascii=False, sort_keys=True)
        # Scrolling again on a screen that did move is progress (a long form or list), not a loop.
        scrolled_on = action["action"] == "swipe" and self.prior_hash is not None and screen_sha != self.prior_hash
        self.repeated = self.repeated + 1 if fingerprint == self.prior_fingerprint and not scrolled_on else 0
        if self.repeated >= 2:
            raise AgentError("stopped after the model repeated the same action three times")
        self.same_swipes = self.same_swipes + 1 if action["action"] == "swipe" and fingerprint == self.prior_fingerprint else 0
        if self.same_swipes >= 5:
            raise AgentError("stopped after the model scrolled the same way six times")
        self.recent = (self.recent + [fingerprint])[-self.ALTERNATIONS:]
        if (len(self.recent) == self.ALTERNATIONS and self.recent[0] != self.recent[1]
                and all(f == self.recent[i % 2] for i, f in enumerate(self.recent))):
            raise AgentError("stopped after the model alternated between the same two actions three times")
        self.prior_fingerprint = fingerprint

    def screen_changed(self, screen_sha: str) -> bool:
        return self.prior_hash is None or screen_sha != self.prior_hash

    def after_execute(self, screen_sha: str) -> None:
        self.stuck = self.stuck + 1 if self.prior_hash == screen_sha else 0
        if self.stuck >= 3:
            raise AgentError("stopped because the private display did not change for four steps")
        self.prior_hash = screen_sha


class StepTimer:
    """Wall-clock seconds per phase of one step, plus the adb calls made in it."""

    def __init__(self, controller: Any):
        self.controller = controller
        self.started = self.phase_started = time.perf_counter()
        self.timing: dict[str, Any] = {"phases": {}, "adb": {}}
        controller.drain_adb_timing()

    def end(self, name: str) -> None:
        now = time.perf_counter()
        self.timing["phases"][name] = round(now - self.phase_started, 3)
        adb = self.controller.drain_adb_timing()
        if adb:
            self.timing["adb"][name] = adb
        self.timing["total_s"] = round(now - self.started, 3)
        self.phase_started = now


class AgentSession:
    opened_from: dict | None = None  # the control whose tap opened the dialog on screen
    text_replans = 0

    def __init__(self, args: Any, controller: Any, planner: Any, log: RunLog, run_id: str):
        self.args, self.controller, self.planner, self.log, self.run_id = args, controller, planner, log, run_id
        self.history: list[dict[str, Any]] = []
        self.loops = LoopGuard()
        self.sms_replans = 0
        self.app_replans = 0
        self.corrections = 0
        self.flight = None
        self.target: str | None = None
        self.planner_goal = mail_draft_goal(sms_draft_goal(args.goal))
        self.recorder = None
        self.replay = None
        self.ui_readers: dict[str, Any] = {}

    # ---- the run -------------------------------------------------------------

    def run(self) -> int:
        outcome = self.prepare()
        if outcome is not None:
            return outcome
        self.load_experience()
        for step in range(self.args.max_steps):
            outcome = self.step(step)
            if outcome is not None:
                return outcome
        raise AgentError(f"stopped at the configured {self.args.max_steps}-step limit")

    def prepare(self) -> int | None:
        """Start the workspace from a clean state and open the target app (or the browser)."""
        args, controller, log = self.args, self.controller, self.log
        emit_progress("initialize", 0, args.max_steps)
        print(f"[0/{args.max_steps}] initialize_workspace: verify independent content before capture")
        acting = not args.observe_only and not args.dry_run
        try:
            if acting:
                from workspace_runtime import ensure_running
                try:
                    ensure_running()
                except RuntimeError as error:
                    raise AgentError(str(error)) from error
            if acting and not args.keep_workspace:
                # Start from the app's home, not from whatever a previous run left behind.
                emit_progress("reset", 0, args.max_steps)
                removed = controller.reset_private_workspace(controller.require_display())
                log.stage("workspace_reset", removed=removed, focus=controller.last_focus_evidence)
            if acting and not args.no_skills:
                outcome = self.legacy_shortcuts()
                if outcome is not None:
                    return outcome
            self.target = explicit_target_package(args.goal) if not args.observe_only else None
            self.flight = (None if self.target or args.observe_only or args.no_skills
                           else parse_flight(args.goal, datetime.now().date()))
            if self.flight:
                # Open the results page directly; every later action first checks the page (cookies, booking).
                args.initial_url = self.flight.url()
                controller.order_guard = True
                log.stage("flight_query", origin=self.flight.origin, destination=self.flight.destination,
                          date=self.flight.day.isoformat())
            if self.target:
                emit_progress("launch", 0, args.max_steps, package=self.target)
                controller.execute({"action": "launch_app", "arguments": {"package": self.target}})
                controller.wait_private_content(controller.require_display())
                log.stage("explicit_app_setup", package=self.target, display_id=controller.require_display(),
                          result="executed", focus=controller.last_focus_evidence)
            initialized = controller.prepare_workspace(args.initial_url, allow_initialize=not args.observe_only)
        except AgentError as error:
            log.stage("workspace_setup", result="blocked", error=str(error))
            raise
        log.stage("workspace_setup", display_id=controller.require_display(),
                  result="initialized" if initialized else "existing_content",
                  action={"action": "open_url", "arguments": {"url": args.initial_url}} if initialized else None)
        if self.flight:
            outcome = self.legacy_flight_results()
            if outcome is not None:
                return outcome
        if (self.target in (CALENDAR_PACKAGE, SMS_PACKAGE, GMAIL_PACKAGE) and args.provider in MODEL_PROVIDERS
                and not args.no_skills and acting):
            from legacy_presets import try_skill
            outcome = try_skill(args, controller, log.events_path, log.run_dir, log.metadata, self.target)
            if outcome is not None:
                return outcome
        return None

    def legacy_shortcuts(self) -> int | None:
        from legacy_presets import late_notice, set_alarm
        outcome = set_alarm(self.args, self.controller, self.log.events_path, self.log.run_dir, self.log.metadata)
        if outcome is None and self.args.provider in MODEL_PROVIDERS:
            outcome = late_notice(self.args, self.controller, self.log.events_path, self.log.run_dir,
                                  self.log.metadata)
        return outcome

    def legacy_flight_results(self) -> int | None:
        from legacy_presets import read_flight_results
        flight, controller = self.flight, self.controller
        try:
            controller.wait_private_content(controller.require_display())
            controller.check_order_page(controller.require_display(), PACKAGE)
        except OrderPageBlocked as error:
            # e.g. the cookie consent page: the owner chooses, before any model step.
            emit_progress("paused", 0, self.args.max_steps)
            self.log.stage("flight_page_check", result="blocked", error=str(error))
            return self.log.pause(order_block_message(error))
        summary = read_flight_results(controller, flight, self.log.events_path)
        if summary:
            return self.log.finish(summary)
        self.planner_goal = (
            f"The browser already shows Google Flights results for {flight.origin} -> {flight.destination} "
            f"on {flight.day.isoformat()}. Only read the results (scroll if needed); never select a flight, "
            "book, sign in or answer cookie dialogs. Finish with up to 5 flights in Chinese: airline, "
            "departure–arrival time, duration, stops, price. Owner's request: " + self.args.goal)
        return None

    # ---- experience ------------------------------------------------------------

    def load_experience(self) -> None:
        """Every model-driven run is recorded; a verified one is learned from (see experience.py)."""
        import experience
        args = self.args
        if not (args.mode in ("agent", "experience") and args.provider in MODEL_PROVIDERS
                and not args.observe_only and not args.dry_run):
            return
        self.recorder = experience.Recorder(args.goal, self.run_id)
        if args.mode == "experience":
            learned = experience.find(args.goal)
            if learned:
                self.replay = experience.Replay(learned)
                self.log.stage("experience_loaded", path=learned["path"], learned_from=learned.get("learned_from"),
                               steps=len(learned["steps"]))

    def learn_from_run(self) -> None:
        if self.recorder is None:
            return
        learned_path = self.recorder.learn()
        stats = {"learned": str(learned_path) if learned_path else None}
        if self.replay is not None:
            self.replay.finish()
            stats.update({"loaded": self.replay.record["path"], "predicted_steps": self.replay.hits})
        self.log.metadata["experience"] = stats
        self.log.stage("experience", **stats)

    def read_ui(self) -> tuple[str | None, list[dict] | None]:
        """The resumed private app and its UI tree (None when it cannot be read)."""
        import experience
        controller = self.controller
        display = controller.require_display()
        package = resumed_package(str(controller._run("shell", "dumpsys", "activity", "activities")), display)
        if package not in experience.READABLE:
            return package, None
        try:
            if package not in self.ui_readers:
                from private_ui import PrivateUiClient
                # Android allows one UiAutomation connection at a time: close the other app's reader.
                self.close_ui_readers()
                self.ui_readers[package] = PrivateUiClient(controller, package, resident=True)
            return package, self.ui_readers[package].snapshot()
        except AgentError as error:
            reader = self.ui_readers.pop(package, None)
            if reader is not None:
                # Its resident process holds Android's one UiAutomation connection: end it, or the
                # next reader and the Gmail/SMS control guards fail with "already registered".
                reader.close()
            self.log.stage("ui_read", package=package, error=str(error))
            return package, None

    def close_ui_readers(self) -> None:
        for package in list(self.ui_readers):
            self.ui_readers.pop(package).close()

    # ---- one step ----------------------------------------------------------------

    def step(self, step: int) -> int | None:
        """One observe-decide-act cycle. None: go on with the next step."""
        args = self.args
        emit_progress("observe", step + 1, args.max_steps)
        timer = StepTimer(self.controller)
        observation, ui_package, ui_nodes = self.observe(step)
        timer.end("capture")
        action, predicted, ui_package, ui_nodes = self.decide(observation, ui_package, ui_nodes, step, timer)
        model_finished = action["action"] == "finish"
        action = mail_completion_handoff(args.goal, sms_completion_handoff(args.goal, action))
        self.loops.check_action(action, observation.sha256)
        event = {"timestamp": utc_now(), "step": step, "display_id": observation.display_id,
                 "screenshot": observation.path.name, "screenshot_sha256": observation.sha256,
                 "action": action, "timing": timer.timing}
        if getattr(self.planner, "screen_note", None) and not predicted:
            event["screen_note"] = self.planner.screen_note
        # Raw model analysis stays in local audit files, not the bridge's
        # public channel where it could also spoof an execution event.
        print(f"[{step + 1}/{args.max_steps}] {action['action']}: validated action")

        if action["action"] == "request_user":
            return self.handle_request_user(action, event, step, model_finished)
        if action["action"] == "finish":
            return self.handle_finish(action, event, step, ui_nodes)
        outcome = self.guard(action, event, observation, step)
        if outcome is not None:
            return None if outcome == REPLANNED else outcome
        timer.end("guards")
        return self.execute(action, event, observation, step, timer, ui_package, ui_nodes, predicted)

    def observe(self, step: int):
        observation = self.controller.capture(self.log.run_dir / f"step-{step:02d}.png")
        ui_package, ui_nodes = self.read_ui() if self.recorder is not None else (None, None)
        last = self.history[-1] if self.history else {}
        field = next((n for n in ui_nodes or [] if n.get("focused") and n.get("editable")), None)
        if field is not None and last.get("action", {}).get("action") == "tap":
            # The tap focused a text field (no keyboard appears on this display): say so, by name.
            last["private_input_focused"] = True
            name = str(field.get("id") or "").split(":id/")[-1] or str(field.get("text") or "")
            last["focused_field"] = name[:30] or None
        if ui_nodes and last.get("action", {}).get("action") == "tap":
            last["focused_nodes"] = [{k: n.get(k) for k in ("id", "class", "text", "editable")}
                                     for n in ui_nodes if n.get("focused")][:3]
        return observation, ui_package, ui_nodes

    def decide(self, observation, ui_package, ui_nodes, step: int, timer: StepTimer):
        """A learned step when the live screen matches the experience, otherwise the model."""
        args, replay = self.args, self.replay
        predicted = replay.predict(ui_package, ui_nodes) if replay is not None else None
        if replay is not None and not predicted and replay.hint is None and ui_nodes is not None:
            # The page may still be animating from the last step: look once more before the model.
            time.sleep(0.5)
            ui_package, ui_nodes = self.read_ui()
            predicted = replay.predict(ui_package, ui_nodes)
        self.planner.experience_hint = replay.hint if replay is not None and not predicted else None
        self.planner.screen_note = screen_note(ui_package, ui_nodes, self.opened_from)
        if predicted:
            # A learned structural step on a matching live screen: no model call.
            action = predicted[0]
            emit_progress("predicted", step + 1, args.max_steps, action=action["action"])
            note_external_step(self.planner, self.planner_goal, len(self.history), observation.display_id, action)
            timer.end("experience")
            return action, predicted, ui_package, ui_nodes
        emit_progress("model", step + 1, args.max_steps)
        if getattr(self.controller, "order_guard", False) is True:
            # The ~2 s order-page check of this screen runs while the model thinks (app_guards.py).
            self.close_ui_readers()
            self.controller.prefetch_order_check(observation.display_id)
        for attempt in range(2):
            try:
                action = self.planner.next_action(self.planner_goal, observation, self.history,
                                                  include_image=not args.no_image)
                break
            except (ValueError, SyntaxError, KeyError) as error:
                raise AgentError(f"model request/action failed: {error}") from error
            except OSError as error:  # URLError, a dropped tunnel (RemoteDisconnected), a timeout
                if attempt == 1:
                    raise AgentError(f"model server unreachable: {error}") from error
                self.log.stage("model_retry", step=step, error=str(error)[:200])
                time.sleep(3)
        timer.end("model")
        if getattr(self.planner, "last_timing", None):
            timer.timing["model"] = self.planner.last_timing
        return action, None, ui_package, ui_nodes

    def replan(self, event: dict, step: int, action: dict, result: str, told: str | None = None, **note: Any) -> None:
        """Log a step that was not executed and tell the model why (`told`) on its next turn."""
        event["result"] = result
        self.log.event(event)
        self.history.append({"step": step, "action": action, "screen_changed": False, "result": told or result,
                             "executed": False, **note})
        emit_progress("replan", step + 1, self.args.max_steps)

    def pause(self, event: dict, step: int, message: str, metadata: dict | None = None, **result: Any) -> int:
        emit_progress("paused", step + 1, self.args.max_steps)
        event.update(result)
        self.log.event(event)
        return self.log.pause(message, **(metadata or {}))

    def handle_request_user(self, action: dict, event: dict, step: int, model_finished: bool) -> int | None:
        arguments = action["arguments"]
        if arguments.get("unavailable_app") and self.app_replans < 2:
            # An app that is not installed: tell the model and let it choose another way.
            self.app_replans += 1
            self.replan(event, step, action, "app_unavailable", app=arguments["unavailable_app"])
            return None
        emit_progress("paused", step + 1, self.args.max_steps)
        event["result"] = "awaiting_user"
        self.log.event(event)
        if model_finished:
            self.learn_from_run()  # a finished draft handed to the owner is a success too
        return self.log.pause(arguments.get("message", "User confirmation required"))

    def handle_finish(self, action: dict, event: dict, step: int, ui_nodes) -> int | None:
        """The finish message is a claim: check it against the app before reporting it."""
        from completion_check import alarm_missing, missing_values, required_values, ui_texts
        args = self.args
        emit_progress("finished", step + 1, args.max_steps)
        event["result"] = "finished"
        self.log.event(event)
        summary = action["arguments"].get("summary", "Task finished")
        if getattr(self.controller, "order_guard", False) is True:
            self.controller.wait_order_prefetch()  # one UiAutomation connection: the re-reads below need it
        missing = missing_values(args.goal, ui_texts(ui_nodes)) if ui_nodes is not None else []
        for _ in range(2):
            if not missing:
                break
            # The result page may still be loading: look again before calling the claim unproven.
            time.sleep(1.5)
            _, fresh_nodes = self.read_ui()
            if fresh_nodes is not None:
                ui_nodes = fresh_nodes
                missing = missing_values(args.goal, ui_texts(ui_nodes))
        if self.recorder is not None:
            alarm = alarm_missing(args.goal, str(self.controller._run("shell", "dumpsys", "alarm")), datetime.now(),
                                  ui_nodes)
            missing += [alarm] if alarm else []
        if self.target == CALENDAR_PACKAGE and not missing:
            calendar_event = self.calendar_missing()
            missing += [calendar_event] if calendar_event else []
        checked = [v for v in required_values(args.goal) if v not in missing] if ui_nodes is not None else []
        self.log.metadata["verification"] = {"checked": checked, "missing": missing, "ui_read": ui_nodes is not None}
        if missing and self.corrections < 1 and step + 1 < args.max_steps:
            # Self-correction: tell the model what the app does not show yet and let it continue.
            self.corrections += 1
            event["missing"] = missing
            self.replan(event, step, action, "finish_unverified_retry", told="finish_unverified", missing=missing)
            return None
        if missing:
            message = ("模型说已经完成，但核对时没在应用里找到：" + "、".join(missing)
                       + "。所以没有当作完成。请看后台画面（可以点「接管到主屏」）核对后再处理。")
            return self.pause(event, step, message, {"model_claim": summary[:1000]}, result="finish_unverified")
        if checked:
            summary += "\n（已在应用里核对到：" + "、".join(checked) + "）"
        self.learn_from_run()
        return self.log.finish(summary)

    def calendar_missing(self) -> str | None:
        """Read the event back from the calendar provider: title, date, start, end (and a named place) must match."""
        import shlex
        from zoneinfo import ZoneInfo
        from completion_check import calendar_expected, calendar_missing, calendar_rows
        today = datetime.now().date()
        expected = calendar_expected(self.args.goal, today)
        if expected is None:
            return None
        zone = ZoneInfo(str(self.controller._run("shell", "getprop", "persist.sys.timezone")).strip() or "UTC")
        title = expected[0].replace("'", "''")  # SQL string literal
        where = shlex.quote(f"title='{title}' AND deleted=0")
        missing = None
        for _ in range(4):  # Calendar writes the event to its provider a moment after Save
            rows = calendar_rows(str(self.controller._run(
                "shell", "content", "query", "--uri", "content://com.android.calendar/events",
                "--projection", "title:dtstart:dtend:eventLocation", "--where", where)))
            missing = calendar_missing(self.args.goal, rows, zone, today)
            if missing is None:
                return None
            time.sleep(1.5)
        return missing

    def guard(self, action: dict, event: dict, observation, step: int) -> int | None:
        """Word-level safety, observe-only, SMS app lock and permission prompts. Pause or raise to stop."""
        args, controller = self.args, self.controller
        safety_block = sensitive_reason(action)
        # A mail draft task restates "send" in nearly every analysis. When that
        # is the ONLY match, a tap/type inside Gmail is left to the code-level
        # Gmail guard, which inspects the real target control instead of words.
        if (safety_block and is_mail_compose_goal(args.goal) and action["action"] in {"tap", "input_text"}
                and sensitive_reason(action, ignore=SEND_TERMS) is None
                and controller.mail_is_resumed(observation.display_id)):
            event["safety_note"] = safety_block + "; deferred to the Gmail send/delete control guard"
            safety_block = None
        if safety_block:
            message = "模型尝试执行需要人工确认的操作，已被安全检查拦截，本步没有执行。请人工处理后重新提交任务。"
            return self.pause(event, step, message, {"safety_reason": safety_block},
                              result="blocked_sensitive", safety_reason=safety_block)
        if action["action"] == "input_text" and self.text_replans < 2:
            try:
                validate_input_text(str(action["arguments"].get("text", "")))
            except AgentError:
                # Usually a trailing "\n" meant as Enter: say how, instead of ending the run.
                self.text_replans += 1
                self.replan(event, step, action, "blocked_control_characters",
                            told="not executed: Type text cannot contain line breaks or control characters; "
                                 "Type the plain text, then press the Enter key as a separate action")
                return REPLANNED
        if args.observe_only:
            event["result"] = "blocked_observe_only"
            self.log.event(event)
            raise AgentError("observe-only verification refused a UI action")
        allowed_apps = {SMS_PACKAGE} | ({CALENDAR_PACKAGE} if controller.sms_allows_calendar else set())
        if is_sms_goal(args.goal) and (action["action"] == "open_url"
                                       or (action["action"] == "launch_app"
                                           and action["arguments"].get("package") not in allowed_apps)):
            raise AgentError("SMS task refused to switch away from Messages")
        if (action["action"] in {"tap", "swipe", "input_text", "key"}
                and controller.permission_prompt_open(observation.display_id) is True):
            # Granting or denying a permission changes the owner's settings: never decide it.
            message = ("后台屏幕弹出了系统权限申请，Agent 没有替你选择（允许或拒绝都没点）。"
                       "可以点「接管到主屏」自己决定；不处理的话，下次任务开始时会直接关闭它。")
            return self.pause(event, step, message, result="blocked_permission_prompt", executed=False)
        return None

    def execute(self, action: dict, event: dict, observation, step: int, timer: StepTimer,
                ui_package, ui_nodes, predicted) -> int | None:
        args, controller = self.args, self.controller
        # Text input and the control guards need the one UiAutomation connection: free it first.
        # Plain taps and scrolls in other apps keep the reader open (no reconnect next step).
        if (action["action"] == "input_text" or getattr(controller, "order_guard", False)
                or ui_package in (SMS_PACKAGE, GMAIL_PACKAGE)):
            self.close_ui_readers()
        emit_progress("execute", step + 1, args.max_steps, action=action["action"])
        try:
            controller.execute(action)
        except OrderPageBlocked as error:
            # The owner places and pays for orders; the Agent never acts on such a page.
            return self.pause(event, step, order_block_message(error),
                              result="blocked_order_page", executed=False, error=str(error))
        except MailControlBlocked as error:
            # Never retried or replanned: the owner reviews and sends/deletes manually.
            message = ("Agent 想点击发送、删除或归档类按钮，已被安全检查拦下，没有执行。"
                       "邮件草稿（如有）保留在 Gmail 中；请你核对后手动处理。")
            return self.pause(event, step, message, result="blocked_mail_control", executed=False, error=str(error))
        except SMSControlBlocked:
            event["executed"] = False
            if not controller.sms_task or self.sms_replans >= 2:
                event["result"] = "blocked_unknown_control"
                self.log.event(event)
                raise AgentError("SMS safety guard: unknown control replan limit reached; no click performed")
            self.sms_replans += 1
            self.replan(event, step, action, "blocked_unknown_control")
            return None
        except AgentError as error:
            event.update({"result": "blocked", "error": str(error)})
            self.log.event(event)
            raise
        event["focus"] = getattr(controller, "last_focus_evidence", None)
        event["private_input_focused"] = getattr(controller, "last_private_input_focused", False)
        timer.end("execute")
        if action["action"] != "wait":
            time.sleep(0.5)  # the next step reads the screen again; experience re-reads if it lags
        timer.end("settle")
        changed = self.loops.screen_changed(observation.sha256)
        event.update({"screen_changed_since_previous_observation": changed, "result": "executed",
                      "source": "experience" if predicted else "model",
                      "ui": {"package": ui_package, "nodes": None if ui_nodes is None else len(ui_nodes)}})
        self.log.event(event)
        self.opened_from = opener_after(ui_nodes, action, self.opened_from)
        from experience import target_at
        tapped = target_at(ui_nodes, int(action["arguments"].get("x", -1)),
                                      int(action["arguments"].get("y", -1))) if ui_nodes and action["action"] == "tap" else None
        # Some apps (Gmail's Subject) do not report the field as focused: knowing that the tap
        # hit a text field is enough to stop the model from tapping it again and again.
        tapped_text_field = bool(tapped and str(tapped.get("class", "")).endswith("EditText"))
        if self.recorder is not None:
            self.recorder.add(ui_package, ui_nodes, action, event["source"])
            self.recorder.save(self.log.run_dir)
        emit_progress("verified", step + 1, args.max_steps, action=action["action"])
        self.history.append({"step": step, "action": action, "screen_changed": changed,
                             "private_input_focused": event["private_input_focused"],
                             "tapped_text_field": tapped_text_field})
        self.loops.after_execute(observation.sha256)
        return None

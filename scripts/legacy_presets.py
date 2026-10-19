"""Hand-written skills and rule shortcuts, used only in --mode legacy for comparison."""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from agent_common import PACKAGE, AgentError, log_event, utc_now
from alarm_params import AlarmUnclear, alarm_scheduled, parse_alarm
from app_targets import CALENDAR_PACKAGE, GMAIL_PACKAGE, SMS_PACKAGE, is_mail_to_calendar
from execution_progress import emit_progress


def set_alarm(args, controller, events_path: Path, run_dir: Path, metadata: dict) -> int | None:
    """An alarm request: standard set-alarm intent on the private display, checked in the system list."""
    def end(status: str, message: str, code: int) -> int:
        key = "summary" if code == 0 else "message"
        metadata.update({"status": status, ("finished_at" if code == 0 else "paused_at"): utc_now(), key: message})
        (run_dir / "run.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("Task finished; result saved in run.json" if code == 0 else f"Paused for user: {message}")
        return code

    now = datetime.now()
    try:
        alarm = parse_alarm(args.goal, now)
    except AlarmUnclear as question:
        emit_progress("paused", 0, args.max_steps)
        log_event(events_path, {"timestamp": utc_now(), "stage": "alarm", "result": "unclear", "message": str(question)})
        return end("awaiting_user", str(question), 2)
    if alarm is None:
        return None
    display_id = controller.require_display()
    emit_progress("skill", 0, args.max_steps, label="设置闹钟")
    controller.require_isolation(display_id)
    controller._run("shell", alarm.command(display_id))
    emit_progress("skill", 0, args.max_steps, label="在系统闹钟列表中核对")
    ring = alarm.next_ring(now)
    for _ in range(10):
        if alarm_scheduled(str(controller._run("shell", "dumpsys", "alarm")), ring):
            log_event(events_path, {"timestamp": utc_now(), "stage": "alarm", "result": "set",
                                    "ring": ring.isoformat(), "days": list(alarm.days), "label": alarm.label})
            return end("finished", f"闹钟已设好：{alarm.describe()}（{alarm.label}），已在系统闹钟列表中核对。"
                       f"下次响铃：{ring.month}月{ring.day}日 {ring:%H:%M}。", 0)
        time.sleep(0.5)
    return end("awaiting_user", "已经发出设闹钟指令，但没在系统闹钟列表里看到它。请打开时钟应用核对；"
               "Agent 不会再重复设置。", 2)


def late_notice(args, controller, events_path: Path, run_dir: Path, metadata: dict) -> int | None:
    """Calendar -> message: find the meeting in the calendar, then draft (never send) the late notice."""
    from zoneinfo import ZoneInfo
    from event_params import DayParams
    from late_params import LateUnclear, message_body, parse_late, pick_event
    from message_params import MailParams, SmsParams, subject_from_body
    from skill_runner import SkillPaused, load_skill, run_skill

    def pause(message: str) -> int:
        emit_progress("paused", 0, args.max_steps)
        log_event(events_path, {"timestamp": utc_now(), "stage": "late_notice", "result": "paused", "message": message})
        metadata.update({"status": "awaiting_user", "paused_at": utc_now(), "message": message})
        (run_dir / "run.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("Paused for user: " + message.replace("\n", " "))
        return 2

    now = datetime.now(ZoneInfo("Europe/Zurich")).replace(tzinfo=None)
    try:
        notice = parse_late(args.goal, now.date())
    except LateUnclear as question:
        return pause(str(question))
    if notice is None:
        return None
    progress = lambda label: emit_progress("skill", 0, args.max_steps, label=label)  # noqa: E731
    display_id = controller.require_display()
    try:
        # 1) Read that day in the calendar (read-only).
        controller.execute({"action": "launch_app", "arguments": {"package": CALENDAR_PACKAGE}})
        controller.wait_private_content(display_id)
        outputs: dict = {}
        run_skill(controller, load_skill("google_calendar_read_day"), DayParams(notice.day), progress, outputs=outputs)
        start, title = pick_event(args.goal, outputs.get("events", []), now, notice.day)
        body = message_body(title, start, notice.minutes)
        log_event(events_path, {"timestamp": utc_now(), "stage": "late_notice", "event": f"{start} {title}",
                                "channel": notice.channel, "body": body})
        # 2) Draft the message in the right app; the owner reviews and sends.
        controller.reset_private_workspace(display_id)
        package = SMS_PACKAGE if notice.channel == "sms" else GMAIL_PACKAGE
        controller.execute({"action": "launch_app", "arguments": {"package": package}})
        controller.wait_private_content(display_id)
        if notice.channel == "sms":
            summary = run_skill(controller, load_skill("messages_sms_draft"), SmsParams(notice.to, body), progress)
        else:
            summary = run_skill(controller, load_skill("gmail_draft"),
                                MailParams(notice.to, subject_from_body(body), body), progress)
    except LateUnclear as question:
        return pause(str(question))
    except SkillPaused as error:
        return pause(str(error))
    except AgentError as error:
        return pause(f"没能完成「查日程 → 写晚到消息」：{error}。没有发送任何消息。")
    return pause(f"在日历里找到 {notice.day.month}月{notice.day.day}日 {start} 的「{title}」。\n" + summary)


def read_flight_results(controller, flight, events_path: Path, seconds: float = 20.0) -> str | None:
    """Read the Google Flights result cards without the model; None lets the model take over."""
    from flight_params import answer, read_results
    from private_ui import PrivateUiClient
    emit_progress("skill", 0, 25, label="读取航班列表")
    ui = PrivateUiClient(controller, PACKAGE, resident=True)
    try:
        deadline, previous = time.monotonic() + seconds, None
        while time.monotonic() < deadline:
            flights = read_results([str(n.get("description") or n.get("text") or "") for n in ui.snapshot()])
            if flights and flights == previous:  # two identical reads: the list has finished loading
                log_event(events_path, {"timestamp": utc_now(), "stage": "flight_results", "count": len(flights)})
                return answer(flight, flights)
            previous = flights
            time.sleep(1.0)
    except AgentError as error:
        log_event(events_path, {"timestamp": utc_now(), "stage": "flight_results", "error": str(error)})
    finally:
        ui.close()
    return None


def try_skill(args, controller, events_path: Path, run_dir: Path, metadata: dict,
              target: str) -> int | None:
    """Replay a learned skill for the target app; None means: continue step by step."""
    from zoneinfo import ZoneInfo
    from event_params import (QUESTION, UNSUPPORTED, agenda_day, parse_day, parse_times, quoted_title,
                              resolve, unquoted)
    from message_params import MailParams, parse_mail, parse_sms, subject_from_body
    from skill_runner import SkillPaused, ask_model, load_skill, run_skill
    today = datetime.now(ZoneInfo("Europe/Zurich")).date()
    goal = args.goal
    params = (agenda_day(goal, today) if target == CALENDAR_PACKAGE
              else parse_sms(goal) if target == SMS_PACKAGE else parse_mail(goal))
    if target == GMAIL_PACKAGE and is_mail_to_calendar(goal):
        return mail_to_calendar(args, controller, events_path, run_dir, metadata, today)
    if target == GMAIL_PACKAGE:
        # Address, subject and body come only from the owner's words.
        if params is None:
            return None
        if not params.subject:
            params = MailParams(params.to, subject_from_body(params.body), params.body)
        skill = load_skill("gmail_draft")
        log_event(events_path, {"timestamp": utc_now(), "stage": "skill_params",
                                "params": {"to": params.to, "subject": params.subject, "body_chars": len(params.body)}})
    elif target == SMS_PACKAGE:
        # Number and body come only from the owner's words; anything unclear goes step by step.
        if params is None:
            return None
        skill = load_skill("messages_sms_draft")
        log_event(events_path, {"timestamp": utc_now(), "stage": "skill_params",
                                "params": {"number": params.number, "body": params.body}})
    elif params is not None:
        # A read-only question about one day: rules give the date, no model call.
        skill = load_skill("google_calendar_read_day")
        log_event(events_path, {"timestamp": utc_now(), "stage": "skill_params",
                                "params": {"date": params.day.isoformat(),
                                           "last": params.last and params.last.isoformat()}})
    else:
        # Cheap rule checks first: no model call for requests the skill cannot do.
        if UNSUPPORTED.search(unquoted(goal)) or QUESTION.search(unquoted(goal)) or parse_day(goal, today) is None \
                or parse_times(goal) is None:
            return None
        reply = ""
        if quoted_title(goal) is None:  # a 「quoted title」 needs no model call
            try:
                reply = ask_model(args.base_url, args.model, args.api_key, goal, today, args.timeout)
            except (OSError, ValueError, KeyError) as error:
                log_event(events_path, {"timestamp": utc_now(), "stage": "skill_params", "error": str(error)})
        params = resolve(goal, reply, today)
        log_event(events_path, {"timestamp": utc_now(), "stage": "skill_params", "model_reply": reply[-500:],
                                "params": None if params is None else {
                                    "title": params.title, "date": params.day.isoformat(),
                                    "start": params.start.strftime("%H:%M"), "end": params.end.strftime("%H:%M")}})
        if params is None:
            return None
        skill = load_skill("google_calendar_create_event")
    print(f"[skill] {skill['name']}: {params}", flush=True)
    timings: list = []
    try:
        summary = run_skill(controller, skill, params,
                            lambda label: emit_progress("skill", 0, args.max_steps, label=label), timings)
    except SkillPaused as error:
        log_event(events_path, {"timestamp": utc_now(), "stage": "skill", "result": "paused", "error": str(error)})
        metadata.update({"status": "awaiting_user", "paused_at": utc_now(), "message": str(error)})
        (run_dir / "run.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("Paused for user: " + str(error).replace("\n", " "))
        return 2
    except AgentError as error:
        # SkillFallback, or an error before the first step (e.g. while connecting).
        # Errors after the commit step always arrive as SkillPaused above, so
        # nothing was saved here: discard the draft, reopen the app, let the model work.
        import traceback
        log_event(events_path, {"timestamp": utc_now(), "stage": "skill", "result": "fallback", "error": str(error),
                                "traceback": traceback.format_exc()[-3000:]})
        print(f"[skill] fallback to step-by-step control: {error}", flush=True)
        controller.reset_private_workspace(controller.require_display())
        controller.execute({"action": "launch_app", "arguments": {"package": target}})
        controller.wait_private_content(controller.require_display())
        return None
    if skill.get("handoff"):
        # Draft skills never finish as "done": the owner reviews and sends.
        log_event(events_path, {"timestamp": utc_now(), "stage": "skill", "result": "handoff", "summary": summary,
                                "step_seconds": timings})
        emit_progress("paused", 0, args.max_steps)
        metadata.update({"status": "awaiting_user", "paused_at": utc_now(), "message": summary, "skill": skill["name"]})
        (run_dir / "run.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("Paused for user: " + summary.replace("\n", " "))
        return 2
    log_event(events_path, {"timestamp": utc_now(), "stage": "skill", "result": "finished", "summary": summary,
                            "step_seconds": timings})
    emit_progress("finished", 0, args.max_steps)
    metadata.update({"status": "finished", "finished_at": utc_now(), "summary": summary, "skill": skill["name"]})
    (run_dir / "run.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Task finished; result saved in run.json")
    return 0


def pause_with(events_path: Path, run_dir: Path, metadata: dict, message: str, max_steps: int) -> int:
    emit_progress("paused", 0, max_steps)
    metadata.update({"status": "awaiting_user", "paused_at": utc_now(), "message": message})
    (run_dir / "run.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Paused for user: " + message.replace("\n", " "))
    return 2


def mail_to_calendar(args, controller, events_path: Path, run_dir: Path, metadata: dict, today) -> int | None:
    """Find the email (read-only), let the model read it, verify, then ASK before writing.

    The proposal ends with an action marker; the APP shows it as a button that
    sends an ordinary calendar request, handled by the calendar skill.
    """
    import itinerary
    from message_params import mail_query
    from skill_runner import load_skill, run_skill
    query = mail_query(args.goal)
    outputs: dict = {}
    timings: list = []
    try:
        run_skill(controller, load_skill("gmail_find_email"), query,
                  lambda label: emit_progress("skill", 0, args.max_steps, label=label), timings, outputs=outputs)
    except AgentError as error:
        log_event(events_path, {"timestamp": utc_now(), "stage": "skill", "result": "fallback", "error": str(error)})
        return pause_with(events_path, run_dir, metadata,
                          "没能在 Gmail 里完成搜索：" + str(error) + "。没有改动任何邮件或日历。", args.max_steps)
    emails = outputs.get("emails", [])
    log_event(events_path, {"timestamp": utc_now(), "stage": "skill", "result": "read", "query": query.query,
                            "matches": len(emails), "step_seconds": timings})
    if not emails:
        return pause_with(events_path, run_dir, metadata,
                          f"没有找到匹配的邮件（搜索条件：{query.query}）。可以告诉我邮件的标题。", args.max_steps)
    if len(emails) > 1:
        titles = "；".join(f"「{e.get('subject', '')}」" for e in emails[:5])
        return pause_with(events_path, run_dir, metadata,
                          f"找到 {len(emails)} 封可能相关的邮件：{titles}。请告诉我是哪一封（说出标题）。", args.max_steps)
    email = emails[0]
    subject, body = email.get("subject", ""), email.get("snippet", "")
    try:
        reply = itinerary.ask(args.base_url, args.model, args.api_key, subject, body, today, args.timeout)
    except (OSError, ValueError, KeyError) as error:
        return pause_with(events_path, run_dir, metadata, "读到了邮件，但模型暂时不可用：" + str(error), args.max_steps)
    proposal = itinerary.verify(subject, body, reply, today)
    log_event(events_path, {"timestamp": utc_now(), "stage": "itinerary", "model_reply": reply[-500:],
                            "proposal": None if proposal is None else proposal.request()})
    if proposal is None:
        return pause_with(events_path, run_dir, metadata,
                          f"读到了邮件「{subject}」，但没能可靠地提取出时间和日期，所以没有往日历里写。\n邮件内容：{body}",
                          args.max_steps)
    message = (f"我在邮件「{subject}」（{email.get('senders', '')}，{email.get('date', '')}）里找到这个行程：\n"
               f"{proposal.title}\n{proposal.day.month}月{proposal.day.day}日 "
               f"{proposal.start:%H:%M}–{proposal.end:%H:%M}\n"
               "还没有写进日历。确认无误的话，点下面的按钮添加。\n"
               f"[[action:添加到日历|{proposal.request()}]]")
    return pause_with(events_path, run_dir, metadata, message, args.max_steps)

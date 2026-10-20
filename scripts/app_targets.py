"""Explicit supported app names; SMS is not an alias for WeChat."""
from __future__ import annotations

import re

SMS_PACKAGE = "com.google.android.apps.messaging"
CALENDAR_PACKAGE = "com.google.android.calendar"  # synced to the owner's Google account
FOSSIFY_CALENDAR_PACKAGE = "org.fossify.calendar"  # offline calendar used by the old preset
GMAIL_PACKAGE = "com.google.android.gm"
CLOCK_PACKAGE = "com.google.android.deskclock"
MAPS_PACKAGE = "com.google.android.apps.maps"
CONTACTS_PACKAGE = "com.google.android.contacts"
RAILWAY_PACKAGE = "com.MobileTicket"  # 铁路12306: only searches trains; never books or pays
MEITUAN_PACKAGE = "com.sankuai.meituan"  # 美团 (takeout lives inside it); never orders or pays
MAIL_NAMES = ("gmail", "email", "e-mail", "mail", "邮件", "邮箱", "电子邮件")
MAIL_DRAFT_PREFIX = "Prepare an email draft in Gmail. NEVER send it. "
SMS_DRAFT_PREFIX = "Prepare an SMS draft in Messages (the Android SMS app), NOT WeChat. "
SMS_NAMES = ("messages", "message", "google messages", "android messages", "sms",
             "短信", "短信息", "信息")
APP_PACKAGES = {
    "taobao": "com.taobao.taobao", "淘宝": "com.taobao.taobao",
    "手机淘宝": "com.taobao.taobao", "jd": "com.jingdong.app.mall",
    "jd.com": "com.jingdong.app.mall", "京东": "com.jingdong.app.mall",
    "pinduoduo": "com.xunmeng.pinduoduo", "拼多多": "com.xunmeng.pinduoduo",
    "maps": MAPS_PACKAGE, "google maps": MAPS_PACKAGE, "地图": MAPS_PACKAGE, "谷歌地图": MAPS_PACKAGE,
    "google 地图": MAPS_PACKAGE,
    "contacts": CONTACTS_PACKAGE, "通讯录": CONTACTS_PACKAGE, "联系人": CONTACTS_PACKAGE,
    "google contacts": CONTACTS_PACKAGE,
    "clock": CLOCK_PACKAGE, "google clock": CLOCK_PACKAGE, "时钟": CLOCK_PACKAGE, "闹钟": CLOCK_PACKAGE,
    "meituan": MEITUAN_PACKAGE, "美团": MEITUAN_PACKAGE, "美团外卖": MEITUAN_PACKAGE, "外卖": MEITUAN_PACKAGE,
    "12306": RAILWAY_PACKAGE, "铁路12306": RAILWAY_PACKAGE, "railway 12306": RAILWAY_PACKAGE,
    "calendar": CALENDAR_PACKAGE, "日历": CALENDAR_PACKAGE, "google calendar": CALENDAR_PACKAGE,
    "fossify calendar": FOSSIFY_CALENDAR_PACKAGE,
    **{name: SMS_PACKAGE for name in SMS_NAMES},
    **{name: GMAIL_PACKAGE for name in MAIL_NAMES},
}


def is_sms_goal(goal: str) -> bool:
    """Route explicit SMS requests, not generic messages or WeChat requests."""
    if re.search(r"(?:不要|别|不需|don't|do not)\s*(?:打开|启动|进入|发(?:送)?|open|launch|send)\s*"
                 r"(?:一下\s*)?(?:短信|短信息|SMS\b|(?:Google\s+)?Messages?\b|text messages?\b)", goal, re.I):
        return False
    if re.search(r"微信|\bwechat\b", goal, re.I):
        return False
    return bool(re.search(
        r"短信|短信息|\bSMS\b|\btext messages?\b|"
        r"(?:打开|启动|进入|\bopen\b|\blaunch\b)\s*(?:一下\s*)?"
        r"(?:Google\s+)?Messages?\b", goal, re.I))


def expected_sms_body(goal: str) -> str | None:
    """The message body an SMS draft must contain, taken from the quoted text in the request.

    Used to stop a premature hand-off before the body is typed. Only the clearly-quoted body is
    returned (the recipient is never quoted); an unquoted request returns None, so no false guard.
    """
    match = re.search(r"『([^』]+)』|「([^」]+)」|“([^”]+)”|\"([^\"]+)\"|'([^']+)'", goal)
    if match:
        return next(group for group in match.groups() if group)
    return None


def is_sms_open_only(goal: str) -> bool:
    return bool(re.fullmatch(r"\s*(?:请\s*)?(?:打开|启动|进入|open|launch)\s*(?:一下\s*)?"
                            r"(?:短信|短信息|SMS|(?:Google\s+)?Messages?)[。.!！\s]*", goal, re.I))


def sms_draft_goal(goal: str) -> str:
    if not is_sms_goal(goal):
        return goal
    if is_sms_open_only(goal):
        return ("ONLY open Messages (Android SMS), NOT WeChat. If Messages is already visible, "
                "finish immediately with that observation. Do NOT start a conversation or "
                "prepare a draft. NEVER tap Send. Original request: " + goal)
    return (
        SMS_DRAFT_PREFIX +
        "Use UI to fill the intended recipient and message body, then Take_over "
        "for the owner to review and send manually. NEVER tap Send or press an "
        "Enter/Send key, and never claim that the SMS was delivered. Do not modify "
        "an existing draft unless it is clearly the same requested draft. If the "
        "recipient is missing/ambiguous, ask the owner instead of guessing. For a "
        "draft only, copy an explicitly supplied numeric recipient EXACTLY, even "
        "if short; never complete or invent digits. The owner must verify the "
        "full recipient before any manual sending. "
        "Ignore optional Save contact / Report spam banners when the message "
        "composer is usable; do not save contacts or dismiss unrelated controls. "
        "If the correct recipient and requested body are ALREADY visible in the "
        "composer, do not retype or tap anything. Return exactly this single call: "
        "do(action='Take_over', message='草稿已准备，未发送；请核对号码和正文后手动发送。') "
        "An open-only request needs no draft. Original owner request: " + goal
    )


def sms_completion_handoff(goal: str, action: dict) -> dict:
    if is_sms_goal(goal) and not is_sms_open_only(goal) and action.get("action") == "finish":
        return {"action": "request_user", "arguments": {"message":
                "短信任务已暂停，未自动发送。请在 Messages 核对收件人和正文，最后由你手动发送。"},
                "reason": "SMS drafts require owner review and manual sending", "risk": "sensitive",
                "native_action": action.get("native_action", ""), "policy_override": "sms_manual_send"}
    return action


def is_mail_compose_goal(goal: str) -> bool:
    """Writing/sending/replying to mail is draft-only; reading the inbox is not."""
    return bool(re.search(r"(?:写|发|回复?|转发|起草|草拟)(?:一封|一下|一个|这封|那封|封|个)?\s*(?:电子)?邮件|邮件给|发邮件|"
                          r"\b(?:send|write|compose|draft|reply|forward)\b.{0,30}\b(?:e-?mail|gmail|mail)\b|"
                          r"\be-?mail\s+to\b", goal, re.I))


def mail_draft_goal(goal: str) -> str:
    if not is_mail_compose_goal(goal):
        return goal
    return (
        MAIL_DRAFT_PREFIX +
        "Use Launch with app=\"Gmail\", tap Compose, fill To, Subject and the body exactly as requested, "
        "then use Take_over for the owner to review and send manually. NEVER tap Send, "
        "Schedule send, Delete, Archive or Spam, and never claim the email was sent. If the "
        "recipient address is missing or ambiguous, ask the owner instead of guessing. "
        "If the requested draft is already complete on screen, return exactly: "
        "do(action='Take_over', message='邮件草稿已写好，未发送；请核对收件人和正文后手动发送。') "
        "Original owner request: " + goal
    )


def mail_completion_handoff(goal: str, action: dict) -> dict:
    """A draft task never finishes as 'sent': hand the result to the owner."""
    if is_mail_compose_goal(goal) and action.get("action") == "finish":
        return {"action": "request_user", "arguments": {"message":
                "邮件草稿任务已暂停，未自动发送。请在 Gmail 核对收件人、主题和正文，最后由你手动发送。"},
                "reason": "Email drafts require owner review and manual sending", "risk": "sensitive",
                "native_action": action.get("native_action", ""), "policy_override": "mail_manual_send"}
    return action


def is_mail_to_calendar(goal: str) -> bool:
    """Read an email and put what it describes into the calendar."""
    return bool(re.search(r"(?:邮件|邮箱|\be-?mail\b|\bgmail\b).*(?:加到|添加到|加进|放到|放进|记到|写到|写进|同步到|存到).*(?:日历|日程)",
                          goal, re.I))


# Sequence words that join "first do X, THEN do Y"; longest first so a match consumes
# the whole connective phrase (e.g. prefer "然后再去" over "然后").
_CONNECTORS = sorted((
    "然后再帮我去", "然后再帮我", "然后再去", "然后帮我去", "然后帮我", "然后再", "然后去", "然后",
    "接着再帮我去", "接着再去", "接着去", "接着帮我", "接着", "随后再去", "随后去", "随后",
    "之后再帮我去", "之后再去", "之后去", "之后再", "之后", "再帮我去", "再帮我", "再去", "最后再", "最后",
), key=len, reverse=True)
_SEQUENCE = re.compile(r"[，,。；;、\s]*(?:" + "|".join(_CONNECTORS) + r")[，,：:、\s]*")
# Quoted message bodies: a connector inside the drafted text is not a task boundary.
_QUOTED = re.compile(r"『[^』]*』|「[^」]*」|“[^”]*”|‘[^’]*’|\"[^\"]*\"|'[^']*'")
_TRIM = " 　，,。.；;、:："


def split_composite_goal(goal: str) -> tuple[str, str | None]:
    """Split a "draft first, THEN another task" request into (first, rest).

    Only a request whose FIRST part is an SMS or mail *draft* is split, because
    only a draft pauses for an owner takeover and so would otherwise strand the
    rest of the goal. After the owner takes the draft to the main screen, the
    bridge continues with `rest` on the private display. A generic multi-step
    goal (no leading draft) is left whole for the model to work through in one
    run, so this returns (goal, None).
    """
    if is_mail_to_calendar(goal):
        return goal, None
    spans = [m.span() for m in _QUOTED.finditer(goal)]
    for match in _SEQUENCE.finditer(goal):
        if any(start <= match.start() < end for start, end in spans):
            continue  # the connector sits inside the drafted message, not between tasks
        first = goal[: match.start()].strip(_TRIM)
        rest = goal[match.end():].strip(_TRIM)
        if len(rest) < 2 or not first:
            continue
        if is_sms_goal(first) or is_mail_compose_goal(first):
            return first, rest
    return goal, None


def draft_phase_goal(full: str, leg: str) -> str:
    """First leg of a composite "draft THEN more" task: complete the draft fully, then hand off.

    The primary instruction is the draft leg alone (so the body stays prominent and the model does
    not rush past it); the whole request follows as context so the task still reads as connected.
    Leading with the full request made the model select the recipient and hand off without the body.
    """
    return (mail_draft_goal(sms_draft_goal(leg)) +
            " （这是一条连续任务的第一步。完整任务：" + full + "。现在只、且完整地做好这条短信/邮件草稿——"
            "收件人和正文都要填好，再 Take_over 交接；后面的部分（例如去 12306 订票）会在你交接之后"
            "自动继续，不要现在就去做，也不要因为后面还有任务就跳过正文或提前交接。）")


def continuation_goal(full: str, rest: str) -> str:
    """Model instructions for the SECOND leg of a composite task, keeping the whole request in view.

    The draft was already prepared and handed to the owner's main screen, so this leg must not
    reopen Messages/Gmail; it only finishes the remaining part. The full request is shown so the
    task reads as one connected flow rather than a disconnected fragment.
    """
    return (
        "这是一条连续任务的第二步。第一步的短信/邮件草稿已经准备好并交到主屏，由主人自己核对后手动发送——"
        "不要再打开短信或 Messages/Gmail，也不要重复准备或发送它。现在只需完成这条任务剩下的部分："
        + rest + "。（完整任务：" + full + "）"
    )

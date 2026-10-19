"""Which app a goal opens before the model starts (explicit, single-app requests only)."""

from __future__ import annotations

import re

from app_targets import (CALENDAR_PACKAGE, GMAIL_PACKAGE, MEITUAN_PACKAGE, RAILWAY_PACKAGE, SMS_PACKAGE,
                         is_mail_compose_goal, is_mail_to_calendar, is_sms_goal)


CALENDAR_GOAL = re.compile(r"日历|日程|行程|有什么安排|\bcalendar\b|(?:新建|创建|建|安排|添加|约)(?:一个|个)?.{0,15}(?:会议|周会|例会|会)",
                           re.I)
RAILWAY_GOAL = re.compile(r"12306|火车票|高铁票|动车票|(?:高铁|动车|火车).{0,6}(?:票|车次|余票)")


def explicit_target_package(goal: str) -> str | None:
    if CALENDAR_GOAL.search(goal) and (is_sms_goal(goal) or is_mail_compose_goal(goal)):
        return None  # two apps (calendar, then a message): the model picks the order itself
    if is_sms_goal(goal):
        return SMS_PACKAGE
    if is_mail_compose_goal(goal) or is_mail_to_calendar(goal):
        return GMAIL_PACKAGE
    if CALENDAR_GOAL.search(goal) and not re.search(r"不要|别|不需|don't|do not", goal, re.I):
        return CALENDAR_PACKAGE
    if RAILWAY_GOAL.search(goal):
        return RAILWAY_PACKAGE  # Chinese train tickets: 12306 is the only railway app here
    # Route only an explicit, single-app open request; merely mentioning a brand
    # or asking for a comparison must not navigate the existing workspace.
    names = {"京东": "com.jingdong.app.mall", "jd": "com.jingdong.app.mall",
             "淘宝": "com.taobao.taobao", "taobao": "com.taobao.taobao",
             "拼多多": "com.xunmeng.pinduoduo", "pinduoduo": "com.xunmeng.pinduoduo",
             "美团": MEITUAN_PACKAGE, "meituan": MEITUAN_PACKAGE, "外卖": MEITUAN_PACKAGE}
    mentioned = {package for name, package in names.items()
                 if re.search(re.escape(name) if not name.isascii() else rf"\b{name}\b", goal, re.I)}
    if len(mentioned) != 1 or re.search(r"不要|别|不需|don't|do not", goal, re.I):
        return None
    if mentioned == {MEITUAN_PACKAGE}:
        return MEITUAN_PACKAGE  # "用美团…" / "点外卖…": takeout only ever happens in 美团 here
    match = re.search(r"(?:打开|启动|进入|\bopen\b|\blaunch\b)\s*(?:一下\s*)?(京东|淘宝|拼多多|JD\b|Taobao\b|Pinduoduo\b)", goal, re.I)
    return names[match.group(1).lower()] if match else None

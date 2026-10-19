"""Action validation and the word-level safety checks run before any action executes."""

from __future__ import annotations

import json
from typing import Any

from agent_common import AgentError


ALLOWED_ACTIONS = {
    "open_url",
    "launch_app",
    "tap",
    "swipe",
    "input_text",
    "key",
    "wait",
    "request_user",
    "finish",
}
SENSITIVE_TERMS = (
    "pay",
    "payment",
    "purchase",
    "checkout",
    "password",
    "passcode",
    "verification code",
    "otp",
    "delete",
    "uninstall",
    "send message",
    "send email",
    "agree to terms",
    "付款",
    "支付",
    "购买",
    "密码",
    "验证码",
    "删除",
    "卸载",
    "发送",
    "同意条款",
    "captcha",
    "login",
    "登录",
    "快速验证",
    "京东验证",
    "安全验证",
    "security verification",
    "人机验证",
)


def validate_input_text(value: str) -> None:
    if not 1 <= len(value) <= 500:
        raise AgentError("input_text is limited to 1..500 characters")
    if any(ord(c) < 32 or 127 <= ord(c) <= 159 or 0xD800 <= ord(c) <= 0xDFFF for c in value):
        raise AgentError("input_text cannot contain control characters or invalid Unicode")


def require_string(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AgentError(f"{key} must be a non-empty string")
    return value


def bounded_int(arguments: dict[str, Any], key: str, low: int, high: int) -> int:
    try:
        value = int(arguments[key])
    except (KeyError, TypeError, ValueError) as error:
        raise AgentError(f"{key} must be an integer") from error
    if not low <= value <= high:
        raise AgentError(f"{key} must be between {low} and {high}")
    return value
SEND_TERMS = ("send message", "send email", "发送")


def order_block_message(error: Exception) -> str:
    """Chinese explanation for a page only the owner may act on (see check_order_page)."""
    word = str(error)
    if any(w in word.lower() for w in ("全部接受", "全部拒绝", "接受全部", "拒绝全部", "accept all", "reject all")):
        message = ("网页在请你选择是否接受 Cookie。这算同意条款，Agent 没有替你选。"
                   "请点「接管到主屏」自己选（查航班选「全部拒绝」也可以），然后重新提交任务。")
    elif any(w in word.lower() for w in ("预订选项", "book with")):
        message = ("已经到了预订页面，Agent 在这里停下，没有点击。订票请你点「接管到主屏」自己处理。")
    elif any(w in word for w in ("协议", "隐私政策")):
        message = ("美团在请你同意用户协议/隐私政策。是否同意由你决定，Agent 没有点击。"
                   "请点「接管到主屏」阅读后自己选择；同意后可以重新提交任务。")
    elif any(w in word for w in ("验证码", "登录")):
        message = ("美团需要登录（手机号和验证码）。Agent 不代你登录，没有点击。"
                   "请点「接管到主屏」自己登录，完成后重新提交任务。")
    else:
        message = ("已经到了下单/付款页面，Agent 在这里停下，没有点击任何按钮。"
                   "请点「接管到主屏」核对商品、地址和金额，由你决定是否下单付款。")
    return message


def sensitive_reason(action: dict[str, Any], ignore: tuple[str, ...] = ()) -> str | None:
    if str(action.get("risk", "none")).lower() == "sensitive":
        return "the model marked this action as sensitive"
    searchable = " ".join(
        [
            str(action.get("reason", "")),
            json.dumps(action.get("arguments", {}), ensure_ascii=False),
        ]
    ).lower()
    for term in SENSITIVE_TERMS:
        if term in searchable and term not in ignore:
            return f"the action mentions sensitive operation {term!r}"
    return None

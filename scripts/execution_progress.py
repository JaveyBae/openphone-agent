"""User-facing execution updates, generated from stages/actions, never reasoning."""
from __future__ import annotations

import json
from pathlib import Path

from app_targets import SMS_PACKAGE

PREFIX = "APP_PROGRESS "
STAGES = {"initialize", "reset", "skill", "launch", "observe", "model", "predicted", "execute", "verified", "replan",
          "paused", "finished"}
ACTION_LABELS = {
    "open_url": "打开网页", "launch_app": "打开应用", "tap": "操作页面",
    "swipe": "滚动页面", "input_text": "填写输入框", "key": "页面内导航",
    "wait": "等待页面", "finish": "整理结果", "request_user": "等待你确认",
}
APP_LABELS = {SMS_PACKAGE: "Messages（短信）", "com.jingdong.app.mall": "京东",
              "com.google.android.gm": "Gmail", "com.google.android.calendar": "Google 日历",
              "org.fossify.calendar": "离线日历",
              "com.taobao.taobao": "淘宝", "com.xunmeng.pinduoduo": "拼多多", "com.sankuai.meituan": "美团",
              "com.google.android.deskclock": "时钟", "com.google.android.apps.maps": "Google 地图",
              "com.google.android.contacts": "通讯录"}


def skill_labels() -> set[str]:
    """Step labels from the repository's own skill files: the only skill copy shown."""
    labels = set()
    for path in (Path(__file__).resolve().parents[1] / "skills").glob("*.json"):
        labels.update(step["label"] for step in json.loads(path.read_text(encoding="utf-8"))["steps"])
    return labels


SKILL_LABELS = skill_labels()


def progress_text(event: dict) -> str | None:
    """Only known metadata can become UI copy; arbitrary model prose is ignored."""
    if (not isinstance(event, dict) or not isinstance(event.get("stage"), str)
            or event["stage"] not in STAGES
            or not isinstance(event.get("action", ""), str)
            or not isinstance(event.get("package", ""), str)
            or (event["stage"] == "skill" and event.get("label") not in SKILL_LABELS)):
        return None
    step, total = event.get("step"), event.get("total")
    if type(step) is not int or type(total) is not int or not 0 <= step <= total <= 50 or total < 1:
        return None
    stage = event["stage"]
    label = ACTION_LABELS.get(event.get("action"), "操作页面")
    labels = {
        "initialize": "检查独立工作区，不读取主屏…",
        "reset": "清理后台屏幕上次留下的页面，准备干净的起点…",
        "skill": "使用已学会的技能：" + str(event.get("label", "")) + "…",
        "launch": "正在打开" + APP_LABELS.get(event.get("package"), "目标应用") + "…",
        "observe": "正在读取私有屏幕，核对当前页面…",
        "model": "模型处理中：正在选择下一步操作…",
        "predicted": "页面与学到的经验一致：直接" + label + "，不等模型…",
        "execute": "操作已校验，正在" + label + "…",
        "verified": "已执行：" + label + "；继续核对私有屏幕…",
        "replan": "该控件未被识别，未执行点击；正在选择安全的其他路径…",
        "paused": "任务暂停，等待你处理或确认。",
        "finished": "执行结束，正在整理结果。",
    }
    return f"第 {step}/{total} 步 · " + labels[stage]


def emit_progress(stage: str, step: int, total: int, **metadata) -> None:
    event = {"stage": stage, "step": step, "total": total, **metadata}
    if progress_text(event) is None:
        raise ValueError("Invalid public execution progress")
    print(PREFIX + json.dumps(event, ensure_ascii=False), flush=True)


def parse_progress(line: str) -> dict | None:
    if not line.startswith(PREFIX):
        return None
    try:
        event = json.loads(line[len(PREFIX):])
    except json.JSONDecodeError:
        return None
    return event if progress_text(event) is not None else None

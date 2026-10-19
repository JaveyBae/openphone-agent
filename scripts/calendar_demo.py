"""Offline calendar preset, executed through private UI, not a model or database API."""
from __future__ import annotations
import base64
from datetime import date, datetime, time, timedelta
import json
import fcntl
from pathlib import Path
import shlex
import time as clock
from zoneinfo import ZoneInfo
from agent_common import AgentError
from android_device import AndroidController, locate_adb
from workspace_runtime import ensure_running, install_text_helper

CALENDAR_PACKAGE = "org.fossify.calendar"
TITLE = "OpenPhone 项目演示"
LOCATION = "线上会议"
DESCRIPTION = "演示日程：验证 Agent 在私有屏创建日程，用户可以继续使用主屏。无参会人，不发送邀请。"
ZONE = "Europe/Zurich"

def display_title(spec: dict) -> str:
    return TITLE + " · " + spec["date"]

def preset(day: date | None = None, reminder: int = 0) -> dict:
    day = day or datetime.now(ZoneInfo(ZONE)).date() + timedelta(days=1)
    return {"date": day.isoformat(), "timezone": ZONE, "title": TITLE,
            "start": "15:00", "end": "15:30", "location": LOCATION, "reminderMinutes": reminder}

def validate_spec(spec: dict) -> tuple[datetime, datetime]:
    if not isinstance(spec, dict) or set(spec) != set(preset(date(2026, 1, 1))):
        raise AgentError("日程演示参数不完整；未操作日历。")
    try:
        day = date.fromisoformat(spec["date"])
    except (ValueError, TypeError):
        raise AgentError("日程日期无效；未操作日历。")
    if type(spec["reminderMinutes"]) is not int or spec["reminderMinutes"] not in (0,10) or spec != preset(day,spec["reminderMinutes"]):
        raise AgentError("当前只支持界面中明确展示的日程预设；未操作日历。")
    start = datetime.combine(day, time(15), ZoneInfo(ZONE))
    return start, start + timedelta(minutes=30)

class CalendarUiClient:
    def __init__(self, controller: AndroidController, jar: str):
        self.c, self.jar = controller, jar
        self.state = controller.state()
        self.display = controller.require_display()
        self.last_nodes: list[dict] = []

    def call(self, mode: str, selector: dict | None = None, text: str | None = None) -> dict:
        if self.c.state() != self.state:
            raise AgentError("日历工作区已变化，操作停止。")
        self.c.require_isolation(self.display)
        before = self.c.require_owner_focus(self.display, private_window=True)
        parts = [str(self.display), self.state["session"], mode]
        for value in ([json.dumps(selector, ensure_ascii=False)] if selector is not None else []) + ([text] if text is not None else []):
            parts.append(base64.b64encode(value.encode("utf-8")).decode("ascii"))
        command = "CLASSPATH=" + shlex.quote(self.jar) + " app_process / ai.openphone.workspace.CalendarUi " + " ".join(map(shlex.quote, parts))
        result = json.loads(self.c._run("shell", command))
        if not isinstance(result,dict) or result.get("ok") is not True or result.get("verified") is not True \
                or result.get("displayId") != self.display or result.get("session") != self.state["session"] \
                or result.get("package") != CALENDAR_PACKAGE:
            raise AgentError("日历 UI 检查失败：" + str(result.get("error","identity mismatch") if isinstance(result,dict) else "invalid response"))
        if mode == "text" and result.get("readback") is not True:
            raise AgentError("日历输入没有通过读回校验。")
        if self.c.state() != self.state:
            raise AgentError("日历会话已变化，操作停止。")
        result["focus"] = {"before": before, "after": self.c.require_owner_focus(self.display,private_window=True)}
        return result

    def snapshot(self) -> list[dict]:
        self.last_nodes = self.call("snapshot")["nodes"]
        return self.last_nodes

    def click(self, selector: dict):
        return self.call("click", selector)

    def text(self, selector: dict, value: str):
        return self.call("text", selector, value)

def node_id(suffix: str) -> dict:
    return {"id": CALENDAR_PACKAGE + ":id/" + suffix}

def field(nodes: list[dict], suffix: str) -> dict:
    found = [n for n in nodes if n.get("id") == node_id(suffix)["id"]]
    if len(found) != 1:
        raise AgentError("日历字段缺失或不唯一：" + suffix)
    return found[0]

def verify_editor(nodes: list[dict], spec: dict) -> None:
    start, end = validate_spec(spec)
    expected = {
        "event_title": display_title(spec), "event_location": LOCATION, "event_description": DESCRIPTION,
        "event_start_date": start.strftime("%B ") + str(start.day) + start.strftime(" (%a)"),
        "event_end_date": end.strftime("%B ") + str(end.day) + end.strftime(" (%a)"),
        "event_start_time": "03:00 PM", "event_end_time": "03:30 PM",
        "event_reminder_1": "10 minutes before" if spec["reminderMinutes"] else "No reminder",
    }
    for name, value in expected.items():
        if field(nodes,name).get("text") != value:
            raise AgentError("日历字段未通过核对：" + name + "；没有确认保存成功。")
    if field(nodes,"event_all_day").get("checked") is not False:
        raise AgentError("日程意外变成全天；操作停止。")

def verify_local_calendar(nodes: list[dict]) -> None:
    for name, text in {"event_calendar_title":"Local calendar",
                       "event_calendar_subtitle":"Offline (never synced)",
                       "event_repetition":"No repetition"}.items():
        if field(nodes,name).get("text") != text:
            raise AgentError("必须是非重复的离线本地日程；操作停止。")

def run_calendar_demo(spec: dict, runs_dir: Path, progress=lambda message: None) -> str:
    """One explicit preset only; verify persistence by searching and reopening it."""
    start,end = validate_spec(spec)
    if start <= datetime.now(ZoneInfo(ZONE)):
        raise AgentError("预设时间已过，请重新点击日程演示，生成明天的预设。")
    runs_dir = Path(runs_dir); runs_dir.mkdir(parents=True,exist_ok=True)
    with (runs_dir / "calendar-demo.lock").open("a") as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AgentError("已有日程演示正在执行，未重复创建。")
        run_dir = runs_dir / ("calendar-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
        run_dir.mkdir(); events=[]
        def record(stage: str, result=None):
            events.append({"stage":stage,"result":result})
            (run_dir / "audit.json").write_text(json.dumps({"mode":"deterministic_ui_preset",
                "spec":spec,"events":events},ensure_ascii=False,indent=2),encoding="utf-8")
        try:
            progress("本地 UI 预设：检查私有屏与日历，不调用模型…")
            state=ensure_running(); c=AndroidController(locate_adb()); d=state["displayId"]
            if spec["reminderMinutes"] and "android.permission.POST_NOTIFICATIONS: granted=true" not in str(c._run("shell","dumpsys","package",CALENDAR_PACKAGE)):
                raise AgentError("提前提醒需要 Fossify 日历的通知权限；请先授权，或取消勾选提醒。未点击保存。")
            if str(c._run("shell","getprop","persist.sys.timezone")).strip() != ZONE:
                raise AgentError("模拟器时区不是 Europe/Zurich，未修改你的系统时区或创建日程。")
            # Same clean start as model tasks: close what earlier runs left on the private screen.
            record("workspace_reset",{"removed":c.reset_private_workspace(d)})
            c.execute({"action":"launch_app","arguments":{"package":CALENDAR_PACKAGE}})
            clock.sleep(.5)
            u=CalendarUiClient(c,install_text_helper()); nodes=u.snapshot()
            if any(n.get("id")==node_id("event_title")["id"] for n in nodes):
                raise AgentError("私有日历有未关闭的编辑页面，请先人工保存或返回，避免覆盖草稿。")
            record("launch", {"display":d,"focus":c.require_owner_focus(d,private_window=True)})

            def search():
                u.text(node_id("top_toolbar_search"),display_title(spec)); clock.sleep(.8)
                items=u.snapshot()
                matches=[n for n in items if n.get("text")==display_title(spec) and not n.get("editable")]
                empty=any(n.get("id")==node_id("search_placeholder")["id"]
                          and n.get("text")=="No items found." for n in items)
                if len(matches)>1: raise AgentError("发现多条同名日程，需要你核对，未新增或删除。")
                if not matches and not empty: raise AgentError("不能确认搜索结果为空，未尝试保存。")
                return bool(matches)

            progress("本地 UI 预设：搜索同名日程，避免重复创建…")
            exists=search()
            if exists:
                u.call("open",{**node_id("event_item_title"),"text":display_title(spec)});clock.sleep(.4)
                # Event detail may expose an Edit button rather than an editor.
                nodes=u.snapshot()
                if not any(n.get("id")==node_id("event_title")["id"] for n in nodes):
                    edit=[n for n in nodes if n.get("description")=="Edit" and n.get("clickable")]
                    if len(edit)!=1: raise AgentError("已有日程需要人工打开详情核对，未重复创建。")
                    u.click({"description":"Edit"})
            else:
                u.click(node_id("top_toolbar_search_icon"))
                c.require_isolation(d);c.require_owner_focus(d,private_window=True)
                c.require_package_off_owner_display(CALENDAR_PACKAGE,d)
                if c.state()!=state: raise AgentError("私有会话变化，操作停止。")
                # Calendar INSERT intent opens an editor; it does NOT insert a row.
                c._run("shell","am","start","--display",str(d),"-a","android.intent.action.INSERT",
                       "-t","vnd.android.cursor.dir/event","-n",CALENDAR_PACKAGE+"/.activities.EventActivity",
                       "--el","beginTime",str(int(start.timestamp()*1000)),
                       "--el","endTime",str(int(end.timestamp()*1000)))
                clock.sleep(.5);c.require_owner_focus(d,private_window=True)
                progress("本地 UI 预设：填写中文标题、地点与说明…")
                for suffix,value in [("event_title",display_title(spec)),("event_location",LOCATION),("event_description",DESCRIPTION)]:
                    record("fill_"+suffix,u.text(node_id(suffix),value))
                reminder_text=field(u.snapshot(),"event_reminder_1").get("text")
                expected_reminder="10 minutes before" if spec["reminderMinutes"] else "No reminder"
                if reminder_text != expected_reminder:
                    if not spec["reminderMinutes"] and "android.permission.POST_NOTIFICATIONS: granted=true" not in str(c._run("shell","dumpsys","package",CALENDAR_PACKAGE)):
                        raise AgentError("请先在 Fossify 设置中关闭沿用上次提醒，并把默认提醒设为 No reminder；未点击保存。")
                    u.click(node_id("event_reminder_1"));u.call("open",{"text":expected_reminder})
            verify_editor(u.snapshot(),spec)
            record("editor_verified",{"existing":exists})
            c.capture(run_dir / "editor.png")
            u.call("scroll",node_id("event_nested_scrollview"));verify_local_calendar(u.snapshot())
            record("offline_calendar_verified")
            if not exists:
                progress("本地 UI 预设：字段核对通过，点击保存到离线日历…")
                # Record BEFORE Save: never automatically replay an uncertain save.
                record("save_attempted");u.click(node_id("save"));clock.sleep(.5)
                nodes=u.snapshot()
                if any(n.get("id")==node_id("reminder_warning")["id"] for n in nodes):
                    warning=field(nodes,"reminder_warning").get("text","")
                    if not warning.startswith("Please make sure the reminders work properly"):
                        raise AgentError("未识别的提示，等待人工处理。")
                    u.click({"id":"android:id/button1","text":"OK"});clock.sleep(.5)
                if not search(): raise AgentError("保存后没有搜到日程，请人工核对，未重试保存。")
                u.call("open",{**node_id("event_item_title"),"text":display_title(spec)});clock.sleep(.4)
                nodes=u.snapshot()
                if not any(n.get("id")==node_id("event_title")["id"] for n in nodes):
                    u.click({"description":"Edit"})
                verify_editor(u.snapshot(),spec)
                record("persisted_editor_readback")
                c.capture(run_dir / "persisted.png")
                u.call("scroll",node_id("event_nested_scrollview"));verify_local_calendar(u.snapshot())
            # Finish on a read-only detail/list, not an editor with an active Save button.
            u.click({"description":"Back"});clock.sleep(.5)
            c.capture(run_dir / "result.png")
            record("verified",{"focus":c.require_owner_focus(d,private_window=True),"existing":exists})
            progress("本地 UI 预设：已重新打开并核对日程；没有发送邀请。")
            return ("已核对已有日程，没有重复创建。" if exists else "日程已保存并重新打开核对。") + \
                f"\n{display_title(spec)}\n{spec['date']} 15:00–15:30（苏黎世）\n地点：{LOCATION}；" \
                + ("提醒设置：提前 10 分钟，实际通知尚未触发验证。" if spec["reminderMinutes"] else "未设置弹窗提醒。") \
                + "\n本地 UI 预设，无模型调用、无邀请。"
        except Exception as error:
            record("paused",{"error":str(error)});raise

"""Teacher dataset for the Qwen3.8-27B planner, labelled by Claude in hindsight.

Every step of our real runs becomes one item: goal, actions already taken,
the screenshot, the action the agent took, and the screenshot after it. The
teacher sees all of that (the student never sees the "after" screenshot) and
answers: was the action right, what should the next action be, and one
sentence of reasoning grounded in the screen. The student is then trained to
produce exactly that reasoning + JSON action from the same text and image it
gets at run time (qwen_planner.user_text / PROMPT).

  python3 scripts/teacher_dataset.py tasks          # runs/ -> teacher_data/tasks.jsonl + marked images
  python3 scripts/teacher_dataset.py label-api      # label with the Claude API (ANTHROPIC_API_KEY)
  python3 scripts/teacher_dataset.py build          # tasks + labels -> teacher_data/sft.jsonl

Labels can also come from Claude in a Claude Code session: each labeller reads
a chunk of tasks and appends lines to teacher_data/labels/*.jsonl in the format
of TEACHER_PROMPT. Private apps (mail, SMS, shopping) and the evaluation runs
of model_eval.py are left out.
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from model_eval import DEFAULT_RUNS, HEIGHT, WIDTH  # noqa: E402
from qwen_planner import PROMPT, to_json_action, user_text  # noqa: E402

OUT = ROOT / "teacher_data"
# Goals whose screens hold only test data. Mail, SMS and shopping show the owner's own account.
INCLUDE = re.compile(r"联系人|通讯录|contact|闹钟|闹铃|叫我起床|alarm|日历|日程|会议|calendar|event|地图|路线|"
                     r"route|maps|机票|航班|flight|维基|wikipedia", re.I)
EXCLUDE = re.compile(r"邮件|mail|短信|sms|message|京东|jd|美团|淘宝|拼多多|有什么安排", re.I)

TEACHER_PROMPT = """You are the teacher for a phone-operating model. The student sees only the task,
the actions already taken and the CURRENT screenshot, and must output one sentence of reasoning and
one JSON action in this format:
""" + PROMPT.split("Choose exactly ONE next action.")[1] + """

You get, for one step of a real run: the task, the actions already taken, the CURRENT screenshot
(the action the agent took is marked on it with a red circle for taps, or described in text), the
action taken, and the AFTER screenshot that resulted. Decide:
- "good": the action taken is a correct next step toward the task (the AFTER screen confirms it);
- "bad": it is wrong, useless or premature (e.g. typing into the wrong field, saving before every
  requested field is filled, scrolling the wrong way, finishing without the result visible);
- "unclear": you cannot tell from the screens.
Reply with exactly one JSON object, nothing else:
{"verdict": "good|bad|unclear",
 "reason": "<one sentence the STUDENT could say from the CURRENT screen only: what it sees and why the
            next action follows; no mention of the AFTER screen, the red circle or the teacher>",
 "action": <the correct next action in the student's JSON format>}
For "good", copy the action taken (same coordinates). For "bad", give the action you would take;
estimate tap coordinates on the 0-1000 grid of the CURRENT screenshot (x right, y down) at the centre
of the control. Use the language of the task for finish/take_over messages."""


def verified(meta: dict) -> bool:
    check = meta.get("verification") or {}
    return meta.get("status") == "finished" and bool(check.get("checked")) and not check.get("missing")


def run_items(run: Path) -> list[dict]:
    meta = json.loads((run / "run.json").read_text())
    events = [json.loads(line) for line in (run / "events.jsonl").read_text().splitlines() if line.strip()]
    steps = [e for e in events if e.get("step") is not None and e.get("screenshot")
             and e.get("result") in ("executed", "finished") and isinstance(e.get("action"), dict)
             and e["action"].get("action") != "wait"]
    items, history = [], []
    for index, event in enumerate(steps):
        taken = to_json_action(event["action"], WIDTH, HEIGHT)
        if taken is None or not (run / event["screenshot"]).exists():
            continue
        after = steps[index + 1]["screenshot"] if index + 1 < len(steps) else None
        items.append({"id": f"{run.name}-{event['step']:02d}", "run": run.name, "goal": meta["goal"],
                      "status": meta.get("status"), "verified": verified(meta),
                      "history": [dict(h) for h in history], "screenshot": str(run / event["screenshot"]),
                      "after": str(run / after) if after else None, "taken": taken,
                      "student_text": user_text(meta["goal"], history, WIDTH, HEIGHT)})
        history.append({"action": event["action"],
                        "private_input_focused": bool(event.get("private_input_focused"))})
    return items


def mark(item: dict, folder: Path) -> str:
    """A copy of the screenshot with the taken tap circled, for the teacher only."""
    from PIL import Image, ImageDraw
    target = folder / f"{item['id']}.png"
    image = Image.open(item["screenshot"]).convert("RGB")
    if item["taken"]["action"] == "tap":
        x, y = item["taken"]["x"] * image.width // 1000, item["taken"]["y"] * image.height // 1000
        draw = ImageDraw.Draw(image)
        for r in (22, 23, 24):
            draw.ellipse((x - r, y - r, x + r, y + r), outline=(255, 0, 0))
    image.save(target)
    return str(target)


def tasks(args) -> None:
    runs = sorted(p for p in (ROOT / "runs").iterdir() if (p / "run.json").exists() and p.name not in DEFAULT_RUNS)
    marked = args.out / "marked"
    marked.mkdir(parents=True, exist_ok=True)
    items = []
    for run in runs:
        goal = json.loads((run / "run.json").read_text()).get("goal", "")
        if not INCLUDE.search(goal) or EXCLUDE.search(goal) or not (run / "events.jsonl").exists():
            continue
        for item in run_items(run):
            item["marked"] = mark(item, marked)
            items.append(item)
    with (args.out / "tasks.jsonl").open("w") as handle:
        for item in items:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
    (args.out / "TEACHER.md").write_text(TEACHER_PROMPT + "\n")
    chunks = args.out / "chunks"
    chunks.mkdir(exist_ok=True)
    for old in chunks.glob("*.jsonl"):
        old.unlink()
    for start in range(0, len(items), args.chunk):
        with (chunks / f"chunk-{start // args.chunk:02d}.jsonl").open("w") as handle:
            for item in items[start:start + args.chunk]:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
    print(f"{len(items)} items from {len({i['run'] for i in items})} runs -> {args.out}")


def teacher_text(item: dict) -> str:
    return (f"Task: {item['goal']}\nActions already taken:\n{item['student_text'].split('Actions already taken:', 1)[1].strip()}"
            f"\nAction taken at this step: {json.dumps(item['taken'], ensure_ascii=False)}"
            f"\nRun outcome: {item['status']}{' (result verified in the app)' if item['verified'] else ''}")


def image_block(path: str) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                        "data": base64.b64encode(Path(path).read_bytes()).decode("ascii")}}


def parse_label(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.S)
    label = json.loads(match.group(0))
    if label.get("verdict") not in ("good", "bad", "unclear") or not isinstance(label.get("action"), dict):
        raise ValueError("label without verdict/action")
    return label


def label_api(args) -> None:
    import anthropic
    client = anthropic.Anthropic()
    labels = args.out / "labels"
    labels.mkdir(exist_ok=True)
    target = labels / "api.jsonl"
    done = {json.loads(line)["id"] for path in labels.glob("*.jsonl") for line in path.read_text().splitlines() if line}
    items = [json.loads(line) for line in (args.out / "tasks.jsonl").read_text().splitlines()]
    with target.open("a") as handle:
        for item in items:
            if item["id"] in done:
                continue
            content = [{"type": "text", "text": teacher_text(item)},
                       {"type": "text", "text": "CURRENT screenshot:"}, image_block(item["marked"])]
            if item["after"]:
                content += [{"type": "text", "text": "AFTER screenshot:"}, image_block(item["after"])]
            reply = client.messages.create(model=args.model, max_tokens=600, system=TEACHER_PROMPT,
                                           messages=[{"role": "user", "content": content}])
            try:
                label = parse_label(reply.content[0].text)
            except (ValueError, AttributeError, json.JSONDecodeError) as error:
                print(f"skip {item['id']}: {error}")
                continue
            handle.write(json.dumps({"id": item["id"], **label}, ensure_ascii=False) + "\n")
            handle.flush()
            print(item["id"], label["verdict"])


def build(args) -> None:
    items = {json.loads(line)["id"]: json.loads(line) for line in (args.out / "tasks.jsonl").read_text().splitlines()}
    labels = {}
    for path in sorted((args.out / "labels").glob("*.jsonl")):
        for line in path.read_text().splitlines():
            if line.strip():
                label = json.loads(line)
                labels[label["id"]] = label
    counts = {"good": 0, "bad": 0, "unclear": 0, "missing": 0}
    with (args.out / "sft.jsonl").open("w") as handle:
        for item_id, item in items.items():
            label = labels.get(item_id)
            if not label:
                counts["missing"] += 1
                continue
            counts[label["verdict"]] += 1
            if label["verdict"] == "unclear":
                continue
            # A verified "good" step keeps the coordinates that actually worked on the device.
            action = item["taken"] if label["verdict"] == "good" else label["action"]
            handle.write(json.dumps({"id": item_id, "verdict": label["verdict"], "images": [item["screenshot"]],
                                     "messages": [
                {"role": "system", "content": PROMPT},
                {"role": "user", "content": [{"type": "text", "text": item["student_text"]}, {"type": "image"}]},
                {"role": "assistant", "content": label["reason"].strip() + "\n" + json.dumps(action, ensure_ascii=False)},
            ]}, ensure_ascii=False) + "\n")
    print(json.dumps(counts), "->", args.out / "sft.jsonl")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("tasks", "label-api", "build"))
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--chunk", type=int, default=40)
    parser.add_argument("--model", default="claude-opus-5-5")
    args = parser.parse_args()
    {"tasks": tasks, "label-api": label_api, "build": build}[args.command](args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

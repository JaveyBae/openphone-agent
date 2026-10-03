#!/usr/bin/env python3
"""Send one screenshot to AutoGLM; report its plan without executing any action."""
import argparse
import base64
import json
import time
import urllib.request
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--goal", default="Describe the visible screen and propose one next UI action.")
    parser.add_argument("--base-url", default="http://127.0.0.1:18085/v1")
    parser.add_argument("--model", default="autoglm-phone-9b-multilingual")
    parser.add_argument("--output", type=Path, help="Optional JSON evidence file")
    parser.add_argument("--messages-file", type=Path,
                        help="Use system messages from an official AutoGLM sample JSON")
    args = parser.parse_args()
    encoded = base64.b64encode(args.image.read_bytes()).decode("ascii")
    body = {
        "model": args.model,
        "messages": [
            {"role": "system", "content": (
                "You are a phone UI agent. Inspect the screenshot and propose one action. "
                "Do not claim to have executed it. Return only one native AutoGLM action, "
                'such as do(action="Tap", element=[x,y]), or finish(message="..."). '
                "Coordinates are normalized to 0..1000 relative to the screenshot."
            )},
            {"role": "user", "content": [
                {"type": "text", "text": args.goal},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + encoded}},
            ]},
        ],
        "temperature": 0,
        "max_tokens": 512,
    }
    if args.messages_file:
        sample = json.loads(args.messages_file.read_text(encoding="utf-8"))
        system_messages = [m for m in sample if m.get("role") == "system"]
        if not system_messages:
            raise ValueError("Sample JSON has no system message")
        body["messages"] = system_messages + [body["messages"][-1]]
    request = urllib.request.Request(
        args.base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=180) as response:
        result = json.load(response)
    content = result["choices"][0]["message"]["content"]
    if not content:
        raise RuntimeError("API returned no visible model output")
    evidence = json.dumps({
        "model": result.get("model"),
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "usage": result.get("usage"),
        "output": content,
        "actions_executed": 0,
    }, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(evidence + "\n", encoding="utf-8")
    print(evidence)


if __name__ == "__main__":
    main()

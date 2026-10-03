# Open-AutoGLM on n30285

The server runs the vision model. The Mac retains ADB access and controls the
Android companion app's private display. No public ADB port is opened.

| Component | Deployment |
| --- | --- |
| Official framework | `/opt/open-autoglm`, commit `86f55382982fb054e8fc98ca80609dff8a2cdc3c` |
| Framework Python | `/opt/open-autoglm/.venv/bin/python` (3.10) |
| Model | `zai-org/AutoGLM-Phone-9B-Multilingual`, BF16 |
| Model revision | `832ab5e014b965268dafd944e64cbc8e4bade892` |
| Model files | `/opt/open-autoglm-deployment/model` (~20.6 GB download) |
| Inference | SGLang 0.5.18, PyTorch 2.13.0+cu130, Transformers 5.12.1 |
| Container | `open-autoglm-api`, single A100 40GB (GPU 0) |
| Server API | `http://127.0.0.1:18085/v1`, loopback only |
| Served model name | `autoglm-phone-9b-multilingual` |

Verified on 2026-10-03: the official screenshot check returned
`do(action="Launch", app="Chrome")` (3,925 input / 84 output tokens); a Mac
screenshot request through SSH recognized the Wikipedia page and Commons link
in 5.17 seconds (1,248 input / 157 output tokens). Neither check executed any
ADB action. GPU 0 reserved about 35,520 MiB including the preallocated KV cache;
GPUs 1–7 remained unused by this service.

Using the official system prompt with the same private-display screenshot
returned `do(action="Tap", element=[285,763])` in 3.10 seconds (1,780 input /
148 output tokens). Evidence is stored locally under
`runs/autoglm-deployment/native-smoke.json`; the action was not executed.

## Connect from the Mac

In a terminal, keep the SSH tunnel running:

```bash
bash scripts/autoglm_tunnel.sh
```

The tunnel uses your existing `n30285` SSH alias and jump host. It carries the
API over SSH; no paid model-provider API key is needed. On the Mac:

```bash
curl http://127.0.0.1:18085/v1/models
./scripts/agentctl.sh capture
python3 scripts/autoglm_smoke.py --image captures/<captured-file>.png
```

The smoke test submits a screenshot and prints the model's proposed action;
**it never executes a phone action**. Screenshots are transmitted to n30285.
For native action formatting, use the official system prompt rather than the
short generic smoke prompt:

```bash
scp n30285:/opt/open-autoglm/scripts/sample_messages_en.json /tmp/autoglm-sample.json
python3 scripts/autoglm_smoke.py --image captures/<captured-file>.png --messages-file /tmp/autoglm-sample.json
```

## Manage the service

```bash
ssh n30285 'docker logs --tail 100 open-autoglm-api'
ssh n30285 'docker stop open-autoglm-api'    # releases GPU 0
ssh n30285 'docker start open-autoglm-api'
ssh n30285 'curl -f http://127.0.0.1:18085/health'
```

Docker restarts the service after a server reboot (`unless-stopped`). The
launcher is saved at `/opt/open-autoglm-deployment/autoglm_server.sh`; it refuses
to overwrite an existing container. The image is pinned by SHA256 in
`scripts/deploy/autoglm_server.sh`. Existing server containers are untouched.

This pinned Transformers version rejects the older example argument
`--mm-process-config '{"image":{"max_pixels":5000000}}'`. The launcher instead
uses the model's own `preprocessor_config.json` size settings. The first failed
container is retained, stopped, as `open-autoglm-api-failed-maxpixels` for logs;
it does not reserve a GPU and has automatic restart disabled.

Official framework multimodal check (no ADB actions):

```bash
ssh n30285 'cd /opt/open-autoglm && .venv/bin/python scripts/check_deployment_en.py --base-url http://127.0.0.1:18085/v1 --model autoglm-phone-9b-multilingual --max-tokens 512'
```

## Integration boundary

AutoGLM normally emits `do(action="Tap", element=[x,y])` / `finish(message=...)`,
not the custom JSON action schema currently required by `scripts/agent_loop.py`.
It needs a native-prompt/action adapter before replacing the mock chat bridge.
Its coordinates are normalized, so the adapter must map them to the actual
private-display dimensions and retain validation and sensitive-action pauses.

Do **not** run the stock `main.py` against the owner's emulator to claim private
display isolation: its default capture/input path targets the main screen.
Model API deployment does not by itself prove concurrent phone task execution,
keyboard/focus isolation, or third-party app compatibility.

Sources: [official framework](https://github.com/zai-org/Open-AutoGLM),
[official model](https://huggingface.co/zai-org/AutoGLM-Phone-9B-Multilingual).

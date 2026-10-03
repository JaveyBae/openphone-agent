#!/usr/bin/env bash
# Run on n30285; uses only GPU 0 and binds the API to host loopback.
set -euo pipefail
deploy_dir=/opt/open-autoglm-deployment
# Pin the already-installed image, not a moving :latest tag.
image=sha256:13ca204951b0d747df158d0048bdf89a90ec55b68674b7dd9f32b79ebe0f24e8
if docker container inspect open-autoglm-api >/dev/null 2>&1; then
  echo 'Container already exists. Use docker start/restart open-autoglm-api.' >&2
  exit 1
fi
test -f "$deploy_dir/model/model.safetensors.index.json"
docker run -d --name open-autoglm-api --restart unless-stopped \
  --gpus device=0 --shm-size 8g --network host \
  --log-opt max-size=20m --log-opt max-file=3 \
  -v "$deploy_dir/model:/model:ro" \
  -v "$deploy_dir/runtime-cache:/root/.cache" \
  -e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1 \
  --entrypoint python3 "$image" -m sglang.launch_server \
  --model-path /model --served-model-name autoglm-phone-9b-multilingual \
  --host 127.0.0.1 --port 18085 --dtype bfloat16 \
  --context-length 25480 --mem-fraction-static 0.85 \
  --max-running-requests 2

#!/usr/bin/env python3
"""Download a revision-pinned public AutoGLM snapshot into a mounted directory."""
import json
import os
from pathlib import Path

from huggingface_hub import snapshot_download

model_id = "zai-org/AutoGLM-Phone-9B-Multilingual"
root = Path(os.environ.get("AUTOGLM_DEPLOY_DIR", "/deployment"))
revision = os.environ.get("AUTOGLM_REVISION", "832ab5e014b965268dafd944e64cbc8e4bade892")
print(f"Downloading {model_id} revision {revision}", flush=True)
snapshot_download(repo_id=model_id, revision=revision, local_dir=root / "model", max_workers=4)
(root / "model-revision.json").write_text(
    json.dumps({"model_id": model_id, "revision": revision}, indent=2) + "\n"
)
print("MODEL_DOWNLOAD_COMPLETE", flush=True)

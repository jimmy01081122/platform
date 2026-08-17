#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${1:-$ROOT/results/environment.json}"
mkdir -p "$(dirname "$OUT")"
python3 - "$OUT" <<'PY'
import json, os, platform, subprocess, sys, time

def cmd(argv):
    try:
        return subprocess.run(argv, text=True, capture_output=True, timeout=20).stdout.strip()
    except Exception as exc:
        return f"unavailable: {exc}"

data = {
    "schema_version": "gpu-environment-v1",
    "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "platform": platform.platform(),
    "python": sys.version,
    "cpu": platform.processor(),
    "nvidia_smi": cmd(["nvidia-smi", "-q"]),
    "nvcc": cmd(["nvcc", "--version"]),
    "git_revision": cmd(["git", "rev-parse", "HEAD"]),
    "container_image": os.environ.get("GPU_CONTAINER_IMAGE"),
    "colab": bool(os.environ.get("COLAB_RELEASE_TAG")),
}
try:
    import torch
    data["torch"] = {"version": torch.__version__, "cuda": torch.version.cuda,
                     "cudnn": torch.backends.cudnn.version()}
except Exception as exc:
    data["torch"] = {"unavailable": str(exc)}
with open(sys.argv[1], "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
PY
echo "$OUT"

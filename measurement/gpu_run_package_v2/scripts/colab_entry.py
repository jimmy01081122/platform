#!/usr/bin/env python3
"""Order-independent Colab entry: install, preflight, smoke, run, package."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def run(argv: list[str], cwd: Path) -> None:
    subprocess.run(argv, cwd=cwd, check=True)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--experiment", default="cross-device-validation")
    p.add_argument("--skip-install", action="store_true")
    args = p.parse_args()
    root = Path(__file__).resolve().parents[1]
    if not os.environ.get("COLAB_RELEASE_TAG"):
        raise SystemExit("COLAB_RELEASE_TAG absent: this entry is only for Colab")
    if args.experiment == "rtx-pro-6000-calibration":
        raise SystemExit("Colab cannot be labeled RTX PRO 6000 hardware calibration")
    if not args.skip_install:
        run([sys.executable, "-m", "pip", "install", "-r", "requirements.lock"], root)
    run(["./preflight.sh", args.experiment], root)
    run(["./run.sh", "--smoke", "--experiment", args.experiment], root)
    run(["./run.sh", "--experiment", args.experiment], root)
    results_root = Path(os.environ.get("GPU_PERSIST_ROOT", root / "results"))
    result = results_root / args.experiment / "result.json"
    data = json.loads(result.read_text())
    data["execution_context"] = {
        "platform": "Google Colab",
        "trust": "colab-uncontrolled",
        "high_trust_hardware_calibration": False,
    }
    result.write_text(json.dumps(data, indent=2) + "\n")
    run(["./run.sh", "--package-results"], root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

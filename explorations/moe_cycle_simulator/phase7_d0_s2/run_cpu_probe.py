#!/usr/bin/env python3
"""Create a local CPU-only D0-S2 probe/replay evidence run."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from explorations.moe_cycle_simulator.phase7_d0_s2.classifier import classify_probe  # noqa: E402
from explorations.moe_cycle_simulator.phase7_d0_s2.validate_d0_s2 import (  # noqa: E402
    canonical_bytes,
    load_json,
    validate_schema,
)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def write_json(path: Path, value: Any) -> bytes:
    payload = canonical_bytes(value)
    write_bytes(path, payload + b"\n")
    return payload + b"\n"


def build_ledger(root: Path, excluded: set[str]) -> dict[str, Any]:
    members: list[dict[str, Any]] = []
    for path in sorted((item for item in root.rglob("*") if item.is_file()), key=lambda item: item.relative_to(root).as_posix().encode()):
        relative = path.relative_to(root).as_posix()
        if relative in excluded:
            continue
        payload = path.read_bytes()
        members.append({"path": relative, "size_bytes": len(payload), "sha256": sha256_bytes(payload)})
    rows = [(item["path"], item["sha256"]) for item in members]
    ledger_sha256 = sha256_bytes(canonical_bytes(rows))
    return {
        "schema_version": "moe-simulator-phase7-gputw-d0-s2-evidence-ledger-v1",
        "run_id": root.name,
        "evidence_class": "CPU_ONLY_LOCAL_PROBE_NONFORMAL_DISCOVERY_PROVENANCE",
        "member_count": len(members),
        "members": members,
        "ledger_sha256": ledger_sha256,
        "network_access": False,
        "ssh_attempted": False,
        "model_accessed": False,
        "gpu_workload_performed": False,
    }


def run(output: Path) -> int:
    if output.exists():
        raise RuntimeError(f"refusing to overwrite existing run: {output}")
    output.mkdir(parents=True)
    (output / "artifacts").mkdir()
    (output / "logs").mkdir()
    (output / "environment").mkdir()
    run_id = output.name
    command = [sys.executable, "-I", "-B", str(PACKAGE_ROOT / "probe.py")]
    command_log = json.dumps(command, ensure_ascii=False, separators=(",", ":")) + "\n"
    write_bytes(output / "logs/command.log", command_log.encode())
    started = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    completed = subprocess.run(command, cwd=REPO_ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"}, timeout=120, check=False)
    write_bytes(output / "logs/stdout.log", completed.stdout)
    write_bytes(output / "logs/stderr.log", completed.stderr)
    if completed.returncode != 0:
        write_json(output / "failure.json", {"status": "FAILED", "returncode": completed.returncode, "stderr_sha256": sha256_bytes(completed.stderr)})
        write_json(output / "manifest.json", {"run_id": run_id, "stage": "S2", "status": "FAILED", "started_at_utc": started, "command": command})
        return 1
    probe = json.loads(completed.stdout.decode("utf-8"))
    classification = classify_probe(probe)
    validate_schema(probe, load_json(PACKAGE_ROOT / "schemas/probe.schema.json"))
    validate_schema(classification, load_json(PACKAGE_ROOT / "schemas/classification.schema.json"))
    probe_bytes = write_json(output / "artifacts/probe.json", probe)
    classification_bytes = write_json(output / "artifacts/classification.json", classification)
    write_json(output / "environment/tool_versions.json", {"python": platform.python_version(), "probe_schema": probe["schema_version"], "classifier_revision": "D0-S2-v1"})
    write_bytes(output / "resolved_config.yaml", b"stage: S2\nmode: CPU_ONLY_LOCAL_PROBE\nnetwork_access: false\nssh_attempted: false\nmodel_accessed: false\ngpu_workload_performed: false\n")
    metrics = {
        "d0_status": classification["d0_status"],
        "blocking_finding_count": len(classification["blocking_findings"]),
        "observational_finding_count": len(classification["observational_findings"]),
        "probe_sha256": sha256_bytes(probe_bytes),
        "classification_sha256": sha256_bytes(classification_bytes),
        "formal_status": classification["formal_status"],
        "authority": classification["authority"],
    }
    write_json(output / "metrics.json", metrics)
    write_json(output / "manifest.json", {"run_id": run_id, "stage": "S2", "status": "COMPLETE", "evidence_class": "CPU_ONLY_LOCAL_PROBE_NONFORMAL_DISCOVERY_PROVENANCE", "started_at_utc": started, "command": command, "network_access": False, "ssh_attempted": False, "model_accessed": False, "gpu_workload_performed": False})
    ledger = build_ledger(output, {"evidence_ledger.json"})
    write_json(output / "evidence_ledger.json", ledger)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        return run(args.output.resolve())
    except (OSError, RuntimeError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        print(f"D0-S2 local run failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

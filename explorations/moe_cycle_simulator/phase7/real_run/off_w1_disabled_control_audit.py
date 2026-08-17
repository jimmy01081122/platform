#!/usr/bin/env python3
"""Validate the frozen OFF-W1 no-offload control after its GPU run."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runner-dir", type=Path, required=True)
    parser.add_argument("--contract", type=Path, required=True)
    args = parser.parse_args()
    runner = args.runner_dir
    contract = json.loads(args.contract.read_text())
    if contract.get("contract_state") != "FROZEN_BEFORE_EXECUTION":
        raise SystemExit("OFF-W1 contract is not frozen")
    status = json.loads((runner / "status.json").read_text())
    result = json.loads((runner / "result.json").read_text())
    engine = json.loads((runner / "requested_engine_args.json").read_text())
    resolved = json.loads((runner / "resolved_runtime.json").read_text())
    records = [json.loads(line) for line in (runner / "requests.jsonl").read_text().splitlines() if line]
    routing_json = sorted((runner / "routing").glob("*.json"))
    routing_npy = sorted((runner / "routing").glob("*.npy"))
    logs = (runner / "stdout.log").read_text(errors="replace") + "\n" + (runner / "stderr.log").read_text(errors="replace")
    matches = [float(value) for value in re.findall(
        r"Total CPU offloaded parameters:\s*([0-9.]+)(?:\s*(?:GiB|GB))?", logs
    )]
    reference = contract["off_w0_reference"]
    routing = result.get("routing", {})
    requested_disabled = (
        engine.get("cpu_offload_gb") == 0.0
        and engine.get("cpu_offload_params") == []
        and engine.get("offload_backend") == "auto"
    )
    resolved_args = resolved.get("constructor_args", {})
    resolved_disabled = (
        resolved_args.get("cpu_offload_gb") == 0.0
        and resolved_args.get("cpu_offload_params") == []
        and resolved_args.get("offload_backend") == "auto"
    )
    request_correct = (
        status.get("status") == "PASS" and status.get("total_completed_requests") == 1
        and len(records) == 1 and result.get("input_token_count") == 128
        and result.get("output_token_count") == 32 and result.get("finish_reason") == "length"
    )
    equivalent = (
        result.get("output_token_ids") == reference["expected_output_token_ids"]
        and routing.get("validation_status") == "PASS"
        and routing.get("shape") == reference["expected_routing_shape"]
        and len(routing_json) == 1 and len(routing_npy) == 1
        and sha256_file(routing_npy[0]) == reference["routing_array_sha256"]
    )
    no_runtime_offload = not matches or max(matches) == 0.0
    gpu = subprocess.run(
        ["nvidia-smi", "--query-gpu=uuid,name,memory.used,memory.free,utilization.gpu", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=10,
    ).stdout.strip()
    apps = subprocess.run(
        ["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=10,
    ).stdout.strip()
    passed = requested_disabled and resolved_disabled and request_correct and equivalent and no_runtime_offload and not apps
    audit = {
        "schema_version": "phase7-off-w1-disabled-control-audit-v1",
        "captured_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "contract_sha256": sha256_file(args.contract),
        "requested_disabled_gate": "PASS" if requested_disabled else "FAIL",
        "resolved_disabled_gate": "PASS" if resolved_disabled else "FAIL",
        "runtime_log_offloaded_gib_values": matches,
        "no_runtime_offload_gate": "PASS" if no_runtime_offload else "FAIL",
        "request_correctness": "PASS" if request_correct else "FAIL",
        "off_w0_output_and_routing_equivalence": "PASS" if equivalent else "FAIL",
        "routing_array_sha256": sha256_file(routing_npy[0]) if len(routing_npy) == 1 else None,
        "gpu_terminal_cleanup": {"gpu": gpu, "compute_apps": apps.splitlines() if apps else []},
        "claim_boundary": contract["claim_boundary"],
    }
    write_json(runner / "off_w1_disabled_control_audit.json", audit)
    (runner / "off_w1_contract.json").write_bytes(args.contract.read_bytes())
    write_json(runner / "off_w1_terminal.json", {
        "status": "PASS" if passed else "FAIL",
        "control_status": "DISABLED_CONTROL_EQUIVALENCE_PASS" if passed else "DISABLED_CONTROL_GATE_FAILED",
        "finished_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Bind OFF-W0 source and runtime-log evidence to a routing canary."""

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
        raise SystemExit("OFF-W0 contract is not frozen")
    expected_sources = {item["path"]: item["sha256"] for item in contract["source_contract"]}
    actual_sources = {path: sha256_file(Path(path)) for path in expected_sources}
    if actual_sources != expected_sources:
        raise SystemExit("OFF-W0 source hash mismatch")
    status = json.loads((runner / "status.json").read_text())
    result = json.loads((runner / "result.json").read_text())
    engine = json.loads((runner / "requested_engine_args.json").read_text())
    records = [json.loads(line) for line in (runner / "requests.jsonl").read_text().splitlines() if line]
    logs = (runner / "stdout.log").read_text(errors="replace") + "\n" + (runner / "stderr.log").read_text(errors="replace")
    # vLLM obtains this value via format_gib(), but the current log template
    # emits only the numeric payload; retain compatibility with unit-suffixed
    # releases while treating the bare value as GiB.
    matches = re.findall(
        r"Total CPU offloaded parameters:\s*([0-9.]+)(?:\s*(?:GiB|GB))?",
        logs,
    )
    offloaded_gib = float(matches[-1]) if matches else 0.0
    routing_json = sorted((runner / "routing").glob("*.json"))
    routing_npy = sorted((runner / "routing").glob("*.npy"))
    params = engine.get("cpu_offload_params")
    if isinstance(params, dict):
        params = params.get("values") or params.get("attributes") or params
    params_repr = json.dumps(params, sort_keys=True)
    correct = (
        status.get("status") == "PASS"
        and status.get("total_completed_requests") == 1
        and result.get("input_token_count") == 128
        and result.get("output_token_count") == 32
        and result.get("finish_reason") == "length"
        and len(records) == 1
        and len(routing_json) == 1
        and len(routing_npy) == 1
    )
    configured = (
        engine.get("offload_backend") == "uva"
        and engine.get("cpu_offload_gb") == 1.0
        and "experts" in params_repr
        and engine.get("enable_return_routed_experts") is True
    )
    available = correct and configured and offloaded_gib > 0
    gpu = subprocess.run(
        ["nvidia-smi", "--query-gpu=uuid,name,memory.used,memory.free,utilization.gpu", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=10,
    ).stdout.strip()
    apps = subprocess.run(
        ["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=10,
    ).stdout.strip()
    capability = (
        "RUNTIME_NATIVE_CPU_WEIGHT_OFFLOAD_AVAILABLE"
        if available else "RUNTIME_CPU_WEIGHT_OFFLOAD_UNAVAILABLE_WITH_CONSEQUENCE"
    )
    audit = {
        "schema_version": "phase7-off-w0-source-audit-v1",
        "captured_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "contract_sha256": sha256_file(args.contract),
        "source_hashes": actual_sources,
        "requested_engine_args": engine,
        "runtime_log_offloaded_gib": offloaded_gib,
        "runtime_log_matches": matches,
        "request_correctness": "PASS" if correct else "FAIL",
        "configuration_gate": "PASS" if configured else "FAIL",
        "routing_json": [{"path": str(path), "sha256": sha256_file(path)} for path in routing_json],
        "routing_npy": [{"path": str(path), "sha256": sha256_file(path)} for path in routing_npy],
        "capability_status": capability,
        "trigger_consequence": "OFF-W1/2/3 triggered" if available else "OFF-W1/2/3 not triggered with evidence",
        "gpu_terminal_cleanup": {"gpu": gpu, "compute_apps": apps.splitlines() if apps else []},
        "claim_boundary": contract["claim_boundary"],
    }
    write_json(runner / "off_w0_capability_audit.json", audit)
    (runner / "off_w0_contract.json").write_bytes(args.contract.read_bytes())
    write_json(runner / "off_w0_terminal.json", {
        "status": "PASS" if available else "PASS_NEGATIVE_EVIDENCE",
        "capability_status": capability,
        "runtime_log_offloaded_gib": offloaded_gib,
        "request_correctness": audit["request_correctness"],
        "configuration_gate": audit["configuration_gate"],
        "finished_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    return 0 if correct and configured else 2


if __name__ == "__main__":
    raise SystemExit(main())

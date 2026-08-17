#!/usr/bin/env python3
"""Bind OFF-E-RT0 source/API semantics to one completed routing canary."""

from __future__ import annotations

import argparse
import hashlib
import inspect
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


def gpu_state() -> dict[str, Any]:
    gpu = subprocess.run(
        ["nvidia-smi", "--query-gpu=uuid,name,memory.used,memory.free,utilization.gpu", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=10,
    )
    apps = subprocess.run(
        ["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=10,
    )
    return {"gpu": gpu.stdout.strip(), "compute_apps": [line for line in apps.stdout.splitlines() if line.strip()]}


def source_observations(path: Path) -> dict[str, Any]:
    text = path.read_text(errors="replace")
    patterns = {
        "route_condition_terms": r"route|routed|router|selected_expert|expert_id",
        "host_residency_terms": r"residen|evict|host|cpu|offload",
        "static_layer_group_terms": r"group_size|module_index|offload last|layer",
        "rl_training_terms": r"RL training|trainer",
    }
    return {
        "path": str(path), "sha256": sha256_file(path),
        "matched_line_counts": {
            key: sum(bool(re.search(pattern, line, re.I)) for line in text.splitlines())
            for key, pattern in patterns.items()
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runner-dir", type=Path, required=True)
    parser.add_argument("--contract", type=Path, required=True)
    args = parser.parse_args()
    contract = json.loads(args.contract.read_text())
    if contract.get("contract_state") != "FROZEN_BEFORE_EXECUTION":
        raise SystemExit("OFF-E-RT0 contract is not frozen")
    expected = {item["path"]: item["sha256"] for item in contract["source_contract"]}
    actual = {path: sha256_file(Path(path)) for path in expected}
    if actual != expected:
        raise SystemExit(f"OFF-E-RT0 source hash mismatch: {actual}")
    runner = args.runner_dir
    status = json.loads((runner / "status.json").read_text())
    manifest = json.loads((runner / "manifest.json").read_text())
    engine = json.loads((runner / "requested_engine_args.json").read_text())
    records = [json.loads(line) for line in (runner / "requests.jsonl").read_text().splitlines() if line]
    measured = [record for record in records if record.get("repetition_role") == "measured"]
    routing_json = sorted((runner / "routing").glob("*.json"))
    routing_npy = sorted((runner / "routing").glob("*.npy"))
    if (
        status.get("status") != "PASS"
        or manifest.get("runtime_class") != "ROUTING"
        or len(measured) != 1
        or measured[0].get("input_token_count") != 128
        or measured[0].get("output_token_count") != 32
        or measured[0].get("finish_reason") != "length"
        or len(routing_json) != 1
        or len(routing_npy) != 1
    ):
        raise SystemExit("OFF-E-RT0 routing canary gate failed")
    if (
        engine.get("cpu_offload_gb") != 0
        or engine.get("enable_expert_parallel") is not False
        or engine.get("enable_return_routed_experts") is not True
        or engine.get("enforce_eager") is not True
    ):
        raise SystemExit("OFF-E-RT0 canonical canary engine mismatch")

    from vllm.engine.arg_utils import EngineArgs
    parameters = sorted(inspect.signature(EngineArgs).parameters)
    explicit_dynamic = [
        name for name in parameters
        if "expert" in name.lower() and any(term in name.lower() for term in ("offload", "residen"))
    ]
    observations = [source_observations(Path(path)) for path in expected]
    # The installed APIs offer generic parameter filters, static layer groups,
    # RL weight-update transfer, and EPLB. None exposes a routed-expert demand
    # key tied to host/device residency and object lifecycle events.
    capability = "RUNTIME_EXPERT_OFFLOAD_UNAVAILABLE_WITH_CONSEQUENCE" if not explicit_dynamic else "CANDIDATE_REQUIRES_TRIGGER_MATRIX"
    audit = {
        "schema_version": "phase7-off-e-rt0-source-audit-v1",
        "captured_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "contract_sha256": sha256_file(args.contract),
        "source_hashes": actual,
        "source_observations": observations,
        "engine_args_signature": str(inspect.signature(EngineArgs)),
        "generic_weight_offload_parameters": [name for name in parameters if name in {
            "cpu_offload_gb", "cpu_offload_params", "offload_backend", "offload_group_size",
            "offload_num_in_group", "offload_prefetch_step", "offload_params", "weight_transfer_config"
        }],
        "explicit_dynamic_expert_residency_parameters": explicit_dynamic,
        "non_equivalent_feature_adjudication": contract["non_equivalent_features"],
        "canary": {
            "runner_dir": str(runner), "status": status,
            "measured_request": measured[0],
            "routing_json": [{"path": str(path), "sha256": sha256_file(path)} for path in routing_json],
            "routing_npy": [{"path": str(path), "sha256": sha256_file(path)} for path in routing_npy],
        },
        "capability_status": capability,
        "trigger_consequence": "OFF-E-RT1/2/3 are NOT_TRIGGERED_WITH_EVIDENCE; OFF-E-PR and shared-fabric OFFKV remain required.",
        "gpu_terminal_cleanup": gpu_state(),
        "claim_boundary": contract["claim_boundary"],
    }
    write_json(runner / "off_e_rt0_capability_audit.json", audit)
    (runner / "off_e_rt0_contract.json").write_bytes(args.contract.read_bytes())
    write_json(runner / "off_e_rt0_terminal.json", {
        "status": "PASS_NEGATIVE_EVIDENCE" if capability == "RUNTIME_EXPERT_OFFLOAD_UNAVAILABLE_WITH_CONSEQUENCE" else "SUPPLEMENT_REQUIRED",
        "capability_status": capability,
        "request_correctness": "PASS",
        "routing_evidence": "PASS",
        "finished_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

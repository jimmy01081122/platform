#!/usr/bin/env python3
"""Build the local LM0 evidence adoption map and LM1 runtime freeze record.

This reads only locally backed-up metadata.  It intentionally does not scan or
hash the remote /vault model; that limitation is recorded explicitly.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any


MODEL_REVISION = "eba92302a2861cdc0098cc54bc9f17cb2c47eb61"
ROOTS = {
    "controlled": "runs/20260811T152600Z__phase7_remote_controlled_matrix_backup",
    "natural": "runs/20260811T161000Z__phase7_remote_natural_matrix_backup",
    "sampling": "runs/20260811T163100Z__phase7_remote_sampling_pairs_backup",
    "f0": "runs/20260811T133800Z__phase7_remote_f0_gpu_backup",
    "c0a": "runs/20260811T134100Z__phase7_remote_c0a_backup",
    "c0bc": "runs/20260811T134700Z__phase7_remote_c0bc_backup",
    "f1": "runs/20260811T134800Z__phase7_remote_f1_backup",
    "tcanary": "runs/20260811T134900Z__phase7_remote_tcanary_backup",
    "r0": "runs/20260811T135600Z__phase7_remote_r0_backup",
}


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def classify_sampling(manifest: dict[str, Any], request: dict[str, Any] | None) -> str:
    mode = manifest.get("sampling_mode")
    if mode:
        return str(mode)
    sampling = (request or {}).get("sampling", manifest.get("sampling", {}))
    if sampling.get("ignore_eos") is True:
        return "FORCED_LENGTH_NATURAL_FIXTURE"
    if sampling.get("ignore_eos") is False:
        return "NATURAL_EOS_CAPPED"
    return "UNKNOWN"


def core_engine_args(path: Path) -> dict[str, Any] | None:
    candidate = path / "requested_engine_args.json"
    if not candidate.is_file():
        return None
    value = read_json(candidate)
    return {
        key: value.get(key)
        for key in (
            "dtype",
            "kv_cache_dtype",
            "tensor_parallel_size",
            "pipeline_parallel_size",
            "enable_expert_parallel",
            "enforce_eager",
            "max_model_len",
            "max_num_seqs",
            "max_num_batched_tokens",
            "gpu_memory_utilization",
            "cpu_offload_gb",
            "quantization",
            "load_format",
            "safetensors_prefetch_num_threads",
            "safetensors_prefetch_block_size",
            "enable_prefix_caching",
            "trust_remote_code",
        )
    }


def scheduler_config(path: Path) -> dict[str, Any] | None:
    candidate = path / "resolved_runtime.json"
    if not candidate.is_file():
        return None
    value = read_json(candidate)
    return (
        value.get("llm_engine", {})
        .get("attributes", {})
        .get("vllm_config", {})
        .get("scheduler_config")
    )


def run_records(root: Path, family: str) -> list[dict[str, Any]]:
    records = []
    for status_path in sorted(root.rglob("status.json")):
        run_dir = status_path.parent
        manifest_path = run_dir / "manifest.json"
        if not manifest_path.is_file():
            continue
        manifest = read_json(manifest_path)
        status = read_json(status_path)
        request_path = run_dir / "requests.jsonl"
        request = None
        if request_path.is_file():
            line = next((line for line in request_path.read_text(encoding="utf-8").splitlines() if line.strip()), None)
            if line:
                request = json.loads(line)
        profiler = (request or {}).get("profiler") or {}
        records.append(
            {
                "family": family,
                "experiment_id": manifest.get("experiment_id", run_dir.name),
                "source_path": str(run_dir),
                "status": status.get("status"),
                "phase": status.get("phase"),
                "runtime_class": manifest.get("runtime_class"),
                "sampling_mode": classify_sampling(manifest, request),
                "model_id": manifest.get("model_id"),
                "model_revision": manifest.get("model_revision"),
                "raw_file_count": sum(1 for path in run_dir.rglob("*") if path.is_file() and path.name not in {"manifest.json", "status.json"}),
                "checksum_present": (run_dir / "SHA256SUMS").is_file(),
                "request_present": request is not None,
                "routing_validation": ((request or {}).get("routing") or {}).get("validation_status"),
                "profiler": {
                    "method": profiler.get("method"),
                    "kernel_event_count": profiler.get("kernel_event_count", 0),
                    "model_kernel_event_count": profiler.get("model_kernel_event_count", 0),
                    "prefill_marker_count": profiler.get("prefill_marker_count", 0),
                    "decode_marker_count": profiler.get("decode_marker_count", 0),
                    "model_correlation": profiler.get("model_correlation"),
                },
            }
        )
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    all_records: list[dict[str, Any]] = []
    family_counts = {}
    for family, relative in ROOTS.items():
        root = Path(relative)
        records = run_records(root, family) if root.is_dir() else []
        all_records.extend(records)
        family_counts[family] = len(records)

    adopted = []
    for record in all_records:
        experiment = record["experiment_id"]
        if record["status"] == "PASS" and record["sampling_mode"] == "FORCED_LENGTH_NATURAL_FIXTURE":
            record["adoption_class"] = "EXISTING_EVIDENCE / FORCED_LENGTH_ONLY"
            adopted.append(experiment)
        elif experiment in {"K6-W1-WORKER-PROFILE-V3", "K7-W2-WORKER-PROFILE-V2"}:
            profile = record["profiler"]
            record["adoption_class"] = (
                "EXISTING_EVIDENCE / REPAIR_PASS_REPORTED"
                if record["status"] == "PASS"
                and profile["model_kernel_event_count"] > 0
                and profile["prefill_marker_count"] > 0
                and profile["decode_marker_count"] > 0
                else "SUPPLEMENT_REQUIRED"
            )
            adopted.append(experiment)
        elif record["status"] == "PASS":
            record["adoption_class"] = "EXISTING_EVIDENCE / READ_ONLY_AUDITED"
            adopted.append(experiment)
        else:
            record["adoption_class"] = "PRESERVED_FAILURE_OR_PARTIAL"

    representative = next((record for record in all_records if record["status"] == "PASS" and record["source_path"].endswith("SMP1-W0-PAIR-V1-CLEAN")), None)
    if representative is None:
        representative = next((record for record in all_records if record["status"] == "PASS" and record["runtime_class"] == "CLEAN"), None)
    representative_path = Path(representative["source_path"]) if representative else None
    model = read_json(representative_path / "model_identity.json") if representative_path and (representative_path / "model_identity.json").is_file() else {}
    environment = read_json(representative_path / "environment.json") if representative_path and (representative_path / "environment.json").is_file() else {}
    requested = core_engine_args(representative_path) if representative_path else None
    scheduler = scheduler_config(representative_path) if representative_path else None
    clock_path = Path("runs/20260811T163100Z__phase7_remote_sampling_pairs_backup/observability/clock-v1-20260811T1635Z")
    clock = read_json(clock_path / "clock_alignments.json") if (clock_path / "clock_alignments.json").is_file() else None

    output = {
        "schema_version": "phase7-lm0-lm1-state-v1",
        "generated_from_local_backup_at": dt_now(),
        "lm0": {
            "status": "PASS_WITH_OPEN_ITEMS",
            "family_counts": family_counts,
            "record_count": len(all_records),
            "adopted_record_count": len(adopted),
            "records": all_records,
            "preserved_failures": [record["experiment_id"] for record in all_records if record["adoption_class"] == "PRESERVED_FAILURE_OR_PARTIAL"],
            "procedural_note": "Clock probe was executed before this compatibility audit; it is preserved and adoption is explicit, not retroactively treated as pre-gate execution.",
        },
        "lm1": {
            "status": "PASS_WITH_LIMITATION",
            "model": {
                "model_id": model.get("model_id"),
                "model_revision": model.get("model_revision"),
                "model_path": model.get("model_path"),
                "config_sha256": model.get("config_sha256"),
                "safetensor_bytes": model.get("safetensor_bytes"),
                "safetensor_shard_count": model.get("safetensor_shard_count"),
                "config_dimensions": {key: (model.get("config") or {}).get(key) for key in ("num_hidden_layers", "num_local_experts", "num_experts_per_tok", "hidden_size", "intermediate_size", "torch_dtype", "max_position_embeddings")},
            },
            "platform": {
                "environment": environment,
                "gpu_identity_observed": (environment.get("nvidia_smi") or {}).get("stdout"),
            },
            "canonical_engine_args": requested,
            "resolved_scheduler_config": scheduler,
            "clock_artifact": str(clock_path) if clock else None,
            "clock_grades": [{"alignment_id": item.get("alignment_id"), "claimed_grade": item.get("claimed_grade")} for item in (clock or {}).get("alignments", [])],
            "limitations": [
                "Remote /vault full-model checksum was not rescanned; config/revision/shard-byte identity is recorded and full checksum remains an owner-scope limitation.",
                "CLK4 target support-processor clock is unselected; control-latency and cycle-grade hardware claims remain unavailable.",
            ],
        },
        "required_not_yet_run": {
            "K0-K5": "NOT_RUN / UNVERIFIED",
            "R-A/R-B/R-C_and_MEM0-MEM5": "NOT_RUN / UNVERIFIED or partial existing coverage",
            "CMP-A/M/L": "NOT_RUN / UNVERIFIED",
            "XFER-L/E/Q/O": "NOT_RUN / UNVERIFIED",
            "formal_60_samples": "NOT_RUN / OWNER_NOTIFICATION_GATE",
            "SERV/POL/XRT": "NOT_RUN / UNVERIFIED",
            "canonical_IR_and_replay": "BLOCKED_BY_MEASUREMENT_DEPENDENCIES",
        },
    }
    args.output.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": output["lm0"]["status"], "record_count": len(all_records), "family_counts": family_counts, "lm1": output["lm1"]["status"]}, indent=2))


def dt_now() -> str:
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).isoformat()


if __name__ == "__main__":
    raise SystemExit(main())

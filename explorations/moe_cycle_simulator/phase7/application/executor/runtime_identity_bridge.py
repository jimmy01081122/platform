#!/usr/bin/env python3
"""Convert the M0 application runtime into the canonical Phase 7 identity."""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[5]))

from explorations.moe_cycle_simulator.phase7.application.executor.common import (  # noqa: E402
    M0Error,
    canonical_bytes,
    load_json,
    write_new_json,
)


def runtime_variant_hash(value: dict) -> str:
    semantic = {key: item for key, item in value.items() if key != "variant_id"}
    return hashlib.sha256(
        b"moe-runtime-variant-v1\0" + canonical_bytes(semantic)
    ).hexdigest()


def convert(runtime: dict) -> dict:
    rt = runtime["runtime"]
    collector = runtime["collector"]
    value = {
        "schema_version": "runtime-variant-v1",
        "variant_id": "",
        "runtime": {"name": "vllm", "revision": rt["git_commit"]},
        "runtime_adapter_contract_hash": runtime["runtime_adapter_contract"][
            "file_sha256"
        ],
        "runtime_build_attestation_hash": runtime["runtime_attestation"][
            "build_attestation_file_sha256"
        ],
        "container": {
            "name": rt["container_image"],
            "revision": rt["container_digest"],
        },
        "cuda": {"name": "cuda-runtime", "revision": rt["cuda_runtime"]},
        "driver": {"name": "nvidia-driver", "revision": rt["driver"]},
        "attention_backend": rt["attention_backend"],
        "fused_moe_backend": rt["fused_moe_backend"],
        "tensor_parallel_size": rt["tensor_parallel_size"],
        "expert_parallel_size": rt["expert_parallel_size"],
        "pipeline_parallel_size": rt["pipeline_parallel_size"],
        "distributed_executor": rt["distributed_executor"],
        "execution_mode": rt["execution_mode"],
        "max_model_length": rt["max_model_length"],
        "max_batched_tokens": rt["max_batched_tokens"],
        "max_sequences": rt["max_sequences"],
        "scheduler_policy": rt["scheduler_policy"],
        "kv_cache_dtype": rt["kv_cache_dtype"],
        "nccl_environment": rt["nccl_environment"],
        "placement": {"mode": rt["placement"]},
        "offload": {
            "enabled": False,
            "cpu_offload_gb": rt["cpu_offload_gb"],
            "swap_space_gb": rt["swap_space_gb"],
        },
        "kernel_backend": rt["kernel_backend"],
        "seed": runtime["generation"]["seed"],
        "generation": runtime["generation"],
        "collector_hash": collector["phase7_local_framework_hash"],
        "adapter_hash": collector["capacity_probe_hash"],
    }
    value["variant_id"] = runtime_variant_hash(value)
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    runtime = load_json(args.runtime)
    if args.output.exists():
        raise M0Error("canonical runtime identity output already exists")
    value = convert(runtime)
    write_new_json(args.output, value)
    print(value["variant_id"])
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (M0Error, KeyError, TypeError) as exc:
        raise SystemExit(f"HARD-STOP: {exc}") from exc

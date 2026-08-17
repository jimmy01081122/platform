#!/usr/bin/env python3
"""Run exactly one fresh-process Mixtral BF16 capacity probe."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

REPO_IMPORT_ROOT: str | None = None
if __package__ in {None, ""}:
    REPO_IMPORT_ROOT = str(Path(__file__).resolve().parents[5])
    sys.path.insert(0, REPO_IMPORT_ROOT)

from explorations.moe_cycle_simulator.phase7.application.executor.common import (  # noqa: E402
    M0Error,
    canonical_bytes,
    file_sha256,
    load_capacity_fixture,
    load_json,
    process_identity,
    require_unlock,
    semantic_sha256,
    validate_contract,
    validate_probe_record,
    validate_runtime,
    verify_model_ledger,
    write_new_json,
)
from explorations.moe_cycle_simulator.phase7.application.executor.runtime_attestation import (  # noqa: E402
    attest_loaded_distribution_modules,
    validate_runtime_attestation,
)
from explorations.moe_cycle_simulator.phase7.application.executor.vllm_runtime_adapter import (  # noqa: E402
    bind_llm_constructor,
    load_adapter_contract,
    resolve_kv_cache_dtype,
)

if REPO_IMPORT_ROOT is not None:
    sys.path = [item for item in sys.path if item != REPO_IMPORT_ROOT]


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def gpu_snapshot() -> dict[str, Any]:
    completed = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=name,memory.total,memory.free,memory.used,uuid,driver_version",
            "--format=csv,noheader,nounits",
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode != 0:
        raise M0Error(f"nvidia-smi failed: {completed.stderr.strip()}")
    rows = [row for row in completed.stdout.splitlines() if row.strip()]
    if len(rows) != 1:
        raise M0Error("exactly one visible GPU is required")
    fields = [item.strip() for item in rows[0].split(",")]
    if len(fields) != 6:
        raise M0Error("unexpected nvidia-smi field count")
    try:
        total, free, used = (int(fields[index]) * 1024 * 1024 for index in (1, 2, 3))
    except ValueError as exc:
        raise M0Error("invalid nvidia-smi memory value") from exc
    return {
        "count": 1,
        "name": fields[0],
        "total_memory_bytes": total,
        "free_memory_bytes": free,
        "used_memory_bytes": used,
        "uuid": fields[4],
        "driver_version": fields[5],
    }


def parse_utilization(value: str) -> float:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise M0Error("invalid frozen gpu_memory_utilization") from exc
    if parsed <= 0 or parsed > 1:
        raise M0Error("gpu_memory_utilization must be in (0, 1]")
    return float(parsed)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--model-ledger", type=Path, required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--launch-index", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    contract = load_json(args.contract)
    runtime = load_json(args.runtime)
    model_ledger = load_json(args.model_ledger)
    validate_contract(contract)
    require_unlock(contract)
    validate_runtime(runtime, contract)
    adapter_contract = load_adapter_contract(runtime)
    build_attestation = validate_runtime_attestation(runtime)
    installed_manifest = load_json(
        Path(build_attestation["installed_distribution"]["manifest_path"])
    )
    if runtime["model"]["model_file_ledger_sha256"] != model_ledger.get(
        "ledger_sha256"
    ):
        raise M0Error("runtime does not bind the supplied model ledger")
    if args.launch_index not in range(1, contract["probe"]["repetitions"] + 1):
        raise M0Error("launch index is outside the frozen repetition set")
    snapshot = Path(runtime["model"]["local_snapshot_path"]).resolve(strict=True)
    verify_model_ledger(snapshot, model_ledger, contract=contract)
    prompt_fixture, prompt_ids = load_capacity_fixture(
        runtime, contract, model_ledger
    )
    if args.output.exists():
        raise M0Error("fresh probe output already exists")

    contract_hash = file_sha256(args.contract)
    runtime_hash = file_sha256(args.runtime)
    process_nonce = uuid.uuid4().hex
    identity = process_identity(process_nonce)
    before = gpu_snapshot()
    if (
        before["name"] != contract["target"]["exact_product_name"]
        or before["total_memory_bytes"] < contract["target"]["minimum_memory_bytes"]
    ):
        raise M0Error("observed GPU does not satisfy the exact M0 target")
    started_at = utc_now()
    monotonic_start = time.monotonic_ns()

    actual_vllm = importlib.metadata.version("vllm")
    if actual_vllm != runtime["runtime"]["version"]:
        raise M0Error(
            f"vLLM version mismatch: {actual_vllm} != {runtime['runtime']['version']}"
        )
    actual_torch = importlib.metadata.version("torch")
    actual_transformers = importlib.metadata.version("transformers")
    import vllm
    from vllm import LLM, SamplingParams

    engine_arguments = bind_llm_constructor(LLM, {
        "model": str(snapshot),
        "tokenizer": str(snapshot),
        "tokenizer_mode": "auto",
        "skip_tokenizer_init": True,
        "trust_remote_code": False,
        "tensor_parallel_size": 1,
        "pipeline_parallel_size": 1,
        "dtype": "bfloat16",
        "quantization": None,
        "seed": 0,
        "gpu_memory_utilization": parse_utilization(
            runtime["runtime"]["gpu_memory_utilization"]
        ),
        "cpu_offload_gb": 0,
        "swap_space": 0,
        "enforce_eager": True,
        "max_model_len": 32768,
        "max_num_batched_tokens": 32768,
        "max_num_seqs": 1,
        "generation_config": "vllm",
    }, adapter_contract)
    llm = LLM(**engine_arguments)
    resolved_kv_cache_dtype = resolve_kv_cache_dtype(llm, adapter_contract)
    after_load = gpu_snapshot()
    sampling = SamplingParams(
        n=1,
        temperature=0.0,
        top_p=1.0,
        seed=0,
        max_tokens=contract["probe"]["output_tokens"],
        min_tokens=contract["probe"]["output_tokens"],
        ignore_eos=True,
        detokenize=False,
    )
    outputs = llm.generate(
        [{"prompt_token_ids": prompt_ids}], sampling, use_tqdm=False
    )
    after_generation = gpu_snapshot()
    if len(outputs) != 1 or len(outputs[0].outputs) != 1:
        raise M0Error("vLLM returned an unexpected request/output multiplicity")
    request = outputs[0]
    completion = request.outputs[0]
    observed_prompt = [int(item) for item in request.prompt_token_ids]
    output_ids = [int(item) for item in completion.token_ids]
    if observed_prompt != prompt_ids:
        raise M0Error("vLLM observed prompt IDs differ from the exact fixture")
    module_binding = adapter_contract["loaded_module_binding"]
    vllm_import_evidence = attest_loaded_distribution_modules(
        installed_manifest,
        distribution_name=module_binding["distribution_name"],
        module_prefix=module_binding["module_prefix"],
    )

    finished_at = utc_now()
    engine_argument_evidence = dict(engine_arguments)
    engine_argument_evidence["gpu_memory_utilization"] = runtime["runtime"][
        "gpu_memory_utilization"
    ]
    record = {
        "schema_version": "moe-simulator-phase7-m0-probe-record-v1",
        "status": "COMPLETE",
        "session_id": args.session_id,
        "launch_index": args.launch_index,
        "contract_sha256": contract_hash,
        "runtime_variant_sha256": runtime_hash,
        "model_ledger_sha256": model_ledger["ledger_sha256"],
        "process_identity": identity,
        "started_at_utc": started_at,
        "finished_at_utc": finished_at,
        "elapsed_monotonic_ns": time.monotonic_ns() - monotonic_start,
        "software": {
            "vllm_version": actual_vllm,
            "vllm_init_sha256": file_sha256(Path(vllm.__file__).resolve(strict=True)),
            "vllm_import_evidence": vllm_import_evidence,
            "vllm_source_git_commit": runtime["runtime"]["git_commit"],
            "installed_distribution_ledger_sha256": build_attestation[
                "installed_distribution"
            ]["ledger_sha256"],
            "build_attestation_file_sha256": runtime["runtime_attestation"][
                "build_attestation_file_sha256"
            ],
            "runtime_adapter_contract_sha256": runtime[
                "runtime_adapter_contract"
            ]["file_sha256"],
            "torch_version": actual_torch,
            "transformers_version": actual_transformers,
        },
        "runtime_qualified_version": runtime["runtime"]["version"],
        "runtime_qualified_git_commit": runtime["runtime"]["git_commit"],
        "engine": {
            "constructor_arguments": engine_argument_evidence,
            "constructor_arguments_sha256": semantic_sha256(
                engine_argument_evidence
            ),
            "resolved_kv_cache_dtype": resolved_kv_cache_dtype,
            "attention_backend": runtime["runtime"]["attention_backend"],
            "fused_moe_backend": runtime["runtime"]["fused_moe_backend"],
            "kernel_backend": runtime["runtime"]["kernel_backend"],
            "resolved_backend_evidence": "AUDITED_BY_PARENT_LOG_CONTRACT",
        },
        "gpu": {
            key: before[key]
            for key in (
                "count",
                "name",
                "total_memory_bytes",
                "uuid",
                "driver_version",
            )
        },
        "memory": {
            "before_load": {
                "free_memory_bytes": before["free_memory_bytes"],
                "used_memory_bytes": before["used_memory_bytes"],
            },
            "after_load": {
                "free_memory_bytes": after_load["free_memory_bytes"],
                "used_memory_bytes": after_load["used_memory_bytes"],
            },
            "after_generation": {
                "free_memory_bytes": after_generation["free_memory_bytes"],
                "used_memory_bytes": after_generation["used_memory_bytes"],
            },
        },
        "probe": {
            "input_token_count": len(observed_prompt),
            "prompt_token_ids_sha256": semantic_sha256(observed_prompt),
            "capacity_prompt_fixture_sha256": file_sha256(
                Path(runtime["model"]["capacity_prompt_fixture_path"])
            ),
            "output_token_count": len(output_ids),
            "output_token_ids": output_ids,
            "output_token_ids_sha256": semantic_sha256(output_ids),
            "finish_reason": completion.finish_reason,
            "stop_reason": completion.stop_reason,
            "finished": bool(request.finished),
        },
        "authority": {
            "gpu_workload": True,
            "promotion_stage": "M0",
            "m1_through_m4": False,
            "formal_profiling": False,
        },
    }
    validate_probe_record(
        record,
        contract_sha256=contract_hash,
        runtime_sha256=runtime_hash,
        model_ledger_sha256=model_ledger["ledger_sha256"],
        launch_index=args.launch_index,
        session_id=args.session_id,
        contract=contract,
    )
    write_new_json(args.output, record)
    print(semantic_sha256(record))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except M0Error as exc:
        raise SystemExit(f"HARD-STOP: {exc}") from exc

#!/usr/bin/env python3
"""Independently validate all three M0 launches and emit the only M0 verdict."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[5]))

from explorations.moe_cycle_simulator.phase7.application.executor.common import (  # noqa: E402
    M0Error,
    file_sha256,
    load_json,
    require_unlock,
    semantic_sha256,
    validate_contract,
    validate_probe_record,
    validate_runtime,
    validate_session_id,
    verify_model_ledger,
    write_new_json,
)


def validate_summary(
    summary: dict[str, Any],
    *,
    qualification_root: Path,
    contract: dict[str, Any],
    contract_hash: str,
    runtime_hash: str,
    model_ledger: dict[str, Any],
    session_id: str,
    runtime: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if (
        summary.get("schema_version")
        != "moe-simulator-phase7-m0-qualification-summary-v1"
        or summary.get("status") != "COMPLETE"
        or summary.get("session_id") != session_id
        or summary.get("contract_sha256") != contract_hash
        or summary.get("runtime_variant_sha256") != runtime_hash
        or summary.get("model_ledger_sha256") != model_ledger["ledger_sha256"]
        or summary.get("launch_count") != contract["probe"]["repetitions"]
        or summary.get("fresh_process_identity_count")
        != contract["probe"]["repetitions"]
        or summary.get("retry_used") is not False
        or summary.get("resume_used") is not False
    ):
        raise M0Error("qualification summary binding or state mismatch")
    launches = summary.get("launches")
    if not isinstance(launches, list) or len(launches) != 3:
        raise M0Error("qualification summary must contain exactly three launches")
    records = []
    for expected_index, launch in enumerate(launches, start=1):
        if (
            launch.get("launch_index") != expected_index
            or launch.get("returncode") != 0
            or launch.get("timed_out") is not False
            or launch.get("cleanup", {}).get("status") != "PASS"
            or launch.get("cleanup", {}).get("residual_processes") != []
            or launch.get("environment_sha256")
            != summary.get("environment_sha256")
            or launch.get("process_tree_cleanup", {}).get("status") != "CLEAN"
        ):
            raise M0Error(f"launch {expected_index} did not complete and clean up")
        if runtime is not None:
            backend_evidence = launch.get("backend_evidence")
            if (
                not isinstance(backend_evidence, dict)
                or backend_evidence.get("source")
                != runtime["backend_evidence_contract"]["source"]
                or backend_evidence.get("required_utf8_markers")
                != runtime["backend_evidence_contract"]["required_utf8_markers"]
                or backend_evidence.get("all_markers_observed") is not True
            ):
                raise M0Error(
                    f"launch {expected_index} backend evidence contract mismatch"
                )
        relative = Path(launch.get("probe_path", ""))
        if relative.is_absolute() or ".." in relative.parts:
            raise M0Error("probe path escapes qualification evidence root")
        probe_path = (qualification_root / relative).resolve(strict=True)
        if qualification_root.resolve(strict=True) not in probe_path.parents:
            raise M0Error("probe path containment failure")
        stdout_path = probe_path.with_name("stdout.log")
        stderr_path = probe_path.with_name("stderr.log")
        if (
            file_sha256(stdout_path) != launch.get("stdout_sha256")
            or file_sha256(stderr_path) != launch.get("stderr_sha256")
        ):
            raise M0Error(f"launch {expected_index} log checksum mismatch")
        record = load_json(probe_path)
        validate_probe_record(
            record,
            contract_sha256=contract_hash,
            runtime_sha256=runtime_hash,
            model_ledger_sha256=model_ledger["ledger_sha256"],
            launch_index=expected_index,
            session_id=session_id,
            contract=contract,
        )
        if runtime is not None:
            adapter_contract = load_json(
                Path(runtime["runtime_adapter_contract"]["path"])
            )
            if (
                record.get("runtime_qualified_version")
                != runtime["runtime"]["version"]
                or record.get("runtime_qualified_git_commit")
                != runtime["runtime"]["git_commit"]
                or record["software"].get("vllm_source_git_commit")
                != runtime["runtime"]["git_commit"]
                or record["software"].get("build_attestation_file_sha256")
                != runtime["runtime_attestation"][
                    "build_attestation_file_sha256"
                ]
                or record["software"].get("runtime_adapter_contract_sha256")
                != runtime["runtime_adapter_contract"]["file_sha256"]
                or record["engine"]["resolved_kv_cache_dtype"].get(
                    "attribute_path"
                )
                != adapter_contract["resolved_kv_cache_evidence"][
                    "attribute_path"
                ]
            ):
                raise M0Error(
                    f"launch {expected_index} runtime attestation mismatch"
                )
        if record["probe"].get("finished") is not True:
            raise M0Error(f"launch {expected_index} request is not complete")
        records.append(record)
    identities = {
        (
            record["process_identity"]["boot_id"],
            record["process_identity"]["start_ticks"],
            record["process_identity"]["nonce"],
        )
        for record in records
    }
    if len(identities) != 3:
        raise M0Error("three unique fresh-process identities were not proven")
    prompt_hashes = {
        record["probe"]["prompt_token_ids_sha256"] for record in records
    }
    fixture_hashes = {
        record["probe"]["capacity_prompt_fixture_sha256"] for record in records
    }
    output_hashes = {
        record["probe"]["output_token_ids_sha256"] for record in records
    }
    engine_hashes = {
        record["engine"]["constructor_arguments_sha256"] for record in records
    }
    software_rows = {
        (
            record["software"]["vllm_version"],
            record["software"]["vllm_init_sha256"],
            record["software"]["vllm_import_evidence"]["evidence_sha256"],
            record["software"]["torch_version"],
            record["software"]["transformers_version"],
        )
        for record in records
    }
    gpu_rows = {
        (
            record["gpu"]["name"],
            record["gpu"]["uuid"],
            record["gpu"]["driver_version"],
            record["gpu"]["total_memory_bytes"],
        )
        for record in records
    }
    if any(
        len(values) != 1
        for values in (
            prompt_hashes,
            fixture_hashes,
            output_hashes,
            engine_hashes,
            software_rows,
            gpu_rows,
        )
    ):
        raise M0Error("fresh launches do not share exact prompt/output/runtime/GPU identity")
    return records


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--model-ledger", type=Path, required=True)
    parser.add_argument("--qualification", type=Path, required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    contract = load_json(args.contract)
    runtime = load_json(args.runtime)
    model_ledger = load_json(args.model_ledger)
    validate_contract(contract)
    require_unlock(contract)
    validate_runtime(runtime, contract)
    if runtime["model"]["model_file_ledger_sha256"] != model_ledger.get(
        "ledger_sha256"
    ):
        raise M0Error("runtime does not bind the supplied model ledger")
    validate_session_id(args.session_id)
    snapshot = Path(runtime["model"]["local_snapshot_path"]).resolve(strict=True)
    verify_model_ledger(snapshot, model_ledger, contract=contract)
    qualification = args.qualification.resolve(strict=True)
    summary_path = qualification / "qualification_summary.json"
    if not summary_path.is_file():
        raise M0Error("qualification summary is absent")
    contract_hash = file_sha256(args.contract)
    runtime_hash = file_sha256(args.runtime)
    summary = load_json(summary_path)
    records = validate_summary(
        summary,
        qualification_root=qualification,
        contract=contract,
        contract_hash=contract_hash,
        runtime_hash=runtime_hash,
        model_ledger=model_ledger,
        session_id=args.session_id,
        runtime=runtime,
    )
    if (
        records[0]["probe"]["capacity_prompt_fixture_sha256"]
        != runtime["model"]["capacity_prompt_fixture_sha256"]
    ):
        raise M0Error("audited prompt fixture is not the runtime-bound fixture")
    result = {
        "schema_version": "moe-simulator-phase7-m0-result-v1",
        "session_id": args.session_id,
        "verdict": "PASS",
        "failure_class": None,
        "findings": [],
        "contract_sha256": contract_hash,
        "runtime_variant_sha256": runtime_hash,
        "model_ledger_sha256": model_ledger["ledger_sha256"],
        "qualification_summary_sha256": file_sha256(summary_path),
        "completed_units": 3,
        "failed_units": 0,
        "pending_units": 0,
        "fresh_processes": 3,
        "input_tokens_per_unit": contract["probe"]["input_tokens"],
        "output_tokens_per_unit": contract["probe"]["output_tokens"],
        "prompt_token_ids_sha256": records[0]["probe"]["prompt_token_ids_sha256"],
        "output_token_ids_sha256": records[0]["probe"]["output_token_ids_sha256"],
        "gpu": records[0]["gpu"],
        "next_legal_action": "SEPARATE_M1_APPLICATION_REQUIRED",
        "m1_through_m4_authorized": False,
    }
    write_new_json(args.output, result)
    print(semantic_sha256(result))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except M0Error as exc:
        raise SystemExit(f"HARD-STOP: {exc}") from exc

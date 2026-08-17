#!/usr/bin/env python3
"""Materialize one pinned model snapshot and seal its complete file ledger."""

from __future__ import annotations

import argparse
import importlib.metadata
import os
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[5]))

from explorations.moe_cycle_simulator.phase7.application.executor.common import (  # noqa: E402
    M0Error,
    build_model_ledger,
    load_json,
    require_materialization_unlock,
    validate_contract,
    validate_fresh_target,
    validate_materialization_plan,
    write_new_json,
)


def make_read_only(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_symlink():
            raise M0Error(f"model snapshot symlink is forbidden: {path}")
        if path.is_file():
            path.chmod(0o444)
        elif path.is_dir():
            path.chmod(0o555)
    root.chmod(0o555)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args()
    contract = load_json(args.contract)
    plan = load_json(args.plan)
    validate_contract(contract)
    require_materialization_unlock(contract)
    validate_materialization_plan(plan, contract)
    snapshot = validate_fresh_target(
        Path(plan["paths"]["snapshot"]), "model snapshot"
    )
    ledger_path = validate_fresh_target(
        Path(plan["paths"]["model_ledger"]), "model ledger"
    )
    result_path = validate_fresh_target(
        Path(plan["paths"]["materialization_result"]), "materialization result"
    )

    expected_version = plan["materializer"]["version"]
    actual_version = importlib.metadata.version("huggingface_hub")
    if actual_version != expected_version:
        raise M0Error(
            f"huggingface_hub version mismatch: {actual_version} != {expected_version}"
        )
    from huggingface_hub import snapshot_download

    snapshot.mkdir(mode=0o700, exist_ok=False)
    returned = Path(
        snapshot_download(
            repo_id=contract["model"]["model_id"],
            revision=contract["model"]["repository_commit"],
            local_dir=str(snapshot),
            force_download=False,
            local_files_only=False,
            max_workers=plan["materializer"]["max_workers"],
            allow_patterns=plan["materializer"]["allow_patterns"],
            ignore_patterns=plan["materializer"]["ignore_patterns"],
        )
    ).resolve(strict=True)
    if returned != snapshot.resolve(strict=True):
        raise M0Error("materializer returned an unexpected snapshot path")
    ledger = build_model_ledger(
        returned,
        model_id=contract["model"]["model_id"],
        repository_commit=contract["model"]["repository_commit"],
    )
    make_read_only(returned)
    write_new_json(ledger_path, ledger)
    write_new_json(
        result_path,
        {
            "schema_version": "moe-simulator-phase7-model-materialization-result-v1",
            "status": "COMPLETE",
            "model_id": contract["model"]["model_id"],
            "repository_commit": contract["model"]["repository_commit"],
            "snapshot_path": str(returned),
            "model_ledger_path": str(ledger_path.resolve()),
            "model_ledger_sha256": ledger["ledger_sha256"],
            "materializer": {
                "library": "huggingface_hub",
                "version": actual_version,
                "method": plan["materializer"]["method"],
            },
            "snapshot_structure": ledger["snapshot_structure"],
            "gpu_workload_performed": False,
            "next_legal_action": "HARD_STOP_FREEZE_RUNTIME_AND_REQUEST_EXACT_M0_APPROVAL",
        },
    )
    print(ledger["ledger_sha256"])
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except M0Error as exc:
        raise SystemExit(f"HARD-STOP: {exc}") from exc

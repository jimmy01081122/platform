#!/usr/bin/env python3
"""Fail-closed source and sealed-run validation for CPU-only Phase 5."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
from pathlib import Path

PHASE5_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PHASE5_ROOT.parents[2]
PHASE4_LEDGER = (
    REPO_ROOT
    / "explorations/moe_cycle_simulator/phase4/governance/checksums.sha256"
)
PHASE4_AGGREGATE = (
    PHASE4_LEDGER.parent / "reviews/phase4_r4_aggregate.json"
)
EXPECTED_PHASE4_LEDGER = (
    "2109278b1b2ad66803cb59b60e29015a958b95db64b26f82f4ca4ef56fe9675b"
)
SOURCE_MEMBERS = [
    "CMakeLists.txt",
    "contracts/build_authority.json",
    "contracts/checkpoint_schema.json",
    "contracts/routing_residency_policy.json",
    "include/moe_sim/routing_residency_policy.hpp",
    "run_phase5_suite.py",
    "src/routing_residency_policy.cpp",
    "tests/routing_residency_policy_tests.cpp",
    "validate_phase5.py",
]
AUTHORITY_ARTIFACTS = {
    "kPhase5BuildAuthoritySha256": "contracts/build_authority.json",
    "kPhase5PolicyContractSha256": "contracts/routing_residency_policy.json",
    "kPhase5CheckpointSchemaSha256": "contracts/checkpoint_schema.json",
}


class ValidationError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_root() -> str:
    digest = hashlib.sha256()
    for relative in SOURCE_MEMBERS:
        path = PHASE5_ROOT / relative
        if not path.is_file() or path.is_symlink():
            raise ValidationError(f"invalid source member: {relative}")
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(bytes.fromhex(sha256(path)))
    return digest.hexdigest()


def validate_source() -> dict[str, object]:
    if sha256(PHASE4_LEDGER) != EXPECTED_PHASE4_LEDGER:
        raise ValidationError("frozen Phase 4 ledger mismatch")
    aggregate = json.loads(PHASE4_AGGREGATE.read_text(encoding="utf-8"))
    if (
        aggregate["ledger_sha256"] != EXPECTED_PHASE4_LEDGER
        or aggregate["verdict"] != "GO"
        or aggregate["blockers"]
        or not aggregate["phase4_freeze"]
        or aggregate["phase5_authorized_scope"]
        != "CPU_SYNTHETIC_ROUTING_RESIDENCY_POLICY_ONLY"
        or aggregate["gpu_authority"] != "NONE"
    ):
        raise ValidationError("Phase 4 promotion authority mismatch")
    contract = json.loads(
        (PHASE5_ROOT / "contracts/routing_residency_policy.json").read_text(
            encoding="utf-8"
        )
    )
    header = (
        PHASE5_ROOT / "include/moe_sim/routing_residency_policy.hpp"
    ).read_text(encoding="utf-8")
    for constant, relative in AUTHORITY_ARTIFACTS.items():
        expected = sha256(PHASE5_ROOT / relative)
        declaration = f'{constant} =\n    "{expected}"'
        if declaration not in header:
            raise ValidationError(
                f"authority artifact is not exact-bound: {relative}"
    )
    scheduler = contract["scheduler"]
    routing = contract["routing_input"]
    capacity = contract["capacity"]
    checkpoint = json.loads(
        (PHASE5_ROOT / "contracts/checkpoint_schema.json").read_text(
            encoding="utf-8"
        )
    )
    if (
        contract["execution_mode"] != "TRACE_COMPILED_NON_ADAPTIVE"
        or contract["execution_scope"] != "CPU_SYNTHETIC_ONLY"
        or contract["calibration_pass"]
        or contract["gpu_authority"] != "NONE"
        or scheduler["authority"] != "Phase4 SingleGpuModel"
        or scheduler["run_count"] != 1
        or scheduler["runtime_callbacks"]
        or scheduler["dynamic_admission"]
        or scheduler["compiler_predicts_runtime_timing_or_lanes"]
        or routing["granularity"] != "TOKEN_ONLY"
        or routing["aggregate_routing_accepted"]
        or routing["routing_weights_modeled"]
        or routing["selected_expert_ids_and_top_k"]
        != "HASH_BOUND_FROZEN_TOKEN_INPUT"
        or capacity["unit"] != "bytes"
        or contract["eviction"]["state"] != "CLEAN_IMMUTABLE"
        or contract["eviction"]["writeback"]
        or contract["prefetch_hint"]["future_oracle"]
        or contract["postprocess_repair"]
        or contract["fidelity"] != "FUNCTIONAL_ONLY"
        or contract["range_status"] != "RANGE_UNKNOWN"
        or checkpoint["phase4_checkpoint"]
        != "live exact-prefix resumable checkpoint"
        or checkpoint["terminal_checkpoint_with_earlier_cursor"]
        != "forbidden"
    ):
        raise ValidationError("Phase 5 contract boundary mismatch")
    forbidden: list[str] = []
    for root, directories, files in os.walk(PHASE5_ROOT, followlinks=False):
        base = Path(root)
        for name in [*directories, *files]:
            path = base / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not (
                stat.S_ISDIR(mode) or stat.S_ISREG(mode)
            ):
                forbidden.append(str(path.relative_to(PHASE5_ROOT)))
            if name == "__pycache__" or name.endswith((".pyc", ".pyo")):
                forbidden.append(str(path.relative_to(PHASE5_ROOT)))
    if forbidden:
        raise ValidationError(f"forbidden source entries: {sorted(forbidden)}")
    return {
        "status": "PASS",
        "phase4_ledger_sha256": EXPECTED_PHASE4_LEDGER,
        "source_member_count": len(SOURCE_MEMBERS),
        "source_root_sha256": source_root(),
        "gpu_used": False,
        "calibration_pass": False,
    }


def validate_run(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_dir():
        raise ValidationError("invalid run directory")
    ledger: dict[str, str] = {}
    for line in (path / "checksums.sha256").read_text().splitlines():
        digest, relative = line.split("  ", 1)
        if relative in ledger:
            raise ValidationError("duplicate run ledger member")
        ledger[relative] = digest
    actual: dict[str, str] = {}
    for root, directories, files in os.walk(path, followlinks=False):
        base = Path(root)
        for name in [*directories, *files]:
            entry = base / name
            mode = entry.lstat().st_mode
            if stat.S_ISLNK(mode) or not (
                stat.S_ISDIR(mode) or stat.S_ISREG(mode)
            ):
                raise ValidationError("forbidden run entry")
        for name in files:
            entry = base / name
            if name != "checksums.sha256":
                actual[entry.relative_to(path).as_posix()] = sha256(entry)
    if actual != ledger:
        raise ValidationError("run ledger exact-set mismatch")
    metrics = json.loads((path / "metrics.json").read_text(encoding="utf-8"))
    if (
        metrics["status"] != "PASS"
        or metrics["source_root_sha256"] != source_root()
        or metrics["gpu_used"]
        or metrics["ctest_passed"] != 4
        or not metrics["asan_ubsan_pass"]
        or metrics["calibration_pass"]
    ):
        raise ValidationError("run result boundary mismatch")
    return metrics


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path)
    args = parser.parse_args()
    result = validate_source()
    if args.run_dir:
        result = validate_run(args.run_dir.resolve())
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

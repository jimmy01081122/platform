#!/usr/bin/env python3
"""Fail-closed source and sealed-run validation for Phase 4."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
from pathlib import Path

PHASE4_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PHASE4_ROOT.parents[2]
PHASE3_LEDGER = REPO_ROOT / "explorations/moe_cycle_simulator/phase3/governance/checksums.sha256"
PHASE3_AGGREGATE = PHASE3_LEDGER.parent / "reviews/phase3_r5_aggregate.json"
EXPECTED_PHASE3_LEDGER = "c4b9209d95bbf91c607d65a70062e3bbb03a5892807ce08d8a4a370000535e42"
SOURCE_MEMBERS = [
    "CMakeLists.txt",
    "contracts/build_authority.json",
    "contracts/checkpoint_schema.json",
    "contracts/single_gpu_model.json",
    "include/moe_sim/single_gpu_model.hpp",
    "run_phase4_suite.py",
    "src/single_gpu_model.cpp",
    "tests/single_gpu_model_tests.cpp",
    "validate_phase4.py",
]
AUTHORITY_ARTIFACTS = {
    "kPhase4BuildAuthoritySha256": "contracts/build_authority.json",
    "kPhase4ModelContractSha256": "contracts/single_gpu_model.json",
    "kPhase4CheckpointSchemaSha256": "contracts/checkpoint_schema.json",
}


class ValidationError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_root() -> str:
    digest = hashlib.sha256()
    for relative in SOURCE_MEMBERS:
        path = PHASE4_ROOT / relative
        if not path.is_file() or path.is_symlink():
            raise ValidationError(f"invalid source member: {relative}")
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(bytes.fromhex(sha256(path)))
    return digest.hexdigest()


def validate_source() -> dict[str, object]:
    if sha256(PHASE3_LEDGER) != EXPECTED_PHASE3_LEDGER:
        raise ValidationError("frozen Phase 3 ledger mismatch")
    aggregate = json.loads(PHASE3_AGGREGATE.read_text(encoding="utf-8"))
    if (
        aggregate["ledger_sha256"] != EXPECTED_PHASE3_LEDGER
        or aggregate["verdict"] != "GO"
        or aggregate["blockers"]
        or aggregate["gpu_authority"] != "NONE"
    ):
        raise ValidationError("Phase 3 promotion authority mismatch")
    contract = json.loads(
        (PHASE4_ROOT / "contracts/single_gpu_model.json").read_text()
    )
    header = (
        PHASE4_ROOT / "include/moe_sim/single_gpu_model.hpp"
    ).read_text(encoding="utf-8")
    for constant, relative in AUTHORITY_ARTIFACTS.items():
        expected = sha256(PHASE4_ROOT / relative)
        declaration = f'{constant} =\n    "{expected}"'
        if declaration not in header:
            raise ValidationError(
                f"authority artifact is not exact-bound: {relative}"
            )
    replay = contract["replay"]
    envelope = contract["operational_envelope"]
    if (
        contract["phase3_ledger_sha256"] != EXPECTED_PHASE3_LEDGER
        or contract["execution_scope"] != "CPU_SYNTHETIC_ONLY"
        or contract["formal_calibration_pass"]
        or contract["range_status_required"] != "RANGE_UNKNOWN"
        or replay["checkpoint_schema"] != "phase4-checkpoint-v1"
        or "complete canonical body" not in replay["checkpoint_wire"]
        or len(replay["required_authority_hashes"]) != 4
        or envelope["full_event_vector_scan_per_step"]
        or envelope["production_scale_claim"]
    ):
        raise ValidationError("Phase 4 contract boundary mismatch")
    forbidden: list[str] = []
    for root, directories, files in os.walk(PHASE4_ROOT, followlinks=False):
        base = Path(root)
        for name in [*directories, *files]:
            path = base / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not (
                stat.S_ISDIR(mode) or stat.S_ISREG(mode)
            ):
                forbidden.append(str(path.relative_to(PHASE4_ROOT)))
            if name == "__pycache__" or name.endswith((".pyc", ".pyo")):
                forbidden.append(str(path.relative_to(PHASE4_ROOT)))
    if forbidden:
        raise ValidationError(f"forbidden source entries: {sorted(forbidden)}")
    return {
        "status": "PASS",
        "phase3_ledger_sha256": EXPECTED_PHASE3_LEDGER,
        "source_member_count": len(SOURCE_MEMBERS),
        "source_root_sha256": source_root(),
        "gpu_used": False,
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
    metrics = json.loads((path / "metrics.json").read_text())
    if (
        metrics["status"] != "PASS"
        or metrics["source_root_sha256"] != source_root()
        or metrics["gpu_used"]
        or metrics["ctest_passed"] != 3
        or not metrics["asan_ubsan_pass"]
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

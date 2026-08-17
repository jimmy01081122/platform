#!/usr/bin/env python3
"""Fail-closed source and sealed-run validation for CPU-only Phase 6."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
from pathlib import Path

PHASE6_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PHASE6_ROOT.parents[2]
PHASE5_ROOT = REPO_ROOT / "explorations/moe_cycle_simulator/phase5"
PHASE5_LEDGER = PHASE5_ROOT / "governance/checksums.sha256"
PHASE5_REVIEW_LEDGER = (
    PHASE5_ROOT / "governance/reviews/phase5_r2_checksums.sha256"
)
PHASE5_AGGREGATE = (
    PHASE5_ROOT / "governance/reviews/phase5_r2_aggregate.json"
)
EXPECTED_PHASE5_LEDGER = (
    "b6b593a1fe79f1ddb60706c5c4af17470608c08589cd654eb7ea006d71ded834"
)
EXPECTED_PHASE5_REVIEW_LEDGER = (
    "fb08d88c0f7ea381492a067ea10ee8286e24e63ca205e706bf0805967f10d93e"
)
EXPECTED_PHASE5_AGGREGATE = (
    "c4e3140a301721f13d1b33c111ca9dd09388247b06db84e1cdf11433ca703370"
)
SOURCE_MEMBERS = [
    "CMakeLists.txt",
    "contracts/build_authority.json",
    "contracts/cdc_protocol.json",
    "contracts/checkpoint_schema.json",
    "contracts/claim_boundary.json",
    "contracts/coherent_uma.json",
    "contracts/discrete_p2p.json",
    "contracts/topology_operation.json",
    "include/moe_sim/multi_domain_scheduler.hpp",
    "run_phase6_suite.py",
    "src/multi_domain_scheduler.cpp",
    "tests/multi_domain_scheduler_tests.cpp",
    "validate_phase6.py",
]
AUTHORITY_ARTIFACTS = {
    "kPhase6BuildAuthoritySha256": "contracts/build_authority.json",
    "kPhase6TopologyContractSha256": "contracts/topology_operation.json",
    "kPhase6CdcContractSha256": "contracts/cdc_protocol.json",
    "kPhase6P2pContractSha256": "contracts/discrete_p2p.json",
    "kPhase6UmaContractSha256": "contracts/coherent_uma.json",
    "kPhase6CheckpointContractSha256": "contracts/checkpoint_schema.json",
    "kPhase6ClaimBoundarySha256": "contracts/claim_boundary.json",
}


class ValidationError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_root() -> str:
    digest = hashlib.sha256()
    for relative in SOURCE_MEMBERS:
        path = PHASE6_ROOT / relative
        if not path.is_file() or path.is_symlink():
            raise ValidationError(f"invalid source member: {relative}")
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(bytes.fromhex(sha256(path)))
    return digest.hexdigest()


def validate_source() -> dict[str, object]:
    if sha256(PHASE5_LEDGER) != EXPECTED_PHASE5_LEDGER:
        raise ValidationError("frozen Phase 5 ledger mismatch")
    if sha256(PHASE5_REVIEW_LEDGER) != EXPECTED_PHASE5_REVIEW_LEDGER:
        raise ValidationError("frozen Phase 5 review ledger mismatch")
    if sha256(PHASE5_AGGREGATE) != EXPECTED_PHASE5_AGGREGATE:
        raise ValidationError("frozen Phase 5 aggregate mismatch")
    aggregate = json.loads(PHASE5_AGGREGATE.read_text(encoding="utf-8"))
    if (
        aggregate["ledger_sha256"] != EXPECTED_PHASE5_LEDGER
        or aggregate["verdict"] != "GO"
        or aggregate["blockers"]
        or not aggregate["phase5_freeze"]
        or aggregate["phase6_authorized_scope"]
        != "CPU_SYNTHETIC_MULTI_GPU_NVLINK_AND_COHERENT_UMA_ONLY"
        or aggregate["gpu_authority"] != "NONE"
    ):
        raise ValidationError("Phase 5 promotion authority mismatch")

    header = (
        PHASE6_ROOT / "include/moe_sim/multi_domain_scheduler.hpp"
    ).read_text(encoding="utf-8")
    for constant, relative in AUTHORITY_ARTIFACTS.items():
        expected = sha256(PHASE6_ROOT / relative)
        declaration = f'{constant} =\n    "{expected}"'
        if declaration not in header:
            raise ValidationError(
                f"authority artifact is not exact-bound: {relative}"
            )
    for constant, expected in {
        "kPhase5LedgerSha256": EXPECTED_PHASE5_LEDGER,
        "kPhase5ReviewLedgerSha256": EXPECTED_PHASE5_REVIEW_LEDGER,
        "kPhase5ReviewAggregateSha256": EXPECTED_PHASE5_AGGREGATE,
    }.items():
        if f'{constant} =\n    "{expected}"' not in header:
            raise ValidationError(f"missing upstream exact binding: {constant}")

    build = json.loads(
        (PHASE6_ROOT / "contracts/build_authority.json").read_text()
    )
    topology = json.loads(
        (PHASE6_ROOT / "contracts/topology_operation.json").read_text()
    )
    cdc = json.loads(
        (PHASE6_ROOT / "contracts/cdc_protocol.json").read_text()
    )
    p2p = json.loads(
        (PHASE6_ROOT / "contracts/discrete_p2p.json").read_text()
    )
    uma = json.loads(
        (PHASE6_ROOT / "contracts/coherent_uma.json").read_text()
    )
    checkpoint = json.loads(
        (PHASE6_ROOT / "contracts/checkpoint_schema.json").read_text()
    )
    claims = json.loads(
        (PHASE6_ROOT / "contracts/claim_boundary.json").read_text()
    )
    scheduler = build["scheduler_authority"]
    alignment = cdc["synthetic_clock_alignment"]
    if (
        build["phase5_ledger_sha256"] != EXPECTED_PHASE5_LEDGER
        or build["phase5_review_ledger_sha256"]
        != EXPECTED_PHASE5_REVIEW_LEDGER
        or build["phase5_review_aggregate_sha256"]
        != EXPECTED_PHASE5_AGGREGATE
        or build["execution_scope"] != "CPU_SYNTHETIC_ONLY"
        or build["gpu_authority"] != "NONE"
        or scheduler["type"] != "MultiDomainSchedulerV1"
        or scheduler["global_scheduler_count"] != 1
        or scheduler["phase4_scheduler_instances"] != 0
        or topology["execution_mode"] != "TRACE_COMPILED_NON_ADAPTIVE"
        or topology["routing"]["granularity"] != "TOKEN_ONLY"
        or topology["routing"]["aggregate_input_accepted"]
        or topology["routing"]["routing_weights_modeled"]
        or not topology["program"]["compile_before_execution"]
        or topology["program"]["runtime_callbacks"]
        or not cdc["required_properties"]["queue_and_credit_conservation"]
        or alignment["calibration_method"]
        != "SIMULATOR_EXACT_SYNTHETIC"
        or alignment["residual_error_fs"] != 0
        or alignment["confidence_interval_95_fs"] != [0, 0]
        or alignment["quality"] != "CYCLE_GRADE"
        or p2p["mode"] != "DISCRETE_P2P_2GPU"
        or p2p["resources"]["aggregate_capacity_substitution"]
        or uma["mode"] != "COHERENT_UMA_2COMPUTE"
        or uma["topology"]["phantom_p2p_events"] != "forbidden"
        or checkpoint["checkpoint_type"]
        != "live exact-prefix resumable checkpoint"
        or checkpoint["wire"]["tamper"] != "reject"
        or claims["profile_origin"] != "CPU_SYNTHETIC"
        or claims["range_status"] != "RANGE_UNKNOWN"
        or claims["calibration_pass"]
        or claims["gpu_used"]
        or claims["gpu_authority"] != "NONE"
    ):
        raise ValidationError("Phase 6 contract boundary mismatch")

    forbidden: list[str] = []
    for root, directories, files in os.walk(PHASE6_ROOT, followlinks=False):
        base = Path(root)
        for name in [*directories, *files]:
            path = base / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not (
                stat.S_ISDIR(mode) or stat.S_ISREG(mode)
            ):
                forbidden.append(str(path.relative_to(PHASE6_ROOT)))
            if name == "__pycache__" or name.endswith((".pyc", ".pyo")):
                forbidden.append(str(path.relative_to(PHASE6_ROOT)))
    if forbidden:
        raise ValidationError(f"forbidden source entries: {sorted(forbidden)}")
    return {
        "status": "PASS",
        "phase5_ledger_sha256": EXPECTED_PHASE5_LEDGER,
        "source_member_count": len(SOURCE_MEMBERS),
        "source_root_sha256": source_root(),
        "profile_origin": "CPU_SYNTHETIC",
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
        or metrics["ctest_passed"] != 5
        or not metrics["asan_ubsan_pass"]
        or metrics["calibration_pass"]
        or metrics["profile_origin"] != "CPU_SYNTHETIC"
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

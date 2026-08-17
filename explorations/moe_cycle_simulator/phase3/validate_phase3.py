#!/usr/bin/env python3
"""Fail-closed source and sealed-run validation for Phase 3."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
from pathlib import Path

PHASE3_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PHASE3_ROOT.parents[2]
PHASE2_LEDGER = (
    REPO_ROOT
    / "explorations"
    / "moe_cycle_simulator"
    / "phase2"
    / "governance"
    / "checksums.sha256"
)
PHASE2_AGGREGATE = (
    PHASE2_LEDGER.parent / "reviews" / "phase2_r6_aggregate.json"
)
EXPECTED_PHASE2_LEDGER = (
    "dd2b9d9f8217fd6b78c1ae8b6f6e52c94a45774919cb36f0b407b53762c3d984"
)
SOURCE_MEMBERS = [
    "CMakeLists.txt",
    "contracts/checkpoint_wire.json",
    "contracts/engine_profile.json",
    "include/moe_sim/c_api.h",
    "include/moe_sim/engine.hpp",
    "python/moe_sim_phase3.py",
    "run_phase3_suite.py",
    "src/c_api.cpp",
    "src/engine.cpp",
    "tests/engine_tests.cpp",
    "tests/python_binding_test.py",
    "validate_phase3.py",
]


class ValidationError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_root() -> str:
    digest = hashlib.sha256()
    for relative in SOURCE_MEMBERS:
        path = PHASE3_ROOT / relative
        if not path.is_file() or path.is_symlink():
            raise ValidationError(f"missing or invalid source member: {relative}")
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(bytes.fromhex(sha256(path)))
    return digest.hexdigest()


def validate_source() -> dict[str, object]:
    if sha256(PHASE2_LEDGER) != EXPECTED_PHASE2_LEDGER:
        raise ValidationError("Phase 2 frozen ledger mismatch")
    aggregate = json.loads(PHASE2_AGGREGATE.read_text(encoding="utf-8"))
    if (
        aggregate["ledger_sha256"] != EXPECTED_PHASE2_LEDGER
        or aggregate["verdict"] != "GO"
        or aggregate["blockers"]
    ):
        raise ValidationError("Phase 2 GO aggregate mismatch")
    profile = json.loads(
        (PHASE3_ROOT / "contracts" / "engine_profile.json").read_text(
            encoding="utf-8"
        )
    )
    checkpoint = json.loads(
        (PHASE3_ROOT / "contracts" / "checkpoint_wire.json").read_text(
            encoding="utf-8"
        )
    )
    if (
        profile["phase2_ledger_sha256"] != EXPECTED_PHASE2_LEDGER
        or profile["service_mode"] != "REPLAY_VALIDATE"
        or profile["unsupported_action"] != "FAIL_CLOSED"
        or profile["arbitration"]["ROUND_ROBIN"]
        != "UNSUPPORTED_FAIL_CLOSED"
        or profile["limits"]["max_wait_for_nodes"] != 100000
        or not profile["limits"]["maxima_are_normative"]
        or not profile["limits"]["smaller_test_limits_allowed"]
        or profile["limits"]["enforcement"]
        != (
            "construction, every completion transition, checkpoint restore "
            "and deadlock traversal"
        )
        or profile["resource_state"]["arithmetic"]
        != "CHECKED_ARBITRARY_PRECISION_THEN_U128"
        or profile["resource_state"]["fresh_constructor_waiters"]
        != "FORBIDDEN"
        or profile["resource_state"]["holder_sum"]
        != "EXACT_CPP_INT_EQUALS_OCCUPANCY"
        or profile["resource_state"]["restore_waiters"]
        != "PRIVATE_RECONSTRUCTION_PATH_ONLY"
        or checkpoint["heap_layout_serialized"]
        or checkpoint["digest_algorithm"] != "SHA-256"
        or checkpoint["digest_preimage"]
        != (
            "complete canonical checkpoint body from magic through final "
            "trace field; excludes only state_digest"
        )
        or (
            "reverse resource reconstruction and deterministic "
            "scheduler-prefix replay"
            not in checkpoint["restore_checks"]
        )
    ):
        raise ValidationError("Phase 3 design boundary mismatch")
    forbidden: list[str] = []
    for root, directories, files in os.walk(PHASE3_ROOT, followlinks=False):
        base = Path(root)
        for name in [*directories, *files]:
            path = base / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not (
                stat.S_ISDIR(mode) or stat.S_ISREG(mode)
            ):
                forbidden.append(str(path.relative_to(PHASE3_ROOT)))
            if name == "__pycache__" or name.endswith((".pyc", ".pyo")):
                forbidden.append(str(path.relative_to(PHASE3_ROOT)))
    if forbidden:
        raise ValidationError(f"forbidden source entries: {sorted(forbidden)}")
    return {
        "status": "PASS",
        "phase2_ledger_sha256": EXPECTED_PHASE2_LEDGER,
        "source_member_count": len(SOURCE_MEMBERS),
        "source_root_sha256": source_root(),
        "gpu_used": False,
    }


def parse_ledger(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        digest, relative = line.split("  ", 1)
        if relative in result:
            raise ValidationError("duplicate run ledger member")
        result[relative] = digest
    return result


def validate_run(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_dir():
        raise ValidationError("run directory is invalid")
    ledger = parse_ledger(path / "checksums.sha256")
    actual: dict[str, str] = {}
    for root, directories, files in os.walk(path, followlinks=False):
        base = Path(root)
        for name in [*directories, *files]:
            entry = base / name
            mode = entry.lstat().st_mode
            if stat.S_ISLNK(mode) or not (
                stat.S_ISDIR(mode) or stat.S_ISREG(mode)
            ):
                raise ValidationError("run contains forbidden entry")
        for name in files:
            entry = base / name
            if entry.name != "checksums.sha256":
                actual[entry.relative_to(path).as_posix()] = sha256(entry)
    if actual != ledger:
        raise ValidationError("run ledger exact-set mismatch")
    metrics = json.loads((path / "metrics.json").read_text())
    result = json.loads((path / "suite_result.json").read_text())
    if (
        metrics["status"] != "PASS"
        or result["status"] != "PASS"
        or metrics["gpu_used"]
        or result["gpu_used"]
        or metrics["ctest_passed"] != 2
        or not metrics["asan_ubsan_pass"]
    ):
        raise ValidationError("run result boundary mismatch")
    return metrics


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path)
    args = parser.parse_args()
    result = validate_run(args.run_dir) if args.run_dir else validate_source()
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Validate the Phase 4 R4 candidate ledger as an exact file-set."""
from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path

GOVERNANCE = Path(__file__).resolve().parent
PHASE4 = GOVERNANCE.parent
REPO = PHASE4.parents[2]
LEDGER = GOVERNANCE / "checksums.sha256"
ACTIVE_LEDGER = LEDGER.relative_to(REPO).as_posix()
R4_REVIEW_PREFIX = (
    "explorations/moe_cycle_simulator/phase4/governance/reviews/phase4_r4_"
)
RUN = REPO / (
    "runs/20260728T123113Z__"
    "moe_cycle_simulator_phase4_single_gpu_r3__S4"
)
FIXED = {
    "experiments/specs/moe_cycle_simulator_phase4.yaml",
    "explorations/moe_cycle_simulator/phase3/governance/checksums.sha256",
    (
        "explorations/moe_cycle_simulator/phase3/governance/reviews/"
        "phase3_r5_aggregate.json"
    ),
}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def regular_files(root: Path) -> set[str]:
    members: set[str] = set()
    for path in root.rglob("*"):
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise RuntimeError(f"symlink forbidden: {path}")
        if path.is_file():
            members.add(path.relative_to(REPO).as_posix())
        elif not path.is_dir():
            raise RuntimeError(f"special entry forbidden: {path}")
    return members


def main() -> int:
    expected = FIXED | regular_files(PHASE4) | regular_files(RUN)
    expected.remove(ACTIVE_LEDGER)
    expected = {
        path for path in expected if not path.startswith(R4_REVIEW_PREFIX)
    }
    listed: dict[str, str] = {}
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        value, path = line.split("  ", 1)
        if path in listed:
            raise RuntimeError(f"duplicate ledger member: {path}")
        listed[path] = value
    if set(listed) != expected:
        raise RuntimeError(
            json.dumps(
                {
                    "missing": sorted(expected - set(listed)),
                    "extra": sorted(set(listed) - expected),
                },
                sort_keys=True,
            )
        )
    for path, value in listed.items():
        if digest(REPO / path) != value:
            raise RuntimeError(f"hash mismatch: {path}")
    print(
        json.dumps(
            {
                "status": "PASS",
                "member_count": len(listed),
                "ledger_sha256": digest(LEDGER),
                "nested_checksum_ledgers": "INCLUDED",
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

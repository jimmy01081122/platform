#!/usr/bin/env python3
"""Validate the Phase 7 R12-R3 candidate ledger as an exact file set."""

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path


GOVERNANCE = Path(__file__).resolve().parent
PHASE7 = GOVERNANCE.parent
REPO = PHASE7.parents[2]
LEDGER = GOVERNANCE / "checksums.sha256"
ACTIVE_LEDGER = LEDGER.relative_to(REPO).as_posix()
REVIEW_PREFIX = (
    "explorations/moe_cycle_simulator/phase7/governance/reviews/phase7_r12r3_"
)
RUN = REPO / (
    "runs/20260801T173446Z__"
    "moe_cycle_simulator_phase7_cpu_governance_r12r3_final__S2"
)
FIXED = {
    "experiments/specs/moe_cycle_simulator_phase7.yaml",
    "experiments/specs/moe_cycle_simulator_phase7_cpu_framework.yaml",
    "explorations/moe_cycle_simulator/phase6/governance/checksums.sha256",
    (
        "explorations/moe_cycle_simulator/phase6/governance/reviews/"
        "phase6_r4_checksums.sha256"
    ),
    (
        "explorations/moe_cycle_simulator/phase6/governance/reviews/"
        "phase6_r4_aggregate.json"
    ),
    "docs/status/AGENT_HANDOFF.md",
    "docs/status/ASSUMPTION_REGISTER.md",
    "docs/status/CURRENT_STATUS.md",
    "docs/status/DECISION_LOG.md",
    "docs/status/VALIDATION_MATRIX.md",
    "docs/status/RTX_PRO_6000_MIXTRAL_BF16_M0_APPLICATION_REQUEST_20260729.md",
    "docs/status/RTX_PRO_6000_MIXTRAL_BF16_DEPLOYMENT_USE_PLAN_20260801.md",
}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def is_ephemeral(path: Path) -> bool:
    return "__pycache__" in path.parts or path.suffix == ".pyc"


def regular_files(root: Path) -> set[str]:
    members: set[str] = set()
    for path in root.rglob("*"):
        if is_ephemeral(path):
            continue
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise RuntimeError(f"symlink forbidden: {path}")
        if path.is_file():
            members.add(path.relative_to(REPO).as_posix())
        elif not path.is_dir():
            raise RuntimeError(f"special entry forbidden: {path}")
    return members


def main() -> int:
    expected = FIXED | regular_files(PHASE7) | regular_files(RUN)
    expected.discard(ACTIVE_LEDGER)
    expected = {path for path in expected if not path.startswith(REVIEW_PREFIX)}
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
    if list(listed) != sorted(listed):
        raise RuntimeError("candidate ledger paths are not lexically sorted")
    for path, value in listed.items():
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise RuntimeError(f"malformed SHA-256: {path}")
        if digest(REPO / path) != value:
            raise RuntimeError(f"hash mismatch: {path}")
    print(
        json.dumps(
            {
                "status": "PASS",
                "member_count": len(listed),
                "ledger_sha256": digest(LEDGER),
                "nested_checksum_ledgers": "INCLUDED",
                "gpu_authority": "NONE",
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Build or verify the recursive, symlink-free Phase 7 application ledger."""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[5]))

from explorations.moe_cycle_simulator.phase7.application.executor.common import (  # noqa: E402
    M0Error,
    file_sha256,
    load_json,
    write_new_json,
)


EXCLUSIONS = {
    "approval.template.json",
    "environment_disclosure_approval.template.json",
    "materialization_approval.template.json",
}


def build(root: Path) -> dict:
    root = root.resolve(strict=True)
    if not root.is_dir() or root.is_symlink():
        raise M0Error("application package root must be a real directory")
    members = []
    ledger_rows = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise M0Error(f"application package symlink is forbidden: {path}")
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        relative = path.relative_to(root).as_posix()
        if relative in EXCLUSIONS or relative.endswith(".pyc"):
            continue
        digest = file_sha256(path)
        members.append(
            {
                "path": relative,
                "size_bytes": path.stat().st_size,
                "sha256": digest,
            }
        )
        ledger_rows.append(f"{digest}  {relative}\n".encode("utf-8"))
    if not members:
        raise M0Error("application package ledger is empty")
    return {
        "schema_version": "moe-simulator-phase7-application-ledger-v2",
        "root_name": root.name,
        "member_count": len(members),
        "members": members,
        "ledger_sha256": hashlib.sha256(b"".join(ledger_rows)).hexdigest(),
        "excluded_mutable_approval_files": sorted(EXCLUSIONS),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--output", type=Path)
    group.add_argument("--verify", type=Path)
    args = parser.parse_args()
    actual = build(args.root)
    if args.output is not None:
        if args.output.resolve().is_relative_to(args.root.resolve()):
            raise M0Error("ledger output must be outside its package root")
        write_new_json(args.output, actual)
    else:
        expected = load_json(args.verify)
        if actual != expected:
            raise M0Error("application package differs from its frozen ledger")
    print(actual["ledger_sha256"])
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except M0Error as exc:
        raise SystemExit(f"HARD-STOP: {exc}") from exc

#!/usr/bin/env python3
"""Freeze a benchmark manifest into an immutable revision inventory."""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs" / "test_suites"


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_hash(path: Path) -> str:
    return sha256(path.read_bytes())


def merkle_root(leaves: list[bytes]) -> str:
    if not leaves:
        return sha256(b"")
    level = [hashlib.sha256(leaf).digest() for leaf in leaves]
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [
            hashlib.sha256(level[index] + level[index + 1]).digest()
            for index in range(0, len(level), 2)
        ]
    return level[0].hex()


def freeze_root(manifest_root: str, source_files: dict[str, str]) -> str:
    leaves = [canonical({
        "domain": "manifest_merkle_root",
        "sha256": manifest_root,
    })]
    leaves.extend(
        canonical({
            "domain": "source_file_sha256",
            "path": path,
            "sha256": digest,
        })
        for path, digest in sorted(source_files.items())
    )
    return merkle_root(leaves)


def load_manifest(path: Path) -> tuple[list[dict], list[bytes]]:
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    canonical_lines = [canonical(row) for row in rows]
    sample_ids = [row["sample_id"] for row in rows]
    if len(sample_ids) != len(set(sample_ids)):
        raise ValueError("manifest contains duplicate sample_id")
    return rows, canonical_lines


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path,
                        default=CONFIG / "sample_manifest_v1.jsonl")
    parser.add_argument("--suite", type=Path,
                        default=CONFIG / "moe_trace_suite_v1.yaml")
    parser.add_argument("--gate-report", type=Path,
                        default=CONFIG / "unresolved_gates_v1.json")
    parser.add_argument("--output-root", type=Path, default=CONFIG / "frozen")
    parser.add_argument("--revision")
    args = parser.parse_args()
    suite = yaml.safe_load(args.suite.read_text(encoding="utf-8"))
    revision = args.revision or suite["suite_revision"]
    if not re.fullmatch(r"[A-Za-z0-9._-]+", revision):
        raise SystemExit("revision must contain only letters, digits, dot, underscore, dash")
    destination = args.output_root / revision
    if destination.exists():
        raise SystemExit(f"refusing to overwrite frozen revision: {destination}")
    rows, lines = load_manifest(args.manifest)
    gates = (
        json.loads(args.gate_report.read_text(encoding="utf-8"))
        if args.gate_report.is_file() else {"gates": []}
    )
    split_index = yaml.safe_load(
        (CONFIG / "splits" / "v1.yaml").read_text(encoding="utf-8")
    )
    split_axis_paths = [
        ROOT / details["manifest"]
        for _, details in sorted(split_index["axes"].items())
    ]
    source_paths = [
        args.manifest,
        args.suite,
        CONFIG / "benchmark_registry.yaml",
        CONFIG / "splits" / "v1.yaml",
        CONFIG / "prompt_templates" / "v1.yaml",
        CONFIG / "generation_configs" / "v1.yaml",
        CONFIG / "serving_schedules" / "v1.yaml",
        CONFIG / "model_benchmark_matrix.yaml",
        ROOT / "datasets/snapshots/snapshot_inventory_v1.json",
        *split_axis_paths,
    ]
    source_files = {
        str(path.relative_to(ROOT)): file_hash(path)
        for path in source_paths
    }
    manifest_root = merkle_root(lines)
    inventory = {
        "schema_version": "frozen-benchmark-suite-v2",
        "suite_id": suite["suite_id"],
        "suite_revision": revision,
        "sample_count": len(rows),
        "merkle": {
            "algorithm": "sha256",
            "leaf_contract": (
                "manifest_merkle_root plus domain-separated canonical "
                "source-file path/SHA-256 leaves"
            ),
            "odd_leaf_rule": "duplicate_last",
            "manifest_root": manifest_root,
            "root": freeze_root(manifest_root, source_files),
        },
        "counts": {
            "by_task": dict(sorted(collections.Counter(
                row["task_id"] for row in rows
            ).items())),
            "by_split": dict(sorted(collections.Counter(
                row["split"] for row in rows
            ).items())),
            "by_role": dict(sorted(collections.Counter(
                row["role"] for row in rows
            ).items())),
        },
        "unresolved_gates": gates.get("gates", []),
        "source_files": source_files,
        "split_axes": {
            name: {
                "active": details["active"],
                "manifest": details["manifest"],
                "sha256": source_files[details["manifest"]],
            }
            for name, details in sorted(split_index["axes"].items())
        },
    }
    destination.mkdir(parents=True)
    try:
        frozen_manifest = destination / "sample_manifest.jsonl"
        frozen_manifest.write_bytes(b"".join(line + b"\n" for line in lines))
        inventory["frozen_manifest_sha256"] = file_hash(frozen_manifest)
        (destination / "inventory.json").write_text(
            json.dumps(inventory, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except Exception:
        shutil.rmtree(destination)
        raise
    print(json.dumps({
        "revision": revision,
        "sample_count": len(rows),
        "merkle_root": inventory["merkle"]["root"],
        "unresolved_gate_count": len(inventory["unresolved_gates"]),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

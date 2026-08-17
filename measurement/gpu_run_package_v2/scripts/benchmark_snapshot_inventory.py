#!/usr/bin/env python3
"""Create or verify the local pinned benchmark snapshot inventory."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "configs/test_suites/benchmark_registry.yaml"
DEFAULT_OUTPUT = ROOT / "datasets/snapshots/snapshot_inventory_v1.json"


def build_inventory() -> dict:
    try:
        import pyarrow.parquet as parquet
    except ImportError as exc:
        raise RuntimeError(
            "pyarrow is required; run with PYTHONPATH=.benchmark-runtime"
        ) from exc
    registry = yaml.safe_load(REGISTRY.read_text(encoding="utf-8"))
    files = []
    for benchmark, spec in sorted(registry["datasets"].items()):
        for file_spec in spec["files"]:
            path = ROOT / spec["snapshot_root"] / file_spec["path"]
            if not path.is_file():
                raise FileNotFoundError(path)
            relative = str(path.relative_to(ROOT))
            data = path.read_bytes()
            metadata = parquet.read_metadata(path)
            source_url = (
                f"{spec['source_url']}/resolve/{spec['dataset_revision']}/"
                f"{file_spec['path']}?download=true"
            )
            files.append({
                "benchmark": benchmark,
                "dataset_id": spec["dataset_id"],
                "dataset_revision": spec["dataset_revision"],
                "config": file_spec["config"],
                "split": file_spec["split"],
                "domain": file_spec.get("domain"),
                "path": relative,
                "bytes": len(data),
                "rows": metadata.num_rows,
                "sha256": hashlib.sha256(data).hexdigest(),
                "license": spec["license"],
                "source_url": source_url,
            })
    return {
        "schema_version": "benchmark-snapshot-inventory-v1",
        "registry_revision": registry["registry_revision"],
        "file_count": len(files),
        "total_bytes": sum(item["bytes"] for item in files),
        "files": files,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    actual = build_inventory()
    encoded = json.dumps(
        actual, ensure_ascii=False, indent=2, sort_keys=True
    ) + "\n"
    if args.verify:
        if not args.output.is_file():
            raise SystemExit(f"inventory missing: {args.output}")
        if args.output.read_text(encoding="utf-8") != encoded:
            raise SystemExit("snapshot inventory differs from local files")
        print(json.dumps({
            "status": "verified",
            "file_count": actual["file_count"],
            "total_bytes": actual["total_bytes"],
        }, sort_keys=True))
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(encoded, encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

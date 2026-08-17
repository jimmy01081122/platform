#!/usr/bin/env python3
"""Export immutable native traces and register canonical conversion provenance."""
from __future__ import annotations

import argparse
import os
import shutil
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))

from collectors.trace_contract import (  # noqa: E402
    load_json, relative_package_path, sha256_file, write_json,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def immutable_copy(source: Path, destination: Path) -> None:
    if destination.exists():
        if not destination.is_file() or sha256_file(destination) != sha256_file(source):
            raise ValueError(f"content-address collision: {destination}")
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
    shutil.copyfile(source, temporary)
    os.chmod(temporary, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    temporary.replace(destination)


def load_inventory(path: Path) -> dict:
    if path.exists():
        inventory = load_json(path)
        if inventory.get("schema_version") != "trace-raw-inventory-v2":
            raise ValueError("existing raw inventory has unsupported schema")
        return inventory
    return {
        "schema_version": "trace-raw-inventory-v2",
        "digest_algorithm": "sha256",
        "content_addressed": True,
        "immutable": True,
        "entries": [],
        "conversions": [],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-root", required=True, type=Path)
    parser.add_argument("--raw", required=True, type=Path, action="append")
    parser.add_argument("--pass-id", required=True, choices=[f"P{x}" for x in range(7)])
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--native-format", required=True)
    parser.add_argument("--canonical", type=Path)
    parser.add_argument("--converter-name")
    parser.add_argument("--converter-version")
    parser.add_argument("--converter-source", type=Path)
    args = parser.parse_args()
    root = args.session_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    inventory_path = root / "raw_traces" / "RAW_INVENTORY.json"
    inventory = load_inventory(inventory_path)
    content_ids: list[str] = []
    known = {entry["content_id"] for entry in inventory["entries"]}
    for source in args.raw:
        source = source.resolve()
        if not source.is_file() or source.is_symlink():
            raise SystemExit(f"raw trace must be a regular non-symlink file: {source}")
        size = source.stat().st_size
        if size <= 0:
            raise SystemExit(f"raw trace is empty: {source}")
        content_id = sha256_file(source)
        suffix = "".join(source.suffixes)[-32:] or ".bin"
        destination = root / "raw_traces" / "sha256" / content_id[:2] / (
            content_id + suffix
        )
        immutable_copy(source, destination)
        content_ids.append(content_id)
        if content_id not in known:
            inventory["entries"].append({
                "content_id": content_id,
                "sha256": content_id,
                "bytes": size,
                "path": relative_package_path(root, destination),
                "source_name": source.name,
                "native_format": args.native_format,
                "pass_id": args.pass_id,
                "run_id": args.run_id,
                "captured_utc": utc_now(),
                "immutable": True,
                "truncated": False,
            })
            known.add(content_id)
    converter_args = (
        args.converter_name, args.converter_version, args.converter_source
    )
    if args.canonical or any(converter_args):
        if not args.canonical or not all(converter_args):
            parser.error(
                "--canonical requires --converter-name, --converter-version, "
                "and --converter-source"
            )
        canonical = args.canonical.resolve()
        if not canonical.is_file() or canonical.stat().st_size <= 0:
            raise SystemExit(f"canonical trace is missing or empty: {canonical}")
        canonical_id = sha256_file(canonical)
        destination = root / "canonical_traces" / f"{canonical_id}{canonical.suffix}"
        immutable_copy(canonical, destination)
        inventory["conversions"].append({
            "canonical_path": relative_package_path(root, destination),
            "canonical_sha256": canonical_id,
            "input_content_ids": sorted(set(content_ids)),
            "converter": {
                "name": args.converter_name,
                "version": args.converter_version,
                "source_hash": sha256_file(args.converter_source.resolve()),
            },
            "converted_utc": utc_now(),
        })
    inventory["entries"].sort(key=lambda item: item["content_id"])
    write_json(inventory_path, inventory)
    print(inventory_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

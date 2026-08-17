#!/usr/bin/env python3
"""Read-only audit for the external Switch Colab trace registry."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = REPO_ROOT / "data/registry/switch_colab_trace_readonly_v1.yaml"
DEFAULT_SCHEMA = REPO_ROOT / "schemas/external_trace_registry.schema.json"


def sha256_file(path: Path, chunk_size: int = 4 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_registry(path: Path) -> dict[str, Any]:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError("PyYAML is required: install the project 'yaml' extra") from exc
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("registry must be a YAML mapping")
    return data


def validate_registry_schema(registry: dict[str, Any], schema_path: Path) -> list[str]:
    try:
        import jsonschema
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError("jsonschema is required: install the project 'validation' extra") from exc
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator_class = getattr(
        jsonschema, "Draft202012Validator", jsonschema.Draft7Validator
    )
    validator = validator_class(schema)
    return [
        f"registry schema: {'/'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.message}"
        for error in sorted(
            validator.iter_errors(registry),
            key=lambda item: "/".join(str(part) for part in item.absolute_path),
        )
    ]


def load_inventory(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    errors: list[str] = []
    with path.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        required = {
            "relative_path",
            "kind",
            "bytes",
            "sha256",
            "observed_schema",
            "schema_sample_scope",
        }
        missing = required - set(reader.fieldnames or ())
        if missing:
            return [], [f"inventory columns missing: {sorted(missing)}"]
        rows = list(reader)
    paths = [row["relative_path"] for row in rows]
    duplicates = sorted(path for path, count in Counter(paths).items() if count > 1)
    if duplicates:
        errors.append(f"duplicate inventory paths: {duplicates[:10]}")
    return rows, errors


def content_set_sha256(rows: Iterable[dict[str, str]]) -> str:
    digest = hashlib.sha256()
    for row in sorted(rows, key=lambda item: item["relative_path"]):
        digest.update(row["relative_path"].encode("utf-8"))
        digest.update(b"\0")
        digest.update(row["bytes"].encode("ascii"))
        digest.update(b"\0")
        digest.update(row["sha256"].encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def observed_column_names(raw: str) -> set[str]:
    if not raw:
        return set()
    value = json.loads(raw)
    if not isinstance(value, list):
        return set()
    return {
        item["name"]
        for item in value
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }


def audit_schema_relations(
    rows: list[dict[str, str]], trace_types: list[dict[str, Any]]
) -> tuple[dict[str, int], list[str]]:
    errors: list[str] = []
    counts: dict[str, int] = {}
    for trace_type in trace_types:
        file_name = trace_type["file_name"]
        relation = trace_type["schema_relation"]
        matched = [row for row in rows if Path(row["relative_path"]).name == file_name]
        counts[trace_type["name"]] = len(matched)
        if not matched:
            errors.append(f"schema relation has no inventory rows: {file_name}")
            continue
        required_columns = set(relation["required_columns"])
        for row in matched:
            if row["kind"] != relation["inventory_kind"]:
                errors.append(
                    f"{row['relative_path']}: kind {row['kind']!r} != "
                    f"{relation['inventory_kind']!r}"
                )
            try:
                observed = observed_column_names(row["observed_schema"])
            except (json.JSONDecodeError, TypeError) as exc:
                errors.append(f"{row['relative_path']}: invalid observed_schema: {exc}")
                continue
            missing = required_columns - observed
            if missing:
                errors.append(f"{row['relative_path']}: missing observed columns {sorted(missing)}")
    return counts, errors


def safe_source_path(root: Path, relative_path: str) -> Path:
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"unsafe relative path: {relative_path}")
    candidate = root / relative
    resolved_root = root.resolve()
    resolved_candidate = candidate.resolve()
    if resolved_candidate != resolved_root and resolved_root not in resolved_candidate.parents:
        raise ValueError(f"path escapes source root: {relative_path}")
    return candidate


def audit_files(
    root: Path, rows: list[dict[str, str]], mode: str
) -> tuple[int, int, list[str]]:
    errors: list[str] = []
    checked_bytes = 0
    checked_sha256 = 0
    for row in rows:
        relative_path = row["relative_path"]
        try:
            path = safe_source_path(root, relative_path)
            expected_bytes = int(row["bytes"])
        except (ValueError, OSError) as exc:
            errors.append(f"{relative_path}: {exc}")
            continue
        try:
            stat = path.stat()
        except OSError as exc:
            errors.append(f"{relative_path}: cannot stat: {exc}")
            continue
        if not path.is_file():
            errors.append(f"{relative_path}: not a regular file")
            continue
        checked_bytes += 1
        if stat.st_size != expected_bytes:
            errors.append(f"{relative_path}: bytes {stat.st_size} != {expected_bytes}")
        if mode == "full":
            actual_sha256 = sha256_file(path)
            checked_sha256 += 1
            if actual_sha256 != row["sha256"]:
                errors.append(
                    f"{relative_path}: sha256 {actual_sha256} != {row['sha256']}"
                )
    return checked_bytes, checked_sha256, errors


def audit_corpus(
    root: Path, rows: list[dict[str, str]], registry: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    metadata_rows = [
        row for row in rows if Path(row["relative_path"]).name == "run_metadata.json"
    ]
    models: set[tuple[str, int]] = set()
    benchmarks: set[str] = set()
    for row in metadata_rows:
        try:
            path = safe_source_path(root, row["relative_path"])
            metadata = json.loads(path.read_text(encoding="utf-8"))
            models.add((metadata["model_name"], int(metadata["num_experts"])))
            benchmarks.add(metadata["dataset_name"])
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            errors.append(f"{row['relative_path']}: invalid run metadata: {exc}")
    expected_models = {
        (item["name"], int(item["num_experts"])) for item in registry["corpus"]["models"]
    }
    expected_benchmarks = set(registry["corpus"]["benchmarks"])
    expected_runs = int(registry["corpus"]["run_count"])
    if len(metadata_rows) != expected_runs:
        errors.append(f"run count {len(metadata_rows)} != {expected_runs}")
    if models != expected_models:
        errors.append(f"models {sorted(models)} != {sorted(expected_models)}")
    if benchmarks != expected_benchmarks:
        errors.append(
            f"benchmarks {sorted(benchmarks)} != {sorted(expected_benchmarks)}"
        )
    return {
        "run_count": len(metadata_rows),
        "models": [
            {"name": name, "num_experts": experts}
            for name, experts in sorted(models, key=lambda item: item[1])
        ],
        "benchmarks": sorted(benchmarks),
    }, errors


def run_audit(
    registry_path: Path,
    schema_path: Path,
    root_override: Path | None,
    inventory_override: Path | None,
    mode: str,
) -> dict[str, Any]:
    registry = load_registry(registry_path)
    errors = validate_registry_schema(registry, schema_path)
    if registry.get("read_only") is not True or registry["source"]["access"] != "read_only":
        errors.append("registry and source must both declare read-only access")
    root = root_override or Path(registry["source"]["default_path"])
    inventory = inventory_override or Path(registry["inventory"]["default_path"])

    actual_inventory_sha256 = sha256_file(inventory)
    if actual_inventory_sha256 != registry["inventory"]["sha256"]:
        errors.append(
            f"inventory sha256 {actual_inventory_sha256} != "
            f"{registry['inventory']['sha256']}"
        )
    rows, inventory_errors = load_inventory(inventory)
    errors.extend(inventory_errors)
    actual_content_set_sha256 = content_set_sha256(rows)
    if actual_content_set_sha256 != registry["inventory"]["content_set_sha256"]:
        errors.append(
            f"content-set sha256 {actual_content_set_sha256} != "
            f"{registry['inventory']['content_set_sha256']}"
        )
    file_count = len(rows)
    total_bytes = sum(int(row["bytes"]) for row in rows)
    if file_count != registry["inventory"]["file_count"]:
        errors.append(f"file count {file_count} != {registry['inventory']['file_count']}")
    if total_bytes != registry["inventory"]["total_bytes"]:
        errors.append(f"total bytes {total_bytes} != {registry['inventory']['total_bytes']}")

    trace_counts, relation_errors = audit_schema_relations(rows, registry["trace_types"])
    errors.extend(relation_errors)
    checked_bytes, checked_sha256, file_errors = audit_files(root, rows, mode)
    errors.extend(file_errors)
    corpus, corpus_errors = audit_corpus(root, rows, registry)
    errors.extend(corpus_errors)

    return {
        "registry_id": registry["registry_id"],
        "mode": mode,
        "status": "pass" if not errors else "fail",
        "read_only": registry["read_only"],
        "complete": registry["complete"],
        "root": str(root),
        "inventory": str(inventory),
        "inventory_sha256": actual_inventory_sha256,
        "content_set_sha256": actual_content_set_sha256,
        "file_count": file_count,
        "total_bytes": total_bytes,
        "files_stat_checked": checked_bytes,
        "files_sha256_checked": checked_sha256,
        "trace_type_counts": trace_counts,
        "corpus": corpus,
        "error_count": len(errors),
        "errors": errors[:100],
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--root", type=Path, help="override the read-only trace root")
    parser.add_argument("--inventory", type=Path, help="override TRACE_INVENTORY.csv")
    parser.add_argument("--mode", choices=("quick", "full"), default="quick")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = run_audit(
            args.registry, args.schema, args.root, args.inventory, args.mode
        )
    except (OSError, RuntimeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        result = {
            "mode": args.mode,
            "status": "fail",
            "error_count": 1,
            "errors": [str(exc)],
        }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())

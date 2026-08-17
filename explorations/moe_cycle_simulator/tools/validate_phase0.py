#!/usr/bin/env python3
"""Fail-closed validator for the MoE simulator Phase 0 candidate."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from contract_runtime import (  # noqa: E402
    Clock,
    ContractError,
    alignment_grade,
    canonical_bytes,
    cdc_arrival,
    dataset_semantic_hash,
    pair_alignment_grade,
    schema_fingerprint,
    validate_alignment,
    validate_bridge,
    validate_events,
    validate_observability,
    validate_result_evidence,
    validate_routing,
    validate_runtime_variant,
)

REQUIRED_JSONSCHEMA = "4.24.0"
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
DECIMAL_RE = re.compile(r"^(0|[1-9][0-9]*)$")


class ValidationFailure(RuntimeError):
    pass


def load_json(path: Path) -> dict[str, Any]:
    def reject_constant(value: str) -> None:
        raise ValidationFailure(f"{path}: non-finite JSON number {value}")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for raw_key, item in pairs:
            key = unicodedata.normalize("NFC", raw_key)
            if key in result:
                raise ValidationFailure(
                    f"{path}: duplicate JSON object key after NFC normalization: {key}"
                )
            result[key] = item
        return result

    value = json.loads(
        path.read_text(encoding="utf-8"),
        parse_constant=reject_constant,
        object_pairs_hook=unique_object,
    )
    if not isinstance(value, dict):
        raise ValidationFailure(f"{path}: top-level value must be an object")
    reject_floats(value, str(path))
    return value


def reject_floats(value: Any, location: str) -> None:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValidationFailure(f"{location}: non-finite float")
        raise ValidationFailure(f"{location}: JSON floats are forbidden")
    if isinstance(value, dict):
        for key, item in value.items():
            reject_floats(item, f"{location}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            reject_floats(item, f"{location}[{index}]")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_schema_documents(root: Path) -> None:
    try:
        if importlib.metadata.version("jsonschema") != REQUIRED_JSONSCHEMA:
            raise ValidationFailure(
                f"jsonschema must be exactly {REQUIRED_JSONSCHEMA}"
            )
        import jsonschema
    except importlib.metadata.PackageNotFoundError as exc:
        raise ValidationFailure("jsonschema is not installed") from exc

    for path in sorted((root / "schemas").glob("*.schema.json")):
        schema = load_json(path)
        if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
            raise ValidationFailure(f"{path}: Draft 2020-12 is required")
        jsonschema.Draft202012Validator.check_schema(schema)
        if schema.get("additionalProperties") is not False:
            raise ValidationFailure(f"{path}: root additionalProperties must be false")


def _schema_validators(root: Path) -> dict[str, Any]:
    import jsonschema
    from referencing import Registry, Resource

    schemas = {
        path.name: load_json(path)
        for path in sorted((root / "schemas").glob("*.schema.json"))
    }
    registry = Registry().with_resources(
        (schema["$id"], Resource.from_contents(schema))
        for schema in schemas.values()
    )
    return {
        name: jsonschema.Draft202012Validator(schema, registry=registry)
        for name, schema in schemas.items()
    }


def validate_semantic_fixtures(root: Path) -> None:
    fixture = load_json(root / "fixtures" / "phase0_semantic_fixtures.json")
    validators = _schema_validators(root)
    descriptor_contract = load_json(
        root / "contracts" / "semantic_descriptors.json"
    )["descriptors"]
    descriptor_hashes = {
        name: schema_fingerprint(value)
        for name, value in descriptor_contract.items()
    }
    clocks: dict[str, Clock] = {}
    for value in fixture["clocks"]:
        validators["clock_domain.schema.json"].validate(value)
        clock = Clock.from_record(value)
        clocks[clock.clock_id] = clock
    for value in fixture["bridges"]:
        validators["bridge.schema.json"].validate(value)
        validate_bridge(value, clocks)
    calibration_records = {
        value["record_id"]: value for value in fixture["calibration_records"]
    }
    clock_records = {value["clock_id"]: value for value in fixture["clocks"]}
    for value in fixture["alignments"]:
        validators["clock_alignment.schema.json"].validate(value)
        validate_alignment(value)
        target_record = clock_records[value["target_clock_id"]]
        target_hash = hashlib.sha256(canonical_bytes(target_record)).hexdigest()
        inputs = value["grading_inputs"]
        if target_hash != inputs["target_clock_profile_hash"]:
            raise ValidationFailure("alignment target clock profile hash mismatch")
        target_clock = clocks[value["target_clock_id"]]
        period_numerator = (
            10**15 * target_clock.frequency_denominator_hz
        )
        period_denominator = target_clock.frequency_numerator_hz
        divisor = math.gcd(period_numerator, period_denominator)
        if (
            str(period_numerator // divisor)
            != inputs["target_period_numerator_fs"]
            or str(period_denominator // divisor)
            != inputs["target_period_denominator"]
        ):
            raise ValidationFailure("alignment target period mismatch")
        shortest = min(
            calibration_records.values(),
            key=lambda item: int(item["duration_fs"]),
        )
        shortest_hash = hashlib.sha256(canonical_bytes(shortest)).hexdigest()
        if (
            shortest_hash != inputs["shortest_component_record_hash"]
            or shortest["duration_fs"]
            != inputs["shortest_component_duration_fs"]
        ):
            raise ValidationFailure("alignment shortest component binding mismatch")
    coarse = json.loads(json.dumps(fixture["alignments"][0]))
    coarse["confidence_interval_95_fs"] = {
        "lower_error_fs": "-200000",
        "upper_error_fs": "200000",
    }
    coarse["claimed_grade"] = "AGGREGATE_ONLY"
    validate_alignment(coarse)
    if pair_alignment_grade(coarse, 0, coarse, 1_000_000) != "ORDERING_ONLY":
        raise ValidationFailure("alignment ordering-only golden mismatch")
    if pair_alignment_grade(coarse, 0, coarse, 100_000) != "AGGREGATE_ONLY":
        raise ValidationFailure("alignment aggregate-only golden mismatch")
    for value in fixture["observability"]:
        validators["observability.schema.json"].validate(value)
        validate_observability(value)
    for value in fixture["result_evidence"]:
        validators["result_evidence.schema.json"].validate(value)
        validate_result_evidence(value)
    for value in fixture["routing"]:
        validators["routing_ir.schema.json"].validate(value)
        if value["semantic_descriptor_hash"] != descriptor_hashes["routing-ir-v1"]:
            raise ValidationFailure("routing semantic descriptor hash mismatch")
        validate_routing(value)
    for value in fixture["runtime_variants"]:
        validators["runtime_variant.schema.json"].validate(value)
        validate_runtime_variant(value)
    for value in fixture["events"]:
        validators["event_ir.schema.json"].validate(value)
        if value["semantic_descriptor_hash"] != descriptor_hashes["event-ir-v1"]:
            raise ValidationFailure("event semantic descriptor hash mismatch")
        validate_observability(value["observability"])
    priority_contract = load_json(root / "contracts" / "event_priorities.json")
    priorities = {
        entry["name"]: entry["value"]
        for entry in priority_contract["priorities"]
    }
    validate_events(fixture["events"], clocks, priorities)
    if cdc_arrival(0, fixture["bridges"][0], clocks) != 1_250_000:
        raise ValidationFailure("CDC golden arrival mismatch")
    semantic = fixture["semantic_hash"]
    if schema_fingerprint(semantic["descriptor"]) != semantic[
        "expected_schema_fingerprint"
    ]:
        raise ValidationFailure("semantic schema fingerprint mismatch")
    rows, aggregate = dataset_semantic_hash(
        semantic["rows"], semantic["descriptor"]
    )
    if rows != semantic["expected_row_hashes"]:
        raise ValidationFailure("semantic row hashes mismatch")
    if aggregate != semantic["expected_aggregate_hash"]:
        raise ValidationFailure("semantic aggregate hash mismatch")


def validate_architecture(root: Path) -> None:
    decisions = load_json(root / "contracts" / "architecture_decisions.json")
    time = decisions["time"]
    if time["repeated_rounded_period_addition"] != "forbidden":
        raise ValidationFailure("rounded period addition must be forbidden")
    if time["maximum_drift_from_reference_fs"] != "0":
        raise ValidationFailure("clock drift contract must be zero fs")

    priorities = load_json(root / "contracts" / "event_priorities.json")
    names: set[str] = set()
    values: set[int] = set()
    for entry in priorities["priorities"]:
        if entry["name"] in names or entry["value"] in values:
            raise ValidationFailure("event priorities must be unique")
        names.add(entry["name"])
        values.add(entry["value"])


def validate_model_and_benchmarks(root: Path) -> None:
    profile = load_json(root / "contracts" / "model_profile.json")
    if profile["precision"] != "BF16" or profile["quantization"] is not None:
        raise ValidationFailure("canonical model must remain unquantized BF16")
    if profile["model_id"] != "mistralai/Mixtral-8x7B-Instruct-v0.1":
        raise ValidationFailure("canonical model identity mismatch")
    if not COMMIT_RE.fullmatch(profile["repository_commit"]):
        raise ValidationFailure("model repository commit must be SHA-1")
    if profile["artifact_identity"]["status"] != "NOT_MATERIALIZED":
        raise ValidationFailure("Phase 0 must not claim materialized model artifacts")
    if profile["formal_use"][:9] != "forbidden":
        raise ValidationFailure("unmaterialized model cannot be used formally")

    matrix = load_json(root / "contracts" / "benchmark_matrix.json")
    if sum(item["core_count"] for item in matrix["benchmarks"]) != 48:
        raise ValidationFailure("benchmark core count must total 48")
    if sum(item["deep_count"] for item in matrix["benchmarks"]) != 12:
        raise ValidationFailure("benchmark deep count must total 12")
    if matrix["sample_manifest"] is not None:
        raise ValidationFailure("Phase 0 must not invent unmaterialized sample IDs")


def parse_ledger(path: Path) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line:
            continue
        parts = line.split("  ", 1)
        if len(parts) != 2 or not HASH_RE.fullmatch(parts[0]):
            raise ValidationFailure(f"invalid checksum ledger line {number}")
        rel = parts[1]
        candidate = Path(rel)
        if candidate.is_absolute() or ".." in candidate.parts or rel in seen:
            raise ValidationFailure(f"unsafe or duplicate ledger path: {rel}")
        seen.add(rel)
        entries.append((parts[0], rel))
    if not entries:
        raise ValidationFailure("checksum ledger is empty")
    return entries


def validate_ledger(root: Path) -> None:
    ledger = root / "governance" / "checksums.sha256"
    for expected, rel in parse_ledger(ledger):
        path = root / rel
        if not path.is_file() or path.is_symlink():
            raise ValidationFailure(f"ledger member missing or symlink: {rel}")
        actual = sha256_file(path)
        if actual != expected:
            raise ValidationFailure(f"checksum mismatch: {rel}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--skip-ledger", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    validate_schema_documents(root)
    validate_architecture(root)
    validate_model_and_benchmarks(root)
    validate_semantic_fixtures(root)
    if not args.skip_ledger:
        validate_ledger(root)
    print("PHASE0_VALIDATION: PASS")
    print(f"jsonschema: {REQUIRED_JSONSCHEMA}")
    print("gpu_used: false")
    print("model_downloaded: false")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValidationFailure as exc:
        print(f"PHASE0_VALIDATION: FAIL: {exc}", file=sys.stderr)
        raise SystemExit(2)

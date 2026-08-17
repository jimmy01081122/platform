#!/usr/bin/env python3
"""Run the CPU-only Phase 1 synthetic runtime/schema/alignment spike."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import stat
import sys
from pathlib import Path
from typing import Any

SIM_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SIM_ROOT.parents[1]
sys.path.insert(0, str(SIM_ROOT / "tools"))

from contract_runtime import (  # noqa: E402
    Clock,
    canonical_bytes,
    dataset_semantic_hash,
    schema_fingerprint,
    transform_alignment,
    validate_alignment,
    validate_events,
    validate_observability,
    validate_routing,
)
from validate_phase0 import (  # noqa: E402
    REQUIRED_JSONSCHEMA,
    ValidationFailure,
    _schema_validators,
    load_json,
)

PHASE0_LEDGER_SHA256 = (
    "4a53c3d2fbbab330151679ea2b831b91b404239b88db7edc616f2900d61544bc"
)
SHAPE_EVENT = {
    "request": "REQUEST_ARRIVAL",
    "tokenization": "RESOURCE_ACQUIRE",
    "allocator": "RESOURCE_ACQUIRE",
    "copy": "TRANSFER_START",
    "kernel": "COMPUTE_START",
    "router": "DEPENDENCY_READY",
    "collective": "TRANSFER_START",
    "p2p": "TRANSFER_START",
    "kernel_complete": "COMPUTE_COMPLETE",
    "telemetry": "TELEMETRY_SAMPLE",
}


def require_unique_ids(records: list[dict[str, Any]], key: str) -> None:
    identifiers = [record[key] for record in records]
    if len(identifiers) != len(set(identifiers)):
        raise ValidationFailure(f"{key} values must be unique")


def load_fixture(path: Path) -> dict[str, Any]:
    if importlib.metadata.version("jsonschema") != REQUIRED_JSONSCHEMA:
        raise ValidationFailure(
            f"jsonschema must be exactly {REQUIRED_JSONSCHEMA}"
        )
    import jsonschema

    value = load_json(path)
    schema = load_json(
        Path(__file__).resolve().parent
        / "schemas"
        / "mock_runtime_trace.schema.json"
    )
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.Draft202012Validator(schema).validate(value)
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def write_jsonl(path: Path, values: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(
                value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            )
            + "\n"
            for value in values
        ),
        encoding="utf-8",
    )


def write_checksum_ledger(output: Path) -> None:
    members: list[Path] = []
    for directory, directory_names, file_names in os.walk(
        output, topdown=True, followlinks=False
    ):
        base = Path(directory)
        for name in [*directory_names, *file_names]:
            path = base / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not (
                stat.S_ISDIR(mode) or stat.S_ISREG(mode)
            ):
                raise RuntimeError(f"run tree contains forbidden entry: {path}")
            if stat.S_ISREG(mode) and path.name != "checksums.sha256":
                members.append(path)
    members.sort()
    lines = [
        f"{sha256_file(path)}  {path.relative_to(output).as_posix()}"
        for path in members
    ]
    (output / "checksums.sha256").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def build_alignments(
    fixture: dict[str, Any],
    raw_sha256: str,
) -> tuple[dict[str, Clock], list[dict[str, Any]], dict[str, dict[str, Any]]]:
    global_record = {
        "schema_version": "clock-domain-v1",
        "clock_id": "global",
        "frequency_numerator_hz": "1000000000000000",
        "frequency_denominator_hz": "1",
        "phase_offset_fs": "0",
        "local_cycle": "0",
        "fractional_remainder": "0",
    }
    global_clock = Clock.from_record(global_record)
    global_hash = hashlib.sha256(canonical_bytes(global_record)).hexdigest()
    shortest_record = {
        "record_id": "phase1-shortest-synthetic-component",
        "duration_fs": "100",
    }
    shortest_hash = hashlib.sha256(canonical_bytes(shortest_record)).hexdigest()
    by_source: dict[str, dict[str, Any]] = {}
    alignments: list[dict[str, Any]] = []
    raw_events = fixture["raw_events"]
    require_unique_ids(fixture["source_clocks"], "clock_id")
    source_ranks = {
        source["clock_id"]: source["rank"] for source in fixture["source_clocks"]
    }
    for event in raw_events:
        source_id = event["source_clock_id"]
        if source_id not in source_ranks:
            raise ValidationFailure(f"event references unknown source clock: {source_id}")
        if event["rank"] != source_ranks[source_id]:
            raise ValidationFailure(
                f"event rank does not match source clock rank: {event['event_id']}"
            )
    for source in fixture["source_clocks"]:
        source_id = source["clock_id"]
        source_times = [
            int(event["source_timestamp"])
            for event in raw_events
            if event["source_clock_id"] == source_id
        ]
        maximum = max(source_times)
        numerator = int(source["scale_numerator"])
        denominator = int(source["scale_denominator"])
        offset = int(source["offset_fs"])

        def mapped(value: int) -> int:
            return value * numerator // denominator + offset

        alignment = {
            "schema_version": "clock-alignment-v1",
            "alignment_id": f"{source_id}-to-global",
            "source_clock_id": source_id,
            "target_clock_id": "global",
            "source_rank": source["rank"],
            "target_rank": None,
            "transform_type": "AFFINE_RATIONAL",
            "scale_numerator": str(numerator),
            "scale_denominator": str(denominator),
            "offset_fs": str(offset),
            "calibration_method": "synthetic-exact-two-anchor",
            "calibration_points": [
                {"source_time": "0", "target_time": str(mapped(0))},
                {"source_time": str(maximum), "target_time": str(mapped(maximum))},
            ],
            "residual_error_fs": "0",
            "confidence_interval_95_fs": {
                "lower_error_fs": "0",
                "upper_error_fs": "0",
            },
            "valid_time_range": {
                "source_start": "0",
                "source_end": str(maximum),
            },
            "drift_bound_ppm": "0",
            "grading_inputs": {
                "target_clock_profile_hash": global_hash,
                "target_period_numerator_fs": "1",
                "target_period_denominator": "1",
                "shortest_component_record_hash": shortest_hash,
                "shortest_component_duration_fs": "100",
            },
            "provenance": {
                "producer": "phase1-cpu-mock-alignment",
                "producer_version": "1",
                "source_content_ids": [raw_sha256],
            },
            "claimed_grade": "CYCLE_GRADE",
        }
        validate_alignment(alignment)
        alignments.append(alignment)
        by_source[source_id] = alignment
    require_unique_ids(alignments, "alignment_id")
    return {"global": global_clock}, alignments, by_source


def canonicalize(
    fixture_path: Path,
) -> tuple[
    dict[str, Any],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    fixture = load_fixture(fixture_path)
    raw_sha256 = sha256_file(fixture_path)
    clocks, alignments, by_source = build_alignments(fixture, raw_sha256)
    validators = _schema_validators(SIM_ROOT)
    for alignment in alignments:
        validators["clock_alignment.schema.json"].validate(alignment)
    priorities_contract = load_json(
        SIM_ROOT / "contracts" / "event_priorities.json"
    )
    priorities = {
        item["name"]: item["value"] for item in priorities_contract["priorities"]
    }
    descriptors = load_json(
        SIM_ROOT / "contracts" / "semantic_descriptors.json"
    )["descriptors"]
    event_descriptor_hash = schema_fingerprint(descriptors["event-ir-v1"])
    routing_descriptor_hash = schema_fingerprint(descriptors["routing-ir-v1"])
    events: list[dict[str, Any]] = []
    for raw in fixture["raw_events"]:
        event_type = SHAPE_EVENT[raw["shape"]]
        aligned_time = transform_alignment(
            by_source[raw["source_clock_id"]],
            int(raw["source_timestamp"]),
        )
        event = {
            "schema_version": "event-ir-v1",
            "trace_id": fixture["fixture_id"],
            "event_id": raw["event_id"],
            "event_type": event_type,
            "time_fs": str(aligned_time),
            "event_priority": priorities[event_type],
            "request_id": raw["request_id"],
            "token_index": raw["token_index"],
            "layer_index": raw["layer_index"],
            "component_id": raw["source_clock_id"],
            "clock_id": "global",
            "rank": raw["rank"],
            "stream_id": raw["stream_id"],
            "correlation_id": raw["correlation_id"],
            "semantic_descriptor_hash": event_descriptor_hash,
            "duration_fs": None,
            "dependencies": raw["dependencies"],
            "observability": {
                "schema_version": "observability-v1",
                "availability": "CONFIRMED",
                "evidence_mode": "INSTRUMENTED",
            },
            "provenance": {
                "producer": "phase1-mock-vllm-shape-adapter",
                "producer_version": "1",
                "raw_content_ids": [raw_sha256],
            },
            "attributes": {
                **raw["attributes"],
                "mock_shape": raw["shape"],
                "source_clock_id": raw["source_clock_id"],
                "source_timestamp": raw["source_timestamp"],
                "evidence_class": fixture["evidence_class"],
            },
        }
        validators["event_ir.schema.json"].validate(event)
        validate_observability(event["observability"])
        events.append(event)
    ordered_events = validate_events(events, clocks, priorities)
    routing = {
        "schema_version": "routing-ir-v1",
        **fixture["routing"],
        "observability": {
            "schema_version": "observability-v1",
            "availability": "CONFIRMED",
            "evidence_mode": "INSTRUMENTED",
        },
        "model_profile_hash": sha256_file(
            SIM_ROOT / "contracts" / "model_profile.json"
        ),
        "semantic_descriptor_hash": routing_descriptor_hash,
    }
    validators["routing_ir.schema.json"].validate(routing)
    validate_routing(routing)
    event_rows, event_root = dataset_semantic_hash(
        ordered_events, descriptors["event-ir-v1"]
    )
    routing_rows, routing_root = dataset_semantic_hash(
        [routing], descriptors["routing-ir-v1"]
    )
    hashes = {
        "raw_sha256": raw_sha256,
        "event_file_semantic_rows": event_rows,
        "event_semantic_root": event_root,
        "routing_semantic_rows": routing_rows,
        "routing_semantic_root": routing_root,
    }
    return hashes, alignments, ordered_events, [routing]


def run(fixture: Path, output: Path, command: list[str]) -> None:
    if output.exists():
        raise RuntimeError(f"fresh Phase 1 run directory already exists: {output}")
    # All dependency, schema, fixture, clock, rank and semantic preflight occurs
    # before any run-directory entry is created.
    hashes, alignments, events, routing = canonicalize(fixture)
    artifacts = output / "artifacts"
    logs = output / "logs"
    environment = output / "environment"
    artifacts.mkdir(parents=True)
    logs.mkdir(parents=True)
    environment.mkdir(parents=True)
    shutil.copyfile(fixture, artifacts / "raw_mock_runtime_trace.json")
    write_json(artifacts / "clock_alignments.json", {"alignments": alignments})
    write_jsonl(artifacts / "event_ir.jsonl", events)
    write_jsonl(artifacts / "routing_ir.jsonl", routing)
    write_json(
        artifacts / "v0_audit.json",
        {
            "schema_version": "moe-simulator-v0-audit-v1",
            "status": "PASS",
            "findings": [],
            "execution_role": "OFFLINE_VALIDATION",
            "gpu_execution": False,
            "runtime_pass": False,
            "hashes": hashes,
        },
    )
    run_id = output.name
    write_json(
        output / "manifest.json",
        {
            "run_id": run_id,
            "experiment_id": "moe_cycle_simulator_phase1_cpu_mock",
            "stage": "S1",
            "platform_profile": "cpu_mock_multiclock_multirank",
            "status": "passed",
            "phase0_ledger_sha256": PHASE0_LEDGER_SHA256,
            "command": command,
            "gpu_used": False,
            "model_downloaded": False,
            "evidence_class": "synthetic-cpu-mock",
        },
    )
    write_json(
        output / "resolved_config.yaml",
        {
            "note": "JSON content with YAML-compatible syntax",
            "fixture": str(fixture),
            "phase0_ledger_sha256": PHASE0_LEDGER_SHA256,
        },
    )
    required_shapes = sorted(SHAPE_EVENT)
    observed_shapes = sorted(
        {event["attributes"]["mock_shape"] for event in events}
    )
    write_json(
        output / "metrics.json",
        {
            "status": "PASS",
            "event_count": len(events),
            "routing_record_count": len(routing),
            "alignment_count": len(alignments),
            "source_rank_count": len(
                {
                    item["source_rank"]
                    for item in alignments
                    if item["source_rank"] is not None
                }
            ),
            "required_shapes": required_shapes,
            "observed_shapes": observed_shapes,
            "all_required_shapes_present": required_shapes == observed_shapes,
            "v0_findings": 0,
            "gpu_used": False,
        },
    )
    write_json(
        environment / "tool_versions.json",
        {
            "python": platform.python_version(),
            "python_executable": sys.executable,
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "jsonschema": importlib.metadata.version("jsonschema"),
            "phase1_tool_sha256": sha256_file(Path(__file__).resolve()),
            "phase1_fixture_sha256": sha256_file(fixture),
            "phase1_schema_sha256": sha256_file(
                Path(__file__).resolve().parent
                / "schemas"
                / "mock_runtime_trace.schema.json"
            ),
            "phase0_ledger_sha256": PHASE0_LEDGER_SHA256,
        },
    )
    (logs / "command.log").write_text(" ".join(command) + "\n", encoding="utf-8")
    (logs / "stdout.log").write_text(
        "PHASE1_CPU_MOCK_SPIKE: PASS\nGPU_USED: false\n",
        encoding="utf-8",
    )
    (logs / "stderr.log").write_text("", encoding="utf-8")
    write_checksum_ledger(output)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--fixture",
        type=Path,
        default=Path(__file__).resolve().parent / "fixtures" / "mock_runtime_trace.json",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    command = [sys.executable, *sys.argv]
    run(args.fixture.resolve(), args.output.resolve(), command)
    print(f"PHASE1_CPU_MOCK_SPIKE: PASS: {args.output}")
    print("GPU_USED: false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

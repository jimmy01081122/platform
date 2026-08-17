"""Build the minimal complete v2 package used by positive and negative tests."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

PASSES = {
    "P0": "p0_baseline",
    "P1": "p1_timeline",
    "P2": "p2_routing",
    "P3": "p3_memory_transfer",
    "P4": "p4_gpu_counters",
    "P5": "p5_telemetry",
    "P6": "p6_detailed_optional",
}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def dump(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def refresh_checksums(root: Path) -> None:
    result_manifest = root / "RESULT_PACKAGE_MANIFEST.json"
    if result_manifest.is_file():
        excluded = {
            "RESULT_PACKAGE_MANIFEST.json",
            "checksums.sha256",
            "TRACE_COMPLETENESS_REPORT.json",
        }
        manifest = json.loads(result_manifest.read_text())
        files = [
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": digest(path),
                "bytes": path.stat().st_size,
            }
            for path in sorted(root.rglob("*"))
            if path.is_file()
            and path.relative_to(root).as_posix() not in excluded
        ]
        manifest["file_coverage"] = {
            "excluded": sorted(excluded),
            "file_count": len(files),
            "files": files,
        }
        dump(result_manifest, manifest)
    selected = [
        path for path in root.rglob("*")
        if path.is_file()
        and path.name not in ("checksums.sha256", "TRACE_COMPLETENESS_REPORT.json")
    ]
    (root / "checksums.sha256").write_text("".join(
        f"{digest(path)}  {path.relative_to(root).as_posix()}\n"
        for path in sorted(selected)
    ))


def build_positive(root: Path, required_repetitions: int = 3) -> Path:
    environment = root / "environment/environment.json"
    workload = root / "workloads/workload_manifest.json"
    configuration = root / "models/configs/config.json"
    dump(environment, {"gpu": "fixture-gpu", "driver": "fixture-driver"})
    dump(workload, {"workload_id": "fixture-workload", "seed": 7})
    dump(configuration, {"model": "fixture-model", "batch_size": 1})
    base_identity = {
        "session_id": "fixture-session",
        "model_revision": "fixture-revision",
        "workload_hash": digest(workload),
        "configuration_hash": digest(configuration),
        "environment_hash": digest(environment),
    }
    entries = []
    conversions = []
    raw_by_pass = {}
    converter_hash = hashlib.sha256(b"fixture-converter-v1").hexdigest()
    canonical_schema_path = root / "provenance/schemas/fixture-canonical.schema.json"
    dump(canonical_schema_path, {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["pass_id", "repetition_index", "source", "source_content_ids",
                     "records"],
        "properties": {
            "pass_id": {"type": "string"},
            "repetition_index": {"type": "integer"},
            "source": {"type": "string"},
            "source_content_ids": {"type": "array"},
            "records": {"type": "array"},
        },
        "additionalProperties": False,
    })
    for pass_id in PASSES:
        for repetition in range(required_repetitions):
            run_id = f"fixture-run-{pass_id.lower()}-r{repetition}"
            payload = f"native trace for {pass_id} repetition {repetition}\n".encode()
            content_id = hashlib.sha256(payload).hexdigest()
            raw_path = (
                root / "raw_traces/sha256" / content_id[:2]
                / f"{content_id}.raw"
            )
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_bytes(payload)
            canonical_path = root / "canonical_traces" / f"{content_id}.json"
            dump(canonical_path, {
                "pass_id": pass_id,
                "repetition_index": repetition,
                "source": content_id,
                "source_content_ids": [content_id],
                "records": [],
            })
            entries.append({
                "content_id": content_id,
                "sha256": content_id,
                "bytes": len(payload),
                "path": raw_path.relative_to(root).as_posix(),
                "source_name": f"{run_id}.raw",
                "native_format": "fixture-native-v1",
                "pass_id": pass_id,
                "run_id": run_id,
                "captured_utc": "2026-07-18T00:00:00Z",
                "immutable": True,
                "truncated": False,
            })
            conversions.append({
                "canonical_path": canonical_path.relative_to(root).as_posix(),
                "canonical_sha256": digest(canonical_path),
                "input_content_ids": [content_id],
                "canonical_schema": {
                    "schema_id": "fixture-canonical.schema.json",
                    "path": canonical_schema_path.relative_to(root).as_posix(),
                    "sha256": digest(canonical_schema_path),
                },
                "converter": {
                    "name": "fixture-converter",
                    "version": "1",
                    "source_hash": converter_hash,
                },
                "converted_utc": "2026-07-18T00:00:01Z",
            })
            raw_by_pass[(pass_id, repetition)] = entries[-1]
    inventory_path = root / "raw_traces/RAW_INVENTORY.json"
    dump(inventory_path, {
        "schema_version": "trace-raw-inventory-v2",
        "digest_algorithm": "sha256",
        "content_addressed": True,
        "immutable": True,
        "canonical_contract": {
            "schema_id": "fixture-canonical.schema.json",
            "schema_path": canonical_schema_path.relative_to(root).as_posix(),
            "schema_sha256": digest(canonical_schema_path),
            "record_order": "zero-based array order",
            "source_id_policy": "exact conversion inputs",
        },
        "entries": entries,
        "conversions": conversions,
    })
    run_group = "fixture-group"
    for pass_id, directory in PASSES.items():
        for repetition in range(required_repetitions):
            raw = raw_by_pass[(pass_id, repetition)]
            run_id = f"fixture-run-{pass_id.lower()}-r{repetition}"
            identity = {
                **base_identity,
                "run_group_id": run_group,
                "run_id": run_id,
                "repetition_index": repetition,
                "profiler_pass": pass_id,
            }
            dump(
                root / "runs" / run_group / directory / "runs" / run_id
                / "PASS_MANIFEST.json",
                {
                    "schema_version": "trace-pass-manifest-v2",
                    "pass_id": pass_id,
                    "status": "complete",
                    "failure_reason": None,
                    "identity": identity,
                    "clock": {
                        "status": "observed",
                        "wall_clock_utc": "2026-07-18T00:00:00Z",
                        "monotonic_host_ns": 1000 + repetition,
                        "profiler_domain": "fixture_gpu_clock",
                        "timezone": "UTC",
                        "alignment": {
                            "method": "fixture NVTX anchor",
                            "max_error_ns": 10,
                            "anchors": [{
                                "wall_clock_unix_ns": 1784332800000000000,
                                "monotonic_host_ns": 1000 + repetition,
                                "profiler_timestamp": 500 + repetition,
                            }],
                        },
                    },
                    "raw_observation": {
                        "environment": {
                            "status": "observed",
                            "source_content_ids": [raw["content_id"]],
                            "value": {"gpu": "fixture-gpu"},
                        },
                        "gpu_uuid": {
                            "status": "observed",
                            "source_content_ids": [raw["content_id"]],
                            "value": "GPU-fixture",
                        },
                        "runtime": {
                            "status": "observed",
                            "source_content_ids": [raw["content_id"]],
                            "value": {"runtime": "fixture"},
                        },
                        "start_utc": {
                            "status": "observed",
                            "source_content_ids": [raw["content_id"]],
                            "value": "2026-07-18T00:00:00Z",
                        },
                        "end_utc": {
                            "status": "observed",
                            "source_content_ids": [raw["content_id"]],
                            "value": "2026-07-18T00:00:01Z",
                        },
                    },
                    "raw_artifacts": [{
                        "content_id": raw["content_id"],
                        "path": raw["path"],
                        "sha256": raw["sha256"],
                        "bytes": raw["bytes"],
                    }],
                    "converter_provenance": {
                        "name": "fixture-converter",
                        "version": "1",
                        "source_hash": converter_hash,
                        "input_content_ids": [raw["content_id"]],
                    },
                    "rerun_command": (
                        f"./run.sh --run-group {run_group} "
                        f"--profiler-pass {pass_id.lower()} --resume"
                    ),
                },
            )
    matrix_path = root / "provenance/capture/frozen_matrix.json"
    matrix_value = {
        "schema_version": "benchmark-capture-matrix-v1",
        "frozen": True,
        "session_id": "fixture-session",
        "repetitions": required_repetitions,
        "benchmarks": [{
            "benchmark_id": "fixture",
            "samples": [
                {"sample_id": f"fixture-sample-{index}"} for index in range(8)
            ],
        }],
    }
    dump(matrix_path, matrix_value)
    capture_plan_path = root / "provenance/capture/CAPTURE_PLAN.json"
    dump(capture_plan_path, {
        "schema_version": "benchmark-capture-plan-v1",
        "frozen_matrix_hash": canonical_hash(matrix_value),
        "profiler_concurrency": 1,
        "simultaneous_profilers_forbidden": True,
        "states": [{
            "state_id": "fixture-p4-binding",
            "pass_id": "P4",
            "status": "blocked",
            "blocked_reason": "fixture P4 adapter not installed",
            "command": "python3 collectors/gpu_counters.py --profiler-pass P4",
            "estimate_minutes": 1,
        }],
    })
    dump(root / "SESSION_MANIFEST.json", {
        "schema_version": "trace-session-manifest-v2",
        "release_class": "pipeline_smoke",
        "identity": base_identity,
        "required_repetitions": required_repetitions,
        "frozen_matrix": {
            "schema_version": "benchmark-capture-matrix-v1",
            "matrix_hash": canonical_hash(matrix_value),
            "path": matrix_path.relative_to(root).as_posix(),
        },
        "capture_plan": {
            "path": capture_plan_path.relative_to(root).as_posix(),
            "sha256": digest(capture_plan_path),
        },
        "expected_runs": [{
            "run_group_id": run_group,
            "model_revision": base_identity["model_revision"],
            "workload_hash": base_identity["workload_hash"],
            "configuration_hash": base_identity["configuration_hash"],
            "environment_hash": base_identity["environment_hash"],
            "planned_passes": list(PASSES),
        }],
        "accepted_incomplete": False,
        "artifacts": {
            "environment": environment.relative_to(root).as_posix(),
            "workload": workload.relative_to(root).as_posix(),
            "configuration": configuration.relative_to(root).as_posix(),
            "raw_inventory": inventory_path.relative_to(root).as_posix(),
        },
    })
    excluded = {
        "RESULT_PACKAGE_MANIFEST.json",
        "checksums.sha256",
        "TRACE_COMPLETENESS_REPORT.json",
    }
    payload = [
        {
            "path": path.relative_to(root).as_posix(),
            "sha256": digest(path),
            "bytes": path.stat().st_size,
        }
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.relative_to(root).as_posix() not in excluded
    ]
    dump(root / "RESULT_PACKAGE_MANIFEST.json", {
        "schema_version": "gpu-result-archive-v2",
        "release_class": "pipeline_smoke",
        "release_eligible": False,
        "created_utc": "2026-07-18T00:00:00Z",
        "session_root_name": root.name,
        "source_verification_exit_code": 0,
        "source_verification_status": "complete",
        "measurement_claim_inferred_by_packager": False,
        "file_coverage": {
            "excluded": sorted(excluded),
            "file_count": len(payload),
            "files": payload,
        },
    })
    refresh_checksums(root)
    return root


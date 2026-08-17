#!/usr/bin/env python3
"""Build and execute the offline CPU-only T0 MoE vertical slice."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(PACKAGE_ROOT / "scripts"))

from collectors.trace_contract import PASSES, sha256_file, write_json  # noqa: E402
from canonicalize_trace import ADAPTER, canonicalize  # noqa: E402
from system_simulate import simulate  # noqa: E402
from workload_expand import expand  # noqa: E402

FIXED_UTC = "2026-07-18T00:00:00Z"


def canonical_digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def raw_event(identity: dict[str, Any], pass_id: str, sequence: int,
              event_type: str, evidence_class: str = "fixture_ground_truth",
              **fields: Any) -> dict[str, Any]:
    return {
        "schema_version": "t0-fixture-native-v1",
        **identity,
        "pass_id": pass_id,
        "sequence": sequence,
        "event_id": f"{pass_id.lower()}-{sequence:03d}-{event_type}",
        "event_type": event_type,
        "timestamp_ns": sequence * 10,
        "evidence": {
            "class": evidence_class,
            "source_pass": pass_id,
            "synthetic": True,
            "measurement_claim": False,
        },
        **fields,
    }


def fixture_events(fixture: dict[str, Any], identity: dict[str, Any]) -> dict[str, list[dict]]:
    routing = [
        raw_event(identity, "P2", index, "routing_decision",
                  token_index=item["token_index"], token_id=item["token_id"],
                  experts=item["experts"], weights=item["weights"], top_k=2)
        for index, item in enumerate(fixture["routing"])
    ]
    return {
        "P0": [
            raw_event(identity, "P0", 0, "baseline_start", execution_device="CPU"),
            raw_event(identity, "P0", 1, "expected_result",
                      generated_tokens=fixture["expected_generated_tokens"], matched=True),
            raw_event(identity, "P0", 2, "baseline_end", return_code=0),
        ],
        "P1": [
            raw_event(identity, "P1", 0, "timeline_anchor", clock="fixture_logical_ns"),
            raw_event(identity, "P1", 1, "queue_marker", resource="fixture_dma0", depth=2),
            raw_event(identity, "P1", 2, "interference_note", "interference",
                      interference=True, label="deliberate_fixture_background_noise"),
        ],
        "P2": routing,
        "P3": [
            raw_event(identity, "P3", 0, "dma_marker", direction="H2D", expert_id=0, bytes=4096),
            raw_event(identity, "P3", 1, "dma_marker", direction="H2D", expert_id=1, bytes=4096),
            raw_event(identity, "P3", 2, "dma_marker", direction="H2D", expert_id=2, bytes=4096),
            raw_event(identity, "P3", 3, "residency_marker", resident_experts=[0, 1, 2]),
            raw_event(identity, "P3", 4, "interference_note", "interference",
                      interference=True, label="synthetic_dma_contention"),
        ],
        "P4": [
            raw_event(identity, "P4", 0, "counter_unavailable_marker", "synthetic_marker",
                      counters_available=False, reason="CPU-only fixture; no GPU counter queried"),
        ],
        "P5": [
            raw_event(identity, "P5", 0, "telemetry_sample",
                      telemetry_kind="fixture_constant", cpu_load_fraction=0.25,
                      units="synthetic_fraction"),
        ],
        "P6": [
            raw_event(identity, "P6", 0, "rtl_reference_event",
                      reference_only=True, rtl_executed=False,
                      expected_event="expert_dispatch_then_combine"),
        ],
    }


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
                "sha256": sha256_file(path),
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
        write_json(result_manifest, manifest)
    selected = [
        path for path in root.rglob("*")
        if path.is_file() and path.name not in ("checksums.sha256", "TRACE_COMPLETENESS_REPORT.json")
    ]
    (root / "checksums.sha256").write_text(
        "".join(f"{sha256_file(path)}  {path.relative_to(root).as_posix()}\n"
                for path in sorted(selected)),
        encoding="utf-8",
    )


def run(fixture_path: Path, output_root: Path, replace: bool = False) -> dict[str, Any]:
    if output_root.exists():
        if not replace:
            raise FileExistsError(f"output exists: {output_root}")
        shutil.rmtree(output_root)
    output_root.mkdir(parents=True)
    canonical_schema_path = (
        output_root / "provenance/schemas/canonical_moe_ir.schema.json"
    )
    canonical_schema_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        PACKAGE_ROOT / "schemas/canonical_moe_ir.schema.json",
        canonical_schema_path,
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    prompt_hash = hashlib.sha256(fixture["prompt"].encode("utf-8")).hexdigest()
    identity = {
        "fixture_id": fixture["fixture_id"],
        "seed": fixture["seed"],
        "prompt_sha256": prompt_hash,
        "token_sequence": fixture["token_sequence"],
    }
    write_json(output_root / "workloads/workload_manifest.json", {
        "schema_version": "t0-workload-v1",
        **identity,
        "prompt": fixture["prompt"],
        "expected_generated_tokens": fixture["expected_generated_tokens"],
    })
    write_json(output_root / "models/configs/configuration.json", fixture["model_profile"])
    write_json(output_root / "environment/environment.json", {
        "schema_version": "t0-environment-v1",
        "execution_device": "CPU",
        "platform_profile": fixture["platform_profile"],
        "gpu_queried": False,
        "measurement_claim": False,
    })

    artifacts = []
    raw_by_pass: dict[str, dict[str, Any]] = {}
    for pass_id, events in fixture_events(fixture, identity).items():
        raw_path = output_root / "raw_traces/native" / f"{pass_id.lower()}.jsonl"
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(
            "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
            encoding="utf-8",
        )
        artifact = {
            "pass_id": pass_id,
            "path": raw_path.relative_to(output_root).as_posix(),
            "sha256": sha256_file(raw_path),
            "bytes": raw_path.stat().st_size,
            "native_format": "t0-fixture-native-v1-jsonl",
        }
        artifacts.append(artifact)
        raw_by_pass[pass_id] = artifact
    capture_manifest = output_root / "T0_CAPTURE_MANIFEST.json"
    write_json(capture_manifest, {
        "schema_version": "t0-capture-manifest-v1",
        "identity": identity,
        "fixture_spec_sha256": sha256_file(fixture_path),
        "adapter": ADAPTER,
        "source_format": "t0-fixture-native-v1",
        "artifacts": artifacts,
    })

    routing_path = output_root / "canonical_traces/moe_routing.json"
    observed_path = output_root / "canonical_traces/observed_system_markers.json"
    canonicalize(capture_manifest, routing_path, observed_path)
    expanded_path = output_root / "canonical_traces/expanded_system_events.json"
    expand(fixture_path, routing_path, expanded_path)
    simulation_path = output_root / "derived_metrics/t0_simulation.json"
    result = simulate(expanded_path, simulation_path)

    expected = fixture["expected"]
    for key, value in expected.items():
        actual_key = "dma_backpressure_events" if key == "backpressure_events" else key
        if result["summary"].get(actual_key) != value:
            raise AssertionError(
                f"deterministic expectation failed for {key}: "
                f"{result['summary'].get(actual_key)!r} != {value!r}"
            )
    if result["summary"]["generated_tokens"] != fixture["expected_generated_tokens"]:
        raise AssertionError("generated token sequence differs from fixture expectation")

    workload = output_root / "workloads/workload_manifest.json"
    configuration = output_root / "models/configs/configuration.json"
    environment = output_root / "environment/environment.json"
    base_identity = {
        "session_id": "t0-fixture-session-20260718",
        "model_revision": fixture["model_profile"]["revision"],
        "workload_hash": sha256_file(workload),
        "configuration_hash": sha256_file(configuration),
        "environment_hash": sha256_file(environment),
    }
    run_group = "t0-fixture-group"
    inventory_entries = []
    for pass_id, artifact in raw_by_pass.items():
        content_id = artifact["sha256"]
        inventory_entries.append({
            "content_id": content_id,
            "sha256": content_id,
            "bytes": artifact["bytes"],
            "path": artifact["path"],
            "source_name": Path(artifact["path"]).name,
            "native_format": artifact["native_format"],
            "pass_id": pass_id,
            "run_id": f"t0-{pass_id.lower()}-r0",
            "capture_time_status": "known_limitation",
            "capture_time_limitation": (
                "deterministic CPU fixture has logical timestamps only"
            ),
            "immutable": True,
            "truncated": False,
        })
        pass_identity = {
            **base_identity,
            "run_group_id": run_group,
            "run_id": f"t0-{pass_id.lower()}-r0",
            "repetition_index": 0,
            "profiler_pass": pass_id,
        }
        write_json(
            output_root / "runs" / run_group / PASSES[pass_id] / "runs"
            / pass_identity["run_id"] / "PASS_MANIFEST.json",
            {
                "schema_version": "trace-pass-manifest-v2",
                "pass_id": pass_id,
                "status": "complete",
                "failure_reason": None,
                "identity": pass_identity,
                "clock": {
                    "status": "known_limitation",
                    "reason": (
                        "raw fixture records logical timestamp_ns only; no wall clock "
                        "or host monotonic capture clock exists"
                    ),
                    "source_content_ids": [content_id],
                },
                "raw_observation": {
                    "environment": {
                        "status": "observed",
                        "source_content_ids": [content_id],
                        "value": {
                            "execution_device": "CPU",
                            "measurement_claim": False,
                        },
                    },
                    "gpu_uuid": {
                        "status": "not_applicable",
                        "source_content_ids": [content_id],
                        "reason": "CPU-only fixture does not query a GPU",
                    },
                    "runtime": {
                        "status": "known_limitation",
                        "source_content_ids": [content_id],
                        "reason": "fixture raw event does not encode runtime versions",
                    },
                    "start_utc": {
                        "status": "known_limitation",
                        "source_content_ids": [content_id],
                        "reason": "fixture raw event has no UTC timestamp",
                    },
                    "end_utc": {
                        "status": "known_limitation",
                        "source_content_ids": [content_id],
                        "reason": "fixture raw event has no UTC timestamp",
                    },
                },
                "raw_artifacts": [{
                    "content_id": content_id,
                    "path": artifact["path"],
                    "sha256": content_id,
                    "bytes": artifact["bytes"],
                }],
                "converter_provenance": {
                    "name": "canonicalize_trace.py",
                    "version": "t0-adapter-v1",
                    "source_hash": sha256_file(PACKAGE_ROOT / "scripts/canonicalize_trace.py"),
                    "input_content_ids": [content_id],
                },
                "rerun_command": (
                    "python3 scripts/t0_fixture_runner.py --replace "
                    f"--output {output_root.as_posix()}"
                ),
            },
        )
    all_content_ids = sorted(item["content_id"] for item in inventory_entries)
    write_json(output_root / "raw_traces/RAW_INVENTORY.json", {
        "schema_version": "trace-raw-inventory-v2",
        "digest_algorithm": "sha256",
        "content_addressed": True,
        "immutable": True,
        "canonical_contract": {
            "schema_id": "canonical_moe_ir.schema.json",
            "schema_path": canonical_schema_path.relative_to(output_root).as_posix(),
            "schema_sha256": sha256_file(canonical_schema_path),
            "record_order": "events.sequence must equal zero-based array order",
            "source_id_policy": (
                "provenance.source_content_ids must exactly equal conversion inputs"
            ),
        },
        "entries": inventory_entries,
        "conversions": [{
            "canonical_path": routing_path.relative_to(output_root).as_posix(),
            "canonical_sha256": sha256_file(routing_path),
            "input_content_ids": all_content_ids,
            "canonical_schema": {
                "schema_id": "canonical_moe_ir.schema.json",
                "path": canonical_schema_path.relative_to(output_root).as_posix(),
                "sha256": sha256_file(canonical_schema_path),
            },
            "converter": {
                "name": "canonicalize_trace.py",
                "version": "t0-adapter-v1",
                "source_hash": sha256_file(PACKAGE_ROOT / "scripts/canonicalize_trace.py"),
            },
            "converted_utc": FIXED_UTC,
        }],
    })
    matrix_path = output_root / "provenance/capture/frozen_matrix.json"
    matrix_value = {
        "schema_version": "benchmark-capture-matrix-v1",
        "frozen": True,
        "session_id": base_identity["session_id"],
        "repetitions": 1,
        "benchmarks": [{
            "benchmark_id": "t0-fixture",
            "samples": [
                {"sample_id": f"t0-fixture-{index}"} for index in range(8)
            ],
        }],
    }
    write_json(matrix_path, matrix_value)
    capture_plan_path = output_root / "provenance/capture/CAPTURE_PLAN.json"
    write_json(capture_plan_path, {
        "schema_version": "benchmark-capture-plan-v1",
        "frozen_matrix_hash": canonical_digest(matrix_value),
        "profiler_concurrency": 1,
        "simultaneous_profilers_forbidden": True,
        "states": [{
            "state_id": "t0-p4-fixture-binding",
            "pass_id": "P4",
            "status": "blocked",
            "blocked_reason": "CPU fixture has no GPU-counter collector adapter",
            "command": "python3 collectors/gpu_counters.py --profiler-pass P4",
            "estimate_minutes": 1,
        }],
    })
    write_json(output_root / "SESSION_MANIFEST.json", {
        "schema_version": "trace-session-manifest-v2",
        "release_class": "pipeline_smoke",
        "identity": base_identity,
        "required_repetitions": 1,
        "frozen_matrix": {
            "schema_version": "benchmark-capture-matrix-v1",
            "matrix_hash": canonical_digest(matrix_value),
            "path": matrix_path.relative_to(output_root).as_posix(),
        },
        "capture_plan": {
            "path": capture_plan_path.relative_to(output_root).as_posix(),
            "sha256": sha256_file(capture_plan_path),
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
            "environment": environment.relative_to(output_root).as_posix(),
            "workload": workload.relative_to(output_root).as_posix(),
            "configuration": configuration.relative_to(output_root).as_posix(),
            "raw_inventory": "raw_traces/RAW_INVENTORY.json",
            "t0_capture_manifest": "T0_CAPTURE_MANIFEST.json",
            "simulation": simulation_path.relative_to(output_root).as_posix(),
        },
        "t0_scope": {
            "cpu_only": True,
            "synthetic_timing": True,
            "gpu_counters_claimed": False,
            "integration_adapter": ADAPTER,
        },
    })
    write_json(output_root / "TRACE_CAPTURE_PLAN.yaml", {
        "schema_version": "trace-capture-plan-v2",
        "profile": "t0_fixture_vertical_slice",
        "passes": list(PASSES),
        "native_raw_per_pass": True,
        "hardware_measurement": False,
    })
    write_json(output_root / "RESULT_PACKAGE_MANIFEST.json", {
        "schema_version": "gpu-result-archive-v2",
        "release_class": "pipeline_smoke",
        "release_eligible": False,
        "created_utc": "2026-07-18T00:00:00Z",
        "session_root_name": output_root.name,
        "source_verification_exit_code": 0,
        "source_verification_status": "complete",
        "measurement_claim_inferred_by_packager": False,
        "file_coverage": {
            "excluded": [
                "RESULT_PACKAGE_MANIFEST.json",
                "checksums.sha256",
                "TRACE_COMPLETENESS_REPORT.json",
            ],
            "file_count": 0,
            "files": [],
        },
    })
    refresh_checksums(output_root)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path,
                        default=PACKAGE_ROOT / "fixtures/t0/t0_fixture.json")
    parser.add_argument("--output", type=Path,
                        default=PACKAGE_ROOT / "artifacts/t0")
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    result = run(args.fixture.resolve(), args.output.resolve(), args.replace)
    print(json.dumps(result["summary"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

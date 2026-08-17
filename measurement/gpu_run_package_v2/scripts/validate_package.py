#!/usr/bin/env python3
"""Offline syntax, schema-shape, checksum, and dry-run validation."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

REQUIRED = (
    "README_RUN.md", "PRE_FLIGHT_CHECKLIST.md", "run.sh", "preflight.sh",
    "collect_environment.sh", "requirements.lock", "Dockerfile",
    "configs/splits.yaml", "configs/benchmark_matrix.yaml",
    "configs/gpu_profiles.yaml", "configs/model_registry.yaml",
    "configs/model_compatibility.yaml", "configs/hardware_schedule.yaml",
    "configs/storage_budget.yaml", "configs/gpu_execution_review.yaml",
    "configs/gpu_entrypoints_v1.json",
    "workloads/windows.json", "scripts/benchmark.py",
    "scripts/extract_workloads.py", "scripts/package_results.py",
    "scripts/session_plan.py", "scripts/session_identity.py",
    "scripts/compatibility_plan.py", "scripts/trace_audit.py",
    "scripts/trace_export.py", "scripts/trace_package_verify.py",
    "scripts/colab_entry.py", "scripts/freeze_benchmark_suite.py",
    "scripts/generate_sample_manifest.py", "scripts/benchmark_snapshot_inventory.py",
    "scripts/capture_orchestrator.py", "scripts/executable_moe_benchmark.py",
    "scripts/ingest_benchmark_session.py", "scripts/canonicalize_trace.py",
    "scripts/workload_expand.py", "scripts/system_simulate.py",
    "scripts/score_validation_mape.py",
    "scripts/review_gate.py",
    "projectctl", "scripts/projectctl.py", "scripts/c1_worker.py",
    "scripts/c1_canonicalize.py", "scripts/c1_evaluator.py",
    "scripts/c1_quality.py", "scripts/c1_system_ir.py",
    "scripts/c1_trace_audit.py", "scripts/c1_cleanroom_verify.py",
    "scripts/g25_qualification.py",
    "scripts/g25_application_runner.py", "scripts/g25_isolated_bootstrap.py",
    "scripts/g25_worker.py",
    "scheduler/g25_application.py", "scheduler/g25_deadlines.py",
    "scheduler/g25_runtime_closure.py", "scheduler/g25_session.py",
    "scheduler/g25_snapshot.py", "scheduler/g25_worker_lifetime.py",
    "scheduler/gpu_entrypoint_policy.py",
    "scheduler/g25_historical_evidence.py",
    "adapters/models/granite_moe/snapshot.py",
    "collectors/__init__.py", "collectors/trace_contract.py",
    "collectors/pass_manifest.py",
    "docs/DECISION_RECORD_V2.md", "docs/GPU_EXPERIMENT_PLAN_V2.md",
    "docs/TRACE_ACQUISITION_RUNBOOK.md",
    "schemas/session_manifest.schema.json",
    "schemas/pass_manifest.schema.json",
    "schemas/benchmark_trace_record.schema.json",
    "schemas/canonical_moe_ir.schema.json",
    "schemas/canonical_pass_ir.schema.json",
    "schemas/gpu_execution_approval.schema.json",
    "schemas/result_package_manifest.schema.json",
    "schemas/validation_mape_report.schema.json",
    "schemas/quality_release_report.schema.json",
    "schemas/c1_benchmark.schema.json", "schemas/c1_pass.schema.json",
    "schemas/c1_quality.schema.json", "schemas/c1_routing.schema.json",
    "schemas/g25_workload_qualification.schema.json",
    "schemas/g25_qualification_audit.schema.json",
    "schemas/g25_qualification_cell.schema.json",
    "schemas/g25_qualification_ledger.schema.json",
    "schemas/g25_qualification_session.schema.json",
    "schemas/g25_qualification_verdict.schema.json",
    "schemas/g25_expected_artifacts.schema.json",
    "schemas/g25_gpu_pilot_matrix.schema.json",
    "schemas/g25_gpu_pilot_session_contract.schema.json",
    "schemas/g25_gpu_pilot_approval.schema.json",
    "schemas/g25_same_source_review.schema.json",
    "schemas/g25_5_6sol_evaluation.schema.json",
    "schemas/g25_worker_descriptor.schema.json",
    "schemas/g25_application_terminal.schema.json",
    "schemas/g25_application_final_seal.schema.json",
    "schemas/g25_parent_output_replay.schema.json",
    "schemas/g25_session_file_inventory.schema.json",
    "schemas/g25_external_seal_anchor.schema.json",
    "schemas/g25_gpu_entrypoint_policy.schema.json",
    "schemas/c1_session.schema.json", "schemas/c1_system.schema.json",
    "schemas/c1_work_unit.schema.json", "schemas/scheduler_state.schema.json",
    "TRACE_CAPTURE_PLAN.yaml", "TRACE_COMPLETENESS_SCHEMA.yaml",
    "tests/__init__.py", "tests/fixture_factory.py",
    "tests/test_benchmark_contract.py", "tests/test_trace_completeness.py",
    "tests/test_benchmark_suite.py", "tests/test_capture_orchestrator.py",
    "tests/test_benchmark_trace_contract.py", "tests/test_executable_moe_benchmark.py",
    "tests/test_ingest_benchmark_session.py", "tests/test_m0_vertical.py",
    "tests/test_score_validation_mape.py",
    "tests/test_c1_audit.py", "tests/test_c1_canonical.py",
    "tests/test_c1_cleanroom.py", "tests/test_c1_quality_v2.py",
    "tests/test_g25_workload_qualification.py",
    "tests/test_g25_qualification_engine.py",
    "tests/test_g25_gpu_pilot_contract.py",
    "tests/test_g25_application.py", "tests/test_g25_application_runner.py",
    "tests/test_g25_deadlines_snapshot.py", "tests/test_g25_session.py",
    "tests/test_g25_runtime_closure.py", "tests/test_g25_supervisor_lifetime.py",
    "tests/test_g25_worker.py",
    "tests/test_c1_schema_negatives.py",
    "tests/test_t0_vertical.py", "tests/test_execution_gates.py",
    "tests/fixtures/model_snapshot/config.json",
    "configs/test_suites/moe_trace_suite_v1.yaml",
    "configs/test_suites/model_benchmark_matrix.yaml",
    "configs/test_suites/benchmark_registry.yaml",
    "configs/test_suites/splits/v1.yaml",
    "configs/test_suites/splits/v1.4.0/sample_split.yaml",
    "configs/test_suites/splits/v1.4.0/domain_split.yaml",
    "configs/test_suites/splits/v1.4.0/model_holdout.yaml",
    "configs/test_suites/splits/v1.4.0/hardware_holdout.yaml",
    "configs/test_suites/generation_configs/v1.yaml",
    "configs/test_suites/prompt_templates/v1.yaml",
    "configs/test_suites/serving_schedules/v1.yaml",
    "configs/test_suites/sample_manifest_v1.jsonl",
    "configs/test_suites/unresolved_gates_v1.json",
    "configs/test_suites/frozen/v1.2.0/inventory.json",
    "configs/test_suites/frozen/v1.2.0/sample_manifest.jsonl",
    "configs/test_suites/frozen/v1.3.0/inventory.json",
    "configs/test_suites/frozen/v1.3.0/sample_manifest.jsonl",
    "configs/test_suites/frozen/v1.4.0/inventory.json",
    "configs/test_suites/frozen/v1.4.0/sample_manifest.jsonl",
    "configs/test_suites/granite_c1/c1_quality_contract_v2.json",
    "configs/test_suites/granite_c1/g25_workload_qualification_v1.json",
    "configs/test_suites/granite_c1/g25_generation_profiles_v1.yaml",
    "configs/test_suites/granite_c1/g25_expected_artifacts_v1.json",
    "configs/test_suites/granite_c1/g25_gpu_pilot_matrix_v1.json",
    "configs/test_suites/granite_c1/g25_gpu_pilot_session_v1.json",
    "configs/runtime/g25_local_runtime_v1.json",
    "configs/runtime/g25_system_closure_v2.json",
    "configs/model_snapshots/granite-3.1-1b-a400m-instruct/"
    "0da7a48b0276d500ce5922fd2b33944091fc6c09/"
    "g25_runtime_payload_contract_v1.json",
    "configs/executable_moe/m0_tiny_qwen2moe_v1.yaml",
    "configs/capture_matrices/m0_rtx3050_vertical_v1.json",
    "datasets/snapshots/snapshot_inventory_v1.json",
    "historical_evidence/g3_failures_v1/archive.json",
    "expected_outputs.yaml", "checksums.txt", "package_manifest.json",
)
SOURCE_SCAN_ROOTS = (
    "adapters", "collectors", "configs", "templates", "generators",
    "scheduler", "schemas", "scripts", "tests",
)
SOURCE_INTEGRITY_EXCLUDED_TOP_LEVEL = {
    ".benchmark-runtime", ".pytest_cache", "artifacts", "models", "cache",
    "results", "runtime", "archive", "scheduler_runs", "diagnostic_runs",
    "qualification_runs",
}
SOURCE_INTEGRITY_EXCLUDED_ANY_PART = {"__pycache__"}
FROZEN_SUITE_SHA256 = "4de9eda6a8eabb5e49c897563033e2fa9d9a8b62db7b81790bb9a4c871f5621e"
FROZEN_SUITE_MERKLE = "5a7b1c4c7f7ab52d7172aa229d94bd59332b5df161c3f3b0170d863836070b75"
PACKAGE_REVISION = (
    "benchmark-driven-pipeline-smoke-v1.22-g25-s4-r5-application-review-candidate"
)
MEASUREMENT_STATUS = (
    "C1-FAIL: G3-R4 is FINAL FAIL / IMMUTABLE after c1a-t1-01/P0 reached "
    "max_new_tokens=256 and failed QG-1; C1-B is NOT-ELIGIBLE and G4-G6 are "
    "STOPPED; the fresh G2.5-S4-R4 review was NO-GO and prospective S4-R5 "
    "CPU-only remediation is eligible only for a new immutable same-hash "
    "three-role review, then "
    "gpt-5.6-sol GO and owner exact-command approval; qualification GPU cells "
    "remain zero and GPU authorization is NONE"
)
FIT_ROLES = {
    "cpu_runtime", "gpu_service", "pcie_transfer", "copy_engine",
    "memory", "queueing", "contention",
}
EVALUATION_METRICS = {
    "component_latency", "pcie_transfer_latency",
    "moe_replay_tpot", "moe_replay_throughput",
}
REPLAY_FEATURES = {
    "tokens", "cpu_calls", "gpu_operations", "memory_bytes",
    "queue_depth", "transfers", "phase", "concurrency",
}
CHECKSUM_UPDATE = (
    "cd PACKAGE_ROOT && python3 -c 'import hashlib,pathlib; "
    "import json; r=pathlib.Path(\".\"); "
    "fs=json.loads((r/\"package_manifest.json\").read_text())"
    "[\"file_inventory\"][\"files\"]; "
    "(r/\"checksums.txt\").write_text(\"\".join("
    "f\"{hashlib.sha256((r/p).read_bytes()).hexdigest()}  {p}\\n\" "
    "for p in fs if p != \"checksums.txt\"))'"
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_files(root: Path) -> list[Path]:
    """Return package source files, excluding runtimes, caches, models, and artifacts."""
    files: list[Path] = []
    for directory in SOURCE_SCAN_ROOTS:
        base = root / directory
        if not base.exists():
            continue
        files.extend(
            path for path in base.rglob("*")
            if path.is_file()
            and not excluded_from_source_integrity(
                path.relative_to(root).as_posix()
            )
        )
    return sorted(set(files))


def package_inventory_files(root: Path) -> list[str]:
    return sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
        and not excluded_from_source_integrity(path.relative_to(root).as_posix())
        and not path.name.endswith((".tar", ".tar.gz", ".tgz", ".zip"))
    )


def excluded_from_source_integrity(relative: str) -> bool:
    parts = Path(relative).parts
    return (
        not parts
        or parts[0] in SOURCE_INTEGRITY_EXCLUDED_TOP_LEVEL
        or bool(SOURCE_INTEGRITY_EXCLUDED_ANY_PART.intersection(parts))
        or any(
        part.startswith(".venv") for part in parts
        )
    )


def require_validation_runtime() -> None:
    missing = []
    for import_name, package_name in (
        ("yaml", "PyYAML"), ("jsonschema", "jsonschema"), ("torch", "torch")
    ):
        try:
            __import__(import_name)
        except ImportError:
            missing.append(package_name)
    if missing:
        raise SystemExit(
            "validation runtime missing required packages: "
            + ", ".join(missing)
            + "; install requirements.lock and/or set BENCHMARK_PYTHON to that "
            "environment. The complete torch-dependent test suite is mandatory."
        )


def parse_structured_source(path: Path) -> None:
    if path.suffix == ".json":
        json.loads(path.read_text(encoding="utf-8"))
    elif path.suffix in {".yaml", ".yml"}:
        import yaml
        yaml.safe_load(path.read_text(encoding="utf-8"))
    elif path.suffix == ".jsonl":
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip():
                try:
                    json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"line {number}: {exc}") from exc


def validate_result_contract(result: dict) -> list[str]:
    failures: list[str] = []
    required = {
        "schema_version", "status", "evidence", "metric_scope", "split",
        "experiment", "seed", "command", "package_id", "package_revision",
        "package_manifest_sha256", "checksums_sha256", "device", "runtime",
        "timestamp_utc", "raw_profiler_output", "raw_benchmarks",
    }
    missing = sorted(required - result.keys())
    if missing:
        failures.append("result missing fields: " + ", ".join(missing))
    split = result.get("split")
    if split != result.get("experiment", {}).get("split"):
        failures.append("top-level split does not match experiment.split")
    if not isinstance(result.get("seed"), int):
        failures.append("result seed must be an integer")
    if not isinstance(result.get("command"), list) or not result.get("command"):
        failures.append("result command must be a non-empty argv array")
    for field in ("package_manifest_sha256", "checksums_sha256"):
        value = result.get(field)
        if not isinstance(value, str) or len(value) != 64:
            failures.append(f"{field} must be SHA-256")

    rows = result.get("raw_benchmarks")
    if not isinstance(rows, list) or not rows:
        return failures + ["raw_benchmarks must be a non-empty array"]
    record_ids = [row.get("record_id") for row in rows]
    if any(not isinstance(value, str) or not value for value in record_ids):
        failures.append("every raw record requires record_id")
    if len(record_ids) != len(set(record_ids)):
        failures.append("raw record_id values are not unique")

    by_id = {}
    for row in rows:
        record_id = row.get("record_id")
        if record_id:
            by_id[record_id] = row
        samples = row.get("repeats_ms", row.get("repeats_ms_per_token"))
        if not isinstance(samples, list) or len(samples) < 3:
            failures.append(f"{record_id}: fewer than three raw repeats")
            continue
        statistics_value = row.get("statistics", {})
        if statistics_value.get("n") != len(samples):
            failures.append(f"{record_id}: statistics.n/repeats mismatch")
        if not isinstance(statistics_value.get("ci95"), list) or len(
            statistics_value.get("ci95", [])
        ) != 2:
            failures.append(f"{record_id}: missing 95% CI")
        operation = row.get("operation")
        if operation in {"h2d_pinned", "d2h_pinned"}:
            expected_direction = "h2d" if operation == "h2d_pinned" else "d2h"
            if row.get("direction") != expected_direction:
                failures.append(f"{record_id}: transfer direction mismatch")
            if not isinstance(row.get("bytes"), int) or row["bytes"] <= 0:
                failures.append(f"{record_id}: transfer bytes invalid")
            streams = row.get("copy_streams")
            if not isinstance(streams, int) or streams <= 0:
                failures.append(f"{record_id}: copy_streams invalid")
            expected_role = "pcie_transfer" if streams == 1 else "copy_engine"
            if row.get("calibration_role") != expected_role:
                failures.append(f"{record_id}: transfer calibration_role mismatch")
        if operation == "window_replay":
            missing_features = sorted(REPLAY_FEATURES - row.keys())
            if missing_features:
                failures.append(
                    f"{record_id}: replay missing " + ", ".join(missing_features)
                )
            throughput = row.get("repeats_tokens_per_second")
            if not isinstance(throughput, list) or len(throughput) != len(samples):
                failures.append(f"{record_id}: throughput repeats mismatch")
        if operation == "dequant" and (
            row.get("implementation")
            != "synthetic_symmetric_int4_proxy_not_checkpoint_awq"
            or not row.get("evidence_limit")
        ):
            failures.append(f"{record_id}: synthetic int4 scope was weakened")

    if split == "calibration":
        roles = {
            row.get("calibration_role") for row in rows
            if row.get("calibration_role")
        }
        missing_roles = sorted(FIT_ROLES - roles)
        if missing_roles:
            failures.append("calibration roles missing: " + ", ".join(missing_roles))
        memory_bytes = {
            row.get("bytes") for row in rows
            if row.get("calibration_role") == "memory"
        }
        if len(memory_bytes) < 2:
            failures.append("memory calibration needs two distinct byte sizes")
        queue_depths = {
            row.get("queue_depth") for row in rows
            if row.get("calibration_role") == "queueing"
        }
        if len(queue_depths) < 2:
            failures.append("queue calibration needs two distinct depths")
        contention = [
            row for row in rows if row.get("probe_family") == "contention"
        ]
        if {row.get("concurrency") for row in contention} != {1, 4}:
            failures.append("contention requires fixed-shape concurrency 1/4")
        shapes = {row.get("fixed_expert_tokens") for row in contention}
        if len(shapes) != 1:
            failures.append("contention probes do not use one fixed shape")
        fitted_contention = [
            row for row in contention
            if row.get("calibration_role") == "contention"
        ]
        if len(fitted_contention) != 1 or not fitted_contention[0].get(
            "base_service_ms"
        ):
            failures.append("contention=4 lacks measured base_service_ms")
        if result.get("evaluation_points"):
            failures.append("calibration split must not emit evaluation_points")
    else:
        points = result.get("evaluation_points")
        if not isinstance(points, list) or not points:
            failures.append("non-calibration split lacks evaluation_points")
        else:
            point_ids = [point.get("point_id") for point in points]
            if len(point_ids) != len(set(point_ids)):
                failures.append("evaluation point IDs are not unique")
            metrics = {point.get("metric") for point in points}
            missing_metrics = sorted(EVALUATION_METRICS - metrics)
            if missing_metrics:
                failures.append(
                    "evaluation metrics missing: " + ", ".join(missing_metrics)
                )
            for point in points:
                source = by_id.get(point.get("source_record_id"))
                if source is None:
                    failures.append(
                        f"{point.get('point_id')}: source record does not exist"
                    )
                    continue
                metric = point.get("metric")
                stats_key = (
                    "throughput_statistics"
                    if metric == "moe_replay_throughput" else "statistics"
                )
                expected = source.get(stats_key, {}).get("mean")
                if point.get("measured") != expected:
                    failures.append(
                        f"{point.get('point_id')}: measured not from source mean"
                    )
    return failures


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    p.add_argument("--skip-checksums", action="store_true")
    args = p.parse_args()
    root = args.root.resolve()
    require_validation_runtime()
    failures = []
    package_manifest: dict = {}
    evidence_value = os.environ.get("M0_EVIDENCE_ROOT")
    external_evidence_root = (
        Path(evidence_value).expanduser().resolve() if evidence_value else None
    )
    if external_evidence_root is not None and not external_evidence_root.is_dir():
        failures.append(
            f"M0_EVIDENCE_ROOT is not a directory: {external_evidence_root}"
        )
    for rel in REQUIRED:
        if not (root / rel).is_file():
            failures.append(f"missing {rel}")
    scanned_sources = source_files(root)
    if not scanned_sources:
        failures.append("source scan found no files")
    for path in scanned_sources:
        try:
            if path.suffix == ".py":
                ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            parse_structured_source(path)
        except Exception as exc:
            failures.append(f"{path.relative_to(root)}: {exc}")
    for rel in (
        "workloads/windows.json", "package_manifest.json",
        "schemas/session_manifest.schema.json",
        "schemas/pass_manifest.schema.json",
        "schemas/benchmark_trace_record.schema.json",
        "schemas/canonical_moe_ir.schema.json",
        "schemas/gpu_execution_approval.schema.json",
        "schemas/c1_benchmark.schema.json",
        "schemas/c1_pass.schema.json",
        "schemas/c1_quality.schema.json",
        "schemas/c1_routing.schema.json",
        "schemas/c1_session.schema.json",
        "schemas/c1_system.schema.json",
        "schemas/c1_work_unit.schema.json",
        "schemas/scheduler_state.schema.json",
    ):
        try:
            json.loads((root / rel).read_text())
        except Exception as exc:
            failures.append(f"{rel}: {exc}")
    try:
        import yaml
        splits = yaml.safe_load((root / "configs/splits.yaml").read_text())
        matrix = yaml.safe_load((root / "configs/benchmark_matrix.yaml").read_text())
        expected_outputs = yaml.safe_load((root / "expected_outputs.yaml").read_text())
        gpu_profiles = yaml.safe_load((root / "configs/gpu_profiles.yaml").read_text())
        model_registry = yaml.safe_load((root / "configs/model_registry.yaml").read_text())
        compatibility = yaml.safe_load(
            (root / "configs/model_compatibility.yaml").read_text()
        )
        hardware_schedule = yaml.safe_load(
            (root / "configs/hardware_schedule.yaml").read_text()
        )
        execution_review = yaml.safe_load(
            (root / "configs/gpu_execution_review.yaml").read_text()
        )
        trace_plan = yaml.safe_load((root / "TRACE_CAPTURE_PLAN.yaml").read_text())
        workloads = json.loads((root / "workloads/windows.json").read_text())
        package_manifest = json.loads((root / "package_manifest.json").read_text())
        suite = yaml.safe_load(
            (root / "configs/test_suites/moe_trace_suite_v1.yaml").read_text()
        )
        suite_mapping = yaml.safe_load(
            (root / "configs/test_suites/model_benchmark_matrix.yaml").read_text()
        )
        model_holdout_axis = yaml.safe_load(
            (root / "configs/test_suites/splits/v1.4.0/model_holdout.yaml").read_text()
        )
        hardware_holdout_axis = yaml.safe_load(
            (root / "configs/test_suites/splits/v1.4.0/hardware_holdout.yaml").read_text()
        )
        frozen_inventory = json.loads(
            (root / "configs/test_suites/frozen/v1.4.0/inventory.json").read_text()
        )
        frozen_manifest = root / "configs/test_suites/frozen/v1.4.0/sample_manifest.jsonl"
        snapshot_inventory = json.loads(
            (root / "datasets/snapshots/snapshot_inventory_v1.json").read_text()
        )
        capture_matrix = json.loads(
            (root / "configs/capture_matrices/m0_rtx3050_vertical_v1.json").read_text()
        )
        if suite.get("suite_revision") != "v1.4.0":
            failures.append("benchmark suite revision is not v1.4.0")
        expected_tasks = {f"T{index}" for index in range(9)}
        if set(suite.get("tasks", {})) != expected_tasks:
            failures.append("suite task definitions do not map T0-T8")
        if set(suite_mapping.get("task_classes", {})) != expected_tasks:
            failures.append("model benchmark manifest mapping does not cover T0-T8")
        if model_holdout_axis.get("assignment_unit") != "model_id":
            failures.append("model holdout axis is not assigned by model_id")
        if (
            hardware_holdout_axis.get("status")
            != "unassigned_pending_future_decision"
            or hardware_holdout_axis.get("active_split") is not False
            or hardware_holdout_axis.get("assignments") != []
        ):
            failures.append("hardware holdout is not inactive and unassigned")
        if frozen_inventory.get("suite_revision") != "v1.4.0":
            failures.append("frozen suite inventory revision is not v1.4.0")
        if frozen_inventory.get("frozen_manifest_sha256") != FROZEN_SUITE_SHA256:
            failures.append("frozen suite inventory hash changed")
        if frozen_inventory.get("merkle", {}).get("root") != FROZEN_SUITE_MERKLE:
            failures.append("frozen suite Merkle root changed")
        if digest(frozen_manifest) != FROZEN_SUITE_SHA256:
            failures.append("frozen v1.4.0 sample manifest hash mismatch")
        split_axes = frozen_inventory.get("split_axes", {})
        if set(split_axes) != {"sample", "domain", "model", "hardware"}:
            failures.append("frozen suite does not bind all four split axes")
        elif split_axes["hardware"].get("active") is not False:
            failures.append("hardware holdout must not participate in active split")
        if frozen_inventory.get("source_files", {}).get(
            "datasets/snapshots/snapshot_inventory_v1.json"
        ) != digest(root / "datasets/snapshots/snapshot_inventory_v1.json"):
            failures.append("frozen suite does not map current external snapshot inventory")
        if snapshot_inventory.get("file_count") != len(snapshot_inventory.get("files", [])):
            failures.append("external snapshot inventory file_count mismatch")
        for item in snapshot_inventory.get("files", []):
            snapshot = root / item.get("path", "")
            if not snapshot.is_file() or digest(snapshot) != item.get("sha256"):
                failures.append(f"external snapshot inventory mismatch: {item.get('path')}")
        smoke_samples = [
            sample
            for benchmark in capture_matrix.get("benchmarks", [])
            for sample in benchmark.get("samples", [])
        ]
        if len(smoke_samples) != 8 or len({
            sample.get("sample_id") for sample in smoke_samples
        }) != 8:
            failures.append("M0 capture matrix must contain exactly 8 unique smoke samples")
        if capture_matrix.get("suite", {}).get("suite_revision") != "v1.4.0":
            failures.append("M0 capture matrix source suite is not v1.4.0")
        frozen_by_id = {
            row["sample_id"]: row
            for row in (
                json.loads(line)
                for line in frozen_manifest.read_text(encoding="utf-8").splitlines()
            )
        }
        if any(
            sample.get("sample_id") not in frozen_by_id
            or frozen_by_id[sample["sample_id"]].get("split") != "smoke"
            or frozen_by_id[sample["sample_id"]].get("raw_sample_hash")
            != sample.get("raw_sample_hash")
            for sample in smoke_samples
        ):
            failures.append("M0 capture matrix contains non-v1.4 smoke samples")
        if capture_matrix.get("passes") != [f"P{index}" for index in range(7)]:
            failures.append("M0 capture matrix does not require P0-P6")
        evidence_status = capture_matrix.get("evidence_status", {})
        if (
            evidence_status.get("plan_is_capture_complete") is not False
            or evidence_status.get("measured_artifact_is_separate") is not True
        ):
            failures.append("capture matrix confuses planning with measured artifact evidence")
        def selector_key(selector: dict) -> tuple[str, str, str]:
            return (
                str(selector["benchmark"]),
                str(selector["subject"]),
                str(selector["query_id"]),
            )

        selectors = [
            {selector_key(selector) for selector in splits[name]["queries"]}
            for name in ("calibration", "validation", "holdout")
        ]
        if any(
            selectors[i] & selectors[j]
            for i in range(3)
            for j in range(i + 1, 3)
        ):
            failures.append("whole-query split selectors overlap")
        if splits["holdout"].get("frozen") is not True:
            failures.append("holdout is not frozen")
        if set(matrix["operations"]) != {
            "h2d_pinned", "d2h_pinned", "selected_expert", "grouped_gemm",
            "gather_scatter", "dequant", "window_replay",
            "cpu_runtime", "device_memory", "queue_depth",
            "contention_fixed_shape",
        }:
            failures.append("benchmark operation matrix is incomplete")
        if matrix["defaults"]["hidden_size"] != 7168 or matrix["defaults"]["intermediate_size"] != 2048:
            failures.append("benchmark matrix is not DeepSeek-R1-shaped")
        if matrix["defaults"]["num_experts"] != 256 or matrix["defaults"]["top_k"] != 8:
            failures.append("DeepSeek-R1 expert/top-k dimensions changed")
        if len(set(matrix["defaults"]["device_memory_sizes_bytes"])) < 2:
            failures.append("device-memory calibration has fewer than two sizes")
        if len(set(matrix["defaults"]["queue_depths"])) < 2:
            failures.append("queue calibration has fewer than two depths")
        if not isinstance(matrix["defaults"].get("seed"), int):
            failures.append("benchmark seed is not fixed")
        for name, wanted in zip(
            ("calibration", "validation", "holdout"), selectors
        ):
            items = workloads["splits"].get(name, [])
            got = {
                (str(x["benchmark"]), str(x["subject"]), str(x["query_id"]))
                for x in items
            }
            if got != wanted or any(not x.get("steps") for x in items):
                failures.append(f"workload split mismatch/empty: {name}")
        if expected_outputs["result_contract"]["required_metric_name"] != "MoE-replay TPOT":
            failures.append("TPOT metric naming contract changed")
        if expected_outputs.get("benchmark_suite", {}).get("suite_revision") != "v1.4.0":
            failures.append("expected outputs missing benchmark suite v1.4.0 contract")
        if expected_outputs.get("benchmark_smoke", {}).get(
            "distinct_from_legacy_smoke_microbenchmark"
        ) is not True:
            failures.append("legacy smoke and benchmark smoke are not distinguished")
        if expected_outputs.get("benchmark_smoke", {}).get(
            "online_preflight_required_for_gpu_execution"
        ) is not True:
            failures.append("benchmark GPU smoke lacks online preflight contract")
        if expected_outputs.get("capture_matrix", {}).get(
            "plan_is_capture_complete"
        ) is not False:
            failures.append("capture plan may not claim completed measurement")
        if expected_outputs.get("m0_evidence_limits", {}).get("tier") != "M0":
            failures.append("M0 evidence limitation contract missing")
        if expected_outputs.get("simulation", {}).get(
            "hardware_latency_claim"
        ) is not False:
            failures.append("simulation contract may not claim hardware latency")
        if package_manifest.get("schema_version") != "gpu-run-package-v2":
            failures.append("package manifest is not v2")
        if package_manifest.get("package_revision") != PACKAGE_REVISION:
            failures.append(f"package revision must be {PACKAGE_REVISION}")
        if package_manifest.get("measurement_status") != MEASUREMENT_STATUS:
            failures.append("package manifest measurement status is not exact")
        if expected_outputs.get("measurement_status") != MEASUREMENT_STATUS:
            failures.append("expected outputs measurement status is not exact")
        distribution = package_manifest.get("evidence_distribution", {})
        if (
            distribution.get("source_package_self_contained") is not True
            or distribution.get("embedded_measurement_evidence") != "not_included"
            or distribution.get("external_evidence_environment_variable")
            != "M0_EVIDENCE_ROOT"
            or distribution.get("missing_external_evidence_test_result")
            != "explicit_skip"
            or distribution.get("skip_satisfies_measurement_gate") is not False
        ):
            failures.append("package manifest evidence separation contract is incomplete")
        expected_distribution = expected_outputs.get("evidence_distribution", {})
        if (
            expected_distribution.get("source_package") != "PASS"
            or expected_distribution.get("embedded_measurement_evidence")
            != "NOT_INCLUDED"
            or expected_distribution.get("skip_satisfies_formal_or_paid_gate")
            is not False
        ):
            failures.append("expected outputs evidence separation contract is incomplete")
        runtime_contract = package_manifest.get("runtime", {})
        runtime_inventory_path = root / "configs/runtime/g25_local_runtime_v1.json"
        system_closure_path = root / "configs/runtime/g25_system_closure_v2.json"
        if (
            runtime_contract.get("runtime_inventory")
            != "configs/runtime/g25_local_runtime_v1.json"
            or runtime_contract.get("runtime_inventory_sha256")
            != "c3282bb8a6531ac442172278489eff391b0c5d042f610203081930ba873792d3"
            or runtime_contract.get("runtime_distribution_count") != 71
            or runtime_contract.get("record_bound_file_count") != 22491
            or runtime_contract.get("isolated_python_flags")
            != ["-I", "-S", "-B", "-X", "utf8"]
            or runtime_contract.get("runtime_exact_file_count") != 58970
            or runtime_contract.get("runtime_exact_tree_sha256")
            != "179cc8fa7f0598b956e77ffcecf9f336ebc7ddd61c785c48e28d0a39140b4625"
            or runtime_contract.get("stdlib_exact_file_count") != 1361
            or runtime_contract.get("stdlib_exact_tree_sha256")
            != "fabc15b299143df7560a170b358a671347984d808b49efe61b06483f6dc12e5e"
            or runtime_contract.get("driver_runtime_exact_file_count") != 21
            or runtime_contract.get("driver_runtime_exact_tree_sha256")
            != "eacd617d22d8762d600624820796d3d851a2002ac641868213084104251205b5"
            or runtime_contract.get("system_dependency_count") != 30
            or runtime_contract.get("system_files_sha256")
            != "f4c7e80e83400a789390a963e593dff3ee9ac6d72d5d168bef8ce0dce53c54ce"
            or runtime_contract.get("system_closure")
            != "configs/runtime/g25_system_closure_v2.json"
            or runtime_contract.get("system_closure_sha256")
            != "cb81a3c638df8a5e6ed32a7210f3f7d0140e8c982e816b135fc23ec6b7c19ed5"
            or runtime_contract.get("static_dependency_edges_sha256")
            != "c497fd6849f348ef1a534a38e570750e46762759d999ead08b43f02329b7af2f"
            or runtime_contract.get("dynamic_loader_cache_policy")
            != "inhibit-cache"
            or runtime_contract.get("locale") != {"LC_ALL": "C", "LANG": "C"}
            or runtime_contract.get("live_loaded_dependency_closure") is not True
            or not runtime_inventory_path.is_file()
            or digest(runtime_inventory_path)
            != runtime_contract.get("runtime_inventory_sha256")
            or not system_closure_path.is_file()
            or digest(system_closure_path)
            != runtime_contract.get("system_closure_sha256")
        ):
            failures.append("package-local runtime inventory contract is incomplete")
        if package_manifest.get("release_class") != "pipeline_smoke":
            failures.append("package manifest release_class must be pipeline_smoke")
        execution_policy = package_manifest.get("execution_policy", {})
        if (
            execution_policy.get("governing_decision") != "D-062"
            or execution_policy.get("paid_gpu_execution") != "no_go"
        ):
            failures.append("package manifest does not record D-062 paid NO-GO")
        review_gate = package_manifest.get("second_decision_layer_gate", {})
        if (
            review_gate.get("config") != "configs/gpu_execution_review.yaml"
            or review_gate.get("schema")
            != "schemas/gpu_execution_approval.schema.json"
            or review_gate.get("script") != "scripts/review_gate.py"
        ):
            failures.append("package manifest second-decision-layer gate is incomplete")
        expected_entrypoints = {
            "legacy_smoke", "experiment", "freeze_suite", "capture_matrix",
            "benchmark_smoke", "ingest_session", "canonicalize_m0",
            "expand_workload", "simulate_workload", "capture_plan_deprecated",
            "all_models_compatibility_plan", "trace_audit", "package_results",
            "verify_package", "dry_run", "c1_model_preflight", "c1_model_smoke",
            "c1_run_start", "c1_run_resume", "c1_trace_audit", "c1_package",
            "c1_verify", "c1_diagnostic_run", "c1_diagnostic_status",
            "c1_diagnostic_compare", "g25_synthetic_governance",
            "g25_pilot_plan", "g25_pilot_static_preflight", "g25_pilot_status",
            "g25_application_start",
        }
        if set(package_manifest.get("entrypoints", {})) != expected_entrypoints:
            failures.append("package manifest entrypoints do not match run.sh modes")
        manifest_suite = package_manifest.get("benchmark_suite", {})
        if (
            manifest_suite.get("suite_revision") != "v1.4.0"
            or manifest_suite.get("frozen_manifest_sha256") != FROZEN_SUITE_SHA256
            or manifest_suite.get("merkle_root") != FROZEN_SUITE_MERKLE
        ):
            failures.append("package manifest frozen suite provenance mismatch")
        granite_c1 = package_manifest.get("granite_c1", {})
        granite_selection = root / (
            "configs/test_suites/granite_c1/sample_manifest.jsonl"
        )
        if (
            granite_c1.get("suite_revision") != "granite-c1-v1.1.0"
            or granite_c1.get("adapter_version") != "granite-c1-adapter-v8"
            or granite_c1.get("status")
            != (
                "g3_r4_final_fail_immutable_g25_s4_r4_review_no_go_s4_r5_cpu_"
                "only_review_candidate_5_6sol_owner_gpu_none"
            )
            or granite_c1.get("diagnostic_status")
            != "no_observed_drift_under_tested_configuration_p0x2_p2x2"
            or granite_c1.get("selection_manifest_sha256")
            != "f32aa23823c2b88f7f2fab2a11cbbafe39c10adc54d5570ca7474b27996cd2bd"
            or granite_c1.get("chat_template_sha256")
            != "08962c2f15d56767854b46dfc4070b37f4c443551833bba65b417191735f3187"
            or not granite_selection.is_file()
            or digest(granite_selection)
            != granite_c1.get("selection_manifest_sha256")
            or granite_c1.get("g25_status")
            != (
                "S4_R4_review_NO_GO_S4_R5_CPU_only_review_candidate_fresh_"
                "same_hash_review_required_gpt_5_6_sol_pending_owner_pending_"
                "GPU_not_run_not_authorized"
            )
            or granite_c1.get("g25_system_closure")
            != "configs/runtime/g25_system_closure_v2.json"
            or granite_c1.get("g25_model_runtime_payload_contract")
            != (
                "configs/model_snapshots/granite-3.1-1b-a400m-instruct/"
                "0da7a48b0276d500ce5922fd2b33944091fc6c09/"
                "g25_runtime_payload_contract_v1.json"
            )
            or granite_c1.get("g25_model_runtime_payload_contract_sha256")
            != "015e070a3f9137d93263604c9c2f0ee967bb1e8d4610dd728d876ff2a0fd0876"
            or granite_c1.get("g25_model_snapshot_verifier")
            != "adapters/models/granite_moe/snapshot.py"
            or granite_c1.get("g25_parent_output_replay_schema")
            != "schemas/g25_parent_output_replay.schema.json"
            or granite_c1.get("g25_parent_output_replay_schema_sha256")
            != "fcf46333f6e7c8d17ff2d51d921790102b4d659963051702f9f5708254d8ead3"
            or granite_c1.get("g25_session_file_inventory_schema")
            != "schemas/g25_session_file_inventory.schema.json"
            or granite_c1.get("g25_external_seal_anchor_schema")
            != "schemas/g25_external_seal_anchor.schema.json"
        ):
            failures.append("package manifest Granite C1 v1.1 contract mismatch")
        archive = package_manifest.get("provenance_archive", {})
        if external_evidence_root is not None and external_evidence_root.is_dir():
            artifacts_root = external_evidence_root.parent
            archive_relative = Path(archive.get("path", "")).relative_to("artifacts")
            archive_path = artifacts_root / archive_relative
            if (
                not archive_path.is_file()
                or digest(archive_path) != archive.get("sha256")
            ):
                failures.append(
                    "external package provenance archive path/SHA mismatch"
                )
            sidecar_relative = Path(
                archive.get("sidecar_path", "")
            ).relative_to("artifacts")
            sidecar_path = artifacts_root / sidecar_relative
            expected_sidecar = f"{archive.get('sha256')}  {archive_path.name}\n"
            if (
                not sidecar_path.is_file()
                or sidecar_path.read_text(encoding="utf-8") != expected_sidecar
                or archive.get("sidecar_verified") is not True
            ):
                failures.append(
                    "external package provenance archive sidecar mismatch"
                )
        m0 = package_manifest.get("m0_evidence", {})
        if (
            m0.get("model_revision")
            != "f736f270816032b3c721f7422c62dea1381f49d7"
            or m0.get("suite_revision") != "v1.2.0"
            or m0.get("measured_sample_count") != 8
        ):
            failures.append("package manifest M0 model/data evidence mismatch")
        profile_ids = set(gpu_profiles.get("profiles", {}))
        compatibility_ids = set(compatibility.get("gpu_profiles", {}))
        aliases = compatibility.get("profile_aliases", {})
        unresolved_profiles = sorted(
            profile_id for profile_id in profile_ids
            if profile_id not in compatibility_ids
            and aliases.get(profile_id) not in compatibility_ids
        )
        if unresolved_profiles:
            failures.append(
                "GPU profiles lack compatibility mapping: "
                + ", ".join(unresolved_profiles)
            )
        for alias, target in aliases.items():
            if alias not in profile_ids or target not in compatibility_ids:
                failures.append(f"invalid compatibility alias: {alias} -> {target}")
        tiers = {"M0", "M1", "M2", "M3"}
        registry_tiers = {
            model.get("tier")
            for model in model_registry.get("models", {}).values()
        }
        if registry_tiers != tiers:
            failures.append(
                f"model registry tier coverage mismatch: {sorted(registry_tiers)}"
            )
        scheduled = set(
            hardware_schedule.get("hardware_order", {}).get("required", [])
        ) | set(hardware_schedule.get("hardware_order", {}).get("optional", []))
        scheduled.discard("h100_pcie_80gb_or_h100_sxm5_80gb")
        missing_schedule_profiles = sorted(scheduled - profile_ids)
        if missing_schedule_profiles:
            failures.append(
                "hardware schedule references unknown profiles: "
                + ", ".join(missing_schedule_profiles)
            )
        holdout_profiles = set(
            hardware_schedule.get("trust_roles", {})
            .get("holdout", {})
            .get("hardware_profiles", [])
        )
        if not holdout_profiles <= profile_ids:
            failures.append("holdout schedule references unknown GPU profile")
        if holdout_profiles:
            failures.append("D-062 requires no active holdout schedule")
        if hardware_schedule.get("scheduling_policy", {}).get("execution_allowed") is not False:
            failures.append("D-062 paid execution stop is not active")
        h100_profiles = {"h100_pcie_80gb", "h100_sxm5_80gb"}
        for profile_id in h100_profiles:
            profile = gpu_profiles["profiles"][profile_id]
            if (
                profile.get("enabled") is not False
                or profile.get("optional") is not True
                or profile.get("execution_enabled") is not False
                or profile.get("disabled_reason") != "D-062"
            ):
                failures.append(f"{profile_id} does not preserve D-062 disabled history")
        paid_profiles = {
            profile_id
            for profile_id, assignment in yaml.safe_load(
                (root / "configs/storage_budget.yaml").read_text()
            ).get("profile_assignment", {}).items()
            if assignment.get("paid_session") is True
        }
        for profile_id in paid_profiles:
            if gpu_profiles["profiles"][profile_id].get("execution_enabled") is not False:
                failures.append(f"paid profile execution remains enabled: {profile_id}")
        decision = execution_review.get("governing_decision", {})
        if decision.get("decision_id") != "D-062" or decision.get("superseded_by") is not None:
            failures.append("review policy does not hard-block active D-062")
        if execution_review.get("governance_baseline", {}).get("decision_id") != "D-063":
            failures.append("review policy does not record D-063 governance baseline")
        paid_review = execution_review.get("paid_execution", {})
        if set(paid_review.get("required_distinct_reviewer_roles", [])) != {
            "architecture_system", "model_benchmark", "trace_statistics",
        }:
            failures.append("review policy lacks three required distinct reviewer roles")
        if "superseding_decision_id" not in paid_review.get("bind_to", []):
            failures.append("approval is not bound to superseding_decision_id")
        pro6000 = gpu_profiles["profiles"][
            "rtx_pro_6000_blackwell_workstation_96gb"
        ]
        if (
            pro6000.get("accepted_name_regex")
            != "^NVIDIA RTX PRO 6000 Blackwell Workstation Edition 96GB$"
        ):
            failures.append("RTX PRO 6000 accepted-name regex is not exact")
        expected_passes = {f"P{index}" for index in range(7)}
        trace_profiles = trace_plan.get("profiles", {})
        if set(trace_profiles) != {"minimal", "standard", "maximal"}:
            failures.append("trace profiles must be minimal/standard/maximal")
        for name, profile in trace_profiles.items():
            if set(profile.get("passes", {})) != expected_passes:
                failures.append(f"trace profile {name} does not cover P0-P6")
        if "baseline" in trace_profiles:
            failures.append("legacy baseline trace profile remains")
        if set(expected_outputs["result_contract"]["required_evaluation_metrics"]) != (
            EVALUATION_METRICS
        ):
            failures.append("four-metric evaluation contract changed")
        if set(
            expected_outputs["result_contract"]["calibration_required_operations"]
        ) != {
            "cpu_runtime", "device_memory", "queue_depth",
            "contention_fixed_shape",
        }:
            failures.append("minimal calibration probe contract changed")
    except Exception as exc:
        failures.append(f"YAML/schema-shape validation failed: {exc}")
    for result_path in sorted((root / "results").glob("*/result.json")):
        try:
            result = json.loads(result_path.read_text())
            failures.extend(
                f"{result_path.relative_to(root)}: {failure}"
                for failure in validate_result_contract(result)
            )
        except Exception as exc:
            failures.append(f"{result_path.relative_to(root)}: {exc}")
    checksum_rels: set[str] = set()
    if not args.skip_checksums and (root / "checksums.txt").is_file():
        for line in (root / "checksums.txt").read_text().splitlines():
            if not line.strip():
                continue
            expected, rel = line.split(maxsplit=1)
            if excluded_from_source_integrity(rel):
                continue
            checksum_rels.add(rel)
            path = root / rel
            if not path.is_file() or digest(path) != expected:
                failures.append(
                    f"checksum mismatch: {rel}; after all staging edits are final, "
                    f"regenerate with: {CHECKSUM_UPDATE}; "
                    "do not copy source-repo checksums"
                )
        inventory = package_manifest.get("file_inventory", {})
        inventory_files = [
            rel for rel in inventory.get("files", [])
            if isinstance(rel, str) and not excluded_from_source_integrity(rel)
        ]
        if (
            not isinstance(inventory_files, list)
            or any(not isinstance(rel, str) for rel in inventory_files)
            or len(inventory_files) != len(set(inventory_files))
        ):
            failures.append("package_manifest file inventory is invalid")
        else:
            inventory_set = set(inventory_files)
            if inventory.get("file_count") != len(inventory_set):
                failures.append("package_manifest file_count does not match files")
            disk_inventory = set(package_inventory_files(root))
            if inventory_set != disk_inventory:
                missing = sorted(disk_inventory - inventory_set)
                extra = sorted(inventory_set - disk_inventory)
                if missing:
                    failures.append(
                        "package manifest inventory missing source files: "
                        + ", ".join(missing)
                    )
                if extra:
                    failures.append(
                        "package manifest inventory has excluded/extra files: "
                        + ", ".join(extra)
                    )
            missing_files = sorted(
                rel for rel in inventory_set if not (root / rel).is_file()
            )
            if missing_files:
                failures.append(
                    "file inventory entries missing: " + ", ".join(missing_files)
                )
            for rel in sorted(inventory_set):
                path = root / rel
                if path.is_symlink():
                    failures.append(f"inventory must not contain symlink: {rel}")
                elif path.is_file() and path.stat().st_nlink != 1:
                    failures.append(f"inventory must not contain hardlinked file: {rel}")
            expected_checksums = inventory_set - {"checksums.txt"}
            if checksum_rels != expected_checksums:
                missing = sorted(expected_checksums - checksum_rels)
                extra = sorted(checksum_rels - expected_checksums)
                if missing:
                    failures.append(
                        "checksum coverage missing: " + ", ".join(missing)
                    )
                if extra:
                    failures.append(
                        "checksums contain non-inventory files: " + ", ".join(extra)
                    )
    run_text = (root / "run.sh").read_text(encoding="utf-8")
    if (
        'if [[ "${1:-}" == "projectctl" ]]' not in run_text
        or '"$ROOT/projectctl"' not in run_text
    ):
        failures.append("run.sh missing C1 projectctl entrypoint")
    for flag in (
        "--capture-plan", "--all-models", "--gpu-profile", "--trace-profile",
        "--trace-audit", "--session-root", "--package-results",
        "--verify-package", "--dry-run", "--smoke", "--experiment",
        "--freeze-suite", "--capture-matrix", "--benchmark-smoke",
        "--ingest-session", "--source", "--archive", "--canonicalize-m0",
        "--expand-workload", "--simulate-workload", "--output", "--device",
        "--release-class",
        "--execution-matrix", "--execution-approval", "--gpu-uuid",
        "--pci-bus-id", "--provider-metadata",
        "--provider-metadata-sha256", "--storage-estimate",
    ):
        if flag not in run_text:
            failures.append(f"run.sh missing v2 entrypoint flag {flag}")
    if (
        'benchmark_args+=(--run-mode local --local-cpu-fallback)' not in run_text
    ):
        failures.append(
            "run.sh CPU benchmark mode does not pass explicit local fallback flags"
        )
    help_result = subprocess.run(
        ["bash", str(root / "run.sh"), "--help"],
        cwd=root, text=True, capture_output=True,
    )
    if help_result.returncode:
        failures.append(f"run.sh --help failed: {help_result.stderr.strip()}")
    else:
        for flag in (
            "--smoke", "--benchmark-smoke", "--freeze-suite", "--capture-matrix",
            "--ingest-session", "--canonicalize-m0", "--expand-workload",
            "--simulate-workload", "--trace-audit", "--package-results",
            "--verify-package",
            "--release-class",
        ):
            if flag not in help_result.stdout:
                failures.append(f"run.sh --help missing {flag}")
    lock = (root / "requirements.lock").read_text(encoding="utf-8")
    required_pins = {
        "torch": "2.7.1+cu128",
        "transformers": "4.47.0",
        "datasets": "4.0.0",
        "accelerate": "1.8.1",
        "safetensors": "0.5.3",
        "huggingface-hub": "0.33.4",
        "tokenizers": "0.21.2",
        "pyarrow": "20.0.0",
        "numpy": "2.2.6",
        "PyYAML": "6.0.2",
        "jsonschema": "4.24.0",
    }
    for name, version in required_pins.items():
        if f"{name}=={version}" not in lock:
            failures.append(f"requirements.lock missing exact pin {name}=={version}")
    if "torch==2.13" in lock:
        failures.append("requirements.lock contains prohibited torch 2.13")
    dry = subprocess.run(
        [sys.executable, str(root / "scripts/benchmark.py"), "--dry-run",
         "--package-root", str(root)],
        cwd=root, text=True, capture_output=True,
    )
    if dry.returncode:
        failures.append(f"benchmark dry-run failed: {dry.stderr.strip()}")
    test_environment = os.environ.copy()
    existing_pythonpath = test_environment.get("PYTHONPATH")
    test_environment["PYTHONPATH"] = (
        str(root) if not existing_pythonpath
        else os.pathsep.join((str(root), existing_pythonpath))
    )
    tests = subprocess.run(
        [
            sys.executable, "-m", "unittest", "discover",
            "-s", str(root / "tests"), "-t", str(root), "-p", "test_*.py",
        ],
        cwd=root, env=test_environment, text=True, capture_output=True,
    )
    if tests.returncode:
        failures.append(
            "unit tests failed: " + (tests.stderr or tests.stdout).strip()
        )
    if failures:
        print("\n".join(f"FAIL: {x}" for x in failures), file=sys.stderr)
        return 1
    print("package validation: PASS")
    print("source_package=PASS")
    print("embedded_measurement_evidence=NOT_INCLUDED")
    if external_evidence_root is None:
        print("external_measurement_evidence=NOT_PROVIDED")
        print(
            "full_evidence_regression_command="
            "M0_EVIDENCE_ROOT=/path/to/artifacts/m0_benchmark_smoke "
            "python3 -m unittest discover -s tests -t . -p 'test_*.py'"
        )
    else:
        print(f"external_measurement_evidence=PASS:{external_evidence_root}")
    print("measurement_gate_from_skips=PROHIBITED")
    print(dry.stdout.strip())
    print(tests.stdout.strip() or tests.stderr.strip())
    if args.skip_checksums:
        print(f"checksums.txt not checked; final refresh command: {CHECKSUM_UPDATE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

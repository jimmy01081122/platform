"""Leakage-safe calibrated transaction backend.

The backend consumes measured GPU result artifacts plus a calibration-pack
split manifest.  It intentionally has no fallback constants: every parameter
family must be identified by calibration-only probes before scoring.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any


class CalibrationError(ValueError):
    """Raised when evidence is incomplete or split isolation is violated."""


METRICS = (
    "component_latency",
    "pcie_transfer_latency",
    "moe_replay_tpot",
    "moe_replay_throughput",
)
FIT_ROLES = {
    "cpu_runtime",
    "gpu_service",
    "pcie_transfer",
    "copy_engine",
    "memory",
    "queueing",
    "contention",
}


def _load(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise CalibrationError(f"missing required artifact: {path}")
    text = path.read_text()
    if path.suffix.lower() == ".json":
        value = json.loads(text)
    else:
        try:
            import yaml
        except ImportError as exc:
            raise CalibrationError("PyYAML is required for non-JSON calibration packs") from exc
        value = yaml.safe_load(text)
    if not isinstance(value, dict):
        raise CalibrationError(f"artifact must contain an object: {path}")
    return value


def _resolve(root: Path, value: str | None, label: str) -> Path:
    if not value:
        raise CalibrationError(f"{label} path is required")
    path = Path(value)
    return path if path.is_absolute() else (root / path).resolve()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _mean_ms(record: dict[str, Any]) -> float:
    samples = record.get("repeats_ms")
    if samples is None and record.get("repeats_ms_per_token") is not None:
        samples = record["repeats_ms_per_token"]
    if not isinstance(samples, list) or not samples:
        raise CalibrationError(
            f"raw record {record.get('record_id', '<unknown>')} lacks repeat samples"
        )
    values = [float(value) for value in samples]
    if any(not math.isfinite(value) or value <= 0 for value in values):
        raise CalibrationError("repeat samples must be finite and positive")
    return statistics.fmean(values)


def _line_fit(rows: list[tuple[float, float]], label: str) -> tuple[float, float]:
    if len(rows) < 2 or len({x for x, _ in rows}) < 2:
        raise CalibrationError(f"{label} requires at least two distinct calibration points")
    xbar = statistics.fmean(x for x, _ in rows)
    ybar = statistics.fmean(y for _, y in rows)
    denominator = sum((x - xbar) ** 2 for x, _ in rows)
    slope = sum((x - xbar) * (y - ybar) for x, y in rows) / denominator
    intercept = ybar - slope * xbar
    if slope <= 0 or intercept < 0:
        raise CalibrationError(f"{label} fit is non-physical")
    return intercept, slope


def _artifact_entry(
    pack_root: Path,
    name: str,
    spec: dict[str, Any],
    allow_synthetic_test_fixture: bool,
) -> tuple[Path, dict[str, Any]]:
    path = _resolve(pack_root, spec.get("artifact"), f"data_split.{name}.artifact")
    result = _load(path)
    expected_hash = spec.get("sha256")
    if expected_hash and _sha256(path) != expected_hash:
        raise CalibrationError(f"sha256 mismatch for split {name}")
    measured = result.get("status") == "measured" and result.get("evidence") == "measured"
    synthetic = (
        allow_synthetic_test_fixture
        and result.get("status") == "synthetic-test-fixture"
        and result.get("evidence") == "synthetic-unit-test"
        and result.get("hardware_validation") is False
    )
    if not measured and not synthetic:
        raise CalibrationError(f"split {name} is not measured hardware evidence")
    if result.get("split") != name:
        raise CalibrationError(
            f"split label mismatch: manifest={name}, artifact={result.get('split')}"
        )
    raw = result.get("raw_benchmarks")
    if not isinstance(raw, list) or not raw:
        raise CalibrationError(f"split {name} lacks raw_benchmarks")
    profiler = _resolve(path.parent, result.get("raw_profiler_output"), f"{name} raw profiler")
    if not profiler.is_file():
        raise CalibrationError(f"split {name} lacks raw profiler data: {profiler}")
    return path, result


def load_evidence(
    pack_path: Path, *, allow_synthetic_test_fixture: bool = False
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Load a pack, fail fast on missing evidence, and reject split overlap."""
    pack_path = pack_path.resolve()
    pack = _load(pack_path)
    if pack.get("schema_version") != "calibration-pack-v1":
        raise CalibrationError("unsupported calibration pack schema")
    contract = pack.get("measurement_contract")
    if not isinstance(contract, dict):
        raise CalibrationError("measurement_contract is required")
    environment = _resolve(
        pack_path.parent, contract.get("environment_manifest"), "environment_manifest"
    )
    env_data = _load(environment)
    required_environment = ("hardware", "driver", "cuda", "runtime")
    missing_environment = [key for key in required_environment if not env_data.get(key)]
    if missing_environment:
        raise CalibrationError(
            "environment manifest missing: " + ", ".join(missing_environment)
        )

    split_specs = pack.get("data_split")
    required_splits = (
        "calibration",
        "validation",
        "holdout_workload",
        "holdout_platform",
    )
    if not isinstance(split_specs, dict):
        raise CalibrationError("data_split is required")
    results: dict[str, dict[str, Any]] = {}
    seen_ids: dict[str, str] = {}
    seen_artifacts: dict[Path, str] = {}
    for name in required_splits:
        if not isinstance(split_specs.get(name), dict):
            raise CalibrationError(f"missing split declaration: {name}")
        path, result = _artifact_entry(
            pack_path.parent, name, split_specs[name], allow_synthetic_test_fixture
        )
        if path in seen_artifacts:
            raise CalibrationError(
                f"split leakage: {name} reuses {seen_artifacts[path]} artifact"
            )
        seen_artifacts[path] = name
        for record in result["raw_benchmarks"]:
            record_id = record.get("record_id")
            if not isinstance(record_id, str) or not record_id:
                raise CalibrationError(f"split {name} has raw record without record_id")
            if record_id in seen_ids:
                raise CalibrationError(
                    f"split leakage: record_id {record_id} occurs in "
                    f"{seen_ids[record_id]} and {name}"
                )
            seen_ids[record_id] = name
        results[name] = result
    return pack, results


def fit_parameters(calibration_result: dict[str, Any]) -> dict[str, Any]:
    """Fit all parameter families from the calibration artifact only."""
    rows_by_role: dict[str, list[tuple[dict[str, Any], float]]] = defaultdict(list)
    for record in calibration_result["raw_benchmarks"]:
        role = record.get("calibration_role")
        if role:
            if role not in FIT_ROLES:
                raise CalibrationError(f"unknown calibration_role: {role}")
            rows_by_role[role].append((record, _mean_ms(record)))
    missing = sorted(FIT_ROLES - rows_by_role.keys())
    if missing:
        raise CalibrationError("calibration probes missing roles: " + ", ".join(missing))

    cpu_ms = statistics.fmean(value for _, value in rows_by_role["cpu_runtime"])
    gpu_groups: dict[str, list[float]] = defaultdict(list)
    for row, value in rows_by_role["gpu_service"]:
        operation = row.get("operation")
        if not operation:
            raise CalibrationError("gpu_service probe lacks operation")
        gpu_groups[str(operation)].append(value)
    gpu_service = {
        operation: statistics.fmean(values) for operation, values in sorted(gpu_groups.items())
    }

    pcie: dict[str, dict[str, float]] = {}
    pcie_rows: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for row, value in rows_by_role["pcie_transfer"]:
        if int(row.get("copy_streams", 1)) != 1:
            raise CalibrationError("pcie_transfer baseline probes must use one copy stream")
        pcie_rows[str(row.get("direction"))].append((float(row["bytes"]), value))
    for direction in ("h2d", "d2h"):
        intercept, slope = _line_fit(pcie_rows.get(direction, []), f"PCIe {direction}")
        pcie[direction] = {
            "intercept_ms": intercept,
            "bandwidth_bytes_per_ms": 1.0 / slope,
        }

    stream_factors: dict[str, list[float]] = defaultdict(list)
    for row, value in rows_by_role["copy_engine"]:
        direction = str(row.get("direction"))
        streams = str(int(row["copy_streams"]))
        base = pcie[direction]["intercept_ms"] + (
            float(row["bytes"]) / pcie[direction]["bandwidth_bytes_per_ms"]
        )
        stream_factors[streams].append(value / base)
    copy_engine = {
        "stream_latency_factors": {
            "1": 1.0,
            **{
                streams: statistics.fmean(values)
                for streams, values in sorted(stream_factors.items())
            },
        }
    }

    memory_intercept, memory_slope = _line_fit(
        [(float(row["bytes"]), value) for row, value in rows_by_role["memory"]],
        "memory",
    )
    queue_intercept, queue_slope = _line_fit(
        [
            (float(row["queue_depth"]), value)
            for row, value in rows_by_role["queueing"]
        ],
        "queueing",
    )
    contention_slopes = []
    for row, value in rows_by_role["contention"]:
        concurrency = int(row["concurrency"])
        base_ms = float(row["base_service_ms"])
        if concurrency <= 1 or base_ms <= 0:
            raise CalibrationError("contention probes require concurrency > 1 and base_service_ms")
        contention_slopes.append((value / base_ms - 1.0) / (concurrency - 1))
    contention_slope = statistics.fmean(contention_slopes)
    if contention_slope < 0:
        raise CalibrationError("contention fit is non-physical")

    return {
        "schema_version": "calibrated-backend-parameters-v1",
        "fit_split": "calibration",
        "cpu_runtime": {"per_call_ms": cpu_ms},
        "gpu_service": {"operation_ms": gpu_service},
        "pcie": pcie,
        "copy_engine": copy_engine,
        "memory": {
            "intercept_ms": memory_intercept,
            "bandwidth_bytes_per_ms": 1.0 / memory_slope,
        },
        "queueing": {
            "intercept_ms": queue_intercept,
            "per_queue_entry_ms": queue_slope,
        },
        "contention": {"per_extra_concurrency": contention_slope},
    }


def _component_latency(features: dict[str, Any], params: dict[str, Any]) -> float:
    total = int(features.get("cpu_calls", 0)) * params["cpu_runtime"]["per_call_ms"]
    for operation, count in features.get("gpu_operations", {}).items():
        try:
            total += float(count) * params["gpu_service"]["operation_ms"][operation]
        except KeyError as exc:
            raise CalibrationError(f"operation outside calibrated domain: {operation}") from exc
    memory_bytes = float(features.get("memory_bytes", 0))
    if memory_bytes:
        memory = params["memory"]
        total += memory["intercept_ms"] + memory_bytes / memory["bandwidth_bytes_per_ms"]
    depth = float(features.get("queue_depth", 0))
    if depth:
        queue = params["queueing"]
        total += queue["intercept_ms"] + depth * queue["per_queue_entry_ms"]
    concurrency = int(features.get("concurrency", 1))
    return total * (
        1.0 + max(0, concurrency - 1) * params["contention"]["per_extra_concurrency"]
    )


def _pcie_latency(features: dict[str, Any], params: dict[str, Any]) -> float:
    direction = str(features["direction"])
    model = params["pcie"][direction]
    streams = str(int(features.get("copy_streams", 1)))
    try:
        factor = params["copy_engine"]["stream_latency_factors"][streams]
    except KeyError as exc:
        raise CalibrationError(f"copy stream count outside calibrated domain: {streams}") from exc
    return (
        model["intercept_ms"] + float(features["bytes"]) / model["bandwidth_bytes_per_ms"]
    ) * factor


def predict(metric: str, features: dict[str, Any], params: dict[str, Any]) -> float:
    if metric == "component_latency":
        return _component_latency(features, params)
    if metric == "pcie_transfer_latency":
        return _pcie_latency(features, params)
    if metric in {"moe_replay_tpot", "moe_replay_throughput"}:
        tokens = int(features["tokens"])
        if tokens <= 0:
            raise CalibrationError("MoE replay tokens must be positive")
        total_ms = _component_latency(features, params)
        for transfer in features.get("transfers", []):
            total_ms += _pcie_latency(transfer, params)
        tpot = total_ms / tokens
        return tpot if metric == "moe_replay_tpot" else 1000.0 / tpot
    raise CalibrationError(f"unknown metric: {metric}")


def evaluate_split(
    result: dict[str, Any],
    params: dict[str, Any],
    split: str,
    mape_threshold_pct: float = 15.0,
    point_threshold_pct: float = 20.0,
) -> dict[str, Any]:
    points = result.get("evaluation_points")
    if not isinstance(points, list) or not points:
        raise CalibrationError(f"split {split} lacks evaluation_points")
    scored: list[dict[str, Any]] = []
    for point in points:
        metric = point.get("metric")
        if metric not in METRICS:
            raise CalibrationError(f"split {split} has unknown metric: {metric}")
        measured = float(point["measured"])
        if measured <= 0 or not math.isfinite(measured):
            raise CalibrationError("evaluation measurements must be finite and positive")
        predicted = predict(metric, point.get("features", {}), params)
        ape = abs(predicted - measured) / measured * 100.0
        scored.append(
            {
                "point_id": point["point_id"],
                "split": split,
                "metric": metric,
                "predicted": predicted,
                "measured": measured,
                "ape_pct": ape,
                "point_pass": ape <= point_threshold_pct,
                "domain": point.get("domain", {}),
            }
        )
    reports: dict[str, dict[str, Any]] = {}
    for metric in METRICS:
        rows = [row for row in scored if row["metric"] == metric]
        if not rows:
            raise CalibrationError(
                f"split {split} has no evaluation points for required metric: {metric}"
            )
        mape = statistics.fmean(row["ape_pct"] for row in rows)
        reports[metric] = {
            "mape_pct": mape,
            "mape_pass": mape <= mape_threshold_pct,
            "point_pass": all(row["point_pass"] for row in rows),
            "rows": rows,
            "over_threshold_points": [
                row for row in rows if not row["point_pass"]
            ],
        }
    mape_ok = all(report["mape_pass"] for report in reports.values())
    points_ok = all(row["point_pass"] for row in scored)
    status = "pass" if mape_ok and points_ok else ("partial" if mape_ok else "fail")
    return {
        "split": split,
        "status": status,
        "thresholds_pct": {
            "mape": mape_threshold_pct,
            "single_point_ape": point_threshold_pct,
        },
        "metrics": reports,
        "points": scored,
    }


def evaluate(
    results: dict[str, dict[str, Any]],
    params: dict[str, Any],
    mape_threshold_pct: float = 15.0,
    point_threshold_pct: float = 20.0,
) -> dict[str, Any]:
    split_assessments = {
        split: evaluate_split(
            results[split],
            params,
            split,
            mape_threshold_pct,
            point_threshold_pct,
        )
        for split in ("validation", "holdout_workload", "holdout_platform")
    }
    all_points = [
        row
        for assessment in split_assessments.values()
        for row in assessment["points"]
    ]
    reports: dict[str, dict[str, Any]] = {}
    for metric in METRICS:
        rows = [row for row in all_points if row["metric"] == metric]
        mape = statistics.fmean(row["ape_pct"] for row in rows)
        reports[metric] = {
            "mape_pct": mape,
            "mape_pass": mape <= mape_threshold_pct,
            "rows": rows,
            "over_threshold_points": [
                row for row in rows if not row["point_pass"]
            ],
        }
    mape_ok = all(report["mape_pass"] for report in reports.values()) and all(
        report["mape_pass"]
        for assessment in split_assessments.values()
        for report in assessment["metrics"].values()
    )
    points_ok = all(row["point_pass"] for row in all_points)
    status = "pass" if mape_ok and points_ok else ("partial" if mape_ok else "fail")
    accepted = [row for row in all_points if row["point_pass"]]
    domain_keys = sorted(
        {key for row in accepted for key in row.get("domain", {}).keys()}
    )
    calibrated_domain = {
        "included_point_ids": [row["point_id"] for row in accepted],
        "excluded_point_ids": [row["point_id"] for row in all_points if not row["point_pass"]],
        "dimensions": {
            key: sorted(
                {row["domain"][key] for row in accepted if key in row["domain"]},
                key=str,
            )
            for key in domain_keys
        },
    }
    return {
        "status": status,
        "thresholds_pct": {
            "mape": mape_threshold_pct,
            "single_point_ape": point_threshold_pct,
        },
        "split_assessments": split_assessments,
        "split_metrics": {
            split: assessment["metrics"]
            for split, assessment in split_assessments.items()
        },
        "metrics": reports,
        "calibrated_domain": calibrated_domain,
    }


def calibrate(
    pack_path: Path, *, allow_synthetic_test_fixture: bool = False
) -> dict[str, Any]:
    pack, results = load_evidence(
        pack_path, allow_synthetic_test_fixture=allow_synthetic_test_fixture
    )
    params = fit_parameters(results["calibration"])
    assessment = evaluate(results, params)
    evidence = (
        "synthetic-unit-test" if allow_synthetic_test_fixture else "measured"
    )
    return {
        "schema_version": "calibrated-backend-report-v1",
        "pack_id": pack["pack_id"],
        "evidence": evidence,
        "hardware_validation": not allow_synthetic_test_fixture,
        "metric_scope": "MoE-replay; not full-model TPOT/throughput",
        "parameters": params,
        **assessment,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calibration-pack", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--allow-synthetic-test-fixture",
        action="store_true",
        help="accept explicitly marked synthetic unit-test inputs; never hardware validation",
    )
    args = parser.parse_args(argv)
    report = calibrate(
        args.calibration_pack,
        allow_synthetic_test_fixture=args.allow_synthetic_test_fixture,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

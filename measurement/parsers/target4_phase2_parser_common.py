"""Strict common validation for the three target_4 Phase 2 probe schemas."""

from __future__ import annotations

import math
import statistics
from typing import Any

from measurement.parsers.common import (
    ValidationError,
    require_equal,
    require_key,
    require_list,
    require_mapping,
    require_nonneg_int,
    require_positive_int,
)
from measurement.parsers.component_eval_parser import validate as validate_component_points
from measurement.probes.target4_phase2_common import DIRECT_DERIVATION


FORMAL_REPEATS = 5
FORMAL_WARMUP = 10
FORMAL_MINIMUM_INNER_SECONDS = 1.0
FORMAL_T95_DF4 = 2.776
EXPECTED_TORCH_VERSION = "2.11.0+cu130"
EXPECTED_TORCH_CUDA_VERSION = "13.0"
EXPECTED_SEED = 20260718


def _positive_number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{where}: expected number")
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValidationError(f"{where}: expected finite positive number, got {value}")
    return value


def _nonnegative_number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{where}: expected number")
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValidationError(
            f"{where}: expected finite non-negative number, got {value}"
        )
    return value


def _finite_number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{where}: expected number")
    value = float(value)
    if not math.isfinite(value):
        raise ValidationError(f"{where}: expected finite number, got {value}")
    return value


def _require_close(actual: float, expected: float, where: str) -> None:
    if not math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-12):
        raise ValidationError(f"{where}: {actual} != recomputed {expected}")


def _validate_runtime_identity(identity: dict[str, Any], backend: str) -> None:
    require_equal(require_key(identity, "backend", "runtime_identity"), backend,
                  "runtime_identity.backend")
    if backend == "mock":
        runtime = require_key(identity, "runtime", "runtime_identity")
        if not isinstance(runtime, str) or not runtime.strip():
            raise ValidationError("runtime_identity.runtime must be a nonempty string")
        return

    require_equal(
        require_key(identity, "torch_version", "runtime_identity"),
        EXPECTED_TORCH_VERSION,
        "runtime_identity.torch_version",
    )
    require_equal(
        require_key(identity, "torch_cuda_version", "runtime_identity"),
        EXPECTED_TORCH_CUDA_VERSION,
        "runtime_identity.torch_cuda_version",
    )
    device_name = require_key(identity, "device_name", "runtime_identity")
    if not isinstance(device_name, str) or "RTX PRO 6000" not in device_name:
        raise ValidationError(
            "runtime_identity.device_name must identify NVIDIA RTX PRO 6000"
        )
    require_positive_int(
        require_key(identity, "device_total_memory_bytes", "runtime_identity"),
        "runtime_identity.device_total_memory_bytes",
    )
    require_equal(
        require_key(identity, "activation_dtype", "runtime_identity"),
        "bfloat16",
        "runtime_identity.activation_dtype",
    )
    require_equal(
        require_key(identity, "seed", "runtime_identity"),
        EXPECTED_SEED,
        "runtime_identity.seed",
    )


def validate_base(
    result: Any,
    *,
    probe_schema: str,
    expected_cells: int | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Validate no-join structure and one-to-one raw/point lineage."""
    root = require_mapping(result, "result")
    require_equal(
        require_key(root, "schema_version", "result"),
        "gpu-benchmark-result-v1",
        "schema_version",
    )
    require_equal(
        require_key(root, "probe_schema_version", "result"),
        probe_schema,
        "probe_schema_version",
    )
    repeats = require_positive_int(require_key(root, "repeats", "result"), "repeats")
    require_equal(repeats, FORMAL_REPEATS, "repeats")
    warmup = require_nonneg_int(require_key(root, "warmup", "result"), "warmup")
    require_equal(warmup, FORMAL_WARMUP, "warmup")
    minimum_inner_seconds = _positive_number(
        require_key(root, "minimum_inner_seconds", "result"),
        "minimum_inner_seconds",
    )
    _require_close(
        minimum_inner_seconds,
        FORMAL_MINIMUM_INNER_SECONDS,
        "minimum_inner_seconds",
    )
    backend = require_key(root, "backend", "result")
    evidence = require_key(root, "evidence", "result")
    expected_evidence = {
        "mock": "cpu_smoke_test_not_measurement",
        "gpu": "measured",
    }
    if backend not in expected_evidence:
        raise ValidationError(f"backend: unregistered value {backend!r}")
    require_equal(
        evidence,
        expected_evidence[backend],
        "evidence (must match backend; mock may never masquerade as measured)",
    )
    require_equal(
        require_key(root, "operand_shape_serialization", "result"),
        "structured_fields_only_no_case_string",
        "operand_shape_serialization",
    )
    runtime_identity = require_mapping(
        require_key(root, "runtime_identity", "result"), "runtime_identity"
    )
    _validate_runtime_identity(runtime_identity, backend)
    exact_argv = require_list(require_key(root, "exact_argv", "result"), "exact_argv")
    if not exact_argv or not all(
        isinstance(value, str) and value for value in exact_argv
    ):
        raise ValidationError("exact_argv must be a nonempty list of nonempty strings")

    rows = require_list(require_key(root, "raw_benchmarks", "result"), "raw_benchmarks")
    points = require_list(require_key(root, "evaluation_points", "result"), "evaluation_points")
    if not rows:
        raise ValidationError("raw_benchmarks must not be empty")
    if len(rows) != len(points):
        raise ValidationError(
            f"raw/point count mismatch: {len(rows)} raw != {len(points)} points"
        )
    if expected_cells is not None and len(rows) != expected_cells:
        raise ValidationError(
            f"incomplete formal grid: expected {expected_cells} cells, got {len(rows)}"
        )

    # Reuse the frozen consumer's strongest GAP-4 rule.  This does not replace
    # the probe-specific checks below; it proves downstream compatibility.
    validate_component_points(root, enforce_gap4=True)

    by_source: dict[str, dict[str, Any]] = {}
    record_ids: set[str] = set()
    for index, raw in enumerate(rows):
        where = f"raw_benchmarks[{index}]"
        raw = require_mapping(raw, where)
        if "case" in raw:
            raise ValidationError(
                f"{where}.case is forbidden: new probes must carry axes structurally"
            )
        record_id = require_key(raw, "record_id", where)
        if not isinstance(record_id, str) or not record_id:
            raise ValidationError(f"{where}.record_id must be a nonempty string")
        if record_id in record_ids:
            raise ValidationError(f"duplicate raw record_id {record_id!r}")
        record_ids.add(record_id)
        require_equal(
            require_key(raw, "operand_shape_source", where),
            DIRECT_DERIVATION,
            f"{where}.operand_shape_source",
        )
        samples = require_list(require_key(raw, "repeats_ms", where), f"{where}.repeats_ms")
        if len(samples) != FORMAL_REPEATS:
            raise ValidationError(
                f"{where}.repeats_ms must contain n={FORMAL_REPEATS} samples"
            )
        require_equal(
            require_nonneg_int(require_key(raw, "warmup", where), f"{where}.warmup"),
            FORMAL_WARMUP,
            f"{where}.warmup",
        )
        require_equal(
            require_positive_int(
                require_key(raw, "outer_repeats", where), f"{where}.outer_repeats"
            ),
            FORMAL_REPEATS,
            f"{where}.outer_repeats",
        )
        require_positive_int(
            require_key(raw, "inner_iterations", where), f"{where}.inner_iterations"
        )
        raw_minimum = _positive_number(
            require_key(raw, "minimum_inner_seconds", where),
            f"{where}.minimum_inner_seconds",
        )
        _require_close(
            raw_minimum,
            FORMAL_MINIMUM_INNER_SECONDS,
            f"{where}.minimum_inner_seconds",
        )
        implementation = require_key(raw, "implementation", where)
        if not isinstance(implementation, str) or not implementation.strip():
            raise ValidationError(f"{where}.implementation must be a nonempty string")
        sample_values = [
            _positive_number(value, f"{where}.repeats_ms[{sample_index}]")
            for sample_index, value in enumerate(samples)
        ]
        stats = require_mapping(require_key(raw, "statistics", where), f"{where}.statistics")
        require_equal(
            require_positive_int(
                require_key(stats, "n", f"{where}.statistics"),
                f"{where}.statistics.n",
            ),
            FORMAL_REPEATS,
            f"{where}.statistics.n",
        )
        require_equal(
            require_key(stats, "unit", f"{where}.statistics"),
            "ms",
            f"{where}.statistics.unit",
        )
        require_equal(
            require_key(stats, "ci_method", f"{where}.statistics"),
            "two-sided Student-t",
            f"{where}.statistics.ci_method",
        )
        mean = _positive_number(require_key(stats, "mean", f"{where}.statistics"),
                                f"{where}.statistics.mean")
        actual_mean = statistics.fmean(sample_values)
        _require_close(mean, actual_mean, f"{where}.statistics.mean")
        variance = _nonnegative_number(
            require_key(stats, "variance", f"{where}.statistics"),
            f"{where}.statistics.variance",
        )
        actual_variance = statistics.variance(sample_values)
        _require_close(variance, actual_variance, f"{where}.statistics.variance")
        stdev = _nonnegative_number(
            require_key(stats, "stdev", f"{where}.statistics"),
            f"{where}.statistics.stdev",
        )
        _require_close(stdev, math.sqrt(actual_variance), f"{where}.statistics.stdev")
        ci95 = require_list(
            require_key(stats, "ci95", f"{where}.statistics"),
            f"{where}.statistics.ci95",
        )
        if len(ci95) != 2:
            raise ValidationError(f"{where}.statistics.ci95 must contain two bounds")
        ci_values = [
            _finite_number(value, f"{where}.statistics.ci95[{index}]")
            for index, value in enumerate(ci95)
        ]
        half_width = FORMAL_T95_DF4 * math.sqrt(
            actual_variance / FORMAL_REPEATS
        )
        expected_ci = [actual_mean - half_width, actual_mean + half_width]
        for index in range(2):
            _require_close(
                ci_values[index], expected_ci[index],
                f"{where}.statistics.ci95[{index}]",
            )

    point_ids: set[str] = set()
    for index, point in enumerate(points):
        where = f"evaluation_points[{index}]"
        point = require_mapping(point, where)
        point_id = require_key(point, "point_id", where)
        if not isinstance(point_id, str) or not point_id:
            raise ValidationError(f"{where}.point_id must be a nonempty string")
        if point_id in point_ids:
            raise ValidationError(f"duplicate point_id {point_id!r}")
        point_ids.add(point_id)
        source = require_key(point, "source_record_id", where)
        if source not in record_ids:
            raise ValidationError(f"{where} references unknown source_record_id {source!r}")
        if source in by_source:
            raise ValidationError(f"multiple evaluation points reference {source!r}")
        require_equal(
            require_key(point, "derivation", where),
            DIRECT_DERIVATION,
            f"{where}.derivation",
        )
        by_source[source] = point

    if set(by_source) != record_ids:
        raise ValidationError("not every raw record has exactly one evaluation point")
    return root, rows, by_source


def validate_row_point_match(
    raw: dict[str, Any],
    point: dict[str, Any],
    *,
    feature_names: tuple[str, ...],
) -> None:
    features = require_mapping(point["features"], "point.features")
    for name in feature_names:
        require_equal(
            require_key(features, name, "point.features"),
            require_key(raw, name, "raw"),
            f"point.features.{name}",
        )
    if not math.isclose(
        float(point["measured"]),
        float(raw["statistics"]["mean"]),
        rel_tol=1e-12,
        abs_tol=1e-12,
    ):
        raise ValidationError("evaluation point measured value differs from raw mean")

#!/usr/bin/env python3
"""Parser + validator for gpu-dispatch-inserving-result-v1 (priority 1 output).

Enforces the dispatch byte-accounting invariants:

    dispatch_bytes == 2 * move_granularity_bytes   (gather in + scatter out)
    expert_tokens == routing_width * concurrency    (per decode step)
    total_dispatch_bytes == sum(per-step dispatch_bytes)

A mis-shaped record raises rather than being skipped.
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path
from typing import Any

try:
    from measurement.parsers.common import (
        ValidationError, load_json, require_mapping, require_list, require_key,
        require_equal, require_positive_int, require_nonneg_int, require_type,
    )
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from measurement.parsers.common import (
        ValidationError, load_json, require_mapping, require_list, require_key,
        require_equal, require_positive_int, require_nonneg_int, require_type,
    )

SCHEMA = "gpu-dispatch-inserving-result-v1"
FORMAL_CONCURRENCY = [1, 2, 4, 8]
FORMAL_WINDOWS = 3
FORMAL_STEPS_PER_WINDOW = 128
FORMAL_ROUTING_WIDTH = 8
MODEL_HIDDEN_SIZE = 4096
ACTIVATION_DTYPE_BYTES = 2
INSTRUMENTATION_OVERHEAD_LIMIT = 0.05
MEASUREMENT_REFUSED_EVIDENCE = "measurement_refused_not_measurement"
DECOMPOSITION_FIELDS = ["T_prepare_ns", "T_queue_ns", "T_sync_ns", "T_move_ns"]


def _finite_number(value: Any, where: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{where}: expected a finite number")
    number = float(value)
    if not math.isfinite(number) or (positive and number <= 0):
        relation = "positive finite" if positive else "finite"
        raise ValidationError(f"{where}: expected {relation} number, got {value!r}")
    return number


def _validate_instrumentation_guard(identity: dict[str, Any]) -> None:
    """Require the frozen paired control proving instrumentation overhead <=5%."""
    guard = require_mapping(
        require_key(identity, "instrumentation_perturbation", "runtime_identity"),
        "runtime_identity.instrumentation_perturbation",
    )
    where = "runtime_identity.instrumentation_perturbation"
    require_equal(
        require_key(guard, "method", where),
        "paired_uninstrumented_vs_instrumented_serving_latency",
        f"{where}.method",
    )
    sample_count = require_positive_int(
        require_key(guard, "sample_count", where), f"{where}.sample_count"
    )
    if sample_count < FORMAL_WINDOWS:
        raise ValidationError(
            f"{where}.sample_count: expected at least {FORMAL_WINDOWS}, got {sample_count}"
        )
    control = _finite_number(
        require_key(guard, "uninstrumented_control_latency_ns", where),
        f"{where}.uninstrumented_control_latency_ns",
        positive=True,
    )
    instrumented = _finite_number(
        require_key(guard, "instrumented_latency_ns", where),
        f"{where}.instrumented_latency_ns",
        positive=True,
    )
    reported = _finite_number(
        require_key(guard, "relative_overhead", where),
        f"{where}.relative_overhead",
    )
    computed = (instrumented - control) / control
    if not math.isclose(reported, computed, rel_tol=1e-12, abs_tol=1e-12):
        raise ValidationError(
            f"{where}.relative_overhead {reported} != recomputed {computed}"
        )
    threshold = _finite_number(
        require_key(guard, "threshold", where), f"{where}.threshold"
    )
    if not math.isclose(
        threshold, INSTRUMENTATION_OVERHEAD_LIMIT, rel_tol=0.0, abs_tol=1e-12
    ):
        raise ValidationError(
            f"{where}.threshold must be {INSTRUMENTATION_OVERHEAD_LIMIT}"
        )
    if reported > INSTRUMENTATION_OVERHEAD_LIMIT:
        raise ValidationError(
            f"{where}: instrumentation overhead {reported:.6f} exceeds 5%"
        )
    require_equal(require_key(guard, "status", where), "PASS", f"{where}.status")


def validate(result: Any) -> dict[str, Any]:
    root = require_mapping(result, "result")
    require_equal(require_key(root, "schema_version", "result"), SCHEMA, "schema_version")
    backend = require_key(root, "backend", "result")
    evidence = require_key(root, "evidence", "result")
    allowed_pairs = {
        ("mock_dispatch", "cpu_smoke_test_not_measurement"),
        ("vllm_dispatch", "measured"),
        ("vllm_dispatch", MEASUREMENT_REFUSED_EVIDENCE),
    }
    if (backend, evidence) not in allowed_pairs:
        raise ValidationError(
            "unregistered backend/evidence pair: "
            f"backend={backend!r}, evidence={evidence!r}"
        )
    measured = (backend, evidence) == ("vllm_dispatch", "measured")
    terminal_failure = evidence == MEASUREMENT_REFUSED_EVIDENCE
    identity: dict[str, Any] | None = None
    if measured or terminal_failure:
        identity = require_mapping(
            require_key(root, "runtime_identity", "result"), "runtime_identity"
        )
    if measured:
        assert identity is not None
        _validate_instrumentation_guard(identity)
        require_equal(
            require_key(root, "break_even_decomposition_fields", "result"),
            DECOMPOSITION_FIELDS,
            "break_even_decomposition_fields",
        )
        require_equal(
            require_key(root, "ir_evaluation_point_fields", "result"),
            "FILLED_PREP2",
            "ir_evaluation_point_fields",
        )
    groups = require_list(require_key(root, "groups", "result"), "groups")
    if terminal_failure:
        failure = require_mapping(
            require_key(root, "terminal_failure", "result"), "terminal_failure"
        )
        require_equal(require_key(failure, "status", "terminal_failure"), "FAIL",
                      "terminal_failure.status")
        for key in ("classification", "stage", "error"):
            value = require_key(failure, key, "terminal_failure")
            if not isinstance(value, str) or not value.strip():
                raise ValidationError(f"terminal_failure.{key}: expected nonempty string")
        if root.get("ir_evaluation_points") not in (None, []):
            raise ValidationError(
                "terminal refusal must not emit IR evaluation points as measurement"
            )
    elif root.get("terminal_failure") is not None:
        raise ValidationError("successful dispatch result carries terminal_failure")
    if not groups and not terminal_failure:
        raise ValidationError("groups: empty; dispatch probe must have concurrency groups")

    observed_concurrency: list[int] = []
    for gi, group in enumerate(groups):
        gw = f"groups[{gi}]"
        require_mapping(group, gw)
        concurrency = require_positive_int(require_key(group, "concurrency", gw), f"{gw}.concurrency")
        observed_concurrency.append(concurrency)
        per_step = require_list(require_key(group, "per_step", gw), f"{gw}.per_step")
        if not per_step:
            raise ValidationError(f"{gw}.per_step: empty")
        steps_instrumented = require_positive_int(
            require_key(group, "steps_instrumented", gw),
            f"{gw}.steps_instrumented",
        )
        if steps_instrumented != len(per_step):
            raise ValidationError(
                f"{gw}.steps_instrumented {steps_instrumented} != "
                f"len(per_step) {len(per_step)}"
            )
        if measured:
            require_equal(
                require_key(group, "serving_windows", gw),
                FORMAL_WINDOWS,
                f"{gw}.serving_windows",
            )
            require_equal(
                require_key(group, "steps_per_window", gw),
                FORMAL_STEPS_PER_WINDOW,
                f"{gw}.steps_per_window",
            )
            require_equal(
                steps_instrumented,
                FORMAL_WINDOWS * FORMAL_STEPS_PER_WINDOW,
                f"{gw}.steps_instrumented",
            )
        running_bytes = 0
        running_decisions = 0
        formal_step_coordinates: set[tuple[int, int]] = set()
        for si, step in enumerate(per_step):
            sw = f"{gw}.per_step[{si}]"
            require_mapping(step, sw)
            require_equal(require_key(step, "concurrency", sw), concurrency, f"{sw}.concurrency")
            expert_tokens = require_positive_int(require_key(step, "expert_tokens", sw), f"{sw}.expert_tokens")
            expected_expert_tokens = FORMAL_ROUTING_WIDTH * concurrency
            if expert_tokens != expected_expert_tokens:
                raise ValidationError(
                    f"{sw}: expert_tokens {expert_tokens} != routing_width(8)*"
                    f"concurrency {expected_expert_tokens}"
                )
            dispatch_bytes = require_positive_int(require_key(step, "dispatch_bytes", sw), f"{sw}.dispatch_bytes")
            gran = require_positive_int(require_key(step, "move_granularity_bytes", sw), f"{sw}.move_granularity_bytes")
            expected_granularity = (
                expert_tokens * MODEL_HIDDEN_SIZE * ACTIVATION_DTYPE_BYTES
            )
            if gran != expected_granularity:
                raise ValidationError(
                    f"{sw}: move_granularity_bytes {gran} != BF16 activation "
                    f"payload {expected_granularity}"
                )
            if dispatch_bytes != 2 * gran:
                raise ValidationError(
                    f"{sw}: dispatch_bytes {dispatch_bytes} != 2*granularity {2 * gran}"
                )
            require_nonneg_int(require_key(step, "control_decisions", sw), f"{sw}.control_decisions")
            require_positive_int(
                require_key(step, "dispatch_period_ns", sw), f"{sw}.dispatch_period_ns"
            )
            if measured:
                repeat_index = require_nonneg_int(
                    require_key(step, "repeat_index", sw), f"{sw}.repeat_index"
                )
                step_index = require_nonneg_int(
                    require_key(step, "step_index", sw), f"{sw}.step_index"
                )
                coordinate = (repeat_index, step_index)
                if coordinate in formal_step_coordinates:
                    raise ValidationError(f"{sw}: duplicate repeat/step coordinate {coordinate}")
                formal_step_coordinates.add(coordinate)
                for field in DECOMPOSITION_FIELDS:
                    require_nonneg_int(
                        require_key(step, field, sw), f"{sw}.{field}"
                    )
                source = require_mapping(
                    require_key(step, "measurement_source", sw),
                    f"{sw}.measurement_source",
                )
                require_type(
                    require_key(source, "worker_hook_observed", f"{sw}.measurement_source"),
                    bool,
                    f"{sw}.measurement_source.worker_hook_observed",
                )
                require_equal(
                    source["worker_hook_observed"],
                    True,
                    f"{sw}.measurement_source.worker_hook_observed",
                )
            running_bytes += dispatch_bytes
            running_decisions += step["control_decisions"]
        total_bytes = require_nonneg_int(require_key(group, "total_dispatch_bytes", gw), f"{gw}.total_dispatch_bytes")
        if total_bytes != running_bytes:
            raise ValidationError(
                f"{gw}: total_dispatch_bytes {total_bytes} != sum of steps {running_bytes}"
            )
        total_dec = require_nonneg_int(require_key(group, "total_control_decisions", gw), f"{gw}.total_control_decisions")
        if total_dec != running_decisions:
            raise ValidationError(
                f"{gw}: total_control_decisions {total_dec} != sum of steps {running_decisions}"
            )
        mean_dispatch_bytes = _finite_number(
            require_key(group, "mean_dispatch_bytes", gw),
            f"{gw}.mean_dispatch_bytes",
            positive=True,
        )
        expected_mean = running_bytes / len(per_step)
        if not math.isclose(
            mean_dispatch_bytes, expected_mean, rel_tol=1e-12, abs_tol=1e-12
        ):
            raise ValidationError(
                f"{gw}.mean_dispatch_bytes {mean_dispatch_bytes} != {expected_mean}"
            )
        if measured:
            expected_coordinates = {
                (repeat, step)
                for repeat in range(FORMAL_WINDOWS)
                for step in range(FORMAL_STEPS_PER_WINDOW)
            }
            if formal_step_coordinates != expected_coordinates:
                raise ValidationError(f"{gw}: incomplete formal repeat/step coordinate grid")
    if measured:
        require_equal(observed_concurrency, FORMAL_CONCURRENCY, "formal concurrency axis")
    if terminal_failure and observed_concurrency:
        expected_prefix = FORMAL_CONCURRENCY[:len(observed_concurrency)]
        require_equal(
            observed_concurrency, expected_prefix, "terminal failure concurrency prefix"
        )
    return root


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path")
    args = ap.parse_args(argv)
    root = validate(load_json(args.path))
    print(f"dispatch OK: {len(root['groups'])} groups, evidence={root['evidence']}")
    return 1 if root.get("terminal_failure") is not None else 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Expand one frozen benchmark matrix into isolated P0-P6 capture commands."""
from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import sys
from itertools import product
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))

from collectors.trace_contract import (  # noqa: E402
    MANDATORY_GATE_PASSES, PASSES, canonical_hash, write_json,
)

PASS_CONTRACT = {
    "P0": ("baseline", 5.0),
    "P1": ("timeline", 8.0),
    "P2": ("routing", 8.0),
    "P3": ("memory_transfer", 10.0),
    "P4": ("gpu_counters", 12.0),
    "P5": ("telemetry", 6.0),
    "P6": ("detailed_optional", 15.0),
}
PAID_SESSION_LIMIT_SECONDS = 120 * 60
STOP_DISPATCH_SECONDS = 105 * 60
AUDIT_RESERVE_SECONDS = 15 * 60


def _items(matrix: dict[str, Any], plural: str, singular: str) -> list[Any]:
    value = matrix.get(plural, matrix.get(singular))
    if not isinstance(value, list) or not value:
        raise ValueError(f"frozen matrix requires non-empty {plural}")
    return value


def _id(item: Any, *names: str) -> str:
    if isinstance(item, str) and item:
        return item
    if isinstance(item, dict):
        for name in names:
            value = item.get(name)
            if isinstance(value, str) and value:
                return value
    raise ValueError(f"matrix item lacks one of {names}: {item!r}")


def _benchmark_cases(matrix: dict[str, Any]) -> list[tuple[dict, dict]]:
    cases: list[tuple[dict, dict]] = []
    for benchmark in _items(matrix, "benchmarks", "benchmark"):
        if not isinstance(benchmark, dict):
            benchmark = {"benchmark_id": _id(benchmark, "benchmark_id", "id")}
        samples = benchmark.get("samples")
        if not isinstance(samples, list) or not samples:
            sample_id = benchmark.get("sample_id")
            if not sample_id:
                raise ValueError(
                    f"benchmark {_id(benchmark, 'benchmark_id', 'id')} has no samples"
                )
            samples = [{"sample_id": sample_id}]
        for sample in samples:
            if isinstance(sample, str):
                sample = {"sample_id": sample}
            if not isinstance(sample, dict):
                raise ValueError("benchmark samples must be strings or objects")
            _id(sample, "sample_id", "id")
            cases.append((benchmark, sample))
    return cases


def _generation_hash(configuration: Any) -> str:
    if isinstance(configuration, dict):
        supplied = configuration.get("generation_config_hash")
        if isinstance(supplied, str):
            if len(supplied) != 64 or any(c not in "0123456789abcdef" for c in supplied):
                raise ValueError("generation_config_hash must be lowercase SHA-256")
            return supplied
        payload = configuration.get("generation_config", configuration)
    else:
        payload = configuration
    return canonical_hash(payload)


def _adapter_path(matrix: dict[str, Any], pass_id: str, package_root: Path) -> Path:
    adapters = matrix.get("collector_adapters", {})
    configured = adapters.get(pass_id) if isinstance(adapters, dict) else None
    relative = configured or f"collectors/{PASS_CONTRACT[pass_id][0]}.py"
    path = Path(relative)
    return path if path.is_absolute() else package_root / path


def _command(matrix_path: Path, state: dict[str, Any], adapter: Path) -> str:
    arguments = [
        sys.executable, str(adapter), "--matrix", str(matrix_path),
        "--session-id", state["session_id"], "--gpu-id", state["gpu_id"],
        "--model-id", state["model_id"], "--model-revision",
        state["model_revision"], "--benchmark-id", state["benchmark_id"],
        "--sample-id", state["sample_id"], "--configuration-id",
        state["configuration_id"], "--profiler-pass", state["pass_id"],
        "--repetition-index", str(state["repetition_index"]),
    ]
    return " ".join(shlex.quote(value) for value in arguments)


def build_capture_plan(
    matrix: dict[str, Any], matrix_path: Path, package_root: Path = PACKAGE_ROOT,
) -> dict[str, Any]:
    """Build states only; it never launches profilers or claims capture completion."""
    if matrix.get("frozen") is not True:
        raise ValueError("matrix must declare frozen=true")
    session_id = matrix.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("frozen matrix requires session_id")
    repetitions = matrix.get("repetitions", matrix.get("required_repetitions"))
    if not isinstance(repetitions, int) or isinstance(repetitions, bool) or repetitions < 1:
        raise ValueError("repetitions must be a positive integer")
    gpus = _items(matrix, "gpus", "gpu")
    models = _items(matrix, "models", "model")
    configurations = _items(matrix, "configurations", "configuration")
    benchmark_cases = _benchmark_cases(matrix)
    passes = matrix.get("passes", list(PASSES))
    if (
        not isinstance(passes, list) or not passes
        or any(item not in PASSES for item in passes)
        or len(set(passes)) != len(passes)
    ):
        raise ValueError("passes must be a unique non-empty subset of P0-P6")
    missing_gates = sorted(MANDATORY_GATE_PASSES - set(passes))
    if missing_gates:
        raise ValueError(f"frozen matrix omits mandatory gates: {', '.join(missing_gates)}")

    states: list[dict[str, Any]] = []
    for gpu, model, case, configuration, pass_id, repetition in product(
        gpus, models, benchmark_cases, configurations, passes, range(repetitions)
    ):
        benchmark, sample = case
        model_id = _id(model, "model_id", "id")
        model_revision = (
            _id(model, "model_revision", "revision")
            if isinstance(model, dict) else model_id
        )
        state = {
            "session_id": session_id,
            "gpu_id": _id(gpu, "gpu_id", "id", "name"),
            "model_id": model_id,
            "model_revision": model_revision,
            "benchmark_id": _id(benchmark, "benchmark_id", "id"),
            "suite_id": _id(benchmark, "suite_id", "benchmark_id", "id"),
            "sample_id": _id(sample, "sample_id", "id"),
            "configuration_id": _id(configuration, "configuration_id", "id", "name"),
            "generation_config_hash": _generation_hash(configuration),
            "pass_id": pass_id,
            "repetition_index": repetition,
            "mandatory_gate": pass_id in MANDATORY_GATE_PASSES,
            "estimate_minutes": PASS_CONTRACT[pass_id][1],
        }
        state["state_id"] = hashlib.sha256(
            json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        adapter = _adapter_path(matrix, pass_id, package_root)
        state["collector_adapter"] = str(adapter)
        state["command"] = _command(matrix_path, state, adapter)
        state["dispatch_contract"] = {
            "clock": "monotonic",
            "session_monotonic_start_required": True,
            "latest_dispatch_elapsed_seconds": STOP_DISPATCH_SECONDS,
            "audit_reserve_seconds": AUDIT_RESERVE_SECONDS,
            "dispatch_at_or_after_deadline": "prohibited",
        }
        if adapter.is_file():
            state["status"] = "planned"
            state["blocked_reason"] = None
        else:
            state["status"] = "blocked"
            state["blocked_reason"] = f"collector adapter not implemented: {adapter}"
        states.append(state)
    blocked_count = sum(item["status"] == "blocked" for item in states)
    return {
        "schema_version": "benchmark-capture-plan-v1",
        "status": "no_go" if blocked_count else "planned",
        "execution_allowed": blocked_count == 0,
        "session_id": session_id,
        "frozen_matrix_path": str(matrix_path),
        "frozen_matrix_hash": canonical_hash(matrix),
        "profiler_concurrency": 1,
        "simultaneous_profilers_forbidden": True,
        "mandatory_gate_passes": sorted(MANDATORY_GATE_PASSES),
        "session_dispatch_contract": {
            "clock": "monotonic",
            "session_limit_seconds": PAID_SESSION_LIMIT_SECONDS,
            "stop_new_dispatch_elapsed_seconds": STOP_DISPATCH_SECONDS,
            "audit_package_reserve_seconds": AUDIT_RESERVE_SECONDS,
            "wall_clock_deadline_is_not_sufficient": True,
        },
        "state_count": len(states),
        "blocked_state_count": blocked_count,
        "estimated_minutes": sum(item["estimate_minutes"] for item in states),
        "states": states,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    matrix = json.loads(args.matrix.read_text(encoding="utf-8"))
    if not isinstance(matrix, dict):
        parser.error("matrix top level must be an object")
    try:
        plan = build_capture_plan(matrix, args.matrix.resolve())
    except ValueError as exc:
        parser.error(str(exc))
    write_json(args.output, plan)
    print(args.output)
    if plan["status"] == "no_go":
        print(
            "NO-GO: one or more collector adapters are missing; "
            "the plan is not executable or complete",
            file=sys.stderr,
        )
        return 20
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

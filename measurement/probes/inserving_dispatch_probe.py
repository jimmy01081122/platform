#!/usr/bin/env python3
"""In-serving MoE dispatch instrumentation (TRACK_GPU_PREP priority 1, A2).

The existing ``gather_scatter`` probe (benchmark.py:463) is a *same-device
synthetic proxy*: ``x.index_select(0, idx).index_select(0, inv)`` on a
``torch.randn`` tensor. It yields only the execute term and carries none of
``T_prepare / T_queue / T_sync / T_move`` (root spec §10.4). It does not need to
be rewritten; what is missing is the *system-level* movement and control
structure of dispatch inside a live serving loop.

This probe instruments, per decode step: how many bytes each dispatch moves, the
move granularity, how often dispatch happens, and how many control decisions
accompany it. Those quantities are physical and schema-independent, so they are
emitted directly. PREP-2 fixed their projection against CalibrationIR; a live
record is still accepted only when an auditable worker-side hook supplies every
decomposition field.

CPU smoke test: ``--backend mock_dispatch``. Live TRACK_GPU backend:
``--backend vllm_dispatch`` with an explicit worker-capable runtime adapter.
Result stamped
``evidence = "cpu_smoke_test_not_measurement"``. The bundled vLLM adapter fails
closed when the fused worker path cannot expose those terms separately; it never
substitutes parent-process wall time.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

try:
    from measurement.probes import SCHEMA_DISPATCH_INSERVING
    from measurement.probes.mock_backend import (
        BackendError,
        MockDispatchBackend,
        resolve_backend,
    )
    from measurement.probes.ir_evaluation_point import dispatch_result_to_points
    from measurement.probes.vllm_backend import (
        DispatchRuntimeConfig,
        VllmDispatchBackend,
    )
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from measurement.probes import SCHEMA_DISPATCH_INSERVING
    from measurement.probes.mock_backend import (
        BackendError,
        MockDispatchBackend,
        resolve_backend,
    )
    from measurement.probes.ir_evaluation_point import dispatch_result_to_points
    from measurement.probes.vllm_backend import (
        DispatchRuntimeConfig,
        VllmDispatchBackend,
    )

import hashlib


DEFAULT_CONCURRENCY = (1, 2, 4, 8)
MIN_IR_REPETITIONS = 3
INSTRUMENTATION_OVERHEAD_LIMIT = 0.05
MEASUREMENT_REFUSED_EVIDENCE = "measurement_refused_not_measurement"


def _finite_number(value: Any, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BackendError(f"{label} must be a finite number")
    number = float(value)
    if not math.isfinite(number) or (positive and number <= 0):
        relation = "positive finite" if positive else "finite"
        raise BackendError(f"{label} must be {relation}, got {value!r}")
    return number


def _validate_instrumentation_guard(identity: Any) -> None:
    """Fail closed unless a paired serving-latency guard proves <=5% overhead."""
    if not isinstance(identity, dict):
        raise BackendError("runtime identity is unavailable for instrumentation guard")
    guard = identity.get("instrumentation_perturbation")
    if not isinstance(guard, dict):
        raise BackendError(
            "runtime identity has no paired instrumentation_perturbation guard"
        )
    if guard.get("method") != (
        "paired_uninstrumented_vs_instrumented_serving_latency"
    ):
        raise BackendError("instrumentation guard has an unsupported comparison method")
    sample_count = guard.get("sample_count")
    if (
        isinstance(sample_count, bool)
        or not isinstance(sample_count, int)
        or sample_count < MIN_IR_REPETITIONS
    ):
        raise BackendError("instrumentation guard requires at least 3 paired samples")
    control = _finite_number(
        guard.get("uninstrumented_control_latency_ns"),
        "uninstrumented control latency",
        positive=True,
    )
    instrumented = _finite_number(
        guard.get("instrumented_latency_ns"),
        "instrumented latency",
        positive=True,
    )
    reported = _finite_number(
        guard.get("relative_overhead"), "reported instrumentation overhead"
    )
    computed = (instrumented - control) / control
    if not math.isclose(reported, computed, rel_tol=1e-12, abs_tol=1e-12):
        raise BackendError(
            f"instrumentation overhead {reported} disagrees with recomputed {computed}"
        )
    threshold = _finite_number(guard.get("threshold"), "instrumentation threshold")
    if not math.isclose(
        threshold, INSTRUMENTATION_OVERHEAD_LIMIT, rel_tol=0.0, abs_tol=1e-12
    ):
        raise BackendError("instrumentation threshold must remain frozen at 0.05")
    if reported > INSTRUMENTATION_OVERHEAD_LIMIT:
        raise BackendError(
            f"instrumentation overhead {reported:.6f} exceeds the frozen 5% limit"
        )
    if guard.get("status") != "PASS":
        raise BackendError("instrumentation guard status is not PASS")


def _runtime_preflight_failure(identity: Any) -> dict[str, Any] | None:
    """Turn an explicit adapter refusal/invalid perturbation guard into evidence."""
    if isinstance(identity, dict):
        capability = identity.get("measurement_capability")
        if isinstance(capability, dict) and capability.get("measurement_supported") is False:
            classification = capability.get("status")
            if not isinstance(classification, str) or not classification:
                classification = "TARGET_1_MEASUREMENT_REFUSED"
            return {
                "status": "FAIL",
                "classification": classification,
                "stage": "runtime_capability_preflight",
                "error": json.dumps(
                    {
                        "status": capability.get("status"),
                        "refused_fields": capability.get("refused_fields", []),
                        "reasons": capability.get("reasons", []),
                    },
                    sort_keys=True,
                ),
            }
    try:
        _validate_instrumentation_guard(identity)
    except BackendError as exc:
        return {
            "status": "FAIL",
            "classification": "INSTRUMENTATION_PERTURBATION_GUARD_FAILED",
            "stage": "instrumentation_perturbation_guard",
            "error": str(exc),
        }
    return None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--backend", required=True,
                   help="backend name (mock_dispatch for CPU smoke test)")
    p.add_argument("--concurrency", default=",".join(str(c) for c in DEFAULT_CONCURRENCY),
                   help="comma-separated concurrency values")
    p.add_argument("--steps", type=int, default=32,
                   help="decode steps to instrument per concurrency (>=1)")
    p.add_argument("--repeats", type=int, default=3,
                   help="independent serving windows per concurrency (>=1)")
    p.add_argument("--model-path",
                   help="vllm_dispatch only: absolute pinned model path")
    p.add_argument("--runtime-adapter-module",
                   help="vllm_dispatch only: worker-capable adapter module")
    p.add_argument("--out", required=True, help="output JSON path")
    p.add_argument("--pretty", action="store_true")
    return p.parse_args(argv)


def _concurrency(spec: str) -> list[int]:
    out: list[int] = []
    for tok in spec.split(","):
        tok = tok.strip()
        if not tok:
            continue
        val = int(tok)
        if val <= 0:
            raise SystemExit(f"concurrency must be positive: {val}")
        out.append(val)
    if not out:
        raise SystemExit("no concurrency values given")
    return out


def _build_backend(
    name: str,
    model_path: str | None = None,
    runtime_adapter_module: str | None = None,
):
    cls = resolve_backend(name)
    if cls is MockDispatchBackend:
        return cls()
    if cls is VllmDispatchBackend:
        if not model_path:
            raise BackendError("vllm_dispatch requires --model-path")
        if not runtime_adapter_module:
            raise BackendError("vllm_dispatch requires --runtime-adapter-module")
        return cls(
            config=DispatchRuntimeConfig(model_path=model_path),
            runtime_adapter_module=runtime_adapter_module,
        )
    raise BackendError(
        f"backend {name!r} is not runnable in TRACK_GPU_PREP; use mock_dispatch "
        "for the CPU smoke test. Real in-serving dispatch belongs to TRACK_GPU."
    )


def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.steps < 1:
        raise SystemExit("--steps must be >= 1")
    if args.repeats < MIN_IR_REPETITIONS:
        raise SystemExit(
            f"--repeats must be >= {MIN_IR_REPETITIONS}; CalibrationIR repetitions "
            "must be observed, never silently inflated"
        )
    concurrency_values = _concurrency(args.concurrency)
    backend = _build_backend(
        args.backend, args.model_path, args.runtime_adapter_module
    )
    is_mock = isinstance(backend, MockDispatchBackend)

    runtime_identity = getattr(backend, "runtime_identity", None)
    groups: list[dict[str, Any]] = []
    terminal_failure: dict[str, Any] | None = None
    try:
        if not is_mock:
            terminal_failure = _runtime_preflight_failure(runtime_identity)
        if terminal_failure is None:
            try:
                for concurrency in concurrency_values:
                    steps: list[dict[str, Any]] = []
                    for repeat_index in range(args.repeats):
                        measure_window = getattr(backend, "measure_window", None)
                        if callable(measure_window):
                            window = measure_window(
                                concurrency, args.steps, repeat_index
                            )
                        else:
                            window = [
                                {
                                    **backend.measure_step(i, concurrency),
                                    "repeat_index": repeat_index,
                                }
                                for i in range(args.steps)
                            ]
                        steps.extend(window)
                    total_bytes = sum(s["dispatch_bytes"] for s in steps)
                    total_decisions = sum(s["control_decisions"] for s in steps)
                    groups.append({
                        "concurrency": concurrency,
                        "serving_windows": args.repeats,
                        "steps_per_window": args.steps,
                        "steps_instrumented": len(steps),
                        "per_step": steps,
                        "total_dispatch_bytes": total_bytes,
                        "total_control_decisions": total_decisions,
                        "mean_dispatch_bytes": total_bytes / len(steps),
                    })
            except BackendError as exc:
                text = str(exc)
                terminal_failure = {
                    "status": "FAIL",
                    "classification": (
                        "TARGET_1_MEASUREMENT_REFUSED"
                        if "TARGET_1_MEASUREMENT_REFUSED" in text
                        else "BACKEND_MEASUREMENT_FAILURE"
                    ),
                    "stage": "measure_window",
                    "error": text,
                }
    finally:
        closer = getattr(backend, "close", None)
        if callable(closer):
            closer()

    runtime_variant_hash = hashlib.sha256(
        json.dumps({
            "backend": backend.name,
            "concurrency": concurrency_values,
            "steps": args.steps,
            "repeats": args.repeats,
            "runtime_config": getattr(backend, "runtime_config", None),
        }, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    result = {
        "schema_version": SCHEMA_DISPATCH_INSERVING,
        "target": "A2_moe_dispatch_data_movement",
        "backend": backend.name,
        "evidence": (
            MEASUREMENT_REFUSED_EVIDENCE
            if terminal_failure is not None
            else ("cpu_smoke_test_not_measurement" if is_mock else "measured")
        ),
        "argv": _reconstruct_argv(args),
        "runtime_variant_hash": runtime_variant_hash,
        "runtime_identity": runtime_identity,
        "note": (
            "system-level dispatch movement + control structure; complements the "
            "same-device execute-only gather_scatter proxy at benchmark.py:463"
        ),
        "groups": groups,
        "terminal_failure": terminal_failure,
    }
    # PREP-2: the break-even decomposition (T_prepare/T_queue/T_sync/T_move,
    # root spec 10.4) is now emitted per step by the backend, and the IR
    # evaluation-point fields are FIXED against STAGE_A2's CalibrationIR schema.
    # Each point carries the operand-shape coordinate [expert_tokens, concurrency]
    # DIRECTLY, so the IR pipeline needs no join to raw (closes the GAP-4 class).
    result["break_even_decomposition_fields"] = [
        "T_prepare_ns", "T_queue_ns", "T_sync_ns", "T_move_ns"]
    result["ir_evaluation_point_schema"] = "CalibrationIR"
    if terminal_failure is None:
        result["ir_evaluation_point_fields"] = "FILLED_PREP2"
        result["ir_evaluation_points"] = dispatch_result_to_points(result)
    else:
        result["ir_evaluation_point_fields"] = "TERMINAL_FAILURE_NO_POINTS"
        result["ir_evaluation_points"] = []
    return result


def _reconstruct_argv(args: argparse.Namespace) -> list[str]:
    argv = ["--backend", args.backend, "--concurrency", args.concurrency,
            "--steps", str(args.steps), "--repeats", str(args.repeats),
            "--out", args.out]
    if args.model_path:
        argv += ["--model-path", args.model_path]
    if args.runtime_adapter_module:
        argv += ["--runtime-adapter-module", args.runtime_adapter_module]
    return argv


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = run(args)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(result, indent=2 if args.pretty else None, sort_keys=True)
        + "\n"
    )
    print(
        f"inserving_dispatch_probe: backend={result['backend']} "
        f"evidence={result['evidence']} groups={len(result['groups'])} -> {out_path}"
    )
    if result.get("terminal_failure") is not None:
        print(
            "inserving_dispatch_probe: terminal failure retained in output: "
            + result["terminal_failure"]["classification"],
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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
emitted directly. The projection of these into A2's IR evaluation points is
schema-dependent and therefore left PENDING_A2 (hard rule 5).

CPU smoke test: ``--backend mock_dispatch``. Live TRACK_GPU backend:
``--backend vllm_dispatch`` with an explicit worker-capable runtime adapter.
Result stamped
``evidence = "cpu_smoke_test_not_measurement"``.
"""

from __future__ import annotations

import argparse
import json
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
    if args.repeats < 1:
        raise SystemExit("--repeats must be >= 1")
    concurrency_values = _concurrency(args.concurrency)
    backend = _build_backend(
        args.backend, args.model_path, args.runtime_adapter_module
    )
    is_mock = isinstance(backend, MockDispatchBackend)

    groups: list[dict[str, Any]] = []
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
            "cpu_smoke_test_not_measurement" if is_mock else "measured"
        ),
        "argv": _reconstruct_argv(args),
        "runtime_variant_hash": runtime_variant_hash,
        "runtime_identity": getattr(backend, "runtime_identity", None),
        "note": (
            "system-level dispatch movement + control structure; complements the "
            "same-device execute-only gather_scatter proxy at benchmark.py:463"
        ),
        "groups": groups,
    }
    # PREP-2: the break-even decomposition (T_prepare/T_queue/T_sync/T_move,
    # root spec 10.4) is now emitted per step by the backend, and the IR
    # evaluation-point fields are FIXED against STAGE_A2's CalibrationIR schema.
    # Each point carries the operand-shape coordinate [expert_tokens, concurrency]
    # DIRECTLY, so the IR pipeline needs no join to raw (closes the GAP-4 class).
    result["break_even_decomposition_fields"] = [
        "T_prepare_ns", "T_queue_ns", "T_sync_ns", "T_move_ns"]
    result["ir_evaluation_point_schema"] = "CalibrationIR"
    result["ir_evaluation_point_fields"] = "FILLED_PREP2"
    result["ir_evaluation_points"] = dispatch_result_to_points(result)
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

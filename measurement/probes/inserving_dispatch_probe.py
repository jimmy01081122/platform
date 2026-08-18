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

CPU smoke test: ``--backend mock_dispatch``. Result stamped
``evidence = "cpu_smoke_test_not_measurement"``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

try:
    from measurement.probes import SCHEMA_DISPATCH_INSERVING, PENDING_A2_SENTINEL
    from measurement.probes.mock_backend import (
        BackendError,
        MockDispatchBackend,
        resolve_backend,
    )
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from measurement.probes import SCHEMA_DISPATCH_INSERVING, PENDING_A2_SENTINEL
    from measurement.probes.mock_backend import (
        BackendError,
        MockDispatchBackend,
        resolve_backend,
    )


DEFAULT_CONCURRENCY = (1, 2, 4, 8)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--backend", required=True,
                   help="backend name (mock_dispatch for CPU smoke test)")
    p.add_argument("--concurrency", default=",".join(str(c) for c in DEFAULT_CONCURRENCY),
                   help="comma-separated concurrency values")
    p.add_argument("--steps", type=int, default=32,
                   help="decode steps to instrument per concurrency (>=1)")
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


def _build_backend(name: str):
    cls = resolve_backend(name)
    if cls is MockDispatchBackend:
        return cls()
    raise BackendError(
        f"backend {name!r} is not runnable in TRACK_GPU_PREP; use mock_dispatch "
        "for the CPU smoke test. Real in-serving dispatch belongs to TRACK_GPU."
    )


def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.steps < 1:
        raise SystemExit("--steps must be >= 1")
    concurrency_values = _concurrency(args.concurrency)
    backend = _build_backend(args.backend)
    is_mock = isinstance(backend, MockDispatchBackend)

    groups: list[dict[str, Any]] = []
    for concurrency in concurrency_values:
        steps = [backend.measure_step(i, concurrency) for i in range(args.steps)]
        total_bytes = sum(s["dispatch_bytes"] for s in steps)
        total_decisions = sum(s["control_decisions"] for s in steps)
        groups.append({
            "concurrency": concurrency,
            "steps_instrumented": len(steps),
            "per_step": steps,
            "total_dispatch_bytes": total_bytes,
            "total_control_decisions": total_decisions,
            "mean_dispatch_bytes": total_bytes / len(steps),
        })

    result = {
        "schema_version": SCHEMA_DISPATCH_INSERVING,
        "target": "A2_moe_dispatch_data_movement",
        "backend": backend.name,
        "evidence": (
            "cpu_smoke_test_not_measurement" if is_mock else "measured"
        ),
        "argv": _reconstruct_argv(args),
        "note": (
            "system-level dispatch movement + control structure; complements the "
            "same-device execute-only gather_scatter proxy at benchmark.py:463"
        ),
        "groups": groups,
        # T_prepare / T_queue / T_sync / T_move break-even decomposition and the
        # IR evaluation-point projection depend on A2's schema. Hard rule 5:
        # not guessed here; filled in PREP-2.
        "break_even_decomposition_fields": PENDING_A2_SENTINEL,
        "ir_evaluation_point_fields": PENDING_A2_SENTINEL,
    }
    return result


def _reconstruct_argv(args: argparse.Namespace) -> list[str]:
    return ["--backend", args.backend, "--concurrency", args.concurrency,
            "--steps", str(args.steps), "--out", args.out]


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

#!/usr/bin/env python3
"""V2-GAP-C route-sort and permutation microbenchmark.

The four explicit operations mirror ``window_replay``'s missing graph terms:
route argsort, route packing (index_select), inverse argsort, and output unpacking
(index_select).  The formal ``n`` axis is the frozen expert-token axis and is
serialized directly as ``expert_tokens`` in raw records and evaluation points.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

try:
    from measurement.probes.mock_backend import BackendError
    from measurement.probes.target4_phase2_backend import (
        TorchTarget4Backend, registered_backends, resolve_backend,
    )
    from measurement.probes.target4_phase2_common import (
        FROZEN_EXPERT_TOKENS,
        SORT_PERMUTE_OPERATIONS,
        make_component_point,
        make_record,
        parse_positive_int_csv,
        write_json,
    )
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from measurement.probes.mock_backend import BackendError
    from measurement.probes.target4_phase2_backend import (
        TorchTarget4Backend, registered_backends, resolve_backend,
    )
    from measurement.probes.target4_phase2_common import (
        FROZEN_EXPERT_TOKENS,
        SORT_PERMUTE_OPERATIONS,
        make_component_point,
        make_record,
        parse_positive_int_csv,
        write_json,
    )


PROBE_SCHEMA = "gpu-v2-gap-c-sort-permute-probe-v1"
BASE_SCHEMA = "gpu-benchmark-result-v1"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=registered_backends(), required=True)
    parser.add_argument(
        "--expert-tokens",
        type=lambda value: parse_positive_int_csv(value, "expert_tokens"),
        default=FROZEN_EXPERT_TOKENS,
    )
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--warmup", type=int, default=10)
    parser.add_argument("--minimum-inner-seconds", type=float, default=1.0)
    parser.add_argument("--out", type=Path, required=True)
    return parser.parse_args(argv)


def _build_backend(args: argparse.Namespace):
    backend_type = resolve_backend(args.backend)
    if backend_type is TorchTarget4Backend:
        return backend_type(
            warmup=args.warmup,
            repeats=args.repeats,
            minimum_inner_seconds=args.minimum_inner_seconds,
        )
    return backend_type()


def run(args: argparse.Namespace, exact_argv: list[str] | None = None) -> dict[str, Any]:
    if tuple(args.expert_tokens) != FROZEN_EXPERT_TOKENS:
        raise BackendError(
            f"V2-GAP-C n axis is frozen at {list(FROZEN_EXPERT_TOKENS)}"
        )
    if args.repeats != 5:
        raise BackendError(f"V2-GAP-C sample size is frozen at n=5, got {args.repeats}")
    if args.warmup < 0 or args.minimum_inner_seconds <= 0:
        raise BackendError("warmup must be non-negative and minimum-inner-seconds positive")
    backend = _build_backend(args)
    records: list[dict[str, Any]] = []
    points: list[dict[str, Any]] = []
    for expert_tokens in FROZEN_EXPERT_TOKENS:
        for operation in SORT_PERMUTE_OPERATIONS:
            measurement = backend.measure_sort_permute(
                operation, expert_tokens, args.repeats
            )
            activation_bytes = expert_tokens * 7168 * 2
            record = make_record(
                probe=PROBE_SCHEMA,
                operation=operation,
                structured_axes={
                    "expert_tokens": expert_tokens,
                    "phase": "decode",
                    "concurrency": 1,
                },
                samples_ms=measurement["samples_ms"],
                inner_iterations=measurement["inner_iterations"],
                warmup=args.warmup,
                minimum_inner_seconds=args.minimum_inner_seconds,
                metadata={
                    "calibration_role": "replay_operator_graph",
                    "implementation": measurement["implementation"],
                    "route_items": expert_tokens,
                    "activation_bytes": activation_bytes,
                    "num_experts": 256,
                },
            )
            features = {
                "expert_tokens": expert_tokens,
                "phase": "decode",
                "concurrency": 1,
                "cpu_calls": 0,
                "gpu_operations": {operation: 1},
                "memory_bytes": activation_bytes,
                "queue_depth": 0,
                "operator_graph_role": operation,
            }
            records.append(record)
            points.append(make_component_point(
                probe=PROBE_SCHEMA, record=record, features=features
            ))
            print(
                f"PROGRESS operation={operation} expert_tokens={expert_tokens} "
                f"cells={len(records)}/32",
                flush=True,
            )
    result = {
        "schema_version": BASE_SCHEMA,
        "probe_schema_version": PROBE_SCHEMA,
        "backend": backend.name,
        "evidence": backend.evidence,
        "evidence_use": "FIT_SIDE_ONLY_NOT_SEALED_HOLDOUT",
        "exact_argv": exact_argv or [],
        "runtime_identity": backend.runtime_identity,
        "repeats": args.repeats,
        "warmup": args.warmup,
        "minimum_inner_seconds": args.minimum_inner_seconds,
        "axes": {
            "operations": list(SORT_PERMUTE_OPERATIONS),
            "expert_tokens": list(FROZEN_EXPERT_TOKENS),
            "phase": ["decode"],
            "concurrency": [1],
        },
        "explicit_window_replay_operator_graph": [
            "argsort_route",
            "index_select_pack",
            "gate_gemm",
            "up_gemm",
            "down_gemm",
            "argsort_inverse",
            "index_select_unpack",
        ],
        "raw_benchmarks": records,
        "evaluation_points": points,
        "operand_shape_serialization": "structured_fields_only_no_case_string",
        "claim_boundary": (
            "FIT-side V2-GAP-C operator microbenchmark only; it does not by "
            "itself FIT-close replay or support a calibrated PASS."
        ),
    }
    if len(records) != 32 or len(points) != 32:
        raise BackendError("internal V2-GAP-C grid closure failure")
    write_json(args.out, result)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    exact = [str(Path(__file__))] + (list(argv) if argv is not None else sys.argv[1:])
    result = run(args, exact_argv=exact)
    print(
        f"sort_permute_probe: backend={result['backend']} "
        f"evidence={result['evidence']} cells={len(result['raw_benchmarks'])} "
        f"-> {args.out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


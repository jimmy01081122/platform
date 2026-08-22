#!/usr/bin/env python3
"""GAP-1 dequant packed-weight byte sweep at a fixed token control.

The contract does not freeze either the byte grid or fixed token count.  Both
are therefore required CLI arguments: silently choosing them here would change
the measurement domain without an owner decision.  The implemented kernel is
explicitly the existing synthetic symmetric-int4 unpack proxy, not checkpoint
AWQ; its results remain PROXY_ONLY and do not close V2-GAP-D.
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
        make_component_point,
        make_record,
        parse_positive_int_csv,
        write_json,
    )


PROBE_SCHEMA = "gpu-gap1-dequant-weight-bytes-probe-v1"
BASE_SCHEMA = "gpu-benchmark-result-v1"
GROUP_SIZE = 128
PACKED_BYTES_PER_GROUP = GROUP_SIZE // 2


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=registered_backends(), required=True)
    parser.add_argument(
        "--weight-bytes",
        type=lambda value: parse_positive_int_csv(value, "weight_bytes"),
        required=True,
        help="owner-approved strictly increasing packed-weight byte grid",
    )
    parser.add_argument(
        "--fixed-expert-tokens",
        type=int,
        required=True,
        help="owner-approved fixed token control (must not vary within the sweep)",
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
    if len(args.weight_bytes) < 2:
        raise BackendError("GAP-1 requires at least two distinct weight-byte values")
    if any(value % PACKED_BYTES_PER_GROUP for value in args.weight_bytes):
        raise BackendError(
            f"each packed weight_bytes value must align to an INT4 group128 "
            f"({PACKED_BYTES_PER_GROUP} bytes)"
        )
    if args.fixed_expert_tokens <= 0:
        raise BackendError("fixed-expert-tokens must be positive")
    if args.repeats != 5:
        raise BackendError(f"target_4 formal sample size is frozen at n=5, got {args.repeats}")
    if args.warmup < 0 or args.minimum_inner_seconds <= 0:
        raise BackendError("warmup must be non-negative and minimum-inner-seconds positive")
    backend = _build_backend(args)
    records: list[dict[str, Any]] = []
    points: list[dict[str, Any]] = []
    for weight_bytes in args.weight_bytes:
        measurement = backend.measure_dequant(
            weight_bytes, args.fixed_expert_tokens, args.repeats
        )
        record = make_record(
            probe=PROBE_SCHEMA,
            operation="dequant",
            structured_axes={
                "weight_bytes": weight_bytes,
                "expert_tokens": args.fixed_expert_tokens,
                "fixed_expert_tokens": args.fixed_expert_tokens,
                "concurrency": 1,
            },
            samples_ms=measurement["samples_ms"],
            inner_iterations=measurement["inner_iterations"],
            warmup=args.warmup,
            minimum_inner_seconds=args.minimum_inner_seconds,
            metadata={
                "calibration_role": "dequant_weight_bytes",
                "implementation": measurement["implementation"],
                "group_size": GROUP_SIZE,
                "weight_layout": "synthetic_symmetric_int4_proxy_not_checkpoint_awq",
                "evidence_limit": "PROXY_ONLY; real checkpoint AWQ layout remains open",
            },
        )
        features = {
            "expert_tokens": args.fixed_expert_tokens,
            "fixed_expert_tokens": args.fixed_expert_tokens,
            "weight_bytes": weight_bytes,
            "concurrency": 1,
            "cpu_calls": 0,
            "gpu_operations": {"dequant": 1},
            "memory_bytes": weight_bytes,
            "queue_depth": 0,
            "group_size": GROUP_SIZE,
            "weight_layout": "synthetic_symmetric_int4_proxy_not_checkpoint_awq",
        }
        records.append(record)
        points.append(make_component_point(
            probe=PROBE_SCHEMA, record=record, features=features
        ))
        print(
            f"PROGRESS weight_bytes={weight_bytes} "
            f"fixed_expert_tokens={args.fixed_expert_tokens} "
            f"cells={len(records)}/{len(args.weight_bytes)}",
            flush=True,
        )
    result = {
        "schema_version": BASE_SCHEMA,
        "probe_schema_version": PROBE_SCHEMA,
        "backend": backend.name,
        "evidence": backend.evidence,
        "evidence_use": "FIT_SIDE_ONLY_NOT_SEALED_HOLDOUT",
        "evidence_limit": "DEQUANT_PROXY_ONLY",
        "exact_argv": exact_argv or [],
        "runtime_identity": backend.runtime_identity,
        "repeats": args.repeats,
        "warmup": args.warmup,
        "minimum_inner_seconds": args.minimum_inner_seconds,
        "axes": {
            "weight_bytes": list(args.weight_bytes),
            "fixed_expert_tokens": [args.fixed_expert_tokens],
            "concurrency": [1],
        },
        "raw_benchmarks": records,
        "evaluation_points": points,
        "operand_shape_serialization": "structured_fields_only_no_case_string",
        "claim_boundary": (
            "GAP-1 packed-byte sensitivity for the synthetic symmetric INT4 "
            "proxy only. This is not checkpoint AWQ and cannot close V2-GAP-D "
            "or support a calibrated dequant claim."
        ),
    }
    if len(records) != len(args.weight_bytes) or len(points) != len(records):
        raise BackendError("internal GAP-1 grid closure failure")
    write_json(args.out, result)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    exact = [str(Path(__file__))] + (list(argv) if argv is not None else sys.argv[1:])
    result = run(args, exact_argv=exact)
    print(
        f"dequant_weight_bytes_probe: backend={result['backend']} "
        f"evidence={result['evidence']} cells={len(result['raw_benchmarks'])} "
        f"-> {args.out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


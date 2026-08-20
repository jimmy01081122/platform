#!/usr/bin/env python3
"""Multi-object concurrent-transfer probe (TRACK_GPU_PREP PREP-3, V2-GAP-A).

V2-GAP-A-MULTISTREAM-AGGREGATE (cal_model_form_repair_v2.yaml
measurement_gaps_opened): the aggregate completion time of *multiple independent*
transfers, needed to validate/extend the two-regime model's S>1 *production*
semantics -- which is currently UNSUPPORTED.

Why a new probe and not the existing copy_streams sweep (benchmark.py:316-334):
the existing ``copy_streams`` S splits ONE object's buffer into S chunks across S
streams. Per OWNER_RESOLUTION 2026-08-18, S is an *intra-object* partition; the
production rule is S=1 for a single residency-managed object (expert 336 MiB, KV
block 2 MiB), and any S>1 *production* prediction is UNSUPPORTED until "the
aggregate completion time of N independent transfers" is measured. That quantity
is *multi-object* concurrency -- a DIFFERENT axis -- and must NOT be expressed
with S. This probe sweeps ``num_objects`` N with ``copy_streams`` fixed at 1.

Boundary vs existing gaps (avoid duplication, measurement_gaps.json):
  * GAP-2 (aggregate contention unvalidated) and GAP-6 (size-dependent stream
    interaction) both live on the *intra-object* S / copy_streams axis.
  * V2-GAP-A is the *multi-object* axis (num_objects), which neither touches.

CPU smoke test: ``--backend mock_aggregate``. Result stamped
``evidence = "cpu_smoke_test_not_measurement"``. This probe never dispatches to a
GPU/PCIe/SSH/serving path; the real backend is a stub that raises here.

CLAIM BOUNDARY (TRACK_GPU_PREP §7): a passing CPU smoke test proves argv parsing,
serialization and error handling are correct. It proves NOTHING about GPU
transfer performance and does NOT resolve the S>1 production semantics -- that
resolution requires the real GPU measurement this contract sizes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

try:
    from measurement.probes.aggregate_backend import (
        MockAggregateBackend, resolve_backend,
    )
    from measurement.probes.mock_backend import BackendError
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from measurement.probes.aggregate_backend import (
        MockAggregateBackend, resolve_backend,
    )
    from measurement.probes.mock_backend import BackendError

SCHEMA_MULTISTREAM_AGGREGATE = "gpu-multistream-aggregate-result-v1"

# Real production residency objects (frozen facts): the meaningful object sizes.
DEFAULT_NUM_OBJECTS = (1, 2, 4, 8)
# 64 KiB small-regime anchor + the two real production residency objects
# (KV block 2 MiB, expert 336 MiB); spans small / transition / bulk so the GPU
# sweep can locate both two-regime knees at multi-object concurrency.
DEFAULT_OBJECT_BYTES = (65536, 2097152, 352321536)
DEFAULT_DIRECTIONS = ("h2d", "d2h")
DEFAULT_REPEATS = 5   # matches the q0 n=5 convention (same as component target_4)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--backend", required=True,
                   help="backend name (mock_aggregate for CPU smoke test)")
    p.add_argument("--num-objects",
                   default=",".join(str(n) for n in DEFAULT_NUM_OBJECTS),
                   help="comma-separated multi-object concurrency values (N); NOT copy_streams")
    p.add_argument("--object-bytes",
                   default=",".join(str(b) for b in DEFAULT_OBJECT_BYTES),
                   help="comma-separated per-object transfer sizes in bytes")
    p.add_argument("--direction", default=",".join(DEFAULT_DIRECTIONS),
                   help="comma-separated directions (h2d,d2h)")
    p.add_argument("--repeats", type=int, default=DEFAULT_REPEATS,
                   help="inner repeats per cell (Student-t CI on GPU; >=1)")
    p.add_argument("--out", required=True, help="output JSON path")
    p.add_argument("--pretty", action="store_true")
    return p.parse_args(argv)


def _int_list(spec: str, name: str) -> list[int]:
    out: list[int] = []
    for tok in spec.split(","):
        tok = tok.strip()
        if not tok:
            continue
        val = int(tok)
        if val <= 0:
            raise SystemExit(f"{name} must be positive: {val}")
        out.append(val)
    if not out:
        raise SystemExit(f"no {name} values given")
    return out


def _directions(spec: str) -> list[str]:
    out: list[str] = []
    for tok in spec.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if tok not in ("h2d", "d2h"):
            raise SystemExit(f"direction must be h2d/d2h, got {tok!r}")
        out.append(tok)
    if not out:
        raise SystemExit("no direction values given")
    return out


def _build_backend(name: str):
    cls = resolve_backend(name)
    if cls is MockAggregateBackend:
        return cls()
    raise BackendError(
        f"backend {name!r} is not runnable in TRACK_GPU_PREP; use mock_aggregate "
        "for the CPU smoke test. Real multi-object PCIe transfers belong to TRACK_GPU."
    )


def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.repeats < 1:
        raise SystemExit("--repeats must be >= 1")
    num_objects = _int_list(args.num_objects, "num-objects")
    object_bytes = _int_list(args.object_bytes, "object-bytes")
    directions = _directions(args.direction)
    backend = _build_backend(args.backend)
    is_mock = isinstance(backend, MockAggregateBackend)

    cells: list[dict[str, Any]] = []
    for direction in directions:
        for obj_bytes in object_bytes:
            for n in num_objects:
                cells.append(
                    backend.measure_cell(n, obj_bytes, direction, args.repeats)
                )

    runtime_variant_hash = hashlib.sha256(
        ("multistream_aggregate|" + backend.name
         + "|N=" + ",".join(str(n) for n in num_objects)
         + "|B=" + ",".join(str(b) for b in object_bytes)
         + "|D=" + ",".join(directions)
         + f"|repeats={args.repeats}").encode()
    ).hexdigest()

    result = {
        "schema_version": SCHEMA_MULTISTREAM_AGGREGATE,
        "target": "V2-GAP-A-MULTISTREAM-AGGREGATE",
        "backend": backend.name,
        "evidence": (
            "cpu_smoke_test_not_measurement" if is_mock else "measured"
        ),
        "argv": _reconstruct_argv(args),
        "runtime_variant_hash": runtime_variant_hash,
        "repeats": args.repeats,
        "concurrency_axis": "num_objects",   # multi-object; explicitly NOT copy_streams
        "note": (
            "aggregate completion time of N INDEPENDENT transfers (multi-object "
            "concurrency). Distinct from the intra-object copy_streams S axis "
            "(GAP-2/GAP-6). Opens data to define the two-regime S>1 PRODUCTION "
            "semantics, which stays UNSUPPORTED until this is measured on GPU."
        ),
        "cells": cells,
        # V2-GAP-A does not yet have a defined production coordinate: the S>1
        # aggregate->production mapping is exactly the UNSUPPORTED item this gap
        # opens. Its projection into A2's CalibrationIR is therefore NOT
        # determinable from existing data and is left PENDING rather than guessed
        # (the GAP-4 lesson: never freeze a measurement schema ahead of its
        # consumer). Fill only after TRACK_GPU resolves the S>1 semantics.
        "ir_evaluation_point_schema": "CalibrationIR",
        "ir_evaluation_point_fields": "PENDING_S_GT_1_SEMANTICS",
        "production_stream_semantics_status": "UNSUPPORTED_UNTIL_MEASURED",
    }
    return result


def _reconstruct_argv(args: argparse.Namespace) -> list[str]:
    return ["--backend", args.backend, "--num-objects", args.num_objects,
            "--object-bytes", args.object_bytes, "--direction", args.direction,
            "--repeats", str(args.repeats), "--out", args.out]


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
        f"multistream_aggregate_probe: backend={result['backend']} "
        f"evidence={result['evidence']} cells={len(result['cells'])} "
        f"axis={result['concurrency_axis']} -> {out_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

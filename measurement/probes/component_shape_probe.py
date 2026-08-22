#!/usr/bin/env python3
"""Target_4 Phase 2 component-shape probe (P-020 owner decision (b)).

The complete sealed grid is 4 component operations x 2 phases x 8 independent
``expert_tokens`` values = 64 cells.  A single invocation measures exactly one
precommitted split from ``holdout_split_v1_manifest.json``; it never mixes the
41 fit, 11 validation, and 12 holdout cells in one output.  Holdout measurement
also requires an explicit acknowledgement so a routine target_4 FIT run cannot
open A4 cells accidentally.  Every selected cell is n=5.

``expert_tokens`` is written as a structured raw field and copied directly into
every evaluation point.  A ``case`` string is intentionally absent.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

try:
    from measurement.probes.mock_backend import BackendError
    from measurement.probes.target4_phase2_backend import (
        TorchTarget4Backend,
        registered_backends,
        resolve_backend,
    )
    from measurement.probes.target4_phase2_common import (
        FROZEN_COMPONENT_OPERATIONS,
        FROZEN_EXPERT_TOKENS,
        FROZEN_PHASES,
        make_component_point,
        make_record,
        parse_positive_int_csv,
        write_json,
    )
except ImportError:  # pragma: no cover - direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from measurement.probes.mock_backend import BackendError
    from measurement.probes.target4_phase2_backend import (
        TorchTarget4Backend,
        registered_backends,
        resolve_backend,
    )
    from measurement.probes.target4_phase2_common import (
        FROZEN_COMPONENT_OPERATIONS,
        FROZEN_EXPERT_TOKENS,
        FROZEN_PHASES,
        make_component_point,
        make_record,
        parse_positive_int_csv,
        write_json,
    )


PROBE_SCHEMA = "gpu-target4-component-shape-probe-v1"
BASE_SCHEMA = "gpu-benchmark-result-v1"
SEALED_ASSIGNMENT_SHA256 = (
    "b73da79cd040dca29f258910d6245de4a9e647137dc4005722ebd8b9b88a5f4b"
)
SPLIT_EVIDENCE_USE = {
    "fit": "FIT_SIDE_ONLY_NOT_SEALED_HOLDOUT",
    "validation": "VALIDATION_SIDE_ONLY_NOT_SEALED_HOLDOUT",
    "holdout": "SEALED_HOLDOUT_STAGE_A4_ONLY_DO_NOT_FIT",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=registered_backends(), required=True)
    parser.add_argument(
        "--expert-tokens",
        type=lambda value: parse_positive_int_csv(value, "expert_tokens"),
        default=FROZEN_EXPERT_TOKENS,
        help="frozen formal axis: 8,16,32,64,128,256,512,1024",
    )
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--warmup", type=int, default=10)
    parser.add_argument("--minimum-inner-seconds", type=float, default=1.0)
    parser.add_argument(
        "--sealed-manifest",
        type=Path,
        required=True,
        help="the precommitted calibration/sealed/holdout_split_v1_manifest.json",
    )
    parser.add_argument(
        "--split",
        choices=("fit", "validation", "holdout"),
        required=True,
        help="measure exactly one precommitted split",
    )
    parser.add_argument(
        "--authorize-holdout-measurement",
        action="store_true",
        help="required only for the STAGE_A4 holdout attempt",
    )
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


def _sealed_component_cells(
    manifest_path: Path, selected_split: str
) -> tuple[dict[tuple[str, str, int], dict[str, Any]], dict[str, Any]]:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise BackendError(
            f"cannot read sealed split manifest {manifest_path}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    try:
        from measurement.parsers.sealed_manifest_validator import validate

        validate(manifest)
    except Exception as exc:
        raise BackendError(f"sealed split manifest validation failed: {exc}") from exc
    if manifest.get("id") != "a4_holdout_split_v1":
        raise BackendError("sealed split manifest id is not a4_holdout_split_v1")
    if manifest.get("assignment_sha256") != SEALED_ASSIGNMENT_SHA256:
        raise BackendError(
            "sealed split assignment differs from the frozen target_4 contract"
        )

    all_cells: dict[tuple[str, str, int], dict[str, Any]] = {}
    selected: dict[tuple[str, str, int], dict[str, Any]] = {}
    for cell in manifest["cells"]:
        if cell.get("metric") != "component_latency":
            continue
        params = cell.get("params", {})
        key = (params.get("op"), params.get("phase"), params.get("expert_tokens"))
        if key in all_cells:
            raise BackendError(f"duplicate sealed component cell {key!r}")
        all_cells[key] = cell
        if cell.get("split") == selected_split:
            selected[key] = cell
    expected = {
        (operation, phase, expert_tokens)
        for operation in FROZEN_COMPONENT_OPERATIONS
        for phase in FROZEN_PHASES
        for expert_tokens in FROZEN_EXPERT_TOKENS
    }
    if set(all_cells) != expected:
        raise BackendError("sealed manifest does not cover the exact 64-cell component grid")
    if not selected:
        raise BackendError(f"sealed split {selected_split!r} has no component cells")
    return selected, manifest


def run(args: argparse.Namespace, exact_argv: list[str] | None = None) -> dict[str, Any]:
    if tuple(args.expert_tokens) != FROZEN_EXPERT_TOKENS:
        raise BackendError(
            "component expert_tokens axis is frozen at "
            f"{list(FROZEN_EXPERT_TOKENS)}, got {list(args.expert_tokens)}"
        )
    if args.repeats != 5:
        raise BackendError(f"target_4 formal sample size is frozen at n=5, got {args.repeats}")
    if args.warmup < 0 or args.minimum_inner_seconds <= 0:
        raise BackendError("warmup must be non-negative and minimum-inner-seconds positive")
    if args.split == "holdout" and not args.authorize_holdout_measurement:
        raise BackendError(
            "holdout cells are sealed for STAGE_A4; pass "
            "--authorize-holdout-measurement only in the explicit A4 attempt"
        )
    if args.split != "holdout" and args.authorize_holdout_measurement:
        raise BackendError(
            "--authorize-holdout-measurement is invalid outside split=holdout"
        )

    selected_cells, sealed_manifest = _sealed_component_cells(
        args.sealed_manifest, args.split
    )

    backend = _build_backend(args)
    records: list[dict[str, Any]] = []
    points: list[dict[str, Any]] = []
    for operation in FROZEN_COMPONENT_OPERATIONS:
        for phase in FROZEN_PHASES:
            for expert_tokens in FROZEN_EXPERT_TOKENS:
                sealed_key = (operation, phase, expert_tokens)
                if sealed_key not in selected_cells:
                    continue
                sealed_cell = selected_cells[sealed_key]
                measurement = backend.measure_component(
                    operation, expert_tokens, phase, args.repeats
                )
                logical_bytes = (
                    expert_tokens * ((7168 + 1) // 2)
                    if operation == "dequant"
                    else expert_tokens * 7168 * 2
                )
                record = make_record(
                    probe=PROBE_SCHEMA,
                    operation=operation,
                    structured_axes={
                        "expert_tokens": expert_tokens,
                        "phase": phase,
                        "concurrency": 1,
                    },
                    samples_ms=measurement["samples_ms"],
                    inner_iterations=measurement["inner_iterations"],
                    warmup=args.warmup,
                    minimum_inner_seconds=args.minimum_inner_seconds,
                    metadata={
                        "calibration_role": "gpu_service",
                        "implementation": measurement["implementation"],
                        "memory_bytes": logical_bytes,
                        "memory_bytes_semantics": "logical_operand_payload",
                        "phase_semantics": (
                            "workload_phase_stratum_at_independently_controlled_expert_tokens"
                        ),
                        "sealed_cell_id": sealed_cell["cell_id"],
                        "sealed_cell_sha256": sealed_cell["sha256"],
                        "sealed_split": args.split,
                        **({
                            "evidence_limit": (
                                "synthetic dequant proxy; not checkpoint AWQ layout"
                            )
                        } if operation == "dequant" else {}),
                    },
                )
                features = {
                    "expert_tokens": expert_tokens,
                    "phase": phase,
                    "concurrency": 1,
                    "cpu_calls": 0,
                    "gpu_operations": {operation: 1},
                    "memory_bytes": logical_bytes,
                    "queue_depth": 0,
                }
                records.append(record)
                points.append(make_component_point(
                    probe=PROBE_SCHEMA,
                    record=record,
                    features=features,
                    split=args.split,
                    evidence_use=SPLIT_EVIDENCE_USE[args.split],
                ))
                print(
                    f"PROGRESS operation={operation} phase={phase} "
                    f"expert_tokens={expert_tokens} cells={len(records)}/"
                    f"{len(selected_cells)} split={args.split}",
                    flush=True,
                )

    result = {
        "schema_version": BASE_SCHEMA,
        "probe_schema_version": PROBE_SCHEMA,
        "backend": backend.name,
        "evidence": backend.evidence,
        "evidence_use": SPLIT_EVIDENCE_USE[args.split],
        "exact_argv": exact_argv or [],
        "runtime_identity": backend.runtime_identity,
        "repeats": args.repeats,
        "warmup": args.warmup,
        "minimum_inner_seconds": args.minimum_inner_seconds,
        "axes": {
            "operations": list(FROZEN_COMPONENT_OPERATIONS),
            "phases": list(FROZEN_PHASES),
            "expert_tokens": list(FROZEN_EXPERT_TOKENS),
            "concurrency": [1],
        },
        "sealed_assignment": {
            "manifest_id": sealed_manifest["id"],
            "assignment_sha256": sealed_manifest["assignment_sha256"],
            "selected_split": args.split,
            "selected_component_cells": len(selected_cells),
            "holdout_measurement_authorized": args.authorize_holdout_measurement,
        },
        "raw_benchmarks": records,
        "evaluation_points": points,
        "operand_shape_serialization": "structured_fields_only_no_case_string",
        "claim_boundary": (
            f"{args.split} component microbenchmark only; split comes from the "
            "precommitted A4 assignment. Dequant cells retain synthetic PROXY_ONLY "
            "status. No calibrated PASS is claimed."
        ),
    }
    if len(records) != len(selected_cells) or len(points) != len(selected_cells):
        raise BackendError("internal sealed-subset closure failure; refusing partial output")
    write_json(args.out, result)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    exact = [str(Path(__file__))] + (list(argv) if argv is not None else sys.argv[1:])
    result = run(args, exact_argv=exact)
    print(
        f"component_shape_probe: backend={result['backend']} "
        f"evidence={result['evidence']} cells={len(result['raw_benchmarks'])} "
        f"-> {args.out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

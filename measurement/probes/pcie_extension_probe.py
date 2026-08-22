#!/usr/bin/env python3
"""PCIe extension probe: four axes beyond V2-GAP-A and the frozen S-sweep.

Both existing GPU concurrency probes on the canonical RTX PRO 6000 Blackwell
box measure only *same-direction* concurrency:
  * V2-GAP-A (``multistream_aggregate_probe.py``): N independent objects, one
    direction at a time -- found exact serialization at every N tested.
  * the frozen harness's ``copy_streams`` S sweep (``gpu_run_package_v2``):
    one object split across S streams, one direction at a time -- found a
    small-transfer-only penalty that fades to ~1.00x by 22020096 bytes.

This probe covers the four gaps neither of those axes touches:

  1. ``bidirectional`` -- one independent H2D copy and one independent D2H
     copy issued concurrently (two independent pinned-host + device buffer
     pairs, two streams, a common CUDA start event), at the SAME
     ``object_bytes`` grid V2-GAP-A used (65536 / 2097152 / 352321536) for
     direct comparability. Reports h2d_ms, d2h_ms, joint_completion_ms (the
     wait-all completion, via a distinct coordinator event recorded only
     after waiting on both legs -- see pcie_extension_backend.py's module
     docstring for why that ordering matters) PLUS each unidirectional
     baseline (H2D alone, D2H alone) at the same sizes in the same run, so
     ``joint_over_max_ratio`` / ``joint_over_sum_ratio`` are self-contained
     and never require cross-referencing V2-GAP-A's separate output file.
     Neither ratio is asserted to sit near a particular value: true
     dual-engine overlap (joint tracks max(baselines)) and a single shared
     engine (joint tracks sum(baselines)) are both physically valid answers,
     and finding out which this GPU does is this axis's entire point.

  2. ``stream_split`` (crossover densification) -- the same single-object
     ``copy_streams``-way split the frozen harness already uses, at 5
     log-spaced byte sizes strictly between the frozen grid's 1048576 and
     22020096 (2097152 / 3145728 / 5242880 / 8388608 / 13631488 bytes -- 2,
     3, 5, 8, 13 MiB) where the frozen harness's own S4/S1 penalty is known
     to fall from 2.10x to ~1.00x but was never sampled in between.

  3. ``stream_split`` (declared-never-measured cell) -- the same primitive at
     4096 bytes, the one point already named in the frozen measurement
     contract's byte grid that was never actually run.

  4. ``pcie_link`` -- nvidia-smi PCIe link generation/width capture
     (pcie.link.gen.current/max, pcie.link.width.current/max), always
     recorded alongside every invocation's output regardless of ``--axis``
     (see pcie_extension_backend.capture_pcie_link_info), closing the gap
     where every existing GPU-attempt environment record has GPU name/uuid/
     memory/driver/compute_cap but never the PCIe link itself.

Axes 2 and 3 share one CLI axis name (``stream_split``) because they are the
exact same measurement primitive (a single object chunked across
``copy_streams`` streams, replicated from the frozen harness's own chunking
math -- see pcie_extension_backend.py) differing only in WHICH byte sizes are
swept; each cell records a ``gap_label`` recording which of the two gaps (or
neither) motivated that byte size, so the two "axes" can be told apart in the
output without two near-duplicate code paths.

CPU smoke test: ``--backend mock_pcie_extension``. Result stamped
``evidence = "cpu_smoke_test_not_measurement"``. TRACK_GPU dispatches the same
axes through ``--backend gpu``; that backend refuses loudly if CUDA is
unavailable and never substitutes mock data.

CLAIM BOUNDARY (matches TRACK_GPU_PREP §7 / multistream_aggregate_probe.py):
a passing CPU smoke test proves argv parsing, serialization and error
handling are correct. It proves NOTHING about GPU transfer performance.

exact_argv (matches the style already used per-target in
experiments/specs/gpu_measurement_contract_v1.yaml -- NOT written there
because that file is being actively edited by a concurrent session; recorded
here and in docs/status/PCIE_EXTENSION_PROBE_NOTE.md for the supervisor to
fold in by hand):

  cpu_smoke_test (covers all four axes in one call):
    python3 measurement/probes/pcie_extension_probe.py --backend mock_pcie_extension --axis all --bidirectional-object-bytes 65536,2097152,352321536 --stream-split-bytes 4096,2097152,3145728,5242880,8388608,13631488 --copy-streams 1,2,4 --direction h2d,d2h --repeats 5 --out runs/<run_id>/pcie_extension_smoke.json

  gpu_run, axis 1 (bidirectional concurrency):
    python3 measurement/probes/pcie_extension_probe.py --backend gpu --axis bidirectional --bidirectional-object-bytes 65536,2097152,352321536 --repeats 5 --out runs/<run_id>/pcie_extension_bidirectional.json   # TRACK_GPU only

  gpu_run, axis 2 (S-axis crossover densification):
    python3 measurement/probes/pcie_extension_probe.py --backend gpu --axis stream_split --stream-split-bytes 2097152,3145728,5242880,8388608,13631488 --copy-streams 1,2,4 --direction h2d,d2h --repeats 5 --out runs/<run_id>/pcie_extension_crossover.json   # TRACK_GPU only

  gpu_run, axis 3 (declared-never-measured 4096-byte cell):
    python3 measurement/probes/pcie_extension_probe.py --backend gpu --axis stream_split --stream-split-bytes 4096 --copy-streams 1,2,4 --direction h2d,d2h --repeats 5 --out runs/<run_id>/pcie_extension_4096cell.json   # TRACK_GPU only

  gpu_run, axis 4 (PCIe link capability capture only, no transfers):
    python3 measurement/probes/pcie_extension_probe.py --backend gpu --axis pcie_link --repeats 5 --out runs/<run_id>/pcie_extension_linkcap.json   # TRACK_GPU only

  gpu_run, all four axes in one dispatch (axis 2 + 3 combined via the default
  --stream-split-bytes, which is their union):
    python3 measurement/probes/pcie_extension_probe.py --backend gpu --axis all --bidirectional-object-bytes 65536,2097152,352321536 --stream-split-bytes 4096,2097152,3145728,5242880,8388608,13631488 --copy-streams 1,2,4 --direction h2d,d2h --repeats 5 --out runs/<run_id>/pcie_extension.json   # TRACK_GPU only
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

try:
    from measurement.probes.pcie_extension_backend import (
        MockPcieExtensionBackend, TorchPcieExtensionBackend, resolve_backend,
        capture_pcie_link_info,
    )
    from measurement.probes.mock_backend import BackendError
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from measurement.probes.pcie_extension_backend import (
        MockPcieExtensionBackend, TorchPcieExtensionBackend, resolve_backend,
        capture_pcie_link_info,
    )
    from measurement.probes.mock_backend import BackendError

SCHEMA_PCIE_EXTENSION = "gpu-pcie-extension-result-v1"

# Axis 1 default grid: identical to V2-GAP-A's DEFAULT_OBJECT_BYTES so the
# bidirectional numbers are directly comparable to the same-direction ones.
DEFAULT_BIDIRECTIONAL_OBJECT_BYTES = (65536, 2097152, 352321536)
# Axes 2 + 3 default grid: the declared-never-measured 4 KiB cell, then the
# five log-spaced crossover-densification points (2/3/5/8/13 MiB) strictly
# between the frozen harness's 1048576 and 22020096 byte points.
DEFAULT_STREAM_SPLIT_BYTES = (4096, 2097152, 3145728, 5242880, 8388608, 13631488)
DEFAULT_COPY_STREAMS = (1, 2, 4)   # matches the frozen harness's own S grid
DEFAULT_DIRECTIONS = ("h2d", "d2h")
DEFAULT_REPEATS = 5   # matches the q0 / V2-GAP-A n=5 convention
DEFAULT_AXIS = "all"
AXIS_CHOICES = ("all", "bidirectional", "stream_split", "pcie_link")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--backend", required=True,
                   help="backend name (mock_pcie_extension for CPU smoke test)")
    p.add_argument("--axis", default=DEFAULT_AXIS, choices=AXIS_CHOICES,
                   help="which axis group to run; pcie_link is always captured "
                        "regardless of this flag")
    p.add_argument("--bidirectional-object-bytes",
                   default=",".join(str(b) for b in DEFAULT_BIDIRECTIONAL_OBJECT_BYTES),
                   help="comma-separated object sizes (bytes) for the bidirectional axis")
    p.add_argument("--stream-split-bytes",
                   default=",".join(str(b) for b in DEFAULT_STREAM_SPLIT_BYTES),
                   help="comma-separated object sizes (bytes) for the stream-split axis")
    p.add_argument("--copy-streams",
                   default=",".join(str(s) for s in DEFAULT_COPY_STREAMS),
                   help="comma-separated copy_streams values for the stream-split axis")
    p.add_argument("--direction", default=",".join(DEFAULT_DIRECTIONS),
                   help="comma-separated directions (h2d,d2h) for the stream-split axis")
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
    if cls is MockPcieExtensionBackend:
        return cls()
    if cls is TorchPcieExtensionBackend:
        return cls()
    raise BackendError(f"registered backend {name!r} has no constructor")


def deterministic_point_id(prefix: str, *parts: object) -> str:
    """Same scheme as ``deterministic_id`` in
    measurement/gpu_run_package_v2/scripts/benchmark.py (read-only
    reference): canonical-JSON the parts, sha256, truncate to 24 hex chars,
    prefix. Deterministic and reproducible: identical inputs always yield the
    identical point_id, across processes and runs.
    """
    payload = json.dumps(list(parts), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return f"{prefix}-{hashlib.sha256(payload.encode()).hexdigest()[:24]}"


def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.repeats < 1:
        raise SystemExit("--repeats must be >= 1")
    axis = args.axis
    backend = _build_backend(args.backend)
    is_mock = isinstance(backend, MockPcieExtensionBackend)

    bidirectional_cells: list[dict[str, Any]] = []
    stream_split_cells: list[dict[str, Any]] = []

    if axis in ("all", "bidirectional"):
        object_bytes_list = _int_list(
            args.bidirectional_object_bytes, "bidirectional-object-bytes")
        for object_bytes in object_bytes_list:
            cell = backend.measure_bidirectional_cell(object_bytes, args.repeats)
            cell["point_id"] = deterministic_point_id(
                "bidir", backend.name, object_bytes)
            bidirectional_cells.append(cell)

    if axis in ("all", "stream_split"):
        stream_split_bytes_list = _int_list(args.stream_split_bytes, "stream-split-bytes")
        copy_streams_list = _int_list(args.copy_streams, "copy-streams")
        directions = _directions(args.direction)
        for direction in directions:
            for object_bytes in stream_split_bytes_list:
                for copy_streams in copy_streams_list:
                    cell = backend.measure_stream_split_cell(
                        object_bytes, copy_streams, direction, args.repeats)
                    cell["point_id"] = deterministic_point_id(
                        "streamsplit", backend.name, object_bytes, copy_streams, direction)
                    stream_split_cells.append(cell)

    # Always captured, regardless of --axis: cheap metadata, and every other
    # axis's numbers are only interpretable against the link the box actually
    # negotiated. Exercises the same code path (and the same "no GPU here"
    # unavailable-status branch) whether --backend is mock or gpu.
    pcie_link_environment = capture_pcie_link_info()

    runtime_variant_hash = hashlib.sha256(
        ("pcie_extension|" + backend.name
         + "|axis=" + axis
         + "|BID=" + args.bidirectional_object_bytes
         + "|SS_BYTES=" + args.stream_split_bytes
         + "|SS_STREAMS=" + args.copy_streams
         + "|D=" + args.direction
         + f"|repeats={args.repeats}").encode()
    ).hexdigest()

    result = {
        "schema_version": SCHEMA_PCIE_EXTENSION,
        "target": "PCIE-EXTENSION-BIDIRECTIONAL-CROSSOVER-SMALLCELL-LINKCAP",
        "backend": backend.name,
        "evidence": (
            "cpu_smoke_test_not_measurement" if is_mock else "measured"
        ),
        "argv": _reconstruct_argv(args),
        "runtime_variant_hash": runtime_variant_hash,
        "repeats": args.repeats,
        "axis": axis,
        "note": (
            "Four FIT-side PCIe axes beyond V2-GAP-A (multi-object, "
            "same-direction concurrency) and the frozen copy_streams sweep "
            "(single-object, same-direction concurrency): (1) bidirectional "
            "H2D+D2H concurrency via two independent streams from a common "
            "start event, with self-contained unidirectional baselines; "
            "(2) S-axis crossover densification between the frozen grid's "
            "1048576 and 22020096 byte points; (3) the frozen contract's "
            "declared-but-never-measured 4096-byte cell; (4) nvidia-smi PCIe "
            "link generation/width capture. Not a production claim; FIT-side "
            "measurement only (matches TRACK_GPU_PREP claim boundary)."
        ),
        "bidirectional_cells": bidirectional_cells,
        "stream_split_cells": stream_split_cells,
        "pcie_link_environment": pcie_link_environment,
        # No CalibrationIR consumer has yet decided how bidirectional-overlap
        # or S-axis-crossover measurements map into an evaluation coordinate
        # (unlike V2-GAP-A's num_objects axis, these are new axes this probe
        # itself introduces) -- left PENDING rather than guessed, matching
        # the GAP-4 lesson (never freeze a measurement schema ahead of its
        # consumer) that multistream_aggregate_probe.py already follows.
        "ir_evaluation_point_schema": "CalibrationIR",
        "ir_evaluation_point_fields": "PENDING_PCIE_EXTENSION_SEMANTICS",
        "production_stream_semantics_status": "UNSUPPORTED_UNTIL_MEASURED",
    }
    return result


def _reconstruct_argv(args: argparse.Namespace) -> list[str]:
    return ["--backend", args.backend, "--axis", args.axis,
            "--bidirectional-object-bytes", args.bidirectional_object_bytes,
            "--stream-split-bytes", args.stream_split_bytes,
            "--copy-streams", args.copy_streams,
            "--direction", args.direction,
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
        f"pcie_extension_probe: backend={result['backend']} "
        f"evidence={result['evidence']} axis={result['axis']} "
        f"bidirectional_cells={len(result['bidirectional_cells'])} "
        f"stream_split_cells={len(result['stream_split_cells'])} "
        f"pcie_link_status={result['pcie_link_environment']['status']} "
        f"-> {out_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

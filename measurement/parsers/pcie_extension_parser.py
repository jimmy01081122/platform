#!/usr/bin/env python3
"""Parser + validator for gpu-pcie-extension-result-v1.

Enforces the physical and structural invariants of the four PCIe-extension
axes (see pcie_extension_probe.py's module docstring for what each axis is):

  * ``bidirectional_cells`` -- for each cell, TWO independent envelope checks,
    never clamped or tolerated, only raised:
      (a) structural: max(h2d_ms_mean, d2h_ms_mean) <= joint_completion_ms_mean
          <= h2d_ms_mean + d2h_ms_mean. Mirrors multistream_aggregate_parser's
          max_per_object_ms <= aggregate <= sum_per_object_ms exactly, applied
          to the two concurrently-measured legs: joint completion cannot
          finish before its slower concurrent leg, nor exceed running those
          same two concurrent-measured legs one after another.
      (b) physical floor: joint_completion_ms_mean cannot be LESS than
          max(h2d_baseline_ms_mean, d2h_baseline_ms_mean) -- the fastest
          either leg could possibly complete is its own uncontended,
          exclusive-access baseline; concurrency can only add contention, it
          cannot make a transfer finish faster than that. This is the "below
          its fastest theoretical bound" check for this axis. No matching
          hard ceiling is imposed against sum(baselines): dispatch/sync
          overhead can legitimately push joint completion slightly past a
          clean sum, so only the floor is a genuine physical law here.
    joint_over_max_ratio / joint_over_sum_ratio are recomputed from the
    reported baselines and compared, but NEVER asserted to sit near a
    particular value -- true dual-engine overlap and a shared single engine
    are both physically valid outcomes; asserting one would beg the question
    this axis exists to answer.

  * ``stream_split_cells`` -- for each cell:
      (a) chunk accounting: chunk_bytes must equal ceil(object_bytes /
          copy_streams), the same formula pcie_extension_backend.py replicates
          from the frozen harness's own copy-splitting logic. A mismatch means
          the record does not describe a valid covering split of object_bytes
          into copy_streams pieces.
      (b) physical floor: latency_ms_mean cannot be faster than
          object_bytes could move at any physically plausible PCIe link
          bandwidth (see PCIE_MAX_PLAUSIBLE_BYTES_PER_MS below) -- the
          "below its fastest theoretical bound" check for this axis. No
          monotonicity claim between different copy_streams values is
          enforced: whether splitting helps, hurts, or does nothing at a
          given size is exactly what this axis measures, so asserting a
          direction would beg the question.

  * ``pcie_link_environment`` -- argv must equal the exact recorded nvidia-smi
    invocation (single source of truth: pcie_extension_backend.PCIE_LINK_ARGV);
    status "ok" requires all four link fields present as non-empty strings;
    status "unavailable" requires a reason string. Never fabricated.

A mis-shaped or non-physical record RAISES rather than being skipped -- the
deliberate opposite of the silent-skip anti-pattern (parsers/__init__.py).
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
        require_equal, require_positive_int, require_in,
    )
    from measurement.probes.pcie_extension_backend import PCIE_LINK_ARGV
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from measurement.parsers.common import (
        ValidationError, load_json, require_mapping, require_list, require_key,
        require_equal, require_positive_int, require_in,
    )
    from measurement.probes.pcie_extension_backend import PCIE_LINK_ARGV

SCHEMA = "gpu-pcie-extension-result-v1"
_REL_TOL = 1e-6

# Generous ceiling on plausible PCIe bandwidth used only as a lower bound on
# latency (an upper bound on speed). No shipping PCIe generation exceeds this
# per direction (PCIe 6.0 x16 raw is ~121 GB/s = 121e6 bytes/ms); comfortably
# above that so the check only ever fires on a genuine measurement/unit bug
# (e.g. microseconds recorded as milliseconds), never on real hardware.
PCIE_MAX_PLAUSIBLE_BYTES_PER_MS = 200_000_000.0   # ~200 GB/s

_AXIS_CHOICES = ("all", "bidirectional", "stream_split", "pcie_link")
_GAP_LABELS = (
    "declared_never_measured_4096", "crossover_densification", "custom_sweep_point",
)


def _require_positive_number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{where}: expected number, got {type(value).__name__}")
    if value <= 0:
        raise ValidationError(f"{where}: expected positive number, got {value}")
    return float(value)


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= _REL_TOL * max(1.0, abs(a), abs(b))


def _require_sample_block(
    cell: dict, mean_key: str, samples_key: str, repeats: int, cw: str
) -> tuple[float, list[float]]:
    samples = require_list(require_key(cell, samples_key, cw), f"{cw}.{samples_key}")
    if len(samples) != repeats:
        raise ValidationError(
            f"{cw}.{samples_key}: has {len(samples)} entries != repeats {repeats}")
    sample_vals = [
        _require_positive_number(v, f"{cw}.{samples_key}[{i}]")
        for i, v in enumerate(samples)
    ]
    mean_reported = _require_positive_number(
        require_key(cell, mean_key, cw), f"{cw}.{mean_key}")
    mean_actual = sum(sample_vals) / len(sample_vals)
    if not _close(mean_reported, mean_actual):
        raise ValidationError(
            f"{cw}.{mean_key} {mean_reported} != mean({samples_key}) {mean_actual}")
    return mean_reported, sample_vals


def _validate_bidirectional_cell(cell: Any, ci: int, repeats: int) -> None:
    cw = f"bidirectional_cells[{ci}]"
    require_mapping(cell, cw)
    _require_positive_number(require_key(cell, "object_bytes", cw), f"{cw}.object_bytes")

    h2d_mean, _ = _require_sample_block(cell, "h2d_ms_mean", "h2d_ms_samples", repeats, cw)
    d2h_mean, _ = _require_sample_block(cell, "d2h_ms_mean", "d2h_ms_samples", repeats, cw)
    joint_mean, _ = _require_sample_block(
        cell, "joint_completion_ms_mean", "joint_completion_ms_samples", repeats, cw)
    h2d_baseline_mean, _ = _require_sample_block(
        cell, "h2d_baseline_ms_mean", "h2d_baseline_ms_samples", repeats, cw)
    d2h_baseline_mean, _ = _require_sample_block(
        cell, "d2h_baseline_ms_mean", "d2h_baseline_ms_samples", repeats, cw)

    # (a) structural envelope over the concurrently-measured legs.
    concurrent_max = max(h2d_mean, d2h_mean)
    concurrent_sum = h2d_mean + d2h_mean
    if joint_mean < concurrent_max and not _close(joint_mean, concurrent_max):
        raise ValidationError(
            f"{cw}: non-physical joint_completion_ms_mean {joint_mean} < "
            f"max(h2d_ms_mean, d2h_ms_mean) {concurrent_max} (joint completion "
            f"cannot finish before its slower concurrent leg)")
    if joint_mean > concurrent_sum and not _close(joint_mean, concurrent_sum):
        raise ValidationError(
            f"{cw}: non-physical joint_completion_ms_mean {joint_mean} > "
            f"h2d_ms_mean + d2h_ms_mean {concurrent_sum} (cannot exceed running "
            f"the two concurrently-measured legs one after another)")

    # (b) physical floor against the self-contained unidirectional baselines:
    # joint completion cannot be faster than the slower leg's own uncontended
    # baseline -- concurrency can only add contention, never speed a leg up
    # past running it alone with exclusive access.
    baseline_max = max(h2d_baseline_mean, d2h_baseline_mean)
    baseline_sum = h2d_baseline_mean + d2h_baseline_mean
    if joint_mean < baseline_max and not _close(joint_mean, baseline_max):
        raise ValidationError(
            f"{cw}: non-physical joint_completion_ms_mean {joint_mean} < "
            f"max(h2d_baseline_ms_mean, d2h_baseline_ms_mean) {baseline_max} "
            f"(faster than the fastest theoretical bound: the slower leg's own "
            f"uncontended baseline)")

    joint_over_max = _require_positive_number(
        require_key(cell, "joint_over_max_ratio", cw), f"{cw}.joint_over_max_ratio")
    joint_over_sum = _require_positive_number(
        require_key(cell, "joint_over_sum_ratio", cw), f"{cw}.joint_over_sum_ratio")
    if not _close(joint_over_max, joint_mean / baseline_max):
        raise ValidationError(
            f"{cw}.joint_over_max_ratio {joint_over_max} != "
            f"joint_completion_ms_mean / max(baselines) {joint_mean / baseline_max}")
    if not _close(joint_over_sum, joint_mean / baseline_sum):
        raise ValidationError(
            f"{cw}.joint_over_sum_ratio {joint_over_sum} != "
            f"joint_completion_ms_mean / sum(baselines) {joint_mean / baseline_sum}")


def _validate_stream_split_cell(cell: Any, ci: int, repeats: int) -> None:
    cw = f"stream_split_cells[{ci}]"
    require_mapping(cell, cw)
    object_bytes = require_positive_int(
        require_key(cell, "object_bytes", cw), f"{cw}.object_bytes")
    copy_streams = require_positive_int(
        require_key(cell, "copy_streams", cw), f"{cw}.copy_streams")
    require_in(require_key(cell, "direction", cw), ("h2d", "d2h"), f"{cw}.direction")
    require_in(require_key(cell, "gap_label", cw), _GAP_LABELS, f"{cw}.gap_label")

    chunk_reported = require_positive_int(
        require_key(cell, "chunk_bytes", cw), f"{cw}.chunk_bytes")
    chunk_expected = math.ceil(object_bytes / copy_streams)
    if chunk_reported != chunk_expected:
        raise ValidationError(
            f"{cw}.chunk_bytes {chunk_reported} != ceil(object_bytes / "
            f"copy_streams) {chunk_expected} (does not describe a valid "
            f"covering split of object_bytes into copy_streams pieces)")

    latency_mean, _ = _require_sample_block(
        cell, "latency_ms_mean", "latency_ms_samples", repeats, cw)
    floor_ms = object_bytes / PCIE_MAX_PLAUSIBLE_BYTES_PER_MS
    if latency_mean < floor_ms and not _close(latency_mean, floor_ms):
        raise ValidationError(
            f"{cw}: non-physical latency_ms_mean {latency_mean} < {floor_ms} "
            f"(faster than any plausible PCIe link: {object_bytes} bytes in "
            f"{latency_mean} ms exceeds {PCIE_MAX_PLAUSIBLE_BYTES_PER_MS / 1e6:.0f} GB/s)")


def _validate_pcie_link_environment(env: Any) -> None:
    require_mapping(env, "pcie_link_environment")
    argv = require_list(require_key(env, "argv", "pcie_link_environment"),
                         "pcie_link_environment.argv")
    require_equal(argv, list(PCIE_LINK_ARGV), "pcie_link_environment.argv")
    status = require_in(
        require_key(env, "status", "pcie_link_environment"),
        ("ok", "unavailable"), "pcie_link_environment.status")
    if status == "ok":
        for field in ("pcie_link_gen_current", "pcie_link_gen_max",
                      "pcie_link_width_current", "pcie_link_width_max"):
            value = require_key(env, field, "pcie_link_environment")
            if not isinstance(value, str) or not value.strip():
                raise ValidationError(
                    f"pcie_link_environment.{field}: expected non-empty string, "
                    f"got {value!r}")
    else:
        reason = require_key(env, "reason", "pcie_link_environment")
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationError(
                f"pcie_link_environment.reason: expected non-empty string, "
                f"got {reason!r}")


def validate(result: Any) -> dict[str, Any]:
    root = require_mapping(result, "result")
    require_equal(require_key(root, "schema_version", "result"), SCHEMA, "schema_version")
    require_key(root, "backend", "result")
    require_key(root, "evidence", "result")
    require_in(require_key(root, "axis", "result"), _AXIS_CHOICES, "axis")
    repeats = require_positive_int(require_key(root, "repeats", "result"), "repeats")

    bidirectional_cells = require_list(
        require_key(root, "bidirectional_cells", "result"), "bidirectional_cells")
    stream_split_cells = require_list(
        require_key(root, "stream_split_cells", "result"), "stream_split_cells")
    for ci, cell in enumerate(bidirectional_cells):
        _validate_bidirectional_cell(cell, ci, repeats)
    for ci, cell in enumerate(stream_split_cells):
        _validate_stream_split_cell(cell, ci, repeats)

    _validate_pcie_link_environment(require_key(root, "pcie_link_environment", "result"))
    return root


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path")
    args = ap.parse_args(argv)
    root = validate(load_json(args.path))
    print(f"pcie_extension OK: {len(root['bidirectional_cells'])} bidirectional cells, "
          f"{len(root['stream_split_cells'])} stream_split cells, "
          f"axis={root['axis']}, evidence={root['evidence']}, "
          f"pcie_link_status={root['pcie_link_environment']['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Parser + validator for gpu-longctx-kv-result-v1 (priority 2 output).

Enforces byte-accounting conservation so a mis-shaped or internally inconsistent
result fails loudly instead of being trusted:

    kv_resident_bytes + kv_offloaded_bytes == kv_total_bytes == seq_len * 131072
    offload_engaged == (kv_offloaded_bytes > 0)

A validated result also checks that the reported boundary flag and knee agree
with the repeat-level observations.  A terminal OOM before the boundary remains
a valid, explicitly classified result, as required by the measurement contract.
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
        require_equal, require_positive_int, require_nonneg_int, require_type,
    )
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from measurement.parsers.common import (
        ValidationError, load_json, require_mapping, require_list, require_key,
        require_equal, require_positive_int, require_nonneg_int, require_type,
    )

SCHEMA = "gpu-longctx-kv-result-v1"
KV_BYTES_PER_TOKEN = 131072  # 128 KiB/token (SWAP-K2)
KV_BLOCK_BYTES = 2097152
KV_BLOCK_TOKENS = 16
FORMAL_SEQ_LENS = [4096, 16384, 65536, 131072, 262144, 524288, 1048576]
PRIMARY_MEAN_FIELDS = [
    "kv_resident_bytes",
    "kv_offloaded_bytes",
    "kv_offloaded_blocks",
    "ttft_ns",
    "decode_per_token_ns",
    "kv_move_ns",
    "kv_move_bytes",
]


def _nonnegative_number(value: Any, where: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{where}: expected non-negative number")
    if not math.isfinite(value) or value < 0:
        raise ValidationError(f"{where}: expected finite non-negative number")
    return value


def _close(left: int | float, right: int | float) -> bool:
    # Primary means can be non-integral JSON numbers.  This tolerance is below
    # one byte/ns throughout the target_2 range while allowing a final float ULP.
    return math.isclose(left, right, rel_tol=1e-15, abs_tol=1e-9)


def _nonempty_string(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{where}: expected nonempty string")
    return value


def _validate_success_measurement(
    rec: dict[str, Any], where: str, *, raw_repeat: bool
) -> dict[str, int | float | bool]:
    seq_len = require_positive_int(
        require_key(rec, "seq_len", where), f"{where}.seq_len"
    )
    total = require_positive_int(
        require_key(rec, "kv_total_bytes", where), f"{where}.kv_total_bytes"
    )
    number = require_nonneg_int if raw_repeat else _nonnegative_number
    resident = number(
        require_key(rec, "kv_resident_bytes", where),
        f"{where}.kv_resident_bytes",
    )
    offloaded = number(
        require_key(rec, "kv_offloaded_bytes", where),
        f"{where}.kv_offloaded_bytes",
    )
    expected_total = seq_len * KV_BYTES_PER_TOKEN
    if total != expected_total:
        raise ValidationError(
            f"{where}: kv_total_bytes {total} != seq_len*128KiB {expected_total}"
        )
    conserved = resident + offloaded
    conservation_failed = (
        conserved != total if raw_repeat else not _close(conserved, total)
    )
    if conservation_failed:
        raise ValidationError(
            f"{where}: resident+offloaded {conserved} != total {total}"
        )

    blocks_total = require_positive_int(
        require_key(rec, "kv_blocks_total", where),
        f"{where}.kv_blocks_total",
    )
    expected_blocks = (seq_len + KV_BLOCK_TOKENS - 1) // KV_BLOCK_TOKENS
    if blocks_total != expected_blocks:
        raise ValidationError(f"{where}: kv_blocks_total disagrees with seq_len")
    offloaded_blocks = number(
        require_key(rec, "kv_offloaded_blocks", where),
        f"{where}.kv_offloaded_blocks",
    )
    if offloaded_blocks > blocks_total:
        raise ValidationError(
            f"{where}: kv_offloaded_blocks exceeds kv_blocks_total"
        )
    if raw_repeat:
        expected_offloaded_blocks = (
            (offloaded + KV_BLOCK_BYTES - 1) // KV_BLOCK_BYTES if offloaded else 0
        )
        if offloaded_blocks != expected_offloaded_blocks:
            raise ValidationError(
                f"{where}: kv_offloaded_blocks disagrees with offloaded-byte accounting"
            )

    engaged = require_type(
        require_key(rec, "offload_engaged", where), bool,
        f"{where}.offload_engaged",
    )
    if engaged != (offloaded > 0):
        raise ValidationError(
            f"{where}: offload_engaged {engaged} inconsistent with "
            f"offloaded bytes {offloaded}"
        )
    if engaged != (offloaded_blocks > 0):
        raise ValidationError(
            f"{where}: offload_engaged inconsistent with offloaded block count"
        )

    ttft = number(require_key(rec, "ttft_ns", where), f"{where}.ttft_ns")
    decode = number(
        require_key(rec, "decode_per_token_ns", where),
        f"{where}.decode_per_token_ns",
    )
    move_ns = number(
        require_key(rec, "kv_move_ns", where), f"{where}.kv_move_ns"
    )
    move_bytes = number(
        require_key(rec, "kv_move_bytes", where), f"{where}.kv_move_bytes"
    )
    if not engaged and (move_ns != 0 or move_bytes != 0):
        raise ValidationError(
            f"{where}: nonzero KV move without offload engagement"
        )
    if engaged:
        if move_ns <= 0 or move_bytes <= 0:
            raise ValidationError(
                f"{where}: offload engagement requires positive KV move time/bytes"
            )
        if move_bytes < offloaded:
            raise ValidationError(
                f"{where}: kv_move_bytes is smaller than kv_offloaded_bytes"
            )

    return {
        "seq_len": seq_len,
        "kv_total_bytes": total,
        "kv_resident_bytes": resident,
        "kv_offloaded_bytes": offloaded,
        "kv_blocks_total": blocks_total,
        "kv_offloaded_blocks": offloaded_blocks,
        "offload_engaged": engaged,
        "ttft_ns": ttft,
        "decode_per_token_ns": decode,
        "kv_move_ns": move_ns,
        "kv_move_bytes": move_bytes,
    }


def _validate_worker_source(rec: dict[str, Any], where: str) -> None:
    source = require_mapping(
        require_key(rec, "measurement_source", where),
        f"{where}.measurement_source",
    )
    require_equal(
        require_key(source, "worker_hook_observed", f"{where}.measurement_source"),
        True,
        f"{where}.measurement_source.worker_hook_observed",
    )


def validate(result: Any) -> dict[str, Any]:
    root = require_mapping(result, "result")
    require_equal(require_key(root, "schema_version", "result"), SCHEMA, "schema_version")
    backend = require_key(root, "backend", "result")
    evidence = require_key(root, "evidence", "result")
    measured = evidence == "measured"
    requested = require_list(
        require_key(root, "seq_lens_requested", "result"), "seq_lens_requested"
    )
    if not requested or any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in requested
    ):
        raise ValidationError("seq_lens_requested must be a nonempty positive-int list")
    if requested != sorted(set(requested)):
        raise ValidationError("seq_lens_requested must be strictly increasing and unique")
    repeats = require_positive_int(require_key(root, "repeats", "result"), "repeats")
    require_equal(
        require_key(root, "primary_statistic", "result"),
        "arithmetic_mean",
        "primary_statistic",
    )
    require_equal(
        require_list(
            require_key(root, "primary_statistic_fields", "result"),
            "primary_statistic_fields",
        ),
        PRIMARY_MEAN_FIELDS,
        "primary_statistic_fields",
    )
    if measured:
        require_equal(repeats, 3, "repeats for measured target_2")
        require_equal(backend, "vllm_longctx_offload_on", "backend for measured target_2")
        require_equal(
            require_key(root, "runtime_variant", "result"),
            "offload-on",
            "runtime_variant",
        )
        identity = require_mapping(
            require_key(root, "runtime_identity", "result"), "runtime_identity"
        )
        resolved = require_mapping(
            require_key(identity, "resolved_config", "runtime_identity"),
            "runtime_identity.resolved_config",
        )
        require_equal(
            resolved.get("kv_offloading_backend"),
            "native",
            "runtime_identity.resolved_config.kv_offloading_backend",
        )
        offload_size = resolved.get("kv_offloading_size_gb")
        if offload_size == 16.0:
            if len(requested) != 1:
                raise ValidationError(
                    "16-GiB mechanism canary requires exactly one requested sequence"
                )
        elif offload_size == 140.0:
            require_equal(requested, FORMAL_SEQ_LENS, "formal target_2 sequence axis")
        else:
            raise ValidationError(
                "measured target_2 requires 16-GiB canary or 140-GiB formal offload"
            )
        environment = require_mapping(
            require_key(identity, "environment", "runtime_identity"),
            "runtime_identity.environment",
        )
        require_equal(
            environment.get("VLLM_USE_SIMPLE_KV_OFFLOAD", "__missing__"),
            None,
            "runtime_identity.environment.VLLM_USE_SIMPLE_KV_OFFLOAD",
        )
    records = require_list(require_key(root, "records", "result"), "records")
    if not records:
        raise ValidationError("records: empty; a long-context sweep must have points")

    saw_offload = False
    observed_seq_lens: list[int] = []
    saw_terminal = False
    for i, rec in enumerate(records):
        w = f"records[{i}]"
        rec = require_mapping(rec, w)
        terminal_failure = rec.get("measurement_failed") is True
        terminal_oom = rec.get("oom") is True
        if terminal_failure and terminal_oom:
            raise ValidationError(f"{w}: terminal record cannot be both OOM and failure")
        if terminal_failure or terminal_oom:
            require_equal(rec.get("stopped_sweep"), True, f"{w}.stopped_sweep")
            if i != len(records) - 1:
                kind = "OOM" if terminal_oom else "measurement failure"
                raise ValidationError(f"{w}: terminal {kind} is not last")
            seq_len = require_positive_int(
                require_key(rec, "seq_len", w), f"{w}.seq_len"
            )
            observed_seq_lens.append(seq_len)
            require_equal(rec.get("oom"), terminal_oom, f"{w}.oom")
            require_equal(
                rec.get("measurement_failed"), terminal_failure,
                f"{w}.measurement_failed",
            )
            _nonempty_string(require_key(rec, "error", w), f"{w}.error")
            classification = _nonempty_string(
                require_key(rec, "failure_classification", w),
                f"{w}.failure_classification",
            )
            if terminal_oom and classification != "CUDA_OR_ENGINE_OOM":
                raise ValidationError(f"{w}: OOM has non-OOM failure classification")
            if terminal_failure and classification == "CUDA_OR_ENGINE_OOM":
                raise ValidationError(f"{w}: non-OOM failure has OOM classification")
            repeat_index = require_nonneg_int(
                require_key(rec, "repeat_index", w), f"{w}.repeat_index"
            )
            if repeat_index >= repeats:
                raise ValidationError(f"{w}.repeat_index is outside repeat range")
            require_equal(
                require_key(rec, "repeats_expected", w), repeats,
                f"{w}.repeats_expected",
            )
            completed_count = require_nonneg_int(
                require_key(rec, "valid_repeats_completed", w),
                f"{w}.valid_repeats_completed",
            )
            require_equal(completed_count, repeat_index, f"{w}.valid_repeats_completed")
            completed = require_list(
                require_key(rec, "completed_repeat_measurements", w),
                f"{w}.completed_repeat_measurements",
            )
            if len(completed) != completed_count:
                raise ValidationError(
                    f"{w}.completed_repeat_measurements length disagrees with count"
                )
            for completed_index, row in enumerate(completed):
                rw = f"{w}.completed_repeat_measurements[{completed_index}]"
                row = require_mapping(row, rw)
                require_equal(
                    require_key(row, "repeat_index", rw), completed_index,
                    f"{rw}.repeat_index",
                )
                values = _validate_success_measurement(row, rw, raw_repeat=True)
                require_equal(values["seq_len"], seq_len, f"{rw}.seq_len")
                if measured:
                    _validate_worker_source(row, rw)
            saw_terminal = True
            continue

        if rec.get("stopped_sweep") is True:
            raise ValidationError(f"{w}: successful record cannot stop the sweep")
        primary = _validate_success_measurement(rec, w, raw_repeat=False)
        seq_len = int(primary["seq_len"])
        observed_seq_lens.append(seq_len)
        require_equal(require_key(rec, "repeats", w), repeats, f"{w}.repeats")
        require_equal(
            require_key(rec, "primary_statistic", w),
            "arithmetic_mean",
            f"{w}.primary_statistic",
        )
        require_equal(
            require_list(
                require_key(rec, "primary_statistic_fields", w),
                f"{w}.primary_statistic_fields",
            ),
            PRIMARY_MEAN_FIELDS,
            f"{w}.primary_statistic_fields",
        )

        repeat_rows = require_list(
            require_key(rec, "repeat_measurements", w),
            f"{w}.repeat_measurements",
        )
        if len(repeat_rows) != repeats:
            raise ValidationError(f"{w}.repeat_measurements must contain {repeats} repeats")
        raw_values: list[dict[str, int | float | bool]] = []
        for repeat_index, row in enumerate(repeat_rows):
            rw = f"{w}.repeat_measurements[{repeat_index}]"
            row = require_mapping(row, rw)
            require_equal(
                require_key(row, "repeat_index", rw), repeat_index,
                f"{rw}.repeat_index",
            )
            values = _validate_success_measurement(row, rw, raw_repeat=True)
            require_equal(values["seq_len"], seq_len, f"{rw}.seq_len")
            require_equal(
                values["kv_total_bytes"], primary["kv_total_bytes"],
                f"{rw}.kv_total_bytes",
            )
            require_equal(
                values["kv_blocks_total"], primary["kv_blocks_total"],
                f"{rw}.kv_blocks_total",
            )
            if measured:
                _validate_worker_source(row, rw)
            raw_values.append(values)

        for field in PRIMARY_MEAN_FIELDS:
            samples_name = f"{field}_repeats"
            samples = require_list(
                require_key(rec, samples_name, w), f"{w}.{samples_name}"
            )
            if len(samples) != repeats:
                raise ValidationError(
                    f"{w}.{samples_name} must contain {repeats} samples"
                )
            sample_values = [
                require_nonneg_int(sample, f"{w}.{samples_name}[{sample_index}]")
                for sample_index, sample in enumerate(samples)
            ]
            for sample_index, sample in enumerate(sample_values):
                if sample != raw_values[sample_index][field]:
                    raise ValidationError(
                        f"{w}.{samples_name}[{sample_index}] disagrees with raw repeat"
                    )
            expected_mean = sum(sample_values) / repeats
            if not _close(primary[field], expected_mean):
                raise ValidationError(
                    f"{w}.{field} {primary[field]} != arithmetic mean "
                    f"{expected_mean}"
                )

        engaged_samples = require_list(
            require_key(rec, "offload_engaged_repeats", w),
            f"{w}.offload_engaged_repeats",
        )
        if len(engaged_samples) != repeats:
            raise ValidationError(
                f"{w}.offload_engaged_repeats must contain {repeats} samples"
            )
        for repeat_index, engaged in enumerate(engaged_samples):
            require_type(engaged, bool, f"{w}.offload_engaged_repeats[{repeat_index}]")
            if engaged != raw_values[repeat_index]["offload_engaged"]:
                raise ValidationError(
                    f"{w}.offload_engaged_repeats[{repeat_index}] disagrees with raw repeat"
                )
        require_equal(
            primary["offload_engaged"], any(engaged_samples),
            f"{w}.offload_engaged",
        )
        if measured:
            _validate_worker_source(rec, w)
            source = require_mapping(rec["measurement_source"], f"{w}.measurement_source")
            observed_repeats = require_list(
                require_key(
                    source, "worker_hook_observed_repeats",
                    f"{w}.measurement_source",
                ),
                f"{w}.measurement_source.worker_hook_observed_repeats",
            )
            require_equal(
                observed_repeats, [True] * repeats,
                f"{w}.measurement_source.worker_hook_observed_repeats",
            )
        saw_offload = saw_offload or bool(primary["offload_engaged"])

    if observed_seq_lens != requested[:len(observed_seq_lens)]:
        raise ValidationError(
            "records are not the ordered prefix of seq_lens_requested"
        )
    if not saw_terminal and len(observed_seq_lens) != len(requested):
        raise ValidationError("non-terminal sweep omitted requested sequence lengths")

    require_type(
        require_key(root, "sweep_crossed_offload_boundary", "result"),
        bool, "sweep_crossed_offload_boundary",
    )
    if root["sweep_crossed_offload_boundary"] != saw_offload:
        raise ValidationError(
            "sweep_crossed_offload_boundary disagrees with per-record offload flags"
        )
    expected_knee = next(
        (
            rec["seq_len"] for rec in records
            if not rec.get("oom") and not rec.get("measurement_failed")
            and rec.get("offload_engaged")
        ),
        None,
    )
    require_equal(
        require_key(root, "offload_knee_seq_len", "result"),
        expected_knee,
        "offload_knee_seq_len",
    )
    return root


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path")
    ap.add_argument("--require-offload", action="store_true",
                    help="fail unless the sweep actually crossed the offload boundary")
    args = ap.parse_args(argv)
    root = validate(load_json(args.path))
    if args.require_offload and not root["sweep_crossed_offload_boundary"]:
        raise SystemExit(
            "VALIDATION FAILED: sweep never crossed the offload boundary; "
            "widen --seq-lens or lower the KV budget"
        )
    n = len([
        r for r in root["records"]
        if not r.get("oom") and not r.get("measurement_failed")
    ])
    print(f"longctx_kv OK: {n} records, crossed_offload="
          f"{root['sweep_crossed_offload_boundary']}, evidence={root['evidence']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Parser + validator for gpu-longctx-kv-result-v1 (priority 2 output).

Enforces byte-accounting conservation so a mis-shaped or internally inconsistent
result fails loudly instead of being trusted:

    kv_resident_bytes + kv_offloaded_bytes == kv_total_bytes == seq_len * 131072
    offload_engaged == (kv_offloaded_bytes > 0)

A validated result also asserts the *contract* invariant that the sweep actually
crossed the offload boundary -- a long-context sweep that never offloads has not
exercised the regime it exists to measure.
"""

from __future__ import annotations

import argparse
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


def validate(result: Any) -> dict[str, Any]:
    root = require_mapping(result, "result")
    require_equal(require_key(root, "schema_version", "result"), SCHEMA, "schema_version")
    require_key(root, "backend", "result")
    require_key(root, "evidence", "result")
    records = require_list(require_key(root, "records", "result"), "records")
    if not records:
        raise ValidationError("records: empty; a long-context sweep must have points")

    saw_offload = False
    for i, rec in enumerate(records):
        w = f"records[{i}]"
        require_mapping(rec, w)
        if rec.get("oom"):
            # An OOM record is terminal and need not carry byte accounting, but
            # it must be honestly flagged, never silently dropped.
            require_type(rec.get("stopped_sweep"), bool, f"{w}.stopped_sweep")
            continue
        seq_len = require_positive_int(require_key(rec, "seq_len", w), f"{w}.seq_len")
        total = require_positive_int(require_key(rec, "kv_total_bytes", w), f"{w}.kv_total_bytes")
        resident = require_nonneg_int(require_key(rec, "kv_resident_bytes", w), f"{w}.kv_resident_bytes")
        offloaded = require_nonneg_int(require_key(rec, "kv_offloaded_bytes", w), f"{w}.kv_offloaded_bytes")
        expected_total = seq_len * KV_BYTES_PER_TOKEN
        if total != expected_total:
            raise ValidationError(
                f"{w}: kv_total_bytes {total} != seq_len*128KiB {expected_total}"
            )
        if resident + offloaded != total:
            raise ValidationError(
                f"{w}: resident+offloaded {resident + offloaded} != total {total}"
            )
        engaged = require_type(require_key(rec, "offload_engaged", w), bool, f"{w}.offload_engaged")
        if engaged != (offloaded > 0):
            raise ValidationError(
                f"{w}: offload_engaged {engaged} inconsistent with offloaded bytes {offloaded}"
            )
        require_nonneg_int(require_key(rec, "ttft_ns", w), f"{w}.ttft_ns")
        require_nonneg_int(require_key(rec, "decode_per_token_ns", w), f"{w}.decode_per_token_ns")
        saw_offload = saw_offload or engaged

    require_type(
        require_key(root, "sweep_crossed_offload_boundary", "result"),
        bool, "sweep_crossed_offload_boundary",
    )
    if root["sweep_crossed_offload_boundary"] != saw_offload:
        raise ValidationError(
            "sweep_crossed_offload_boundary disagrees with per-record offload flags"
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
    n = len([r for r in root["records"] if not r.get("oom")])
    print(f"longctx_kv OK: {n} records, crossed_offload="
          f"{root['sweep_crossed_offload_boundary']}, evidence={root['evidence']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

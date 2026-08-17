#!/usr/bin/env python3
"""CLI: convert a raw MoE batch_expert_load_trace into a canonical event stream.

Usage:
    python scripts/build_canonical.py \
        --raw <batch_expert_load_trace.csv> \
        --meta <run_metadata.json> \
        --trace-id <id> \
        --out <canonical.jsonl> \
        [--characterize <characterization.json>]

Deterministic and re-runnable. Validates output against schemas/trace.schema.json
and performs ordering/dependency checks.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from edgeflow import canonical as C  # noqa: E402
from edgeflow import validate as V  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", required=True, help="batch_expert_load_trace.csv path")
    ap.add_argument("--meta", required=True, help="run_metadata.json path")
    ap.add_argument("--trace-id", required=True)
    ap.add_argument("--out", required=True, help="canonical .jsonl output path")
    ap.add_argument("--characterize", default=None, help="optional characterization json output")
    ap.add_argument("--include-zero-load", action="store_true")
    args = ap.parse_args()

    meta = json.loads(Path(args.meta).read_text(encoding="utf-8"))
    rows = C.load_batch_expert_load(args.raw)
    source_info = C.build_source_info(args.raw, meta)
    events = C.to_canonical_events(
        rows, meta, trace_id=args.trace_id, source_info=source_info,
        include_zero_load=args.include_zero_load,
    )

    n_valid, errors = V.validate_events(events)
    ordering_problems = V.validate_ordering(events)
    if errors or ordering_problems:
        print("VALIDATION FAILED", file=sys.stderr)
        for e in errors[:20]:
            print("  schema:", e, file=sys.stderr)
        for p in ordering_problems[:20]:
            print("  order:", p, file=sys.stderr)
        return 2

    C.write_jsonl(events, args.out)
    print(f"wrote {len(events)} canonical events -> {args.out}")
    print(f"schema_valid={n_valid}/{len(events)} ordering_problems={len(ordering_problems)}")

    if args.characterize:
        char = C.characterize(events)
        Path(args.characterize).parent.mkdir(parents=True, exist_ok=True)
        Path(args.characterize).write_text(json.dumps(char, indent=2) + "\n", encoding="utf-8")
        print(f"characterization -> {args.characterize}")
        print(json.dumps(char, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

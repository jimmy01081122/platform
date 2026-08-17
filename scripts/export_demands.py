#!/usr/bin/env python3
"""Export a canonical event stream to the compact demands format read by the
shared C scheduler kernel (firmware/main.c).

Format:
  line 1: <num_experts> <num_steps>
  next num_steps lines: <count> <expert_id> ...   (experts sorted, one step/line)

Steps are the per-(batch, layer_step) groups in canonical execution order,
identical to edgeflow.residency.demands_from_events, so the C kernel and the
Python reference model consume the exact same work-unit sequence.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from edgeflow import canonical as C  # noqa: E402
from edgeflow import residency as RS  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--canonical", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    events = C.read_jsonl(args.canonical)
    demands = RS.demands_from_events(events)
    num_experts = events[0]["attributes"]["num_experts"] if events else 0
    lines = [f"{num_experts} {len(demands)}"]
    for d in demands:
        lines.append(f"{len(d.experts)} " + " ".join(str(e) for e in d.experts))
    Path(args.out).write_text("\n".join(lines) + "\n")
    print(f"wrote {len(demands)} steps, {num_experts} experts -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

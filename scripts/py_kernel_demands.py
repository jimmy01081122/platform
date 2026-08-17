#!/usr/bin/env python3
"""Run the Python reference residency kernel directly on a demands.txt file, so
the reference model consumes the EXACT same work-unit stream as the C/RV64/RTL
layers (used by the S7 four-layer equivalence check).
"""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from edgeflow import residency as RS  # noqa: E402


def load_demands(path: str) -> tuple[int, list[RS.LayerDemand]]:
    lines = Path(path).read_text().split("\n")
    ne, ns = (int(x) for x in lines[0].split())
    demands = []
    for s in range(ns):
        parts = [int(x) for x in lines[1 + s].split()]
        experts = sorted(parts[1:1 + parts[0]])
        demands.append(RS.LayerDemand(batch="0", layer_step=s, experts=experts,
                                      assigned_tokens={e: 1 for e in experts}))
    return ne, demands


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--demands", required=True)
    ap.add_argument("--capacity", type=int, required=True)
    ap.add_argument("--depth", type=int, required=True)
    args = ap.parse_args()
    _, demands = load_demands(args.demands)
    policy = "prefetch" if args.depth > 0 else "on_demand"
    r = RS.simulate(demands, capacity=args.capacity, prefetch_depth=args.depth, policy=policy)
    out = {
        "target": "python", "capacity": args.capacity, "depth": args.depth,
        "demand_misses": r.demand_misses, "prefetch_hits": r.prefetch_hits,
        "transfers": r.transfers, "evictions": r.evictions,
        "wasted_prefetches": r.wasted_prefetches, "total_demands": r.total_demands,
    }
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

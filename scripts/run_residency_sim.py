#!/usr/bin/env python3
"""S2 CLI: sweep MoE expert residency/prefetch policies over capacity and depth.

Primary (robust) output: miss rate vs capacity, independent of any timing
assumption. Secondary (assumption-dependent) output: stall/total time under a
platform cost model with registered/swept bandwidth.

Usage:
    python scripts/run_residency_sim.py \
        --canonical <canonical.jsonl> \
        --model configs/model/switch_base_8.yaml \
        --platform configs/platforms/discrete_edge_workstation.yaml \
        --link-bandwidth-gbps 16,32 \
        --link-latency-us 2 \
        --copy-engines 2 \
        --compute-time-per-layer-us 200 \
        --out-csv <sweep.csv> --out-json <summary.json>
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import yaml  # noqa: E402

from edgeflow import canonical as C  # noqa: E402
from edgeflow import residency as RS  # noqa: E402


def _floats(s: str) -> list[float]:
    return [float(x) for x in s.split(",") if x.strip()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--canonical", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--platform", required=True)
    ap.add_argument("--capacities", default="1,2,3,4,5,6,7,8")
    ap.add_argument("--depths", default="0,1,2")
    ap.add_argument("--link-bandwidth-gbytes-per-s", default="16", help="GB/s comma list, swept (A-006/A-007)")
    ap.add_argument("--link-latency-us", type=float, default=2.0)
    ap.add_argument("--copy-engines", type=int, default=2)
    ap.add_argument("--compute-time-per-layer-us", type=float, default=None,
                    help="if omitted, timing columns are left null (robust-only run)")
    ap.add_argument("--out-csv", required=True)
    ap.add_argument("--out-json", required=True)
    args = ap.parse_args()

    events = C.read_jsonl(args.canonical)
    demands = RS.demands_from_events(events)
    model = yaml.safe_load(Path(args.model).read_text())
    platform = yaml.safe_load(Path(args.platform).read_text())
    expert_bytes = int(model["derived"]["expert_weight_bytes"])
    profile_id = platform.get("profile_id", "unknown")

    capacities = [int(x) for x in args.capacities.split(",")]
    depths = [int(x) for x in args.depths.split(",")]
    bw_list_gbytes = _floats(args.link_bandwidth_gbytes_per_s)
    bandwidths = [g * 1e9 for g in bw_list_gbytes]  # GB/s -> bytes/s
    latency_s = args.link_latency_us * 1e-6
    compute_s = args.compute_time_per_layer_us * 1e-6 if args.compute_time_per_layer_us else None

    rows: list[dict] = []
    for bw in bandwidths:
        cost = RS.PlatformCost(
            profile_id=profile_id, expert_weight_bytes=expert_bytes,
            link_bandwidth_bytes_per_s=bw, link_latency_s=latency_s,
            copy_engines=args.copy_engines,
        )
        for cap in capacities:
            plans = [("on_demand", 0), ("lru", 0)]
            for d in depths:
                if d > 0:
                    plans.append(("prefetch", d))
            for policy, depth in plans:
                res = RS.simulate(demands, capacity=cap, prefetch_depth=depth,
                                  policy=policy, cost=cost, per_layer_compute_time_s=compute_s)
                row = res.to_dict()
                row.pop("extra", None)
                row["link_bandwidth_gbytes_per_s"] = round(bw / 1e9, 3)
                row["expert_weight_bytes"] = expert_bytes
                rows.append(row)

    # write CSV
    fieldnames = list(rows[0].keys())
    Path(args.out_csv).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

    # robust summary: miss rate by capacity for on_demand vs best prefetch
    by_cap: dict[int, dict[str, float]] = {}
    for r in rows:
        if r["link_bandwidth_gbytes_per_s"] != rows[0]["link_bandwidth_gbytes_per_s"]:
            continue
        cap = r["capacity"]
        by_cap.setdefault(cap, {})
        key = f"{r['policy']}_d{r['prefetch_depth']}"
        by_cap[cap][key] = r["miss_rate"]

    summary = {
        "num_demands": demands and sum(len(d.experts) for d in demands),
        "num_layer_steps": len(demands),
        "num_experts": events[0]["attributes"]["num_experts"] if events else None,
        "expert_weight_bytes": expert_bytes,
        "capacities": capacities,
        "depths": depths,
        "bandwidths_gbytes_per_s": bw_list_gbytes,
        "miss_rate_by_capacity": by_cap,
        "rows": len(rows),
    }
    Path(args.out_json).write_text(json.dumps(summary, indent=2) + "\n")
    print(f"wrote {len(rows)} rows -> {args.out_csv}")
    print(json.dumps(summary["miss_rate_by_capacity"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

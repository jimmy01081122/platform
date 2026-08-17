#!/usr/bin/env python3
"""S7 cross-platform (P-D vs P-I) sensitivity under the SAME workload, algorithm
revision, and experiment contract.

The residency DECISION and its counters (misses/transfers/...) are platform
independent (verified python==C==rv64==RTL). Platforms differ only in the cost of
MOVING an expert weight and in control-hiding. This script sweeps the transfer
bandwidth for both platform families and asks: does expert-weight transfer still
dominate the per-step control cost (the S4 conclusion), and where is the crossover?

Nothing is fabricated:
  - transfers/step, expert_bytes: measured/derived (RTL==golden; A-005).
  - control costs: measured/derived (S4, run 20260716T191240Z__s4_break_even__S4).
  - bandwidths: SWEPT ranges (P-D device link; P-I shared memory) with an explicit
    P-I contention factor (also swept). No bare vendor default is used as truth.
"""
from __future__ import annotations
import argparse, csv, json
from pathlib import Path

# measured/derived work-unit facts (switch-base-32, C=28, depth-1)
TRANSFERS_TOTAL = 1377          # RTL == golden C == rv64 == python
NUM_STEPS = 192
EXPERT_BYTES = 9_437_184        # A-005 (derived, resolved)

# control cost per step (us), from S4 (measured decision + swept sync)
CTRL_US = {"hardware": 0.18, "software": 2.07, "firmware": 10.44}

# SWEPT bandwidth ranges (bytes/s)
PD_LINK_BW = {"pcie_lo_8GBs": 8e9, "pcie_mid_16GBs": 16e9, "pcie_hi_32GBs": 32e9}
PI_SHARED_BW = {"shared_25GBs": 25e9, "shared_51GBs": 51e9, "shared_102GBs": 102e9}
PI_CONTENTION = {"heavy_0.4": 0.4, "moderate_0.6": 0.6, "light_0.8": 0.8}


def t_transfer_us(bytes_per_step: float, bw: float) -> float:
    return bytes_per_step / bw * 1e6


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-csv", required=True)
    ap.add_argument("--out-json", required=True)
    args = ap.parse_args()

    tpstep = TRANSFERS_TOTAL / NUM_STEPS           # transfers per step (steady traced rate)
    bytes_per_step = tpstep * EXPERT_BYTES
    ctrl_max = max(CTRL_US.values())               # firmware is the largest control cost

    rows = []
    # P-D
    for name, bw in PD_LINK_BW.items():
        tt = t_transfer_us(bytes_per_step, bw)
        rows.append({
            "platform": "P-D", "config": name, "eff_bw_GBs": round(bw / 1e9, 2),
            "t_transfer_us_per_step": round(tt, 2),
            "transfer_dominates_control": tt > ctrl_max,
            "ratio_transfer_over_ctrlmax": round(tt / ctrl_max, 1),
        })
    # P-I (aggregate x contention)
    for bname, bw in PI_SHARED_BW.items():
        for cname, cf in PI_CONTENTION.items():
            eff = bw * cf
            tt = t_transfer_us(bytes_per_step, eff)
            rows.append({
                "platform": "P-I", "config": f"{bname}|{cname}",
                "eff_bw_GBs": round(eff / 1e9, 2),
                "t_transfer_us_per_step": round(tt, 2),
                "transfer_dominates_control": tt > ctrl_max,
                "ratio_transfer_over_ctrlmax": round(tt / ctrl_max, 1),
            })

    # crossover: effective BW at which a SINGLE expel transfer per step equals each
    # control cost (control becomes exposed only beyond this BW at ~1 transfer/step)
    crossover_GBs = {
        p: round(EXPERT_BYTES / (c * 1e-6) / 1e9, 1)  # bytes / (us->s) -> B/s -> GB/s
        for p, c in CTRL_US.items()
    }

    summary = {
        "operating_point": {"fixture": "switch-base-32", "capacity": 28, "depth": 1},
        "work_unit_facts": {
            "transfers_per_step": round(tpstep, 3), "expert_bytes": EXPERT_BYTES,
            "bytes_per_step": int(bytes_per_step), "evidence": "measured/derived (RTL==golden; A-005)",
        },
        "control_cost_us_per_step": CTRL_US,
        "bandwidth_evidence": "swept (P-D device link; P-I shared memory x contention)",
        "all_points": rows,
        "min_ratio_transfer_over_ctrlmax": min(r["ratio_transfer_over_ctrlmax"] for r in rows),
        "transfer_dominates_on_all_swept_points": all(r["transfer_dominates_control"] for r in rows),
        "single_transfer_crossover_GBs": crossover_GBs,
        "conclusion": (
            "Across ALL swept P-D and P-I bandwidths, expert-weight transfer time per "
            "step (hundreds of us to ms) dominates the per-step control cost (<=10.4 us), "
            "so the S4 SW-sufficiency-for-decision conclusion is platform-insensitive for "
            "this MoE workload. P-I only shortens absolute transfer latency (higher shared "
            "BW) but contention erodes it; the SW/FW/HW decision-placement break-even does "
            "not flip. Control becomes exposed only above ~%.0f GB/s effective at ~1 "
            "transfer/step (beyond current P-D/P-I), or for sub-expert streaming. The "
            "durable HW value remains the autonomous prefetch/DMA-issue datapath (S3 miss "
            "reduction without host intervention), on BOTH platforms." % crossover_GBs["software"]
        ),
    }

    Path(args.out_csv).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)
    Path(args.out_json).write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""S4 break-even surface for scheduler placement (software / firmware / hardware).

Same work unit and frozen algorithm across all placements (verified: python == C
== rv64). The bulk data movement (move_data bucket) is a shared DMA cost and does
NOT differ by placement. The placements differ in per-invocation CONTROL cost:

    control_cost[p] = decision_time[p] + sync_overhead[p]      (per layer-step)

where decision_time is MEASURED (software: native host time; firmware: RV64
retired instructions / (freq * IPC)) and hardware is a DERIVED cycle estimate to
be validated by the S5 RTL. sync_overhead is a registered/swept per-invocation
dispatch/synchronization cost.

A placement's control is "hidden" if control_cost[p] <= T_avail, the time the
GPU spends computing+transferring per layer-step (which overlaps control). The
break-even surface is computed over two independent axes:

    X: T_avail per step (proxy for work size / compute+transfer overlap)
    Y: software sync overhead per invocation (swept)

Only measured/derived/swept quantities are used; nothing is fabricated.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path


def load_decision_costs(path: str, capacity: int) -> dict:
    sw = fw = None
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r["capacity"] != capacity:
            continue
        if r["target"] == "native":
            sw = r
        elif r["target"] == "rv64":
            fw = r
    if not sw or not fw:
        raise SystemExit(f"missing native/rv64 rows for capacity {capacity}")
    return {"sw": sw, "fw": fw}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--decision-costs", required=True)
    ap.add_argument("--capacity", type=int, default=28)
    # firmware (A-003 freq, A-011 CPI) swept/registered
    ap.add_argument("--fw-freq-ghz", type=float, default=1.0)
    ap.add_argument("--fw-cpi", type=float, default=1.0)
    # hardware (A-012 freq, A-013 cycles/step derived) — validated at S5
    ap.add_argument("--hw-freq-ghz", type=float, default=0.5)
    ap.add_argument("--hw-cycles-per-step", type=float, default=64.0)
    # sync overheads per invocation (A-010), microseconds
    ap.add_argument("--sw-sync-us", default="1,5,10,20")
    ap.add_argument("--fw-sync-us", type=float, default=0.5)
    ap.add_argument("--hw-sync-us", type=float, default=0.05)
    ap.add_argument("--out-csv", required=True)
    ap.add_argument("--out-json", required=True)
    args = ap.parse_args()

    dc = load_decision_costs(args.decision_costs, args.capacity)
    # decision time per step (microseconds)
    sw_decision_us = dc["sw"]["ns_per_step"] / 1000.0                 # measured
    fw_instr = dc["fw"]["instructions_per_step"]                       # measured
    fw_decision_us = fw_instr * args.fw_cpi / (args.fw_freq_ghz * 1e3) # instr/(GHz*1e3)=us
    hw_decision_us = args.hw_cycles_per_step / (args.hw_freq_ghz * 1e3)  # derived

    sw_syncs = [float(x) for x in args.sw_sync_us.split(",")]

    # T_avail grid (us): log-spaced from 0.01us to 10 ms
    t_grid = [10 ** (x / 4.0) for x in range(-8, 17)]  # ~0.01us .. ~10000us

    rows = []
    for sw_sync in sw_syncs:
        ctrl = {
            "software": sw_decision_us + sw_sync,
            "firmware": fw_decision_us + args.fw_sync_us,
            "hardware": hw_decision_us + args.hw_sync_us,
        }
        for t in t_grid:
            hidden = {p: (ctrl[p] <= t) for p in ctrl}
            # recommended placement: cheapest that is hidden; else min control cost
            hidden_ps = [p for p in ctrl if hidden[p]]
            if hidden_ps:
                # prefer software > firmware > hardware among hidden (least HW cost)
                order = ["software", "firmware", "hardware"]
                rec = next(p for p in order if p in hidden_ps)
            else:
                rec = min(ctrl, key=lambda p: ctrl[p])
            rows.append({
                "sw_sync_us": sw_sync,
                "t_avail_us": round(t, 4),
                "sw_control_us": round(ctrl["software"], 4),
                "fw_control_us": round(ctrl["firmware"], 4),
                "hw_control_us": round(ctrl["hardware"], 4),
                "sw_hidden": int(hidden["software"]),
                "fw_hidden": int(hidden["firmware"]),
                "hw_hidden": int(hidden["hardware"]),
                "recommended": rec,
            })

    Path(args.out_csv).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    # break-even T_avail per placement (control cost = break-even T_avail)
    summary = {
        "capacity": args.capacity,
        "measured": {
            "sw_decision_us": round(sw_decision_us, 4),
            "fw_instructions_per_step": fw_instr,
        },
        "derived_or_swept": {
            "fw_decision_us": round(fw_decision_us, 4),
            "fw_freq_ghz": args.fw_freq_ghz, "fw_cpi": args.fw_cpi,
            "hw_decision_us": round(hw_decision_us, 4),
            "hw_freq_ghz": args.hw_freq_ghz, "hw_cycles_per_step": args.hw_cycles_per_step,
            "fw_sync_us": args.fw_sync_us, "hw_sync_us": args.hw_sync_us,
        },
        "break_even_t_avail_us": {},
        "hardware_only_window_us": {},
    }
    for sw_sync in sw_syncs:
        ctrl = {
            "software": sw_decision_us + sw_sync,
            "firmware": fw_decision_us + args.fw_sync_us,
            "hardware": hw_decision_us + args.hw_sync_us,
        }
        summary["break_even_t_avail_us"][str(sw_sync)] = {p: round(ctrl[p], 4) for p in ctrl}
        lo = ctrl["hardware"]
        hi = min(ctrl["software"], ctrl["firmware"])
        summary["hardware_only_window_us"][str(sw_sync)] = [round(lo, 4), round(hi, 4)] if hi > lo else None

    Path(args.out_json).write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

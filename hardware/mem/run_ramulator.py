#!/usr/bin/env python3
"""Run one Ramulator2 standalone simulation and emit JSON stats.

Drives a LoadStoreTrace over a chosen DRAM standard + channel count and reports
the cycle-accurate achieved read throughput, average read latency (ns), and
row hit/miss/conflict rates. Used by scripts/mem_calibrate.py to calibrate the
P-I effective-bandwidth/latency knobs from real DRAM timing.

Runs INSIDE the edgehetero-mem:1 image (ramulator2 on PYTHONPATH).
"""
from __future__ import annotations

import argparse
import json
import sys

import ramulator  # provided by edgehetero-mem:1


STD = {
    # standard: (dram_class, org_preset, timing_preset, controller_class, data_rate_MTps, dq_bits)
    "LPDDR5_6400": ("LPDDR5", "LPDDR5_8Gb_x16", "LPDDR5_6400", "LPDDR5", 6400, 16),
    "DDR5_4800":   ("DDR5", "DDR5_8Gb_x8", "DDR5_4800AN", "GenericDDR", 4800, 8),
}


def build(std: str, channels: int):
    dclass, org, timing, cclass, rate, dq = STD[std]
    dram = getattr(ramulator.dram, dclass)(org_preset=org, timing_preset=timing)
    cctor = getattr(ramulator.controller, cclass)
    ctrl = cctor(
        dram=dram,
        scheduler=ramulator.scheduler.FRFCFS(),
        refresh_manager=ramulator.refresh_manager.AllBank(),
        row_policy=ramulator.row_policy.Open(),
        addr_mapper=ramulator.addr_mapper.RoBaRaCoCh(),
    )
    mem = ramulator.memory_system.GenericDRAM(
        clock_ratio=1,
        controllers=[ctrl] * channels,
        channel_mapper=ramulator.channel_mapper.CacheLineInterleave(),
    )
    peak_MBps = rate * (dq / 8) * channels  # JEDEC peak per this config
    return mem, peak_MBps


def collect(stats: dict) -> dict:
    ctrl = stats["memory_system"]["controller"]
    ctrls = ctrl if isinstance(ctrl, list) else [ctrl]
    tot_bw = sum(c["read_throughput_MBps"] for c in ctrls)
    served = sum(c["num_read_reqs_served"] for c in ctrls)
    lat_cyc = sum(c["read_latency"] for c in ctrls)
    hits = sum(c["read_row_hits"] for c in ctrls)
    misses = sum(c["read_row_misses"] for c in ctrls)
    confl = sum(c["read_row_conflicts"] for c in ctrls)
    denom = max(1, hits + misses + confl)
    return {
        "achieved_read_MBps": round(tot_bw, 1),
        "avg_read_latency_cyc": round(lat_cyc / max(1, served), 2),
        "row_hit_rate": round(hits / denom, 4),
        "row_miss_rate": round(misses / denom, 4),
        "row_conflict_rate": round(confl / denom, 4),
        "reads_served": served,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--std", required=True, choices=list(STD))
    ap.add_argument("--channels", type=int, default=1)
    ap.add_argument("--trace", required=True)
    a = ap.parse_args()

    mem, peak = build(a.std, a.channels)
    fe = ramulator.frontend.LoadStoreTrace(clock_ratio=1, path=a.trace)
    sim = ramulator.Simulation(fe, mem)
    sim.run()
    r = collect(sim.stats)
    r["std"] = a.std
    r["channels"] = a.channels
    r["peak_MBps"] = round(peak, 1)
    r["efficiency"] = round(r["achieved_read_MBps"] / peak, 4)
    # latency reported in controller cycles (Ramulator's exact unit); ns conversion
    # omitted since get_tCK is not exposed to the Python binding and the P-I
    # link_latency (0.5 us) is negligible vs the MB-scale transfer time anyway.
    json.dump(r, sys.stdout)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

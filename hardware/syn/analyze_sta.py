#!/usr/bin/env python3
"""Analyze real gate-level STA-lite results (Nangate45 typical).

Turns the S6 AIG-depth proxy into REAL units: critical-path delay (ns), Fmax
(MHz), and cell area (um^2). Evidence class: derived (FreePDK45 academic PDK,
cell delays only, no wire parasitics / no sign-off STA). Also folds in the
standalone LRU-victim argmin fix (combinational vs sequential).
"""
import csv, sys, os

TARGET_MHZ = 200.0

def load(path):
    rows = []
    with open(path) as f:
        for r in csv.DictReader(f):
            if r.get("sta_ok") != "1":
                continue
            try:
                rows.append({
                    "ne": int(r["max_experts"]), "ts": int(r["ts_w"]),
                    "ns": float(r["delay_ns"]), "fmax": float(r["fmax_mhz"]),
                    "area": float(r["chip_area_um2"]),
                })
            except (ValueError, KeyError):
                pass
    return rows

def main():
    csv_path = sys.argv[1] if len(sys.argv) > 1 else "syn/out_sta/sta_dse.csv"
    rows = load(csv_path)
    if not rows:
        print("no STA rows"); sys.exit(1)

    print("== Real gate-level STA-lite (Nangate45 typical, cell-only) ==")
    print(f"{'NE':>3} {'TS':>3} {'delay_ns':>9} {'Fmax_MHz':>9} {'area_um2':>10} {'>=200MHz':>8}")
    for r in sorted(rows, key=lambda x: (x["ne"], x["ts"])):
        meets = "yes" if r["fmax"] >= TARGET_MHZ else "no"
        print(f"{r['ne']:>3} {r['ts']:>3} {r['ns']:>9.3f} {r['fmax']:>9.2f} {r['area']:>10.1f} {meets:>8}")

    # Functionally-required region for the switch-base-32 workload: NE>=32 and
    # TS_W high enough to avoid LRU wrap (TS_W>=16 per S6 boundary A-014).
    valid = [r for r in rows if r["ne"] >= 32 and r["ts"] >= 16]
    print("\n-- functionally-valid region (NE>=32, TS_W>=16) --")
    for r in sorted(valid, key=lambda x: -x["fmax"]):
        print(f"  ({r['ne']},{r['ts']}): {r['fmax']:.2f} MHz, {r['ns']:.3f} ns, {r['area']:.0f} um^2")
    best_valid = max(valid, key=lambda x: x["fmax"]) if valid else None
    any_meets = any(r["fmax"] >= TARGET_MHZ for r in valid)
    print(f"\n  target {TARGET_MHZ:.0f} MHz met in valid region: {'YES' if any_meets else 'NO'}")
    if best_valid:
        print(f"  best valid single-cycle-argmin point: ({best_valid['ne']},{best_valid['ts']}) "
              f"= {best_valid['fmax']:.2f} MHz")

    # Pareto in real units (min area, max Fmax)
    pts = sorted(rows, key=lambda x: (x["area"], -x["fmax"]))
    pareto, bf = [], -1.0
    for r in pts:
        if r["fmax"] > bf:
            pareto.append(r); bf = r["fmax"]
    print("\n-- area/Fmax Pareto front (real units) --")
    for r in pareto:
        print(f"  ({r['ne']},{r['ts']}): {r['area']:.0f} um^2, {r['fmax']:.2f} MHz")

    # argmin fix (standalone, verified equivalent by tb_lru_victim)
    fix = os.path.join(os.path.dirname(csv_path), "argmin_fix.csv")
    if os.path.exists(fix):
        print("\n-- LRU argmin critical-path fix (standalone, N=32, TSW=16) --")
        with open(fix) as f:
            impls = {r["impl"]: r for r in csv.DictReader(f)}
        for k in ("comb", "seq"):
            if k in impls:
                r = impls[k]
                print(f"  {k:>4}: {float(r['delay_ns']):.3f} ns "
                      f"({float(r['fmax_mhz']):.1f} MHz), area {float(r['area_um2']):.0f} um^2")
        if "comb" in impls and "seq" in impls:
            c, s = float(impls["comb"]["delay_ns"]), float(impls["seq"]["delay_ns"])
            print(f"  speedup: {c/s:.2f}x  ({c:.2f} ns -> {s:.2f} ns), "
                  f"crosses {TARGET_MHZ:.0f} MHz: {'YES' if 1e3/s >= TARGET_MHZ else 'NO'}")

if __name__ == "__main__":
    main()

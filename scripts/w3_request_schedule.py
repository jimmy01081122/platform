#!/usr/bin/env python3
"""W3 request-schedule + cross-domain sensitivity (charter W3 "request schedule",
W2 "request diversity"/"burstiness").

Part A - request schedule: serve K requests under ONE shared residency of capacity
C and compare three schedules with identical work:
  * isolated    : each request runs on a fresh residency (sum of per-request misses)
                  -> no cross-request sharing (upper bound on misses).
  * sequential  : requests back-to-back on a warm shared residency
                  -> captures cross-request reuse of hot experts.
  * interleaved : round-robin the (step,layer) demands of the K requests
                  -> models concurrent serving; shared hot experts help, capacity
                     contention hurts.
Reveals whether multi-request serving is residency-friendly (shared skew) or
thrash-prone (contention) for a given model.

Part B - cross-domain sensitivity: per workload domain, the residency miss-rate
and prefetch benefit, to quantify how routing locality varies with the domain
(code vs general vs Chinese knowledge).

All assumption-free (demand-driven counters only).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from edgeflow.moe_routing import expand_to_expert_demand  # noqa: E402
from edgeflow.residency import demands_from_events, simulate, LayerDemand  # noqa: E402

CANON = ROOT / "data" / "canonical" / "moe_routing_v1"
VARIANT = "Qwen/Qwen3-235B-A22B-FP8"


def load_query(rel: str) -> list[dict]:
    with open(CANON / rel) as f:
        return [json.loads(l) for l in f]


def demands_of(q) -> list[LayerDemand]:
    return demands_from_events(expand_to_expert_demand(load_query(q["canonical_path"])))


def miss_rate(dm: list[LayerDemand], cap: int, depth: int, policy: str):
    r = simulate(dm, cap, depth, policy)
    return r.demand_misses, r.total_demands


def sched_isolated(dms, cap, depth, policy):
    tot_m = tot_d = 0
    for dm in dms:
        m, d = miss_rate(dm, cap, depth, policy)
        tot_m += m; tot_d += d
    return tot_m, tot_d


def sched_sequential(dms, cap, depth, policy):
    concat = [d for dm in dms for d in dm]
    return miss_rate(concat, cap, depth, policy)


def sched_interleaved(dms, cap, depth, policy):
    inter: list[LayerDemand] = []
    maxlen = max(len(dm) for dm in dms)
    for i in range(maxlen):
        for dm in dms:
            if i < len(dm):
                inter.append(dm[i])
    return miss_rate(inter, cap, depth, policy)


def main() -> int:
    manifest = json.loads((CANON / "manifest.json").read_text())
    qall = [q for q in manifest["queries"] if q["model"] == VARIANT]
    ne = qall[0]["dims"]["num_experts_observed"]
    cap = max(1, int(round(ne * 0.5)))

    report = {"variant": VARIANT, "num_experts": ne, "capacity": cap,
              "request_schedule": {}, "cross_domain": {}}

    # ---- Part A: request schedule (mmlu/abstract_algebra pool) ----
    pool = sorted([q for q in qall if q["benchmark"] == "mmlu"],
                  key=lambda x: int(x["query_id"]) if x["query_id"].isdigit() else 1 << 30)
    dms_pool = [demands_of(q) for q in pool[:8]]
    for K in (2, 4, 8):
        if K > len(dms_pool):
            continue
        dms = dms_pool[:K]
        row = {}
        for name, fn in (("isolated", sched_isolated), ("sequential", sched_sequential),
                         ("interleaved", sched_interleaved)):
            m_od, d_od = fn(dms, cap, 0, "on_demand")
            m_pf, d_pf = fn(dms, cap, 2, "prefetch")
            row[name] = {
                "on_demand_miss_rate": round(m_od / d_od, 4),
                "prefetch_miss_rate": round(m_pf / d_pf, 4),
                "on_demand_misses": m_od, "total_demands": d_od,
            }
        base = row["isolated"]["on_demand_miss_rate"]
        row["seq_vs_isolated_miss_delta_pct"] = round((row["sequential"]["on_demand_miss_rate"] - base) / base * 100, 2)
        row["inter_vs_isolated_miss_delta_pct"] = round((row["interleaved"]["on_demand_miss_rate"] - base) / base * 100, 2)
        report["request_schedule"][f"K={K}"] = row

    # ---- Part B: cross-domain sensitivity ----
    domains = sorted(set(q["benchmark"] for q in qall))
    for dom in domains:
        qs = [q for q in qall if q["benchmark"] == dom]
        qs = sorted(qs, key=lambda x: int(x["query_id"]) if x["query_id"].isdigit() else 1 << 30)[:3]
        ods = []; pfs = []
        for q in qs:
            dm = demands_of(q)
            m_od, d_od = miss_rate(dm, cap, 0, "on_demand")
            m_pf, d_pf = miss_rate(dm, cap, 2, "prefetch")
            ods.append(m_od / d_od); pfs.append(m_pf / d_pf)
        n = len(ods)
        mean_od = sum(ods) / n; mean_pf = sum(pfs) / n
        report["cross_domain"][dom] = {
            "queries": n,
            "on_demand_miss_mean": round(mean_od, 4),
            "prefetch_miss_mean": round(mean_pf, 4),
            "miss_reduction_pct": round((mean_od - mean_pf) / mean_od * 100, 1) if mean_od else 0.0,
        }

    (CANON / "w3_request_schedule.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    _md(report)
    _print(report)
    return 0


def _print(r):
    print(f"{r['variant']} experts={r['num_experts']} C={r['capacity']}")
    print("\n-- request schedule (shared residency) --")
    for k, row in r["request_schedule"].items():
        print(f" {k}: isolated od={row['isolated']['on_demand_miss_rate']:.3f} "
              f"seq={row['sequential']['on_demand_miss_rate']:.3f} "
              f"({row['seq_vs_isolated_miss_delta_pct']:+.1f}%) "
              f"interleaved={row['interleaved']['on_demand_miss_rate']:.3f} "
              f"({row['inter_vs_isolated_miss_delta_pct']:+.1f}%) | "
              f"prefetch iso={row['isolated']['prefetch_miss_rate']:.3f} seq={row['sequential']['prefetch_miss_rate']:.3f}")
    print("\n-- cross-domain sensitivity --")
    for dom, d in r["cross_domain"].items():
        print(f" {dom:<14} on_demand={d['on_demand_miss_mean']:.3f} prefetch={d['prefetch_miss_mean']:.3f} "
              f"reduction={d['miss_reduction_pct']:.1f}% (n={d['queries']})")


def _md(r):
    out = ROOT / "explorations" / "moe_orchestration" / "W3_REQUEST_SCHEDULE.md"
    L = ["# W3 Request Schedule + Cross-Domain Sensitivity\n",
         f"Model `{r['variant']}`, {r['num_experts']} experts, C={r['capacity']} (0.5N), "
         "assumption-free demand counters.\n\n",
         "## Request schedule (K requests, one shared residency)\n\n",
         "| K | isolated on-demand | sequential (delta) | interleaved (delta) | isolated prefetch | sequential prefetch |\n",
         "|---|---|---|---|---|---|\n"]
    for k, row in r["request_schedule"].items():
        L.append(f"| {k[2:]} | {row['isolated']['on_demand_miss_rate']} | "
                 f"{row['sequential']['on_demand_miss_rate']} ({row['seq_vs_isolated_miss_delta_pct']:+.1f}%) | "
                 f"{row['interleaved']['on_demand_miss_rate']} ({row['inter_vs_isolated_miss_delta_pct']:+.1f}%) | "
                 f"{row['isolated']['prefetch_miss_rate']} | {row['sequential']['prefetch_miss_rate']} |\n")
    L.append("\nSequential = requests back-to-back on a warm shared residency; "
             "interleaved = round-robin concurrent serving. A negative delta vs isolated "
             "means multi-request serving REDUCES misses (shared hot experts); positive "
             "means capacity contention dominates.\n\n")
    L.append("## Cross-domain sensitivity\n\n")
    L.append("| domain | on-demand miss | prefetch miss | miss reduction | n |\n|---|---|---|---|---|\n")
    for dom, d in r["cross_domain"].items():
        L.append(f"| {dom} | {d['on_demand_miss_mean']} | {d['prefetch_miss_mean']} | "
                 f"{d['miss_reduction_pct']}% | {d['queries']} |\n")
    out.write_text("".join(L))


if __name__ == "__main__":
    raise SystemExit(main())

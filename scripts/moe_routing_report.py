#!/usr/bin/env python3
"""Routing statistics + sampling convergence + routing-level residency baseline.

Consumes canonical moe-routing-v1 JSONL (data/canonical/moe_routing_v1/) and:
  1. per-query + per-variant routing statistics (measured; assumption-free);
  2. a sampling-convergence check (expert-load L1 stability vs #queries);
  3. a routing-level (W2) residency baseline: on_demand vs prefetch demand-driven
     metrics over a capacity sweep. These metrics depend ONLY on measured demand
     order + capacity + policy (no bandwidth/latency assumption), so they are the
     robust layer of the vertical slice. Device-timing (W3/H2+) is attached later.

Outputs (committed): data/canonical/moe_routing_v1/routing_stats.json and
explorations/moe_orchestration/ROUTING_BASELINE.md
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from edgeflow.moe_routing import routing_stats, expand_to_expert_demand  # noqa: E402
from edgeflow.residency import demands_from_events, simulate  # noqa: E402

CANON = ROOT / "data" / "canonical" / "moe_routing_v1"


def load_query(rel_path: str) -> list[dict]:
    recs = []
    with open(CANON / rel_path) as f:
        for line in f:
            recs.append(json.loads(line))
    return recs


def l1(a: Counter, b: Counter, keys) -> float:
    ta = sum(a.values()) or 1
    tb = sum(b.values()) or 1
    return sum(abs(a[k] / ta - b[k] / tb) for k in keys)


def main() -> int:
    manifest = json.loads((CANON / "manifest.json").read_text())
    by_variant: dict[str, list[dict]] = defaultdict(list)
    for q in manifest["queries"]:
        by_variant[q["model"]].append(q)

    report = {"dataset": manifest["dataset"], "source_revision": manifest["source_revision"],
              "variants": {}}

    for variant, queries in by_variant.items():
        ne = queries[0]["dims"]["num_experts_observed"]
        # aggregate expert load across all queries of this variant + per-query stats
        agg_load = Counter()
        per_query_load: list[Counter] = []
        per_query_stats = []
        all_records_for_baseline = None
        for q in sorted(queries, key=lambda x: (x["benchmark"], x["subject"], x["query_id"])):
            recs = load_query(q["canonical_path"])
            st = routing_stats(recs)
            per_query_stats.append({
                "benchmark": q["benchmark"], "subject": q["subject"], "query_id": q["query_id"],
                "active_experts": st["active_experts"], "coverage": round(st["coverage_fraction"], 4),
                "entropy_norm": round(st["expert_entropy_normalized"], 4),
                "gini": round(st["gini_load_imbalance"], 4),
                "ws_prefill_mean": round(st["working_set_prefill_mean"], 2),
                "ws_decode_mean": round(st["working_set_decode_mean"], 2),
            })
            c = Counter({int(k): v for k, v in st["expert_load"].items()})
            per_query_load.append(c)
            agg_load += c
            if all_records_for_baseline is None:
                all_records_for_baseline = recs  # first query = baseline stimulus

        # --- sampling convergence: L1 of cumulative-k load vs full aggregate ---
        keys = list(range(ne))
        conv = []
        cum = Counter()
        for k, c in enumerate(per_query_load, 1):
            cum += c
            conv.append({"k_queries": k, "l1_vs_full": round(l1(cum, agg_load, keys), 4)})

        agg_stats_full = {
            "num_experts": ne,
            "active_experts": len([e for e in keys if agg_load[e] > 0]),
            "coverage_fraction": round(len([e for e in keys if agg_load[e] > 0]) / ne, 4),
            "gini_load_imbalance": round(_gini([agg_load[e] for e in keys]), 4),
        }

        # --- routing-level residency baseline (W2, assumption-free) ---
        dm = demands_from_events(expand_to_expert_demand(all_records_for_baseline))
        baseline = []
        for frac in (0.25, 0.5, 0.75):
            cap = max(1, int(round(ne * frac)))
            od = simulate(dm, capacity=cap, prefetch_depth=0, policy="on_demand")
            pf = simulate(dm, capacity=cap, prefetch_depth=2, policy="prefetch")
            baseline.append({
                "capacity": cap, "capacity_frac": frac, "total_demands": od.total_demands,
                "on_demand_miss_rate": round(od.miss_rate, 4),
                "prefetch_miss_rate": round(pf.miss_rate, 4),
                "on_demand_transfers": od.transfers,
                "prefetch_transfers": pf.transfers,
                "prefetch_hits": pf.prefetch_hits,
                "prefetch_wasted": pf.wasted_prefetches,
                "miss_reduction": round((od.miss_rate - pf.miss_rate) / od.miss_rate, 4) if od.miss_rate else 0.0,
            })

        report["variants"][variant] = {
            "num_experts": ne,
            "num_layers": queries[0]["dims"]["num_layers"],
            "num_moe_layers": queries[0]["dims"]["num_moe_layers"],
            "top_k": queries[0]["dims"]["top_k"],
            "queries": len(queries),
            "aggregate": agg_stats_full,
            "per_query": per_query_stats,
            "sampling_convergence": conv,
            "residency_baseline_query": {
                "stimulus": f"{sorted(queries, key=lambda x:(x['benchmark'],x['subject'],x['query_id']))[0]['benchmark']}/"
                            f"{sorted(queries, key=lambda x:(x['benchmark'],x['subject'],x['query_id']))[0]['query_id']}",
                "sweep": baseline,
            },
        }

    (CANON / "routing_stats.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    _write_md(report)
    _print(report)
    return 0


def _gini(values):
    xs = sorted(values); n = len(xs); s = sum(xs)
    if n == 0 or s == 0:
        return 0.0
    cum = sum(i * x for i, x in enumerate(xs, 1))
    return (2 * cum) / (n * s) - (n + 1) / n


def _print(report):
    for v, info in report["variants"].items():
        print(f"\n=== {v} ===")
        print(f"  experts={info['num_experts']} moe_layers={info['num_moe_layers']}/{info['num_layers']} "
              f"top_k={info['top_k']} queries={info['queries']}")
        a = info["aggregate"]
        print(f"  aggregate coverage={a['coverage_fraction']} gini={a['gini_load_imbalance']}")
        conv = info["sampling_convergence"][-1]
        print(f"  convergence L1(k={conv['k_queries']} vs full)={conv['l1_vs_full']}")
        print("  residency baseline (query stimulus, assumption-free):")
        for b in info["residency_baseline_query"]["sweep"]:
            print(f"    C={b['capacity']:>3} ({b['capacity_frac']:.2f}N): "
                  f"on_demand_miss={b['on_demand_miss_rate']:.3f} "
                  f"prefetch_miss={b['prefetch_miss_rate']:.3f} "
                  f"reduction={b['miss_reduction']*100:.1f}% wasted={b['prefetch_wasted']}")


def _write_md(report):
    out = ROOT / "explorations" / "moe_orchestration" / "ROUTING_BASELINE.md"
    lines = ["# Large-MoE Routing Baseline (W2, routing-level, assumption-free)\n",
             f"Dataset: `{report['dataset']}` @ `{report['source_revision']}`\n",
             "Metrics below depend ONLY on measured routing demand order, residency "
             "capacity C, and policy. No bandwidth/latency is assumed (that is the "
             "W3/H2+ device-timing layer, attached later).\n"]
    for v, info in report["variants"].items():
        lines.append(f"\n## {v}\n")
        lines.append(f"- experts={info['num_experts']}, MoE layers={info['num_moe_layers']}/{info['num_layers']}, "
                     f"top_k={info['top_k']}, queries={info['queries']}\n")
        a = info["aggregate"]
        lines.append(f"- aggregate coverage={a['coverage_fraction']}, load Gini={a['gini_load_imbalance']}\n")
        lines.append("\n| C (frac N) | on-demand miss | prefetch miss | miss reduction | wasted pf |\n")
        lines.append("|---|---|---|---|---|\n")
        for b in info["residency_baseline_query"]["sweep"]:
            lines.append(f"| {b['capacity']} ({b['capacity_frac']:.2f}N) | {b['on_demand_miss_rate']:.3f} | "
                         f"{b['prefetch_miss_rate']:.3f} | {b['miss_reduction']*100:.1f}% | {b['prefetch_wasted']} |\n")
    out.write_text("".join(lines))


if __name__ == "__main__":
    raise SystemExit(main())

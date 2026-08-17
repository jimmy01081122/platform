#!/usr/bin/env python3
"""Sampling convergence test for the large-MoE routing workload (charter task 10).

Question: how many query traces are needed before the workload statistics that
drive the residency conclusion stabilize? We measure two convergences on the
Qwen3-235B mmlu/abstract_algebra pool (30 queries), with bootstrap 95% CIs:

  1. expert-load distribution: L1 distance of a k-query aggregate to the full-pool
     distribution (mean +/- CI over bootstrap resamples of k queries);
  2. residency miss-rate (on-demand and prefetch, C=0.5N): population mean +/- std,
     and the k-sample-mean CI vs the full-pool mean.

k* = smallest k whose bootstrap-mean metric is within tolerance. Nothing is
fabricated: all inputs are measured routing; CIs come from resampling the real
query pool.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from edgeflow.moe_routing import expand_to_expert_demand  # noqa: E402
from edgeflow.residency import demands_from_events, simulate  # noqa: E402

CANON = ROOT / "data" / "canonical" / "moe_routing_v1"
TARGET_VARIANT = "Qwen/Qwen3-235B-A22B-FP8"
TARGET_BENCH = "mmlu"
TARGET_SUBJECT = "abstract_algebra"
L1_TOL = 0.05
REL_TOL = 0.05
B = 500
SEED = 12345


def load_query(rel: str) -> list[dict]:
    with open(CANON / rel) as f:
        return [json.loads(l) for l in f]


def main() -> int:
    manifest = json.loads((CANON / "manifest.json").read_text())
    qs = [q for q in manifest["queries"]
          if q["model"] == TARGET_VARIANT and q["benchmark"] == TARGET_BENCH
          and q["subject"] == TARGET_SUBJECT]
    qs.sort(key=lambda x: int(x["query_id"]) if x["query_id"].isdigit() else 1 << 30)
    n = len(qs)
    if n < 5:
        print(f"need >=5 queries, have {n}", file=sys.stderr)
        return 2
    ne = qs[0]["dims"]["num_experts_observed"]
    cap = max(1, int(round(ne * 0.5)))

    # per-query expert-load vectors + miss rates
    loads = np.zeros((n, ne), dtype=np.float64)
    od_miss = np.zeros(n)
    pf_miss = np.zeros(n)
    for i, q in enumerate(qs):
        recs = load_query(q["canonical_path"])
        for r in recs:
            for tok in r["selected_experts"]:
                for e in tok:
                    loads[i, e] += 1
        dm = demands_from_events(expand_to_expert_demand(recs))
        od_miss[i] = simulate(dm, cap, 0, "on_demand").miss_rate
        pf_miss[i] = simulate(dm, cap, 2, "prefetch").miss_rate

    full_dist = loads.sum(0)
    full_dist = full_dist / full_dist.sum()
    rng = np.random.default_rng(SEED)

    # --- convergence 1: expert-load L1 vs full ---
    conv_load = []
    k_star_load = None
    for k in range(1, n + 1):
        l1s = np.empty(B)
        for b in range(B):
            idx = rng.integers(0, n, size=k)
            agg = loads[idx].sum(0)
            agg = agg / agg.sum()
            l1s[b] = np.abs(agg - full_dist).sum()
        mean = float(l1s.mean())
        lo, hi = np.percentile(l1s, [2.5, 97.5])
        conv_load.append({"k": k, "l1_mean": round(mean, 4),
                          "l1_ci95": [round(float(lo), 4), round(float(hi), 4)]})
        if k_star_load is None and hi < L1_TOL:
            k_star_load = k

    # --- convergence 2: miss-rate mean vs full-pool mean ---
    full_od = float(od_miss.mean())
    full_pf = float(pf_miss.mean())
    conv_miss = []
    k_star_miss = None
    for k in range(1, n + 1):
        means = np.empty(B)
        for b in range(B):
            idx = rng.integers(0, n, size=k)
            means[b] = od_miss[idx].mean()
        lo, hi = np.percentile(means, [2.5, 97.5])
        halfwidth = (hi - lo) / 2
        conv_miss.append({"k": k, "od_miss_mean": round(float(means.mean()), 4),
                          "ci95": [round(float(lo), 4), round(float(hi), 4)],
                          "ci_halfwidth_rel": round(halfwidth / full_od, 4) if full_od else 0.0})
        if k_star_miss is None and halfwidth <= REL_TOL * full_od:
            k_star_miss = k

    report = {
        "target": f"{TARGET_VARIANT}/{TARGET_BENCH}/{TARGET_SUBJECT}",
        "num_queries": n, "num_experts": ne, "capacity": cap,
        "bootstrap_resamples": B, "seed": SEED,
        "population": {
            "on_demand_miss_mean": round(full_od, 4), "on_demand_miss_std": round(float(od_miss.std()), 4),
            "prefetch_miss_mean": round(full_pf, 4), "prefetch_miss_std": round(float(pf_miss.std()), 4),
            "miss_reduction_mean": round((full_od - full_pf) / full_od, 4) if full_od else 0.0,
        },
        "convergence_expert_load": {"tolerance_l1": L1_TOL, "k_star": k_star_load, "curve": conv_load},
        "convergence_miss_rate": {"rel_tolerance": REL_TOL, "k_star": k_star_miss, "curve": conv_miss},
    }
    (CANON / "convergence.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    _md(report)
    _print(report)
    return 0


def _print(r):
    p = r["population"]
    print(f"target {r['target']}  n={r['num_queries']} experts={r['num_experts']} C={r['capacity']}")
    print(f"population on_demand miss = {p['on_demand_miss_mean']} +/- {p['on_demand_miss_std']}, "
          f"prefetch = {p['prefetch_miss_mean']} +/- {p['prefetch_miss_std']}, "
          f"reduction = {p['miss_reduction_mean']*100:.1f}%")
    print(f"expert-load L1<{r['convergence_expert_load']['tolerance_l1']}: k* = {r['convergence_expert_load']['k_star']}")
    print(f"miss-rate CI halfwidth<={r['convergence_miss_rate']['rel_tolerance']*100:.0f}% of mean: "
          f"k* = {r['convergence_miss_rate']['k_star']}")
    print("k :  L1_mean [ci]        od_miss_mean [ci]  ci_halfwidth_rel")
    for a, b in zip(r["convergence_expert_load"]["curve"], r["convergence_miss_rate"]["curve"]):
        if a["k"] in (1, 2, 3, 5, 8, 10, 15, 20, 25, 30):
            print(f"{a['k']:>2}: {a['l1_mean']:.3f} {str(a['l1_ci95']):<16} "
                  f"{b['od_miss_mean']:.3f} {str(b['ci95']):<16} {b['ci_halfwidth_rel']:.3f}")


def _md(r):
    out = ROOT / "explorations" / "moe_orchestration" / "SAMPLING_CONVERGENCE.md"
    p = r["population"]
    L = ["# Sampling Convergence Test\n",
         f"Target: `{r['target']}`, n={r['num_queries']} queries, {r['num_experts']} experts, "
         f"C={r['capacity']} (0.5N), bootstrap B={r['bootstrap_resamples']}.\n\n",
         f"Population residency (C=0.5N): on-demand miss {p['on_demand_miss_mean']} +/- "
         f"{p['on_demand_miss_std']}, prefetch {p['prefetch_miss_mean']} +/- {p['prefetch_miss_std']}, "
         f"mean reduction {p['miss_reduction_mean']*100:.1f}%.\n\n",
         f"- Expert-load distribution converges (bootstrap L1 CI upper < {r['convergence_expert_load']['tolerance_l1']}) "
         f"at **k* = {r['convergence_expert_load']['k_star']}** queries.\n",
         f"- Residency miss-rate mean converges (bootstrap CI halfwidth <= "
         f"{r['convergence_miss_rate']['rel_tolerance']*100:.0f}% of the mean) at "
         f"**k* = {r['convergence_miss_rate']['k_star']}** queries.\n\n",
         "| k | expert-load L1 mean [95% CI] | on-demand miss mean [95% CI] | CI halfwidth (rel) |\n",
         "|---|---|---|---|\n"]
    for a, b in zip(r["convergence_expert_load"]["curve"], r["convergence_miss_rate"]["curve"]):
        if a["k"] in (1, 2, 3, 5, 8, 10, 15, 20, 25, 30):
            L.append(f"| {a['k']} | {a['l1_mean']} {a['l1_ci95']} | {b['od_miss_mean']} {b['ci95']} | {b['ci_halfwidth_rel']} |\n")
    out.write_text("".join(L))


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""W3 capacity x prefetch-depth DSE on the large-MoE workload (criterion-A DSE).

For each model, sweep residency capacity C (fraction of num_experts) and prefetch
depth d, on the measured large-MoE demand stream. Report the assumption-free
demand counters (miss rate, transfers, wasted prefetches), pick:
  * optimal prefetch depth d* (min miss rate at C=0.5N, ties -> smallest d);
  * knee capacity C* (smallest C whose miss rate is within KNEE_TOL of the best
    miss rate at that depth) = recommended residency sizing.

This closes the DSE step of success-criterion A on the real workload and feeds
the residency-engine sizing question (MAX_EXPERTS) back to the S6 synthesis DSE.
All counters depend only on demand order + capacity + depth (no device timing).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from edgeflow.model_config import ModelConfig  # noqa: E402
from edgeflow.moe_routing import expand_to_expert_demand  # noqa: E402
from edgeflow.residency import demands_from_events, simulate  # noqa: E402

CANON = ROOT / "data" / "canonical" / "moe_routing_v1"
MODEL_CFG = ROOT / "configs" / "model" / "moe"
CAP_FRACS = [0.125, 0.25, 0.375, 0.5, 0.625, 0.75, 0.875]
DEPTHS = [0, 1, 2, 4, 8]
KNEE_TOL = 0.10  # within 10% of best miss rate at that depth


def stimulus_for(variant, manifest):
    qs = [q for q in manifest["queries"] if q["model"] == variant]
    qs.sort(key=lambda x: (x["benchmark"], x["subject"], x["query_id"]))
    return qs[0]["canonical_path"]


def load_query(rel):
    with open(CANON / rel) as f:
        return [json.loads(l) for l in f]


def main() -> int:
    manifest = json.loads((CANON / "manifest.json").read_text())
    report = {"cap_fracs": CAP_FRACS, "depths": DEPTHS, "knee_tol": KNEE_TOL, "models": {}}

    for cfg_path in sorted(MODEL_CFG.glob("*.json")):
        mc = ModelConfig.load(cfg_path)
        variant = mc.trace_variant
        rel = stimulus_for(variant, manifest)
        dm = demands_from_events(expand_to_expert_demand(load_query(rel)))
        ne = mc.num_experts

        grid = {}
        for d in DEPTHS:
            policy = "on_demand" if d == 0 else "prefetch"
            row = []
            for f in CAP_FRACS:
                cap = max(1, int(round(ne * f)))
                r = simulate(dm, cap, d, policy)
                row.append({"cap_frac": f, "capacity": cap, "miss_rate": round(r.miss_rate, 4),
                            "transfers": r.transfers, "wasted": r.wasted_prefetches})
            grid[str(d)] = row

        # optimal depth at C=0.5N
        c50 = min(range(len(CAP_FRACS)), key=lambda i: abs(CAP_FRACS[i] - 0.5))
        best_d, best_mr = None, 1e9
        for d in DEPTHS:
            mr = grid[str(d)][c50]["miss_rate"]
            if mr < best_mr - 1e-9:
                best_mr, best_d = mr, d
        # knee capacity at optimal depth
        col = grid[str(best_d)]
        best_at_maxc = min(x["miss_rate"] for x in col)
        knee = next((x for x in col if x["miss_rate"] <= best_at_maxc * (1 + KNEE_TOL)), col[-1])

        report["models"][variant] = {
            "num_experts": ne, "top_k": mc.top_k, "num_moe_layers": mc.num_moe_layers,
            "stimulus": rel, "grid": grid,
            "optimal_depth_at_C50": best_d, "miss_at_C50_optdepth": best_mr,
            "knee_capacity": knee["capacity"], "knee_cap_frac": knee["cap_frac"],
            "knee_miss_rate": knee["miss_rate"],
            "rtl_engine_max_experts_current": 32,
            "engine_sizing_gap": ne - 32,
        }

    (CANON / "w3_capacity_dse.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    _md(report)
    _print(report)
    return 0


def _print(r):
    for v, info in r["models"].items():
        print(f"\n=== {v} (experts={info['num_experts']}, top_k={info['top_k']}) ===")
        print(f"  optimal prefetch depth @C=0.5N: d*={info['optimal_depth_at_C50']} "
              f"(miss {info['miss_at_C50_optdepth']:.4f})")
        print(f"  knee capacity C*={info['knee_capacity']} ({info['knee_cap_frac']:.3f}N), "
              f"miss={info['knee_miss_rate']:.4f}")
        print(f"  engine sizing: needs MAX_EXPERTS>={info['num_experts']} (current RTL 32, "
              f"gap {info['engine_sizing_gap']})")
        print("  miss-rate grid (rows=depth, cols=cap frac):")
        print("    d\\C  " + "  ".join(f"{f:>6}" for f in r["cap_fracs"]))
        for d in r["depths"]:
            print(f"    {d:>2}   " + "  ".join(f"{x['miss_rate']:>6.3f}" for x in info["grid"][str(d)]))


def _md(r):
    out = ROOT / "explorations" / "moe_orchestration" / "W3_CAPACITY_DSE.md"
    L = ["# W3 Capacity x Prefetch-Depth DSE (large-MoE workload)\n",
         "Assumption-free demand counters over measured routing. `d*` = optimal "
         "prefetch depth at C=0.5N; `C*` = knee capacity (miss rate within "
         f"{int(r['knee_tol']*100)}% of the best at d*).\n"]
    for v, info in r["models"].items():
        L.append(f"\n## {v}\n")
        L.append(f"- experts={info['num_experts']}, top_k={info['top_k']}, "
                 f"MoE layers={info['num_moe_layers']}\n")
        L.append(f"- **optimal depth d* = {info['optimal_depth_at_C50']}** (miss "
                 f"{info['miss_at_C50_optdepth']:.4f} @C=0.5N)\n")
        L.append(f"- **knee capacity C* = {info['knee_capacity']} "
                 f"({info['knee_cap_frac']:.3f}N)**, miss {info['knee_miss_rate']:.4f}\n")
        L.append(f"- engine sizing: needs MAX_EXPERTS >= {info['num_experts']} "
                 f"(current RTL parameter 32 -> S6 synthesis DSE must re-sweep for "
                 f"{info['num_experts']} experts)\n")
        L.append("\n| depth \\ cap frac | " + " | ".join(f"{f}" for f in r["cap_fracs"]) + " |\n")
        L.append("|" + "---|" * (len(r["cap_fracs"]) + 1) + "\n")
        for d in r["depths"]:
            L.append(f"| d={d} | " + " | ".join(f"{x['miss_rate']}" for x in info["grid"][str(d)]) + " |\n")
    out.write_text("".join(L))


if __name__ == "__main__":
    raise SystemExit(main())

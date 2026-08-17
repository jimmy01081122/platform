#!/usr/bin/env python3
"""W3 device-timed residency baseline for large-MoE routing.

Combines the three calibration sources, each provenance-tagged and separable:
  1. routing behavior  : W2 measured demand order (canonical moe-routing-v1);
  2. model dimensions  : registered config -> expert_weight_bytes (DERIVED);
  3. device service     : platform profile -> bandwidth/latency (H1, swept).

Robust (assumption-free-ish) device-timed outputs: transfer time per expert and
critical-path stall for on_demand vs prefetch at C=0.5N. The per-layer COMPUTE
time is NOT fabricated: instead we report the break-even compute time t* (the
per-step compute at which transfer stall equals compute), which partitions each
model x platform point into transfer-bound (residency/bandwidth is the limiter,
offload/prefetch matters) vs compute-bound (transfer hidden, accelerate compute).

Outputs: data/canonical/moe_routing_v1/w3_device_timing.json and
explorations/moe_orchestration/W3_DEVICE_TIMING.md
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from edgeflow.model_config import ModelConfig  # noqa: E402
from edgeflow.moe_routing import expand_to_expert_demand  # noqa: E402
from edgeflow.residency import demands_from_events, simulate, PlatformCost  # noqa: E402

CANON = ROOT / "data" / "canonical" / "moe_routing_v1"
MODEL_CFG = ROOT / "configs" / "model" / "moe"
PLAT_CFG = ROOT / "configs" / "platform"


def stimulus_for(variant: str, manifest: dict) -> str:
    qs = [q for q in manifest["queries"] if q["model"] == variant]
    qs.sort(key=lambda x: (x["benchmark"], x["subject"], x["query_id"]))
    return qs[0]["canonical_path"]


def load_query(rel: str) -> list[dict]:
    with open(CANON / rel) as f:
        return [json.loads(l) for l in f]


def pd_costs(bytes_: int, plat: dict):
    for name, bw in plat["link_bandwidth_bytes_per_s_sweep"].items():
        yield name, round(bw / 1e9, 1), PlatformCost(
            profile_id="P-D", expert_weight_bytes=bytes_,
            link_bandwidth_bytes_per_s=bw, link_latency_s=plat["link_latency_s"],
            copy_engines=plat["copy_engines"], prefetch_bw_fraction=1.0)


def pi_costs(bytes_: int, plat: dict):
    for bname, bw in plat["link_bandwidth_bytes_per_s_sweep"].items():
        for cname, cf in plat["contention_sweep"].items():
            eff = bw * cf
            yield f"{bname}|{cname}", round(eff / 1e9, 1), PlatformCost(
                profile_id="P-I", expert_weight_bytes=bytes_,
                link_bandwidth_bytes_per_s=eff, link_latency_s=plat["link_latency_s"],
                copy_engines=plat["copy_engines"], prefetch_bw_fraction=cf)


def main() -> int:
    manifest = json.loads((CANON / "manifest.json").read_text())
    pd = json.loads((PLAT_CFG / "p_d_discrete.json").read_text())
    pi = json.loads((PLAT_CFG / "p_i_integrated.json").read_text())

    report = {"dataset": manifest["dataset"], "source_revision": manifest["source_revision"],
              "note": "expert_weight_bytes derived from model config; bandwidth swept (H1).",
              "models": {}}

    for cfg_path in sorted(MODEL_CFG.glob("*.json")):
        mc = ModelConfig.load(cfg_path)
        variant = mc.trace_variant
        rel = stimulus_for(variant, manifest)
        recs = load_query(rel)
        dm = demands_from_events(expand_to_expert_demand(recs))
        cap = max(1, int(round(mc.num_experts * 0.5)))
        ebytes = mc.expert_weight_bytes()
        n_steps = len(dm)

        # dummy compute=0 to isolate the transfer stall (compute-independent stall)
        rows = []
        for plat_name, gen in (("P-D", pd_costs(ebytes, pd)), ("P-I", pi_costs(ebytes, pi))):
            for cfg_name, eff_gbs, cost in gen:
                od = simulate(dm, cap, 0, "on_demand", cost=cost, per_layer_compute_time_s=0.0)
                pf = simulate(dm, cap, 2, "prefetch", cost=cost, per_layer_compute_time_s=0.0)
                tt_us = cost.transfer_time_s() * 1e6
                od_stall_ms = (od.total_stall_time_s or 0) * 1e3
                pf_stall_ms = (pf.total_stall_time_s or 0) * 1e3
                # break-even per-step compute (us) at which on_demand transfer is hidden
                t_star_us = (od.total_stall_time_s or 0) / n_steps * 1e6
                rows.append({
                    "platform": plat_name, "config": cfg_name, "eff_bw_GBs": eff_gbs,
                    "transfer_time_per_expert_us": round(tt_us, 2),
                    "on_demand_stall_ms": round(od_stall_ms, 3),
                    "prefetch_stall_ms": round(pf_stall_ms, 3),
                    "stall_reduction_pct": round((od_stall_ms - pf_stall_ms) / od_stall_ms * 100, 1) if od_stall_ms else 0.0,
                    "breakeven_compute_us_per_step": round(t_star_us, 3),
                })
        report["models"][variant] = {
            "config": mc.summary(),
            "capacity": cap, "num_steps": n_steps,
            "on_demand_misses": simulate(dm, cap, 0, "on_demand").demand_misses,
            "prefetch_misses": simulate(dm, cap, 2, "prefetch").demand_misses,
            "stimulus": rel,
            "sweep": rows,
        }

    (CANON / "w3_device_timing.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    _md(report)
    _print(report)
    return 0


def _print(report):
    for v, info in report["models"].items():
        c = info["config"]
        print(f"\n=== {v} ===")
        print(f"  expert={c['expert_weight_MiB']} MiB ({c['precision']}), experts={c['num_experts']}, "
              f"C={info['capacity']}, steps={info['num_steps']}, "
              f"misses on_demand={info['on_demand_misses']} prefetch={info['prefetch_misses']}")
        for r in info["sweep"]:
            print(f"  {r['platform']:>3} {r['config']:<24} bw={r['eff_bw_GBs']:>5}GB/s "
                  f"t_xfer={r['transfer_time_per_expert_us']:>7}us "
                  f"stall od={r['on_demand_stall_ms']:>8}ms pf={r['prefetch_stall_ms']:>8}ms "
                  f"(-{r['stall_reduction_pct']:>4}%) t*={r['breakeven_compute_us_per_step']}us")


def _md(report):
    out = ROOT / "explorations" / "moe_orchestration" / "W3_DEVICE_TIMING.md"
    L = ["# W3 Device-Timed MoE Residency Baseline\n",
         f"Dataset `{report['dataset']}` @ `{report['source_revision']}`.\n",
         "expert_weight_bytes is DERIVED from registered model config; link bandwidth "
         "is SWEPT (H1 service model). Stall = critical-path expert-transfer time for "
         "on_demand vs prefetch at C=0.5N. `t*` = break-even per-step compute time: if "
         "the real per-MoE-layer compute exceeds `t*` the transfer is hidden "
         "(compute-bound); below it the point is transfer-bound (residency/bandwidth is "
         "the limiter and prefetch/offload matters).\n"]
    for v, info in report["models"].items():
        c = info["config"]
        L.append(f"\n## {v}\n")
        L.append(f"- expert={c['expert_weight_MiB']} MiB ({c['precision']}), experts={c['num_experts']}, "
                 f"top_k={c['top_k']}, MoE layers={c['num_moe_layers']}, C={info['capacity']}, "
                 f"steps={info['num_steps']}\n")
        L.append(f"- demand misses: on_demand={info['on_demand_misses']}, prefetch={info['prefetch_misses']}\n")
        L.append("\n| plat | config | eff BW (GB/s) | t_xfer/expert (us) | on-demand stall (ms) | prefetch stall (ms) | stall reduction | break-even compute/step (us) |\n")
        L.append("|---|---|---|---|---|---|---|---|\n")
        for r in info["sweep"]:
            L.append(f"| {r['platform']} | {r['config']} | {r['eff_bw_GBs']} | "
                     f"{r['transfer_time_per_expert_us']} | {r['on_demand_stall_ms']} | "
                     f"{r['prefetch_stall_ms']} | {r['stall_reduction_pct']}% | "
                     f"{r['breakeven_compute_us_per_step']} |\n")
    out.write_text("".join(L))


if __name__ == "__main__":
    raise SystemExit(main())

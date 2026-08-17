#!/usr/bin/env python3
"""W3 copy-engine scheduling DSE (the named slice component: "copy-engine scheduling").

Question: on a discrete CPU-GPU link, how many DMA copy engines are worth provisioning
for large-MoE expert-weight offload, and when do more engines stop helping?

Physically correct model (shared_link_bandwidth=True, DECISION_LOG D-048): E copy
engines share ONE link of bandwidth B. A step with m critical-path demand-miss loads
then costs

    crit_time(m, E) = ceil(m/E) * link_latency          # latency: parallelizes over E
                    + m * (expert_bytes / B)            # bandwidth: SHARED, independent of E

So copy engines hide the per-transfer LATENCY, never the link bandwidth. For large MoE
the expert weight is tens of MiB, so bytes/B >> latency and the transfer is
BANDWIDTH-BOUND: extra engines give near-zero speedup. This script sweeps
E in {1,2,4,8,16}, reports the knee E* (last E that still cuts stall > `KNEE_PCT`),
decomposes stall into latency vs bandwidth, and contrasts with the OPTIMISTIC
per-engine-full-bandwidth model to quantify how much that model over-credits engines.

Outputs:
  data/canonical/moe_routing_v1/w3_copy_engine_dse.json
  explorations/moe_orchestration/W3_COPY_ENGINE_DSE.md
"""
from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from edgeflow.model_config import ModelConfig  # noqa: E402
from edgeflow.moe_routing import expand_to_expert_demand  # noqa: E402
from edgeflow.residency import demands_from_events, simulate, PlatformCost  # noqa: E402

CANON = ROOT / "data" / "canonical" / "moe_routing_v1"
MODEL_CFG = ROOT / "configs" / "model" / "moe"
PLAT_CFG = ROOT / "configs" / "platform"

ENGINE_SWEEP = [1, 2, 4, 8, 16]
KNEE_PCT = 2.0  # E* = last engine count that still reduces stall by > this %


def stimulus_for(variant: str, manifest: dict) -> str:
    qs = [q for q in manifest["queries"] if q["model"] == variant]
    qs.sort(key=lambda x: (x["benchmark"], x["subject"], x["query_id"]))
    return qs[0]["canonical_path"]


def load_query(rel: str) -> list[dict]:
    with open(CANON / rel) as f:
        return [json.loads(l) for l in f]


def run_model(mc: ModelConfig, dm, bw: float, latency_s: float):
    cap = max(1, int(round(mc.num_experts * 0.5)))
    ebytes = mc.expert_weight_bytes()
    base = PlatformCost(profile_id="P-D", expert_weight_bytes=ebytes,
                        link_bandwidth_bytes_per_s=bw, link_latency_s=latency_s,
                        copy_engines=1, prefetch_bw_fraction=1.0)
    rows = []
    prev_shared = None
    e_star = 1
    for e in ENGINE_SWEEP:
        cost = replace(base, copy_engines=e)
        # on_demand critical path: copy engines matter most for compulsory misses.
        shared = simulate(dm, cap, 0, "on_demand", cost=cost,
                          per_layer_compute_time_s=0.0, shared_link_bandwidth=True)
        opt = simulate(dm, cap, 0, "on_demand", cost=cost,
                       per_layer_compute_time_s=0.0, shared_link_bandwidth=False)
        s_ms = (shared.total_stall_time_s or 0) * 1e3
        o_ms = (opt.total_stall_time_s or 0) * 1e3
        lat_comp = shared.extra["latency_batches"] * latency_s * 1e3
        bw_comp = shared.extra["critical_transfer_units"] * (mc.expert_weight_bytes() / bw) * 1e3
        drop = (prev_shared - s_ms) / prev_shared * 100 if prev_shared else None
        if drop is not None and drop > KNEE_PCT:
            e_star = e
        rows.append({
            "engines": e,
            "shared_stall_ms": round(s_ms, 4),
            "optimistic_stall_ms": round(o_ms, 4),
            "latency_component_ms": round(lat_comp, 4),
            "bandwidth_component_ms": round(bw_comp, 4),
            "stall_drop_vs_prev_pct": round(drop, 2) if drop is not None else None,
            "optimistic_overcredit_x": round(s_ms / o_ms, 2) if o_ms else None,
        })
        prev_shared = s_ms
    # regime: bandwidth-bound if the bandwidth component dominates at E=1
    e1 = rows[0]
    bw_frac = e1["bandwidth_component_ms"] / (e1["bandwidth_component_ms"] + e1["latency_component_ms"]) \
        if (e1["bandwidth_component_ms"] + e1["latency_component_ms"]) else 0.0
    regime = "bandwidth-bound" if bw_frac >= 0.9 else ("latency-bound" if bw_frac <= 0.1 else "mixed")
    return {
        "capacity": cap,
        "expert_weight_MiB": round(mc.expert_weight_bytes() / (1024 * 1024), 2),
        "num_experts": mc.num_experts,
        "e_star": e_star,
        "bandwidth_fraction_at_e1": round(bw_frac, 4),
        "regime": regime,
        "sweep": rows,
    }


def main() -> int:
    manifest = json.loads((CANON / "manifest.json").read_text())
    pd = json.loads((PLAT_CFG / "p_d_discrete.json").read_text())
    bw_map = pd["link_bandwidth_bytes_per_s_sweep"]
    latency_s = pd["link_latency_s"]
    # representative bandwidth = the mid point of the sweep
    bw_name = "pcie_mid_16GBs" if "pcie_mid_16GBs" in bw_map else sorted(bw_map)[len(bw_map) // 2]
    bw = bw_map[bw_name]

    report = {
        "dataset": manifest["dataset"], "source_revision": manifest["source_revision"],
        "model_note": "shared-link copy-engine model (D-048): latency parallelizes over "
                      "engines, bandwidth is shared; crit = ceil(m/E)*lat + m*bytes/B.",
        "bandwidth_name": bw_name, "bandwidth_GBs": round(bw / 1e9, 1),
        "link_latency_us": round(latency_s * 1e6, 3),
        "engine_sweep": ENGINE_SWEEP, "knee_pct": KNEE_PCT,
        "models": {},
    }
    for cfg_path in sorted(MODEL_CFG.glob("*.json")):
        mc = ModelConfig.load(cfg_path)
        rel = stimulus_for(mc.trace_variant, manifest)
        dm = demands_from_events(expand_to_expert_demand(load_query(rel)))
        report["models"][mc.trace_variant] = run_model(mc, dm, bw, latency_s)

    (CANON / "w3_copy_engine_dse.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    _md(report)
    _print(report)
    return 0


def _print(report):
    print(f"\ncopy-engine DSE @ {report['bandwidth_GBs']} GB/s, "
          f"latency {report['link_latency_us']} us, knee>{report['knee_pct']}%")
    for v, info in report["models"].items():
        print(f"\n=== {v} ===  expert={info['expert_weight_MiB']} MiB, "
              f"N={info['num_experts']}, regime={info['regime']} "
              f"(bw_frac@E1={info['bandwidth_fraction_at_e1']}), E*={info['e_star']}")
        for r in info["sweep"]:
            print(f"  E={r['engines']:>2}: stall={r['shared_stall_ms']:>9} ms "
                  f"(lat={r['latency_component_ms']:>8} + bw={r['bandwidth_component_ms']:>9}) "
                  f"drop={r['stall_drop_vs_prev_pct']}% | optimistic over-credit {r['optimistic_overcredit_x']}x")


def _md(report):
    out = ROOT / "explorations" / "moe_orchestration" / "W3_COPY_ENGINE_DSE.md"
    L = ["# W3 Copy-Engine Scheduling DSE\n",
         f"Dataset `{report['dataset']}` @ `{report['source_revision']}`.\n\n",
         "**Named slice component**: copy-engine scheduling for MoE expert-weight offload "
         "on a discrete CPU-GPU link.\n\n",
         "**Model (D-048)**: `E` copy engines share ONE link of bandwidth `B`. A step with "
         "`m` critical-path demand-miss loads costs "
         "`crit = ceil(m/E)*link_latency + m*(expert_bytes/B)`. Latency parallelizes over "
         "engines; bandwidth is shared, so the bandwidth term is independent of `E`. "
         "Copy engines hide latency, never link bandwidth. The OPTIMISTIC model "
         "(`ceil(m/E)*(latency+bytes/B)`, used by earlier W3 reports at fixed E=2) gives "
         "each engine full bandwidth and over-credits parallelism for bandwidth-bound "
         "transfers; the `over-credit` column quantifies the gap.\n\n",
         f"Point: **{report['bandwidth_GBs']} GB/s** ({report['bandwidth_name']}), link "
         f"latency **{report['link_latency_us']} us**, on-demand critical path, C=0.5N. "
         f"E* = last engine count that still cuts stall > {report['knee_pct']}%.\n"]
    for v, info in report["models"].items():
        L.append(f"\n## {v}\n")
        L.append(f"- expert weight **{info['expert_weight_MiB']} MiB**, experts "
                 f"{info['num_experts']}, C={info['capacity']}; regime **{info['regime']}** "
                 f"(bandwidth fraction at E=1 = {info['bandwidth_fraction_at_e1']:.1%}); "
                 f"**E\\* = {info['e_star']}**\n\n")
        L.append("| engines | stall (ms) | latency part (ms) | bandwidth part (ms) | drop vs prev | optimistic over-credit |\n")
        L.append("|---|---|---|---|---|---|\n")
        for r in info["sweep"]:
            drop = f"{r['stall_drop_vs_prev_pct']}%" if r["stall_drop_vs_prev_pct"] is not None else "-"
            oc = f"{r['optimistic_overcredit_x']}x" if r["optimistic_overcredit_x"] is not None else "-"
            L.append(f"| {r['engines']} | {r['shared_stall_ms']} | {r['latency_component_ms']} | "
                     f"{r['bandwidth_component_ms']} | {drop} | {oc} |\n")
    L.append("\n## Design takeaway\n\n")
    L.append("For large-MoE expert offload the transfer is **bandwidth-bound**: the per-expert "
             "weight (tens of MiB) makes `bytes/B` dominate the fixed link latency, so E* is "
             "small (1-2). Provisioning many copy engines does **not** cut expert-load stall; "
             "the correct lever is aggregate link bandwidth (or on-package integration / "
             "weight compression), not copy-engine count. This directly sizes the copy-engine "
             "scheduler in the discrete-platform vertical slice and corrects the optimistic "
             "fixed-E=2 assumption used in the earlier W3 device-timing baseline (the "
             "qualitative transfer-bound conclusion is unchanged and in fact strengthened).\n")
    out.write_text("".join(L))


if __name__ == "__main__":
    raise SystemExit(main())

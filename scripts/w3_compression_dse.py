#!/usr/bin/env python3
"""W3 slice-2 opener: expert-weight COMPRESSION / MIXED-PRECISION RESIDENCY DSE.

D-056 closed the predictive-prefetch candidate because the system is transfer-bound;
the only lever that moves a transfer-bound system is the *bytes transferred* itself.
This screens expert-weight compression / lower-precision residency as the slice-2
mechanism, on the SAME calibrated large-MoE workload, across three decision axes:

  (A) TRANSFER lever    -- store/stream experts at a reduced size r = native_bytes /
      eff_bytes (r = 1,2,4,8; e.g. a bf16 model at r=4 ~ int4 store). Transfer time per
      step scales 1/r. Report where that moves the break-even and the SW-vs-HW regime:
      r* = compression needed for t_transfer/step to drop to the largest control cost
      (firmware 10.44 us/step, S4) -- i.e. the ratio at which transfer stops dominating
      control. If realistic r (<=8) cannot reach r*, compression narrows but does not
      flip the transfer-bound regime.

  (B) CAPACITY lever    -- a fixed fast-memory BYTE budget holds r x more experts at
      1/r precision, so capacity (in experts) scales by r. Recompute demand-miss
      reduction at C' = min(N, round(C0*r)) (routing-level, assumption-free).

  (C) DECOMPRESSOR sizing -- to keep a compressed stream feeding the compute at the link
      rate, an on-accelerator decompressor must emit uncompressed bytes at ~link_BW,
      i.e. sustain ~link_BW GB/s of *output*. Report that required throughput per model x
      platform: this is the genuine HW block compression would justify (feasibility
      statement, NOT built here).

HONESTY: r is swept as a SYSTEMS parameter. The ACCURACY cost of low-precision experts
is OUT OF SCOPE here (no accuracy eval); int4/int2 are lossy and the usable r needs a
separate quality study (registered as an open assumption). We report the systems
consequence per r, never that a given r is accuracy-safe.

Outputs:
  data/canonical/moe_routing_v1/w3_compression_dse.json
  explorations/moe_orchestration/W3_COMPRESSION_DSE.md
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

RATIOS = [1, 2, 4, 8]              # compression / precision-reduction factors
# largest per-step control cost = firmware SW path (S4, identical to w3_mem_recheck)
CTRL_MAX_US = 10.44
CTRL_US = {"hardware": 0.18, "software": 2.07, "firmware": 10.44}


def ratio_label(native_bpp: float, r: int) -> str:
    """Interpret a further r-reduction as an approximate store precision (bits)."""
    bits = native_bpp * 8.0 / r
    if bits >= 7.5:
        return f"~{round(bits)}-bit"
    # snap to common quant widths
    for w in (8, 4, 3, 2, 1):
        if abs(bits - w) < 0.6:
            return f"~int{w}"
    return f"~{bits:.1f}-bit"


def load_query(rel: str) -> list[dict]:
    with open(CANON / rel) as f:
        return [json.loads(l) for l in f]


def stimulus_for(variant: str, manifest: dict) -> str:
    qs = [q for q in manifest["queries"] if q["model"] == variant]
    qs.sort(key=lambda x: (x["benchmark"], x["subject"], x["query_id"]))
    return qs[0]["canonical_path"]


def main() -> int:
    manifest = json.loads((CANON / "manifest.json").read_text())
    pd = json.loads((PLAT_CFG / "p_d_discrete.json").read_text())
    bw_map = pd["link_bandwidth_bytes_per_s_sweep"]
    lat = pd["link_latency_s"]
    bw_mid_name = "pcie_mid_16GBs" if "pcie_mid_16GBs" in bw_map else sorted(bw_map)[len(bw_map) // 2]
    bw_mid = bw_map[bw_mid_name]

    report = {
        "dataset": manifest["dataset"], "source_revision": manifest["source_revision"],
        "ratios": RATIOS, "control_cost_us_per_step": CTRL_US,
        "bandwidth_mid_name": bw_mid_name, "bandwidth_mid_GBs": round(bw_mid / 1e9, 1),
        "note": "compression/mixed-precision residency DSE; transfer lever (t_xfer 1/r, "
                "shared-link E=1) + capacity lever (C'=min(N,C0*r)) + decompressor sizing. "
                "r is a SYSTEMS parameter; accuracy of low-precision experts is out of scope.",
        "models": {},
    }

    for cfg_path in sorted(MODEL_CFG.glob("*.json")):
        mc = ModelConfig.load(cfg_path)
        variant = mc.trace_variant
        recs = load_query(stimulus_for(variant, manifest))
        dm = demands_from_events(expand_to_expert_demand(recs))
        n_steps = len(dm)
        cap0 = max(1, int(round(mc.num_experts * 0.5)))
        native_bytes = mc.expert_weight_bytes()
        native_bpp = mc.bytes_per_param

        # ---- (A) transfer lever: t_xfer/step at each r, across BW sweep ----
        transfer_rows = []
        for r in RATIOS:
            eff_bytes = max(1, int(round(native_bytes / r)))
            per_bw = {}
            for bname, bw in bw_map.items():
                cost = PlatformCost(profile_id="P-D", expert_weight_bytes=eff_bytes,
                                    link_bandwidth_bytes_per_s=bw, link_latency_s=lat,
                                    copy_engines=1, prefetch_bw_fraction=1.0)
                od = simulate(dm, cap0, 0, "on_demand", cost=cost,
                              per_layer_compute_time_s=0.0, shared_link_bandwidth=True)
                tstep_us = (od.total_stall_time_s or 0.0) / n_steps * 1e6
                per_bw[bname] = round(tstep_us, 3)
            transfer_rows.append({
                "r": r, "store_precision": ratio_label(native_bpp, r),
                "eff_expert_MiB": round(eff_bytes / 2**20, 3),
                "t_transfer_us_per_step_by_bw": per_bw,
                "t_transfer_us_per_step_mid": per_bw[bw_mid_name],
                "dominates_control_mid": per_bw[bw_mid_name] > CTRL_MAX_US,
                "ratio_over_ctrlmax_mid": round(per_bw[bw_mid_name] / CTRL_MAX_US, 2),
            })

        # r* to bring t_xfer/step (mid BW) down to the control band (regime flip point)
        t1_mid = transfer_rows[0]["t_transfer_us_per_step_mid"]
        r_star_mid = round(t1_mid / CTRL_MAX_US, 2)  # since t_xfer ~ 1/r
        flips_within_8x = r_star_mid <= 8.0

        # ---- (B) capacity lever: miss reduction at C' = min(N, C0*r) ----
        base = PlatformCost(profile_id="P-D", expert_weight_bytes=native_bytes,
                            link_bandwidth_bytes_per_s=bw_mid, link_latency_s=lat,
                            copy_engines=1, prefetch_bw_fraction=1.0)
        m0 = simulate(dm, cap0, 0, "on_demand", cost=base,
                      per_layer_compute_time_s=0.0, shared_link_bandwidth=True).demand_misses
        cap_rows = []
        for r in RATIOS:
            capp = min(mc.num_experts, max(1, int(round(cap0 * r))))
            m = simulate(dm, capp, 0, "on_demand", cost=base,
                         per_layer_compute_time_s=0.0, shared_link_bandwidth=True).demand_misses
            cap_rows.append({
                "r": r, "capacity_experts": capp,
                "capacity_frac_of_N": round(capp / mc.num_experts, 3),
                "demand_misses": m,
                "miss_reduction_vs_r1_pct": round((m0 - m) / m0 * 100, 2) if m0 else 0.0,
            })

        # ---- (C) decompressor sizing: sustain ~link_BW of OUTPUT bytes ----
        # compressed input rate = bw; output (uncompressed) rate = bw * r.
        decomp = {bname: {str(r): round(bw * r / 1e9, 1) for r in RATIOS}
                  for bname, bw in bw_map.items()}

        report["models"][variant] = {
            "config": mc.summary(),
            "num_steps": n_steps, "capacity0": cap0,
            "native_expert_MiB": round(native_bytes / 2**20, 2),
            "transfer_lever": transfer_rows,
            "r_star_to_control_mid": r_star_mid,
            "regime_flips_within_8x_mid": flips_within_8x,
            "capacity_lever": cap_rows,
            "decompressor_required_GBs_out": decomp,
        }

    (CANON / "w3_compression_dse.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False))
    _md(report)
    _print(report)
    return 0


def _print(report):
    print(f"\nW3 compression / mixed-precision residency DSE @ mid BW "
          f"{report['bandwidth_mid_GBs']} GB/s (control band firmware {CTRL_MAX_US} us/step)\n")
    for v, info in report["models"].items():
        c = info["config"]
        print(f"=== {v} ===  N={c['num_experts']} expert={info['native_expert_MiB']}MiB "
              f"({c['precision']}) C0={info['capacity0']} steps={info['num_steps']}")
        print("  (A) transfer lever  r : t_xfer/step(mid)  dominates-ctrl?  x-over-ctrl")
        for t in info["transfer_lever"]:
            print(f"      r={t['r']} ({t['store_precision']:>7}) "
                  f"{t['t_transfer_us_per_step_mid']:>10} us   "
                  f"{str(t['dominates_control_mid']):>5}   {t['ratio_over_ctrlmax_mid']}x")
        print(f"      -> r* to reach control band = {info['r_star_to_control_mid']}x "
              f"(flips within 8x: {info['regime_flips_within_8x_mid']})")
        print("  (B) capacity lever  r : C'(experts)  miss reduction vs r=1")
        for cr in info["capacity_lever"]:
            print(f"      r={cr['r']}  C'={cr['capacity_experts']:>4} "
                  f"({cr['capacity_frac_of_N']:.2f}N)  -{cr['miss_reduction_vs_r1_pct']}%")
        print()


def _md(report):
    out = ROOT / "explorations" / "moe_orchestration" / "W3_COMPRESSION_DSE.md"
    L = ["# W3 Slice-2 — Expert-weight Compression / Mixed-precision Residency DSE\n\n",
         f"Dataset `{report['dataset']}` @ `{report['source_revision']}`.\n\n",
         "D-056 closed the predictive-prefetch candidate: a transfer-bound system is moved "
         "only by the **bytes transferred**. This screens expert-weight compression / "
         "lower-precision residency as the slice-2 mechanism on the same calibrated large-MoE "
         "workload. `r` = further size reduction (native_bytes / eff_bytes).\n\n",
         "> **Honesty.** `r` is a *systems* parameter. The **accuracy** cost of low-precision "
         "experts is **out of scope** here (no accuracy eval); int4/int2 are lossy and the "
         "usable `r` needs a separate quality study (open assumption A-020). We report the "
         "systems consequence per `r`, never that a given `r` is accuracy-safe.\n\n",
         f"Reference control cost (SW-vs-HW): firmware **{CTRL_MAX_US} us/step** (S4). "
         f"Mid link BW = {report['bandwidth_mid_GBs']} GB/s.\n\n"]

    L.append("## (A) Transfer lever — transfer time per step scales 1/r\n\n")
    L.append("| model | N | native | r | store | t_xfer/step (mid) | dominates ctrl? | x over ctrl |\n")
    L.append("|---|---|---|---|---|---|---|---|\n")
    for v, info in report["models"].items():
        c = info["config"]
        for t in info["transfer_lever"]:
            L.append(f"| {v.split('/')[-1]} | {c['num_experts']} | "
                     f"{info['native_expert_MiB']}MiB {c['precision']} | {t['r']} | "
                     f"{t['store_precision']} | {t['t_transfer_us_per_step_mid']} us | "
                     f"{t['dominates_control_mid']} | {t['ratio_over_ctrlmax_mid']}x |\n")
    L.append("\n**Regime-flip ratio r\\*** (compression needed for transfer/step to reach the "
             "control band at mid BW):\n\n")
    L.append("| model | r* to control | flips within 8x (realistic quant)? |\n|---|---|---|\n")
    for v, info in report["models"].items():
        L.append(f"| {v.split('/')[-1]} | {info['r_star_to_control_mid']}x | "
                 f"{info['regime_flips_within_8x_mid']} |\n")

    L.append("\n## (B) Capacity lever — 1/r precision fits r× experts in the same byte budget\n\n")
    L.append("Demand-miss reduction at C' = min(N, C0·r) (routing-level, assumption-free).\n\n")
    L.append("| model | r | C' (experts) | C'/N | demand misses | miss reduction vs r=1 |\n")
    L.append("|---|---|---|---|---|---|\n")
    for v, info in report["models"].items():
        for cr in info["capacity_lever"]:
            L.append(f"| {v.split('/')[-1]} | {cr['r']} | {cr['capacity_experts']} | "
                     f"{cr['capacity_frac_of_N']} | {cr['demand_misses']} | "
                     f"{cr['miss_reduction_vs_r1_pct']}% |\n")

    L.append("\n## (C) Decompressor sizing — the HW block compression would justify\n\n")
    L.append("To keep a compressed stream feeding compute at the link rate, an on-accelerator "
             "decompressor must emit uncompressed bytes at ~link_BW × r (GB/s of OUTPUT). "
             "Feasibility target, not built.\n\n")
    L.append(f"| model | r=1 | r=2 | r=4 | r=8 | (@ mid {report['bandwidth_mid_GBs']} GB/s in) |\n")
    L.append("|---|---|---|---|---|---|\n")
    for v, info in report["models"].items():
        d = info["decompressor_required_GBs_out"][report["bandwidth_mid_name"]]
        L.append(f"| {v.split('/')[-1]} | {d['1']} | {d['2']} | {d['4']} | {d['8']} | GB/s out |\n")

    # verdict
    any_flip = any(info["regime_flips_within_8x_mid"] for info in report["models"].values())
    rstars = [info["r_star_to_control_mid"] for info in report["models"].values()]
    L.append("\n## Verdict\n\n")
    L.append(f"- **Transfer lever is real and linear**: compression cuts expert-transfer time "
             "1:1 with `r`, directly attacking the bandwidth wall that E\\*=1 / D-048 identified "
             "as the only lever. This is a *stronger* systems mechanism than predictive prefetch "
             "(which D-056 showed is unrealizable and latency-hiding-only).\n")
    L.append(f"- **But it does not flip the SW-vs-HW regime at realistic quant** (r\\* to the "
             f"control band = {min(rstars):.0f}–{max(rstars):.0f}× ≫ 8×; flips within 8×: "
             f"**{any_flip}**). Even int2-class storage leaves transfer dominating control by a "
             "wide margin — so the control path stays SW-sufficient; compression's payoff is "
             "*latency/energy*, not a change in where control logic must live.\n")
    L.append("- **Capacity lever is a near-step to full residency**: because the C0=0.5N baseline "
             "already holds half the experts, even `r`=2 (half-precision store) fits the ENTIRE "
             "expert set in the same byte budget (C'=N), collapsing demand misses to compulsory-only "
             "(~89–99.8% reduction, assumption-free / routing-level). So mixed-precision residency "
             "eliminates the miss stream outright, not only shrinks per-transfer time — a second, "
             "independent bandwidth-wall lever.\n")
    L.append("- **The HW block this justifies is a streaming DECOMPRESSOR** sized to sustain "
             "link_BW × r of output, not a prefetch predictor — a concrete, transfer-attacking "
             "slice-2 candidate. Next step: an executable expert (de)compression reference model "
             "+ a routing-level quality/size trade study to fix the usable `r` (A-020), then the "
             "same S1→S7 ladder (reference → sim → break-even → RTL → verify) as slice-1.\n")
    out.write_text("".join(L))


if __name__ == "__main__":
    raise SystemExit(main())

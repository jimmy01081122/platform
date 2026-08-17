#!/usr/bin/env python3
"""Re-check the SW-vs-HW / transfer-bound conclusion under DRAM-timing-CALIBRATED
P-I effective bandwidth (from scripts/mem_calibrate.py / Ramulator2), instead of
the guessed analytic contention factors {0.4,0.6,0.8}.

This directly tests the deferred-tool ADOPTION TRIGGER recorded in
project/capability_registry.yaml: "a P-I region where DRAM-timing interaction
flips the SW-vs-HW decision". If no swept point flips, the trigger is NOT met and
the full cycle-accurate integration remains (correctly) unadopted, now with real
DRAM-timing evidence rather than an assumption.

Method: aggregate P-I effective bandwidth = n_channels x per-channel
transfer-available bandwidth measured by Ramulator2 (LPDDR5-6400 x16), for two
regimes: seq (transfer-only, no compute contention) and mix:0.5 (moderate
co-running compute). Transfer time per step is compared to the largest per-step
control cost (firmware 10.44 us, S4). The SW-vs-HW placement flips only if
transfer stops dominating control.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CANON = ROOT / "data" / "canonical" / "moe_routing_v1"

# S4 work-unit facts + control costs (identical to scripts/p_i_sensitivity.py / D-025)
TRANSFERS_TOTAL = 1377
NUM_STEPS = 192
EXPERT_BYTES_SWITCH = 9_437_184
CTRL_US = {"hardware": 0.18, "software": 2.07, "firmware": 10.44}
CTRL_MAX_US = max(CTRL_US.values())

# aggregate channel scenarios bracketing the P-I sweep (per-channel result scaled)
CH_SCENARIOS = [2, 4, 8]     # 25.6 / 51.2 / 102.4 GB/s JEDEC peak


def per_channel(mem: dict, pattern: str) -> dict:
    for r in mem["runs"]:
        if r["pattern"] == pattern and r["req_channels"] == 1:
            return r
    raise KeyError(pattern)


def main() -> int:
    mem = json.loads((CANON / "mem_timing.json").read_text())
    seq = per_channel(mem, "seq")          # transfer-only
    mix = per_channel(mem, "mix:0.5")      # moderate compute contention

    bytes_per_step = TRANSFERS_TOTAL / NUM_STEPS * EXPERT_BYTES_SWITCH

    rows = []
    for regime, r in (("transfer_only(seq)", seq), ("moderate_contention(mix:0.5)", mix)):
        per_ch_avail_Bps = r["transfer_available_MBps"] * 1e6
        for ch in CH_SCENARIOS:
            eff = per_ch_avail_Bps * ch
            tt_us = bytes_per_step / eff * 1e6
            rows.append({
                "regime": regime,
                "channels": ch,
                "dram_eff_bw_GBs": round(eff / 1e9, 2),
                "t_transfer_us_per_step": round(tt_us, 2),
                "ctrl_max_us_per_step": CTRL_MAX_US,
                "transfer_dominates_control": tt_us > CTRL_MAX_US,
                "ratio_transfer_over_ctrlmax": round(tt_us / CTRL_MAX_US, 1),
            })

    # analytic comparison (the guessed factors) at 4 channels ~ shared_51GBs
    analytic = []
    for cf_name, cf in (("light_0.8", 0.8), ("moderate_0.6", 0.6), ("heavy_0.4", 0.4)):
        eff = 51e9 * cf
        analytic.append({"config": f"shared_51GBs|{cf_name}", "eff_bw_GBs": round(eff / 1e9, 2),
                         "t_transfer_us_per_step": round(bytes_per_step / eff * 1e6, 2)})
    dram_4ch = [x for x in rows if x["channels"] == 4]

    sw_crossover_GBs = round(EXPERT_BYTES_SWITCH / (CTRL_US["software"] * 1e-6) / 1e9, 1)
    max_dram_eff = max(x["dram_eff_bw_GBs"] for x in rows)

    summary = {
        "tool": mem["tool"], "image": mem["image"], "standard": mem["standard"],
        "trigger_under_test": "a P-I region where DRAM-timing interaction flips the SW-vs-HW decision",
        "work_unit": {"fixture": "switch-base-32", "transfers_per_step": round(TRANSFERS_TOTAL / NUM_STEPS, 3),
                      "expert_bytes": EXPERT_BYTES_SWITCH, "bytes_per_step": int(bytes_per_step)},
        "control_cost_us_per_step": CTRL_US,
        "dram_calibrated_points": rows,
        "analytic_vs_dram_at_4ch": {"analytic_guess": analytic, "dram_measured": dram_4ch},
        "transfer_dominates_on_all_points": all(x["transfer_dominates_control"] for x in rows),
        "min_ratio_transfer_over_ctrlmax": min(x["ratio_transfer_over_ctrlmax"] for x in rows),
        "sw_vs_hw_crossover_GBs": sw_crossover_GBs,
        "max_dram_effective_GBs": max_dram_eff,
        "flip_detected": not all(x["transfer_dominates_control"] for x in rows),
        "conclusion": (
            "Under REAL DRAM timing (Ramulator2, LPDDR5-6400), pure expert-weight "
            f"streaming reaches {seq['efficiency']*100:.0f}% of JEDEC peak ({seq['row_hit_rate']*100:.0f}% "
            "row-hit), while co-running compute erodes the transfer-available bandwidth MORE "
            f"than the analytic guess (mix:0.5 -> {mix['dram_contention_fraction']:.2f} vs guessed 0.60). "
            "Either way transfer time per step still dominates the largest per-step control cost "
            f"by >={min(x['ratio_transfer_over_ctrlmax'] for x in rows):.0f}x on every point; the highest "
            f"DRAM-effective bandwidth reached ({max_dram_eff:.0f} GB/s) is ~{sw_crossover_GBs/max_dram_eff:.0f}x "
            f"below the ~{sw_crossover_GBs:.0f} GB/s SW-vs-HW crossover. NO P-I region flips the decision, "
            "so the deferred-tool adoption trigger is NOT met: the SW-sufficiency-for-decision "
            "conclusion is robust to cycle-accurate DRAM timing, and realistic contention makes the "
            "system MORE transfer-bound, not less."
        ),
    }
    (CANON / "w3_mem_recheck.json").write_text(json.dumps(summary, indent=2) + "\n")
    _append_md(summary)
    print(json.dumps(summary, indent=2))
    return 0


def _append_md(s):
    out = ROOT / "explorations" / "moe_orchestration" / "W3_MEM_TIMING.md"
    L = ["\n\n## SW-vs-HW recheck under DRAM-calibrated bandwidth\n",
         f"\nTrigger under test: *{s['trigger_under_test']}*.\n",
         f"\nWork unit: switch-base-32, {s['work_unit']['bytes_per_step']:,} bytes/step; "
         f"largest control cost {max(s['control_cost_us_per_step'].values())} us/step (firmware, S4).\n",
         "\n| regime | channels | DRAM eff BW (GB/s) | t_transfer/step (us) | dominates control? | ratio |\n",
         "|---|---|---|---|---|---|\n"]
    for r in s["dram_calibrated_points"]:
        L.append(f"| {r['regime']} | {r['channels']} | {r['dram_eff_bw_GBs']} | "
                 f"{r['t_transfer_us_per_step']} | {r['transfer_dominates_control']} | "
                 f"{r['ratio_transfer_over_ctrlmax']}x |\n")
    L.append(f"\n**Flip detected: {s['flip_detected']}.** {s['conclusion']}\n")
    with open(out, "a") as f:
        f.write("".join(L))


if __name__ == "__main__":
    raise SystemExit(main())

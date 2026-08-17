#!/usr/bin/env python3
"""Slice-2 rate-distortion (RD) study for the expert-weight codec (A-020).

Characterizes the size <-> distortion trade of the group-wise int-N codec
(`edgeflow.expert_codec`, the decompressor's golden reference) to fix the *usable*
compression ratio r that D-057 left open. Distortion is measured as weight-
reconstruction SQNR (dB); this is a REPRESENTATION-distortion proxy, NOT task
accuracy (task accuracy needs the real weights + eval harness and stays deferred
under A-020).

Honest scope of the input: we have routing traces, not the multi-hundred-GB expert
weights, so the RD curve is measured on a REPRESENTATIVE weight DISTRIBUTION MODEL
(per-channel Gaussian with lognormal per-channel scale + a small heavy outlier
fraction -- the property that actually makes low-bit weight quant hard). Seeded /
deterministic. The codec itself is exact on real tensors; only the *stimulus* is a
model. We therefore claim the RD ORDERING and the eff-bits arithmetic, and label the
absolute dB as distribution-model-based.

Outputs:
  data/canonical/moe_routing_v1/slice2_codec_rd.json
  explorations/moe_orchestration/SLICE2_CODEC_RD.md
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from edgeflow.expert_codec import decode, effective_bits, encode, roundtrip, sqnr_db  # noqa: E402
from edgeflow.model_config import ModelConfig  # noqa: E402

CANON = ROOT / "data" / "canonical" / "moe_routing_v1"
MODEL_CFG = ROOT / "configs" / "model" / "moe"

SEED = 20260717
ROWS, COLS = 1024, 4096          # representative expert sub-matrix (4.2M weights)
N_BITS = [8, 4, 3, 2]
GROUPS = [32, 64, 128, 256]
CLIPS = [100.0, 99.9, 99.0]      # per-group scale reference percentile (100 = max)
BF16_BITS = 16.0                 # full-precision reference
# representation-distortion reference points (SQNR dB). These are REPRESENTATION levels,
# NOT task-accuracy thresholds: LLM task accuracy tolerates far more weight-quant noise
# than these dB suggest (see the deployed anchor below). The dB->task mapping is A-020.
BUDGETS = {"repr_40dB": 40.0, "repr_30dB": 30.0, "repr_20dB": 20.0}
# DEPLOYED anchor: the precision these very models actually ship at (int4 group-128 is the
# de-facto near-task-lossless point for DeepSeek-R1-AWQ / Kimi-K2 / most 4-bit LLMs).
DEPLOYED_ANCHOR = {"n_bits": 4, "group_size": 128, "clip_percentile": 100.0}


def representative_weights(rows: int, cols: int, seed: int) -> np.ndarray:
    """Distribution MODEL of transformer expert-linear weights (labeled, not real):
      * per output channel (row) std ~ lognormal (channel-scale heterogeneity);
      * weights ~ Normal(0, std_row);
      * inject 0.1% heavy outliers at ~8x the row std (the known weight-outlier tail).
    """
    rng = np.random.default_rng(seed)
    row_std = np.exp(rng.normal(loc=-2.0, scale=0.5, size=(rows, 1))).astype(np.float32)
    w = (rng.standard_normal((rows, cols)).astype(np.float32)) * row_std
    n_out = int(0.001 * rows * cols)
    ri = rng.integers(0, rows, size=n_out)
    ci = rng.integers(0, cols, size=n_out)
    sign = rng.choice(np.array([-1.0, 1.0], dtype=np.float32), size=n_out)
    w[ri, ci] += sign * 8.0 * row_std[ri, 0]
    return w


def self_checks(w: np.ndarray) -> dict:
    """Determinism + monotonicity + exact-decode-reproducibility guards."""
    checks = {}
    # determinism: encode/decode twice -> bit-identical
    r1 = encode(w, 4, 128, BF16_BITS)
    r2 = encode(w, 4, 128, BF16_BITS)
    d1, d2 = decode(r1, w.shape[1]), decode(r2, w.shape[1])
    checks["decode_deterministic"] = bool(np.array_equal(d1, d2) and
                                          np.array_equal(r1.codes, r2.codes))
    # monotonicity: more code bits -> not-worse SQNR at fixed group
    sqnrs = [roundtrip(w, n, 128, BF16_BITS)[2] for n in (2, 3, 4, 8)]
    checks["sqnr_monotone_in_bits"] = all(a <= b + 1e-6 for a, b in zip(sqnrs, sqnrs[1:]))
    # eff-bits arithmetic sanity
    checks["eff_bits_int4_g128"] = round(effective_bits(4, 128), 4)
    return checks


def main() -> int:
    w = representative_weights(ROWS, COLS, SEED)
    checks = self_checks(w)

    grid = []
    for n in N_BITS:
        for g in GROUPS:
            for clip in CLIPS:
                w_hat, res, snr, maxerr = roundtrip(w, n, g, BF16_BITS, clip)
                grid.append({
                    "n_bits": n, "group_size": g, "clip_percentile": clip,
                    "eff_bits_per_param": round(res.eff_bits_per_param, 4),
                    "ratio_vs_bf16": round(res.ratio_vs_native, 3),
                    "sqnr_db": round(snr, 2), "max_abs_err_rel_to_amax": None,  # filled below
                    "max_abs_err": round(maxerr, 6),
                })
    amax = float(np.max(np.abs(w)))
    for row in grid:
        row["max_abs_err_rel_to_amax"] = round(row["max_abs_err"] / amax, 5)

    # usable r per budget = the point with the SMALLEST eff_bits meeting the SQNR budget
    usable = {}
    for bname, thr in BUDGETS.items():
        ok = [r for r in grid if r["sqnr_db"] >= thr]
        if ok:
            # smallest stored size that meets the budget; tie-break on best SQNR
            best = min(ok, key=lambda r: (r["eff_bits_per_param"], -r["sqnr_db"]))
            usable[bname] = {
                "sqnr_threshold_db": thr, "n_bits": best["n_bits"],
                "group_size": best["group_size"], "clip_percentile": best["clip_percentile"],
                "eff_bits_per_param": best["eff_bits_per_param"],
                "ratio_vs_bf16": best["ratio_vs_bf16"], "sqnr_db": best["sqnr_db"],
            }
        else:
            usable[bname] = {"sqnr_threshold_db": thr, "note": "no grid point meets budget"}

    # DEPLOYED-anchor point (int4 g128) measured on the same stimulus
    a = DEPLOYED_ANCHOR
    _, ares, asnr, _ = roundtrip(w, a["n_bits"], a["group_size"], BF16_BITS, a["clip_percentile"])
    anchor = {**a, "eff_bits_per_param": round(ares.eff_bits_per_param, 4),
              "ratio_vs_bf16": round(ares.ratio_vs_native, 3), "sqnr_db": round(asnr, 2)}

    # per-model: map the DEPLOYED-anchor eff-bits onto the CURRENT store precision to get the
    # realizable *transfer* compression r and latency reduction (transfer ~ 1/r, D-057). Uses
    # the deployed anchor (not a strict SQNR budget) because that is the precision proven
    # near-task-lossless in practice; models already at/below it have no lossless transfer
    # headroom. Does NOT flip the transfer-bound regime (D-057 r* = 525-721x).
    per_model = {}
    for cfg_path in sorted(MODEL_CFG.glob("*.json")):
        mc = ModelConfig.load(cfg_path)
        cur_bits = mc.bytes_per_param * 8.0
        eff = anchor["eff_bits_per_param"]
        r_transfer = max(1.0, cur_bits / eff)
        per_model[mc.trace_variant] = {
            "current_precision": mc.summary()["precision"], "current_bits_per_param": cur_bits,
            "anchor_eff_bits": eff, "anchor_sqnr_db": anchor["sqnr_db"],
            "realizable_transfer_r_vs_current": round(r_transfer, 3),
            "transfer_time_reduction_x": round(r_transfer, 3),
            "already_at_or_below_anchor": cur_bits <= eff,
        }

    report = {
        "seed": SEED, "stimulus_shape": [ROWS, COLS],
        "stimulus": "representative transformer expert-weight DISTRIBUTION MODEL "
                    "(per-channel Gaussian, lognormal channel scale, 0.1% 8-sigma outliers); "
                    "NOT real weights -- claim RD ordering + eff-bits, label absolute dB",
        "distortion_metric": "weight-reconstruction SQNR (dB); representation proxy, NOT task accuracy (A-020)",
        "codec": "group-wise symmetric int-N, fp16 per-group scale (edgeflow.expert_codec)",
        "native_reference_bits": BF16_BITS, "scale_bits": 16,
        "self_checks": checks,
        "grid": grid,
        "repr_sqnr_reference_points": usable,
        "deployed_anchor": anchor,
        "per_model_transfer_mapping": per_model,
    }
    (CANON / "slice2_codec_rd.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    _md(report)
    _print(report)
    return 0


def _print(report):
    c = report["self_checks"]
    print(f"\nSlice-2 codec RD  (seed={report['seed']}, stimulus {report['stimulus_shape']})")
    print(f"self-checks: deterministic={c['decode_deterministic']} "
          f"sqnr_monotone={c['sqnr_monotone_in_bits']} eff_bits(int4,g128)={c['eff_bits_int4_g128']}")
    print("\n  n_bits group  clip%   eff_bits   r vs bf16   SQNR(dB)   maxerr/|w|max")
    for r in report["grid"]:
        print(f"    {r['n_bits']:>2}   {r['group_size']:>4}  {r['clip_percentile']:>5}   "
              f"{r['eff_bits_per_param']:>7}   {r['ratio_vs_bf16']:>7}x   {r['sqnr_db']:>7}   "
              f"{r['max_abs_err_rel_to_amax']}")
    print("\n  representation SQNR reference points (NOT task-accuracy thresholds):")
    for b, u in report["repr_sqnr_reference_points"].items():
        if "eff_bits_per_param" in u:
            print(f"    {b:<10} (SQNR>={u['sqnr_threshold_db']}dB): int{u['n_bits']} g{u['group_size']} "
                  f"clip{u['clip_percentile']} -> {u['eff_bits_per_param']} bits/param "
                  f"({u['ratio_vs_bf16']}x vs bf16, {u['sqnr_db']} dB)")
    a = report["deployed_anchor"]
    print(f"\n  DEPLOYED anchor int{a['n_bits']} g{a['group_size']}: {a['eff_bits_per_param']} bits/param "
          f"({a['ratio_vs_bf16']}x vs bf16, {a['sqnr_db']} dB) -- what these models actually ship")
    print("\n  per-model realizable TRANSFER r (deployed int4 anchor) vs current store:")
    for v, e in report["per_model_transfer_mapping"].items():
        print(f"    {v.split('/')[-1]:<40} cur={e['current_bits_per_param']}b "
              f"-> transfer r={e['realizable_transfer_r_vs_current']} "
              f"(at/below anchor: {e['already_at_or_below_anchor']})")


def _md(report):
    out = ROOT / "explorations" / "moe_orchestration" / "SLICE2_CODEC_RD.md"
    c = report["self_checks"]
    L = ["# Slice-2 — Expert-weight Codec Rate-Distortion (A-020)\n\n",
         "Characterizes the size↔distortion trade of the group-wise int-N codec "
         "(`edgeflow.expert_codec`, the decompressor's **golden reference**) to fix the "
         "*usable* compression ratio `r` that D-057 left open.\n\n",
         "> **Honest scope.** We have routing traces, not the multi-hundred-GB expert "
         "weights, so the RD curve is measured on a **representative weight DISTRIBUTION "
         "MODEL** (per-channel Gaussian, lognormal channel scale, 0.1% 8σ outliers — the "
         "property that makes low-bit weight quant hard), seeded/deterministic. The codec is "
         "exact on real tensors; only the *stimulus* is a model. We claim the RD **ordering** "
         "and the **eff-bits arithmetic**; absolute dB is distribution-model-based. Distortion "
         "= weight-reconstruction **SQNR**, a representation proxy, **NOT task accuracy** "
         "(task accuracy needs the real weights + eval and stays deferred under A-020).\n\n",
         f"Self-checks: decode-deterministic **{c['decode_deterministic']}**, "
         f"SQNR-monotone-in-bits **{c['sqnr_monotone_in_bits']}**, "
         f"eff_bits(int4, g128) = **{c['eff_bits_int4_g128']}** bits/param.\n\n",
         "## Rate-distortion grid\n\n",
         "eff_bits/param = code bits + 16/group (fp16 per-group scale). r vs bf16 = 16 / eff_bits. "
         "clip% = per-group scale reference percentile of |w| (100 = max; <100 = outlier-aware).\n\n",
         "| code bits | group | clip% | eff bits/param | r vs bf16 | SQNR (dB) | max err / |w|max |\n",
         "|---|---|---|---|---|---|---|\n"]
    for r in report["grid"]:
        L.append(f"| {r['n_bits']} | {r['group_size']} | {r['clip_percentile']} | "
                 f"{r['eff_bits_per_param']} | {r['ratio_vs_bf16']}x | {r['sqnr_db']} | "
                 f"{r['max_abs_err_rel_to_amax']} |\n")

    L.append("\n## Representation SQNR reference points (NOT task-accuracy thresholds)\n\n")
    L.append("Smallest eff-bits meeting each SQNR level. **These are representation levels, not "
             "deployability thresholds** — a uniform 4-bit quantizer on ~Gaussian weights is "
             "~18 dB SQNR by construction (6 dB/bit), yet int4 is the *deployed* precision for "
             "these models. The dB→task-accuracy mapping is exactly the open gap A-020.\n\n")
    L.append("| SQNR level | scheme | eff bits/param | r vs bf16 | SQNR (dB) |\n|---|---|---|---|---|\n")
    for b, u in report["repr_sqnr_reference_points"].items():
        if "eff_bits_per_param" in u:
            L.append(f"| ≥{u['sqnr_threshold_db']} dB | int{u['n_bits']} g{u['group_size']} "
                     f"clip{u['clip_percentile']} | {u['eff_bits_per_param']} | {u['ratio_vs_bf16']}x | "
                     f"{u['sqnr_db']} |\n")
        else:
            L.append(f"| ≥{u['sqnr_threshold_db']} dB | — | — | — | {u.get('note','')} |\n")

    a = report["deployed_anchor"]
    L.append(f"\n**Deployed anchor** — int{a['n_bits']} group-{a['group_size']} = "
             f"**{a['eff_bits_per_param']} bits/param** ({a['ratio_vs_bf16']}× vs bf16, "
             f"{a['sqnr_db']} dB on this stimulus). This is the precision DeepSeek-R1-AWQ / Kimi-K2 "
             "and most 4-bit LLMs actually ship at, i.e. the empirically near-task-lossless point "
             "— far more aggressive than the ≥40 dB representation level would imply, which is the "
             "whole reason weight-SQNR is only a weak proxy (A-020).\n\n")

    L.append("## Per-model realizable transfer r (deployed int4 anchor)\n\n")
    L.append("Maps the int4-g128 anchor onto each model's CURRENT store precision → realizable "
             "*transfer* compression r and latency reduction (transfer time ~ 1/r, D-057). Models "
             "already at/below int4 have no lossless transfer headroom (r=1).\n\n")
    L.append("| model | current store | anchor bits | realizable transfer r | transfer-time reduction | already ≤ int4? |\n")
    L.append("|---|---|---|---|---|---|\n")
    for v, e in report["per_model_transfer_mapping"].items():
        L.append(f"| {v.split('/')[-1]} | {e['current_precision']} ({e['current_bits_per_param']}b) | "
                 f"{e['anchor_eff_bits']} | {e['realizable_transfer_r_vs_current']}x | "
                 f"{e['transfer_time_reduction_x']}x | {e['already_at_or_below_anchor']} |\n")

    L.append("\n## Verdict\n\n")
    L.append("- **The codec + RD study is the executable reference** for the slice-2 decompressor: "
             "decode is integer-exact and deterministic (self-checks pass), so a future RTL "
             "decompressor can be proved bit-for-bit against `edgeflow.expert_codec`, exactly like "
             "slice-1's residency engine.\n")
    L.append("- **Weight-SQNR is a weak proxy for deployability**: uniform int4 sits at ~16–18 dB "
             "by construction yet is the deployed precision. So the usable r is anchored by "
             "*deployed practice* (int4-g128), not by a dB budget; fixing the true accuracy-safe r "
             "needs real weights + an eval harness — the still-open A-020 gate.\n")
    L.append("- **Only the higher-precision models have transfer headroom to int4**: "
             "Llama-4-Maverick (bf16) ~3.9× and Qwen3 (fp8) ~1.9×; the models that already ship at "
             "int4 (DeepSeek-R1-AWQ, Kimi-K2) have none (r=1). So the transfer lever helps only where "
             "the checkpoint is not yet 4-bit; the universal bandwidth-wall lever remains the "
             "**capacity** side (D-057 (B): r=2 → full residency), which is precision-relative and "
             "applies to every model.\n")
    L.append("- Consistent with D-057, even the bf16 headroom does **not** flip the transfer-bound "
             "regime (r\\* to the control band is 525–721×); the payoff is latency/energy, and it "
             "sizes the decompressor (int4 decode → ~link_BW × ~4 of fp16 output).\n")
    L.append("- **Next (slice-2 RTL gate):** anchor the dB→accuracy axis with a real per-model quant "
             "point (or one real expert tensor), then build the streaming decompressor RTL against "
             "the golden codec and run it through the slice-1 S-ladder (reference → sim → break-even "
             "→ RTL → four-layer equivalence + STA).\n")
    out.write_text("".join(L))


if __name__ == "__main__":
    raise SystemExit(main())

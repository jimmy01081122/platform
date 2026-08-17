# Large-MoE Routing Calibration and Fidelity Ladders

This document defines how the large-model MoE routing dataset
(`core12345/MoE_expert_selection_trace`) is turned into a calibrated,
provenance-tracked workload for the CPU-GPU edge accelerator exploration, and
the fidelity ladders that keep every claim traceable to what was actually
measured.

## 1. Data access and integrity

- The dataset is **gated (auto)** and **large (99,540 query files)**. Full
  download is forbidden by project rule.
- Only a **stratified subset** is fetched by `scripts/hf_sample_download.py`
  under a byte cap, using `configs/sampling/roundN.json`.
- **Token handling (never persisted, never printed):** resolved at runtime from
  `$HF_TOKEN` / `$HUGGINGFACE_TOKEN` / `~/.cache/huggingface/token`, sent only as
  a Bearer header to `huggingface.co`.
- **Provenance:** every fetched file's `sha256` + `source_revision` are recorded
  in `data/registry/hf_downloads.json`; the dataset structure snapshot is in
  `data/registry/dataset_structure.json`; canonical outputs are hashed in
  `data/canonical/moe_routing_v1/manifest.json`.
- Raw files and large per-query canonical JSONL are **git-ignored** and
  regenerated deterministically from the registry.

## 2. Source trace format

One JSON file = one query/request. Top level is a list of generation *steps*:
`step[0]` = prefill (all prompt tokens), `step[i>=1]` = decode (1 token). Each
step is `{layer_id: [per-token [top-k expert ids]]}`. Dense (non-MoE) layers are
`null` and carry no routing. Verified variant dimensions:

| variant | layers | MoE layers | top_k | experts |
|---|---|---|---|---|
| Qwen/Qwen3-235B-A22B-FP8 | 94 | 94 | 8 | 128 |
| cognitivecomputations/DeepSeek-R1-AWQ | 61 | 58 | 8 | 256 |
| meta-llama/Llama-4-Maverick-17B-128E-Instruct | 48 | 24 | 1 | 128 |
| moonshotai/Kimi-K2-Thinking | held-out for generalization | | | |

## 3. Canonical + expansion pipeline

```
discover  scripts/hf_sample_download.py (dry-run)  -> stratum resolution
sample    scripts/hf_sample_download.py            -> data/raw/**  + hf_downloads.json
canonical scripts/moe_canonicalize.py              -> moe-routing-v1 JSONL + manifest.json
expand    edgeflow.moe_routing.expand_to_expert_demand -> expert_demand events
baseline  scripts/moe_routing_report.py            -> routing_stats.json + ROUTING_BASELINE.md
```

`moe-routing-v1` (`schemas/moe_routing.schema.json`) is faithful measured routing
(per step/layer/token top-k). **Workload expansion** turns it into the existing
`expert_demand` event stream consumed by the S1-S5 residency simulator, so the
large-MoE workload reuses the already-verified Python==C==RV64==RTL algorithm.

## 4. Workload fidelity ladder (W)

- **W0** regression fixtures (committed small Switch-Transformer slices).
- **W1** local prototype profiling traces (`/home/a/prototype/...`, read-only).
- **W2** measured large-MoE **routing behavior** from this dataset (implemented):
  which experts each token selects, per layer, prefill vs decode. Assumption-free
  residency metrics (miss rate, transfers, reuse) live here.
- **W3** **expanded system workload**: routing x model dims x precision x platform
  profile -> per-layer expert-weight bytes and a device-timed event stream.
- **W4** closed-loop / multi-request scheduling scenarios.

## 5. Hardware fidelity ladder (H)

Formalized in `configs/fidelity/fidelity_ladders.yaml` (source of truth). Per the
project charter, the H ladder describes **platform/device model** credibility:

- **H0** analytical, uncalibrated (first-order transfer/compute).
- **H1** vendor-spec constrained (swept bandwidth ranges; current W3 device timing).
- **H2** RTX 3050 component-calibrated (requires local profiling; pending).
- **H3** A6000/A100 component-calibrated (uncalibrated profiles created with
  null/sweep unknowns: `configs/platform/p_d_a6000_uncalibrated.json`, `..._a100_...`).
- **H4** MoE layer / kernel replay calibrated.
- **H5** hold-out workload and platform validated (Kimi-K2 model hold-out done at
  W2/W3; platform hold-out pending real hardware).

Note: the S5/S6 RTL + STA work characterizes the *accelerator engine itself*
(cycle counts, gate-level Fmax/power), which is a separate axis from this device
(GPU/link) service-model ladder. Every simulation result is tagged
`fidelity: {workload: Wx, hardware: Hy}`.

## 6. Three-source hybrid calibration

A system-event estimate combines three independently-sourced inputs, each kept
separable and provenance-tagged:

1. **Routing behavior** (W2, measured): expert working sets and reuse per layer.
2. **Model dimensions** (registered config): expert weight bytes = f(hidden,
   intermediate, precision) - drives transfer volume.
3. **Calibrated device service model** (H1, measured curves): bandwidth/latency
   per platform (RTX 3050 discrete P-D, integrated P-I, A6000/A100 reference).

No fabricated point values: any physical cost is either measured, or a swept
registered assumption (see `ASSUMPTION_REGISTER.md`).

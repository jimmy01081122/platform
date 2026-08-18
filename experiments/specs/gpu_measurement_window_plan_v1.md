# GPU measurement window plan v1

Derived from `experiments/specs/gpu_measurement_contract_v1.yaml`.

**CLAIM BOUNDARY:** every number here is built from **estimated** `time_estimate`
values, not measured durations (TRACK_GPU_PREP §7). Use it to size and order the
window; re-plan once the first real timings land — especially target_2 (long
prefill + offload) and target_5 (arrival-bound), the two least-constrained
estimates.

## Per-priority time estimates

| Prio | Target | Est. (min) | Readiness | Dominant cost |
|---|---|---|---|---|
| 1 | A2 in-serving dispatch | 25 | probe written, CPU-smoke-passed | serving windows × concurrency |
| 2 | A6 long-context / KV offload | 60 (HIGH uncertainty) | probe written, CPU-smoke-passed | 1M-token prefill + offload |
| 3 | sealed held-out split | 0 (sealed in PREP-1) | **done** | none (measurement folds into P4) |
| 4 | component service-model gaps | 15 | mostly ready; GAP-5 resolved by code | microbenchmarks, n=5 |
| 5 | SERV-P0-25 tail-CI | 160 | ready (existing runner) | Poisson arrival-bound (~2.65 h) |

Model load (~90 s) is amortized once per session and already folded into each
estimate.

## Key scheduling facts

1. **Target 5 is arrival-bound, not compute-bound.** 10,000 requests at Poisson
   1.0472 rps ≈ 9,550 s ≈ **2.65 h wall-clock regardless of concurrency**. It
   cannot be shortened without changing the arrival process (which changes what
   is measured). It dominates the budget and should run in its own long window,
   or overlap nothing that needs exclusive GPU (it is light per-instant load).

2. **Sensitive microbenchmarks need exclusive GPU** (root spec §9.3). Targets 1,
   2, 4 must not run concurrently with each other or with filler. Target 5's
   serving load is *not* exclusive-safe to co-run with 1/2/4 either — it would
   perturb their timings.

3. **Priority order ≠ readiness order.** Targets 4 and 5 are fully specified by
   A1 and runnable today; targets 1 and 2 needed the probes this track wrote.
   Per stage_ledger `TRACK_GPU.preconditions.exception`, if an endpoint appears
   before PREP finished, 4 and 5 may run first.

## Feasible combinations by window size

Sequential, exclusive-GPU (sum of estimates):

- **Short window (~30 min):** target_4 (15) — the component/PCIe gaps + the
  V2-GAP-C sort/permute probe. Highest calibration value per minute. Sealed
  held-out cells (target_3 assignment) are measured here + in the PCIe sweep.
- **~1 hour window:** target_1 (25) + target_4 (15) = 40 min, leaving margin.
  Covers both the A2 dispatch gap and the component gaps.
- **~1.5–2 hour window:** target_2 (60, uncertain) + target_4 (15) = 75 min.
  Long-context is the biggest information gain; pair it with the cheap
  component gaps. Validate target_2's estimate after the first 2–3 seq_lens and
  re-plan if the 1M point blows the budget.
- **Half-day window (~4 h):** target_5 (160, its own arrival-bound run) in a
  first block, then targets 1+2+4 (100 min) exclusive afterwards. Do NOT overlap
  target_5 with 1/2/4.

## What fits in a single 2-hour exclusive window

Targets 1 + 2 + 4 ≈ **100 min** of exclusive GPU (25+60+15), inside a 2 h window
with ~20 min margin for preflight, guard canary, and per-attempt raw saving —
**if** target_2's high-uncertainty estimate holds. Target 5 does **not** fit
alongside them; schedule its 2.65 h arrival-bound run separately.

## Held-out isolation reminder

The sealed held-out cells (`calibration/sealed/holdout_split_v1_manifest.json`)
must be measured but **scored only once, by STAGE_A4**. Do not evaluate the model
against held-out cells during TRACK_GPU. V2-GAP-B/C measurements are FIT-side and
must not be mixed with the sealed held-out (stage_ledger A1 note).

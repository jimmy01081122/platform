# GAP-5 resolution — moe_replay ↔ gpu_service launch-granularity mapping

```text
TRACK       : GPU_PREP (pure CPU; resolved by reading code, no GPU time used)
GAP         : GAP-5-MOE-REPLAY-TOKEN-NORMALIZATION-MISMATCH
SOURCE SPEC : calibration/fits/v2/measurement_gaps.json
STATUS      : RESOLVED_BY_CODE (mapping documented) + one residual = V2-GAP-C (needs GPU)
```

## What GAP-5 asked

> needs either explicit documentation of the cpu_calls↔launch-granularity mapping
> from the measurement harness, or a dedicated moe_replay-style probe that varies
> tokens_per_launch directly.

The first path — **document the mapping** — is pure code reading. It succeeds.
This note is that documentation, with the derivation and exact code locations.

## The two conventions, located in code

**moe_replay side** — `window_replay`, `benchmark.py:520-590`:

- `replay_routes = [route.repeat(concurrency) for route in base_replay_routes]`,
  one entry per decode **step** in the window (`benchmark.py:520,528`).
- `window_replay` loops `for route in routes` and does **one** grouped_gemm +
  gather/scatter per step (`benchmark.py:534-542`).
- `cpu_calls = len(replay_routes)` = number of decode steps = number of
  grouped_gemm launches in the window (`benchmark.py:578`).
- `tokens = measured_tokens = sum(step["num_tokens"]) * concurrency`
  (`benchmark.py:547-549`). Each decode step has `num_tokens = 1`
  (verified from `workloads/windows.json`: decode step `selected_experts`
  shape `[1, 8]`, `num_tokens = 1`).
- Consumer derives `tokens_per_launch = tokens / cpu_calls`
  (`calibrated_backend.py:361`) = **1 × concurrency**.

**gpu_service side** — `selected_expert / grouped_gemm / gather_scatter` probes,
`benchmark.py:436-518`:

- `base_route = flatten(phase_step["selected_experts"])`; for a decode step this
  is `[1, 8]` → **numel = 8** (`benchmark.py:436-438`).
- `route = base_route.repeat(concurrency)`; `expert_tokens = route.numel()`
  (`benchmark.py:440-441`) = **8 × concurrency** (8 at c=1, 32 at c=4 — matches
  the values recorded in GAP-5).
- `activations = torch.randn(expert_tokens, hidden)` — the grouped_gemm operates
  on `expert_tokens` rows.

## The mapping (the 8× is fully explained)

Both conventions describe the **same** grouped_gemm launch but count different
denominators:

| quantity | moe_replay | gpu_service |
|---|---|---|
| what "tokens per launch" counts | decode token **positions** (1/step) | flattened **routing slots** fed to grouped_gemm (8/step) |
| value at concurrency=1 | 1 | 8 |
| value at concurrency=4 | 4 | 32 |

So:

```
expert_tokens(gpu_service) = tokens_per_launch(moe_replay) × ROUTING_WIDTH
ROUTING_WIDTH = numel(flatten(decode-step selected_experts)) = 8   (a trace fact)
```

The 8× is **not a bug**; it is the per-decode-token routing fan-out. The
calibration consumer evaluates the gpu_service `operation_ms` model at
`tokens_per_launch = 1` while that model was fit against `expert_tokens = 8`.
Multiplying moe_replay's `tokens_per_launch` by the trace's ROUTING_WIDTH (8),
**or** having the eval-point generator emit `expert_tokens` directly (the GAP-4
fix), reconciles the two campaigns. This is documentation/schema, not GPU work.

## The residual ~1.87× is a DIFFERENT gap (not closed by the mapping)

GAP-5 also notes that after accounting for the 8×, moe_replay's implied
per-call-pair cost (~0.171 ms at c=1) is still ~1.87× the standalone
grouped_gemm+gather_scatter probe (~0.092 ms). This is **not** a normalization
issue. `window_replay` times two `argsort` calls inside the timed region:

- `idx = torch.argsort(route)` — `benchmark.py:538`
- `out = (y @ down).index_select(0, torch.argsort(idx))` — `benchmark.py:541`

The standalone probes **exclude** these: `order`/`inverse` are precomputed
**outside** the timed closure (`benchmark.py:445-446`), so `grouped_gemm`
(`benchmark.py:475-478`) and `gather_scatter` (`benchmark.py:463-464`) never
time an argsort. window_replay therefore carries an **unmeasured sort/permute
operator** that no existing probe isolates.

This is exactly `CANDIDATE_C = BLOCKED_ON_MEASUREMENT` from A1 v3
(stage_ledger `v3_fitside_evaluation`) and opens **V2-GAP-C** (a sort/permute
microbenchmark). It needs GPU time; the launch-granularity mapping does not
resolve it and does not claim to.

## Conclusion

- **cpu_calls ↔ launch-granularity mapping: RESOLVED_BY_CODE.** Documented above
  with derivation and line references; ROUTING_WIDTH = 8 from the trace. GAP-5's
  first solution path is satisfied without GPU time.
- **Residual 1.87×: NOT part of GAP-5's mapping question** — it is an unmeasured
  argsort operator, tracked as V2-GAP-C, requiring a GPU sort/permute probe.

Claim boundary: this is a harness-semantics result derived from reading frozen
code (TRACK_GPU_PREP §7). It makes no GPU performance claim.

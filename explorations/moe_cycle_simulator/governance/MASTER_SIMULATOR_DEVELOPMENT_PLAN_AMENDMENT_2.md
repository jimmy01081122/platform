# MASTER_SIMULATOR_DEVELOPMENT_PLAN — Amendment 2

## Status and authority

```text
PLAN_STATUS: FINAL_REVIEW_CANDIDATE
PHASE_0_FREEZE: ELIGIBLE_AFTER_SAME-HASH_REVIEW
GPU_AUTHORITY: NONE
```

This document is the repository-local, reviewable contract for a trace-driven,
cycle-resolved, resource-constrained, dependency-aware, measurement-calibrated,
multi-platform and replayable heterogeneous MoE system simulator.

The simulator models control, routing, scheduling, movement and resource contention
with cycle-resolved semantics. GEMM, attention and expert FFN service are measured,
calibrated surrogates. The project must not claim unobserved proprietary GPU pipeline
behavior as cycle-accurate truth.

## Canonical model and platform boundary

```yaml
canonical_model:
  model: Mixtral-8x7B-Instruct-v0.1
  precision: BF16
  runtime_primary: vLLM
  runtime_cross_check: official Mistral inference
  sampling: deterministic
```

The initial platform matrix is:

- RTX PRO 6000 Blackwell 96 GB: conditional discrete BF16 candidate. It requires a
  separately approved `BF16_CAPACITY_AND_RUNTIME_QUALIFICATION`.
- DGX Spark GB10: coherent UMA only. It must not replace discrete-GPU calibration.
- RTX 3090 x2: NVLink/P2P, FP16, quantized, offload and placement studies. It is not
  an all-resident canonical BF16 platform.
- RTX 3090: bounded offload and single-discrete modeling.
- V100 x8: future FP16 adapter; not an immediate prerequisite.
- RTX 3050: pipeline smoke only; full Mixtral execution is forbidden.
- Guaranteed all-resident discrete BF16 baseline: deferred.

Every large-model download, GPU workload, paid server and formal profiling campaign
requires a new exact application. Nothing in this document grants GPU authority.

## Development tracks

```text
Track A — Evidence and adapters
  A0 runtime/schema spike
  A1 minimal vLLM adapter
  A2 Mistral semantic cross-check
  A3 collector/alignment/observability feasibility
  A4 formal Mixtral profiling

Track B — Simulator
  B0 canonical IR
  B1 deterministic event/resource core
  B2 single-discrete-GPU model
  B3 routing/residency/policy
  B4 multi-GPU/UMA
  B5 calibration/DSE/RTL integration
```

A0–A3 are CPU/mock-only and precede the canonical IR freeze. Fixtures must exercise
request, token, operator/kernel, router/top-k, allocation/copy, NCCL/P2P, stream/rank
correlation and timestamp-domain semantics. Unsupported GPU fields remain
`SCHEMA_HYPOTHESIS` or `UNAVAILABLE`; mock data is never measured GPU evidence.

## Canonical IR and serialization

The versioned IR set is:

```text
WorkloadIR
ModelIR
RoutingIR
PlacementIR
PlatformIR
EventIR
ClockAlignmentIR
CalibrationIR
ResultIR
```

Large traces use Arrow IPC plus Zstd when the optional dependency is available.
Contracts and small artifacts use strict JSON. Every artifact has a file SHA-256 and a
canonical semantic hash. Semantic routing records and aggregate expert-demand records
are distinct; aggregate demand must not be used to invent token-level routing.

## Exact clock and CDC contract

```yaml
GlobalTime:
  representation: unsigned_128_bit_integer
  unit: femtosecond
  serialization: unsigned_decimal_string

ClockDomain:
  frequency_numerator_hz: unsigned_integer
  frequency_denominator_hz: unsigned_integer
  phase_offset_fs: unsigned_integer
  local_cycle: unsigned_64_bit
  fractional_remainder: unsigned_integer
```

The reference edge is:

```text
edge_time(n) =
  phase_offset_fs
  + floor(n * 10^15 * frequency_denominator_hz / frequency_numerator_hz)
```

Exact division or a remainder accumulator may be used, but every edge must equal the
reference. Floating-point event time and repeated rounded-period addition are
forbidden. `rounded_period_fs` is display-only. Simulator drift from the rational
reference is exactly 0 fs.

Checkpoint remainder state is canonical:

```text
fractional_remainder(n) =
  (n * 10^15 * frequency_denominator_hz) mod frequency_numerator_hz
```

It is always less than `frequency_numerator_hz`. Restore recomputes and compares it;
implementation-private accumulator state cannot enter a portable checkpoint.

The deterministic tie key is:

```text
(time_fs, event_priority, request_id, token_index,
 layer_index, component_id, event_id)
```

Cross-domain paths are:

```text
source completion
-> forward bridge latency
-> receiver next-edge ceil
-> receiver synchronization cycles
-> crossing queue
-> destination processing
-> optional acknowledge path
```

Every bridge declares protocol (`ONE_WAY`, `REQUEST_ACK` or `CREDIT`), forward and
reverse latency, receiver and acknowledgement synchronization cycles, queue capacity
and backpressure policy. The request path must provide strict time progress through a
positive forward latency or at least one receiver synchronization cycle, even when the
source and target clocks differ.

## ClockAlignmentIR

Each CPU, profiler, CUDA/device, telemetry and per-rank clock receives its own mapping:

```yaml
source_clock_id:
target_clock_id:
transform_type: IDENTITY | AFFINE_RATIONAL | PIECEWISE_AFFINE_RATIONAL
scale_numerator:
scale_denominator:
offset_fs:
calibration_method:
calibration_points:
residual_error_fs:
confidence_interval_95_fs:
valid_time_range:
drift_bound_ppm:
provenance:
```

The confidence interval is an interval of signed alignment error in femtoseconds.
Its integer half-width is `ceil((upper_error_fs-lower_error_fs)/2)`. Grade derivation
binds the target ClockDomain profile hash, its exact rational period, the shortest
calibrated component-duration record hash and that duration. The validator recomputes
the grade; `claimed_grade` is never trusted.

Affine conversion is integer/rational:

```text
t_target = floor(t_source * scale_numerator / scale_denominator) + offset_fs
```

Alignment quality is:

- `CYCLE_GRADE`: 95% CI half-width is no greater than one quarter of the target clock
  period and 5% of the shortest calibrated component duration.
- `ORDERING_ONLY`: not cycle grade, but compared event intervals do not overlap.
- `AGGREGATE_ONLY`: intervals overlap; only same-source duration or aggregates apply.
- `UNAVAILABLE`: no valid mapping or time is outside its validity range.

Only `CYCLE_GRADE` can calibrate cross-domain launch delay, overlap, queue delay or
critical path.

## Observability, fidelity and range

Observability uses orthogonal fields:

```text
availability = CONFIRMED | CONDITIONAL | UNAVAILABLE | NOT_APPLICABLE
evidence_mode = MEASURED | DERIVED | INSTRUMENTED | NONE
```

`CONDITIONAL` requires `evidence_mode=NONE` and may list expected modes. After the
Phase 1 spike it can become confirmed evidence, unavailable, or not applicable.
Conditional observations cannot pass calibration.

Result classification is also orthogonal:

```text
fidelity =
  MEASURED | CALIBRATED_SURROGATE | ANALYTIC_FIRST_ORDER |
  FUNCTIONAL_ONLY | UNAVAILABLE

range_status =
  IN_CALIBRATION_ENVELOPE | INTERPOLATED | EXTRAPOLATED | RANGE_UNKNOWN
```

Non-measured results retain envelope distance, nearest calibration point and
calibration-profile hash. Extrapolated results are exploratory only.

## Runtime and workload identity

Each runtime variant fixes the runtime/container/CUDA/driver commits, attention and
fused-MoE backends, tensor/expert/pipeline parallel sizes, distributed executor,
CUDA graph/eager mode, maximum model length/batched tokens/sequences, scheduler, KV
cache dtype, NCCL environment, placement/offload, kernel backend, seed, generation
parameters and collector hash.

The frozen benchmark matrix is:

| Category | Core samples | Deep subset |
|---|---:|---:|
| GSM8K | 8 | 2 |
| LongBench v2 | 8 | 2 |
| HumanEval+ | 16 | 4 |
| BigCodeBench-Hard Instruct | 16 | 4 |
| Total | 48 | 12 |

Selection is prospective and hash-stratified by dataset revision, sample ID and
prompt-length bucket. Correctness, routing and latency must not influence selection.
Code evaluation requires a rootless, network-disabled sandbox with CPU, memory, time
and process limits.

Task-level termination ceilings are:

```text
GSM8K:                512 -> 1024 -> 2048
LongBench v2:          64 ->  128 ->  256
HumanEval+:           512 -> 1024 -> 2048
BigCodeBench-Hard:   1024 -> 2048 -> 4096
```

The smallest common legal ceiling is frozen per task. A task that fails at the largest
ceiling cannot be promoted; per-sample exceptions are forbidden.

## Routing ambiguity

Routing compares the canonical score immediately before top-k selection. Records retain
the score dtype, absolute/relative tolerances, kth boundary score and ambiguity set.

```text
abs(a - b) <= absolute_tolerance
              + relative_tolerance * max(abs(a), abs(b))
```

The initial absolute tolerance is four ULP at the boundary magnitude in the source
dtype; canonical scores are exact decimal expansions of values representable by that
dtype. The relative tolerance may not exceed `1e-5`. Phase 1 may tighten but formal
results may not loosen it. Non-ambiguous top-k IDs match exactly. Under ambiguity,
non-boundary IDs match exactly and boundary selections belong to the same defined
ambiguity set.

## Passes, validation and promotion

```text
P0 clean correctness and authoritative timing
P1 operator/kernel shape and service timing
P2 routing/top-k/token-expert mapping
P3 allocation, residency, H2D/D2H and copy engine
P4 P2P/NVLink
P5 session-level >=30-second steady-state telemetry
V0 offline IR conversion and replay validation; no runtime/GPU execution
```

Single-request replay requires exact input IDs, generation, output tokens and stop
reason. Routing follows the ambiguity rule. Serving replay requires the exact request
set, arrival trace, generation configuration, execution validity and completion set;
aggregate routing is confidence-bounded, while batch/schedule/stream timing is
observational.

Promotion is staged:

```text
M0 runtime/capacity qualification
M1 8-sample collector canary
M2 48-sample P0/P2 core matrix: 288 cells
M3 12-sample P1/P3/P4 deep subset: 108 cells
M4 six serving scenarios, three repetitions, clean/instrumented: 36 sessions
```

Changing collector, adapter, schema, correlation, alignment, routing hook or generation
identity after M1 creates a new variant and requires a new M1.

## Calibration statistics

Every blocking metric retains its point estimate, deterministic stratified 10,000-draw
bootstrap 95% CI, sample count, at least three repetitions, noise floor, seed and
strata. The seed derives from the calibration-manifest hash.

For lower-is-better metrics:

```text
PASS: upper CI <= threshold
FAIL: lower CI > threshold
INSUFFICIENT_EVIDENCE: interval crosses threshold or evidence is inadequate
```

Relative metrics use `max(abs(measured), 10 * measurement_resolution)` as denominator.

Component gates are median absolute relative error <=10%, weighted MAPE <=10%,
normalized MAE <=10%, absolute signed bias <=5%, p95 relative error <=20%, and
90%-prediction-interval empirical coverage >=80%.

End-to-end gates are TTFT/TPOT/request-latency median relative error <=10%, p95 relative
error <=15%, throughput error <=10%, and moved-byte/peak-memory error <=5%.

Blocking p95 requires at least 200 held-out requests, 20 observations in the baseline
top decile and a bootstrap CI. Blocking p99 requires at least 1,000 held-out requests
and a bootstrap CI. Otherwise tail results are observational.

DSE validation requires at least five distinct points, ten pairwise held-out
comparisons, two platform/workload strata and three repetitions per point. It then
requires Spearman rho lower CI >=0.90, 100% direction agreement beyond the combined
noise floor, speedup error upper bound <=10%, and break-even error no greater than 15%
of the sweep range or one grid step. Inadequate data is
`INSUFFICIENT_DSE_VALIDATION_POINTS`, not PASS or FAIL.

## Checkpoint and RTL contract

Simulation checkpoints bind the event queue, global/local clock and remainder state,
resources, request/token/layer state, placement/residency, policy/random state,
alignment state, backend state and all manifest/build/schema hashes.

The stable RTL base ABI is:

```text
reset
can_accept
submit
advance
poll_completions
snapshot_counters
```

Factory metadata negotiates optional `TimeDecoupledBackendV1` and
`CheckpointableBackendV1`. A non-checkpointable interrupted session cannot resume.

## Phases and acceptance

| Phase | Delivery | Acceptance |
|---|---|---|
| 0 | Governance/evidence contracts | Same-hash Architecture, Model and Trace GO |
| 1 | Minimal trace/schema/alignment spike | Shapes, correlation, alignment and observability fixtures pass |
| 2 | Canonical IR/codec | Strict validation and semantic round-trip hash match |
| 3 | Deterministic event/resource engine | Rational clock, CDC, capacity and deadlock tests pass |
| 4 | Single-discrete-GPU model | Compute/memory/copy contention is replayable |
| 5 | Routing/residency/policy | Conservation, causality, capacity and ablation pass |
| 6 | Multi-GPU/UMA | Topology, crossing, pressure and coherence tests pass |
| 7 | Formal adapters/profiling | M0–M4 are separately approved and promoted |
| 8 | Calibration/validation | Component, end-to-end and DSE gates are separate |
| 9 | DSE/RTL integration | Dummy backend, capability and checkpoint tests pass |
| 10 | Release | Clean checkout, CPU regression, ledger and guide are complete |

Estimated effort is 24–34 person-weeks for the simulator MVP, 40–60 for validated
multi-platform v1, and 55–80 for the RTL-integrated release. These are estimates, not
completed work or delivery-date promises.

## Phase governance and legal next action

Every phase declares goal, scope, non-goals, inputs, outputs, dependencies, assumptions,
unknowns, alternatives, risks, validation, acceptance, resources, owner decisions,
next legal action and forbidden actions.

The legal sequence is:

1. Freeze this plan, source registry, CPU scope and checksum ledger as one candidate.
2. Architecture/System, Model/Benchmark and Trace/Provenance review the identical hash
   set.
3. Only unanimous GO plus owner CPU approval permits Phase 1 CPU/mock implementation.
4. Canonical IR freezes only after Phase 1 passes.
5. Every GPU platform receives a separate Phase 7 server application.

```text
CURRENT_AUTHORITY:
  CPU-only planning, packaging, tests and approved Phase 1 implementation

NOT_AUTHORIZED:
  Mixtral download
  GPU query or workload
  paid server
  formal profiling
  old-session resume
  frozen-evidence modification
```

S4-R6 remains the higher-priority CPU governance path. This exploration must not modify
or reinterpret G3-R4, S4-R5, S4-R6 or any other frozen evidence.

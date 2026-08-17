# Phase 6 Multi-Domain Scheduler Result

```text
PHASE6_R1_REVIEW: MODIFY / IMMUTABLE
PHASE6_R2_REVIEW: MODIFY / IMMUTABLE
PHASE6_R3_REVIEW: MODIFY / IMMUTABLE
PHASE6_R4_PACKAGE: PASS
SAME_HASH_REVIEW: PENDING
PHASE6_FREEZE: NOT YET
SCHEDULER: MultiDomainSchedulerV1
PROFILE_ORIGIN: CPU_SYNTHETIC
RANGE_STATUS: RANGE_UNKNOWN
GPU_USED: false
MODEL_DOWNLOADED: false
CALIBRATION_PASS: false
```

The candidate adds one global multi-domain scheduling authority. It does not
compose independent Phase 4 schedulers. It reuses the frozen exact unsigned
time, rational clocks, bridges, event keys and hash primitives, while one
ordered event queue owns global time, dependencies, resources, crossings and
trace state.

`DISCRETE_P2P_2GPU` has two compute domains, two private VRAM domains and two
explicit directed single-hop links. Each direction declares its clock,
bridge, finite queue, credit or request-acknowledge protocol, service profile
and duplex group. Capacity is checked per VRAM; aggregate installed bytes
cannot rescue local overflow. Token-level selected-expert identity is
preserved. Remote assignments require dispatch, expert compute, return and
combine dependencies, while local assignments reject phantom transfers.
Each route requires exact request, token and layer fields at priority 100.
Every selected expert belongs to that route layer, and dispatch, expert
compute, return and combine preserve the identical request/token/layer tuple.
The empty request, `UINT64_MAX` token and `UINT32_MAX` layer values are
reserved missing-value sentinels and are rejected for required token routes.
Whole immutable expert replication reserves destination capacity before
visibility. A move commits destination placement and source removal
atomically; a pinned move is hard-rejected in this minimal version.
Admission prevalidates timing, alignment, counters, resources, queue/credit
state, destination capacity and reservation insertion before mutation. A
rejected admission is covered by a byte-identical checkpoint regression.
Duplicate in-flight `(object_id, target_memory_id)` reservations are forbidden.

Every referenced Phase 5 action is backed by its complete compiled-action
preimage. Phase 6 recomputes the frozen `moe-phase5-compiled-action-v1` ID from
the exact Phase 5 plan digest, sequence and semantic fields before accepting
membership. Selected `ExpertKey` values bind to one immutable resident object
with a cryptographic content identity.

`COHERENT_UMA_2COMPUTE` has two compute agents and one physical memory/fabric
capacity. Aliases do not consume bytes twice and ordinary accesses never
create P2P traffic. Immutable expert objects are read-only. A separate mutable
object fixture validates object-granular `UNCACHED`, `SHARED` and `MODIFIED`
visibility, monotonic versions, release and subsequent read. This is a
functional release/acquire abstraction, not a cache-line protocol model.
Same-object UMA operations are serialized explicitly, all directory states
enforce complete owner/sharer invariants, and version increment overflow is
rejected before state mutation.

Each compute and fabric clock has an exact synthetic alignment record:
`SIMULATOR_EXACT_SYNTHETIC`, zero residual, `[0,0]` confidence interval and
`CYCLE_GRADE`. This only states exact internal simulator alignment. It is not
measured device/rank clock evidence.

Crossings emit `START`, `VISIBLE` and a distinct terminal `ACK_OR_CREDIT`.
The acknowledgement uses frozen priority 40 and owns credit return, resource
release and dependency transition. Priority 50 remains reserved for
`DEPENDENCY_READY`; crossing operations do not repurpose it. Inclusive
alignment ranges are checked at admission, visibility, acknowledgement,
next-time advancement and checkpoint/restore.

The live checkpoint binds topology, contracts, Phase 5 authority, payload
profile, complete program, global and local time, resources, queues, credits,
objects, pins, versions, trace and metrics. Object and wire restores are
accepted only when deterministic prefix replay reconstructs the same canonical
state. Tamper, trailing bytes and topology mismatch are rejected.

The R1, R2 and R3 reviewed packages remain immutable under their corresponding
`governance/history` directories. R2 exposed incomplete token identity; R3
then exposed accepted missing-value sentinels. The fresh R4 sealed CPU run is
`20260729T105347Z__moe_cycle_simulator_phase6_multi_domain_r4__S4`. It binds
source root
`dad66c54d0f84e2ad24926f3e614589fac22f1e5f3aa08aa5305ec9ff8c23c74`;
its exact-set run ledger is
`6933d64c5a02927efdf35847bcd5131bb4db89a62bebcce40231d75e2c933a8c`.
Release CTest passes Phase 6 and inherited Phase 3/4/5 regressions. Native
ASAN/UBSAN executions pass. The 1,000-operation fixture satisfies its frozen
comparison-count envelope.

This evidence is `FUNCTIONAL_ONLY` or `ANALYTIC_FIRST_ORDER` with
`RANGE_UNKNOWN`. It does not measure RTX 3090 NVLink/P2P, DGX Spark coherent
UMA, Mixtral, latency, bandwidth, speedup, break-even or production behavior.
Phase 6 remains unfrozen until Architecture/System, Model/Benchmark and
Trace/Provenance all return `GO` with empty blockers on the identical ledger.

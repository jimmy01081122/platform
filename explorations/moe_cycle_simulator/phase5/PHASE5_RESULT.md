# Phase 5 Routing, Residency, and Policy Result

```text
PHASE5_STALE_R1: NOT PROMOTED
PHASE5_R2_PACKAGE: PASS
SAME_HASH_REVIEW: PENDING
PHASE5_FREEZE: NOT YET
EXECUTION_MODE: TRACE_COMPILED_NON_ADAPTIVE
PROFILE_ORIGIN: CPU_SYNTHETIC
RANGE_STATUS: RANGE_UNKNOWN
GPU_USED: false
MODEL_DOWNLOADED: false
CALIBRATION_PASS: false
```

The candidate compiles frozen token-level routing demands and arrived,
hash-bound prefetch hints into one deterministic operation DAG. The frozen
Phase 4 `SingleGpuModel` is instantiated once and remains the sole timed
scheduler. The compiler does not predict lane assignment or completion time,
and trace postprocessing validates rather than repairs the schedule.

Capacity is authoritative in bytes. `reserved_nonexpert_bytes` and resident
expert bytes share one total capacity. Experts are identified by layer and
expert ID; every selected expert has exactly one compute assignment. Aggregate
routing cannot be expanded into token routing, and no routing weights or
weighted expert combination are invented.

Clean immutable evictions use explicit memory operations and require no
writeback. Loads use explicit H2D operations. A route barrier completes only
after the full required expert set is resident, then pins that set until all
corresponding compute assignments complete. LRU/FIFO and OFF/HINT fixtures
verify deterministic ablation behavior without making a performance claim.
Hints carry a complete earlier `EventKey`, target one selected expert, and
cannot use future-oracle information.

Checkpoint evidence is a real live Phase 4 prefix. The outer checkpoint binds
the compiled plan, complete Phase 4 state, trace-prefix residency replay, and
outer state digest. Object and wire restore continue the same scheduler state
and produce an exact terminal semantic digest equal to uninterrupted
execution. A terminal checkpoint paired with an earlier replay cursor is
forbidden.

The first sealed run began before the implementation agent's last two
capacity checks were written. It binds an earlier source root and is
`STALE_BEFORE_REVIEW`; it is excluded from the candidate ledger and can never
be promoted or reinterpreted.

The fresh sealed CPU run is
`20260728T130835Z__moe_cycle_simulator_phase5_routing_residency_r2__S4`.
It binds source root
`828971fa2e035ab1940abf4ac00293c8f1bc036fb9f90b8a5a7537474ad63d3f`;
its exact-set run ledger is
`e7762348513b1aacbb7f8cb6d5ac1b496980616c11e1a37b44c0949fa24e6884`.
Release CTest passes Phase 5 and inherited Phase 3/4 regressions. Native
ASAN/UBSAN executions pass. The 1,000-demand fixture verifies one indexed
action lookup per trace event inside a bounded CPU envelope.

This is functional CPU-synthetic evidence with `RANGE_UNKNOWN`. It is not
Mixtral execution, measured GPU timing, calibration, production-scale
validation, multi-GPU, NVLink, P2P, or coherent UMA evidence. Phase 5 remains
unfrozen until Architecture/System, Model/Benchmark, and Trace/Provenance all
return `GO` with empty blockers on the identical candidate ledger.

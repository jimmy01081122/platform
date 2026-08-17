# Phase 4 Single-Discrete-GPU Model Result

```text
PHASE4_STALE_R1: NOT PROMOTED
PHASE4_R2: MODIFY / IMMUTABLE
PHASE4_R3: MODIFY / IMMUTABLE
PHASE4_R4_PACKAGE: PASS
SAME_HASH_REVIEW: PENDING
PHASE4_FREEZE: NOT YET
SERVICE_MODE: CPU_SYNTHETIC_REPLAY
GPU_USED: false
MODEL_DOWNLOADED: false
CALIBRATION_PASS: false
```

The candidate implements one C++20 single-discrete-GPU timed-service state
machine. It reuses the frozen Phase 3 unsigned-128 time, exact rational clock
and SHA-256 primitives, but it does not invoke a second scheduler. Compute,
memory, H2D and D2H each have explicit primary lanes. Memory and copies also
reserve a shared fabric atomically, while compute may overlap that traffic.
Service duration uses integer ceiling arithmetic and completion is snapped to
the exact next edge of the declared rational clock.

Dependencies are validated independently as an ID DAG. Future arrivals, ready
operations and completions use ordered queues. The complete generated EventIR
key determines deterministic admission; copy completion/start priorities are
20/90 and compute or memory completion/start priorities are 30/100. Generated
event identities bind the platform authority hashes, full service profile,
input key, dependency list, service class, work and trace kind.

The checkpoint preimage is length-prefixed, tagged and counted. It covers
platform and authority identity, service profiles, operations, operation and
reservation state, completion times, dependency counters, future/ready/
completion queues, busy masks, exact clock cursor, schedule entries, trace,
actual class-metric map and scheduler metrics. The wire format byte-counts the
complete canonical body and appends its SHA-256. Restore rejects corruption,
trailing bytes, invalid authority, noncanonical class metrics and any state
that is not the exact deterministic reachable prefix. Running checkpoints
preserve the empty derived semantic digest; terminal checkpoints preserve the
non-recursive canonical state digest.

The authority boundary is not caller-labelled. The constructor requires the
exact frozen Phase 3 ledger and the exact SHA-256 values of the Phase 4 build
authority, model contract and checkpoint schema artifacts. Wrong but
well-formed values for each authority are negative-tested.

R2 was formally reviewed on ledger
`a90b7e0f8717b129e178d50593ad4b24e88cf723b7f658812ae1b398cdf4a389`
and remains immutable `MODIFY` evidence. Architecture/System found that an
invalid `TraceKind` was normalized to `COMPLETE` by a binary serializer and
could therefore escape the checkpoint preimage. Its exact reviewed source,
run and ledger are preserved under the independently checksummed R2 history
snapshot. The prospective fix uses an exhaustive enum switch; both an
in-memory invalid enum and a rehashed wire `INVALID!` token are rejected.

The fresh sealed CPU run is
`20260728T123113Z__moe_cycle_simulator_phase4_single_gpu_r3__S4`. It binds
source root
`510c2cbe19f4187f0f2c6b28fdc8dbc2f188c921daf1b20b1b31d4d27ce243ad`.
Release CTest passes all three tests, including inherited Phase 3 regressions.
Native Phase 4 and Phase 3 ASAN/UBSAN executions pass. The 1000-operation
fixture completes within its bounded CPU envelope and verifies queue/event
conservation plus a deterministic comparison-count bound.

R3 then received `GO` from Architecture/System and Trace/Provenance, but
Model/Benchmark returned `MODIFY` on ledger
`8b94a7d30510102d612f5663a1153fad2fe08aec4a02ffbba210398fd61e4785`.
The source and run were valid; the top-level exact-set generator used an
over-broad filename exclusion and omitted one nested historical
`checksums.sha256`. The complete 89-member R3 reviewed candidate and its
reviews are preserved under the immutable R3 history snapshot. R4 changes
only governance packaging: it excludes exactly the active ledger path and
includes every nested historical ledger. The executable source root and
fresh R3 run remain unchanged.

The still older run
`20260728T120337Z__moe_cycle_simulator_phase4_single_gpu_r1__S4` binds an
obsolete source root and is `STALE_BEFORE_REVIEW`. It is excluded from this
candidate ledger and can never be promoted or reinterpreted as Phase 4 PASS.

This result is not measured GPU timing, calibration, production-scale
validation, model execution, routing/residency policy, multi-GPU or UMA
evidence. Phase 4 remains unfrozen until Architecture/System,
Model/Benchmark and Trace/Provenance all return `GO` with empty blockers on
the identical candidate ledger.

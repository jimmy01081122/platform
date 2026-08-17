# Phase 2 Canonical IR and Codec Result

```text
PHASE2_R1: MODIFY / IMMUTABLE
PHASE2_R2: MODIFY / IMMUTABLE
PHASE2_R3: MODIFY / IMMUTABLE
PHASE2_R4: MODIFY / IMMUTABLE
PHASE2_R5: MODIFY / IMMUTABLE
PHASE2_R6_IMPLEMENTATION: PASS
SAME_HASH_REVIEW: PENDING
CANONICAL_IR_FREEZE: NOT YET
GPU_USED: false
MODEL_DOWNLOADED: false
```

The candidate implements all nine IR families in a strict closed schema:
WorkloadIR, ModelIR, RoutingIR, PlacementIR, PlatformIR, EventIR,
ClockAlignmentIR, CalibrationIR and ResultIR.

Every cross-IR reference contains target kind, schema version, partition
semantic root and primary key. Effective semantic descriptors bind the complete
logical schema and executable invariant implementation. Placement snapshots
carry tensor/shard and memory ranges, compute assignment, non-expert/KV state,
predecessor and migration lineage. Platform and Event IR expose executable
service, capacity, bridge, queue, backpressure and resource-demand semantics.
ClockAlignmentIR includes calibration points, valid range, piecewise model,
uncertainty inputs and a validator-derived grade bound to the exact target
clock profile and shortest-component evidence. Runtime-variant hashes bind
Workload, Event, Calibration, Result and ArtifactEnvelope identities.
The envelope embeds each complete Phase 0-conforming runtime manifest and
validates its content-derived variant identity. Every referenced calibration
profile is likewise embedded, domain-separated and content-addressed, and
closes model, platform, runtime, training/held-out workload and source evidence
roots. Exact integer validation is schema-driven for every unsigned and signed
Canonical IR decimal field, with explicit u128/s128 bounds.
Every PlatformIR bridge also carries the frozen `bridge-v1` identity, passes
the exact Phase 0 bridge schema and reuses `validate_bridge` for CREDIT policy,
acknowledge-path and strict-progress semantics. Thus a CREDIT bridge requires
`CREDIT_BLOCK`, non-CREDIT bridges cannot claim it, and a request path must
advance through forward latency or receiver synchronization.
EventIR carries the complete frozen seven-field ordering key: time, priority,
request, token, layer, component and event identity. Nullable token/layer
indices use the Phase 0 unsigned-maximum sentinels, while non-null indices
cannot collide with those sentinels. The Phase 0 event schema, architecture
decision and priority registry hashes are contract-bound, and projected
canonical events pass `validate_events`; unknown types, priority drift,
dependency cycles and same-time causal inversions are rejected.

The physical codec permits one or more non-overlapping Arrow IPC partitions per
IR kind, each wrapped in one mandatory Zstd frame. Its global canonical-merge
identity is independent of partition count and batching. The profile fixes
Arrow metadata version V5, no IPC
body compression, libzstd 1.4.8, level 3, field order/types/nullability and
allocation/decompression limits. It rejects uncompressed fallback, trailing or
concatenated frames, corrupt/truncated payloads, extra/missing/symlink entries,
unknown metadata and schema coercion. ArtifactEnvelope is byte- and
complexity-bounded before JSON parsing.

The sidecar ArtifactEnvelope binds every partition file hash, row count, key
range and semantic root, plus the global canonical-merge root, codec and
contract hashes, producer build and evidence boundary. Publication uses a
sibling staging directory, fsync, complete read-back, atomic rename and parent
directory fsync.

R1 through R5 were reviewed on ledgers
`f12a8178e6070d64c14b4c573ab1bbf35b5d8a3425008776bffcd5a15d9d2ed9`
`5ae4f8d98923087385d7face44adf5b451428b15b6faf0eab44867f5b2d2baa6`
`2f713135b64efb2dc4c9f72e93421183ff0e3ea40448d85c59aeeb77d3f3f2d0`
`4774792f298d398a4c3d7df8e521afa0c0b2e8543da5ad9d7ed7c49ac3f0156a`
and `ac1d3c8f3a81e50302f5bf3debc8a6f33717d24de33f0f5f00e50138bfcd8fb2`;
all remain immutable `MODIFY` evidence. Their reviewed source bytes and sealed
runs are preserved under five independently checksummed historical snapshots.
The prospective R6 CPU run is
`20260726T074835Z__moe_cycle_simulator_phase2_canonical_ir_r6__S2`.
It contains 11 records across nine partitions. Its semantic root is
`c07d9ec1cbc4d67652f3f73e34e69e27ffc15427d186c8bbb7e28e93f3e5efd7`.
Source validation, bundle replay, 43 tests and the Python/C++ semantic hash
golden pass.

This result is not a phase freeze until all three same-hash reviewers return
GO with empty blockers. It contains no event simulator, model execution,
runtime profiling, GPU evidence or calibration evidence.

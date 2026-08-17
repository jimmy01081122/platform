# Phase 3 Deterministic Event and Resource Core Result

```text
PHASE3_R1: MODIFY / IMMUTABLE
PHASE3_R2: WITHDRAWN BEFORE REVIEW
PHASE3_R3: MODIFY / IMMUTABLE
PHASE3_R4: MODIFY / IMMUTABLE
PHASE3_R5_IMPLEMENTATION: PASS
SAME_HASH_REVIEW: PENDING
PHASE3_FREEZE: NOT YET
SERVICE_MODE: REPLAY_VALIDATE
GPU_USED: false
MODEL_DOWNLOADED: false
```

The candidate provides one authoritative C++20 scheduling core. Python is a
thin `ctypes` adapter and does not implement a second scheduler. Global time is
an unsigned 128-bit femtosecond integer. Clock edges use exact rational
division, receiver crossings use exact edge ceiling, and rounded-period
accumulation is absent. The engine orders simultaneous work by the complete
Phase 2 seven-field key.

The engine models deterministic dependency release, compute-slot, memory-byte,
queue-entry and bridge queue/credit capacity. FIFO and priority arbitration are
explicit. `ROUND_ROBIN`, transfer actions and Phase 4 link/shared/copy/
interconnect resources fail closed. `SERVICE` records replay demand but does
not manufacture completion time; performance service models remain Phase 4.
Capacity and holder conservation, wrong-owner release, dependency inversion,
deadlock, event budget and same-time Zeno behavior have negative tests. The
sums used for holder conservation are arbitrary-precision and checked before
conversion, so the unsigned-128 maximum cannot wrap. Fresh construction rejects
preloaded waiters; only the private checkpoint reconstruction path can admit
validated runtime wait queues.
The
scheduler selects the earliest dependency-ready event, so an unready dependent
cannot prevent a later independent release from unblocking its predecessor.
Input event counts, every completion transition and a bounded iterative
wait-for traversal enforce the frozen limits. Same-time accounting covers each
blocked or completed transition, including multiple waiter completions within
one release step. Transition-limit preflight occurs before any event, waiter,
holder or occupancy mutation, so a Zeno hard-stop remains internally
consistent and self-restorable.

Checkpoint state uses canonical length-prefixed fields rather than heap layout.
It binds Phase 2 ledger, Canonical IR root, engine build/profile and checkpoint
schema hashes. Its SHA-256 preimage is the complete canonical checkpoint body
except the digest field itself, including the event program, last-event time
and static resource semantics. Restore rejects hash mismatch, corruption,
trailing data, event/state or waiter/state mismatch, counter/trace/terminal
inconsistency, non-monotonic trace time, resource-conservation drift and
rational-clock remainder drift. Restore also reverse-derives the initial
resource state from the recorded transition history, replays that history with
the sole deterministic scheduler, and requires an exact full-state digest
match. Counter/time, dependency causality, trace order, terminal state and
current resources therefore form one reachable prefix rather than independent
fields. FAILED checkpoints are non-resumable until a versioned failure-evidence
contract exists. Continuous and restored executions agree at every event
boundary. State identity uses an
internally implemented SHA-256 verified against the empty and `abc` standard
test vectors.

The reviewed source assumes all input has passed the frozen Phase 2 Canonical
IR validator. Therefore strings are already NFC-normalized and event priorities
already match the Phase 0 registry before the trusted low-level C++/C boundary.
This assumption is explicit and does not authorize bypassing the Phase 2 input
gate in production adapters.

R1 was reviewed on ledger
`abeafed5b02929d0a592eb45c0aebfce3f24c2da97aaa34d9afbbc525154ab2e`
and remains immutable `MODIFY` evidence. Its exact 45 reviewed members and
nested run are preserved under an independently checksummed historical
snapshot. R2 ledger
`7708e8a6c49b1b2a9627f15c12daa0a7735d1e847eaaffb1930c282c7a6ba082`
was withdrawn before review when self-audit found incomplete same-time
accounting in waiter drain; it is never promoted. R3 was reviewed on ledger
`7bf3b31c84f21ba9647729ba6532b1ac8aa99f0d77c5af605884e09a65ecbe38`
and remains immutable `MODIFY` evidence in a self-contained 97-file snapshot.
R4 was reviewed on ledger
`6ade9bbfaefacd8cbebbc30fe2181c95b54635fb3da0af581b5e349cf2e6d956`
and remains immutable `MODIFY` evidence in a self-contained 199-file snapshot.
The prospective R5 formal CPU run is
`20260728T113936Z__moe_cycle_simulator_phase3_event_core_r6__S3`. It binds
source root
`c4bf4e7c3a24f588357b639a880d7bbe3d2bbe54c26afdb3ce85afab5dadb9ea`.
Release CTest, C++ ASAN/UBSAN with leak detection, and the ASAN/UBSAN Python
binding smoke all pass. Build evidence now records Boost, OS, kernel and
architecture. The sealed run exact-set checksum validation also passes. The
earlier exploratory run predates final hash and checkpoint hardening, is
excluded from the candidate ledger and is never promoted.

This result is not a phase freeze until Architecture/System, Model/Benchmark
and Trace/Provenance all return `GO` with empty blockers on the identical
ledger. It is not GPU, model execution, runtime profiling, platform
qualification or calibration evidence.

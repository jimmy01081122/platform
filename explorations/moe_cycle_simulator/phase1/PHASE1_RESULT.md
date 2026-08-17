# Phase 1 CPU/Mock Trace-Schema-Alignment Spike

## Formal status

```text
PHASE1_CPU_MOCK_SPIKE: PASS
V0_OFFLINE_REPLAY: PASS
GPU_USED: false
MODEL_DOWNLOADED: false
FORMAL_RUNTIME_PROFILING: NOT_PERFORMED
CANONICAL_IR_FREEZE: NOT YET AUTHORIZED
```

The reviewed Phase 0 ledger is
`4a53c3d2fbbab330151679ea2b831b91b404239b88db7edc616f2900d61544bc`.
R1 was reviewed as `MODIFY / GO / MODIFY` and remains immutable. The
prospective R2 qualifying fresh run is
`20260726T054917Z__moe_cycle_simulator_phase1_cpu_mock_r2__S1`.

## Scope and evidence

The synthetic fixture exercises request admission, tokenization, allocation,
copy, compute start/completion, pre-top-k router scores, a two-rank collective,
P2P receive, and session telemetry. Four independently declared source clocks
are transformed to global femtoseconds by exact rational affine transforms.
GPU/rank 0 and GPU/rank 1 have separate alignment records.

The adapter emits 10 EventIR records, one RoutingIR record and four
ClockAlignmentIR records. V0 regenerates the records from the copied raw
fixture and compares exact records, semantic row hashes and dataset roots.
The run checksum ledger covers every run file other than the ledger itself.

Sixteen negative and positive tests establish fail-closed behavior for
duplicate JSON keys, JSON floats, unknown or duplicate source clocks,
cross-rank misbinding, duplicate alignment IDs, causal time regression,
evidence tampering, extra/missing/symlink/FIFO filesystem entries, coherent
re-ledger attempts, dependency preflight failure and attempted reuse of an
existing run directory.

R2 records the exact suite command and working directory, the declared
`PYTHONPATH`, distribution versions, distribution `RECORD` hashes and a
content root over 468 installed files. Validator and test argv, environment
override, return code, stdout and stderr are all retained in the nested run
ledger. All dependency and fixture preflight happens before run-directory
creation.

## Claim boundary

This spike validates the frozen schema and correlation semantics against
synthetic CPU/mock data. It does not establish observability in a real vLLM,
Mistral, CUDA, NCCL, P2P or telemetry runtime. The synthetic exact transforms
are not measurements and cannot be used for calibration.

The next legal action after same-hash review is a CPU-only Canonical IR/codec
draft and strict round-trip implementation. GPU work, Mixtral materialization,
formal profiling and calibration remain forbidden.

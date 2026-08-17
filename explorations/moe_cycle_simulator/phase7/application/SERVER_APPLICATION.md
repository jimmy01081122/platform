# Server Application: RTX PRO 6000 Blackwell 96 GB, Mixtral BF16 M0

## Resource intent and future decisions

Accept the staged deployment plan for a future single, bounded
`M0 — BF16_CAPACITY_AND_RUNTIME_QUALIFICATION` session on exactly one NVIDIA
RTX PRO 6000 Blackwell Workstation Edition 96 GB. This is not yet an exact M0
command request. Pre-lease R12 closure, D0, materialization and final M0 each
retain their stated evidence and approval gates. This application does not
request M1–M4, formal profiling, calibration or any performance/model-quality
claim.

```text
APPLICATION_STATUS: R12-R5 CPU REPAIR / SAME-HASH REVIEW REQUIRED
TARGET_PLATFORM: NVIDIA RTX PRO 6000 Blackwell Workstation Edition 96GB
MODEL: mistralai/Mixtral-8x7B-Instruct-v0.1
MODEL_REVISION: eba92302a2861cdc0098cc54bc9f17cb2c47eb61
PRECISION: BF16
RUNTIME: vLLM
PROMOTION_STAGE: M0 ONLY
GPU_WORKLOAD_AUTHORITY: NONE
SERVER_AVAILABILITY: OWNER_CONFIRMED
SSH_ENDPOINT: BLOCKING / FRESH HANDOFF NOT YET PROVIDED
LEASE_START_AND_DEADLINE: BLOCKING / NOT YET FROZEN
```

## Purpose

The RTX PRO 6000 is a conditional discrete BF16 candidate. The overall
application first materializes and checksums the pinned snapshot in its
CPU/I/O-only Gate M. M0 then asks only whether one exact runtime variant can:

1. load all execution weights in BF16 without quantization or persistent CPU
   offload;
2. retain enough device capacity for a one-sequence serving configuration;
3. complete a representative 32,768-token envelope consisting of 28,672 input
   tokens and 4,096 forced output tokens;
4. reproduce the result across three fresh runtime launches; and
5. produce a complete independently replayable audit and terminal evidence set.

This is a capacity/runtime-compatibility gate, not a correctness or throughput
benchmark. Forced output disables EOS only for this synthetic bounded capacity
probe; it must not be reused as a benchmark generation contract.

## Project status

Phase 0 through Phase 6 have completed same-hash review with unanimous
Architecture/System, Model/Benchmark and Trace/Provenance `GO`. The Phase 6 R4
aggregate has SHA-256
`c7218aaa9a49d2916fa8ffe99b42525a795708cbba6b7784b5636195f87ec805`
and binds candidate ledger
`eb920b9bf068677af00dfa99d0b1718d6735387db10261e1bfe0faa38dab4a34`.

That decision authorizes local CPU-only Phase 7 adapter, collector and application
framework work. It grants no GPU authority. R12-R3 is immutable
`MODIFY/MODIFY/GO`; R12-R4 is immutable `GO/MODIFY/GO`. The prospective R12-R5
Phase 7 candidate addresses exactly the two R12-R4 blockers and passes local
CPU-only fail-closed tests. Its final CPU evidence, commit/archive replay and
same-hash review are pending. No Mixtral
materialization, GPU execution or measured Phase 7 evidence exists.

## Exact requested configuration

The following values are fixed:

| Field | Requested value |
|---|---|
| GPU count | 1 |
| Exact SKU | NVIDIA RTX PRO 6000 Blackwell Workstation Edition |
| Minimum device memory | 96,000,000,000 bytes |
| Model | `mistralai/Mixtral-8x7B-Instruct-v0.1` |
| Model revision | `eba92302a2861cdc0098cc54bc9f17cb2c47eb61` |
| Weight precision | BF16 |
| Quantization | none |
| CPU offload | 0 GB |
| Swap-backed execution | 0 GB |
| Tensor/pipeline parallel | 1 / 1 |
| Expert parallel | disabled |
| Maximum model length | 32,768 tokens |
| Maximum batched tokens | 32,768 |
| Maximum sequences | 1 |
| Sampling | deterministic |
| Probe input/output | 28,672 / 4,096 tokens |
| Repetitions | 3 fresh launches |

Transient host RAM used by a normal loader is permitted. Keeping execution weights
on the CPU, paging weights during execution, or using a quantized checkpoint is not.

The following fields cannot be honestly fixed before the provider environment is
disclosed and are blocking:

- SSH endpoint and authentication handoff;
- exact six-hour lease start/deadline and allocation identity;
- host OS/kernel, driver and CUDA identities;
- container image digest;
- vLLM version and git commit;
- attention, fused-MoE and kernel backends;
- exact model-cache path and available host RAM/storage;
- exact materialization and qualification command arrays.

No placeholder may remain when the command is submitted for exact-command approval.

## Requested resources and estimates

These are planning estimates, not measurements:

- one exclusive target GPU;
- one prepaid six-hour allocation with no extension or additional cost;
- a provider-indicated 20–30 minute post-window grace that is noncontractual,
  excluded from all required work and usable only for best-effort release/seal;
- D0/materialization/M0 complete stage envelopes of 300/5,400/14,400 seconds;
- a 900-second release reserve and 600 seconds unallocated slack;
- sufficient host RAM to stage the BF16 checkpoint; the provider must disclose the
  exact available amount;
- estimated working set: 160–220 GB;
- required free working storage: at least 300 GB, including a margin greater than
  the plan's minimum 30 percent;
- expected archive excluding reusable model cache: less than 10 GB;
- additional monetary cost: exactly 0 TWD; the existing prepaid allocation is
  the complete authority boundary.

The run must not begin if the price or maximum spend is unresolved.

## Three independently authorized one-shot gates

The model ledger cannot exist before the first pinned materialization, and the
runtime identity cannot be frozen before D0. Authority is therefore split into:

1. read-only D0 environment disclosure;
2. checksum-bound package deployment and pinned model materialization; and
3. the only GPU-compute gate, M0 qualification.

D0 permits one host-key-pinned SSH connection, the retained standard-library
probe and the exact `nvidia-smi` identity query. It permits no remote write,
download, install, CUDA compute or model access. Materialization permits only
the separately reviewed package install, pinned download, complete file ledger,
tokenizer-derived 28,672-token fixture and terminal seal. It cannot authorize
vLLM model loading or generation. The resulting ledger, fixture and runtime
attestation are inputs to a new exact-command review and owner approval for M0.

## Ordered execution

```text
frozen R12 CPU package and same-hash GO/GO/GO
-> owner-provided fresh SSH endpoint, host key and full lease timestamps
-> exact read-only D0 approval and one-shot disclosure
-> hard stop and immutable D0 evidence
-> freeze materialization command and recursive package ledger
-> separate exact materialization approval
-> checksum-bound atomic package install to /vault
-> one-shot materialization preflight
-> pinned model materialization, complete checksum ledger and prompt fixture
-> hard stop; GPU workload remains unauthorized
-> freeze runtime/backend identity, model ledger and exact M0 commands
-> build and validate the immutable COMPLETE_M0_ELIGIBLE Gate M parent
-> separate owner exact M0 command and maximum-spend approval
-> pause and notify the owner that GPU execution is ready
-> one-shot execution preflight
-> fresh launch/repetition 1
-> fresh launch/repetition 2
-> fresh launch/repetition 3
-> evidence audit
-> M0 decision
-> hard stop
```

Each launch must start from a terminated runtime process, but may reuse the same
read-only pinned model cache. A failed or timed-out session is immutable evidence and
cannot be resumed or retried under this application.

The prospective R12 executor candidate enforces a one-use approval registry whose exact approval
bytes and consumption record are retained inside the evidence root, an exact
recursive application-package ledger, exact target SKU, absolute/symlink-free
model paths, full snapshot closure, BF16 weights and BF16 KV cache,
no quantization, CPU offload or swap, and three independent process identities.
Linux subreaper/process-tree containment covers detached descendants, with a
60-second outer graceful-cleanup allowance and a final 10-second kill boundary.
The runtime attestation contract binds the installed vLLM distribution inventory, source
commit, wheel/build provenance, container digest and SBOM. Independent audit and
canonical exact-set ledgers seal every `PASS`, `FAIL` or `INCOMPLETE` terminal
state as read-only evidence.

R12-R4 additionally places local Gate M decoding in a separate process under a
pre-exec Linux address-space limit and absolute deadline, with bounded diagnostic
logs. Remote `timeout` and Python paths are absolute and content-hashed, then
revalidated before transfer and at remote-controller entry. The M0 parent must
cross-bind the exact model ledger and capacity prompt fixture used by M0. Isolated
Python execution removes `PYTHONPATH` influence, the model ledger must name the
frozen Mixtral ID/revision, and every actually loaded `vllm.*` module/extension
must map to the frozen installed-distribution inventory.

R12-R5 removes the remaining parent-path race by capturing the Gate M parent
once from one bounded `O_NOFOLLOW` descriptor and using that exact payload for
approval hashing, strict parsing, live evidence validation and M0 model/fixture
binding. It also fails closed for any loaded `vllm`/`vllm.*` module without a
single resolvable file-backed origin; originless entries cannot disappear from
the attestation inventory.

Authority bytes are copied before live-package comparison and revalidated at
terminal seal. Terminal status is published only after complete evidence
preflight and staged ledger construction; process-cleanup and materialization-
sealing errors are aggregated into explicit hard-stop evidence rather than
being discarded.

## M0 PASS

M0 passes only if all of the following are proven:

- the exact target SKU and minimum memory pass preflight;
- the full runtime identity and model-file SHA-256 ledger are present;
- all three fresh launches load canonical BF16 without OOM;
- quantization, CPU offload and swap-backed execution remain disabled;
- all three requests record exactly 28,672 input and 4,096 generated tokens;
- each request has the registered forced-length stop, completes within its bound and
  leaves auditable device-memory evidence;
- the session finishes inside the outer timeout and evidence audit is complete.

Passing M0 only makes this exact platform/runtime variant eligible for a separate M1
application. It does not authorize M1.

## Failure and fallback decision

Any preflight mismatch, unresolved identity, checksum failure, OOM, truncation below
the requested forced length, timeout, offload/quantization evidence, missing artifact
or audit failure yields `M0: FAIL` or `M0: INCOMPLETE` and a hard stop.

If the platform cannot pass this exact no-offload BF16 gate:

```text
canonical Mixtral precision: BF16 (unchanged)
RTX PRO 6000 role: BF16 near-capacity/offload research platform
all-resident discrete BF16 baseline: DEFERRED
M1-M4 authority: NONE
```

The failure must not be repaired by silently enabling quantization, CPU offload,
another GPU, a shorter context or a smaller output ceiling.

## Owner decisions required

```text
1. Provide the fresh SSH target only when the full prepaid six-hour lease starts.
2. Provide or confirm the independently authenticated host-key fingerprint and
   immutable provider image digest, or approve one bounded discovery step.
3. Approve or reject the instantiated exact read-only D0 command hash.
4. Approve or reject the exact materialization bundle and command hash after D0.
5. Approve or reject the resulting exact runtime, model ledger and M0 command hash.
6. Reaffirm a maximum additional spend of exactly 0 TWD and no lease extension.
```

Items 1 and 6 and the general target/environment/GPU-use envelope are already
approved. The instantiated D0, Gate M and M0 hashes remain separate one-shot
decisions. M0 is never started automatically when readiness is reached.

Until all required inputs and all six decisions are resolved, no connection or
execution is legal.

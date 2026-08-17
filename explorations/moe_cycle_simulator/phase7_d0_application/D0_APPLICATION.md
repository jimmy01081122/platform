# Phase 7 GPUtw D0 Application

## State

```text
D0 APPLICATION: READY_FOR_OWNER_REVIEW
D0 EXECUTION: NOT AUTHORIZED
SSH: NOT AUTHORIZED
GATE M: NOT AUTHORIZED
M0: NOT AUTHORIZED
GPU AUTHORITY: NONE
COST AUTHORITY: INCOMPLETE
```

This is a fresh exact D0 application overlay. It does not modify the frozen
`explorations/moe_cycle_simulator/phase7/application/` package or any R12-R5,
R12-R4, R12-R3, or GPUtw Provider Adaptation R1 review evidence. The overlay
root is `explorations/moe_cycle_simulator/phase7_d0_application/`.

The owner supplied one already-created GPUtw instance. This session only binds
that identity and the future read-only D0 command. It does not connect to the
instance, query the GPU, run `ssh-keyscan`, download Mixtral, or execute D0.

# Historical Authority

```text
R12-R5 review: GO/GO/GO
R12-R5 review closure: 87c8a866e44387100dadf9a087cf55a37c7cc9e0

GPUtw Provider Adaptation R1 review: GO/GO/GO
GPUtw Provider Adaptation R1 review closure: a94f336ad57707c1eca0be10e3d6da257ff7fb46
```

The frozen R12-R5 candidate and GPUtw Provider Adaptation R1 review remain
historical authority. This overlay is prospective and cannot rewrite either
record.

# Starting Repository Identity

Git fetch from the configured GitHub origin is permitted only for repository
identity synchronization.

Before creating or modifying any D0 application artifact, verify:

```text
git rev-parse origin/codex/c1-quality-contract-v2-20260718
```

resolves exactly to:

```text
a94f336ad57707c1eca0be10e3d6da257ff7fb46
```

Also verify that this commit contains and descends from the immutable R12-R5
review closure `87c8a866e44387100dadf9a087cf55a37c7cc9e0` and that the GPUtw
Provider Adaptation R1 aggregate at this exact branch tip remains unconditional
`GO/GO/GO` with empty blockers.

The recorded pre-modification identity was:

```text
branch: codex/c1-quality-contract-v2-20260718
local HEAD: a94f336ad57707c1eca0be10e3d6da257ff7fb46
local tree: 2ab20af3291a9554ad2dae972b7f32891c29e7bc
remote branch tip: a94f336ad57707c1eca0be10e3d6da257ff7fb46
R12-R5 ancestry: PASS
GPUtw R1 aggregate: GO/GO/GO with blockers=[]
```

If the remote branch tip differs, stop with
`D0_APPLICATION_BASE_IDENTITY_MISMATCH`. Do not rebase, update the expected
base automatically, or instantiate D0 on a newer or different branch tip.

# Owner-Supplied Application Identity

```text
application_id: phase7-gputw-d0-7f9804d4-20260809-r1
provider: GPUtw.ai
instance_id: 7f9804d4-2dd0-4196-8215-9049a1d28942
environment_label: Ubuntu 22.04 + CUDA 12 (provider/UI metadata only)
platform_candidate: RTX PRO 6000 WS 96GB
ssh_target: pod-7f9804d4-2dd0-4196-8215-9049a1d28942@ssh.gputw.ai
```

The environment label does not establish an exact CUDA minor version, driver,
container, Python, vLLM or backend identity. Those remain D0 live fields.

# Project-Specific Known Hosts Artifact

The formal known-hosts artifact is byte-canonical:

```text
canonical_encoding: UTF-8
byte_order_mark: FORBIDDEN
line_ending: LF
entry_count: exactly 1
trailing_newline: exactly 1 LF
leading_whitespace: FORBIDDEN
trailing_whitespace: FORBIDDEN
additional_blank_lines: FORBIDDEN

[ssh.gputw.ai]:2222 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPB++siZEvvX9Lv3hNKGQHAJPHxoqW8qSHy+1hxo3aN/
```

Those exact bytes reproduce SHA-256
`b19c0603ac7cc77fa91ba455734a8783f3502535e0fbe0918813f3d676aa6ec2` and
ED25519 fingerprint
`SHA256:NHS1jkKSV3aKwfQwgC1/wSJVlZkJuuG/AH3cUjAZGwU`.
CRLF conversion, a missing final LF, BOM insertion, hostname hashing, shell
escaping, trailing spaces or an additional blank line are identity mismatches.
On mismatch, stop with `KNOWN_HOSTS_CANONICAL_BYTES_MISMATCH`; do not silently
normalize or regenerate the artifact.

The logical path is `phase7/application/known_hosts.gputw`. The actual path in
this fresh overlay is
`explorations/moe_cycle_simulator/phase7_d0_application/known_hosts.gputw`.
The mapping is explicit because the historical `phase7/application/` directory
is frozen and its ledger must not be changed.

```text
gateway_endpoint: [ssh.gputw.ai]:2222
key_type: ssh-ed25519
trusted_fingerprint: SHA256:NHS1jkKSV3aKwfQwgC1/wSJVlZkJuuG/AH3cUjAZGwU
trust_provenance: OFFICIAL_PROVIDER_CONFIRMATION
gateway_host_key_status: TRUSTED_PINNED
```

# Required Application Identity

# Observation Does Not Equal Configuration Mutation

The approved D0 execution is expected to populate fields explicitly frozen as
`UNOBSERVED`, `D0_LIVE_FIELD_REQUIRED` or `NOT_YET_MEASURED`. Observation by
the exact approved D0 command does not itself modify or invalidate the D0
application identity.

The D0 application freezes the configuration and authority under which
observation is permitted, plus the exact observation command and scope. The
D0 result supplies evidence for previously unresolved live fields.

Actual GPU model/count, VRAM, driver/CUDA identity, container/environment,
Python/timeout identity, vLLM/backend, `/vault` identity/capacity,
workspace/storage and OS/kernel identity are D0 evidence, not application
mutations. A new D0 identity is required for instance replacement or restart
requiring revalidation, instance/gateway/principal change, trusted host-key or
known-hosts change, template/image or runtime authority change, owner cost
authority change, D0 timeout/SSH argv/payload change, observation-scope change,
executable-binding change or authorization-boundary change.

```text
D0 observation of a field frozen as UNOBSERVED
  != application mutation

change to an approved input/configuration/authority/command
  == new application identity required
```

The execution result must preserve both `application_identity` and
`result_evidence_identity`, binding result evidence to this exact application.

# Current Price and Cost Governance

```text
observed_compute_price: 34.36 NTD/hour
owner_execution_budget: 21600 seconds
owner_total_cost_cap: 300 NTD
nominal_six_hour_compute_only_exposure: 206.16 NTD
```

The 21600-second value is `OWNER_IMPOSED_EXECUTION_ENVELOPE`, not a provider
lease. The nominal compute-only exposure is below the total cap, but the
remaining difference is not discretionary provider spend. Compute, storage,
port and total cost controls remain separate. Since exact compute, storage and
port sub-caps were not separately supplied, this package freezes zero-spend
prohibitions for those categories until separate numeric sub-cap approval.
Additional billable ports remain `ZERO_SPEND_REQUIRED` and
`FORBIDDEN_UNLESS_EXPLICITLY_APPROVED`.

# Cost Authority Promotion Rule

Unresolved cost sub-caps do not prevent construction, validation or freezing of
the D0 application package.

Therefore the application may reach:

```text
D0_APPLICATION:
  READY_FOR_OWNER_REVIEW
```

while one or more cost sub-caps remain unresolved. However:

```text
D0_EXECUTION_APPROVAL_ELIGIBLE:
  false
```

until every category of spend that can be incurred by the exact approved D0
execution has either an explicit owner-approved numeric cap or an explicit
zero-spend prohibition. The existence of `owner_total_cost_cap: 300 NTD` is not
permission to allocate unused difference among storage, ports or any other
service:

```text
300 NTD total cap
  != discretionary unallocated provider spend
```

If the exact D0 execution would incur a category for which neither an approved
numeric cap nor zero-spend prohibition exists, the package remains
`READY_FOR_OWNER_REVIEW` but reports `COST_AUTHORITY_INCOMPLETE` and
`D0_EXECUTION_APPROVAL_ELIGIBLE = false`. `READY_FOR_OWNER_REVIEW` is never
executable cost authority.

# Time Envelope and D0 Scope

```text
D0:           300 seconds
Gate M:       5400 seconds (future reservation only)
M0:           14400 seconds (future reservation only)
release:       900 seconds
bounded work: 21000 seconds
slack:         600 seconds
owner cap:    21600 seconds
```

Only the D0 read-only disclosure contract is represented here. It may observe
provider/instance identity, GPU identity and memory, driver/CUDA/container
identity, Python/timeout identity, vLLM/backend presence, `/vault` and
workspace identity/capacity, and basic OS/kernel fields. It may not write,
install, download, load Mixtral, run inference, benchmark CUDA, execute Gate M
or execute M0.

# Exact Command Binding

The future command is frozen in `ssh_argv.json`; its canonical argv SHA-256 is
`ed6b316dbab3018e2b23a9dcfbae7918d82097a4cd0adf49405419957613a90a`. It uses
`/usr/bin/ssh`, `/dev/null`-isolated config, strict host-key checking, the
exact project known-hosts file, no forwarding/proxy/interactive TTY and the
exact principal/endpoint. It is not executed in this session.

The remote payload is the exact byte sequence of the frozen Phase 7
standard-library `environment_probe.py` source at base commit
`a94f336ad57707c1eca0be10e3d6da257ff7fb46`, transported over SSH stdin. Its
SHA-256 is
`87b7da0f6acddcfe6b5bfe33c4e90ac49a57f3dea3701b980194a215a3d2bbf1`; the
command binding SHA-256 is
`ac769a67bffd217471930ab6cc62702990654f058ce4924938cbafab001c7184`.

The existing Phase 7 disclosure driver, process-tree containment, bounded
output, deadline and atomic terminal-sealing machinery are hash-bound in
`application_manifest.json`, not replaced by a weaker parallel path.

# Owner Approval Boundary

`approval_request.json` is a pending request, not an approval. Owner approval
would authorize exactly one exact D0 execution over this application identity,
with no retry, resume, instance replacement/restart, runtime installation,
Gate M, model download, M0 or GPU inference. The package is not eligible for
D0 execution while `COST_AUTHORITY_INCOMPLETE` remains.

# Validation and Stop Boundary

Run only the local CPU-only validator:

```text
python3 -B explorations/moe_cycle_simulator/phase7_d0_application/validate_d0_application.py
```

It performs no network access, SSH, GPU query, model download or remote action.
It checks repository ancestry, the frozen branch-tip review aggregate,
known-hosts bytes/fingerprint, command and payload hashes, cost/time arithmetic,
unresolved fields, source bindings, private-credential absence, schema shape
and both checksum ledgers.

The package status is:

```text
D0 APPLICATION: READY_FOR_OWNER_REVIEW
D0 EXECUTION: NOT AUTHORIZED
SSH: NOT AUTHORIZED
GATE M: NOT AUTHORIZED
M0: NOT AUTHORIZED
GPU AUTHORITY: NONE
```

No live evidence is created or implied. Remaining live fields include exact
CUDA minor version, driver, measured GPU/VRAM, container digest,
template/image identity, Python/timeout paths and hashes, vLLM/backend,
`/vault` mount/free space, model snapshot, Mixtral/BF16 capacity, performance
and calibration.

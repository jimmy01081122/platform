# Phase 7 New-Session Reconciliation

```text
session_id: 20260813T160658Z__LUNA-MAX-NEW-SESSION-AFTER-SERV-P0-25
campaign_id: 20260813T160658Z__LUNA-MAX-SPECIAL-MECHANISM-TRACE-CLOSURE-V2-NEW-SESSION
owner_endpoint: pod-9ebe2f5c-81af-44c1-8fb0-a06bfd2d4f9c@ssh.gputw.ai:2222
old_campaign_id: 20260813T130017Z__LUNA-MAX-SPECIAL-MECHANISM-TRACE-CLOSURE-V1
old_server: CLOSED
remote_action_before_new_session_preflight: NONE
```

## NS0 read-only reconciliation

- `SERV-P0-25` base 1K: `PASS`, `DO_NOT_RERUN`.
  - raw: `runs/20260811T175500Z__phase7_fit_anchor_backup/raw/runs/20260811T195121Z__SERV-P0-25-SHORT-C8-NATURAL-V1/`
  - requests: 1,000; arrivals: 1,000; warm-up: 8; telemetry: 127
  - status SHA-256: `9ff7955173c2c0b1d3521699f2fe031233123a4c500fa545d85bd03a9a499d43`
  - manifest SHA-256: `f9305de2691eb8fcc7f2678b5f39dbb9c403bf122a3275efdfaccf8cede7ea02`
  - result SHA-256: `7c37baa6dd55c8f3030646b370ace4378d74d78fb9ea75ea08128f6a5f4787de`
- `SERV-P0-25` EXT10K: `INCOMPLETE`, `NO_VERIFIED_RAW`.
  - sidecar: `runs/20260811T175500Z__phase7_fit_anchor_backup/preliminary_serving_p0_25_remote_environment_loss_v1.json`
  - `is_completion_evidence=false`; `is_raw_backup=false`; observed checkpoint stopped at request 4,946.
- `OFF-E-PR3-CAP-025`: `NOT_RUN`, `UNVERIFIED`; distinct from `SERV-P0-25`.
- Old session guards: historical `MECH-G0=PASS`, `KV-G0=PASS`, `OS-SWAP-G0=PASS`,
  `UM-G0=NEGATIVE_EVIDENCE`; old attempt checksum recheck: 19/19 `OK`.
- Old ledger SHA-256:
  `b5aaa5e5e887809e4c474cf2c6f825bdf0d2293e41316ab5f140516323aab704`.
- Old adoption manifest remains `PENDING_AMENDMENT_GATE_AUDIT`; existing evidence is retained
  as historical input only. Adoption statuses requiring supplements remain unchanged:
  `ADOPT-CMP-M3`, `ADOPT-CMP-A-ISOLATED`, `ADOPT-POL0-POL5`, `ADOPT-LM11-COARSE`,
  `ADOPT-EXPERT-CATALOG`; `ADOPT-XFER-E-Q-O`, `ADOPT-FIT-ANCHORS`, and `ADOPT-SERVING`
  remain pending audit.

## New-session gates and queue

- NS2 pending: exact host key, hostname, GPU SKU/UUID/VRAM/driver/CUDA, `/vault` identity and
  free space, model/revision/file-ledger identity, runtime/tools, foreign GPU/serving process
  check, and writable new namespace check.
- NS3 pending: new attempt ID for `MECH-G0`, `KV-G0`, `OS-SWAP-G0`, and `UM-G0`; old-session
  PASS is not used as proof of this host/session.
- Next GPU unit after NS2 and NS3 direct gates: `OFF-E-RT0`, then `SWAP-K0` and `OFF-W0` as
  independently named capability canaries.
- `OFF-E-PR3-CAP-025` remains required and cannot be skipped because `SERV-P0-25` passed.
- No filler workload, no gate bypass, no rerun of the valid `SERV-P0-25` base raw, and no
  claim of runtime-native expert offload, runtime-native KV swap performance, or stronger UM
  absence than the observed telemetry permits.

## Provenance

- Local git HEAD at reconciliation: `e804b1633a376f63d57aeba60e7fd15068181ea4`.
- Worktree had pre-existing user/agent changes; no existing file was modified for this artifact.
- New session raw and successor ledger must be created under the new campaign namespace; the
  historical campaign ledger and raw evidence are immutable.

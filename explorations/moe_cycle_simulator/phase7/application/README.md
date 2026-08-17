# Phase 7 RTX PRO 6000 BF16 M0 Application Pack

This directory is a fail-closed application and remote-execution template. The
owner has approved one prepaid six-hour environment envelope with no extension
or additional cost. Live access still requires a fresh, exact one-shot D0
approval. D0 authorizes only read-only environment disclosure; it does not
authorize remote writes, package installation, model download or a GPU workload.

The first requested platform is exactly one NVIDIA RTX PRO 6000 Blackwell
Workstation Edition with nominal 96 GB device memory. The requested workload is
only `M0 — BF16_CAPACITY_AND_RUNTIME_QUALIFICATION` for
`mistralai/Mixtral-8x7B-Instruct-v0.1` at the repository revision already frozen
by the project model profile. Quantization, persistent CPU offload and swap-backed
execution are forbidden. M1 through M4 are outside this application.

## Current state

- Phase 0 through Phase 6 have unanimous same-hash `GO` evidence.
- The Phase 6 R4 aggregate authorizes only local CPU adapter, collector and
  profiling-application framework work.
- The local Phase 7 adapter framework and executable M0 control chain are
  implemented and CPU-tested; they have no model, GPU or measured evidence.
- R12-R4 closed the five R12-R3 review blockers: Gate M local
  decode runs in a separate Linux process under a pre-exec `RLIMIT_AS`, its
  stdout/stderr are independently bounded, and remote `timeout` and Python are
  absolute, content-hashed identities revalidated before and immediately after
  remote entry. Gate M parent model-ledger and prompt-fixture hashes must equal
  the M0 runtime hashes. The model ledger is checked against the exact frozen
  Mixtral ID/revision, and isolated Python execution attests every loaded
  `vllm.*` module or extension against the installed-distribution ledger.
- Prospective R12-R5 closes the two R12-R4 adversarial findings. M0 now captures
  the Gate M parent once through one bounded, non-symlink regular-file
  descriptor, then uses those identical bytes for the approval hash, strict
  parse, live validation and model/fixture binding. No pathname reread is used.
  Loaded-module attestation now rejects, rather than skips, every matching
  `vllm` or `vllm.*` entry without one resolvable file-backed origin.
- The prospective executor retains the exact approval bytes, one-shot
  consumption record and recursive application ledger inside every terminal
  evidence root before package comparison, then revalidates them at terminal
  sealing. `PASS`, `FAIL` and `INCOMPLETE` are all sealed.
- R12 uses stage envelopes of 300, 5,400 and 14,400 seconds plus a 900-second
  release reserve. External kill grace is inside, not added after, each stage.
- D0 uses Linux process-tree containment, executes the retained approved probe
  and `known_hosts` bytes, and publishes its terminal marker only through the
  independently replayable exact-set sealer.
- Vault identity is derived from `/proc/self/mountinfo`, boot and device
  provenance and is revalidated before materialization and M0.
- Linux subreaper/process-tree containment covers process-group and `setsid()`
  escape attempts. The outer driver reserves 60 seconds for qualification and
  GPU cleanup before its final 10-second kill boundary.
- The runtime adapter passes and reads back exact BF16 KV-cache configuration,
  while the build attestation binds the vLLM distribution inventory, source
  commit, wheel/build provenance, container digest and SBOM.
- Terminal status is published only after complete evidence preflight and staged
  ledger construction. Cleanup and materialization-sealing errors are retained
  explicitly and cannot be swallowed into an apparent terminal PASS.
- GPU workload authority remains `NONE`; D0 has not run because the server is
  available but the required fresh SSH handoff and immutable R12 package are
  still pending.
- The SSH endpoint, provider price, image digest, vLLM commit and backend identities
  are unresolved blocking fields.

## Files

- `SERVER_APPLICATION.md`: owner/provider-facing application and decision boundary.
- `application_manifest.json`: immutable scope and current authorization state.
- `environment_manifest.template.json`: provider and hardware disclosure template.
- `environment_disclosure_plan.template.json`,
  `environment_disclosure_approval.template.json` and
  `disclose_environment.template.sh`: one-shot read-only D0 gate.
- `runtime_variant.template.json`: complete execution-identity template.
- `m0_plan.json`: ordered qualification cells and hard-stop rules.
- `m0_execution_contract.json`: immutable capacity-envelope and authority contract.
- `materialization_plan.template.json` and
  `materialization_approval.template.json`: the first one-shot approval gate,
  which may materialize the pinned model but cannot run a GPU workload.
- `approval.template.json`: the second, separately issued exact M0 execution
  approval.
- `executor/`: strict authority retention, materializer, prompt builder,
  preflight, process-tree containment, three-process supervisor, versioned vLLM
  adapter/attestation, independent audit and terminal evidence sealer.
- `executor/d0_finalize.py` and `executor/storage_identity.py`: D0 exact-set
  terminal replay and cross-stage Vault identity enforcement.
- `executor/deployment_bundle.py`: canonical bounded application bundle,
  staged no-replace installation, immutable receipt and exact-set replay. It
  performs no SSH, network, model or GPU action.
- `deployment_plan.template.json`, `deployment_approval.external.template.json`,
  `deploy_gate_m.template.sh`, `executor/gate_m_bootstrap.py` and
  `executor/deployment_controller.py`: one host-key-pinned SSH Gate M transport,
  bounded remote export and exact local semantic replay.
- `executor/gate_m_local_replay.py`: separate local decoder/replayer with a
  runtime-enforced address-space ceiling, absolute deadline, bounded logs and
  immutable execution evidence.
- `gate_m_parent_evidence.template.json` and `executor/gate_m_parent.py`:
  fail-closed M0 parent generation/validation; only a same-hash-reviewed,
  locally replayed `COMPLETE_M0_ELIGIBLE` Gate M result can satisfy it.
- `schemas/`: M0 evidence-interface schemas.
- `validate_application.py`: CPU-only draft or execution-readiness validator.
- `preflight_m0.template.sh`: remote hardware/software preflight; locked by default.
- `run_m0.template.sh`: fresh-session command driver; locked by default.

## Legal workflow

1. Review this draft without connecting to a server.
2. On a fresh owner SSH handoff, freeze the D0 endpoint, host key, lease window,
   package ledger and exact read-only command.
3. Execute D0 once and hard-stop. No remote write, install, download, model load
   or CUDA workload is permitted.
4. Freeze the disclosed environment, host key, package ledger and exact
   materialization command.
5. Obtain a one-shot materialization approval. It creates the complete model
   ledger and prompt fixture, then hard-stops without a GPU workload.
6. Freeze the resulting model ledger, exact vLLM variant and M0 command.
7. Generate the immutable Gate M parent and build a fresh M0 package without
   mutating the deployed materialization package.
8. Pause and notify the owner that GPU execution is ready.
9. Obtain a new, one-shot exact M0 execution approval.
10. Execute three separate fresh vLLM processes, audit, seal and hard-stop.
11. Preserve any failed or incomplete session. Do not resume or retry it.

The nominal paid boundary for every required step is exactly 21,600 seconds.
The provider-indicated extra 20–30 minutes is not guaranteed, has zero formal
work or PASS credit, and may be used only for best-effort termination, release
and transfer verification of evidence already completed inside the paid bound.

Draft validation is CPU-only and performs no GPU or network operation:

```text
python3 explorations/moe_cycle_simulator/phase7/application/validate_application.py \
  --mode draft \
  --application-dir explorations/moe_cycle_simulator/phase7/application
```

Execution-ready validation is intentionally expected to fail until every blocking
field and both owner approvals are resolved:

```text
python3 explorations/moe_cycle_simulator/phase7/application/validate_application.py \
  --mode execution-ready \
  --application-dir explorations/moe_cycle_simulator/phase7/application
```

The intermediate materialization gate has its own validator:

```text
python3 explorations/moe_cycle_simulator/phase7/application/validate_application.py \
  --mode materialization-ready \
  --application-dir explorations/moe_cycle_simulator/phase7/application
```

The one-SSH Gate M package has an additional external approval validator:

```text
python3 explorations/moe_cycle_simulator/phase7/application/validate_application.py \
  --mode gate-m-ready \
  --application-dir explorations/moe_cycle_simulator/phase7/application \
  --external-approval /absolute/path/to/approved-gate-m.json
```

The shell templates are deliberately committed without executable permission.
Invoking them with `bash` still requires the exact approval manifest and the explicit
unlock phrase. No credential belongs in this repository.

# Phase 7 D0-R3 CPU-only repair overlay

This directory is a prospective repair overlay for the D0-R1 application. It
does not modify the frozen R12-R5, GPUtw Provider Adaptation R1, D0-R1, or
the immutable D0-R2 candidate artifacts.

The overlay addresses the D0-R3 machinery defects identified by the independent
review of D0-R2:
pre-GPU guide:

- one controller consumes one captured overlay and one-shot approval;
- command, executable, credential-selector and host-key provenance are bound
  by an explicit canonical preimage;
- the result and immutable terminal ledger bind application, review, approval,
  session, probe and retained-input identities;
- probe output is checked against a complete strict schema;
- bounded process execution and descendant cleanup cover fast parent exit;
- lease freshness and minimum remaining D0 reserve are checked at execution;
- the approval evidence root is bound before one-shot consumption;
- the selected SSH agent is queried and must contain the approved public-key
  fingerprint;
- host-key provenance is backed by a hash-bound confirmation artifact;
- discovery-only output is explicitly non-promotable when the container digest
  is not observed.

The checked-in package is intentionally not live-execution eligible. Owner
confirmation of the exact environment, fresh lease timestamps, non-secret
client-key selector, authenticated host-key source and any immutable
container identity remains an external input. No SSH, network, model or GPU
operation is performed by package validation or tests.

Validate the package locally:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -B \
  explorations/moe_cycle_simulator/phase7_d0_r3/validate_d0_r3.py
```

The controller has no default execution path. A live invocation requires a
complete owner approval, a fresh absolute evidence root and an explicit
second-factor unlock; it must never be replaced by a manually copied SSH
command.

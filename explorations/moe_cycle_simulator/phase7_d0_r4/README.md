# Phase 7 D0-R4 CPU-only repair overlay

This directory is a prospective repair overlay for the D0-R1 application. It
does not modify the frozen R12-R5, GPUtw Provider Adaptation R1, D0-R1, or
the immutable D0-R2 candidate artifacts.

The overlay addresses the D0-R3 machinery defects identified by its independent
same-hash review:

- one controller consumes one captured overlay and one-shot approval;
- command, executable, credential-selector and host-key provenance are bound
  by an explicit canonical preimage;
- the result and immutable terminal ledger bind application, review, approval,
  session, probe and retained-input identities;
- probe output is checked against a complete strict schema;
- bounded process execution and descendant cleanup cover fast parent exit;
- lease freshness and minimum remaining D0 reserve are checked at execution;
- the approval evidence root is bound before one-shot consumption;
- a detached GO/GO/GO review authority binds the exact candidate commit, tree,
  application identities and ledgers without self-referencing the candidate;
- the selected SSH-agent key is canonicalized, retained and forced through an
  exact public `IdentityFile` while private key material stays external;
- known-hosts and the SSH executable are captured before one-shot consumption,
  and the live argv consumes only those captured copies;
- the 28,800-second fully prepaid window preserves the 21,000-second stage sum
  and treats the remaining 7,800 seconds only as transition/failure slack;
- the 300-second D0 envelope reserves 280 seconds for transport/cleanup and 20
  seconds for terminal sealing, while the remote probe has a real 120-second
  watchdog;
- Linux child-subreaper containment covers a leader that exits immediately
  after creating a detached `setsid` descendant;
- the package root is an exact set and terminal verification rederives the
  semantic result identity rather than trusting only file hashes;
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
  explorations/moe_cycle_simulator/phase7_d0_r4/validate_d0_r4.py
```

The controller has no default execution path. A live invocation requires a
complete owner approval, a detached same-hash review authority, a fresh
absolute evidence root and an explicit
second-factor unlock; it must never be replaced by a manually copied SSH
command.

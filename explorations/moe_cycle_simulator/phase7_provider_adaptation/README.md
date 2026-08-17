# Phase 7 GPUtw.ai Provider Adaptation R1

This directory is a prospective CPU-only provider-governance overlay layered
after the immutable Phase 7 R12-R5 candidate. It adapts the future D0, Gate M
and M0 contracts to the owner-supplied GPUtw.ai operating model. It does not
modify the simulator, create an instance, connect by SSH, query a GPU, download
Mixtral or authorize execution.

The overlay is intentionally separate from
`explorations/moe_cycle_simulator/phase7/`: the R12-R5 exact-set ledger includes
that directory and its reviewed status files. Adding provider facts there would
invalidate the frozen candidate. The overlay is the prospective revision to be
reviewed before it is integrated into a future execution package.

The `gputw_r1/checksums.sha256` file is an exact ledger for the machine-readable
package. The validator also provides a byte-level check for a future
`known_hosts.gputw` artifact; its caller must supply a digest and trusted
provider provenance, and no discovery command is performed.

## Contract

`gputw_r1/provider_adaptation.json` records:

- GPUtw.ai dedicated-container, prepaid/metered billing that accrues only while
  the instance is `RUNNING`;
- `STOP` and `DELETE` as billing-termination actions;
- `21600` seconds as an owner-imposed hard cap, not a provider lease;
- bounded D0, Gate M, M0 and release envelopes of 300, 5400, 14400 and 900
  seconds, with 600 seconds of owner-envelope slack;
- separate provider, instance, SSH gateway, client-auth and server-auth
  identities;
- the gateway `[ssh.gputw.ai]:2222` and `pod-<instance-id>` principal;
- owner-confirmed public-key registration with no repository credential
  provisioning;
- unresolved server host-key provenance and fail-closed D0 SSH authority;
- a future checksum-bound project-specific `phase7/application/known_hosts.gputw`
  artifact, never the user's personal `known_hosts` file;
- `/vault` as prospective persistent storage and container-local storage as
  non-persistent unless D0 proves otherwise;
- unresolved `TEMPLATE` and `CUSTOM_IMAGE` runtime identity branches;
- owner compute, storage, port and total cost caps, with billable ports forbidden
  by default;
- canonical Mixtral BF16/vLLM/RTX PRO 6000 M0 with no automatic FP8,
  quantization or CPU-offload fallback.

## CPU validation

```text
python3 explorations/moe_cycle_simulator/phase7_provider_adaptation/gputw_r1/validate_provider_adaptation.py
python3 -m pytest -q tests/test_phase7_gputw_provider_adaptation.py
```

The validator is network-free and must report `gpu_authority: NONE`, unresolved
host-key status and non-authorized D0/Gate M/M0. The review request remains
`PENDING`; passing these checks is contract implementation evidence, not a
provider review or execution authority.

# Phase 7 D0-S2 CPU-only runtime identity discovery

D0-S2 is a fresh, additive implementation following the immutable D0-S1
`INCOMPLETE_NOT_READY` discovery.  It does not retry or resume the stopped
`pod-a92587c7-d439-42b1-b305-0843acb46d38` instance and does not modify D0-R2,
D0-R3, D0-R4-C1, or their evidence.

The package closes the *probe coverage* gap exposed by S1.  It observes, when
available, the CUDA runtime, query-only PyTorch CUDA build/availability,
vLLM/Transformers/tokenizers/Hugging Face Hub package versions, installed
distribution inventory hashes, the Python executable hash, GPU identity and
Vault capacity.  Container image/digest and provider metadata are recorded as
`UNAVAILABLE` when the provider does not expose them; the probe never invents
those values.  Time fields remain observational and are not classification
blockers under the current owner decision.

The probe is read-only.  It may run `nvidia-smi` and `nvcc --version`, import
PyTorch for `torch.cuda.is_available()`/`device_count()` queries, and read
package metadata and executable files.  It does not allocate tensors, launch a
kernel, run a benchmark, install a package, access a model, use the network,
write a remote path, or open SSH.

Validate the exact package set locally:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -B \
  explorations/moe_cycle_simulator/phase7_d0_s2/validate_d0_s2.py
```

Generate a local CPU evidence run (not provider evidence):

```text
PYTHONDONTWRITEBYTECODE=1 python3 -B \
  explorations/moe_cycle_simulator/phase7_d0_s2/run_cpu_probe.py \
  --output runs/20260809T000000Z__moe_cycle_simulator_phase7_d0_s2_cpu_local__S2
```

The local run is useful for exercising schema, classification, ledger and
replay machinery.  A local result cannot establish RTX PRO 6000 readiness.  A
future provider result must use a fresh application/session/evidence identity,
remain discovery-only, and still leave D0, Gate M, M0 and GPU authority as
`NOT_AUTHORIZED`/`NONE` until separately reviewed and approved.

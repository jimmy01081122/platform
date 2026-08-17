# NS2 Read-Only Preflight

```text
session_id: 20260813T160658Z__LUNA-MAX-NEW-SESSION-AFTER-SERV-P0-25
campaign_id: 20260813T160658Z__LUNA-MAX-SPECIAL-MECHANISM-TRACE-CLOSURE-V2-NEW-SESSION
endpoint: pod-9ebe2f5c-81af-44c1-8fb0-a06bfd2d4f9c@ssh.gputw.ai:2222
endpoint_host_key: ED25519 SHA256:NHS1jkKSV3aKwfQwgC1/wSJVlZkJuuG/AH3cUjAZGwU
known_hosts_capture_sha256: 46e0a094d401056a07a9124581a28979413025ec48b27a13abed2e0b6fe54da6
remote_observed_at_utc: 2026-08-13T16:09:45Z
hostname: gpu-9ebe2f5c-81af-44c1-8fb0-a06bfd2d4f9c
allocation_identity: NFS user-0ea74f55-945e-4893-9de2-c08ec4424e2f; host allocation suffix matches pod endpoint
```

## Identity and storage

- GPU: one `NVIDIA RTX PRO 6000 Blackwell Workstation Edition`; UUID
  `GPU-177cc8e4-ff4d-a649-ac29-a3807141b521`.
- GPU memory: 97,887 MiB total; 2 MiB used at discovery; no compute applications.
- Driver: `595.71.05`; PyTorch: `2.11.0+cu130`; vLLM: `0.23.0`; Python: `3.12.13`;
  CUDA is available. Optional `nvcc`/`nsys` were not present in PATH; this is not a guard
  failure because the selected guard runner does not require them.
- `/vault`: NFSv4.2 source
  `192.168.7.2:/vault/user-0ea74f55-945e-4893-9de2-c08ec4424e2f`, `rw`, about 1.9T free.
- New parent namespace `/vault/flow/moe_simulator_phase7/special_mechanism_raw` exists and
  is readable, searchable, and writable. No new session directory was created during NS2.

## Canonical model identity

- Path:
  `/vault/flow/moe_simulator_phase7/models/mistralai__Mixtral-8x7B-Instruct-v0.1__eba92302__bf16_safetensors`
- The exact file names and byte sizes match the preserved local model identity ledger:
  19 safetensor shards, 93,405,713,504 safetensor bytes, plus the same config/tokenizer/index
  file set.
- `config.json` resolves to Mixtral, 32 hidden layers, 8 local experts, top-2 routing,
  hidden size 4096, intermediate size 14336, BF16, max position 32768.
- Preserved local identity has `config_sha256=9d56d04b36d0fd12ff54ae4c5bac769cc176e254e64ff71144614b6318b40793`
  and revision `eba92302a2861cdc0098cc54bc9f17cb2c47eb61`; the new host file set and config
  shape/size ledger match. A full weight checksum scan was intentionally not run in the
  sensitive preflight window; no unsupported claim of full-shard hash verification is made.

## Serving-interference gate

- `nvidia-smi --query-compute-apps` returned no rows.
- Process table contained only container/sshd/shell/preflight processes; no vLLM, serving,
  GPU campaign runner, or Phase 7 measurement process was present.
- No signal, profiler attach, model copy, checksum scan, configuration change, priority change,
  or GPU workload was performed during NS2.

## NS2 decision

```text
NS2_READ_ONLY_PREFLIGHT: PASS
CANONICAL_GPU_DOMAIN: PASS
MODEL_FILESET_AND_CONFIG_LEDGER: PASS_WITH_FULL_WEIGHT_HASH_SCAN_DEFERRED
SERVING_INTERFERENCE: CLEAR
NEW_NAMESPACE_PARENT_WRITABLE: PASS
GPU_DISPATCH_AUTHORIZED_TO_NS3_ONLY: YES
```

The full-shard checksum limitation is carried forward as a provenance note. It does not alter
the canonical model identity because the preserved file-set/config ledger matches exactly; it
must not be silently upgraded to a full-weight hash claim.

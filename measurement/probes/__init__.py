"""GPU measurement probes (TRACK_GPU_PREP).

New probes written CPU-first: every probe must run its full code path against a
mock backend with no GPU, no torch, no SSH and no serving process, so that the
GPU window is spent executing rather than debugging argv, output format or error
handling.

Claim boundary (TRACK_GPU_PREP §7): a passing CPU smoke test proves that argv
parsing, output serialization and error handling are correct. It proves NOTHING
about GPU performance or whether the measurement yields meaningful numbers on a
real device. Every mock-backend result is stamped
``evidence = "cpu_smoke_test_not_measurement"`` for exactly this reason.
"""

from __future__ import annotations

SCHEMA_LONGCTX_KV = "gpu-longctx-kv-result-v1"
SCHEMA_DISPATCH_INSERVING = "gpu-dispatch-inserving-result-v1"

# Fields whose definition depends on STAGE_A2's IR evaluation-point schema.
# Hard rule 5 (TRACK_GPU_PREP): these MUST NOT be guessed here. They are emitted
# as explicit PENDING_A2 markers until A2 completes and PREP-2 fills them in.
PENDING_A2_SENTINEL = "PENDING_A2"

__all__ = [
    "SCHEMA_LONGCTX_KV",
    "SCHEMA_DISPATCH_INSERVING",
    "PENDING_A2_SENTINEL",
]

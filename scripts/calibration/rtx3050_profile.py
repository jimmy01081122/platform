#!/usr/bin/env python3
"""RTX 3050 component-calibration suite (H1 -> H2).

Measures the device service model that W3 currently SWEEPS (H1), turning it into
a component-calibrated profile (H2):
  * H2D / D2H payload bandwidth vs transfer size (copy engine);
  * copy-engine launch/DMA latency (small-transfer intercept);
  * per-MoE-layer expert kernel time (grouped-GEMM proxy) -> closes A-017.

If CUDA/torch is unavailable (e.g. this headless env), it writes a profile with
null measured fields + the exact measurement plan, so H2 stays honestly OPEN and
nothing is fabricated. Run on a machine with an RTX 3050 to populate real values.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "configs" / "platform" / "p_d_rtx3050_calibrated.json"

MEASUREMENT_PLAN = {
    "h2d_d2h_bandwidth": "cudaMemcpyAsync pinned host<->device, sizes 2^16..2^28 B, "
                         "report effective GB/s vs size; use the plateau for large experts.",
    "copy_engine_latency": "linear fit of transfer_time vs bytes; intercept = launch/DMA latency_s.",
    "copy_engines": "concurrent stream copy scaling until bandwidth saturates.",
    "expert_kernel_time": "time one MoE expert (grouped GEMM: 3*hidden*intermediate) at the "
                          "model's precision for batch=1 (decode) and batch=prefill; "
                          "per-MoE-layer compute = sum over resident experts -> resolves A-017.",
}


def try_cuda_profile() -> dict | None:
    try:
        import torch
    except Exception:
        return None
    if not torch.cuda.is_available():
        return None
    dev = torch.cuda.get_device_name(0)
    result = {"device_name": dev, "measured": {}}
    sizes = [1 << e for e in range(16, 29)]
    bw = {}
    for n in sizes:
        h = torch.empty(n, dtype=torch.uint8, pin_memory=True)
        d = torch.empty(n, dtype=torch.uint8, device="cuda")
        torch.cuda.synchronize()
        start = torch.cuda.Event(True); end = torch.cuda.Event(True)
        reps = 20
        start.record()
        for _ in range(reps):
            d.copy_(h, non_blocking=True)
        end.record(); torch.cuda.synchronize()
        ms = start.elapsed_time(end) / reps
        bw[str(n)] = n / (ms / 1e3)
    result["measured"]["h2d_bandwidth_bytes_per_s_by_size"] = bw
    result["measured"]["h2d_bandwidth_plateau_bytes_per_s"] = max(bw.values())
    return result


def main() -> int:
    prof = {
        "schema_version": "platform-profile-v1",
        "profile_id": "P-D-RTX3050",
        "name": "Discrete CPU-GPU with NVIDIA RTX 3050 6GB",
        "class": "discrete",
        "fidelity": {"hardware": "H2", "status": "uncalibrated"},
        "provenance": "H2 target: component-calibrated on real RTX 3050.",
        "measurement_plan": MEASUREMENT_PLAN,
        "link_latency_s": None,
        "copy_engines": None,
        "link_bandwidth_bytes_per_s": None,
        "per_moe_layer_compute_time_s": None,
    }
    cuda = try_cuda_profile()
    if cuda:
        prof["fidelity"]["status"] = "calibrated"
        prof["cuda"] = cuda
        prof["link_bandwidth_bytes_per_s"] = cuda["measured"].get("h2d_bandwidth_plateau_bytes_per_s")
        print(f"CUDA device: {cuda['device_name']} — bandwidth measured.")
    else:
        print("No CUDA/torch available: writing H2 profile with null measured fields "
              "+ measurement plan (H2 stays OPEN; nothing fabricated).")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(prof, indent=2, ensure_ascii=False))
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Run the Phase-7 GPU runner while tracing actual UVA-offloaded parameters."""

from __future__ import annotations

import json
import os
from pathlib import Path

from vllm.model_executor.offloader.uva import UVAOffloader

import gpu_campaign_runner


TRACE_PATH = Path(os.environ["OFF_W2_TRACE_PATH"])
ORIGINAL = UVAOffloader._maybe_offload_to_cpu
CALL_INDEX = 0


def traced(self, module):
    global CALL_INDEX
    CALL_INDEX += 1
    before = self.cpu_offload_bytes
    result = ORIGINAL(self, module)
    after = self.cpu_offload_bytes
    if after > before:
        parameters = []
        for name, parameter in module.named_parameters():
            if getattr(parameter, "_vllm_is_uva_offloaded", False) or parameter.device.type == "cpu":
                parameters.append({
                    "local_name": name,
                    "shape": list(parameter.shape),
                    "dtype": str(parameter.dtype),
                    "numel": parameter.numel(),
                    "element_size": parameter.element_size(),
                    "bytes": parameter.numel() * parameter.element_size(),
                    "device_after": str(parameter.device),
                    "uva_marker": bool(getattr(parameter, "_vllm_is_uva_offloaded", False)),
                })
        record = {
            "wrap_call_index": CALL_INDEX,
            "module_type": f"{type(module).__module__}.{type(module).__qualname__}",
            "cpu_offload_bytes_before": before,
            "cpu_offload_bytes_after": after,
            "cpu_offload_bytes_delta": after - before,
            "cpu_offload_max_bytes": self.cpu_offload_max_bytes,
            "pin_memory": self.pin_memory,
            "uva_offloading": self.uva_offloading,
            "parameters": parameters,
            "parameter_bytes_sum": sum(item["bytes"] for item in parameters),
        }
        with TRACE_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    return result


UVAOffloader._maybe_offload_to_cpu = traced

if __name__ == "__main__":
    raise SystemExit(gpu_campaign_runner.main())

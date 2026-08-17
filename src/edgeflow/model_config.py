"""Registered MoE model config -> derived expert-weight transfer volume.

expert_weight_bytes is DERIVED from published model dimensions (config.json),
never fabricated:

  gated_swiglu expert = gate_proj + up_proj + down_proj
                      = 3 * hidden * intermediate parameters
  standard_2mat expert = fc1 + fc2 = 2 * hidden * intermediate parameters

bytes = params * weight_precision.bytes_per_param.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class ModelConfig:
    raw: dict[str, Any]

    @classmethod
    def load(cls, path: str | Path) -> "ModelConfig":
        return cls(json.loads(Path(path).read_text()))

    @property
    def model_id(self) -> str:
        return self.raw["model_id"]

    @property
    def trace_variant(self) -> str:
        return self.raw["trace_variant"]

    @property
    def num_experts(self) -> int:
        return int(self.raw["num_experts"])

    @property
    def top_k(self) -> int:
        return int(self.raw["top_k"])

    @property
    def num_moe_layers(self) -> int:
        return int(self.raw["num_moe_layers"])

    @property
    def bytes_per_param(self) -> float:
        return float(self.raw["weight_precision"]["bytes_per_param"])

    def _mat_factor(self) -> int:
        return 3 if self.raw["mlp_kind"] == "gated_swiglu" else 2

    def expert_params(self) -> int:
        h = int(self.raw["hidden_size"])
        i = int(self.raw["moe_intermediate_size"])
        return self._mat_factor() * h * i

    def expert_weight_bytes(self, bytes_per_param: float | None = None) -> int:
        bpp = bytes_per_param if bytes_per_param is not None else self.bytes_per_param
        return int(round(self.expert_params() * bpp))

    def shared_expert_bytes(self, bytes_per_param: float | None = None) -> int:
        n = int(self.raw.get("num_shared_experts", 0))
        if n == 0:
            return 0
        h = int(self.raw["hidden_size"])
        i = int(self.raw.get("shared_expert_intermediate_size", self.raw["moe_intermediate_size"]))
        bpp = bytes_per_param if bytes_per_param is not None else self.bytes_per_param
        return int(round(n * self._mat_factor() * h * i * bpp))

    def summary(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "trace_variant": self.trace_variant,
            "num_experts": self.num_experts,
            "top_k": self.top_k,
            "num_moe_layers": self.num_moe_layers,
            "precision": self.raw["weight_precision"]["name"],
            "bytes_per_param": self.bytes_per_param,
            "expert_params": self.expert_params(),
            "expert_weight_bytes": self.expert_weight_bytes(),
            "expert_weight_MiB": round(self.expert_weight_bytes() / 2**20, 2),
            "shared_expert_bytes": self.shared_expert_bytes(),
        }

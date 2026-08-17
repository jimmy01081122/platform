#!/usr/bin/env python3
"""Freeze deterministic CMP-M3 occupancy bins from fit-only routing traces."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np


def nearest_rank(values: list[int], q: float) -> int:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1))
    return int(ordered[index])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fit-routing", action="append", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    occupancy: list[int] = []
    source_shapes: list[dict[str, object]] = []
    for path in args.fit_routing:
        array = np.load(path, mmap_mode="r")
        if array.ndim != 3 or array.shape[1:] != (32, 2):
            raise SystemExit(f"unexpected routing shape {path}: {array.shape}")
        counts = np.zeros((array.shape[1], 8), dtype=np.int64)
        for layer in range(array.shape[1]):
            counts[layer] = np.bincount(array[:, layer, :].reshape(-1), minlength=8)
        occupancy.extend(int(value) for value in counts.reshape(-1))
        source_shapes.append({"path": str(path.resolve()), "shape": list(array.shape)})

    payload = {
        "schema_version": "phase7-cmp-m3-fit-occupancy-v1",
        "source_role": "fit_only",
        "deterministic_rule": "per-layer expert forwarded-token counts; nearest-rank quantiles q=0.10/0.50/0.90 and maximum",
        "model_dimensions": {"num_hidden_layers": 32, "num_local_experts": 8, "num_experts_per_tok": 2},
        "source_shapes": source_shapes,
        "sample_count": len(occupancy),
        "bins": {
            "p10": nearest_rank(occupancy, 0.10),
            "p50": nearest_rank(occupancy, 0.50),
            "p90": nearest_rank(occupancy, 0.90),
            "max": max(occupancy),
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

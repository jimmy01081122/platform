#!/usr/bin/env python3
"""Freeze a small fit-only actual layer-expert sequence for XFER-E3."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fit-routing", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--count", type=int, default=8)
    args = parser.parse_args()
    array = np.load(args.fit_routing, mmap_mode="r")
    seen: set[tuple[int, int]] = set()
    sequence: list[dict[str, int]] = []
    for token in range(array.shape[0]):
        for expert in array[token, 0, :].tolist():
            key = (0, int(expert))
            if key not in seen:
                seen.add(key)
                sequence.append({"layer_id": key[0], "local_expert_id": key[1], "global_object_id": key[0] * 8 + key[1]})
            if len(sequence) >= args.count:
                break
        if len(sequence) >= args.count:
            break
    payload = {
        "schema_version": "phase7-xfer-e3-fit-sequence-v1",
        "source_role": "fit_only",
        "source_routing_path": str(args.fit_routing.resolve()),
        "source_shape": list(array.shape),
        "sequence": sequence,
        "identity_formula": "layer_id * num_local_experts + local_expert_id",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

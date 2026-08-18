#!/usr/bin/env python3
"""Design + seal the A4 held-out split BEFORE any new GPU data exists.

Why seal now, with no measured outputs yet (TRACK_GPU_PREP §5 step 5, root spec
§7.2): a held-out set only has force if the model could not see it while fitting.
If we measured first and partitioned afterwards, the partitioner would already
have seen every point and the seal would be theatre. So what is committed here is
the *assignment rule* over the measurement condition-cells -- which cells become
fit / validation / held-out -- fixed and hashed before the numbers exist. When
TRACK_GPU later measures these cells, the assignment is predetermined and
auditable; STAGE_A4 unseals and scores the held-out cells exactly once.

The cells target the four STAGE_A1 model-form defects so the held-out is not
concentrated in easy regions (root spec §2.3, STAGE_A4 §5 step 1):
  defect 1  copy-engine contention   -> multi-stream PCIe, small AND large bytes
  defect 2  component shape-blindness -> operand-shape sweep per component op
  defect 3  MoE-replay batching       -> concurrency sweep for window_replay
  defect 4  small-transfer floor      -> small-size PCIe points

Partition is deterministic: SHA-256 of each cell's canonical id, bucketed by the
low byte, with a recorded seed string. This file is the generator; its output
``holdout_split_v1_manifest.json`` is the sealed artifact. Re-running MUST
reproduce byte-identical assignments (a regression test asserts this).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

SEED = "platform-a4-sealed-holdout-v1"
SPLIT_RATIO = {"fit": 0.60, "validation": 0.20, "holdout": 0.20}

# Byte sizes spanning the small-transfer floor (defect 4 / GAP-3) up through the
# bandwidth-bound regime (defect 1 large-size confirmation).
PCIE_BYTES = [4096, 65536, 1048576, 16777216, 88080384]
PCIE_STREAMS = [1, 2, 4]
PCIE_DIRECTIONS = ["h2d", "d2h"]

# Component operand-shape sweep: expert_tokens spanning the measured 70x range
# (defect 2). Denser prefill shapes address V2-GAP-B.
COMPONENT_OPS = ["selected_expert", "grouped_gemm", "gather_scatter", "dequant"]
COMPONENT_EXPERT_TOKENS = [8, 16, 32, 64, 128, 256, 512, 1024]
COMPONENT_PHASES = ["prefill", "decode"]

# MoE-replay concurrency sweep (defect 3).
REPLAY_CONCURRENCY = [1, 2, 4, 8]


def _cell(metric: str, **params) -> dict:
    canonical = metric + "|" + "|".join(
        f"{k}={params[k]}" for k in sorted(params)
    )
    digest = hashlib.sha256((SEED + "\x1f" + canonical).encode()).hexdigest()
    return {
        "metric": metric,
        "params": params,
        "cell_id": canonical,
        "sha256": digest,
    }


def _assign(sha256: str) -> str:
    # deterministic bucket in [0,1) from the top 8 hex digits
    frac = int(sha256[:8], 16) / 0x1_0000_0000
    if frac < SPLIT_RATIO["fit"]:
        return "fit"
    if frac < SPLIT_RATIO["fit"] + SPLIT_RATIO["validation"]:
        return "validation"
    return "holdout"


def build_cells() -> list[dict]:
    cells: list[dict] = []
    for b in PCIE_BYTES:
        for s in PCIE_STREAMS:
            for d in PCIE_DIRECTIONS:
                cells.append(_cell("pcie_transfer_latency", bytes=b, copy_streams=s, direction=d))
    for op in COMPONENT_OPS:
        for et in COMPONENT_EXPERT_TOKENS:
            for ph in COMPONENT_PHASES:
                cells.append(_cell("component_latency", op=op, expert_tokens=et, phase=ph))
    for cc in REPLAY_CONCURRENCY:
        cells.append(_cell("moe_replay_tpot", concurrency=cc))
        cells.append(_cell("moe_replay_throughput", concurrency=cc))
    cells.sort(key=lambda c: c["cell_id"])
    for c in cells:
        c["split"] = _assign(c["sha256"])
    return cells


def build_manifest(sealed_at: str | None = None) -> dict:
    cells = build_cells()
    counts = {"fit": 0, "validation": 0, "holdout": 0}
    for c in cells:
        counts[c["split"]] += 1
    # hash over the ordered (cell_id, split) assignment: the sealed commitment
    assignment_blob = "\n".join(f"{c['cell_id']}\t{c['split']}" for c in cells)
    assignment_sha256 = hashlib.sha256(assignment_blob.encode()).hexdigest()
    return {
        "schema_version": "sealed-holdout-manifest-v1",
        "id": "a4_holdout_split_v1",
        "purpose": "STAGE_A4 sealed held-out split, committed before GPU data exists",
        "seed": SEED,
        "split_ratio": SPLIT_RATIO,
        "sealed_at": sealed_at or datetime.now(timezone.utc).isoformat(),
        "outputs_status": "PENDING_MEASUREMENT",
        "outputs_note": (
            "Measured values do not exist yet. This manifest seals the "
            "condition-cell -> split ASSIGNMENT. TRACK_GPU measures these cells; "
            "STAGE_A4 unseals the holdout cells and scores exactly once. The seal "
            "is the assignment, not the numbers -- which is what prevents leakage."
        ),
        "targets_defects": {
            "defect_1_contention": "pcie multi-stream, small+large bytes",
            "defect_2_component_shape": "expert_tokens sweep per component op",
            "defect_3_replay_batching": "window_replay concurrency sweep",
            "defect_4_small_transfer_floor": "small-size pcie points",
        },
        "cell_counts": counts,
        "cell_total": len(cells),
        "assignment_sha256": assignment_sha256,
        "cells": cells,
    }


def main() -> int:
    manifest = build_manifest()
    out = Path(__file__).resolve().parent / "holdout_split_v1_manifest.json"
    out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(
        f"sealed {manifest['cell_total']} cells "
        f"(fit={manifest['cell_counts']['fit']} "
        f"val={manifest['cell_counts']['validation']} "
        f"holdout={manifest['cell_counts']['holdout']}) "
        f"assignment_sha256={manifest['assignment_sha256'][:16]}... -> {out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Audit one OFF-E-PR3 capacity point after verified local backup."""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import OrderedDict
from pathlib import Path
from typing import Any

import numpy as np


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def verify_remote_manifest(root: Path) -> None:
    for line in (root / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        expected, remote_path = line.split(None, 1)
        marker = f"/{root.name}/"
        if marker not in remote_path:
            raise RuntimeError(f"manifest path outside attempt: {remote_path}")
        relative = remote_path.split(marker, 1)[1]
        if sha256(root / relative) != expected:
            raise RuntimeError(f"checksum mismatch: {relative}")


def lru_plan(sequence: list[int], capacity: int) -> list[tuple[int, int, int | None]]:
    cache: OrderedDict[int, None] = OrderedDict()
    loads: list[tuple[int, int, int | None]] = []
    for demand_index, object_id in enumerate(sequence):
        if object_id in cache:
            cache.move_to_end(object_id)
            continue
        evicted = None
        if len(cache) >= capacity:
            evicted, _ = cache.popitem(last=False)
        cache[object_id] = None
        loads.append((demand_index, object_id, evicted))
    return loads


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--attempt-root", type=Path, required=True)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--control-runner", type=Path, required=True)
    args = parser.parse_args()
    root = args.attempt_root
    contract = read_json(args.contract)
    verify_remote_manifest(root)
    replay = read_json(root / "off_e_pr3_trace" / "capacity_replay.json")
    cell = replay["canonical_experiment_id"]
    point = next(item for item in contract["capacity_points"] if item["cell_id"] == cell)
    runners = [path for path in (root / "runner_runs").iterdir() if path.is_dir()]
    if len(runners) != 1:
        raise SystemExit("capacity point must contain exactly one fresh runner")
    runner = runners[0]
    status = read_json(runner / "status.json")
    result = read_json(runner / "result.json")
    control_result = read_json(args.control_runner / "result.json")
    routing = list((runner / "routing").glob("*.npy"))
    if len(routing) != 1:
        raise SystemExit("capacity point must contain one routing array")
    array = np.load(routing[0], allow_pickle=False)
    sequence = [
        int(layer * 8 + expert)
        for token in array
        for layer, pair in enumerate(token)
        for expert in pair
    ]
    expected_loads = lru_plan(sequence, point["capacity_objects"])
    events = replay["transfer_events"]
    control = point["label"] == "100"
    event_gate = control or (
        len(events) == len(expected_loads)
        and all(
            event["load_ordinal"] == ordinal
            and event["demand_index"] == expected[0]
            and event["logical_object_id"] == expected[1]
            and event["logical_evicted_object_id"] == expected[2]
            and event["h2d_bytes"] == contract["expert_catalog"]["object_bytes"]
            and event["h2d_complete_monotonic_ns"] >= event["h2d_start_monotonic_ns"]
            for ordinal, (event, expected) in enumerate(zip(events, expected_loads))
        )
    )
    expected_count = 0 if control else len(expected_loads)
    expected_evictions = 0 if control else max(0, expected_count - point["capacity_objects"])
    expected_hits = len(sequence) - expected_count
    preflight = (root / "dispatch_preflight.txt").read_text(encoding="utf-8")
    gates = {
        "remote_manifest": True,
        "preflight": all(
            text in preflight
            for text in ("session_guard=PASS", "serving_conflict=NONE", "compute_conflict=NONE")
        ),
        "runner": status.get("status") == "PASS"
        and result.get("input_token_count") == 128
        and result.get("output_token_count") == 32
        and result.get("finish_reason") == "length",
        "routing": list(array.shape) == [159, 32, 2]
        and str(array.dtype) == "uint8"
        and sha256(routing[0]) == contract["routing_trace"]["array_sha256"]
        and len(sequence) == 10176
        and set(sequence) == set(range(256)),
        "output_equivalence": result.get("output_token_ids") == control_result.get("output_token_ids"),
        "capacity": replay.get("capacity_objects") == point["capacity_objects"]
        and replay.get("capacity_bytes")
        == point["capacity_objects"] * contract["expert_catalog"]["object_bytes"],
        "event_lineage": event_gate,
        "conservation": replay.get("demand_load_count") == expected_count
        and replay.get("hit_count") == expected_hits
        and replay.get("immutable_discard_count") == expected_evictions
        and replay.get("h2d_bytes")
        == expected_count * contract["expert_catalog"]["object_bytes"]
        and replay.get("d2h_writeback_bytes") == 0,
        "dependency_compute": replay.get("dependency_gate") == "PASS"
        and replay.get("actual_expert_compute") is True
        and replay.get("actual_expert_compute_end_monotonic_ns", 0)
        > replay.get("actual_expert_compute_start_monotonic_ns", 0)
        >= replay.get("all_h2d_complete_monotonic_ns", 0),
    }
    passed = all(gates.values())
    audit = {
        "schema_version": "phase7-off-e-pr3-point-audit-v1",
        "status": "PASS" if passed else "FAIL",
        "cell_id": cell,
        "attempt_id": root.name,
        "fit_role": point["fit_role"],
        "contract_sha256": sha256(args.contract),
        "gates": gates,
        "metrics": {
            key: replay[key]
            for key in (
                "capacity_objects",
                "capacity_bytes",
                "demand_load_count",
                "hit_count",
                "immutable_discard_count",
                "h2d_bytes",
                "d2h_writeback_bytes",
                "total_h2d_cuda_elapsed_ms",
            )
        },
        "routing_sha256": sha256(routing[0]),
        "claim_boundary": contract["claim_boundary"],
    }
    (root / "off_e_pr3_point_audit.json").write_text(
        json.dumps(audit, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(audit, indent=2, sort_keys=True))
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())

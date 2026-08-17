#!/usr/bin/env python3
"""Create logical-only system events from C1 canonical routing."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))

from collectors.trace_contract import canonical_hash, write_json  # noqa: E402

try:
    from .c1_canonicalize import _reject_non_finite, validate_routing_event
except ImportError:  # Direct script execution.
    from c1_canonicalize import _reject_non_finite, validate_routing_event


def build_system_ir(canonical_path: Path, output_path: Path) -> dict[str, Any]:
    source = json.loads(
        canonical_path.read_text(encoding="utf-8"),
        parse_constant=_reject_non_finite,
    )
    if not isinstance(source, dict):
        raise ValueError("canonical routing document must be an object")
    if source.get("schema_version") != "c1-canonical-routing-v1":
        raise ValueError("unsupported canonical routing schema")
    events_source = source.get("events")
    if not isinstance(events_source, list) or not events_source:
        raise ValueError("canonical routing events must be a non-empty array")
    expected_hash = canonical_hash({
        key: value for key, value in source.items()
        if key != "canonical_content_hash"
    })
    if source.get("canonical_content_hash") != expected_hash:
        raise ValueError("canonical routing content hash mismatch")
    keys = []
    for index, route in enumerate(events_source):
        if not isinstance(route, dict):
            raise ValueError(f"canonical event {index} must be an object")
        validate_routing_event(route, context=f"canonical event {index}")
        keys.append(route["event_key"])
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate routing event_key")
    events = []
    ordered = sorted(events_source, key=lambda item: (
        item["execution_alignment_key"], item["token_index"],
        item["layer_id"], item.get("dispatch_index", 0), item["event_key"],
    ))
    for route in ordered:
        for expert_order, expert_id in enumerate(route["selected_experts"]):
            payload = {
                "source_event_key": route["event_key"],
                "expert_order": expert_order,
                "expert_id": expert_id,
            }
            events.append({
                "schema_version": "c1-system-event-v1",
                "event_type": "EXPERT_ROUTE",
                "event_key": canonical_hash(payload),
                "request_id": route["request_id"],
                "token_id": route["token_index"],
                "layer_id": route["layer_id"],
                "expert_id": expert_id,
                "logical_order": len(events),
                "timestamp_class": "logical",
                "provenance": {"routing": "measured", "timing": "unassigned"},
            })
    document = {
        "schema_version": "c1-system-ir-v1",
        "source_canonical_hash": source["canonical_content_hash"],
        "timing_policy": "logical_only",
        "events": events,
    }
    document["content_hash"] = canonical_hash(document)
    write_json(output_path, document)
    return document


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--canonical", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    build_system_ir(args.canonical, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

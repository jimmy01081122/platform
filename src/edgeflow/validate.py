"""Schema validation helpers for canonical traces and run artifacts."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

import jsonschema

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = ROOT / "schemas"


def load_schema(name: str) -> dict[str, Any]:
    return json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))


def validate_events(events: Iterable[dict[str, Any]], schema_name: str = "trace.schema.json") -> tuple[int, list[str]]:
    """Validate every canonical event. Returns (count_valid, errors)."""
    schema = load_schema(schema_name)
    validator = jsonschema.Draft7Validator(schema)
    errors: list[str] = []
    count = 0
    for i, ev in enumerate(events):
        found = sorted(validator.iter_errors(ev), key=lambda e: e.path)
        if found:
            for e in found[:3]:
                errors.append(f"event[{i}] {list(e.path)}: {e.message}")
        else:
            count += 1
    return count, errors


def validate_ordering(events: list[dict[str, Any]]) -> list[str]:
    """Structural checks beyond JSON schema (S1 conservation/ordering)."""
    problems: list[str] = []
    seen_ids: set[str] = set()
    last_ts_by_batch: dict[str, float] = {}
    for ev in events:
        eid = ev["event_id"]
        if eid in seen_ids:
            problems.append(f"duplicate event_id: {eid}")
        seen_ids.add(eid)
        b = ev["request_id"]
        ts = float(ev["timestamp"])
        if b in last_ts_by_batch and ts < last_ts_by_batch[b]:
            problems.append(f"non-monotonic timestamp for batch {b}: {ts} < {last_ts_by_batch[b]}")
        last_ts_by_batch[b] = ts
        # dependencies must reference previously seen events
        for dep in ev.get("dependencies", []):
            if dep not in seen_ids:
                problems.append(f"event {eid} depends on unseen event {dep}")
    return problems

#!/usr/bin/env python3
"""Deterministically convert immutable C1 P2 JSONL into canonical routing IR."""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))

from adapters.models.contract import GATE_SUM_ABS_TOLERANCE  # noqa: E402
from collectors.trace_contract import canonical_hash, sha256_file, write_json  # noqa: E402

CONVERTER_VERSION = "c1-routing-canonicalizer-v1"
NUM_EXPERTS = 32
ROUTING_TOP_K = 8
HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def _reject_non_finite(value: str) -> None:
    raise ValueError(f"non-finite JSON number is prohibited: {value}")


def _finite_numbers(
    value: Any, *, field: str, expected_length: int
) -> list[float]:
    if not isinstance(value, list) or len(value) != expected_length:
        raise ValueError(f"{field} must contain exactly {expected_length} numbers")
    result: list[float] = []
    for item in value:
        if (
            not isinstance(item, (int, float))
            or isinstance(item, bool)
            or not math.isfinite(item)
        ):
            raise ValueError(f"{field} must contain only finite numbers")
        result.append(float(item))
    return result


def validate_routing_event(value: dict[str, Any], *, context: str) -> None:
    """Validate C1 routing semantics independently of the JSON schema."""
    if value.get("schema_version") != "c1-routing-event-v1":
        raise ValueError(f"{context}: unsupported routing schema")
    if value.get("actual_dispatch") is not True:
        raise ValueError(f"{context}: route is not actual dispatch")
    for field in ("event_key", "execution_alignment_key"):
        if not isinstance(value.get(field), str) or not HASH_RE.fullmatch(value[field]):
            raise ValueError(f"{context}: {field} must be lowercase SHA-256")
    for field in (
        "generation_step", "token_index", "layer_id", "dispatch_index"
    ):
        item = value.get(field, 0 if field == "dispatch_index" else None)
        if not isinstance(item, int) or isinstance(item, bool) or item < 0:
            raise ValueError(f"{context}: {field} must be a non-negative integer")
    for field in ("request_id", "router_module"):
        if not isinstance(value.get(field), str) or not value[field]:
            raise ValueError(f"{context}: {field} must be a non-empty string")

    selected = value.get("selected_experts")
    if not isinstance(selected, list) or len(selected) != ROUTING_TOP_K:
        raise ValueError(
            f"{context}: selected_experts must contain exactly {ROUTING_TOP_K} experts"
        )
    if any(
        not isinstance(expert, int)
        or isinstance(expert, bool)
        or expert < 0
        or expert >= NUM_EXPERTS
        for expert in selected
    ):
        raise ValueError(
            f"{context}: expert IDs must be integers in [0, {NUM_EXPERTS})"
        )
    if len(set(selected)) != len(selected):
        raise ValueError(f"{context}: selected_experts must not contain duplicates")
    if value.get("top_k") != ROUTING_TOP_K:
        raise ValueError(f"{context}: top_k must equal {ROUTING_TOP_K}")
    gate_dtype = value.get("gate_dtype")
    if gate_dtype not in GATE_SUM_ABS_TOLERANCE:
        raise ValueError(
            f"{context}: gate_dtype must be one of "
            f"{sorted(GATE_SUM_ABS_TOLERANCE)}"
        )

    unavailable = value.get("unavailable_reasons")
    if not isinstance(unavailable, dict):
        raise ValueError(f"{context}: unavailable_reasons must be an object")
    logits = value.get("router_logits")
    if logits is None:
        if not isinstance(unavailable.get("router_logits"), str) or not unavailable[
            "router_logits"
        ]:
            raise ValueError(f"{context}: null router_logits requires a reason")
    else:
        _finite_numbers(logits, field=f"{context}: router_logits", expected_length=32)
    weights = value.get("routing_weights")
    if weights is None:
        if not isinstance(unavailable.get("routing_weights"), str) or not unavailable[
            "routing_weights"
        ]:
            raise ValueError(f"{context}: null routing_weights requires a reason")
    else:
        normalized = _finite_numbers(
            weights, field=f"{context}: routing_weights", expected_length=ROUTING_TOP_K
        )
        if any(weight < 0.0 or weight > 1.0 for weight in normalized):
            raise ValueError(f"{context}: routing_weights must be in [0, 1]")
        tolerance = GATE_SUM_ABS_TOLERANCE[gate_dtype]
        if not math.isclose(
            sum(normalized), 1.0, rel_tol=0.0, abs_tol=tolerance
        ):
            raise ValueError(
                f"{context}: routing_weights exceed {gate_dtype} "
                f"sum tolerance {tolerance}"
            )


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    records = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line, parse_constant=_reject_non_finite)
        if not isinstance(value, dict):
            raise ValueError(f"{path.name}:{number}: expected object")
        validate_routing_event(value, context=f"{path.name}:{number}")
        records.append(value)
    if not records:
        raise ValueError("routing input is empty")
    return records


def canonicalize(
    raw_path: Path, output_path: Path, conversion_log: Path | None = None
) -> dict[str, Any]:
    raw_path = raw_path.resolve()
    before = sha256_file(raw_path)
    records = _read_jsonl(raw_path)
    keys = [item.get("event_key") for item in records]
    if any(not isinstance(key, str) or not key for key in keys):
        raise ValueError("every routing event requires event_key")
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate routing event_key")
    events = sorted(records, key=lambda item: (
        item["execution_alignment_key"], item["token_index"],
        item["layer_id"], item.get("dispatch_index", 0), item["event_key"],
    ))
    document = {
        "schema_version": "c1-canonical-routing-v1",
        "converter_version": CONVERTER_VERSION,
        "input_sha256": before,
        "events": events,
    }
    document["canonical_content_hash"] = canonical_hash(document)
    write_json(output_path, document)
    after = sha256_file(raw_path)
    if after != before:
        raise RuntimeError("raw input changed during conversion")
    if conversion_log is not None:
        write_json(conversion_log, {
            "schema_version": "c1-conversion-log-v1",
            "converter_version": CONVERTER_VERSION,
            "input_sha256": before,
            "output_sha256": sha256_file(output_path),
            "record_count": len(events),
            "raw_unchanged": True,
        })
    return document


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--conversion-log", type=Path)
    args = parser.parse_args()
    canonicalize(args.raw, args.output, args.conversion_log)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

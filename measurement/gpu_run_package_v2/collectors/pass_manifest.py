"""Finalize one profiler pass without hiding unsupported or failed capture."""
from __future__ import annotations

import argparse
from pathlib import Path

from .trace_contract import (
    PASSES, STATUSES, load_json, sha256_file, validate_benchmark_trace_record,
    write_json,
)


def build_raw_observation(
    identity: dict, clock: dict, content_ids: list[str],
    collector_adapter: str | None,
) -> dict:
    """Build the required raw-observation object for old and new callers."""
    def observed(value):
        return {
            "status": "observed",
            "value": value,
            "source_content_ids": list(content_ids),
        }

    def unavailable(reason):
        return {
            "status": "known_limitation",
            "reason": reason,
            "source_content_ids": list(content_ids),
        }

    if not content_ids:
        reason = "pass has no captured raw artifact"
        return {
            name: unavailable(reason)
            for name in ("environment", "gpu_uuid", "runtime", "start_utc", "end_utc")
        }
    wall_clock = clock.get("wall_clock_utc")
    return {
        "environment": observed({"environment_hash": identity.get("environment_hash")}),
        "gpu_uuid": unavailable("identity input does not expose a GPU UUID"),
        "runtime": (
            observed({"collector_adapter": collector_adapter})
            if collector_adapter else
            unavailable("collector adapter was not recorded")
        ),
        "start_utc": (
            observed(wall_clock) if wall_clock else
            unavailable("clock input does not expose wall_clock_utc")
        ),
        "end_utc": (
            observed(wall_clock) if wall_clock else
            unavailable("clock input does not expose wall_clock_utc")
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-root", required=True, type=Path)
    parser.add_argument("--identity", required=True, type=Path,
                        help="JSON emitted by scripts/session_identity.py")
    parser.add_argument("--pass-id", required=True, choices=sorted(PASSES))
    parser.add_argument("--status", required=True, choices=sorted(STATUSES))
    parser.add_argument("--failure-reason")
    parser.add_argument("--rerun-command", required=True)
    parser.add_argument("--raw-content-id", action="append", default=[])
    parser.add_argument("--converter-name", default="not-run")
    parser.add_argument("--converter-version", default="not-run")
    parser.add_argument("--converter-source", type=Path, default=Path(__file__))
    parser.add_argument("--benchmark-record", type=Path, action="append", default=[])
    parser.add_argument("--collector-adapter")
    parser.add_argument("--estimate-minutes", type=float)
    args = parser.parse_args()
    if args.status == "complete" and not args.raw_content_id:
        parser.error("complete status requires at least one --raw-content-id")
    if args.status != "complete" and not args.failure_reason:
        parser.error("non-complete status requires --failure-reason")
    identity_document = load_json(args.identity)
    identity = identity_document.get("identity")
    clock = identity_document.get("clock")
    if not isinstance(identity, dict) or not isinstance(clock, dict):
        parser.error("--identity must contain identity and clock objects")
    if identity.get("profiler_pass") != args.pass_id:
        parser.error("identity profiler_pass differs from --pass-id")
    records = []
    for record_path in args.benchmark_record:
        record = load_json(record_path)
        errors = validate_benchmark_trace_record(record)
        if errors:
            parser.error(f"{record_path}: {'; '.join(errors)}")
        alignment = record["alignment"]
        if alignment["session_id"] != identity.get("session_id"):
            parser.error(f"{record_path}: alignment session_id differs from identity")
        if alignment["repetition_index"] != identity.get("repetition_index"):
            parser.error(f"{record_path}: repetition_index differs from identity")
        if record["profiler_pass"] != args.pass_id:
            parser.error(f"{record_path}: profiler_pass differs from --pass-id")
        records.append(record)
    root = args.session_root.resolve()
    inventory_path = root / "raw_traces" / "RAW_INVENTORY.json"
    inventory = load_json(inventory_path)
    indexed = {
        entry.get("content_id"): entry for entry in inventory.get("entries", [])
        if isinstance(entry, dict)
    }
    missing = sorted(set(args.raw_content_id) - set(indexed))
    if missing:
        parser.error(f"raw content IDs absent from inventory: {', '.join(missing)}")
    manifest = {
        "schema_version": "trace-pass-manifest-v2",
        "pass_id": args.pass_id,
        "status": args.status,
        "failure_reason": args.failure_reason,
        "identity": identity,
        "clock": clock,
        "raw_observation": build_raw_observation(
            identity, clock, args.raw_content_id, args.collector_adapter,
        ),
        "raw_artifacts": [
            {
                "content_id": content_id,
                "path": indexed[content_id]["path"],
                "sha256": indexed[content_id]["sha256"],
                "bytes": indexed[content_id]["bytes"],
            }
            for content_id in args.raw_content_id
        ],
        "converter_provenance": {
            "name": args.converter_name,
            "version": args.converter_version,
            "source_hash": sha256_file(args.converter_source.resolve()),
            "input_content_ids": args.raw_content_id,
        },
        "rerun_command": args.rerun_command,
        "benchmark_trace_records": records,
        "collector_adapter": args.collector_adapter,
    }
    if args.estimate_minutes is not None:
        manifest["estimate_minutes"] = args.estimate_minutes
    destination = (
        root / "runs" / identity["run_group_id"] / PASSES[args.pass_id]
        / "runs" / identity["run_id"] / "PASS_MANIFEST.json"
    )
    if destination.exists():
        raise SystemExit(
            f"refusing to overwrite existing run manifest: {destination}"
        )
    write_json(destination, manifest)
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

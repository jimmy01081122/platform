#!/usr/bin/env python3
"""Ingest measured JSONL profiler passes into a provenance-complete trace package."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))

from collectors.trace_contract import (  # noqa: E402
    PASSES, build_alignment_key, canonical_hash, sha256_file, write_json,
)
from scripts.trace_package_verify import (  # noqa: E402
    COMPLETE, package_root, verify_root,
)
from scripts.capture_orchestrator import build_capture_plan  # noqa: E402

MEASURED_PASSES = ("P0", "P1", "P2", "P3", "P5")
GPU_UUID_RE = re.compile(r"\bGPU-[0-9A-Fa-f-]+\b")
PASS_SOURCES = {
    "P0": ("p0/native.jsonl", "p0/nvidia-smi-before.txt", "p0/nvidia-smi-after.txt"),
    "P1": (
        "p1/native.jsonl", "p1/gsm8k-chrome-trace.json",
        "p1/mmlu-chrome-trace.json",
    ),
    "P2": ("p2/native.jsonl", "p2/nvidia-smi-before.txt", "p2/nvidia-smi-after.txt"),
    "P3": (
        "p3/native.jsonl", "p3/allocator-before.json", "p3/allocator-after.json",
        "p3/nvidia-smi-before.txt", "p3/nvidia-smi-after.txt",
    ),
    "P5": (
        "p5/native.jsonl", "p5/nvidia-smi-telemetry.csv",
        "p5/system-proc-telemetry.jsonl",
    ),
}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{number}: row must be an object")
        rows.append(value)
    return rows


def copy_file(source: Path, destination: Path) -> Path:
    if not source.is_file() or source.is_symlink() or source.stat().st_size == 0:
        raise ValueError(f"required source is missing, empty, or a symlink: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    if sha256_file(source) != sha256_file(destination):
        raise ValueError(f"copy verification failed: {source}")
    return destination


def relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def content_copy(source: Path, root: Path) -> tuple[Path, str]:
    digest = sha256_file(source)
    suffix = "".join(source.suffixes)[-32:] or ".bin"
    destination = root / "raw_traces" / "sha256" / digest[:2] / f"{digest}{suffix}"
    if not destination.exists():
        copy_file(source, destination)
    return destination, digest


def dump_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n"
                for row in rows),
        encoding="utf-8",
    )


def file_inventory(root: Path, excluded: set[str] | None = None) -> list[dict]:
    excluded = excluded or set()
    return [
        {
            "path": relative(root, path),
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
        and relative(root, path) not in excluded
    ]


def write_checksums(root: Path) -> None:
    excluded = {"checksums.sha256", "TRACE_COMPLETENESS_REPORT.json"}
    entries = file_inventory(root, excluded)
    (root / "checksums.sha256").write_text(
        "".join(f"{item['sha256']}  {item['path']}\n" for item in entries),
        encoding="utf-8",
    )


def load_measured(source: Path) -> tuple[dict, list[dict], list[dict]]:
    rows = read_jsonl(source)
    starts = [row for row in rows if row.get("event") == "pass_start"]
    measured = [
        row for row in rows
        if row.get("event") == "sample" and row.get("run_kind") == "measured"
    ]
    warmups = [
        row for row in rows
        if row.get("event") == "sample" and row.get("run_kind") == "warmup"
    ]
    if len(starts) != 1 or len(measured) != 8:
        raise ValueError(
            f"{source} requires one pass_start and exactly 8 measured samples"
        )
    if not warmups:
        raise ValueError(f"{source} must explicitly contain warmup samples")
    return starts[0], measured, warmups


def create_archive(session: Path, archive: Path) -> None:
    archive.parent.mkdir(parents=True, exist_ok=True)
    temporary = archive.with_suffix(archive.suffix + ".tmp")
    with tarfile.open(temporary, "w:gz") as bundle:
        bundle.add(session, arcname=session.name, recursive=True)
    temporary.replace(archive)
    digest = sha256_file(archive)
    archive.with_suffix(archive.suffix + ".sha256").write_text(
        f"{digest}  {archive.name}\n", encoding="utf-8"
    )


def raw_field(
    value: Any, source_content_ids: list[str], limitation: str | None = None,
) -> dict[str, Any]:
    if value is None:
        return {
            "status": "known_limitation",
            "source_content_ids": source_content_ids,
            "reason": limitation or "field is absent from the immutable raw capture",
        }
    return {
        "status": "observed",
        "source_content_ids": source_content_ids,
        "value": value,
    }


def pass_raw_observation(
    pass_id: str, start: dict[str, Any], raw: list[dict], source: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    native_id = raw[0]["content_id"]
    provenance = start.get("provenance", {})
    hardware = provenance.get("hardware")
    runtime = provenance.get("runtime")
    uuid_match = GPU_UUID_RE.search(
        hardware.get("nvidia_smi", "") if isinstance(hardware, dict) else ""
    )
    start_utc = end_utc = monotonic_ns = None
    sampling = None
    clock_ids = [native_id]
    if pass_id == "P5":
        end_rows = [
            row for row in read_jsonl(source / "p5/native.jsonl")
            if row.get("event") == "pass_end"
        ]
        telemetry = end_rows[0].get("telemetry", {}) if len(end_rows) == 1 else {}
        sampling = {
            "status": (
                "sufficient"
                if telemetry.get("duration_seconds", 0) >= 5
                and telemetry.get("sample_count", 0)
                >= telemetry.get("minimum_contract", {}).get("samples", 20)
                else "insufficient"
            ),
            "duration_seconds": telemetry.get("duration_seconds"),
            "sample_count": telemetry.get("sample_count"),
            "minimum_duration_seconds": 5.0,
            "minimum_sample_count": telemetry.get(
                "minimum_contract", {}
            ).get("samples", 20),
            "raw_label": telemetry.get("sampling_sufficiency"),
        }
        system_rows = read_jsonl(source / "p5/system-proc-telemetry.jsonl")
        if system_rows:
            start_utc = datetime.fromtimestamp(
                system_rows[0]["wall_time_ns"] / 1_000_000_000, timezone.utc
            ).isoformat().replace("+00:00", "Z")
            end_utc = datetime.fromtimestamp(
                system_rows[-1]["wall_time_ns"] / 1_000_000_000, timezone.utc
            ).isoformat().replace("+00:00", "Z")
            monotonic_ns = system_rows[0]["monotonic_ns"]
            clock_ids.append(raw[2]["content_id"])
    missing_time = (
        "raw pass stream has no capture timestamp; legacy M0 evidence is retained "
        "as a known limitation"
    )
    observation = {
        "environment": raw_field(hardware, [native_id]),
        "gpu_uuid": raw_field(
            uuid_match.group(0) if uuid_match else None,
            [native_id],
            "GPU UUID is absent from this pass's raw provenance",
        ),
        "runtime": raw_field(runtime, [native_id]),
        "start_utc": raw_field(start_utc, clock_ids, missing_time),
        "end_utc": raw_field(end_utc, clock_ids, missing_time),
    }
    if start_utc is None:
        clock = {
            "status": "known_limitation",
            "reason": missing_time,
            "source_content_ids": clock_ids,
        }
    else:
        clock = {
            "status": "observed",
            "wall_clock_utc": start_utc,
            "monotonic_host_ns": monotonic_ns,
            "profiler_domain": "raw-system-proc-telemetry",
            "timezone": "UTC",
            "source_content_ids": clock_ids,
            "alignment": {
                "method": "same raw telemetry row carries wall_time_ns and monotonic_ns",
                "max_error_ns": 0,
                "anchors": [{
                    "wall_clock_utc": start_utc,
                    "monotonic_host_ns": monotonic_ns,
                }],
            },
        }
    return observation, clock, sampling


def ingest(
    source: Path, destination: Path, suite: Path, model_snapshot: Path,
    dataset_inventory: Path, matrix_path: Path, *, overwrite: bool = False,
) -> dict[str, Any]:
    source, destination = source.resolve(), destination.resolve()
    if destination.exists():
        if not overwrite:
            raise FileExistsError(f"destination exists: {destination}")
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    canonical_schema_path = copy_file(
        PACKAGE_ROOT / "schemas/canonical_pass_ir.schema.json",
        destination / "provenance/schemas/canonical_pass_ir.schema.json",
    )
    frozen_matrix_path = copy_file(
        matrix_path.resolve(),
        destination / "provenance/capture/frozen_matrix.json",
    )

    pass_data = {
        pass_id: load_measured(source / PASS_SOURCES[pass_id][0])
        for pass_id in MEASURED_PASSES
    }
    p0_start = pass_data["P0"][0]
    provenance = p0_start["provenance"]
    model = provenance["model"]
    runtime = provenance["runtime"]
    hardware = provenance["hardware"]
    revision = model["revision"]
    session_id = f"m0-provenance-{sha256_file(source / 'p0/native.jsonl')[:16]}"
    group_id = "m0-tiny-qwen2moe-standard"
    generation = p0_start["generation"]

    mapping = json.loads(
        (source / "suite_class_mapping_v1.2.0.json").read_text(encoding="utf-8")
    )
    sample_map = {
        item["raw_sample_hash"]: item for item in mapping["samples"]
    }
    suite_rows = read_jsonl(suite / "sample_manifest.jsonl")
    selected_hashes = set(sample_map)
    selected = [row for row in suite_rows if row.get("raw_sample_hash") in selected_hashes]
    if len(selected) != 8:
        raise ValueError(f"frozen suite resolves {len(selected)} of 8 measured samples")
    selected_by_hash = {row["raw_sample_hash"]: row for row in selected}

    frozen_root = destination / "provenance/frozen_suite/v1.2.0"
    copy_file(suite / "inventory.json", frozen_root / "inventory.json")
    copy_file(suite / "sample_manifest.jsonl", frozen_root / "sample_manifest.jsonl")
    dump_jsonl(destination / "datasets/selected_samples.jsonl", selected)
    copy_file(
        dataset_inventory,
        destination / "datasets/snapshot_inventory_v1.json",
    )
    copy_file(
        source / "suite_class_mapping_v1.2.0.json",
        destination / "provenance/suite_class_mapping_v1.2.0.json",
    )

    model_root = destination / "models/snapshot"
    model_inventory = json.loads(
        (source / "model_snapshot_inventory.json").read_text(encoding="utf-8")
    )
    for item in model_inventory["files"]:
        copy_file(model_snapshot / item["path"], model_root / item["path"])
    copy_file(
        source / "model_snapshot_inventory.json",
        destination / "models/model_snapshot_inventory.json",
    )

    environment = {
        "schema_version": "m0-environment-v1",
        "hardware": hardware,
        "runtime": runtime,
        "captured_from": "P0 pass_start provenance",
    }
    configuration = {
        "schema_version": "m0-generation-configuration-v1",
        "model": model,
        "generation": generation,
        "warmup_policy": {
            "excluded_from_measured_records": True,
            "per_benchmark_per_pass": 1,
        },
    }
    expanded_workload = {
        "schema_version": "m0-expanded-workload-v1",
        "suite_revision": "v1.2.0",
        "sample_count": 8,
        "samples": selected,
        "generation": generation,
    }
    write_json(destination / "environment/environment.json", environment)
    write_json(destination / "models/configuration.json", configuration)
    write_json(destination / "workloads/expanded_workload.json", expanded_workload)
    environment_hash = sha256_file(destination / "environment/environment.json")
    configuration_hash = sha256_file(destination / "models/configuration.json")
    workload_hash = sha256_file(destination / "workloads/expanded_workload.json")

    raw_entries: list[dict] = []
    raw_by_pass: dict[str, list[dict]] = {}
    raw_by_digest: dict[str, dict] = {}
    for pass_id in MEASURED_PASSES:
        raw_by_pass[pass_id] = []
        for rel_source in PASS_SOURCES[pass_id]:
            source_path = source / rel_source
            stored, digest = content_copy(source_path, destination)
            entry = raw_by_digest.get(digest)
            if entry is None:
                entry = {
                    "content_id": digest,
                    "sha256": digest,
                    "bytes": stored.stat().st_size,
                    "path": relative(destination, stored),
                    "source_name": rel_source,
                    "source_names": [rel_source],
                    "native_format": "m0-native-jsonl-v1"
                    if source_path.suffix == ".jsonl" else f"native{source_path.suffix}",
                    "pass_id": pass_id,
                    "run_id": f"{session_id}-{pass_id.lower()}-r0",
                    "capture_time_status": "known_limitation",
                    "capture_time_limitation": (
                        "legacy raw artifact does not carry a per-file capture timestamp"
                    ),
                    "immutable": True,
                    "truncated": False,
                }
                raw_entries.append(entry)
                raw_by_digest[digest] = entry
            elif rel_source not in entry["source_names"]:
                entry["source_names"].append(rel_source)
            raw_by_pass[pass_id].append(entry)

    template_metadata: dict[str, dict] = {}
    for row in read_jsonl(source / "derived/benchmark_trace_records.jsonl"):
        template_metadata.setdefault(row["benchmark_id"], {
            "template_id": row["template_id"],
            "template_hash": row["template_hash"],
        })

    converter_hash = sha256_file(Path(__file__).resolve())
    conversions: list[dict] = []
    records_by_pass: dict[str, list[dict]] = {}
    warmup_rows: list[dict] = []
    for pass_id in MEASURED_PASSES:
        start, measured, warmups = pass_data[pass_id]
        warmup_rows.extend([
            {
                "pass_id": pass_id,
                "run_kind": "warmup",
                "request_id": row["request_id"],
                "native_sample_id": row["sample_id"],
                "prompt_hash": row["prompt_hash"],
                "output_hash": row["output_hash"],
                "latency_ms": row["latency_ms"],
            }
            for row in warmups
        ])
        canonical_rows = []
        records = []
        # The per-request record points to the JSONL event stream. Global
        # pass-level profiler/telemetry files remain referenced by PASS_MANIFEST.
        native_paths = [raw_by_pass[pass_id][0]["path"]]
        native_checksums = {
            raw_by_pass[pass_id][0]["path"]: raw_by_pass[pass_id][0]["sha256"]
        }
        for request_index, row in enumerate(measured):
            sample = selected_by_hash[row["raw_sample_hash"]]
            mapped = sample_map[row["raw_sample_hash"]]
            sample_id = mapped["v1_2_sample_id"]
            benchmark = row["benchmark"]
            token_payload = {
                "prompt_token_ids": row["input_token_ids"],
                "generated_token_ids": row["output_token_ids"],
            }
            alignment = {
                "suite_id": "moe-trace-suite-v1.2.0",
                "sample_id": sample_id,
                "model_revision": revision,
                "generation_config_hash": canonical_hash(generation),
                "seed": generation["seed"],
                "request_id": row["request_id"],
                "token_index": 0,
                "layer_index": 0,
                "repetition_index": row["repetition"],
                "session_id": session_id,
            }
            alignment["alignment_key"] = build_alignment_key(alignment)
            quality = row["quality"]
            record = {
                "schema_version": "benchmark-trace-record-v1",
                "record_id": canonical_hash({
                    "pass": pass_id, "request_id": row["request_id"],
                    "session_id": session_id,
                }),
                "model_id": model["repo_id"],
                "model_revision": revision,
                "weights_revision": revision,
                "tokenizer_revision": revision,
                "suite_id": "moe-trace-suite-v1.2.0",
                "benchmark_id": benchmark,
                "sample_id": sample_id,
                **template_metadata[benchmark],
                "prompt_hash": row["prompt_hash"],
                "generation_config": generation,
                "generation_config_hash": canonical_hash(generation),
                "actual_tokens": {
                    "prompt": row["input_token_count"],
                    "generated": row["output_token_count"],
                    "total": row["input_token_count"] + row["output_token_count"],
                    **token_payload,
                    "token_ids_hash": canonical_hash(token_payload),
                },
                "output_hash": row["output_hash"],
                "quality": {
                    "status": "pass" if quality.get(
                        "validity", quality.get("valid")
                    ) else "fail",
                    "score": 1.0 if quality.get(
                        "correctness", quality.get("correct")
                    ) else 0.0,
                    "reason": "native M0 evaluator result",
                },
                "serving_runtime": "transformers-generate",
                "serving": start["provenance"]["runtime"],
                "hardware_id": "rtx3050-6gb-local-0",
                "hardware": start["provenance"]["hardware"],
                "repetition_index": row["repetition"],
                "request_index": request_index,
                "profiler_pass": pass_id,
                "native_format": "m0-qwen2moe-jsonl-v1",
                "native_paths": native_paths,
                "native_sha256": raw_by_pass[pass_id][0]["sha256"],
                "native_checksums": native_checksums,
                "environment_hash": environment_hash,
                "alignment": alignment,
                "completeness": {
                    "complete": True, "missing_fields": [], "truncated": False,
                },
                "measurement": {
                    "run_kind": "measured",
                    "latency_ms": row["latency_ms"],
                    "latency_scope": row["latency_scope"],
                    "warmup_excluded": True,
                    "raw_sample_hash": row["raw_sample_hash"],
                    "dataset_revision": sample["source"]["dataset_revision"],
                },
            }
            records.append(record)
            canonical_rows.append({
                "sequence": request_index,
                "source_record_id": record["record_id"],
                "alignment": alignment,
                "benchmark_id": benchmark,
                "sample_id": sample_id,
                "prompt_hash": row["prompt_hash"],
                "output_hash": row["output_hash"],
                "actual_tokens": record["actual_tokens"],
                "quality": record["quality"],
                "measurement": record["measurement"],
            })
            write_json(
                destination / "benchmark_records" / pass_id.lower()
                / f"{record['record_id']}.json",
                record,
            )
        records_by_pass[pass_id] = records
        canonical_path = destination / "canonical_traces" / f"{pass_id.lower()}.json"
        input_content_ids = [item["content_id"] for item in raw_by_pass[pass_id]]
        write_json(canonical_path, {
            "schema_version": "m0-canonical-pass-v2",
            "pass_id": pass_id,
            "source_content_ids": input_content_ids,
            "measured_sample_count": len(canonical_rows),
            "warmup_excluded": True,
            "records": canonical_rows,
        })
        conversions.append({
            "canonical_path": relative(destination, canonical_path),
            "canonical_sha256": sha256_file(canonical_path),
            "input_content_ids": input_content_ids,
            "canonical_schema": {
                "schema_id": "canonical_pass_ir.schema.json",
                "path": "provenance/schemas/canonical_pass_ir.schema.json",
                "sha256": sha256_file(canonical_schema_path),
            },
            "converter": {
                "name": "ingest_benchmark_session",
                "version": "1",
                "source_hash": converter_hash,
            },
            "converted_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        })
    dump_jsonl(destination / "warmup/WARMUP_RECORDS.jsonl", warmup_rows)

    for name in (
        "m0_moe_routing.json", "m0_system_events.json", "m0_route_analysis.json"
    ):
        copy_file(source / "derived" / name, destination / "derived" / name)
    copy_file(
        source / "cross_pass_consistency.json",
        destination / "derived/cross_pass_consistency.json",
    )

    inventory_path = destination / "raw_traces/RAW_INVENTORY.json"
    write_json(inventory_path, {
        "schema_version": "trace-raw-inventory-v2",
        "digest_algorithm": "sha256",
        "content_addressed": True,
        "immutable": True,
        "canonical_contract": {
            "schema_id": "canonical_pass_ir.schema.json",
            "schema_path": relative(destination, canonical_schema_path),
            "schema_sha256": sha256_file(canonical_schema_path),
            "record_order": "records.sequence must equal zero-based array order",
            "source_id_policy": (
                "canonical source_content_ids must exactly equal conversion "
                "input_content_ids"
            ),
        },
        "entries": sorted(raw_entries, key=lambda item: item["content_id"]),
        "conversions": conversions,
    })

    required = ["P0", "P1", "P2", "P3", "P5"]
    optional = ["P4", "P6"]
    for pass_id, directory in PASSES.items():
        run_id = f"{session_id}-{pass_id.lower()}-r0"
        identity = {
            "session_id": session_id,
            "run_group_id": group_id,
            "run_id": run_id,
            "model_revision": revision,
            "workload_hash": workload_hash,
            "configuration_hash": configuration_hash,
            "environment_hash": environment_hash,
            "repetition_index": 0,
            "profiler_pass": pass_id,
        }
        if pass_id in MEASURED_PASSES:
            raw = raw_by_pass[pass_id]
            raw_observation, clock, sampling = pass_raw_observation(
                pass_id, pass_data[pass_id][0], raw, source
            )
            manifest = {
                "schema_version": "trace-pass-manifest-v2",
                "pass_id": pass_id,
                "status": "complete",
                "failure_reason": None,
                "requirement_class": "required",
                "identity": identity,
                "clock": clock,
                "raw_observation": raw_observation,
                "formal_evidence_status": (
                    "insufficient"
                    if pass_id == "P5" and sampling["status"] == "insufficient"
                    else (
                        "known_limitation"
                        if clock.get("status") == "known_limitation"
                        else "sufficient"
                    )
                ),
                "raw_artifacts": [{
                    "content_id": item["content_id"],
                    "path": item["path"],
                    "sha256": item["sha256"],
                    "bytes": item["bytes"],
                } for item in raw],
                "converter_provenance": {
                    "name": "ingest_benchmark_session",
                    "version": "1",
                    "source_hash": converter_hash,
                    "input_content_ids": [item["content_id"] for item in raw],
                },
                "benchmark_trace_records": records_by_pass[pass_id],
                "rerun_command": (
                    f"python3 scripts/ingest_benchmark_session.py --source {source} "
                    f"--destination {destination} --overwrite"
                ),
            }
            if sampling is not None:
                manifest["sampling_sufficiency"] = sampling
        else:
            evidence_source = source / pass_id.lower() / "manifest.json"
            evidence_path = copy_file(
                evidence_source,
                destination / "capabilities" / f"{pass_id.lower()}.json",
            )
            manifest = {
                "schema_version": "trace-pass-manifest-v2",
                "pass_id": pass_id,
                "status": "unsupported" if pass_id == "P4" else "optional_not_run",
                "failure_reason": (
                    "Nsight Compute executable not available in isolated runtime"
                    if pass_id == "P4"
                    else "no detailed simulator requested for M0 standard profile"
                ),
                "requirement_class": "conditional_optional",
                "identity": identity,
                "clock": {
                    "status": "known_limitation",
                    "reason": (
                        f"{pass_id} was not executed; no raw pass clock exists"
                    ),
                    "source_content_ids": [],
                },
                "raw_observation": {
                    field: raw_field(
                        None, [],
                        f"{pass_id} was not executed; raw {field} is unavailable",
                    )
                    for field in (
                        "environment", "gpu_uuid", "runtime", "start_utc", "end_utc"
                    )
                },
                "formal_evidence_status": "not_applicable",
                "raw_artifacts": [],
                "converter_provenance": {
                    "name": "not-run",
                    "version": "1",
                    "source_hash": converter_hash,
                    "input_content_ids": [],
                },
                "benchmark_trace_records": [],
                "rerun_command": (
                    "python3 collectors/gpu_counters.py --matrix "
                    "provenance/capture/frozen_matrix.json --profiler-pass P4 "
                    "--repetition-index 0"
                    if pass_id == "P4"
                    else "python3 collectors/detailed_optional.py --matrix "
                    "provenance/capture/frozen_matrix.json --profiler-pass P6 "
                    "--repetition-index 0"
                ),
                "collector_adapter": (
                    "collectors/gpu_counters.py"
                    if pass_id == "P4"
                    else "collectors/detailed_optional.py"
                ),
                "blocked_command": (
                    "python3 collectors/gpu_counters.py --matrix "
                    "provenance/capture/frozen_matrix.json --profiler-pass P4 "
                    "--repetition-index 0"
                    if pass_id == "P4"
                    else "python3 collectors/detailed_optional.py --matrix "
                    "provenance/capture/frozen_matrix.json --profiler-pass P6 "
                    "--repetition-index 0"
                ),
                "blocked_reason": (
                    "collector adapter collectors/gpu_counters.py is not implemented"
                    if pass_id == "P4"
                    else "collector adapter collectors/detailed_optional.py is not implemented"
                ),
            }
            if pass_id == "P4":
                manifest["capability_evidence"] = {
                    "capability": "ncu",
                    "available": False,
                    "evidence_path": relative(destination, evidence_path),
                    "evidence_sha256": sha256_file(evidence_path),
                }
        write_json(
            destination / "runs" / group_id / directory / "runs" / run_id
            / "PASS_MANIFEST.json",
            manifest,
        )

    frozen_matrix = json.loads(frozen_matrix_path.read_text(encoding="utf-8"))
    capture_plan = build_capture_plan(
        frozen_matrix, frozen_matrix_path, PACKAGE_ROOT
    )
    for state in capture_plan["states"]:
        if state["pass_id"] in MEASURED_PASSES:
            state["status"] = "complete"
            state["blocked_reason"] = None
            state["evidence_source"] = "ingested immutable raw benchmark capture"
        elif state["pass_id"] == "P6":
            state["status"] = "optional_not_run"
            state["blocked_reason"] = (
                "collector adapter collectors/detailed_optional.py is not implemented; "
                "P6 was optional for this pipeline smoke"
            )
    capture_plan_path = destination / "provenance/capture/CAPTURE_PLAN.json"
    write_json(capture_plan_path, capture_plan)

    session_manifest = {
        "schema_version": "trace-session-manifest-v2",
        "release_class": "pipeline_smoke",
        "capture_profile": "standard",
        "required_passes": required,
        "conditional_optional_passes": optional,
        "identity": {
            "session_id": session_id,
            "model_revision": revision,
            "workload_hash": workload_hash,
            "configuration_hash": configuration_hash,
            "environment_hash": environment_hash,
        },
        "required_repetitions": 1,
        "frozen_matrix": {
            "schema_version": "benchmark-capture-matrix-v1",
            "matrix_hash": canonical_hash(frozen_matrix),
            "path": relative(destination, frozen_matrix_path),
        },
        "capture_plan": {
            "path": relative(destination, capture_plan_path),
            "sha256": sha256_file(capture_plan_path),
        },
        "expected_runs": [{
            "run_group_id": group_id,
            "model_revision": revision,
            "workload_hash": workload_hash,
            "configuration_hash": configuration_hash,
            "environment_hash": environment_hash,
            "planned_passes": list(PASSES),
            "required_passes": required,
            "conditional_optional_passes": optional,
        }],
        "accepted_incomplete": False,
        "artifacts": {
            "environment": "environment/environment.json",
            "workload": "workloads/expanded_workload.json",
            "configuration": "models/configuration.json",
            "raw_inventory": "raw_traces/RAW_INVENTORY.json",
            "frozen_suite": "provenance/frozen_suite/v1.2.0/inventory.json",
            "selected_dataset": "datasets/selected_samples.jsonl",
            "model_inventory": "models/model_snapshot_inventory.json",
            "simulation_output": "derived/m0_route_analysis.json",
            "frozen_matrix": relative(destination, frozen_matrix_path),
            "capture_plan": relative(destination, capture_plan_path),
        },
    }
    write_json(destination / "SESSION_MANIFEST.json", session_manifest)
    payload = file_inventory(
        destination,
        {"RESULT_PACKAGE_MANIFEST.json", "checksums.sha256",
         "TRACE_COMPLETENESS_REPORT.json"},
    )
    write_json(destination / "RESULT_PACKAGE_MANIFEST.json", {
        "schema_version": "m0-result-package-manifest-v2",
        "profile": "standard",
        "release_class": "pipeline_smoke",
        "status": "pipeline_smoke_verified",
        "release_eligible": False,
        "release_ineligibility_reasons": [
            "global repetition count is 1; formal candidate/release requires at least 3",
            "P5 telemetry duration is below 5 seconds",
            "legacy pass streams omit per-pass start/end timestamps outside P5",
        ],
        "session_id": session_id,
        "measured_samples": 8,
        "measured_passes": list(MEASURED_PASSES),
        "benchmark_trace_record_count": 8 * len(MEASURED_PASSES),
        "warmup_record_count": len(warmup_rows),
        "optional_passes": {
            "P4": "unsupported_with_capability_evidence",
            "P6": "optional_not_run",
        },
        "file_coverage": {
            "excluded": [
                "RESULT_PACKAGE_MANIFEST.json",
                "checksums.sha256",
                "TRACE_COMPLETENESS_REPORT.json",
            ],
            "file_count": len(payload),
            "files": payload,
        },
    })
    write_checksums(destination)
    code, report = verify_root(destination)
    if code != COMPLETE:
        raise RuntimeError(json.dumps(report, indent=2))
    return {
        "session": str(destination),
        "session_bytes": sum(
            path.stat().st_size for path in destination.rglob("*") if path.is_file()
        ),
        "record_count": 40,
        "warmup_count": len(warmup_rows),
        "verify_status": report["status"],
        "finding_count": report["finding_count"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", type=Path,
        default=PACKAGE_ROOT / "artifacts/m0_benchmark_smoke",
    )
    parser.add_argument(
        "--destination", type=Path,
        default=PACKAGE_ROOT / "artifacts/m0_provenance_package/session",
    )
    parser.add_argument(
        "--suite", type=Path,
        default=PACKAGE_ROOT / "configs/test_suites/frozen/v1.2.0",
    )
    parser.add_argument(
        "--model-snapshot", type=Path,
        default=PACKAGE_ROOT / "models/snapshots"
        / "hf-internal-testing--tiny-random-Qwen2MoeForCausalLM"
        / "f736f270816032b3c721f7422c62dea1381f49d7",
    )
    parser.add_argument(
        "--dataset-inventory", type=Path,
        default=PACKAGE_ROOT / "datasets/snapshots/snapshot_inventory_v1.json",
    )
    parser.add_argument(
        "--matrix", type=Path,
        default=PACKAGE_ROOT / "configs/capture_matrices/m0_rtx3050_vertical_v1.json",
    )
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    result = ingest(
        args.source, args.destination, args.suite, args.model_snapshot,
        args.dataset_inventory, args.matrix, overwrite=args.overwrite,
    )
    audit = subprocess.run(
        [
            sys.executable, str(PACKAGE_ROOT / "scripts/trace_audit.py"),
            "--session-root", str(args.destination),
        ],
        check=False, capture_output=True, text=True,
    )
    if audit.returncode != COMPLETE:
        raise SystemExit(f"trace audit failed ({audit.returncode}):\n{audit.stdout}\n{audit.stderr}")
    audit_report = json.loads(audit.stdout)
    result["session_bytes"] = sum(
        path.stat().st_size
        for path in args.destination.resolve().rglob("*")
        if path.is_file() and not path.is_symlink()
    )
    archive = args.archive or args.destination.parent / "m0_provenance_package.tar.gz"
    create_archive(args.destination.resolve(), archive.resolve())
    with package_root(archive.resolve()) as extracted:
        archive_code, archive_report = verify_root(extracted)
    if archive_code != COMPLETE:
        raise SystemExit(f"archive verification failed: {archive_report}")
    result.update({
        "audit_status": audit_report["status"],
        "audit_findings": audit_report["finding_count"],
        "archive": str(archive.resolve()),
        "archive_bytes": archive.stat().st_size,
        "archive_sha256": sha256_file(archive),
        "archive_verify_status": archive_report["status"],
    })
    build_report = args.destination.parent / "INGEST_REPORT.json"
    write_json(build_report, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

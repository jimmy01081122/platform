#!/usr/bin/env python3
"""Verify a v2 trace directory or tar archive.

Exit 0 means complete, 10 means explicitly approved incomplete, and 20 means
verification failed.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tarfile
import tempfile
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))

from collectors.trace_contract import (  # noqa: E402
    HASH_FIELDS, HASH_RE, MANDATORY_GATE_PASSES, PASSES, STATUSES, Finding,
    canonical_hash, checksum_map, load_json, safe_member_name, sha256_file,
    iter_pass_manifests, validate_benchmark_trace_record, validate_clock,
    validate_identity, valid_utc,
)

COMPLETE = 0
APPROVED_INCOMPLETE = 10
FAILED = 20
OPTIONAL_PASS_STATUSES = {"unsupported", "not_applicable", "optional_not_run"}
VERIFIER_STATUSES = set(STATUSES) | {"optional_not_run"}
RELEASE_CLASSES = ("pipeline_smoke", "formal_candidate", "formal_release")
RELEASE_RANK = {name: index for index, name in enumerate(RELEASE_CLASSES)}
FORMAL_REQUIRED_PASSES = {"P0", "P1", "P2", "P3", "P5"}
FORMAL_OPTIONAL_PASSES = {"P4", "P6"}
REQUIRED_MAPE_METRICS = {
    "component_latency",
    "pcie_transfer_latency",
    "moe_replay_tpot",
    "moe_replay_throughput",
}


def add(
    findings: list[Finding], finding_id: str, message: str, *,
    path: str | None = None, rerun: str | None = None, waivable: bool = True,
    severity: str = "error", details: dict[str, Any] | None = None,
) -> None:
    findings.append(Finding(
        finding_id, severity, message, path, rerun, waivable, details or {}
    ))


def relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def schema_errors(
    instance: dict, schema_name: str, *, require_jsonschema: bool = False,
) -> list[str]:
    errors: list[str] = []
    if schema_name.startswith("session_"):
        if instance.get("schema_version") != "trace-session-manifest-v2":
            errors.append("schema_version must be trace-session-manifest-v2")
        identity = instance.get("identity")
        if not isinstance(identity, dict):
            errors.append("identity must be an object")
        else:
            for name in (
                "session_id", "model_revision", "workload_hash",
                "configuration_hash", "environment_hash",
            ):
                if identity.get(name) in ("", None):
                    errors.append(f"identity.{name} is required")
            for name in HASH_FIELDS:
                if not HASH_RE.fullmatch(str(identity.get(name, ""))):
                    errors.append(f"identity.{name} must be lowercase SHA-256")
        if not isinstance(instance.get("accepted_incomplete"), bool):
            errors.append("accepted_incomplete must be boolean")
        if not isinstance(instance.get("artifacts"), dict):
            errors.append("artifacts must be an object")
        repetitions = instance.get("required_repetitions")
        if (
            not isinstance(repetitions, int)
            or isinstance(repetitions, bool)
            or repetitions < 1
        ):
            errors.append("required_repetitions must be a positive integer")
        expected = instance.get("expected_runs")
        if not isinstance(expected, list) or not expected:
            errors.append("expected_runs must be a non-empty array")
    elif schema_name.startswith("pass_"):
        if instance.get("schema_version") != "trace-pass-manifest-v2":
            errors.append("schema_version must be trace-pass-manifest-v2")
        if instance.get("pass_id") not in PASSES:
            errors.append("pass_id must be P0 through P6")
        if instance.get("status") not in VERIFIER_STATUSES:
            errors.append("status is not in the v2 status enum")
        if not isinstance(instance.get("raw_artifacts"), list):
            errors.append("raw_artifacts must be an array")
        provenance = instance.get("converter_provenance")
        if not isinstance(provenance, dict):
            errors.append("converter_provenance must be an object")
        else:
            for name in ("name", "version"):
                if not isinstance(provenance.get(name), str) or not provenance[name]:
                    errors.append(f"converter_provenance.{name} is required")
            if not HASH_RE.fullmatch(str(provenance.get("source_hash", ""))):
                errors.append("converter_provenance.source_hash must be lowercase SHA-256")
            if not isinstance(provenance.get("input_content_ids"), list):
                errors.append("converter_provenance.input_content_ids must be an array")
        if not isinstance(instance.get("rerun_command"), str) or not instance["rerun_command"]:
            errors.append("rerun_command is required")
    schema_path = PACKAGE_ROOT / "schemas" / schema_name
    try:
        schema = load_json(schema_path)
    except Exception as exc:
        return errors + [f"cannot load bundled schema: {exc}"]
    try:
        import jsonschema
    except ImportError:
        if require_jsonschema:
            errors.append(
                "jsonschema runtime is mandatory for formal verification"
            )
        return errors
    validator_type = getattr(jsonschema, "Draft202012Validator", None)
    if validator_type is None:
        validator_type = getattr(jsonschema, "Draft7Validator", None)
    if validator_type is None:
        if require_jsonschema:
            errors.append("jsonschema has no supported Draft validator")
        return errors
    validator = validator_type(
        schema, format_checker=jsonschema.FormatChecker()
    )
    errors.extend([
        f"{'/'.join(str(part) for part in error.absolute_path) or '$'}: "
        f"{error.message}"
        for error in sorted(validator.iter_errors(instance), key=lambda e: list(e.path))
    ])
    return list(dict.fromkeys(errors))


def validate_benchmark_record(
    root: Path, record: dict[str, Any], source: str, findings: list[Finding],
    *, rerun: str | None = None, estimate_minutes: float | None = None,
) -> None:
    details = {"estimate_minutes": estimate_minutes} if estimate_minutes else {}
    errors = validate_benchmark_trace_record(record)
    errors.extend(schema_errors(record, "benchmark_trace_record.schema.json"))
    for error in dict.fromkeys(errors):
        add(
            findings, "TRACE.BENCHMARK.SCHEMA_INVALID", error, path=source,
            rerun=rerun, waivable=False, details=details,
        )
    generation = record.get("generation_config")
    if isinstance(generation, dict) and canonical_hash(generation) != record.get(
        "generation_config_hash"
    ):
        add(
            findings, "TRACE.BENCHMARK.GENERATION_HASH_MISMATCH",
            "generation_config_hash does not match canonical generation_config",
            path=source, rerun=rerun, waivable=False, details=details,
        )
    for path_field, hash_field, finding_name in (
        ("prompt_path", "prompt_hash", "PROMPT"),
        ("output_path", "output_hash", "OUTPUT"),
    ):
        rel = record.get(path_field)
        if rel is None:
            continue
        if not isinstance(rel, str) or not safe_member_name(rel):
            add(findings, f"TRACE.BENCHMARK.{finding_name}_PATH_INVALID",
                f"{path_field} is unsafe", path=source, rerun=rerun,
                waivable=False, details=details)
            continue
        artifact = root / rel
        if not artifact.is_file() or artifact.is_symlink():
            add(findings, f"TRACE.BENCHMARK.{finding_name}_MISSING",
                f"{path_field} does not exist", path=rel, rerun=rerun,
                waivable=False, details=details)
        elif artifact.stat().st_size == 0:
            add(findings, f"TRACE.BENCHMARK.{finding_name}_EMPTY",
                f"{path_field} is empty", path=rel, rerun=rerun,
                waivable=False, details=details)
        elif sha256_file(artifact) != record.get(hash_field):
            add(findings, f"TRACE.BENCHMARK.{finding_name}_HASH_MISMATCH",
                f"{path_field} does not match {hash_field}", path=rel,
                rerun=rerun, waivable=False, details=details)
    native_paths = record.get("native_paths")
    native_checksums = record.get("native_checksums", {})
    if isinstance(native_paths, list):
        for rel in native_paths:
            if not isinstance(rel, str) or not safe_member_name(rel):
                continue
            artifact = root / rel
            if not artifact.is_file() or artifact.is_symlink():
                add(findings, "TRACE.BENCHMARK.NATIVE_MISSING",
                    "record native artifact is missing", path=rel, rerun=rerun,
                    waivable=False, details=details)
            elif artifact.stat().st_size == 0:
                add(findings, "TRACE.BENCHMARK.NATIVE_EMPTY",
                    "record native artifact is empty", path=rel, rerun=rerun,
                    waivable=False, details=details)
            elif sha256_file(artifact) != (
                native_checksums.get(rel)
                if isinstance(native_checksums, dict) else record.get("native_sha256")
            ):
                add(findings, "TRACE.BENCHMARK.NATIVE_HASH_MISMATCH",
                    "native artifact does not match native_sha256", path=rel,
                    rerun=rerun, waivable=False, details=details)
    completeness = record.get("completeness")
    if isinstance(completeness, dict):
        if completeness.get("truncated") is True:
            add(findings, "TRACE.BENCHMARK.TRUNCATED",
                "benchmark trace record is truncated", path=source, rerun=rerun,
                waivable=False, details=details)
        if completeness.get("complete") is not True:
            pass_id = record.get("profiler_pass")
            add(findings, "TRACE.BENCHMARK.INCOMPLETE",
                "benchmark trace record is incomplete", path=source, rerun=rerun,
                waivable=pass_id not in MANDATORY_GATE_PASSES,
                severity="incomplete", details=details)


def validate_approval(session: dict, findings: list[Finding]) -> bool:
    accepted = session.get("accepted_incomplete") is True
    if not accepted:
        return False
    approval = session.get("approval")
    valid = (
        isinstance(approval, dict)
        and all(isinstance(approval.get(key), str) and approval[key].strip()
                for key in ("approved_by", "approved_utc", "reason"))
        and valid_utc(approval.get("approved_utc"))
    )
    if not valid:
        add(
            findings, "TRACE.APPROVAL.MISSING",
            "accepted_incomplete requires approved_by, approved_utc, and reason",
            path="SESSION_MANIFEST.json", waivable=False,
        )
        return False
    return True


def validate_core_artifacts(
    root: Path, session: dict, findings: list[Finding]
) -> dict[str, Path]:
    artifacts = session.get("artifacts")
    result: dict[str, Path] = {}
    for name in ("environment", "workload", "configuration", "raw_inventory"):
        rel = artifacts.get(name) if isinstance(artifacts, dict) else None
        if not isinstance(rel, str) or not safe_member_name(rel):
            add(
                findings, f"TRACE.CORE.{name.upper()}.MISSING",
                f"session artifact {name} has no safe path",
                path="SESSION_MANIFEST.json", waivable=False,
            )
            continue
        path = root / rel
        if not path.is_file() or path.is_symlink():
            add(
                findings, f"TRACE.CORE.{name.upper()}.MISSING",
                f"mandatory {name} artifact is missing",
                path=rel, waivable=False,
            )
            continue
        if path.stat().st_size == 0:
            add(
                findings, f"TRACE.CORE.{name.upper()}.EMPTY",
                f"mandatory {name} artifact is empty",
                path=rel, waivable=False,
            )
            continue
        result[name] = path
    identity = session.get("identity", {})
    for name, field_name in (
        ("environment", "environment_hash"),
        ("workload", "workload_hash"),
        ("configuration", "configuration_hash"),
    ):
        path = result.get(name)
        if path and sha256_file(path) != identity.get(field_name):
            add(
                findings, f"TRACE.CORE.{name.upper()}.HASH_MISMATCH",
                f"{name} artifact does not match identity.{field_name}",
                path=relative(root, path), waivable=False,
            )
    return result


def validate_inventory(
    root: Path, inventory_path: Path | None, findings: list[Finding],
    *, formal: bool,
) -> tuple[dict[str, dict], list[dict]]:
    if not inventory_path:
        return {}, []
    try:
        inventory = load_json(inventory_path)
    except Exception as exc:
        add(
            findings, "TRACE.RAW.INVENTORY_CORRUPT",
            f"raw inventory is not valid JSON: {exc}",
            path=relative(root, inventory_path), waivable=False,
        )
        return {}, []
    if (
        inventory.get("schema_version") != "trace-raw-inventory-v2"
        or inventory.get("content_addressed") is not True
        or inventory.get("immutable") is not True
    ):
        add(
            findings, "TRACE.RAW.INVENTORY_SCHEMA",
            "raw inventory does not satisfy the v2 immutable content-addressed contract",
            path=relative(root, inventory_path), waivable=False,
        )
    entries = inventory.get("entries")
    if not isinstance(entries, list) or not entries:
        add(
            findings, "TRACE.RAW.MISSING",
            "at least one native raw trace is mandatory",
            path=relative(root, inventory_path), waivable=False,
        )
        return {}, []
    indexed: dict[str, dict] = {}
    for index, entry in enumerate(entries):
        prefix = f"raw inventory entry {index}"
        if not isinstance(entry, dict):
            add(findings, "TRACE.RAW.ENTRY_SCHEMA", f"{prefix} is not an object",
                waivable=False)
            continue
        rel = entry.get("path")
        content_id = entry.get("content_id")
        if not isinstance(rel, str) or not safe_member_name(rel):
            add(findings, "TRACE.RAW.PATH_UNSAFE", f"{prefix} has unsafe path",
                waivable=False)
            continue
        path = root / rel
        if not path.is_file() or path.is_symlink():
            add(findings, "TRACE.RAW.FILE_MISSING", "inventoried raw file is missing",
                path=rel, waivable=False)
            continue
        actual_size = path.stat().st_size
        if actual_size == 0:
            add(findings, "TRACE.RAW.EMPTY", "raw trace is empty", path=rel,
                waivable=False)
        if entry.get("truncated") is True:
            add(findings, "TRACE.RAW.TRUNCATED", "raw trace is marked truncated",
                path=rel, waivable=False)
        actual_hash = sha256_file(path)
        if (
            content_id != actual_hash
            or entry.get("sha256") != actual_hash
            or entry.get("bytes") != actual_size
        ):
            add(
                findings, "TRACE.RAW.CONTENT_MISMATCH",
                "raw size, checksum, or content address is inconsistent",
                path=rel, waivable=False,
            )
        elif content_id in indexed:
            add(findings, "TRACE.RAW.DUPLICATE_CONTENT_ID",
                f"duplicate content ID {content_id}", path=rel, waivable=False)
        else:
            indexed[content_id] = entry
    canonical_contract = inventory.get("canonical_contract")
    canonical_schema = None
    canonical_schema_path = None
    if not isinstance(canonical_contract, dict):
        add(
            findings, "TRACE.CANONICAL.CONTRACT_MISSING",
            "raw inventory must declare canonical schema, order, and source-ID policy",
            path=relative(root, inventory_path), waivable=False,
        )
    else:
        schema_rel = canonical_contract.get("schema_path")
        if not isinstance(schema_rel, str) or not safe_member_name(schema_rel):
            add(findings, "TRACE.CANONICAL.SCHEMA_PATH_INVALID",
                "canonical schema path is missing or unsafe", waivable=False)
        else:
            canonical_schema_path = root / schema_rel
            try:
                canonical_schema = load_json(canonical_schema_path)
            except Exception as exc:
                add(findings, "TRACE.CANONICAL.SCHEMA_MISSING",
                    f"cannot load declared canonical schema: {exc}",
                    path=schema_rel, waivable=False)
            else:
                if sha256_file(canonical_schema_path) != canonical_contract.get(
                    "schema_sha256"
                ):
                    add(findings, "TRACE.CANONICAL.SCHEMA_HASH_MISMATCH",
                        "canonical schema checksum differs from inventory declaration",
                        path=schema_rel, waivable=False)
    conversions = inventory.get("conversions", [])
    if not isinstance(conversions, list):
        add(findings, "TRACE.CONVERTER.INVENTORY_SCHEMA",
            "inventory conversions must be an array", waivable=False)
        conversions = []
    for index, conversion in enumerate(conversions):
        if not isinstance(conversion, dict):
            add(findings, "TRACE.CONVERTER.ENTRY_SCHEMA",
                f"conversion {index} is not an object", waivable=False)
            continue
        rel = conversion.get("canonical_path")
        if not isinstance(rel, str) or not safe_member_name(rel):
            add(findings, "TRACE.CANONICAL.PATH_INVALID",
                f"conversion {index} has unsafe canonical_path", waivable=False)
            continue
        canonical = root / rel
        if not canonical.is_file() or canonical.is_symlink():
            add(findings, "TRACE.CANONICAL.MISSING",
                "canonical conversion output is missing", path=rel, waivable=False)
        elif canonical.stat().st_size == 0:
            add(findings, "TRACE.CANONICAL.EMPTY",
                "canonical conversion output is empty", path=rel, waivable=False)
        elif sha256_file(canonical) != conversion.get("canonical_sha256"):
            add(findings, "TRACE.CANONICAL.HASH_MISMATCH",
                "canonical conversion checksum does not match", path=rel,
                waivable=False)
        else:
            try:
                canonical_value = load_json(canonical)
            except Exception as exc:
                add(findings, "TRACE.CANONICAL.CORRUPT", str(exc), path=rel,
                    waivable=False)
                canonical_value = {}
            declaration = conversion.get("canonical_schema")
            if not isinstance(declaration, dict):
                add(findings, "TRACE.CANONICAL.SCHEMA_UNDECLARED",
                    "conversion must declare its canonical schema", path=rel,
                    waivable=False)
            elif canonical_schema is not None:
                if (
                    declaration.get("path")
                    != canonical_contract.get("schema_path")
                    or declaration.get("sha256")
                    != canonical_contract.get("schema_sha256")
                ):
                    add(findings, "TRACE.CANONICAL.SCHEMA_DECLARATION_MISMATCH",
                        "conversion schema declaration differs from inventory contract",
                        path=rel, waivable=False)
                try:
                    import jsonschema
                except ImportError:
                    add(findings, "TRACE.CANONICAL.JSONSCHEMA_MISSING",
                        "jsonschema runtime is unavailable; canonical IR was not validated",
                        path=rel, waivable=not formal,
                        severity="error" if formal else "warning")
                else:
                    validator_type = getattr(
                        jsonschema, "Draft202012Validator", None
                    ) or getattr(jsonschema, "Draft7Validator", None)
                    if validator_type is None:
                        add(findings, "TRACE.CANONICAL.JSONSCHEMA_MISSING",
                            "jsonschema has no supported Draft validator",
                            path=rel, waivable=not formal,
                            severity="error" if formal else "warning")
                    else:
                        validator = validator_type(canonical_schema)
                        for error in validator.iter_errors(canonical_value):
                            add(findings, "TRACE.CANONICAL.SCHEMA_INVALID",
                                f"{'/'.join(map(str, error.absolute_path)) or '$'}: "
                                f"{error.message}", path=rel, waivable=False)
            source_ids = canonical_value.get("source_content_ids")
            if source_ids is None and isinstance(
                canonical_value.get("provenance"), dict
            ):
                source_ids = canonical_value["provenance"].get(
                    "source_content_ids"
                )
            inputs = conversion.get("input_content_ids")
            if source_ids != inputs:
                add(findings, "TRACE.CANONICAL.SOURCE_IDS_MISMATCH",
                    "canonical source_content_ids must exactly match conversion inputs",
                    path=rel, waivable=False)
            records = canonical_value.get("records")
            if isinstance(records, list) and [
                item.get("sequence") if isinstance(item, dict) else None
                for item in records
            ] != list(range(len(records))):
                add(findings, "TRACE.CANONICAL.ORDER_INVALID",
                    "canonical record sequence must match zero-based array order",
                    path=rel, waivable=False)
            events = canonical_value.get("events")
            if isinstance(events, list) and [
                item.get("sequence") if isinstance(item, dict) else None
                for item in events
            ] != list(range(len(events))):
                add(findings, "TRACE.CANONICAL.ORDER_INVALID",
                    "canonical event sequence must match zero-based array order",
                    path=rel, waivable=False)
        converter = conversion.get("converter")
        if not isinstance(converter, dict) or not all(
            converter.get(name) for name in ("name", "version", "source_hash")
        ):
            add(findings, "TRACE.CONVERTER.PROVENANCE_INVALID",
                f"conversion {index} lacks converter provenance", path=rel,
                waivable=False)
    return indexed, conversions


def validate_passes(
    root: Path, session: dict, inventory: dict[str, dict],
    conversions: list[dict], findings: list[Finding],
    *, release_class: str,
) -> bool:
    runs = root / "runs"
    if not runs.is_dir():
        add(findings, "TRACE.RUNS.MISSING", "runs directory is missing",
            path="runs", waivable=False)
        return False
    groups = sorted(path for path in runs.iterdir() if path.is_dir())
    if not groups:
        add(findings, "TRACE.RUN_GROUP.MISSING", "no run group exists",
            path="runs", waivable=False)
        return False
    session_identity = session.get("identity", {})
    profile = session.get("capture_profile")
    policy_enabled = (
        profile in {"standard", "maximal_paid"}
        or release_class != "pipeline_smoke"
    )
    session_required = session.get("required_passes", [])
    session_optional = session.get("conditional_optional_passes", [])
    if policy_enabled:
        if not isinstance(session_required, list) or not isinstance(
            session_optional, list
        ):
            add(findings, "TRACE.PROFILE.PASS_POLICY_INVALID",
                "required_passes and conditional_optional_passes must be arrays",
                path="SESSION_MANIFEST.json", waivable=False)
            session_required, session_optional = [], []
        if not MANDATORY_GATE_PASSES.issubset(set(session_required)):
            add(findings, "TRACE.PROFILE.MANDATORY_GATE_OMITTED",
                "P0, P2, and P3 must always be required",
                path="SESSION_MANIFEST.json", waivable=False)
        if not FORMAL_REQUIRED_PASSES.issubset(set(session_required)):
            add(findings, "TRACE.PROFILE.REQUIRED_PASS_OMITTED",
                f"{profile} requires {sorted(FORMAL_REQUIRED_PASSES)}",
                path="SESSION_MANIFEST.json", waivable=False)
        if not FORMAL_OPTIONAL_PASSES.issubset(set(session_optional)):
            add(findings, "TRACE.PROFILE.OPTIONAL_PASS_POLICY_INVALID",
                "P4 and P6 must be declared conditional optional",
                path="SESSION_MANIFEST.json", waivable=False)
        if set(session_required) & FORMAL_OPTIONAL_PASSES:
            add(findings, "TRACE.PROFILE.OPTIONAL_PASS_REQUIRED",
                "P4 and P6 cannot be formal required passes",
                path="SESSION_MANIFEST.json", waivable=False)
    expected_runs = session.get("expected_runs")
    if not isinstance(expected_runs, list) or not expected_runs:
        add(findings, "TRACE.RUN_PLAN.MISSING",
            "expected_runs must list every planned model/workload/configuration",
            path="SESSION_MANIFEST.json", waivable=False)
        expected_runs = []
    expected_by_group: dict[str, dict] = {}
    for item in expected_runs:
        group_id = item.get("run_group_id") if isinstance(item, dict) else None
        if not isinstance(group_id, str) or not group_id:
            add(findings, "TRACE.RUN_PLAN.GROUP_ID_INVALID",
                "every expected run needs a non-empty run_group_id",
                path="SESSION_MANIFEST.json", waivable=False)
            continue
        if group_id in expected_by_group:
            add(findings, "TRACE.RUN_PLAN.GROUP_ID_DUPLICATE",
                f"duplicate planned run_group_id {group_id}",
                path="SESSION_MANIFEST.json", waivable=False)
        expected_by_group[group_id] = item
    actual_groups = {path.name for path in groups}
    for group_id in sorted(set(expected_by_group) - actual_groups):
        add(
            findings, "TRACE.RUN_PLAN.STATUS_MISSING",
            f"planned run group {group_id} has no run status",
            path=f"runs/{group_id}",
            rerun=f"./run.sh --run-group {group_id} --trace-profile maximal --resume",
            waivable=False,
        )
    required_repetitions = session.get("required_repetitions", 0)
    coverage: dict[tuple[str, str], set[int]] = defaultdict(set)
    seen_run_ids: dict[str, str] = {}
    incomplete = False
    for group in groups:
        group_id = group.name
        planned = expected_by_group.get(group_id)
        if planned is None:
            add(findings, "TRACE.RUN_PLAN.UNPLANNED_GROUP",
                f"run group {group_id} is absent from expected_runs",
                path=relative(root, group), waivable=False)
        planned_passes = (
            planned.get("planned_passes", []) if isinstance(planned, dict) else []
        )
        required_passes = (
            planned.get("required_passes", session_required)
            if isinstance(planned, dict) else session_required
        )
        optional_passes = (
            planned.get("conditional_optional_passes", session_optional)
            if isinstance(planned, dict) else session_optional
        )
        if (
            not isinstance(planned_passes, list)
            or not planned_passes
            or any(pass_id not in PASSES for pass_id in planned_passes)
            or len(set(planned_passes)) != len(planned_passes)
        ):
            add(findings, "TRACE.RUN_PLAN.PASSES_INVALID",
                f"run group {group_id} has invalid planned_passes",
                path="SESSION_MANIFEST.json", waivable=False)
            planned_passes = []
        for pass_id, directory in PASSES.items():
            pass_root = group / directory
            manifest_paths = sorted(
                pass_root.glob("runs/*/PASS_MANIFEST.json")
            )
            rerun = (
                f"./run.sh --run-group {group_id} --profiler-pass "
                f"{pass_id.lower()} --resume"
            )
            is_planned = pass_id in planned_passes
            if policy_enabled and not manifest_paths:
                add(
                    findings, f"TRACE.{pass_id}.MANIFEST_MISSING",
                    f"profile requires a manifest for {pass_id}, including optional passes",
                    path=relative(root, pass_root / "runs"),
                    rerun=rerun, waivable=False,
                )
            elif is_planned and not manifest_paths:
                add(
                    findings, f"TRACE.{pass_id}.MANIFEST_MISSING",
                    f"planned {pass_id} has no repetition manifest",
                    path=relative(root, pass_root / "runs"),
                    rerun=rerun,
                    waivable=pass_id not in MANDATORY_GATE_PASSES,
                    severity="incomplete",
                )
            if not is_planned and manifest_paths:
                add(
                    findings, f"TRACE.{pass_id}.UNPLANNED_PASS",
                    f"{pass_id} has manifests but is absent from planned_passes",
                    path=relative(root, pass_root), waivable=False,
                )
            for manifest_path in manifest_paths:
                if manifest_path.is_symlink():
                    add(findings, f"TRACE.{pass_id}.MANIFEST_SYMLINK",
                        "pass manifest cannot be a symlink",
                        path=relative(root, manifest_path), waivable=False)
                    continue
                try:
                    manifest = load_json(manifest_path)
                except Exception as exc:
                    add(
                        findings, f"TRACE.{pass_id}.MANIFEST_CORRUPT",
                        f"{pass_id} manifest is invalid JSON: {exc}",
                        path=relative(root, manifest_path), rerun=rerun,
                        waivable=False,
                    )
                    continue
                for error in schema_errors(
                    manifest, "pass_manifest.schema.json",
                    require_jsonschema=release_class != "pipeline_smoke",
                ):
                    add(
                        findings, f"TRACE.{pass_id}.SCHEMA_INVALID", error,
                        path=relative(root, manifest_path), rerun=rerun,
                        waivable=False,
                    )
                if manifest.get("pass_id") != pass_id:
                    add(findings, f"TRACE.{pass_id}.PASS_ID_MISMATCH",
                        "pass_id does not match directory",
                        path=relative(root, manifest_path), rerun=rerun,
                        waivable=False)
                status = manifest.get("status")
                rerun_command = manifest.get("rerun_command")
                if pass_id in {"P4", "P6"} and (
                    not isinstance(rerun_command, str)
                    or "--help" in rerun_command
                ):
                    add(findings, f"TRACE.{pass_id}.RERUN_COMMAND_INVALID",
                        "P4/P6 rerun command must be an executable capture command, not help",
                        path=relative(root, manifest_path), waivable=False)
                adapter = manifest.get("collector_adapter")
                if pass_id in {"P4", "P6"} and isinstance(adapter, str):
                    if not (PACKAGE_ROOT / adapter).is_file() and (
                        manifest.get("blocked_command") != rerun_command
                        or not isinstance(manifest.get("blocked_reason"), str)
                        or not manifest["blocked_reason"].strip()
                    ):
                        add(findings, f"TRACE.{pass_id}.BLOCKED_COMMAND_INVALID",
                            "missing adapter requires exact blocked_command and reason",
                            path=relative(root, manifest_path), waivable=False)
                if status not in VERIFIER_STATUSES:
                    add(findings, f"TRACE.{pass_id}.STATUS_INVALID",
                        f"invalid status {status!r}",
                        path=relative(root, manifest_path), rerun=rerun,
                        waivable=False)
                elif status != "complete":
                    optional_status_valid = (
                        policy_enabled
                        and (
                            pass_id in optional_passes
                            or (
                                profile == "maximal_paid"
                                and pass_id == "P4"
                                and status == "unsupported"
                            )
                        )
                        and status in OPTIONAL_PASS_STATUSES
                        and isinstance(manifest.get("failure_reason"), str)
                        and bool(manifest["failure_reason"].strip())
                    )
                    capability_valid = True
                    if optional_status_valid and pass_id == "P4":
                        evidence = manifest.get("capability_evidence")
                        capability_valid = (
                            isinstance(evidence, dict)
                            and evidence.get("capability") == "ncu"
                            and evidence.get("available") is False
                            and isinstance(evidence.get("evidence_path"), str)
                            and safe_member_name(evidence["evidence_path"])
                        )
                        if capability_valid:
                            evidence_path = root / evidence["evidence_path"]
                            capability_valid = (
                                evidence_path.is_file()
                                and not evidence_path.is_symlink()
                                and evidence_path.stat().st_size > 0
                                and sha256_file(evidence_path)
                                == evidence.get("evidence_sha256")
                            )
                        if not capability_valid:
                            add(findings, "TRACE.P4.CAPABILITY_EVIDENCE_INVALID",
                                "unsupported P4 requires hashed ncu-unavailable evidence",
                                path=relative(root, manifest_path), rerun=rerun,
                                waivable=False)
                    if optional_status_valid and capability_valid:
                        pass
                    else:
                        incomplete = True
                        add(
                            findings, f"TRACE.{pass_id}.STATUS_{status.upper()}",
                            manifest.get("failure_reason")
                            or f"{pass_id} status is {status}",
                            path=relative(root, manifest_path),
                            rerun=manifest.get("rerun_command") or rerun,
                            waivable=pass_id not in MANDATORY_GATE_PASSES,
                            severity="incomplete",
                        )
                identity = manifest.get("identity")
                for error in validate_identity(identity, pass_id):
                    add(findings, f"TRACE.{pass_id}.IDENTITY_INVALID", error,
                        path=relative(root, manifest_path), rerun=rerun,
                        waivable=False)
                if isinstance(identity, dict):
                    run_id = identity.get("run_id")
                    path_run_id = manifest_path.parent.name
                    if run_id != path_run_id:
                        add(findings, "TRACE.RUN_ID.PATH_MISMATCH",
                            f"identity.run_id {run_id!r} differs from run directory",
                            path=relative(root, manifest_path), waivable=False)
                    if isinstance(run_id, str) and run_id:
                        prior = seen_run_ids.get(run_id)
                        if prior is not None:
                            add(
                                findings, "TRACE.RUN_ID.DUPLICATE",
                                f"run_id {run_id} also appears at {prior}",
                                path=relative(root, manifest_path),
                                waivable=False,
                            )
                        seen_run_ids[run_id] = relative(root, manifest_path)
                    if identity.get("session_id") != session_identity.get("session_id"):
                        add(findings, "TRACE.IDENTITY.SESSION_MISMATCH",
                            f"{pass_id} session_id differs from session manifest",
                            path=relative(root, manifest_path), waivable=False)
                    if identity.get("run_group_id") != group_id:
                        add(findings, "TRACE.IDENTITY.RUN_GROUP_MISMATCH",
                            f"{pass_id} run_group_id differs from directory",
                            path=relative(root, manifest_path), waivable=False)
                    if planned:
                        for field_name in (
                            "model_revision", "workload_hash",
                            "configuration_hash", "environment_hash",
                        ):
                            if identity.get(field_name) != planned.get(field_name):
                                finding_id = (
                                    f"TRACE.IDENTITY.{field_name.upper()}_MISMATCH"
                                    if field_name.endswith("_hash")
                                    else f"TRACE.RUN_PLAN.{field_name.upper()}_MISMATCH"
                                )
                                add(
                                    findings,
                                    finding_id,
                                    f"{pass_id} {field_name} differs across passes",
                                    path=relative(root, manifest_path),
                                    waivable=False,
                                )
                    repetition = identity.get("repetition_index")
                    if isinstance(repetition, int) and not isinstance(repetition, bool):
                        repetition_key = (group_id, pass_id)
                        if repetition in coverage[repetition_key]:
                            add(
                                findings, f"TRACE.{pass_id}.REPETITION_DUPLICATE",
                                f"duplicate repetition_index {repetition}",
                                path=relative(root, manifest_path),
                                rerun=rerun,
                                waivable=pass_id not in MANDATORY_GATE_PASSES,
                            )
                        coverage[repetition_key].add(repetition)
                for error in validate_clock(manifest.get("clock")):
                    add(findings, f"TRACE.{pass_id}.CLOCK_INVALID", error,
                        path=relative(root, manifest_path), rerun=rerun,
                        waivable=False)
                if release_class != "pipeline_smoke" and status == "complete":
                    observation = manifest.get("raw_observation")
                    for field_name in (
                        "environment", "gpu_uuid", "runtime", "start_utc", "end_utc"
                    ):
                        field = (
                            observation.get(field_name)
                            if isinstance(observation, dict) else None
                        )
                        if not isinstance(field, dict) or field.get("status") != "observed":
                            add(findings, f"TRACE.{pass_id}.RAW_{field_name.upper()}_MISSING",
                                f"formal verification requires raw-observed {field_name}",
                                path=relative(root, manifest_path), waivable=False)
                    if manifest.get("clock", {}).get("status") == "known_limitation":
                        add(findings, f"TRACE.{pass_id}.RAW_CLOCK_LIMITATION",
                            "formal verification cannot accept a known-limitation pass clock",
                            path=relative(root, manifest_path), waivable=False)
                    if pass_id == "P5":
                        sampling = manifest.get("sampling_sufficiency", {})
                        if (
                            sampling.get("status") != "sufficient"
                            or not isinstance(sampling.get("duration_seconds"), (int, float))
                            or sampling.get("duration_seconds", 0) < 5
                            or not isinstance(sampling.get("sample_count"), int)
                            or sampling.get("sample_count", 0)
                            < sampling.get("minimum_sample_count", 20)
                        ):
                            add(findings, "TRACE.P5.SAMPLING_INSUFFICIENT",
                                "formal P5 requires at least 5 seconds and the minimum sample count",
                                path=relative(root, manifest_path), waivable=False)
                elif pass_id == "P5" and manifest.get(
                    "sampling_sufficiency", {}
                ).get("status") == "insufficient":
                    add(findings, "TRACE.P5.SAMPLING_INSUFFICIENT",
                        "P5 is retained for pipeline smoke but is insufficient for formal use",
                        path=relative(root, manifest_path), waivable=True,
                        severity="warning")
                if status != "complete":
                    continue
                records = manifest.get("benchmark_trace_records", [])
                if records is not None and not isinstance(records, list):
                    add(findings, f"TRACE.{pass_id}.BENCHMARK_RECORDS_INVALID",
                        "benchmark_trace_records must be an array",
                        path=relative(root, manifest_path), rerun=rerun,
                        waivable=False)
                elif isinstance(records, list):
                    for record_index, record in enumerate(records):
                        if not isinstance(record, dict):
                            add(findings, f"TRACE.{pass_id}.BENCHMARK_RECORD_INVALID",
                                f"record {record_index} is not an object",
                                path=relative(root, manifest_path), rerun=rerun,
                                waivable=False)
                            continue
                        validate_benchmark_record(
                            root, record,
                            f"{relative(root, manifest_path)}#benchmark_trace_records/"
                            f"{record_index}",
                            findings, rerun=rerun,
                            estimate_minutes=manifest.get("estimate_minutes"),
                        )
                raw = manifest.get("raw_artifacts")
                if not isinstance(raw, list) or not raw:
                    add(findings, f"TRACE.{pass_id}.RAW_MISSING",
                        "complete pass must reference native raw artifacts",
                        path=relative(root, manifest_path), rerun=rerun,
                        waivable=False)
                    continue
                raw_ids = set()
                for artifact in raw:
                    content_id = (
                        artifact.get("content_id")
                        if isinstance(artifact, dict) else None
                    )
                    if content_id not in inventory:
                        add(findings, f"TRACE.{pass_id}.RAW_NOT_IN_INVENTORY",
                            f"raw content ID {content_id!r} is not inventoried",
                            path=relative(root, manifest_path), rerun=rerun,
                            waivable=False)
                    else:
                        raw_ids.add(content_id)
                provenance = manifest.get("converter_provenance")
                inputs = (
                    provenance.get("input_content_ids")
                    if isinstance(provenance, dict) else []
                )
                if not raw_ids or not raw_ids.issubset(set(inputs or [])):
                    add(findings, f"TRACE.{pass_id}.CONVERTER_INPUT_MISMATCH",
                        "converter provenance does not cover all native raw inputs",
                        path=relative(root, manifest_path), rerun=rerun,
                        waivable=False)
                matching_conversion = any(
                    raw_ids.issubset(set(item.get("input_content_ids", [])))
                    for item in conversions if isinstance(item, dict)
                )
                if not matching_conversion:
                    add(findings, f"TRACE.{pass_id}.CANONICAL_PROVENANCE_MISSING",
                        "raw-to-canonical conversion is absent from inventory",
                        path=relative(root, manifest_path), rerun=rerun,
                        waivable=False)
    formal_minimum = 3 if release_class != "pipeline_smoke" else 1
    if not isinstance(required_repetitions, int) or required_repetitions < formal_minimum:
        add(findings, "TRACE.REPETITION.CONTRACT_INVALID",
            f"{release_class} requires at least {formal_minimum} global repetitions",
            path="SESSION_MANIFEST.json", waivable=False)
    else:
        expected = set(range(max(required_repetitions, formal_minimum)))
        for group_id, planned in expected_by_group.items():
            planned_passes = (
                planned.get("required_passes", session_required)
                if policy_enabled else planned.get("planned_passes", [])
            )
            if not isinstance(planned_passes, list):
                continue
            for pass_id in planned_passes:
                observed = coverage[(group_id, pass_id)]
                if observed == expected:
                    continue
                add(
                    findings, f"TRACE.{pass_id}.REPETITION_INCOMPLETE",
                    f"expected repetition indices {sorted(expected)}, got {sorted(observed)}",
                    path=f"runs/{group_id}/{PASSES[pass_id]}/runs",
                    waivable=pass_id not in MANDATORY_GATE_PASSES,
                    rerun=(
                        f"./run.sh --run-group {group_id} "
                        f"--profiler-pass {pass_id.lower()} --resume"
                    ),
                    details={
                        "run_group_id": group_id,
                        "missing_repetition_indices": sorted(expected - observed),
                    },
                )
        if release_class == "formal_release" and required_repetitions < 5:
            add(findings, "TRACE.REPETITION.RELEASE_RECOMMENDATION",
                "formal_release has 3-4 repetitions; 5 are recommended",
                path="SESSION_MANIFEST.json", waivable=True, severity="warning")
    return incomplete


def validate_checksums(root: Path, findings: list[Finding]) -> None:
    checksum_path = root / "checksums.sha256"
    if not checksum_path.is_file() or checksum_path.is_symlink():
        add(findings, "TRACE.CHECKSUM.MISSING", "checksums.sha256 is mandatory",
            path="checksums.sha256", waivable=False)
        return
    entries, errors = checksum_map(checksum_path)
    for error in errors:
        add(findings, "TRACE.CHECKSUM.SCHEMA", error,
            path="checksums.sha256", waivable=False)
    for rel, expected in entries.items():
        path = root / rel
        if not path.is_file() or path.is_symlink():
            add(findings, "TRACE.CHECKSUM.FILE_MISSING",
                "checksummed file is missing", path=rel, waivable=False)
        elif sha256_file(path) != expected:
            add(findings, "TRACE.CHECKSUM.MISMATCH",
                "file checksum does not match", path=rel, waivable=False)
    mandatory = {
        relative(root, path) for path in root.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and path.name != "checksums.sha256"
        and path.name != "TRACE_COMPLETENESS_REPORT.json"
    }
    for rel in sorted(mandatory - entries.keys()):
        add(findings, "TRACE.CHECKSUM.COVERAGE_MISSING",
            "mandatory file has no checksum entry", path=rel, waivable=False)


def validate_result_manifest(
    root: Path, session: dict, findings: list[Finding],
) -> None:
    path = root / "RESULT_PACKAGE_MANIFEST.json"
    if not path.is_file() or path.is_symlink():
        add(findings, "TRACE.RESULT_MANIFEST.MISSING",
            "RESULT_PACKAGE_MANIFEST.json is mandatory", waivable=False)
        return
    try:
        manifest = load_json(path)
    except Exception as exc:
        add(findings, "TRACE.RESULT_MANIFEST.CORRUPT", str(exc), waivable=False)
        return
    for error in schema_errors(
        manifest, "result_package_manifest.schema.json",
        require_jsonschema=session.get("release_class") != "pipeline_smoke",
    ):
        add(findings, "TRACE.RESULT_MANIFEST.SCHEMA_INVALID", error,
            path="RESULT_PACKAGE_MANIFEST.json", waivable=False)
    if manifest.get("release_class") != session.get("release_class"):
        add(findings, "TRACE.RESULT_MANIFEST.RELEASE_CLASS_MISMATCH",
            "result manifest release_class differs from session", waivable=False)
    if session.get("release_class") in {"pipeline_smoke", "formal_candidate"} and (
        manifest.get("release_eligible") is not False
    ):
        add(findings, "TRACE.RESULT_MANIFEST.NON_RELEASE_ELIGIBLE",
            "pipeline_smoke and formal_candidate must declare release_eligible=false",
            waivable=False)
    if session.get("release_class") == "formal_release" and (
        manifest.get("release_eligible") is not True
    ):
        add(findings, "TRACE.RESULT_MANIFEST.RELEASE_NOT_ELIGIBLE",
            "formal_release must declare release_eligible=true", waivable=False)
    coverage = manifest.get("file_coverage")
    if not isinstance(coverage, dict):
        add(findings, "TRACE.RESULT_MANIFEST.COVERAGE_MISSING",
            "result manifest must declare exact file coverage", waivable=False)
        return
    excluded = coverage.get("excluded")
    required_excluded = {
        "RESULT_PACKAGE_MANIFEST.json",
        "checksums.sha256",
        "TRACE_COMPLETENESS_REPORT.json",
    }
    if set(excluded or []) != required_excluded:
        add(findings, "TRACE.RESULT_MANIFEST.EXCLUSIONS_INVALID",
            "result manifest exclusions are not the fixed contract", waivable=False)
    files = coverage.get("files")
    if not isinstance(files, list):
        add(findings, "TRACE.RESULT_MANIFEST.FILES_INVALID",
            "file_coverage.files must be an array", waivable=False)
        return
    declared: dict[str, dict] = {}
    for item in files:
        rel = item.get("path") if isinstance(item, dict) else None
        if not isinstance(rel, str) or not safe_member_name(rel) or rel in declared:
            add(findings, "TRACE.RESULT_MANIFEST.PATH_INVALID",
                f"invalid or duplicate covered path: {rel!r}", waivable=False)
            continue
        declared[rel] = item
        artifact = root / rel
        if (
            not artifact.is_file()
            or artifact.is_symlink()
            or item.get("bytes") != artifact.stat().st_size
            or item.get("sha256") != sha256_file(artifact)
        ):
            add(findings, "TRACE.RESULT_MANIFEST.FILE_MISMATCH",
                "covered file is missing or differs in size/hash", path=rel,
                waivable=False)
    actual = {
        relative(root, item)
        for item in root.rglob("*")
        if item.is_file() and not item.is_symlink()
        and relative(root, item) not in required_excluded
    }
    if set(declared) != actual or coverage.get("file_count") != len(declared):
        add(findings, "TRACE.RESULT_MANIFEST.COVERAGE_MISMATCH",
            "result manifest does not exactly cover the package payload",
            waivable=False, details={
                "missing": sorted(actual - set(declared)),
                "extra": sorted(set(declared) - actual),
            })


def validate_release_reports(
    root: Path, findings: list[Finding], *, release_class: str,
) -> None:
    if release_class != "formal_release":
        return
    mape_path = root / "VALIDATION_MAPE_REPORT.json"
    quality_path = root / "QUALITY_RELEASE_REPORT.json"
    reports: dict[str, dict[str, Any]] = {}
    for label, path, schema_name in (
        ("MAPE", mape_path, "validation_mape_report.schema.json"),
        ("QUALITY", quality_path, "quality_release_report.schema.json"),
    ):
        if not path.is_file() or path.is_symlink():
            add(findings, f"TRACE.RELEASE.{label}_REPORT_MISSING",
                f"{path.name} is mandatory for formal_release",
                path=path.name, waivable=False)
            continue
        try:
            report = load_json(path)
        except Exception as exc:
            add(findings, f"TRACE.RELEASE.{label}_REPORT_CORRUPT", str(exc),
                path=path.name, waivable=False)
            continue
        reports[label] = report
        for error in schema_errors(
            report, schema_name, require_jsonschema=True
        ):
            add(findings, f"TRACE.RELEASE.{label}_REPORT_SCHEMA_INVALID", error,
                path=path.name, waivable=False)

    mape = reports.get("MAPE")
    if mape is not None:
        if mape.get("gate_pass") is not True:
            add(findings, "TRACE.RELEASE.MAPE_GATE_FAILED",
                "formal_release requires VALIDATION_MAPE_REPORT.json gate_pass=true",
                path=mape_path.name, waivable=False)
        covered: set[str] = set()
        for item in mape.get("per_metric_domain", []):
            if not isinstance(item, dict):
                continue
            metric = item.get("metric")
            if metric in REQUIRED_MAPE_METRICS:
                covered.add(metric)
                value = item.get("mape_percent")
                if (
                    not isinstance(value, (int, float))
                    or isinstance(value, bool)
                    or not math.isfinite(value)
                    or value > 15.0
                    or item.get("pass") is not True
                ):
                    add(findings, "TRACE.RELEASE.MAPE_DOMAIN_GATE_FAILED",
                        f"{metric}/{item.get('domain')} must have MAPE <= 15%",
                        path=mape_path.name, waivable=False)
        for metric in sorted(REQUIRED_MAPE_METRICS - covered):
            add(findings, "TRACE.RELEASE.MAPE_METRIC_MISSING",
                f"required MAPE metric is absent: {metric}",
                path=mape_path.name, waivable=False)

    quality = reports.get("QUALITY")
    if quality is not None and quality.get("gate_pass") is not True:
        add(findings, "TRACE.RELEASE.QUALITY_GATE_FAILED",
            "formal_release requires a passing quality release report",
            path=quality_path.name, waivable=False)


def validate_directory_safety(root: Path, findings: list[Finding]) -> None:
    for path in root.rglob("*"):
        if path.is_symlink():
            add(findings, "TRACE.ARCHIVE.SYMLINK",
                "package contains a symlink", path=relative(root, path), waivable=False)


def validate_matrix_coverage(
    root: Path, session: dict, findings: list[Finding],
    *, release_class: str,
) -> None:
    """Reconcile every frozen-matrix state with an actual trace record."""
    frozen = session.get("frozen_matrix")
    capture = session.get("capture_plan")
    artifacts = session.get("artifacts", {})
    matrix_rel = (
        frozen.get("path") if isinstance(frozen, dict)
        else artifacts.get("frozen_matrix") if isinstance(artifacts, dict) else None
    )
    plan_rel = (
        capture.get("path") if isinstance(capture, dict)
        else artifacts.get("capture_plan") if isinstance(artifacts, dict) else None
    )
    if not matrix_rel and not plan_rel:
        add(findings, "TRACE.MATRIX.BINDING_MISSING",
            "every session, including pipeline smoke, must bind a frozen matrix and capture plan",
            path="SESSION_MANIFEST.json", waivable=False)
        return
    if not matrix_rel or not plan_rel:
        add(findings, "TRACE.MATRIX.BINDING_INCOMPLETE",
            "both frozen matrix and capture plan bindings are mandatory",
            path="SESSION_MANIFEST.json", waivable=False)
        return
    for rel, label in ((matrix_rel, "MATRIX"), (plan_rel, "CAPTURE_PLAN")):
        if not isinstance(rel, str) or not safe_member_name(rel):
            add(findings, f"TRACE.{label}.PATH_INVALID",
                f"{label.lower()} path is missing or unsafe",
                path="SESSION_MANIFEST.json", waivable=False)
            return
    matrix_path, plan_path = root / matrix_rel, root / plan_rel
    try:
        matrix = load_json(matrix_path)
    except Exception as exc:
        add(findings, "TRACE.MATRIX.MISSING_OR_CORRUPT", str(exc),
            path=matrix_rel, waivable=False)
        return
    try:
        plan = load_json(plan_path)
    except Exception as exc:
        add(findings, "TRACE.CAPTURE_PLAN.MISSING_OR_CORRUPT", str(exc),
            path=plan_rel, waivable=False)
        return
    declared_plan_hash = capture.get("sha256") if isinstance(capture, dict) else None
    if declared_plan_hash is not None and declared_plan_hash != sha256_file(plan_path):
        add(findings, "TRACE.CAPTURE_PLAN.HASH_MISMATCH",
            "capture plan does not match session capture_plan.sha256",
            path=plan_rel, waivable=False)
    if matrix.get("frozen") is not True:
        add(findings, "TRACE.MATRIX.NOT_FROZEN",
            "benchmark matrix must declare frozen=true", path=matrix_rel,
            waivable=False)
    samples = [
        sample
        for benchmark in matrix.get("benchmarks", [])
        if isinstance(benchmark, dict)
        for sample in benchmark.get("samples", [])
    ]
    if release_class == "pipeline_smoke" and (
        len(samples) != 8
        or len({item.get("sample_id") for item in samples if isinstance(item, dict)}) != 8
    ):
        add(findings, "TRACE.MATRIX.SMOKE_SAMPLE_COVERAGE",
            "pipeline_smoke must bind exactly eight unique matrix samples",
            path=matrix_rel, waivable=False)
    matrix_hash = canonical_hash(matrix)
    declared_hash = frozen.get("matrix_hash") if isinstance(frozen, dict) else None
    if declared_hash is not None and declared_hash != matrix_hash:
        add(findings, "TRACE.MATRIX.HASH_MISMATCH",
            "frozen matrix does not match session matrix_hash", path=matrix_rel,
            waivable=False)
    if plan.get("frozen_matrix_hash") != matrix_hash:
        add(findings, "TRACE.CAPTURE_PLAN.MATRIX_HASH_MISMATCH",
            "capture plan was not generated from the frozen matrix",
            path=plan_rel, waivable=False)
    if plan.get("profiler_concurrency") != 1 or plan.get(
        "simultaneous_profilers_forbidden"
    ) is not True:
        add(findings, "TRACE.CAPTURE_PLAN.PROFILER_OVERLAP",
            "capture plan must serialize profilers with concurrency=1",
            path=plan_rel, waivable=False)
    states = plan.get("states")
    if not isinstance(states, list) or not states:
        add(findings, "TRACE.CAPTURE_PLAN.EMPTY",
            "capture plan has no matrix states", path=plan_rel, waivable=False)
        return

    records: list[tuple[dict, str]] = []
    record_dir = root / "benchmark_records"
    if record_dir.is_dir():
        for path in sorted(record_dir.rglob("*.json")):
            try:
                record = load_json(path)
            except Exception as exc:
                add(findings, "TRACE.BENCHMARK.RECORD_CORRUPT", str(exc),
                    path=relative(root, path), waivable=False)
                continue
            validate_benchmark_record(root, record, relative(root, path), findings)
            records.append((record, relative(root, path)))
    for _, manifest_path in iter_pass_manifests(root):
        try:
            manifest = load_json(manifest_path)
        except Exception:
            continue
        for index, record in enumerate(manifest.get("benchmark_trace_records", [])):
            if isinstance(record, dict):
                records.append((
                    record,
                    f"{relative(root, manifest_path)}#benchmark_trace_records/{index}",
                ))

    def key_from_record(record: dict) -> tuple:
        return (
            record.get("hardware_id"), record.get("model_id"),
            record.get("model_revision"), record.get("benchmark_id"),
            record.get("sample_id"), record.get("generation_config_hash"),
            record.get("profiler_pass"), record.get("repetition_index"),
        )

    indexed: dict[tuple, dict[str, str]] = defaultdict(dict)
    for record, source in records:
        record_id = str(record.get("record_id", source))
        indexed[key_from_record(record)].setdefault(record_id, source)
        expected_environment = session.get("identity", {}).get("environment_hash")
        if expected_environment and record.get("environment_hash") != expected_environment:
            add(findings, "TRACE.BENCHMARK.ENVIRONMENT_HASH_MISMATCH",
                "record environment_hash differs from session environment",
                path=source, waivable=False)
    seen_state_ids: set[str] = set()
    for state in states:
        if not isinstance(state, dict):
            add(findings, "TRACE.CAPTURE_PLAN.STATE_SCHEMA",
                "capture state is not an object", path=plan_rel, waivable=False)
            continue
        state_id = state.get("state_id")
        if not isinstance(state_id, str) or state_id in seen_state_ids:
            add(findings, "TRACE.CAPTURE_PLAN.STATE_ID_INVALID",
                f"invalid or duplicate state_id {state_id!r}", path=plan_rel,
                waivable=False)
        else:
            seen_state_ids.add(state_id)
        pass_id = state.get("pass_id")
        rerun = state.get("command")
        estimate = state.get("estimate_minutes")
        details = {
            "state_id": state_id,
            "estimate_minutes": estimate,
            "matrix_coordinates": {
                name: state.get(name) for name in (
                    "gpu_id", "model_id", "benchmark_id", "sample_id",
                    "configuration_id", "pass_id", "repetition_index",
                )
            },
        }
        waivable = (
            release_class == "pipeline_smoke"
            and pass_id not in MANDATORY_GATE_PASSES
        )
        if not isinstance(rerun, str) or not rerun:
            add(findings, "TRACE.CAPTURE_PLAN.COMMAND_MISSING",
                "matrix state has no exact capture command", path=plan_rel,
                waivable=False, details=details)
        if not isinstance(estimate, (int, float)) or isinstance(estimate, bool) or estimate <= 0:
            add(findings, "TRACE.CAPTURE_PLAN.ESTIMATE_INVALID",
                "matrix state estimate_minutes must be positive", path=plan_rel,
                rerun=rerun, waivable=False, details=details)
        status = state.get("status")
        if status != "complete":
            pipeline_optional = (
                release_class == "pipeline_smoke" and pass_id in {"P4", "P6"}
            )
            formal_optional = (
                release_class != "pipeline_smoke"
                and (
                    (pass_id == "P4" and status == "unsupported")
                    or (pass_id == "P6" and status == "optional_not_run")
                )
            )
            if formal_optional:
                continue
            add(findings, f"TRACE.MATRIX.STATUS_{str(status).upper()}",
                state.get("blocked_reason") or f"matrix state status is {status}",
                path=plan_rel, rerun=rerun,
                waivable=pipeline_optional,
                severity="warning" if pipeline_optional else "incomplete",
                details=details)
            continue
        state_key = (
            state.get("gpu_id"), state.get("model_id"),
            state.get("model_revision"), state.get("benchmark_id"),
            state.get("sample_id"), state.get("generation_config_hash"),
            pass_id, state.get("repetition_index"),
        )
        matched = list(indexed.get(state_key, {}).values())
        if not matched:
            add(findings, "TRACE.MATRIX.RECORD_MISSING",
                "complete matrix state has no benchmark trace record",
                path=plan_rel, rerun=rerun, waivable=waivable,
                severity="incomplete", details=details)
        elif len(matched) > 1:
            add(findings, "TRACE.MATRIX.RECORD_DUPLICATE",
                f"matrix state has duplicate records: {matched}",
                path=plan_rel, rerun=rerun, waivable=False, details=details)


def verify_root(
    root: Path, requested_release_class: str | None = None,
) -> tuple[int, dict[str, Any]]:
    findings: list[Finding] = []
    validate_directory_safety(root, findings)
    session_path = root / "SESSION_MANIFEST.json"
    if not session_path.is_file() or session_path.is_symlink():
        add(findings, "TRACE.SESSION.MANIFEST_MISSING",
            "SESSION_MANIFEST.json is mandatory", path="SESSION_MANIFEST.json",
            waivable=False)
        session: dict[str, Any] = {}
    else:
        try:
            session = load_json(session_path)
        except Exception as exc:
            add(findings, "TRACE.SESSION.MANIFEST_CORRUPT",
                f"session manifest is invalid JSON: {exc}",
                path="SESSION_MANIFEST.json", waivable=False)
            session = {}
    declared_release_class = session.get("release_class")
    if declared_release_class not in RELEASE_RANK:
        declared_release_class = "pipeline_smoke"
    requested = requested_release_class or "pipeline_smoke"
    if requested not in RELEASE_RANK:
        raise ValueError(f"invalid requested release class: {requested}")
    release_class = max(
        (declared_release_class, requested), key=RELEASE_RANK.__getitem__
    )
    for error in schema_errors(
        session, "session_manifest.schema.json",
        require_jsonschema=release_class != "pipeline_smoke",
    ):
        add(findings, "TRACE.SESSION.SCHEMA_INVALID", error,
            path="SESSION_MANIFEST.json", waivable=False)
    approval_valid = validate_approval(session, findings)
    core = validate_core_artifacts(root, session, findings)
    inventory, conversions = validate_inventory(
        root, core.get("raw_inventory"), findings,
        formal=release_class != "pipeline_smoke",
    )
    incomplete = validate_passes(
        root, session, inventory, conversions, findings,
        release_class=release_class,
    )
    validate_matrix_coverage(
        root, session, findings, release_class=release_class
    )
    validate_release_reports(root, findings, release_class=release_class)
    validate_result_manifest(root, session, findings)
    validate_checksums(root, findings)
    hard_failures = [item for item in findings if not item.waivable]
    unapproved = [
        item for item in findings
        if item.waivable and item.severity in ("error", "incomplete")
    ]
    if hard_failures or (unapproved and not approval_valid):
        code, status = FAILED, "failed"
    elif incomplete or unapproved:
        code, status = APPROVED_INCOMPLETE, "approved_incomplete"
    else:
        code, status = COMPLETE, "complete"
    return code, {
        "schema_version": "trace-completeness-report-v2",
        "status": status,
        "accepted_incomplete": session.get("accepted_incomplete") is True,
        "declared_release_class": session.get("release_class"),
        "requested_release_class": requested,
        "enforced_release_class": release_class,
        "release_eligible": (
            code == COMPLETE and release_class == "formal_release"
        ),
        "finding_count": len(findings),
        "findings": [item.as_dict() for item in findings],
    }


@contextmanager
def package_root(path: Path) -> Iterator[Path]:
    if path.is_dir():
        yield path.resolve()
        return
    if not path.is_file() or not tarfile.is_tarfile(path):
        raise ValueError("package must be a directory or tar archive")
    with tarfile.open(path, "r:*") as archive:
        members = archive.getmembers()
        for member in members:
            if not safe_member_name(member.name):
                raise ValueError(f"unsafe archive path: {member.name}")
            if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                raise ValueError(f"unsafe archive member type: {member.name}")
        sidecar = path.with_suffix(path.suffix + ".sha256")
        if not sidecar.is_file() or sidecar.is_symlink():
            raise ValueError(f"archive SHA-256 sidecar is missing: {sidecar.name}")
        parts = sidecar.read_text(encoding="utf-8").strip().split(maxsplit=1)
        if (
            len(parts) != 2
            or parts[1].lstrip("*") != path.name
            or parts[0] != sha256_file(path)
        ):
            raise ValueError("archive SHA-256 sidecar does not match archive")
        with tempfile.TemporaryDirectory(prefix="trace-verify-") as temporary:
            destination = Path(temporary)
            # Every member and link type was checked above. Avoid the Python
            # 3.12-only extraction filter so remote Python 3.10 images work.
            archive.extractall(destination)
            children = [item for item in destination.iterdir()]
            root = children[0] if len(children) == 1 and children[0].is_dir() else destination
            yield root


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument(
        "--release-class", choices=RELEASE_CLASSES,
        default="pipeline_smoke",
        help="Externally requested verification gate; session self-report cannot lower it",
    )
    args = parser.parse_args()
    try:
        with package_root(args.package) as root:
            code, report = verify_root(
                root, requested_release_class=args.release_class
            )
    except Exception as exc:
        code = FAILED
        report = {
            "schema_version": "trace-completeness-report-v2",
            "status": "failed",
            "accepted_incomplete": False,
            "finding_count": 1,
            "findings": [Finding(
                "TRACE.ARCHIVE.UNSAFE_OR_CORRUPT", "error", str(exc),
                waivable=False,
            ).as_dict()],
        }
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return code


if __name__ == "__main__":
    raise SystemExit(main())

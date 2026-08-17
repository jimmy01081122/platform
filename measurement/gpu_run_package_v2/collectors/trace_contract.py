"""Shared v2 trace package contract and validation primitives."""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

PASSES = {
    "P0": "p0_baseline",
    "P1": "p1_timeline",
    "P2": "p2_routing",
    "P3": "p3_memory_transfer",
    "P4": "p4_gpu_counters",
    "P5": "p5_telemetry",
    "P6": "p6_detailed_optional",
}
STATUSES = {
    "complete", "unsupported", "permission_denied", "not_applicable",
    "failed", "missing", "corrupt", "truncated", "planned", "blocked",
}
MANDATORY_GATE_PASSES = frozenset({"P0", "P2", "P3"})
ALIGNMENT_FIELDS = (
    "suite_id", "sample_id", "model_revision", "generation_config_hash",
    "seed", "request_id", "token_index", "layer_index", "repetition_index",
    "session_id",
)
EXECUTION_ALIGNMENT_FIELDS = (
    "suite_version", "model_revision", "tokenizer_revision", "benchmark_id",
    "sample_id", "prompt_hash", "generation_config_hash", "seed",
    "repetition_id", "hardware_session_id",
)
EVENT_KEY_FIELDS = (
    "request_id", "phase", "generation_step", "token_index", "layer_id",
    "router_module", "dispatch_index",
)
IDENTITY_FIELDS = (
    "session_id", "run_group_id", "run_id", "model_revision",
    "workload_hash", "configuration_hash", "environment_hash",
    "repetition_index", "profiler_pass",
)
HASH_FIELDS = ("workload_hash", "configuration_hash", "environment_hash")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def build_alignment_key(value: dict[str, Any]) -> str:
    """Return the legacy combined alignment key for v1 record compatibility."""
    missing = [name for name in ALIGNMENT_FIELDS if value.get(name) is None]
    if missing:
        raise ValueError(f"alignment key missing fields: {', '.join(missing)}")
    return canonical_hash({name: value[name] for name in ALIGNMENT_FIELDS})


def build_execution_alignment_key(value: dict[str, Any]) -> str:
    """Return a pass-level key that excludes event-local coordinates."""
    missing = [
        name for name in EXECUTION_ALIGNMENT_FIELDS if value.get(name) is None
    ]
    if missing:
        raise ValueError(
            f"execution alignment key missing fields: {', '.join(missing)}"
        )
    return canonical_hash({
        name: value[name] for name in EXECUTION_ALIGNMENT_FIELDS
    })


def build_event_key(value: dict[str, Any]) -> str:
    """Return an event-local key anchored to an execution alignment key."""
    fields = ("execution_alignment_key",) + EVENT_KEY_FIELDS
    missing = [name for name in fields if value.get(name) is None]
    if missing:
        raise ValueError(f"event key missing fields: {', '.join(missing)}")
    return canonical_hash({name: value[name] for name in fields})


def validate_alignment(value: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, dict):
        return ["alignment must be an object"]
    for name in ALIGNMENT_FIELDS:
        if value.get(name) is None or value.get(name) == "":
            errors.append(f"alignment.{name} is required")
    for name in ("seed", "token_index", "layer_index", "repetition_index"):
        item = value.get(name)
        if not isinstance(item, int) or isinstance(item, bool) or item < 0:
            errors.append(f"alignment.{name} must be a non-negative integer")
    key = value.get("alignment_key")
    if not HASH_RE.fullmatch(str(key or "")):
        errors.append("alignment.alignment_key must be lowercase SHA-256")
    elif not errors:
        expected = build_alignment_key(value)
        if key != expected:
            errors.append("alignment.alignment_key does not match canonical fields")
    return errors


def validate_benchmark_trace_record(record: Any) -> list[str]:
    """Validate semantic requirements not conveniently expressed by JSON Schema."""
    if not isinstance(record, dict):
        return ["record must be an object"]
    errors: list[str] = []
    if record.get("schema_version") != "benchmark-trace-record-v1":
        errors.append("schema_version must be benchmark-trace-record-v1")
    required_strings = (
        "record_id", "model_id", "model_revision", "weights_revision",
        "tokenizer_revision", "suite_id", "benchmark_id", "sample_id",
        "template_id", "serving_runtime", "hardware_id", "profiler_pass",
        "native_format",
    )
    for name in required_strings:
        if not isinstance(record.get(name), str) or not record[name]:
            errors.append(f"{name} is required")
    for name in (
        "template_hash", "prompt_hash", "generation_config_hash", "output_hash",
        "environment_hash", "native_sha256",
    ):
        if not HASH_RE.fullmatch(str(record.get(name, ""))):
            errors.append(f"{name} must be lowercase SHA-256")
    for name in ("repetition_index", "request_index"):
        item = record.get(name)
        if not isinstance(item, int) or isinstance(item, bool) or item < 0:
            errors.append(f"{name} must be a non-negative integer")
    tokens = record.get("actual_tokens")
    if not isinstance(tokens, dict):
        errors.append("actual_tokens must be an object")
    else:
        token_values_valid = True
        for name in ("prompt", "generated", "total"):
            item = tokens.get(name)
            if not isinstance(item, int) or isinstance(item, bool) or item < 0:
                errors.append(f"actual_tokens.{name} must be non-negative")
                token_values_valid = False
        if token_values_valid and tokens["total"] != tokens["prompt"] + tokens["generated"]:
            errors.append("actual_tokens.total must equal prompt + generated")
        for name, count_name in (
            ("prompt_token_ids", "prompt"),
            ("generated_token_ids", "generated"),
        ):
            values = tokens.get(name)
            if (
                not isinstance(values, list)
                or any(not isinstance(item, int) or isinstance(item, bool) or item < 0
                       for item in values)
            ):
                errors.append(f"actual_tokens.{name} must be non-negative token IDs")
            elif token_values_valid and len(values) != tokens[count_name]:
                errors.append(f"actual_tokens.{count_name} must match {name} length")
        token_hash = tokens.get("token_ids_hash")
        if not HASH_RE.fullmatch(str(token_hash or "")):
            errors.append("actual_tokens.token_ids_hash must be lowercase SHA-256")
        elif isinstance(tokens.get("prompt_token_ids"), list) and isinstance(
            tokens.get("generated_token_ids"), list
        ):
            expected_token_hash = canonical_hash({
                "prompt_token_ids": tokens["prompt_token_ids"],
                "generated_token_ids": tokens["generated_token_ids"],
            })
            if token_hash != expected_token_hash:
                errors.append("actual_tokens.token_ids_hash does not match token IDs")
    quality = record.get("quality")
    if not isinstance(quality, dict) or not isinstance(quality.get("status"), str):
        errors.append("quality.status is required")
    native_paths = record.get("native_paths")
    native_checksums = record.get("native_checksums")
    if not isinstance(native_paths, list) or not native_paths:
        errors.append("native_paths must be a non-empty array")
    elif any(not isinstance(path, str) or not safe_member_name(path)
             for path in native_paths):
        errors.append("native_paths must contain safe package-relative paths")
    if not isinstance(native_checksums, dict) or not native_checksums:
        errors.append("native_checksums must be a non-empty object")
    elif isinstance(native_paths, list):
        if set(native_checksums) != set(native_paths):
            errors.append("native_checksums keys must exactly match native_paths")
        for path, digest in native_checksums.items():
            if not safe_member_name(path) or not HASH_RE.fullmatch(str(digest)):
                errors.append("native_checksums must map safe paths to SHA-256")
    for name in ("serving", "hardware"):
        if not isinstance(record.get(name), dict) or not record[name]:
            errors.append(f"{name} must be a non-empty object")
    completeness = record.get("completeness")
    if not isinstance(completeness, dict):
        errors.append("completeness must be an object")
    else:
        if not isinstance(completeness.get("complete"), bool):
            errors.append("completeness.complete must be boolean")
        missing = completeness.get("missing_fields")
        if not isinstance(missing, list):
            errors.append("completeness.missing_fields must be an array")
        elif completeness.get("complete") and missing:
            errors.append("complete record cannot list missing_fields")
    errors.extend(validate_alignment(record.get("alignment")))
    return errors


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("top-level JSON value must be an object")
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def relative_package_path(root: Path, path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError(f"path is outside package: {path}") from exc


def safe_member_name(name: str) -> bool:
    path = PurePosixPath(name)
    return bool(name) and not path.is_absolute() and ".." not in path.parts


def valid_utc(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value.endswith("Z") or "+" in value[10:]
    except ValueError:
        return False


@dataclass(frozen=True)
class Finding:
    finding_id: str
    severity: str
    message: str
    path: str | None = None
    rerun_command: str | None = None
    waivable: bool = True
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            key: value for key, value in {
                "finding_id": self.finding_id,
                "severity": self.severity,
                "message": self.message,
                "path": self.path,
                "rerun_command": self.rerun_command,
                "waivable": self.waivable,
                "details": self.details or None,
            }.items() if value is not None
        }


def validate_identity(identity: Any, expected_pass: str | None = None) -> list[str]:
    errors: list[str] = []
    if not isinstance(identity, dict):
        return ["identity must be an object"]
    for field_name in IDENTITY_FIELDS:
        if field_name not in identity or identity[field_name] in ("", None):
            errors.append(f"identity.{field_name} is required")
    for field_name in HASH_FIELDS:
        value = identity.get(field_name)
        if value is not None and not HASH_RE.fullmatch(str(value)):
            errors.append(f"identity.{field_name} must be lowercase SHA-256")
    repetition = identity.get("repetition_index")
    if not isinstance(repetition, int) or isinstance(repetition, bool) or repetition < 0:
        errors.append("identity.repetition_index must be a non-negative integer")
    if expected_pass and identity.get("profiler_pass") != expected_pass:
        errors.append(
            f"identity.profiler_pass must be {expected_pass}, "
            f"got {identity.get('profiler_pass')!r}"
        )
    return errors


def validate_clock(clock: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(clock, dict):
        return ["clock must be an object"]
    if clock.get("status") == "known_limitation":
        if not isinstance(clock.get("reason"), str) or not clock["reason"].strip():
            errors.append("known-limitation clock requires a reason")
        source_ids = clock.get("source_content_ids")
        if not isinstance(source_ids, list) or any(
            not HASH_RE.fullmatch(str(item)) for item in source_ids
        ):
            errors.append("clock.source_content_ids must be SHA-256 values")
        return errors
    if not valid_utc(clock.get("wall_clock_utc")):
        errors.append("clock.wall_clock_utc must be an ISO-8601 timestamp with timezone")
    value = clock.get("monotonic_host_ns")
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        errors.append("clock.monotonic_host_ns must be a non-negative integer")
    for name in ("profiler_domain", "timezone"):
        if not isinstance(clock.get(name), str) or not clock[name]:
            errors.append(f"clock.{name} is required")
    alignment = clock.get("alignment")
    if not isinstance(alignment, dict):
        errors.append("clock.alignment must be an object")
    else:
        if not alignment.get("method"):
            errors.append("clock.alignment.method is required")
        max_error = alignment.get("max_error_ns")
        if not isinstance(max_error, (int, float)) or isinstance(max_error, bool) or max_error < 0:
            errors.append("clock.alignment.max_error_ns must be non-negative")
        anchors = alignment.get("anchors")
        if not isinstance(anchors, list) or not anchors:
            errors.append("clock.alignment.anchors must be a non-empty array")
    return errors


def checksum_map(path: Path) -> tuple[dict[str, str], list[str]]:
    entries: dict[str, str] = {}
    errors: list[str] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != 2 or not HASH_RE.fullmatch(parts[0]):
            errors.append(f"invalid checksum line {number}")
            continue
        rel = parts[1].lstrip("*")
        if not safe_member_name(rel):
            errors.append(f"unsafe checksum path on line {number}: {rel}")
        elif rel in entries:
            errors.append(f"duplicate checksum path: {rel}")
        else:
            entries[rel] = parts[0]
    return entries, errors


def iter_pass_manifests(root: Path) -> Iterable[tuple[str, Path]]:
    runs = root / "runs"
    if not runs.is_dir():
        return
    for pass_id, directory in PASSES.items():
        for path in sorted(
            runs.glob(f"*/{directory}/runs/*/PASS_MANIFEST.json")
        ):
            yield pass_id, path


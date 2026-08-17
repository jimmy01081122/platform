"""Validation and checksum rules for one scheduler work unit."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any

from .model import WorkUnit

RESULT_NAME = "COLLECTOR_RESULT.json"
MANIFEST_NAME = "WORK_UNIT_MANIFEST.json"
CHECKSUM_NAME = "checksums.sha256"
COMMON_C1_ARTIFACTS = {
    "session_manifest.json",
    "generation_results.jsonl",
    "runtime_metadata.json",
    "quality_results.jsonl",
    "pass_manifest.json",
    "stdout.log",
    "stderr.log",
}
C1_PASS_ARTIFACT = {
    "P0": "baseline_metrics.json",
    "P1": "runtime_timeline.json",
    "P2": "routing_dispatch.jsonl",
    "P3": "memory_observations.json",
    "P5_BASIC": "telemetry_availability.json",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_relative_path(value: str) -> bool:
    path = PurePosixPath(value)
    return bool(value) and not path.is_absolute() and ".." not in path.parts


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def validate_collector_output(
    root: Path, unit: WorkUnit, *, diagnostic_mode: str | None = None
) -> dict[str, Any]:
    result_path = root / RESULT_NAME
    if not result_path.is_file() or result_path.stat().st_size == 0:
        raise ValueError(f"mandatory {RESULT_NAME} is missing or empty")
    result = _load_object(result_path)
    if result.get("status") != "success":
        raise ValueError("collector result status is not success")
    if result.get("schema_valid") is not True:
        raise ValueError("collector did not attest schema validation")
    raw_files = result.get("raw_files")
    if not isinstance(raw_files, list) or not raw_files:
        raise ValueError("collector result requires non-empty raw_files")
    if len(raw_files) != len(set(raw_files)):
        raise ValueError("collector result contains duplicate raw_files")
    if result.get("contract_version") == "c1-worker-v1":
        expected = COMMON_C1_ARTIFACTS | {C1_PASS_ARTIFACT[unit.logical_pass]}
        if diagnostic_mode == "token_drift_v1":
            expected.add("diagnostic_scores.json")
        supplied = set(raw_files)
        if not expected.issubset(supplied):
            missing = sorted(expected - set(raw_files))
            raise ValueError(f"C1 artifact contract drift; missing={missing}")
        invalid_extra = sorted(
            path for path in supplied - expected if not path.startswith("raw/")
        )
        if invalid_extra:
            raise ValueError(
                f"C1 dynamic artifacts must be under raw/: {invalid_extra}"
            )
    for relative in raw_files:
        if not isinstance(relative, str) or not safe_relative_path(relative):
            raise ValueError(f"unsafe raw path: {relative!r}")
        path = root / relative
        if not path.is_file() or path.is_symlink() or path.stat().st_size == 0:
            raise ValueError(f"mandatory raw file is missing or empty: {relative}")
    if unit.logical_pass == "P2" and result.get("contract_version") == "c1-worker-v1":
        rows = [
            json.loads(line)
            for line in (root / C1_PASS_ARTIFACT["P2"]).read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip()
        ]
        if not rows or any(row.get("actual_dispatch") is not True for row in rows):
            raise ValueError("P2 requires non-empty verified actual dispatch")
    supplied_id = result.get("work_unit_id")
    if supplied_id is not None and supplied_id != unit.work_unit_id:
        raise ValueError("collector result work_unit_id mismatch")
    return result


def build_manifest(root: Path, unit: WorkUnit, result: dict[str, Any]) -> dict[str, Any]:
    files = sorted({
        RESULT_NAME,
        *(str(item) for item in result["raw_files"]),
    })
    coverage = [
        {
            "path": relative,
            "bytes": (root / relative).stat().st_size,
            "sha256": sha256_file(root / relative),
        }
        for relative in files
    ]
    return {
        "schema_version": "c1-work-unit-v1",
        # A .tmp artifact must never claim COMPLETE before its atomic rename.
        "artifact_state": "VALIDATED_FOR_ATOMIC_RENAME",
        "work_unit": unit.as_dict(),
        "files": coverage,
    }


def render_checksums(manifest: dict[str, Any]) -> str:
    return "".join(
        f"{item['sha256']}  {item['path']}\n"
        for item in manifest["files"]
    )


def verify_complete(root: Path, expected: WorkUnit | None = None) -> list[str]:
    errors: list[str] = []
    manifest_path = root / MANIFEST_NAME
    checksums_path = root / CHECKSUM_NAME
    try:
        manifest = _load_object(manifest_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return [f"invalid work-unit manifest: {exc}"]
    try:
        unit = WorkUnit.from_dict(manifest["work_unit"])
    except (KeyError, TypeError, ValueError) as exc:
        errors.append(f"invalid work-unit identity: {exc}")
        return errors
    if expected is not None and unit != expected:
        errors.append("manifest identity differs from expected work unit")
    if manifest.get("artifact_state") != "VALIDATED_FOR_ATOMIC_RENAME":
        errors.append("manifest was not validated for atomic rename")
    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        errors.append("manifest files must be non-empty")
        return errors
    expected_lines = []
    for entry in entries:
        if not isinstance(entry, dict) or not safe_relative_path(str(entry.get("path", ""))):
            errors.append("manifest contains unsafe file entry")
            continue
        relative = entry["path"]
        path = root / relative
        if not path.is_file() or path.is_symlink():
            errors.append(f"manifest file missing: {relative}")
            continue
        digest = sha256_file(path)
        if digest != entry.get("sha256"):
            errors.append(f"checksum mismatch: {relative}")
        if path.stat().st_size != entry.get("bytes") or path.stat().st_size == 0:
            errors.append(f"size mismatch or empty file: {relative}")
        expected_lines.append(f"{entry.get('sha256')}  {relative}\n")
    try:
        actual_lines = checksums_path.read_text(encoding="utf-8")
    except OSError as exc:
        errors.append(f"missing checksums: {exc}")
    else:
        if actual_lines != "".join(expected_lines):
            errors.append("checksums.sha256 does not match manifest")
    return errors

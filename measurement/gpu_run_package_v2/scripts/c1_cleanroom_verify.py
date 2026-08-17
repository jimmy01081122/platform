#!/usr/bin/env python3
"""Verify a C1 package in a clean temporary directory and rebuild derivatives."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Any

try:
    from .c1_canonicalize import canonicalize
    from .c1_system_ir import build_system_ir
except ImportError:  # Direct script execution.
    from c1_canonicalize import canonicalize
    from c1_system_ir import build_system_ir

SECRET_ASSIGNMENT = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|password|client[_-]?secret|secret)"
    r"\s*[\"']?\s*[:=]\s*[\"']?(?P<value>[^\"'\s,}]{8,})"
)
SECRET_TOKENS = (
    re.compile(r"\b(?:hf_|ghp_|github_pat_)[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{16,}=*\b"),
)
REDACTED_VALUES = {"redacted", "<redacted>", "masked", "<masked>", "none", "null"}
ABSOLUTE_PATH = re.compile(
    r"(?:^|[\"'\s:=])(?:/home/|/Users/|/workspace(?:s)?/|/tmp/|/mnt/[A-Za-z]/|"
    r"[A-Za-z]:[\\/])"
)
PACKAGE_INVENTORY_NAMES = ("PACKAGE_INVENTORY.json", "package_inventory.json")
DERIVATIVE_PATHS = (
    "canonical/routing.json",
    "canonical/system.json",
    "summary.json",
)


def _safe_name(name: str) -> bool:
    path = Path(name)
    return not path.is_absolute() and ".." not in path.parts


def _extract(source: Path, destination: Path) -> Path:
    if source.is_dir():
        shutil.copytree(source, destination, dirs_exist_ok=True)
        return destination
    if zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as archive:
            if any(not _safe_name(item.filename) for item in archive.infolist()):
                raise ValueError("archive contains unsafe member")
            archive.extractall(destination)
    elif tarfile.is_tarfile(source):
        with tarfile.open(source) as archive:
            members = archive.getmembers()
            if any(
                not _safe_name(item.name)
                or not (item.isfile() or item.isdir())
                for item in members
            ):
                raise ValueError("archive contains unsafe member")
            archive.extractall(destination, members=members)
    else:
        raise ValueError("package must be a directory, zip, or tar archive")
    children = list(destination.iterdir())
    return children[0] if len(children) == 1 and children[0].is_dir() else destination


def _scan(root: Path) -> list[dict[str, Any]]:
    findings = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        relative = path.relative_to(root).as_posix()
        for number, line in enumerate(text.splitlines(), 1):
            if ABSOLUTE_PATH.search(line):
                findings.append({"kind": "absolute_path", "path": relative, "line": number})
            assignment = SECRET_ASSIGNMENT.search(line)
            assignment_secret = (
                assignment is not None
                and assignment.group("value").lower() not in REDACTED_VALUES
            )
            if assignment_secret or any(pattern.search(line) for pattern in SECRET_TOKENS):
                findings.append({"kind": "secret", "path": relative, "line": number})
    return findings


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _descriptor_map(
    entries: Any, *, label: str, errors: list[str]
) -> dict[str, dict[str, Any]]:
    if not isinstance(entries, list):
        errors.append(f"{label} files must be an array")
        return {}
    result: dict[str, dict[str, Any]] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            errors.append(f"{label} file entry {index} must be an object")
            continue
        relative = entry.get("path")
        if not isinstance(relative, str) or not _safe_name(relative):
            errors.append(f"{label} contains unsafe file path: {relative!r}")
            continue
        if relative in result:
            errors.append(f"{label} contains duplicate file path: {relative}")
            continue
        if (
            not isinstance(entry.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
            or not isinstance(entry.get("bytes"), int)
            or isinstance(entry.get("bytes"), bool)
            or entry["bytes"] < 0
        ):
            errors.append(f"{label} has invalid digest/size metadata: {relative}")
            continue
        result[relative] = entry
    return result


def _verify_descriptors(
    root: Path, descriptors: dict[str, dict[str, Any]], *, label: str
) -> list[str]:
    errors = []
    for relative, entry in sorted(descriptors.items()):
        path = root / relative
        if not path.is_file() or path.is_symlink():
            errors.append(f"{label} file missing or unsafe: {relative}")
            continue
        if path.stat().st_size != entry["bytes"]:
            errors.append(f"{label} size mismatch: {relative}")
        if _sha256(path) != entry["sha256"]:
            errors.append(f"{label} checksum mismatch: {relative}")
    return errors


def validate_package_inventory(
    root: Path, inventory_path: Path | None = None
) -> list[str]:
    """Validate the package trust root and every nested work-unit inventory."""
    errors: list[str] = []
    candidates = (
        [inventory_path]
        if inventory_path is not None
        else [root / name for name in PACKAGE_INVENTORY_NAMES if (root / name).is_file()]
    )
    if len(candidates) != 1:
        return ["package requires exactly one PACKAGE_INVENTORY.json trust root"]
    inventory_path = candidates[0]
    try:
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"invalid package inventory: {exc}"]
    if (
        not isinstance(inventory, dict)
        or inventory.get("schema_version") != "c1-package-inventory-v1"
    ):
        errors.append("unsupported package inventory schema")
        return errors
    descriptors = _descriptor_map(
        inventory.get("files"), label="package inventory", errors=errors
    )
    inventory_relative = inventory_path.relative_to(root).as_posix()
    disk_files = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
    }
    expected_files = set(descriptors) | {inventory_relative}
    untracked = sorted(disk_files - expected_files)
    absent = sorted(expected_files - disk_files)
    if untracked:
        errors.append("package contains untracked files: " + ", ".join(untracked))
    if absent:
        errors.append("package inventory files are missing: " + ", ".join(absent))
    errors.extend(_verify_descriptors(root, descriptors, label="package inventory"))

    complete_root = root / "complete"
    if complete_root.is_dir():
        for unit_root in sorted(
            path for path in complete_root.iterdir() if path.is_dir()
        ):
            if not (unit_root / "WORK_UNIT_MANIFEST.json").is_file():
                errors.append(
                    "complete work unit is missing WORK_UNIT_MANIFEST.json: "
                    + unit_root.relative_to(root).as_posix()
                )
            if not (unit_root / "checksums.sha256").is_file():
                errors.append(
                    "complete work unit is missing checksums.sha256: "
                    + unit_root.relative_to(root).as_posix()
                )
    for manifest_path in sorted(root.rglob("WORK_UNIT_MANIFEST.json")):
        unit_root = manifest_path.parent
        unit_label = manifest_path.relative_to(root).as_posix()
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"invalid work-unit manifest {unit_label}: {exc}")
            continue
        unit_descriptors = _descriptor_map(
            manifest.get("files"), label=unit_label, errors=errors
        )
        unit_files = {
            path.relative_to(unit_root).as_posix()
            for path in unit_root.rglob("*")
            if path.is_file()
        }
        unit_expected = set(unit_descriptors) | {
            "WORK_UNIT_MANIFEST.json", "checksums.sha256"
        }
        unit_untracked = sorted(unit_files - unit_expected)
        if unit_untracked:
            errors.append(
                f"{unit_label} contains untracked files: " + ", ".join(unit_untracked)
            )
        errors.extend(
            _verify_descriptors(unit_root, unit_descriptors, label=unit_label)
        )
        checksum_path = unit_root / "checksums.sha256"
        expected_checksums = "".join(
            f"{entry['sha256']}  {relative}\n"
            for relative, entry in unit_descriptors.items()
        )
        try:
            actual_checksums = checksum_path.read_text(encoding="utf-8")
        except OSError:
            errors.append(f"{unit_label} is missing checksums.sha256")
        else:
            if actual_checksums != expected_checksums:
                errors.append(f"{unit_label} checksums do not match its inventory")
    return errors


def _routing_inputs(root: Path) -> list[Path]:
    preferred = [
        path
        for path in (root / "raw/P2/routing.jsonl", root / "raw/routing.jsonl")
        if path.is_file()
    ]
    discovered = sorted(root.rglob("routing_dispatch.jsonl"))
    return sorted(
        set(preferred + discovered),
        key=lambda path: path.relative_to(root).as_posix(),
    )


def _rebuild(raw_bytes: bytes, destination: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    raw = destination / "raw/P2/routing.jsonl"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(raw_bytes)
    canonical = canonicalize(
        raw,
        destination / "canonical/routing.json",
        destination / "canonical/conversion_log.json",
    )
    system = build_system_ir(
        destination / "canonical/routing.json",
        destination / "canonical/system.json",
    )
    summary = {
        "schema_version": "c1-rebuilt-summary-v1",
        "routing_event_count": len(canonical["events"]),
        "system_event_count": len(system["events"]),
    }
    (destination / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return canonical, system


def _compare_derivatives(
    package_root: Path, first: Path, second: Path
) -> tuple[list[str], list[dict[str, Any]]]:
    errors = []
    comparisons = []
    for relative in DERIVATIVE_PATHS:
        packaged = package_root / relative
        rebuilt = first / relative
        repeated = second / relative
        if not packaged.is_file():
            errors.append(f"packaged derivative is missing: {relative}")
            continue
        rebuilt_bytes = rebuilt.read_bytes()
        repeated_bytes = repeated.read_bytes()
        packaged_bytes = packaged.read_bytes()
        deterministic = rebuilt_bytes == repeated_bytes
        byte_equal = packaged_bytes == rebuilt_bytes
        packaged_hash = hashlib.sha256(packaged_bytes).hexdigest()
        rebuilt_hash = hashlib.sha256(rebuilt_bytes).hexdigest()
        comparisons.append({
            "path": relative,
            "deterministic": deterministic,
            "byte_equal": byte_equal,
            "packaged_sha256": packaged_hash,
            "rebuilt_sha256": rebuilt_hash,
        })
        if not deterministic:
            errors.append(f"nondeterministic rebuild output: {relative}")
        if not byte_equal or packaged_hash != rebuilt_hash:
            errors.append(f"rebuilt derivative mismatch: {relative}")
    return errors, comparisons


def verify(source: Path, rebuild_root: Path) -> tuple[int, dict[str, Any]]:
    extracted = rebuild_root / "package"
    root = _extract(source.resolve(), extracted)
    inventory_errors = validate_package_inventory(root)
    scan_findings = _scan(root)
    raw_candidates = _routing_inputs(root)
    rebuild_errors = []
    comparisons: list[dict[str, Any]] = []
    canonical = None
    system = None
    if not raw_candidates:
        rebuild_errors.append("raw P2 routing JSONL is missing")
    else:
        raw_bytes = b"".join(path.read_bytes() for path in raw_candidates)
        try:
            first = rebuild_root / "rebuild-first"
            second = rebuild_root / "rebuild-second"
            canonical, system = _rebuild(raw_bytes, first)
            _rebuild(raw_bytes, second)
            comparison_errors, comparisons = _compare_derivatives(
                root, first, second
            )
            rebuild_errors.extend(comparison_errors)
        except (
            OSError, RuntimeError, ValueError, KeyError, TypeError,
            json.JSONDecodeError,
        ) as exc:
            rebuild_errors.append(str(exc))
    summary = {
        "schema_version": "c1-rebuilt-summary-v1",
        "routing_event_count": len(canonical["events"]) if canonical else 0,
        "system_event_count": len(system["events"]) if system else 0,
    }
    ok = not inventory_errors and not scan_findings and not rebuild_errors
    report = {
        "schema_version": "c1-cleanroom-report-v1",
        "status": "pass" if ok else "failed",
        "inventory_errors": inventory_errors,
        "checksum_errors": inventory_errors,
        "scan_findings": scan_findings,
        "rebuild_errors": rebuild_errors,
        "derivative_comparisons": comparisons,
        "summary": summary,
    }
    return (0 if ok else 1), report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", required=True, type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="c1-cleanroom-") as temporary:
        code, report = verify(args.package, Path(temporary))
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return code


if __name__ == "__main__":
    raise SystemExit(main())

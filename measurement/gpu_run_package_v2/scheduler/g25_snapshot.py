"""Session-local source snapshot for long-term G2.5 replay."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Mapping

from .store import atomic_json, fsync_directory


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
INVENTORY_KEYS = {
    "schema_version", "source_checksums_sha256", "file_count", "files",
    "inventory_sha256",
}
ROW_KEYS = {"path", "bytes", "sha256"}


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")).hexdigest()


def _safe_relative_path(relative: Any) -> str:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise ValueError("snapshot path must be a non-empty POSIX relative path")
    candidate = Path(relative)
    if (
        candidate.is_absolute()
        or candidate.as_posix() != relative
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise ValueError(f"snapshot path is unsafe or non-canonical: {relative!r}")
    return relative


def _safe_file(root: Path, relative: str) -> Path:
    relative = _safe_relative_path(relative)
    root = root.resolve(strict=True)
    unresolved = root / relative
    current = root
    for part in Path(relative).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"snapshot path contains a symlink: {relative}")
    resolved = unresolved.resolve(strict=True)
    resolved.relative_to(root)
    if not resolved.is_file():
        raise ValueError(f"snapshot path is not a regular file: {relative}")
    return resolved


def _parse_ledger_bytes(rendered: bytes) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    try:
        lines = rendered.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ValueError("package checksum ledger is not UTF-8") from exc
    for number, line in enumerate(lines, 1):
        if not line:
            raise ValueError(f"package checksum ledger contains a blank line at {number}")
        parts = line.split("  ", 1)
        if len(parts) != 2:
            raise ValueError(f"package checksum ledger row {number} is malformed")
        expected, relative = parts
        if not SHA256_RE.fullmatch(expected):
            raise ValueError(f"package checksum ledger hash is invalid at row {number}")
        relative = _safe_relative_path(relative)
        if relative in seen:
            raise ValueError(f"package checksum ledger path is duplicated: {relative}")
        seen.add(relative)
        rows.append((relative, expected))
    if not rows:
        raise ValueError("package checksum ledger is empty")
    return rows


def verify_package_ledger(package_root: Path) -> list[dict[str, Any]]:
    package_root = package_root.resolve(strict=True)
    ledger_path = package_root / "checksums.txt"
    if ledger_path.is_symlink() or not ledger_path.is_file():
        raise ValueError("package checksum ledger must be a regular non-symlink file")
    ledger_bytes = ledger_path.read_bytes()
    rows: list[dict[str, Any]] = []
    for relative, expected in _parse_ledger_bytes(ledger_bytes):
        candidate = _safe_file(package_root, relative)
        actual = sha256_file(candidate)
        if actual != expected:
            raise ValueError(f"package ledger hash drift: {relative}")
        rows.append({"path": relative, "bytes": candidate.stat().st_size, "sha256": actual})
    return rows


def freeze_package_snapshot(package_root: Path, session_root: Path) -> dict[str, Any]:
    """Copy the verified source ledger once; never copy model weights here."""
    package_root = package_root.resolve(strict=True)
    snapshot_root = session_root.resolve() / "snapshots"
    destination = snapshot_root / "package"
    if destination.exists():
        raise FileExistsError("session package snapshot already exists")
    rows = verify_package_ledger(package_root)
    destination.mkdir(parents=True)
    source_ledger = package_root / "checksums.txt"
    frozen_ledger = snapshot_root / "source_checksums.txt"
    if frozen_ledger.exists() or frozen_ledger.is_symlink():
        raise FileExistsError("session source checksum ledger already exists")
    shutil.copyfile(source_ledger, frozen_ledger)
    with frozen_ledger.open("rb") as stream:
        os.fsync(stream.fileno())
    if sha256_file(frozen_ledger) != sha256_file(source_ledger):
        raise OSError("session source checksum ledger copy hash mismatch")
    copied = []
    for row in rows:
        source = package_root / row["path"]
        target = destination / row["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        with target.open("rb") as stream:
            os.fsync(stream.fileno())
        if sha256_file(target) != row["sha256"]:
            raise OSError(f"session snapshot copy hash mismatch: {row['path']}")
        copied.append(dict(row))
    for directory in sorted(
        {path.parent for path in destination.rglob("*")},
        key=lambda path: len(path.parts), reverse=True,
    ):
        fsync_directory(directory)
    inventory = {
        "schema_version": "g25-session-package-snapshot-v1",
        "source_checksums_sha256": sha256_file(package_root / "checksums.txt"),
        "file_count": len(copied),
        "files": copied,
    }
    inventory["inventory_sha256"] = _canonical_hash(copied)
    atomic_json(snapshot_root / "inventory.json", inventory)
    fsync_directory(snapshot_root)
    return inventory


def audit_package_snapshot(session_root: Path) -> list[str]:
    findings: list[str] = []
    snapshot_root = session_root / "snapshots"
    inventory_path = snapshot_root / "inventory.json"
    try:
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"snapshot inventory unreadable: {exc}"]

    if not isinstance(inventory, Mapping) or set(inventory) != INVENTORY_KEYS:
        return ["snapshot inventory fields differ from the exact contract"]
    if inventory.get("schema_version") != "g25-session-package-snapshot-v1":
        findings.append("snapshot inventory schema version differs")
    source_hash = inventory.get("source_checksums_sha256")
    inventory_hash = inventory.get("inventory_sha256")
    if not isinstance(source_hash, str) or not SHA256_RE.fullmatch(source_hash):
        findings.append("snapshot source checksum hash is invalid")
    if not isinstance(inventory_hash, str) or not SHA256_RE.fullmatch(inventory_hash):
        findings.append("snapshot inventory hash is invalid")
    files = inventory.get("files")
    if not isinstance(files, list):
        return sorted(findings + ["snapshot inventory files must be an array"])
    count = inventory.get("file_count")
    if not isinstance(count, int) or isinstance(count, bool) or count != len(files):
        findings.append("snapshot inventory file_count differs from files")
    try:
        if inventory_hash != _canonical_hash(files):
            findings.append("snapshot inventory hash mismatch")
    except (TypeError, ValueError) as exc:
        findings.append(f"snapshot inventory cannot be canonically hashed: {exc}")

    expected_paths: list[str] = []
    expected_rows: list[tuple[str, str]] = []
    valid_rows: list[tuple[str, int, str]] = []
    for index, row in enumerate(files):
        if not isinstance(row, Mapping) or set(row) != ROW_KEYS:
            findings.append(f"snapshot inventory row fields differ at index {index}")
            continue
        try:
            relative = _safe_relative_path(row["path"])
        except ValueError as exc:
            findings.append(str(exc))
            continue
        size = row["bytes"]
        digest = row["sha256"]
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            findings.append(f"snapshot byte count is invalid: {relative}")
            continue
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            findings.append(f"snapshot file hash is invalid: {relative}")
            continue
        expected_paths.append(relative)
        expected_rows.append((relative, digest))
        valid_rows.append((relative, size, digest))
    duplicates = sorted({path for path in expected_paths if expected_paths.count(path) > 1})
    for relative in duplicates:
        findings.append(f"snapshot inventory path is duplicated: {relative}")

    frozen_ledger = snapshot_root / "source_checksums.txt"
    try:
        if frozen_ledger.is_symlink() or not frozen_ledger.is_file():
            raise ValueError("frozen source checksum ledger is missing or unsafe")
        frozen_bytes = frozen_ledger.read_bytes()
        if sha256_file(frozen_ledger) != source_hash:
            findings.append("snapshot source checksum hash mismatch")
        if _parse_ledger_bytes(frozen_bytes) != expected_rows:
            findings.append("snapshot inventory differs from frozen source checksum ledger")
    except (OSError, ValueError) as exc:
        findings.append(f"snapshot source checksum ledger invalid: {exc}")

    package = snapshot_root / "package"
    actual_paths: set[str] = set()
    if not package.is_dir() or package.is_symlink():
        findings.append("snapshot package root is missing or unsafe")
    else:
        for candidate in package.rglob("*"):
            relative = candidate.relative_to(package).as_posix()
            if candidate.is_symlink():
                findings.append(f"snapshot copied path is a symlink: {relative}")
            elif candidate.is_file():
                actual_paths.add(relative)
    expected_set = set(expected_paths)
    for relative in sorted(expected_set - actual_paths):
        findings.append(f"snapshot file missing: {relative}")
    for relative in sorted(actual_paths - expected_set):
        findings.append(f"snapshot file is extra: {relative}")

    for relative, size, digest in valid_rows:
        try:
            candidate = _safe_file(package, relative)
            if candidate.stat().st_size != size or sha256_file(candidate) != digest:
                findings.append(f"snapshot file mismatch: {relative}")
        except (OSError, ValueError) as exc:
            findings.append(f"snapshot file invalid: {relative}: {exc}")
    return sorted(findings)

#!/usr/bin/env python3
"""Standalone, fail-closed Phase 7 deployment bootstrap.

This file intentionally imports no project module.  It receives one
owner-pinned canonical deployment bundle from stdin, preserves the received
bytes in a fresh file, validates the complete bundle and immutable package
ledger, installs a read-only tree below a caller-pinned root, and publishes an
external receipt.  It contains no network, SSH, GPU, or third-party surface.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import ctypes
import errno
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Mapping, Sequence


BUNDLE_SCHEMA = "moe-simulator-phase7-deployment-bundle-v1"
RECEIPT_SCHEMA = "moe-simulator-phase7-deployment-receipt-v1"
LEDGER_SCHEMA = "moe-simulator-phase7-application-ledger-v2"
MUTABLE_APPROVAL_FILES = {
    "approval.template.json",
    "environment_disclosure_approval.template.json",
    "materialization_approval.template.json",
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_BUNDLE_BYTES = 64 * 1024 * 1024
MAX_PAYLOAD_BYTES = 48 * 1024 * 1024
MAX_MEMBER_BYTES = 16 * 1024 * 1024
MAX_MEMBERS = 2048
MAX_PATH_BYTES = 1024
MAX_PATH_DEPTH = 32
MAX_RECEIPT_BYTES = 4 * 1024 * 1024
READ_CHUNK_BYTES = 1024 * 1024
SEALED_FILE_MODES = {"0444", "0555"}
SEALED_DIRECTORY_MODE = 0o555
SAFE_ROOT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
SAFE_PATH_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,254}$")


class BootstrapError(RuntimeError):
    """A blocking receive, validation, installation, or replay failure."""


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BootstrapError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_float(value: str) -> None:
    raise BootstrapError(f"JSON floating-point values are forbidden: {value}")


def _load_json_bytes(payload: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_float=_reject_float,
            parse_constant=_reject_float,
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise BootstrapError(f"cannot load strict JSON {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise BootstrapError(f"strict JSON root must be an object: {label}")
    return value


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise BootstrapError(f"value is not canonical JSON: {exc}") from exc


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        raise BootstrapError(
            f"{label} key closure mismatch; "
            f"missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}"
        )


def _uint(value: Any, label: str, *, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BootstrapError(f"{label} must be an unsigned integer")
    if maximum is not None and value > maximum:
        raise BootstrapError(f"{label} exceeds its bounded maximum")
    return value


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise BootstrapError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _root_name(value: Any) -> str:
    if (
        not isinstance(value, str)
        or SAFE_ROOT_NAME_RE.fullmatch(value) is None
    ):
        raise BootstrapError("bundle root_name is invalid")
    return value


def _member_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise BootstrapError("bundle member path is invalid")
    if len(value.encode("utf-8")) > MAX_PATH_BYTES:
        raise BootstrapError("bundle member path exceeds its bounded maximum")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or len(path.parts) > MAX_PATH_DEPTH
        or any(
            part in {"", ".", ".."}
            or SAFE_PATH_COMPONENT_RE.fullmatch(part) is None
            for part in path.parts
        )
    ):
        raise BootstrapError(f"non-canonical or traversing bundle path: {value!r}")
    return value


def _parent_directories(paths: Sequence[str]) -> set[str]:
    result: set[str] = set()
    for value in paths:
        parts = PurePosixPath(value).parts
        for count in range(1, len(parts)):
            result.add(PurePosixPath(*parts[:count]).as_posix())
    return result


def _ledger(root_name: str, members: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    immutable = [
        {
            "path": item["path"],
            "size_bytes": item["size_bytes"],
            "sha256": item["sha256"],
        }
        for item in members
        if item["path"] not in MUTABLE_APPROVAL_FILES
    ]
    immutable.sort(key=lambda item: item["path"])
    if not immutable:
        raise BootstrapError("immutable package ledger cannot be empty")
    rows = [
        f"{item['sha256']}  {item['path']}\n".encode("utf-8")
        for item in immutable
    ]
    return {
        "schema_version": LEDGER_SCHEMA,
        "root_name": root_name,
        "member_count": len(immutable),
        "members": immutable,
        "ledger_sha256": hashlib.sha256(b"".join(rows)).hexdigest(),
        "excluded_mutable_approval_files": sorted(MUTABLE_APPROVAL_FILES),
    }


def validate_bundle(payload: bytes) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Strictly validate one bounded canonical deployment bundle."""

    if not payload or len(payload) > MAX_BUNDLE_BYTES:
        raise BootstrapError("bundle byte length is empty or outside the hard bound")
    bundle = _load_json_bytes(payload, "deployment bundle")
    if _canonical_bytes(bundle) != payload:
        raise BootstrapError("deployment bundle is not canonical JSON")
    _exact_keys(
        bundle,
        {
            "schema_version",
            "root_name",
            "member_count",
            "total_payload_bytes",
            "members",
            "package_ledger",
        },
        "deployment bundle",
    )
    if bundle["schema_version"] != BUNDLE_SCHEMA:
        raise BootstrapError("unsupported deployment bundle schema")
    bundle_root_name = _root_name(bundle["root_name"])
    member_count = _uint(bundle["member_count"], "member_count", maximum=MAX_MEMBERS)
    total_declared = _uint(
        bundle["total_payload_bytes"],
        "total_payload_bytes",
        maximum=MAX_PAYLOAD_BYTES,
    )
    raw_members = bundle["members"]
    if not isinstance(raw_members, list) or not raw_members:
        raise BootstrapError("bundle members must be a nonempty array")
    if len(raw_members) != member_count:
        raise BootstrapError("bundle member_count mismatch")

    decoded: dict[str, bytes] = {}
    normalized: list[dict[str, Any]] = []
    previous_path: str | None = None
    total = 0
    for index, member in enumerate(raw_members):
        if not isinstance(member, dict):
            raise BootstrapError(f"bundle member {index} must be an object")
        _exact_keys(
            member,
            {"content_base64", "mode_octal", "path", "sha256", "size_bytes"},
            f"bundle member {index}",
        )
        path = _member_path(member["path"])
        if path in decoded:
            raise BootstrapError(f"duplicate bundle member path: {path}")
        if previous_path is not None and path <= previous_path:
            raise BootstrapError("bundle members must use strict path order")
        previous_path = path
        if member["mode_octal"] not in SEALED_FILE_MODES:
            raise BootstrapError(f"invalid sealed mode for bundle member: {path}")
        size = _uint(member["size_bytes"], f"size_bytes for {path}", maximum=MAX_MEMBER_BYTES)
        digest = _sha256(member["sha256"], f"sha256 for {path}")
        encoded = member["content_base64"]
        if not isinstance(encoded, str):
            raise BootstrapError(f"content_base64 must be a string: {path}")
        try:
            content = base64.b64decode(encoded.encode("ascii"), validate=True)
        except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
            raise BootstrapError(f"invalid base64 for bundle member {path}: {exc}") from exc
        if base64.b64encode(content).decode("ascii") != encoded:
            raise BootstrapError(f"non-canonical base64 for bundle member: {path}")
        if len(content) != size or hashlib.sha256(content).hexdigest() != digest:
            raise BootstrapError(f"bundle member size/hash drift: {path}")
        total += size
        if total > MAX_PAYLOAD_BYTES:
            raise BootstrapError("decoded bundle payload exceeds the hard bound")
        decoded[path] = content
        normalized.append(member)
    if total != total_declared:
        raise BootstrapError("bundle total_payload_bytes mismatch")
    missing = MUTABLE_APPROVAL_FILES - set(decoded)
    if missing:
        raise BootstrapError(f"bundle omitted mutable approval templates: {sorted(missing)}")
    for path in decoded:
        parts = PurePosixPath(path).parts
        for count in range(1, len(parts)):
            if PurePosixPath(*parts[:count]).as_posix() in decoded:
                raise BootstrapError(f"bundle file/directory prefix collision: {path}")
    if bundle["package_ledger"] != _ledger(bundle_root_name, normalized):
        raise BootstrapError("bundle immutable package ledger mismatch")
    return bundle, decoded


def _canonical_absolute(path: Path, label: str) -> Path:
    value = Path(path)
    if not value.is_absolute() or "\\" in str(value) or ".." in value.parts:
        raise BootstrapError(f"{label} must be a canonical absolute path")
    if os.path.normpath(str(value)) != str(value):
        raise BootstrapError(f"{label} contains a path normalization alias")
    return value


def _real_directory(path: Path, label: str) -> Path:
    value = _canonical_absolute(path, label)
    try:
        observed = value.lstat()
        resolved = value.resolve(strict=True)
    except OSError as exc:
        raise BootstrapError(f"cannot resolve {label}: {exc}") from exc
    if stat.S_ISLNK(observed.st_mode) or not stat.S_ISDIR(observed.st_mode) or resolved != value:
        raise BootstrapError(f"{label} must be an existing real directory without symlink aliases")
    return value


def _below_root(path: Path, root: Path, label: str) -> Path:
    value = _canonical_absolute(path, label)
    if value == root or not value.is_relative_to(root):
        raise BootstrapError(f"{label} must be strictly below the allowed root")
    parent = _real_directory(value.parent, f"{label} parent")
    if not parent.is_relative_to(root):
        raise BootstrapError(f"{label} parent escapes the allowed root")
    return value


def _unescape_mountinfo(value: str) -> str:
    for encoded, decoded in (
        ("\\040", " "),
        ("\\011", "\t"),
        ("\\012", "\n"),
        ("\\134", "\\"),
    ):
        value = value.replace(encoded, decoded)
    return value


def _mount_identity(
    path: Path,
    *,
    mountinfo_path: Path = Path("/proc/self/mountinfo"),
    boot_id_path: Path = Path("/proc/sys/kernel/random/boot_id"),
) -> dict[str, Any]:
    """Reproduce the D0 mount identity without importing project modules."""

    root = _real_directory(path, "persistent mount")
    raw = mountinfo_path.read_bytes()
    selected: dict[str, Any] | None = None
    for line in raw.decode("utf-8", errors="strict").splitlines():
        left, separator, right = line.partition(" - ")
        if not separator:
            raise BootstrapError("malformed mountinfo row")
        fields = left.split()
        tail = right.split()
        if len(fields) < 6 or len(tail) < 3:
            raise BootstrapError("truncated mountinfo row")
        if _unescape_mountinfo(fields[4]) != str(root):
            continue
        selected = {
            "mount_id": int(fields[0]),
            "parent_id": int(fields[1]),
            "major_minor": fields[2],
            "root": _unescape_mountinfo(fields[3]),
            "mount_point": _unescape_mountinfo(fields[4]),
            "mount_options": sorted(fields[5].split(",")),
            "filesystem_type": tail[0],
            "mount_source": _unescape_mountinfo(tail[1]),
            "super_options": sorted(tail[2].split(",")),
            "device_id": root.stat().st_dev,
            "boot_id": boot_id_path.read_text(encoding="utf-8").strip(),
            "mountinfo_sha256": hashlib.sha256(raw).hexdigest(),
        }
        break
    if selected is None:
        raise BootstrapError("persistent root is not an explicit mount point")
    identity_fields = {
        key: value for key, value in selected.items() if key != "mountinfo_sha256"
    }
    selected["mount_identity_sha256"] = hashlib.sha256(
        _canonical_bytes(identity_fields)
    ).hexdigest()
    return selected


def _relative_directory(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise BootstrapError("prepared directory must be a canonical relative path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or len(path.parts) > MAX_PATH_DEPTH
        or any(
            part in {"", ".", ".."}
            or SAFE_PATH_COMPONENT_RE.fullmatch(part) is None
            for part in path.parts
        )
    ):
        raise BootstrapError("prepared directory path is unsafe")
    return path


def _lexists(path: Path) -> bool:
    return os.path.lexists(os.fspath(path))


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise BootstrapError(f"cannot open directory for fsync {path}: {exc}") from exc
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise BootstrapError(f"cannot fsync directory {path}: {exc}") from exc
    finally:
        os.close(descriptor)


def initialize_project_root(
    *,
    allowed_root: Path,
    project_root: Path,
    relative_directories: Sequence[str],
    expected_mount_identity_sha256: str,
) -> Path:
    """Create one fresh direct-child project envelope and its exact layout."""

    root = _real_directory(Path(allowed_root), "allowed root")
    expected = _sha256(
        expected_mount_identity_sha256, "expected_mount_identity_sha256"
    )
    observed = _mount_identity(root)
    if observed["mount_identity_sha256"] != expected:
        raise BootstrapError("persistent mount identity differs from D0")
    project = _canonical_absolute(Path(project_root), "project root")
    if project.parent != root or project.name in {"", ".", ".."}:
        raise BootstrapError("project root must be a fresh direct child of allowed root")
    if SAFE_ROOT_NAME_RE.fullmatch(project.name) is None:
        raise BootstrapError("project root name is unsafe")
    if _lexists(project):
        raise BootstrapError("project root already exists")

    requested = [_relative_directory(value) for value in relative_directories]
    if not requested or len({path.as_posix() for path in requested}) != len(requested):
        raise BootstrapError("prepared directory list must be nonempty and unique")
    expanded: set[PurePosixPath] = set()
    for path in requested:
        for count in range(1, len(path.parts) + 1):
            expanded.add(PurePosixPath(*path.parts[:count]))

    try:
        project.mkdir(mode=0o700, exist_ok=False)
    except OSError as exc:
        raise BootstrapError(f"cannot create fresh project root: {exc}") from exc
    _fsync_directory(root)
    for relative in sorted(expanded, key=lambda item: (len(item.parts), item.as_posix())):
        destination = project.joinpath(*relative.parts)
        parent = destination.parent
        if parent.resolve(strict=True) != parent or parent.is_symlink():
            raise BootstrapError("prepared directory parent identity changed")
        try:
            destination.mkdir(mode=0o700, exist_ok=False)
        except OSError as exc:
            raise BootstrapError(
                f"cannot create prepared project directory {relative}: {exc}"
            ) from exc
        _fsync_directory(parent)
    _fsync_directory(project)
    return project


def _open_fresh(path: Path) -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        return os.open(path, flags, 0o600)
    except OSError as exc:
        raise BootstrapError(f"refusing non-fresh output file {path}: {exc}") from exc


def _write_all(descriptor: int, payload: bytes, label: str) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise BootstrapError(f"short write while creating {label}")
        offset += written


def _write_fresh(path: Path, payload: bytes, final_mode: int) -> None:
    descriptor = _open_fresh(path)
    try:
        _write_all(descriptor, payload, str(path))
        os.fsync(descriptor)
        os.fchmod(descriptor, final_mode)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)


def receive_bundle(
    stream: BinaryIO,
    incoming: Path,
    *,
    expected_size: int,
    expected_sha256: str,
) -> bytes:
    """Receive exactly the owner-pinned bytes into one fresh incoming file."""

    expected_size = _uint(expected_size, "expected_size", maximum=MAX_BUNDLE_BYTES)
    if expected_size == 0:
        raise BootstrapError("expected_size must be positive")
    expected_sha256 = _sha256(expected_sha256, "expected_sha256")
    descriptor = _open_fresh(incoming)
    digest = hashlib.sha256()
    blocks: list[bytes] = []
    remaining = expected_size
    try:
        while remaining:
            block = stream.read(min(READ_CHUNK_BYTES, remaining))
            if not isinstance(block, (bytes, bytearray)):
                raise BootstrapError("stdin bundle stream must be binary")
            if not block:
                raise BootstrapError("stdin ended before expected bundle size")
            block = bytes(block)
            if len(block) > remaining:
                raise BootstrapError("stdin stream violated its bounded read contract")
            _write_all(descriptor, block, str(incoming))
            digest.update(block)
            blocks.append(block)
            remaining -= len(block)
        extra = stream.read(1)
        if not isinstance(extra, (bytes, bytearray)):
            raise BootstrapError("stdin bundle stream must be binary")
        if extra:
            raise BootstrapError("stdin contains bytes beyond expected bundle size")
        os.fsync(descriptor)
        if digest.hexdigest() != expected_sha256:
            raise BootstrapError("received bundle SHA-256 differs from owner-pinned value")
        payload = b"".join(blocks)
        validate_bundle(payload)
        os.fchmod(descriptor, 0o444)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(incoming.parent)
    return payload


def _read_regular(path: Path, maximum: int, label: str) -> tuple[bytes, os.stat_result]:
    try:
        before = path.lstat()
    except OSError as exc:
        raise BootstrapError(f"cannot stat {label}: {exc}") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise BootstrapError(f"{label} must be a non-symlink regular file")
    if before.st_size > maximum:
        raise BootstrapError(f"{label} exceeds its hard size bound")
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise BootstrapError(f"cannot open {label} without following symlinks: {exc}") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise BootstrapError(f"{label} identity changed while opening")
        blocks: list[bytes] = []
        observed = 0
        while True:
            block = os.read(descriptor, min(READ_CHUNK_BYTES, maximum + 1 - observed))
            if not block:
                break
            blocks.append(block)
            observed += len(block)
            if observed > maximum:
                raise BootstrapError(f"{label} exceeds its hard size bound")
        after = os.fstat(descriptor)
        if (
            observed != opened.st_size
            or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        ):
            raise BootstrapError(f"{label} changed while being read")
        return b"".join(blocks), opened
    finally:
        os.close(descriptor)


def _required_directories(bundle: Mapping[str, Any]) -> list[str]:
    values = _parent_directories([member["path"] for member in bundle["members"]])
    return sorted(values, key=lambda value: (len(PurePosixPath(value).parts), value))


def _collect_installed(root: Path, root_name: str) -> dict[str, Any]:
    try:
        root_stat = root.lstat()
    except OSError as exc:
        raise BootstrapError(f"cannot stat installed root: {exc}") from exc
    if (
        stat.S_ISLNK(root_stat.st_mode)
        or not stat.S_ISDIR(root_stat.st_mode)
        or stat.S_IMODE(root_stat.st_mode) != SEALED_DIRECTORY_MODE
    ):
        raise BootstrapError("installed root is not a read-only non-symlink directory")
    members: list[dict[str, Any]] = []
    observed_directories: set[str] = set()

    def visit(directory: Path, relative_parts: tuple[str, ...]) -> None:
        try:
            with os.scandir(directory) as stream:
                entries = sorted(stream, key=lambda entry: entry.name)
        except OSError as exc:
            raise BootstrapError(f"cannot enumerate installed directory: {exc}") from exc
        for entry in entries:
            relative = relative_parts + (entry.name,)
            relative_text = PurePosixPath(*relative).as_posix()
            try:
                observed = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise BootstrapError(f"cannot stat installed member {relative_text}: {exc}") from exc
            if entry.is_symlink():
                raise BootstrapError(f"installed symlink is forbidden: {relative_text}")
            if stat.S_ISDIR(observed.st_mode):
                if "__pycache__" in relative:
                    raise BootstrapError(f"installed cache directory is forbidden: {relative_text}")
                if stat.S_IMODE(observed.st_mode) != SEALED_DIRECTORY_MODE:
                    raise BootstrapError(f"installed directory mode drift: {relative_text}")
                observed_directories.add(relative_text)
                visit(Path(entry.path), relative)
                continue
            if not stat.S_ISREG(observed.st_mode):
                raise BootstrapError(f"installed non-regular member is forbidden: {relative_text}")
            if "__pycache__" in relative or relative_text.endswith(".pyc"):
                raise BootstrapError(f"installed bytecode member is forbidden: {relative_text}")
            path = _member_path(relative_text)
            content, captured = _read_regular(Path(entry.path), MAX_MEMBER_BYTES, path)
            mode_octal = f"{stat.S_IMODE(captured.st_mode):04o}"
            if mode_octal not in SEALED_FILE_MODES:
                raise BootstrapError(f"installed file mode drift: {path}")
            members.append(
                {
                    "content_base64": base64.b64encode(content).decode("ascii"),
                    "mode_octal": mode_octal,
                    "path": path,
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "size_bytes": len(content),
                }
            )

    visit(root, ())
    members.sort(key=lambda member: member["path"])
    if not members:
        raise BootstrapError("installed application cannot be empty")
    required = _parent_directories([member["path"] for member in members])
    extras = observed_directories - required
    if extras:
        raise BootstrapError(f"installed tree contains unrepresented directories: {sorted(extras)}")
    total = sum(member["size_bytes"] for member in members)
    return {
        "schema_version": BUNDLE_SCHEMA,
        "root_name": root_name,
        "member_count": len(members),
        "total_payload_bytes": total,
        "members": members,
        "package_ledger": _ledger(root_name, members),
    }


def _materialize(stage: Path, bundle: Mapping[str, Any], decoded: Mapping[str, bytes]) -> None:
    try:
        stage.mkdir(mode=0o700)
    except OSError as exc:
        raise BootstrapError(f"cannot create fresh staged directory: {exc}") from exc
    directories = _required_directories(bundle)
    for relative in directories:
        destination = stage.joinpath(*PurePosixPath(relative).parts)
        try:
            destination.mkdir(mode=0o700)
        except OSError as exc:
            raise BootstrapError(f"cannot create staged directory {relative}: {exc}") from exc
    for member in bundle["members"]:
        relative = member["path"]
        destination = stage.joinpath(*PurePosixPath(relative).parts)
        _write_fresh(destination, decoded[relative], int(member["mode_octal"], 8))
        content, captured = _read_regular(destination, MAX_MEMBER_BYTES, relative)
        if (
            stat.S_IMODE(captured.st_mode) != int(member["mode_octal"], 8)
            or len(content) != member["size_bytes"]
            or hashlib.sha256(content).hexdigest() != member["sha256"]
        ):
            raise BootstrapError(f"staged member mode/size/hash drift: {relative}")
    directory_paths = [stage.joinpath(*PurePosixPath(value).parts) for value in directories]
    for directory in sorted(directory_paths, key=lambda value: len(value.parts), reverse=True):
        _fsync_directory(directory)
        os.chmod(directory, SEALED_DIRECTORY_MODE, follow_symlinks=False)
    _fsync_directory(stage)
    os.chmod(stage, SEALED_DIRECTORY_MODE, follow_symlinks=False)
    _fsync_directory(stage)


def _rename_noreplace(source: Path, destination: Path) -> None:
    library = ctypes.CDLL(None, use_errno=True)
    function = getattr(library, "renameat2", None)
    if function is None:
        raise BootstrapError("atomic renameat2(RENAME_NOREPLACE) is unavailable")
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    result = function(-100, os.fsencode(source), -100, os.fsencode(destination), 1)
    if result != 0:
        observed_errno = ctypes.get_errno()
        if observed_errno == errno.EEXIST:
            raise BootstrapError(f"refusing to replace published path: {destination}")
        raise BootstrapError(
            f"atomic no-replace rename failed {source} -> {destination}: "
            f"{os.strerror(observed_errno)}"
        )


def _receipt(
    root: Path,
    target: Path,
    bundle: Mapping[str, Any],
    bundle_sha256: str,
) -> dict[str, Any]:
    return {
        "schema_version": RECEIPT_SCHEMA,
        "installation_status": "COMPLETE",
        "allowed_root": str(root),
        "target": str(target),
        "bundle_root_name": bundle["root_name"],
        "bundle_sha256": bundle_sha256,
        "package_ledger": bundle["package_ledger"],
        "member_count": bundle["member_count"],
        "total_payload_bytes": bundle["total_payload_bytes"],
        "sealed_file_modes": sorted(SEALED_FILE_MODES),
        "sealed_directory_mode": "0555",
    }


def _verify_receipt_and_target(
    root: Path,
    target: Path,
    receipt_path: Path,
    expected_bundle: Mapping[str, Any],
    expected_bundle_sha256: str,
) -> dict[str, Any]:
    try:
        receipt_stat = receipt_path.lstat()
    except OSError as exc:
        raise BootstrapError(f"deployment receipt is missing: {exc}") from exc
    if (
        stat.S_ISLNK(receipt_stat.st_mode)
        or not stat.S_ISREG(receipt_stat.st_mode)
        or stat.S_IMODE(receipt_stat.st_mode) != 0o444
    ):
        raise BootstrapError("deployment receipt is not a sealed regular file")
    receipt_payload, _ = _read_regular(receipt_path, MAX_RECEIPT_BYTES, "deployment receipt")
    receipt = _load_json_bytes(receipt_payload, "deployment receipt")
    if _canonical_bytes(receipt) != receipt_payload:
        raise BootstrapError("deployment receipt is not canonical JSON")
    expected_receipt = _receipt(root, target, expected_bundle, expected_bundle_sha256)
    if receipt != expected_receipt:
        raise BootstrapError("deployment receipt differs from its expected exact value")
    reconstructed = _collect_installed(target, expected_bundle["root_name"])
    if reconstructed != expected_bundle:
        raise BootstrapError("installed target differs from its canonical deployment bundle")
    if hashlib.sha256(_canonical_bytes(reconstructed)).hexdigest() != expected_bundle_sha256:
        raise BootstrapError("installed target bundle SHA-256 mismatch")
    return receipt


def bootstrap(
    stream: BinaryIO,
    *,
    allowed_root: Path,
    incoming: Path,
    target: Path,
    receipt: Path,
    expected_size: int,
    expected_sha256: str,
) -> dict[str, Any]:
    """Receive, validate, install, seal, and replay-verify one fresh bundle."""

    root = _real_directory(Path(allowed_root), "allowed root")
    incoming_path = _below_root(Path(incoming), root, "incoming bundle")
    target_path = _below_root(Path(target), root, "installation target")
    receipt_path = _below_root(Path(receipt), root, "installation receipt")
    if len({incoming_path, target_path, receipt_path}) != 3:
        raise BootstrapError("incoming, target, and receipt paths must be distinct")
    if incoming_path.is_relative_to(target_path) or receipt_path.is_relative_to(target_path):
        raise BootstrapError("incoming bundle and receipt must be outside installation target")
    if any(_lexists(path) for path in (incoming_path, target_path, receipt_path)):
        raise BootstrapError("incoming bundle, target, and receipt must all be fresh")
    expected_size = _uint(expected_size, "expected_size", maximum=MAX_BUNDLE_BYTES)
    if expected_size == 0:
        raise BootstrapError("expected_size must be positive")
    expected_sha256 = _sha256(expected_sha256, "expected_sha256")

    payload = receive_bundle(
        stream,
        incoming_path,
        expected_size=expected_size,
        expected_sha256=expected_sha256,
    )
    bundle, decoded = validate_bundle(payload)
    stage = target_path.with_name(
        f".{target_path.name}.bootstrap-{expected_sha256[:16]}.staged"
    )
    receipt_stage = receipt_path.with_name(
        f".{receipt_path.name}.bootstrap-{expected_sha256[:16]}.staged"
    )
    if any(_lexists(path) for path in (stage, receipt_stage)):
        raise BootstrapError("bootstrap staged path is not fresh")

    _materialize(stage, bundle, decoded)
    if _collect_installed(stage, bundle["root_name"]) != bundle:
        raise BootstrapError("staged target differs from its canonical bundle")
    receipt_value = _receipt(root, target_path, bundle, expected_sha256)
    _write_fresh(receipt_stage, _canonical_bytes(receipt_value), 0o444)
    _rename_noreplace(stage, target_path)
    _fsync_directory(target_path.parent)
    _rename_noreplace(receipt_stage, receipt_path)
    _fsync_directory(receipt_path.parent)
    return _verify_receipt_and_target(
        root,
        target_path,
        receipt_path,
        bundle,
        expected_sha256,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allowed-root", type=Path, required=True)
    parser.add_argument("--incoming", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--expected-size", type=int, required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--expected-mount-identity-sha256")
    parser.add_argument(
        "--prepare-relative-dir", action="append", default=[]
    )
    args = parser.parse_args()
    initialize_requested = any(
        (
            args.project_root is not None,
            args.expected_mount_identity_sha256 is not None,
            bool(args.prepare_relative_dir),
        )
    )
    if initialize_requested:
        if (
            args.project_root is None
            or args.expected_mount_identity_sha256 is None
            or not args.prepare_relative_dir
        ):
            raise BootstrapError("project initialization arguments are incomplete")
        initialize_project_root(
            allowed_root=args.allowed_root,
            project_root=args.project_root,
            relative_directories=args.prepare_relative_dir,
            expected_mount_identity_sha256=args.expected_mount_identity_sha256,
        )
    value = bootstrap(
        sys.stdin.buffer,
        allowed_root=args.allowed_root,
        incoming=args.incoming,
        target=args.target,
        receipt=args.receipt,
        expected_size=args.expected_size,
        expected_sha256=args.expected_sha256,
    )
    print(value["bundle_sha256"])
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BootstrapError as exc:
        raise SystemExit(f"HARD-STOP: {exc}") from exc

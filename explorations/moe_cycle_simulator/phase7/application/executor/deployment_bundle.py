#!/usr/bin/env python3
"""Build, receive, install, and verify a sealed Phase 7 application bundle.

The transport format is canonical JSON with base64-encoded regular files.  This
module deliberately has no network, SSH, GPU, or third-party-library surface.
It is suitable for copying over a separately owner-approved channel and then
installing below a caller-pinned persistent-storage root.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import ctypes
import errno
import hashlib
import os
import re
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[5]))

from explorations.moe_cycle_simulator.phase7.application.executor.common import (  # noqa: E402
    M0Error,
    SHA256_RE,
    canonical_bytes,
    load_json_bytes,
)
from explorations.moe_cycle_simulator.phase7.application.executor.package_ledger import (  # noqa: E402
    EXCLUSIONS as MUTABLE_APPROVAL_FILES,
)


BUNDLE_SCHEMA = "moe-simulator-phase7-deployment-bundle-v1"
RECEIPT_SCHEMA = "moe-simulator-phase7-deployment-receipt-v1"
DEFAULT_MAX_BUNDLE_BYTES = 64 * 1024 * 1024
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


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        raise M0Error(
            f"{label} key closure mismatch; "
            f"missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}"
        )


def _uint(value: Any, label: str, *, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise M0Error(f"{label} must be an unsigned integer")
    if maximum is not None and value > maximum:
        raise M0Error(f"{label} exceeds its bounded maximum")
    return value


def _bounded_bundle_limit(value: Any) -> int:
    limit = _uint(
        value,
        "max_bundle_bytes",
        maximum=DEFAULT_MAX_BUNDLE_BYTES,
    )
    if limit == 0:
        raise M0Error("max_bundle_bytes must be positive")
    return limit


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise M0Error(f"{label} must be a lowercase SHA-256 digest")
    return value


def _validate_root_name(value: Any) -> str:
    if (
        not isinstance(value, str)
        or SAFE_ROOT_NAME_RE.fullmatch(value) is None
    ):
        raise M0Error("bundle root_name is invalid")
    return value


def _validate_member_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise M0Error("bundle member path is invalid")
    if len(value.encode("utf-8")) > MAX_PATH_BYTES:
        raise M0Error("bundle member path exceeds its bounded maximum")
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
        raise M0Error(f"non-canonical or traversing bundle member path: {value!r}")
    return value


def _relative_parent_directories(paths: Sequence[str]) -> set[str]:
    result: set[str] = set()
    for value in paths:
        parts = PurePosixPath(value).parts
        for count in range(1, len(parts)):
            result.add(PurePosixPath(*parts[:count]).as_posix())
    return result


def _read_regular_file(path: Path, *, maximum: int, label: str) -> tuple[bytes, os.stat_result]:
    try:
        before = path.lstat()
    except OSError as exc:
        raise M0Error(f"cannot stat {label}: {exc}") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise M0Error(f"{label} must be a non-symlink regular file")
    if before.st_size > maximum:
        raise M0Error(f"{label} exceeds its bounded maximum")
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise M0Error(f"cannot open {label} without following symlinks: {exc}") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise M0Error(f"{label} identity changed while opening")
        blocks: list[bytes] = []
        observed = 0
        while True:
            block = os.read(descriptor, min(READ_CHUNK_BYTES, maximum + 1 - observed))
            if not block:
                break
            blocks.append(block)
            observed += len(block)
            if observed > maximum:
                raise M0Error(f"{label} exceeds its bounded maximum")
        after = os.fstat(descriptor)
        if (
            observed != opened.st_size
            or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        ):
            raise M0Error(f"{label} changed while being captured")
        return b"".join(blocks), opened
    finally:
        os.close(descriptor)


def _collect_members(
    root: Path,
    *,
    require_sealed: bool,
) -> list[dict[str, Any]]:
    try:
        root_stat = root.lstat()
    except OSError as exc:
        raise M0Error(f"cannot stat application root: {exc}") from exc
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise M0Error("application root must be a non-symlink directory")
    if require_sealed and stat.S_IMODE(root_stat.st_mode) != SEALED_DIRECTORY_MODE:
        raise M0Error("installed application root is not read-only sealed")

    members: list[dict[str, Any]] = []
    observed_directories: set[str] = set()

    def visit(directory: Path, relative_parts: tuple[str, ...]) -> None:
        try:
            with os.scandir(directory) as stream:
                entries = sorted(stream, key=lambda item: item.name)
        except OSError as exc:
            raise M0Error(f"cannot enumerate application directory {directory}: {exc}") from exc
        for entry in entries:
            relative = relative_parts + (entry.name,)
            relative_text = PurePosixPath(*relative).as_posix()
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise M0Error(f"cannot stat application member {relative_text}: {exc}") from exc
            if entry.is_symlink():
                raise M0Error(f"application symlink is forbidden: {relative_text}")
            excluded_cache = "__pycache__" in relative
            if stat.S_ISDIR(entry_stat.st_mode):
                if require_sealed and excluded_cache:
                    raise M0Error(f"unexpected installed cache directory: {relative_text}")
                if not excluded_cache:
                    observed_directories.add(relative_text)
                    if require_sealed and stat.S_IMODE(entry_stat.st_mode) != SEALED_DIRECTORY_MODE:
                        raise M0Error(f"installed directory mode drift: {relative_text}")
                visit(Path(entry.path), relative)
                continue
            if not stat.S_ISREG(entry_stat.st_mode):
                raise M0Error(f"non-regular application member is forbidden: {relative_text}")
            if excluded_cache or relative_text.endswith(".pyc"):
                if require_sealed:
                    raise M0Error(f"unexpected installed bytecode member: {relative_text}")
                continue
            canonical_path = _validate_member_path(relative_text)
            payload, captured_stat = _read_regular_file(
                Path(entry.path), maximum=MAX_MEMBER_BYTES, label=canonical_path
            )
            source_mode = stat.S_IMODE(captured_stat.st_mode)
            if require_sealed:
                mode_octal = f"{source_mode:04o}"
                if mode_octal not in SEALED_FILE_MODES:
                    raise M0Error(f"installed file mode drift: {canonical_path}")
            else:
                mode_octal = "0555" if source_mode & 0o111 else "0444"
            members.append(
                {
                    "content_base64": base64.b64encode(payload).decode("ascii"),
                    "mode_octal": mode_octal,
                    "path": canonical_path,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "size_bytes": len(payload),
                }
            )

    visit(root, ())
    members.sort(key=lambda item: item["path"])
    if not members:
        raise M0Error("deployment bundle cannot be empty")
    required_directories = _relative_parent_directories(
        [item["path"] for item in members]
    )
    extra_directories = observed_directories - required_directories
    if extra_directories:
        raise M0Error(
            "empty or unrepresented application directories are forbidden: "
            f"{sorted(extra_directories)}"
        )
    return members


def _ledger_from_members(root_name: str, members: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
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
        raise M0Error("immutable application package ledger is empty")
    rows = [
        f"{item['sha256']}  {item['path']}\n".encode("utf-8")
        for item in immutable
    ]
    return {
        "schema_version": "moe-simulator-phase7-application-ledger-v2",
        "root_name": root_name,
        "member_count": len(immutable),
        "members": immutable,
        "ledger_sha256": hashlib.sha256(b"".join(rows)).hexdigest(),
        "excluded_mutable_approval_files": sorted(MUTABLE_APPROVAL_FILES),
    }


def _bundle_object(
    root: Path,
    *,
    root_name: str | None = None,
    require_sealed: bool = False,
) -> dict[str, Any]:
    members = _collect_members(root, require_sealed=require_sealed)
    effective_root_name = _validate_root_name(root_name if root_name is not None else root.name)
    paths = {item["path"] for item in members}
    missing_approvals = set(MUTABLE_APPROVAL_FILES) - paths
    if missing_approvals:
        raise M0Error(
            "deployment bundle must contain every mutable approval template: "
            f"{sorted(missing_approvals)}"
        )
    total_payload_bytes = sum(item["size_bytes"] for item in members)
    if len(members) > MAX_MEMBERS or total_payload_bytes > MAX_PAYLOAD_BYTES:
        raise M0Error("deployment bundle payload exceeds its bounded maximum")
    return {
        "schema_version": BUNDLE_SCHEMA,
        "root_name": effective_root_name,
        "member_count": len(members),
        "total_payload_bytes": total_payload_bytes,
        "members": members,
        "package_ledger": _ledger_from_members(effective_root_name, members),
    }


def build_bundle(source_root: Path, *, max_bundle_bytes: int = DEFAULT_MAX_BUNDLE_BYTES) -> bytes:
    """Return one canonical, bounded deployment bundle for ``source_root``."""

    max_bundle_bytes = _bounded_bundle_limit(max_bundle_bytes)
    source = Path(source_root)
    if source.is_symlink():
        raise M0Error("application source root symlink is forbidden")
    try:
        source = source.resolve(strict=True)
    except OSError as exc:
        raise M0Error(f"cannot resolve application source root: {exc}") from exc
    payload = canonical_bytes(_bundle_object(source))
    if len(payload) > max_bundle_bytes:
        raise M0Error("canonical deployment bundle exceeds max_bundle_bytes")
    return payload


def _validated_bundle(
    payload: bytes,
    *,
    max_bundle_bytes: int = DEFAULT_MAX_BUNDLE_BYTES,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    max_bundle_bytes = _bounded_bundle_limit(max_bundle_bytes)
    if not payload or len(payload) > max_bundle_bytes:
        raise M0Error("deployment bundle byte length is empty or out of bounds")
    bundle = load_json_bytes(payload, "deployment bundle")
    if canonical_bytes(bundle) != payload:
        raise M0Error("deployment bundle is not canonical JSON")
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
        raise M0Error("unsupported deployment bundle schema")
    root_name = _validate_root_name(bundle["root_name"])
    member_count = _uint(bundle["member_count"], "member_count", maximum=MAX_MEMBERS)
    declared_total = _uint(
        bundle["total_payload_bytes"],
        "total_payload_bytes",
        maximum=MAX_PAYLOAD_BYTES,
    )
    raw_members = bundle["members"]
    if not isinstance(raw_members, list) or not raw_members:
        raise M0Error("deployment bundle members must be a nonempty array")
    if len(raw_members) != member_count:
        raise M0Error("deployment bundle member_count mismatch")

    decoded: dict[str, bytes] = {}
    normalized: list[dict[str, Any]] = []
    total = 0
    previous_path: str | None = None
    for index, raw_member in enumerate(raw_members):
        if not isinstance(raw_member, dict):
            raise M0Error(f"bundle member {index} must be an object")
        _exact_keys(
            raw_member,
            {"content_base64", "mode_octal", "path", "sha256", "size_bytes"},
            f"bundle member {index}",
        )
        path = _validate_member_path(raw_member["path"])
        if path in decoded:
            raise M0Error(f"duplicate deployment bundle path: {path}")
        if previous_path is not None and path <= previous_path:
            raise M0Error("deployment bundle members must use strict path order")
        previous_path = path
        if raw_member["mode_octal"] not in SEALED_FILE_MODES:
            raise M0Error(f"invalid sealed mode for bundle member: {path}")
        size = _uint(raw_member["size_bytes"], f"size_bytes for {path}", maximum=MAX_MEMBER_BYTES)
        digest = _sha256(raw_member["sha256"], f"sha256 for {path}")
        encoded = raw_member["content_base64"]
        if not isinstance(encoded, str):
            raise M0Error(f"content_base64 must be a string: {path}")
        try:
            content = base64.b64decode(encoded.encode("ascii"), validate=True)
        except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
            raise M0Error(f"invalid base64 content for {path}: {exc}") from exc
        if base64.b64encode(content).decode("ascii") != encoded:
            raise M0Error(f"non-canonical base64 content for {path}")
        if len(content) != size or hashlib.sha256(content).hexdigest() != digest:
            raise M0Error(f"bundle member size/hash drift: {path}")
        total += size
        if total > MAX_PAYLOAD_BYTES:
            raise M0Error("deployment bundle decoded payload exceeds its bounded maximum")
        decoded[path] = content
        normalized.append(raw_member)
    if total != declared_total:
        raise M0Error("deployment bundle total_payload_bytes mismatch")

    paths = set(decoded)
    missing_approvals = set(MUTABLE_APPROVAL_FILES) - paths
    if missing_approvals:
        raise M0Error(f"deployment bundle omitted mutable approvals: {sorted(missing_approvals)}")
    for path in paths:
        parts = PurePosixPath(path).parts
        for count in range(1, len(parts)):
            if PurePosixPath(*parts[:count]).as_posix() in paths:
                raise M0Error(f"bundle file/directory prefix collision: {path}")

    expected_ledger = _ledger_from_members(root_name, normalized)
    if bundle["package_ledger"] != expected_ledger:
        raise M0Error("deployment bundle immutable package ledger mismatch")
    return bundle, decoded


def _canonical_absolute(path: Path, label: str) -> Path:
    value = Path(path)
    if not value.is_absolute() or "\\" in str(value) or ".." in value.parts:
        raise M0Error(f"{label} must be a canonical absolute path")
    if os.path.normpath(str(value)) != str(value):
        raise M0Error(f"{label} must not contain path normalization aliases")
    return value


def _real_directory(path: Path, label: str) -> Path:
    value = _canonical_absolute(path, label)
    try:
        observed = value.lstat()
        resolved = value.resolve(strict=True)
    except OSError as exc:
        raise M0Error(f"cannot resolve {label}: {exc}") from exc
    if stat.S_ISLNK(observed.st_mode) or not stat.S_ISDIR(observed.st_mode) or resolved != value:
        raise M0Error(f"{label} must be an existing real directory without symlink aliases")
    return value


def _path_below_real_root(path: Path, root: Path, label: str) -> Path:
    value = _canonical_absolute(path, label)
    if value == root or not value.is_relative_to(root):
        raise M0Error(f"{label} must be strictly below the caller-pinned allowed root")
    parent = _real_directory(value.parent, f"{label} parent")
    if not parent.is_relative_to(root):
        raise M0Error(f"{label} parent escapes the caller-pinned allowed root")
    return value


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
        raise M0Error(f"cannot open directory for fsync {path}: {exc}") from exc
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise M0Error(f"cannot fsync directory {path}: {exc}") from exc
    finally:
        os.close(descriptor)


def _write_fresh_bytes(path: Path, payload: bytes, *, final_mode: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise M0Error(f"refusing non-fresh output file {path}: {exc}") from exc
    complete = False
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise M0Error(f"short write while creating {path}")
            offset += written
        os.fsync(descriptor)
        os.fchmod(descriptor, final_mode)
        os.fsync(descriptor)
        complete = True
    finally:
        os.close(descriptor)
        if not complete:
            # The incomplete inode remains as fail-closed evidence and prevents reuse.
            pass
    _fsync_directory(path.parent)


def write_bundle(
    source_root: Path,
    output: Path,
    *,
    max_bundle_bytes: int = DEFAULT_MAX_BUNDLE_BYTES,
) -> str:
    source_input = Path(source_root)
    if source_input.is_symlink():
        raise M0Error("application source root symlink is forbidden")
    source = source_input.resolve(strict=True)
    destination = _canonical_absolute(Path(output), "bundle output")
    destination_parent = _real_directory(destination.parent, "bundle output parent")
    if destination.is_relative_to(source):
        raise M0Error("deployment bundle output must be outside its source root")
    payload = build_bundle(source, max_bundle_bytes=max_bundle_bytes)
    _write_fresh_bytes(destination_parent / destination.name, payload, final_mode=0o444)
    return hashlib.sha256(payload).hexdigest()


def _read_stream_bounded(stream: BinaryIO, maximum: int) -> bytes:
    maximum = _bounded_bundle_limit(maximum)
    blocks: list[bytes] = []
    observed = 0
    while True:
        block = stream.read(min(READ_CHUNK_BYTES, maximum + 1 - observed))
        if not isinstance(block, (bytes, bytearray)):
            raise M0Error("receive stream must be binary")
        if not block:
            break
        blocks.append(bytes(block))
        observed += len(block)
        if observed > maximum:
            raise M0Error("received deployment bundle exceeds the byte bound")
    return b"".join(blocks)


def receive_bundle(
    stream: BinaryIO,
    output: Path,
    *,
    max_bundle_bytes: int = DEFAULT_MAX_BUNDLE_BYTES,
) -> str:
    """Receive one bounded canonical bundle from a binary stream into a fresh file."""

    payload = _read_stream_bounded(stream, max_bundle_bytes)
    _validated_bundle(payload, max_bundle_bytes=max_bundle_bytes)
    destination = _canonical_absolute(Path(output), "receive output")
    parent = _real_directory(destination.parent, "receive output parent")
    destination = parent / destination.name
    _write_fresh_bytes(destination, payload, final_mode=0o444)
    return hashlib.sha256(payload).hexdigest()


def _rename_noreplace(source: Path, destination: Path) -> None:
    """Atomically publish ``source`` without replacing any destination inode."""

    library = ctypes.CDLL(None, use_errno=True)
    function = getattr(library, "renameat2", None)
    if function is None:
        raise M0Error("atomic renameat2(RENAME_NOREPLACE) is unavailable")
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    result = function(
        -100,
        os.fsencode(source),
        -100,
        os.fsencode(destination),
        1,
    )
    if result != 0:
        observed_errno = ctypes.get_errno()
        if observed_errno == errno.EEXIST:
            raise M0Error(f"refusing to replace published path: {destination}")
        raise M0Error(
            f"atomic no-replace rename failed {source} -> {destination}: "
            f"{os.strerror(observed_errno)}"
        )


def _required_directories(bundle: Mapping[str, Any]) -> list[str]:
    values = _relative_parent_directories(
        [member["path"] for member in bundle["members"]]
    )
    return sorted(values, key=lambda value: (len(PurePosixPath(value).parts), value))


def _materialize_stage(
    stage: Path,
    bundle: Mapping[str, Any],
    decoded: Mapping[str, bytes],
) -> None:
    try:
        stage.mkdir(mode=0o700)
    except OSError as exc:
        raise M0Error(f"cannot create fresh staged application directory: {exc}") from exc
    for relative in _required_directories(bundle):
        destination = stage.joinpath(*PurePosixPath(relative).parts)
        try:
            destination.mkdir(mode=0o700)
        except OSError as exc:
            raise M0Error(f"cannot create staged directory {relative}: {exc}") from exc
    for member in bundle["members"]:
        relative = member["path"]
        destination = stage.joinpath(*PurePosixPath(relative).parts)
        _write_fresh_bytes(destination, decoded[relative], final_mode=int(member["mode_octal"], 8))
        captured, _ = _read_regular_file(
            destination, maximum=MAX_MEMBER_BYTES, label=f"staged member {relative}"
        )
        if (
            len(captured) != member["size_bytes"]
            or hashlib.sha256(captured).hexdigest() != member["sha256"]
        ):
            raise M0Error(f"staged member size/hash drift: {relative}")

    directories = [stage.joinpath(*PurePosixPath(value).parts) for value in _required_directories(bundle)]
    for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        _fsync_directory(directory)
        os.chmod(directory, SEALED_DIRECTORY_MODE, follow_symlinks=False)
    _fsync_directory(stage)
    os.chmod(stage, SEALED_DIRECTORY_MODE, follow_symlinks=False)
    _fsync_directory(stage)


def _receipt_object(
    *,
    allowed_root: Path,
    target: Path,
    bundle: Mapping[str, Any],
    bundle_sha256: str,
) -> dict[str, Any]:
    return {
        "schema_version": RECEIPT_SCHEMA,
        "installation_status": "COMPLETE",
        "allowed_root": str(allowed_root),
        "target": str(target),
        "bundle_root_name": bundle["root_name"],
        "bundle_sha256": bundle_sha256,
        "package_ledger": bundle["package_ledger"],
        "member_count": bundle["member_count"],
        "total_payload_bytes": bundle["total_payload_bytes"],
        "sealed_file_modes": sorted(SEALED_FILE_MODES),
        "sealed_directory_mode": "0555",
    }


def _validate_receipt(value: Mapping[str, Any]) -> None:
    _exact_keys(
        value,
        {
            "schema_version",
            "installation_status",
            "allowed_root",
            "target",
            "bundle_root_name",
            "bundle_sha256",
            "package_ledger",
            "member_count",
            "total_payload_bytes",
            "sealed_file_modes",
            "sealed_directory_mode",
        },
        "deployment receipt",
    )
    if value["schema_version"] != RECEIPT_SCHEMA or value["installation_status"] != "COMPLETE":
        raise M0Error("deployment receipt status/schema mismatch")
    _validate_root_name(value["bundle_root_name"])
    _sha256(value["bundle_sha256"], "receipt bundle_sha256")
    _uint(value["member_count"], "receipt member_count", maximum=MAX_MEMBERS)
    _uint(
        value["total_payload_bytes"],
        "receipt total_payload_bytes",
        maximum=MAX_PAYLOAD_BYTES,
    )
    if value["sealed_file_modes"] != sorted(SEALED_FILE_MODES):
        raise M0Error("deployment receipt sealed file modes changed")
    if value["sealed_directory_mode"] != "0555":
        raise M0Error("deployment receipt sealed directory mode changed")


def install_bundle(
    bundle_path: Path,
    *,
    allowed_root: Path,
    target: Path,
    receipt: Path,
    max_bundle_bytes: int = DEFAULT_MAX_BUNDLE_BYTES,
) -> dict[str, Any]:
    """Install a validated bundle to one fresh, sealed target below ``allowed_root``."""

    max_bundle_bytes = _bounded_bundle_limit(max_bundle_bytes)
    root = _real_directory(Path(allowed_root), "allowed root")
    destination = _path_below_real_root(Path(target), root, "installation target")
    receipt_path = _path_below_real_root(Path(receipt), root, "installation receipt")
    if receipt_path.is_relative_to(destination):
        raise M0Error("deployment receipt must be outside the installed target")
    if _lexists(destination) or _lexists(receipt_path):
        raise M0Error("installation target and receipt must both be fresh")

    source = _canonical_absolute(Path(bundle_path), "bundle input")
    payload, _ = _read_regular_file(
        source, maximum=max_bundle_bytes, label="deployment bundle input"
    )
    bundle, decoded = _validated_bundle(payload, max_bundle_bytes=max_bundle_bytes)
    bundle_sha256 = hashlib.sha256(payload).hexdigest()
    stage = destination.with_name(
        f".{destination.name}.deploy-{bundle_sha256[:16]}.staged"
    )
    receipt_stage = receipt_path.with_name(
        f".{receipt_path.name}.deploy-{bundle_sha256[:16]}.staged"
    )
    if any(_lexists(path) for path in (stage, receipt_stage)):
        raise M0Error("staged deployment path is not fresh")

    _materialize_stage(stage, bundle, decoded)
    reconstructed = _bundle_object(
        stage, root_name=bundle["root_name"], require_sealed=True
    )
    if reconstructed != bundle:
        raise M0Error("staged application differs from its deployment bundle")

    receipt_value = _receipt_object(
        allowed_root=root,
        target=destination,
        bundle=bundle,
        bundle_sha256=bundle_sha256,
    )
    receipt_bytes = canonical_bytes(receipt_value)
    _write_fresh_bytes(receipt_stage, receipt_bytes, final_mode=0o444)

    _rename_noreplace(stage, destination)
    _fsync_directory(destination.parent)
    _rename_noreplace(receipt_stage, receipt_path)
    _fsync_directory(receipt_path.parent)
    return verify_install(
        allowed_root=root,
        target=destination,
        receipt=receipt_path,
    )


def verify_install(
    *,
    allowed_root: Path,
    target: Path,
    receipt: Path,
) -> dict[str, Any]:
    """Verify exact members, modes, package ledger, and canonical bundle hash."""

    root = _real_directory(Path(allowed_root), "allowed root")
    destination = _path_below_real_root(Path(target), root, "installation target")
    receipt_path = _path_below_real_root(Path(receipt), root, "installation receipt")
    if receipt_path.is_relative_to(destination):
        raise M0Error("deployment receipt must be outside the installed target")
    try:
        target_stat = destination.lstat()
        receipt_stat = receipt_path.lstat()
    except OSError as exc:
        raise M0Error(f"installed target or receipt is missing: {exc}") from exc
    if stat.S_ISLNK(target_stat.st_mode) or not stat.S_ISDIR(target_stat.st_mode):
        raise M0Error("installed target must be a non-symlink directory")
    if stat.S_ISLNK(receipt_stat.st_mode) or not stat.S_ISREG(receipt_stat.st_mode):
        raise M0Error("deployment receipt must be a non-symlink regular file")
    if stat.S_IMODE(receipt_stat.st_mode) != 0o444:
        raise M0Error("deployment receipt mode drift")
    receipt_payload, _ = _read_regular_file(
        receipt_path, maximum=MAX_RECEIPT_BYTES, label="deployment receipt"
    )
    receipt_value = load_json_bytes(receipt_payload, "deployment receipt")
    if canonical_bytes(receipt_value) != receipt_payload:
        raise M0Error("deployment receipt is not canonical JSON")
    _validate_receipt(receipt_value)
    if receipt_value["allowed_root"] != str(root) or receipt_value["target"] != str(destination):
        raise M0Error("deployment receipt path binding mismatch")

    reconstructed = _bundle_object(
        destination,
        root_name=receipt_value["bundle_root_name"],
        require_sealed=True,
    )
    reconstructed_bytes = canonical_bytes(reconstructed)
    if reconstructed["package_ledger"] != receipt_value["package_ledger"]:
        raise M0Error("installed immutable package ledger mismatch")
    if reconstructed["member_count"] != receipt_value["member_count"]:
        raise M0Error("installed member_count mismatch")
    if reconstructed["total_payload_bytes"] != receipt_value["total_payload_bytes"]:
        raise M0Error("installed total_payload_bytes mismatch")
    if hashlib.sha256(reconstructed_bytes).hexdigest() != receipt_value["bundle_sha256"]:
        raise M0Error("installed canonical bundle hash mismatch")
    return dict(receipt_value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    build_parser = commands.add_parser("build")
    build_parser.add_argument("--source-root", type=Path, required=True)
    build_parser.add_argument("--output", type=Path, required=True)
    build_parser.add_argument(
        "--max-bundle-bytes", type=int, default=DEFAULT_MAX_BUNDLE_BYTES
    )

    receive_parser = commands.add_parser("receive")
    receive_parser.add_argument("--output", type=Path, required=True)
    receive_parser.add_argument(
        "--max-bundle-bytes", type=int, default=DEFAULT_MAX_BUNDLE_BYTES
    )

    install_parser = commands.add_parser("install")
    install_parser.add_argument("--bundle", type=Path, required=True)
    install_parser.add_argument("--allowed-root", type=Path, required=True)
    install_parser.add_argument("--target", type=Path, required=True)
    install_parser.add_argument("--receipt", type=Path, required=True)
    install_parser.add_argument(
        "--max-bundle-bytes", type=int, default=DEFAULT_MAX_BUNDLE_BYTES
    )

    verify_parser = commands.add_parser("verify")
    verify_parser.add_argument("--allowed-root", type=Path, required=True)
    verify_parser.add_argument("--target", type=Path, required=True)
    verify_parser.add_argument("--receipt", type=Path, required=True)

    args = parser.parse_args()
    if args.command == "build":
        digest = write_bundle(
            args.source_root,
            args.output,
            max_bundle_bytes=args.max_bundle_bytes,
        )
        print(digest)
    elif args.command == "receive":
        digest = receive_bundle(
            sys.stdin.buffer,
            args.output,
            max_bundle_bytes=args.max_bundle_bytes,
        )
        print(digest)
    elif args.command == "install":
        value = install_bundle(
            args.bundle,
            allowed_root=args.allowed_root,
            target=args.target,
            receipt=args.receipt,
            max_bundle_bytes=args.max_bundle_bytes,
        )
        print(value["bundle_sha256"])
    else:
        value = verify_install(
            allowed_root=args.allowed_root,
            target=args.target,
            receipt=args.receipt,
        )
        print(value["bundle_sha256"])
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except M0Error as exc:
        raise SystemExit(f"HARD-STOP: {exc}") from exc

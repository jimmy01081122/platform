"""Static ELF and live loaded-object closure for the G2.5 application."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import stat
import struct
import sys
from pathlib import Path
from typing import Any, Mapping

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_INVENTORY_PATH = PACKAGE_ROOT / "configs/runtime/g25_local_runtime_v1.json"
SYSTEM_CLOSURE_PATH = PACKAGE_ROOT / "configs/runtime/g25_system_closure_v2.json"


class RuntimeClosureError(RuntimeError):
    pass


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")).hexdigest()


def _sha256_fd(descriptor: int) -> str:
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while True:
        chunk = os.read(descriptor, 8 * 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


def _secure_file_identity(path: Path) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise RuntimeClosureError(f"cannot securely open closure input: {path}") from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise RuntimeClosureError(f"closure input is not a regular file: {path}")
        digest = _sha256_fd(descriptor)
        after = os.fstat(descriptor)
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ctime_ns != after.st_ctime_ns
        ):
            raise RuntimeClosureError(f"closure input changed while hashing: {path}")
        return {
            "path": str(path),
            "bytes": int(before.st_size),
            "sha256": digest,
            "device": int(before.st_dev),
            "inode": int(before.st_ino),
        }
    finally:
        os.close(descriptor)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RuntimeClosureError(f"cannot load runtime closure input: {path}") from error
    if not isinstance(value, dict):
        raise RuntimeClosureError(f"runtime closure input is not an object: {path}")
    return value


def load_system_closure() -> tuple[dict[str, Any], dict[str, Any]]:
    inventory = _read_json(RUNTIME_INVENTORY_PATH)
    closure = _read_json(SYSTEM_CLOSURE_PATH)
    expected_inventory_keys = {
        "schema_version", "system_closure_relative", "system_closure_sha256",
        "runtime_root_relative", "requirements_lock_relative",
        "requirements_lock_sha256", "clean_environment", "python_no_user_site",
        "runtime_root_absolute", "stdlib_root", "driver_runtime_root",
        "isolated_python", "runtime_tree", "stdlib_tree", "driver_runtime_tree",
        "system_files", "interpreter", "tools", "distributions", "import_roots",
    }
    if (
        set(inventory) != expected_inventory_keys
        or inventory.get("schema_version") != "g25-local-runtime-inventory-v2"
        or inventory.get("system_closure_relative")
        != "configs/runtime/g25_system_closure_v2.json"
    ):
        raise RuntimeClosureError("runtime inventory v2 shape differs")
    expected_closure_keys = {
        "schema_version", "platform", "runtime_system_files_sha256",
        "approved_executable_roots", "approved_non_executable_data_roots",
        "static_executables", "dynamic_loader", "forbidden_environment",
        "roles", "allowed_special_mappings",
        "anonymous_executable_mapping_policy", "file_backed_mapping_policy",
        "live_dependency_policy",
    }
    if set(closure) != expected_closure_keys or closure.get(
        "schema_version"
    ) != "g25-system-closure-v2":
        raise RuntimeClosureError("system closure v2 shape differs")
    actual_closure_hash = _secure_file_identity(SYSTEM_CLOSURE_PATH)["sha256"]
    if inventory["system_closure_sha256"] != actual_closure_hash:
        raise RuntimeClosureError("system closure hash differs from runtime inventory")
    if closure["runtime_system_files_sha256"] != _canonical_hash(
        inventory["system_files"]
    ):
        raise RuntimeClosureError("system file set differs from system closure")
    expected_platform = closure["platform"]
    observed_platform = {
        "system": platform.system(),
        "machine": platform.machine(),
        "byteorder": sys.byteorder,
        "libc": platform.libc_ver()[0],
        "libc_version": platform.libc_ver()[1],
        "python_version": platform.python_version(),
    }
    if observed_platform != expected_platform:
        raise RuntimeClosureError(
            f"runtime platform differs: {observed_platform} != {expected_platform}"
        )
    return inventory, closure


def verify_forbidden_environment(closure: Mapping[str, Any]) -> None:
    present = sorted(
        name for name in closure["forbidden_environment"] if name in os.environ
    )
    if present:
        raise RuntimeClosureError(
            f"forbidden dynamic-loader/runtime environment is present: {present}"
        )


def build_attested_python_argv(
    target: str,
    arguments: list[str] | tuple[str, ...] = (),
    *,
    package_root: Path | None = None,
    python_executable: Path | None = None,
) -> list[str]:
    """Construct the sole loader/cache/isolation prefix for G2.5 Python roles."""
    inventory, closure = load_system_closure()
    root = (package_root or PACKAGE_ROOT).resolve(strict=True)
    bootstrap = root / "scripts/g25_isolated_bootstrap.py"
    if not bootstrap.is_file() or bootstrap.is_symlink():
        raise RuntimeClosureError("attested package lacks a regular isolated bootstrap")
    if not target or any(not isinstance(value, str) for value in arguments):
        raise RuntimeClosureError("attested Python target/arguments are malformed")
    python_path = python_executable or Path(inventory["interpreter"]["argv_path"])
    if str(python_path) != inventory["interpreter"]["argv_path"]:
        raise RuntimeClosureError("attested Python executable differs from runtime inventory")
    flags = list(inventory["isolated_python"]["flags"])
    if flags != ["-I", "-S", "-B", "-X", "utf8"]:
        raise RuntimeClosureError("attested isolated-Python flags differ")
    return [
        closure["dynamic_loader"]["path"],
        "--inhibit-cache",
        str(python_path),
        *flags,
        str(bootstrap),
        target,
        *arguments,
    ]


def verify_current_attested_python_argv(
    target: str, arguments: list[str] | tuple[str, ...] = ()
) -> dict[str, Any]:
    """Prove this process entered through the frozen loader before third-party work."""
    try:
        raw = Path("/proc/self/cmdline").read_bytes()
    except OSError as error:
        raise RuntimeClosureError("cannot read current process argv") from error
    if not raw.endswith(b"\0"):
        raise RuntimeClosureError("current process argv is not NUL terminated")
    try:
        observed = [part.decode("utf-8") for part in raw[:-1].split(b"\0")]
    except UnicodeDecodeError as error:
        raise RuntimeClosureError("current process argv is not UTF-8") from error
    expected_argv = build_attested_python_argv(target, arguments)
    if observed != expected_argv:
        raise RuntimeClosureError(
            "current process bypassed the attested loader/isolation argv"
        )
    inventory, closure = load_system_closure()
    loader = Path(closure["dynamic_loader"]["path"])
    try:
        observed_executable = Path("/proc/self/exe").resolve(strict=True)
    except OSError as error:
        raise RuntimeClosureError("cannot resolve current process executable") from error
    if observed_executable != loader.resolve(strict=True):
        raise RuntimeClosureError("current process executable is not the frozen loader")
    loader_identity = _secure_file_identity(loader)
    expected_loader_hashes = {
        row["path"]: row["sha256"] for row in inventory["system_files"]
    }
    if loader_identity["sha256"] != expected_loader_hashes.get(str(loader)):
        raise RuntimeClosureError("current process loader identity differs")
    return {
        "schema_version": "g25-attested-python-argv-v1",
        "target": target,
        "argv": expected_argv,
        "argv_sha256": _canonical_hash(expected_argv),
        "observed_argv_sha256": _canonical_hash(observed),
        "loader": loader_identity,
        "inhibit_cache": True,
        "isolated_python": True,
    }


def parse_elf_identity(path: Path) -> dict[str, Any] | None:
    """Parse ELF64 little-endian PT_INTERP and DT_NEEDED without executing it."""
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        header = os.pread(descriptor, 64, 0)
        if len(header) < 16 or header[:4] != b"\x7fELF":
            return None
        if header[4] != 2 or header[5] != 1:
            raise RuntimeClosureError(f"unsupported ELF class/endianness: {path}")
        values = struct.unpack("<16sHHIQQQIHHHHHH", header)
        machine = values[2]
        phoff = values[5]
        phentsize = values[9]
        phnum = values[10]
        if machine != 62 or phentsize < 56 or phnum > 4096:
            raise RuntimeClosureError(f"unsupported or malformed x86-64 ELF: {path}")
        segments = []
        interpreter: str | None = None
        dynamic: tuple[int, int] | None = None
        for index in range(phnum):
            raw = os.pread(descriptor, phentsize, phoff + index * phentsize)
            if len(raw) < 56:
                raise RuntimeClosureError(f"truncated ELF program header: {path}")
            p_type, _flags, p_offset, p_vaddr, _p_paddr, p_filesz, p_memsz, _align = (
                struct.unpack("<IIQQQQQQ", raw[:56])
            )
            if p_type == 1:
                segments.append((p_vaddr, p_filesz, p_offset))
            elif p_type == 2:
                dynamic = (p_offset, p_filesz)
            elif p_type == 3:
                payload = os.pread(descriptor, p_filesz, p_offset)
                interpreter = payload.split(b"\0", 1)[0].decode("utf-8")
        needed_offsets: list[int] = []
        soname_offset: int | None = None
        strtab_vaddr: int | None = None
        strtab_size: int | None = None
        if dynamic is not None:
            offset, size = dynamic
            if size > 16 * 1024 * 1024:
                raise RuntimeClosureError(f"oversized ELF dynamic table: {path}")
            raw = os.pread(descriptor, size, offset)
            for cursor in range(0, len(raw) - 15, 16):
                tag, value = struct.unpack("<QQ", raw[cursor : cursor + 16])
                if tag == 0:
                    break
                if tag == 1:
                    needed_offsets.append(value)
                elif tag == 5:
                    strtab_vaddr = value
                elif tag == 10:
                    strtab_size = value
                elif tag == 14:
                    soname_offset = value
        needed: list[str] = []
        soname: str | None = None
        if needed_offsets or soname_offset is not None:
            if strtab_vaddr is None or strtab_size is None or strtab_size > 64 * 1024 * 1024:
                raise RuntimeClosureError(f"ELF string table is incomplete: {path}")
            strtab_offset = None
            for vaddr, filesz, file_offset in segments:
                if vaddr <= strtab_vaddr < vaddr + filesz:
                    strtab_offset = file_offset + (strtab_vaddr - vaddr)
                    break
            if strtab_offset is None:
                raise RuntimeClosureError(f"ELF string table is outside PT_LOAD: {path}")
            strings = os.pread(descriptor, strtab_size, strtab_offset)

            def string_at(offset: int) -> str:
                if offset >= len(strings):
                    raise RuntimeClosureError(f"ELF string offset is invalid: {path}")
                return strings[offset :].split(b"\0", 1)[0].decode("utf-8")

            needed = [string_at(offset) for offset in needed_offsets]
            soname = string_at(soname_offset) if soname_offset is not None else None
        return {
            "interpreter": interpreter,
            "needed": needed,
            "soname": soname,
        }
    finally:
        os.close(descriptor)


def _system_files(inventory: Mapping[str, Any]) -> dict[Path, str]:
    result: dict[Path, str] = {}
    for row in inventory["system_files"]:
        if not isinstance(row, dict) or set(row) != {"path", "sha256"}:
            raise RuntimeClosureError("system dependency descriptor differs")
        path = Path(row["path"])
        if path in result:
            raise RuntimeClosureError("system dependency inventory contains duplicates")
        result[path] = row["sha256"]
    return result


def verify_static_system_closure(
    *, enforce_environment: bool = True,
) -> dict[str, Any]:
    inventory, closure = load_system_closure()
    if enforce_environment:
        verify_forbidden_environment(closure)
    if Path(closure["dynamic_loader"]["preload_path"]).exists():
        raise RuntimeClosureError("/etc/ld.so.preload must remain absent")
    system_files = _system_files(inventory)
    rows = []
    for path, expected_hash in system_files.items():
        identity = _secure_file_identity(path)
        if identity["sha256"] != expected_hash:
            raise RuntimeClosureError(f"system closure input differs: {path}")
        rows.append(identity)
    executables = [Path(value) for value in closure["static_executables"]]
    expected_executables = {
        Path(inventory["interpreter"]["realpath"]),
        *(Path(row["path"]) for row in inventory["tools"].values()),
    }
    if set(executables) != expected_executables:
        raise RuntimeClosureError("static executable set differs from runtime tools")
    identities = {path: parse_elf_identity(path) for path in [*executables, *system_files]}
    aliases: dict[str, list[Path]] = {}
    for path, elf in identities.items():
        if elf is None:
            continue
        aliases.setdefault(path.name, []).append(path)
        if elf["soname"]:
            aliases.setdefault(elf["soname"], []).append(path)
    edges = []
    pending = list(executables)
    visited: set[Path] = set()
    loader = Path(closure["dynamic_loader"]["path"])
    while pending:
        requester = pending.pop()
        if requester in visited:
            continue
        visited.add(requester)
        elf = identities.get(requester)
        if elf is None:
            raise RuntimeClosureError(f"static executable closure contains non-ELF: {requester}")
        if elf["interpreter"] is not None and Path(elf["interpreter"]).resolve(
            strict=True
        ) != loader:
            raise RuntimeClosureError(f"ELF interpreter differs: {requester}")
        for needed in elf["needed"]:
            candidates = sorted(set(aliases.get(needed, [])))
            if len(candidates) != 1:
                raise RuntimeClosureError(
                    f"DT_NEEDED does not resolve uniquely: {requester}: {needed}: {candidates}"
                )
            resolved = candidates[0]
            edges.append({
                "requester": str(requester), "needed": needed,
                "resolved": str(resolved),
            })
            pending.append(resolved)
    edges.sort(key=lambda row: (row["requester"], row["needed"], row["resolved"]))
    return {
        "schema_version": "g25-static-system-closure-attestation-v1",
        "system_closure_sha256": _secure_file_identity(SYSTEM_CLOSURE_PATH)["sha256"],
        "system_file_count": len(rows),
        "system_files_sha256": _canonical_hash(rows),
        "static_executables": [str(path) for path in executables],
        "dependency_edges": edges,
        "dependency_edges_sha256": _canonical_hash(edges),
    }


def verify_live_loaded_closure(role: str) -> dict[str, Any]:
    inventory, closure = load_system_closure()
    verify_forbidden_environment(closure)
    if role not in closure["roles"]:
        raise RuntimeClosureError(f"unknown live closure role: {role}")
    system_files = _system_files(inventory)
    executable_roots = [Path(value) for value in closure["approved_executable_roots"]]
    data_roots = [Path(value) for value in closure["approved_non_executable_data_roots"]]
    interpreter = Path(inventory["interpreter"]["realpath"])
    allowed_special = set(closure["allowed_special_mappings"])
    observed: dict[tuple[int, int], dict[str, Any]] = {}
    for line in Path("/proc/self/maps").read_text(encoding="utf-8").splitlines():
        fields = line.split(maxsplit=5)
        if len(fields) < 5:
            raise RuntimeClosureError("malformed /proc/self/maps row")
        permissions = fields[1]
        raw_path = fields[5] if len(fields) == 6 else ""
        if raw_path.endswith(" (deleted)"):
            raise RuntimeClosureError(f"deleted file remains mapped: {raw_path}")
        if not raw_path:
            if "x" in permissions:
                raise RuntimeClosureError("anonymous executable mapping is forbidden")
            continue
        if raw_path.startswith("["):
            if raw_path not in allowed_special:
                raise RuntimeClosureError(f"unapproved special mapping: {raw_path}")
            if "x" in permissions and raw_path not in {"[vdso]", "[vsyscall]"}:
                raise RuntimeClosureError(f"unapproved executable special mapping: {raw_path}")
            continue
        if raw_path.startswith("/memfd:"):
            raise RuntimeClosureError(f"memfd mapping is forbidden: {raw_path}")
        path = Path(raw_path)
        if not path.is_absolute():
            raise RuntimeClosureError(f"non-absolute file-backed mapping: {raw_path}")
        resolved = path.resolve(strict=True)
        in_exec_root = any(resolved == root or root in resolved.parents for root in executable_roots)
        in_data_root = any(resolved == root or root in resolved.parents for root in data_roots)
        if in_data_root and "x" in permissions:
            raise RuntimeClosureError(f"data root has executable mapping: {resolved}")
        if not (
            resolved == interpreter
            or resolved in system_files
            or in_exec_root
            or in_data_root
        ):
            raise RuntimeClosureError(f"unbound file-backed mapping: {resolved}")
        identity = _secure_file_identity(resolved)
        device_text, inode_text = fields[3], fields[4]
        major_text, minor_text = device_text.split(":", 1)
        if (
            os.major(identity["device"]) != int(major_text, 16)
            or os.minor(identity["device"]) != int(minor_text, 16)
            or identity["inode"] != int(inode_text)
        ):
            raise RuntimeClosureError(f"mapped device/inode differs from path: {resolved}")
        if resolved in system_files and identity["sha256"] != system_files[resolved]:
            raise RuntimeClosureError(f"mapped system object hash differs: {resolved}")
        key = (identity["device"], identity["inode"])
        prior = observed.get(key)
        row = {
            **identity,
            "executable_mapping": "x" in permissions,
            "data_root": in_data_root,
        }
        if prior is not None:
            prior["executable_mapping"] = prior["executable_mapping"] or row[
                "executable_mapping"
            ]
        else:
            observed[key] = row
    rows = sorted(observed.values(), key=lambda row: row["path"])
    elf_by_path = {
        Path(row["path"]): parse_elf_identity(Path(row["path"])) for row in rows
        if not row["data_root"]
    }
    aliases: dict[str, list[Path]] = {}
    for path, elf in elf_by_path.items():
        if elf is None:
            continue
        aliases.setdefault(path.name, []).append(path)
        if elf["soname"]:
            aliases.setdefault(elf["soname"], []).append(path)
    edges = []
    for requester, elf in elf_by_path.items():
        if elf is None:
            continue
        for needed in elf["needed"]:
            candidates = sorted(set(aliases.get(needed, [])))
            if len(candidates) != 1:
                raise RuntimeClosureError(
                    f"live DT_NEEDED does not resolve uniquely: {requester}: {needed}: {candidates}"
                )
            edges.append({
                "requester": str(requester), "needed": needed,
                "resolved": str(candidates[0]),
            })
    edges.sort(key=lambda row: (row["requester"], row["needed"], row["resolved"]))
    return {
        "schema_version": "g25-live-loaded-closure-v1",
        "role": role,
        "system_closure_sha256": _secure_file_identity(SYSTEM_CLOSURE_PATH)["sha256"],
        "loaded_object_count": len(rows),
        "loaded_objects": rows,
        "loaded_set_sha256": _canonical_hash(rows),
        "dependency_edges": edges,
        "dependency_edges_sha256": _canonical_hash(edges),
    }

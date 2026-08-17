#!/usr/bin/env python3
"""Dependency-free, read-only D0 environment disclosure probe."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def read_bytes(path: str) -> bytes:
    return Path(path).read_bytes()


def command(argv: list[str]) -> dict[str, Any]:
    completed = subprocess.run(
        argv,
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return {
        "argv": argv,
        "returncode": completed.returncode,
        "stdout_utf8": completed.stdout.decode("utf-8", errors="strict"),
        "stdout_sha256": sha256_bytes(completed.stdout),
        "stderr_sha256": sha256_bytes(completed.stderr),
    }


def meminfo() -> dict[str, int]:
    values: dict[str, int] = {}
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] in {"MemTotal:", "MemAvailable:"}:
            values[fields[0][:-1]] = int(fields[1]) * 1024
    return values


def cpu_model() -> str:
    for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
        if line.lower().startswith("model name"):
            return line.split(":", 1)[1].strip()
    return "UNAVAILABLE"


def mount_record(path_text: str) -> dict[str, Any]:
    path = Path(path_text)
    exists = path.exists()
    record: dict[str, Any] = {
        "path": path_text,
        "exists": exists,
        "realpath": os.path.realpath(path_text) if exists else None,
        "is_mount": os.path.ismount(path_text) if exists else False,
        "is_symlink": path.is_symlink(),
        "device_id": None,
        "mode_octal": None,
        "owner_uid": None,
        "owner_gid": None,
        "total_bytes": None,
        "free_bytes": None,
        "mount_identity": None,
    }
    if exists:
        stat = path.stat()
        usage = shutil.disk_usage(path)
        record.update(
            {
                "device_id": stat.st_dev,
                "mode_octal": format(stat.st_mode & 0o7777, "04o"),
                "owner_uid": stat.st_uid,
                "owner_gid": stat.st_gid,
                "total_bytes": usage.total,
                "free_bytes": usage.free,
            }
        )
        if record["realpath"] == path_text and not record["is_symlink"]:
            try:
                raw = Path("/proc/self/mountinfo").read_bytes()
                for line in raw.decode("utf-8", errors="strict").splitlines():
                    left, separator, right = line.partition(" - ")
                    if not separator:
                        continue
                    fields = left.split()
                    tail = right.split()
                    if len(fields) < 6 or len(tail) < 3:
                        continue
                    mount_point = fields[4].replace("\\040", " ")
                    if mount_point != path_text:
                        continue
                    identity = {
                        "mount_id": int(fields[0]),
                        "parent_id": int(fields[1]),
                        "major_minor": fields[2],
                        "root": fields[3],
                        "mount_point": mount_point,
                        "mount_options": sorted(fields[5].split(",")),
                        "filesystem_type": tail[0],
                        "mount_source": tail[1],
                        "super_options": sorted(tail[2].split(",")),
                        "device_id": stat.st_dev,
                        "boot_id": Path("/proc/sys/kernel/random/boot_id")
                        .read_text(encoding="utf-8")
                        .strip(),
                        "mountinfo_sha256": sha256_bytes(raw),
                    }
                    identity_fields = {
                        key: value
                        for key, value in identity.items()
                        if key != "mountinfo_sha256"
                    }
                    canonical = json.dumps(
                        identity_fields,
                        ensure_ascii=False,
                        allow_nan=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                    identity["mount_identity_sha256"] = sha256_bytes(canonical)
                    record["mount_identity"] = identity
                    break
            except (OSError, UnicodeError, ValueError):
                record["mount_identity"] = None
    return record


def distribution_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def gpu_record(raw: dict[str, Any]) -> dict[str, Any]:
    rows = [
        row
        for row in raw["stdout_utf8"].splitlines()
        if row.strip()
    ] if raw["returncode"] == 0 else []
    devices = []
    for row in rows:
        fields = [field.strip() for field in row.split(",")]
        if len(fields) != 7:
            continue
        try:
            memory = int(fields[1]) * 1024 * 1024
        except ValueError:
            continue
        devices.append(
            {
                "name": fields[0],
                "total_memory_bytes": memory,
                "uuid": fields[2],
                "pci_bus_id": fields[3],
                "driver_version": fields[4],
                "compute_mode": fields[5],
                "mig_mode": fields[6],
            }
        )
    return {
        "query_status": "COMPLETE" if raw["returncode"] == 0 else "UNAVAILABLE",
        "device_count": len(devices),
        "devices": devices,
        "command": raw,
    }


def main() -> int:
    gpu_command = command(
        [
            "nvidia-smi",
            "--query-gpu=name,memory.total,uuid,pci.bus_id,driver_version,compute_mode,mig.mode.current",
            "--format=csv,noheader,nounits",
        ]
    ) if shutil.which("nvidia-smi") else {
        "argv": ["nvidia-smi"],
        "returncode": 127,
        "stdout_utf8": "",
        "stdout_sha256": sha256_bytes(b""),
        "stderr_sha256": sha256_bytes(b"nvidia-smi unavailable"),
    }
    os_release = read_bytes("/etc/os-release")
    boot_id = read_bytes("/proc/sys/kernel/random/boot_id").decode(
        "utf-8", errors="strict"
    ).strip()
    packages = {
        name: distribution_version(name)
        for name in (
            "vllm",
            "torch",
            "transformers",
            "huggingface_hub",
            "tokenizers",
        )
    }
    result = {
        "schema_version": "moe-simulator-phase7-d0-probe-result-v1",
        "capture_status": "COMPLETE",
        "captured_at_utc": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "host": {
            "hostname": platform.node(),
            "effective_uid": os.geteuid(),
            "effective_gid": os.getegid(),
            "kernel_release": platform.release(),
            "architecture": platform.machine(),
            "python_version": platform.python_version(),
            "cpu_model": cpu_model(),
            "logical_cpu_count": os.cpu_count(),
            "memory": meminfo(),
            "os_release_utf8": os_release.decode("utf-8", errors="strict"),
            "os_release_sha256": sha256_bytes(os_release),
            "boot_id": boot_id,
        },
        "gpu": gpu_record(gpu_command),
        "mounts": {
            "persistent": mount_record("/vault"),
            "ephemeral": mount_record("/workspace"),
        },
        "software": {
            "packages": packages,
            "commands": {
                name: shutil.which(name)
                for name in (
                    "python3",
                    "git",
                    "nvcc",
                    "docker",
                    "podman",
                    "cmake",
                    "ninja",
                )
            },
            "container_digest_attestation": os.environ.get(
                "MOE_PHASE7_CONTAINER_DIGEST"
            ),
        },
        "environment_presence": {
            name: name in os.environ
            for name in (
                "CUDA_VISIBLE_DEVICES",
                "HF_HOME",
                "HF_HUB_OFFLINE",
                "TRANSFORMERS_OFFLINE",
                "LD_LIBRARY_PATH",
            )
        },
        "prohibitions": {
            "remote_file_write_performed": False,
            "download_performed": False,
            "install_performed": False,
            "model_access_performed": False,
            "gpu_workload_performed": False,
            "secret_values_recorded": False,
        },
    }
    sys.stdout.write(
        json.dumps(
            result,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

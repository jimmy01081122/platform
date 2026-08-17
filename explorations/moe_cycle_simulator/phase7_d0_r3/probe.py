#!/usr/bin/env python3
"""Read-only D0-R3 environment probe transported over SSH stdin.

The probe emits every field declared by ``schemas/probe.schema.json``. Missing
live facts are represented as null and are classified by the controller; a
missing field is never silently omitted.
"""

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


GPU_QUERY = "nvidia-smi --query-gpu=name,memory.total,uuid,pci.bus_id,driver_version,compute_mode,mig.mode.current --format=csv,noheader,nounits"


def digest_file(path: str | None) -> str | None:
    if not path:
        return None
    candidate = Path(path)
    try:
        if not candidate.is_file() or candidate.is_symlink():
            return None
        digest = hashlib.sha256()
        with candidate.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()
    except OSError:
        return None


def read_text(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def storage(path: str) -> dict[str, Any]:
    candidate = Path(path)
    mounted = candidate.exists()
    mount_identity = None
    total = free = None
    mountinfo = read_text("/proc/self/mountinfo")
    if mountinfo is not None:
        rows = [row for row in mountinfo.splitlines() if f" - " in row and f" {path} " in row]
        if rows:
            mount_identity = hashlib.sha256((path + "\n" + "\n".join(rows)).encode()).hexdigest()
    if mounted:
        try:
            usage = shutil.disk_usage(candidate)
            total, free = usage.total, usage.free
        except OSError:
            pass
    return {
        "path": path,
        "mounted": mounted,
        "mount_identity_sha256": mount_identity,
        "total_bytes": total,
        "free_bytes": free,
    }


def executable(path: str | None) -> dict[str, Any]:
    return {
        "path": path,
        "version": platform.python_version() if path == sys.executable else None,
        "sha256": digest_file(path),
    }


def gpu_info() -> tuple[str, int | None, list[dict[str, Any]]]:
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,uuid,pci.bus_id,driver_version,compute_mode,mig.mode.current", "--format=csv,noheader,nounits"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return "UNAVAILABLE", None, []
    if result.returncode != 0:
        return "FAILED", None, []
    devices: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        fields = [field.strip() for field in line.split(",")]
        if len(fields) != 7:
            continue
        try:
            memory = int(fields[1]) * 1024 * 1024
        except ValueError:
            memory = None
        devices.append({
            "name": fields[0] or None,
            "memory_total_bytes": memory,
            "uuid": fields[2] or None,
            "pci_bus_id": fields[3] or None,
            "driver_version": fields[4] or None,
            "compute_mode": fields[5] or None,
            "mig_mode": fields[6] or None,
        })
    return "COMPLETE", len(devices), devices


def vllm_identity() -> dict[str, Any]:
    try:
        distribution = importlib.metadata.distribution("vllm")
        version = distribution.version
        path = str(distribution.locate_file(""))
        return {"present": True, "version": version, "path": path, "distribution_sha256": digest_file(path)}
    except importlib.metadata.PackageNotFoundError:
        return {"present": False, "version": None, "path": None, "distribution_sha256": None}
    except OSError:
        return {"present": True, "version": None, "path": None, "distribution_sha256": None}


def main() -> int:
    status, count, devices = gpu_info()
    probe = {
        "schema_version": "moe-simulator-phase7-gputw-d0-r3-probe-v1",
        "capture_status": "COMPLETE",
        "captured_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "provider": {
            "name": os.environ.get("MOE_PHASE7_PROVIDER", "GPUtw.ai"),
            "instance_id": os.environ.get("MOE_PHASE7_INSTANCE_ID"),
            "instance_state": os.environ.get("MOE_PHASE7_INSTANCE_STATE"),
        },
        "instance": {
            "principal": os.environ.get("MOE_PHASE7_INSTANCE_PRINCIPAL"),
            "catalog_gpu_id": os.environ.get("MOE_PHASE7_CATALOG_GPU_ID"),
            "node_id": os.environ.get("MOE_PHASE7_NODE_ID"),
            "environment_label": os.environ.get("MOE_PHASE7_ENVIRONMENT_LABEL"),
        },
        "host": {
            "hostname": platform.node() or None,
            "os_release": read_text("/etc/os-release"),
            "kernel_release": platform.release() or None,
            "boot_id": read_text("/proc/sys/kernel/random/boot_id"),
            "python": executable(sys.executable),
            "timeout": executable(shutil.which("timeout")),
        },
        "gpu": {"query_status": status, "count": count, "devices": devices},
        "runtime": {
            "container_image": os.environ.get("MOE_PHASE7_CONTAINER_IMAGE"),
            "container_digest": os.environ.get("MOE_PHASE7_CONTAINER_DIGEST"),
            "cuda": os.environ.get("CUDA_VERSION"),
            "driver": devices[0]["driver_version"] if devices else None,
            "vllm": vllm_identity(),
            "backends": [],
        },
        "storage": {"vault": storage("/vault"), "workspace": storage("/workspace")},
        "observed_commands": [GPU_QUERY],
        "prohibitions": {
            "remote_writes": False,
            "package_install": False,
            "model_access": False,
            "inference": False,
            "cuda_benchmark": False,
            "gpu_workload": False,
        },
    }
    json.dump(probe, sys.stdout, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

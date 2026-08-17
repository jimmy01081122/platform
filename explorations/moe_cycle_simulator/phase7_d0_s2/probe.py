#!/usr/bin/env python3
"""Read-only D0-S2 runtime and platform identity probe.

The probe is intentionally dependency-free apart from optional imports used to
observe an installed PyTorch build.  It performs no install, download, model
load, inference, benchmark, or remote write.  Missing provider facts are
represented explicitly as ``UNAVAILABLE`` or ``null``; they are never guessed
from an image label.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "moe-simulator-phase7-gputw-d0-s2-probe-v1"
GPU_QUERY = [
    "nvidia-smi",
    "--query-gpu=name,memory.total,uuid,pci.bus_id,driver_version,compute_mode,mig.mode.current",
    "--format=csv,noheader,nounits",
]
CUDA_VERSION_RE = re.compile(r"(?:release|CUDA Version|version)[^0-9]*([0-9]+)\.([0-9]+)", re.I)
PACKAGE_NAMES = ("vllm", "torch", "transformers", "tokenizers", "huggingface_hub")


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def read_bytes(path: str) -> bytes | None:
    try:
        candidate = Path(path)
        if candidate.is_symlink() or not candidate.is_file():
            return None
        return candidate.read_bytes()
    except (OSError, ValueError):
        return None


def digest_file(path: str | None) -> str | None:
    if not path:
        return None
    payload = read_bytes(path)
    return sha256_bytes(payload) if payload is not None else None


def executable_identity(path: str | None, *, version: str | None = None) -> dict[str, Any]:
    resolved = os.path.realpath(path) if path else None
    digest = digest_file(resolved)
    return {
        "path": path,
        "resolved_path": resolved,
        "version": version,
        "sha256": digest,
        "hash_status": "HASHED" if digest else "UNAVAILABLE",
    }


def command_record(argv: list[str], timeout_seconds: int = 15) -> dict[str, Any]:
    """Run one allowlisted, read-only identity command and retain no raw secrets."""

    if not argv or argv[0] not in {"nvidia-smi", "nvcc"}:
        raise ValueError(f"command is outside the D0-S2 allowlist: {argv!r}")
    executable = shutil.which(argv[0])
    if executable is None:
        return {
            "argv": argv,
            "path": None,
            "returncode": 127,
            "stdout_sha256": sha256_bytes(b""),
            "stderr_sha256": sha256_bytes(f"{argv[0]} unavailable".encode()),
            "stdout_bytes": 0,
            "stderr_bytes": len(f"{argv[0]} unavailable".encode()),
            "status": "UNAVAILABLE",
        }
    try:
        completed = subprocess.run(
            [executable, *argv[1:]],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            text=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        message = str(exc).encode("utf-8", errors="replace")
        return {
            "argv": argv,
            "path": executable,
            "returncode": 125,
            "stdout_sha256": sha256_bytes(b""),
            "stderr_sha256": sha256_bytes(message),
            "stdout_bytes": 0,
            "stderr_bytes": len(message),
            "status": "FAILED",
        }
    return {
        "argv": argv,
        "path": executable,
        "returncode": completed.returncode,
        "stdout_sha256": sha256_bytes(completed.stdout),
        "stderr_sha256": sha256_bytes(completed.stderr),
        "stdout_bytes": len(completed.stdout),
        "stderr_bytes": len(completed.stderr),
        "status": "COMPLETE" if completed.returncode == 0 else "FAILED",
        "stdout_utf8": completed.stdout.decode("utf-8", errors="replace"),
    }


def parse_gpu(command: dict[str, Any]) -> dict[str, Any]:
    devices: list[dict[str, Any]] = []
    raw = command.get("stdout_utf8", "")
    if command.get("returncode") == 0:
        for line in raw.splitlines():
            fields = [field.strip() for field in line.split(",")]
            if len(fields) != 7:
                continue
            try:
                memory_bytes = int(fields[1]) * 1024 * 1024
            except ValueError:
                memory_bytes = None
            devices.append(
                {
                    "name": fields[0] or None,
                    "memory_total_bytes": memory_bytes,
                    "uuid": fields[2] or None,
                    "pci_bus_id": fields[3] or None,
                    "driver_version": fields[4] or None,
                    "compute_mode": fields[5] or None,
                    "mig_mode": fields[6] or None,
                }
            )
    return {
        "query_status": "COMPLETE" if command.get("returncode") == 0 else command.get("status", "UNAVAILABLE"),
        "count": len(devices) if command.get("returncode") == 0 else None,
        "devices": devices,
        "command": {key: value for key, value in command.items() if key != "stdout_utf8"},
    }


def distribution_identity(name: str) -> dict[str, Any]:
    """Return an explicit installed-distribution inventory identity.

    The digest is deliberately defined as a hash of the installed ``RECORD``
    bytes plus the sorted file-name inventory and sizes.  This is an identity
    inventory, not a claim that every binary was loaded or functionally tested;
    M0 later performs its stronger runtime attestation.
    """

    base: dict[str, Any] = {
        "name": name,
        "present": False,
        "status": "NOT_INSTALLED",
        "version": None,
        "metadata_path": None,
        "record_sha256": None,
        "distribution_sha256": None,
        "hash_method": "RECORD_BYTES_PLUS_PATH_SIZE_INVENTORY",
        "file_count": 0,
        "regular_file_count": 0,
        "missing_file_count": 0,
        "symlink_file_count": 0,
    }
    try:
        distribution = importlib.metadata.distribution(name)
    except importlib.metadata.PackageNotFoundError:
        return base
    except (OSError, ValueError) as exc:
        base.update({"status": "UNAVAILABLE", "error_type": type(exc).__name__})
        return base

    files = list(distribution.files or [])
    inventory: list[dict[str, Any]] = []
    missing = symlinks = regular = 0
    for item in sorted((str(value) for value in files), key=lambda value: value.encode()):
        if item.startswith("/") or ".." in Path(item).parts:
            continue
        location = Path(distribution.locate_file(item))
        try:
            metadata = location.lstat()
        except OSError:
            missing += 1
            inventory.append({"path": item, "status": "MISSING", "size_bytes": None})
            continue
        if location.is_symlink():
            symlinks += 1
            inventory.append({"path": item, "status": "SYMLINK", "size_bytes": metadata.st_size})
            continue
        if not location.is_file():
            missing += 1
            inventory.append({"path": item, "status": "NON_REGULAR", "size_bytes": metadata.st_size})
            continue
        regular += 1
        inventory.append({"path": item, "status": "REGULAR", "size_bytes": metadata.st_size})

    metadata_path = getattr(distribution, "_path", None)
    metadata_text = str(metadata_path) if metadata_path is not None else None
    record_path = Path(metadata_text) / "RECORD" if metadata_text else None
    record = read_bytes(str(record_path)) if record_path else None
    record_digest = sha256_bytes(record) if record is not None else None
    digest_payload = {
        "name": name,
        "version": distribution.version,
        "record_sha256": record_digest,
        "inventory": inventory,
    }
    base.update(
        {
            "present": True,
            "status": "COMPLETE" if record is not None and missing == 0 and symlinks == 0 else "PARTIAL",
            "version": distribution.version,
            "metadata_path": metadata_text,
            "record_sha256": record_digest,
            "distribution_sha256": sha256_bytes(canonical_bytes(digest_payload)),
            "file_count": len(files),
            "regular_file_count": regular,
            "missing_file_count": missing,
            "symlink_file_count": symlinks,
        }
    )
    return base


def torch_identity() -> dict[str, Any]:
    result: dict[str, Any] = {
        "import_status": "NOT_ATTEMPTED",
        "version": None,
        "cuda_build": None,
        "cuda_available": None,
        "device_count": None,
        "query_only": True,
    }
    try:
        import torch  # type: ignore
    except Exception as exc:  # import can fail for an ABI mismatch
        result.update({"import_status": "FAILED", "error_type": type(exc).__name__})
        return result
    result.update(
        {
            "import_status": "COMPLETE",
            "version": str(getattr(torch, "__version__", "UNAVAILABLE")),
            "cuda_build": getattr(getattr(torch, "version", None), "cuda", None),
        }
    )
    try:
        # Availability/device_count are driver identity queries; no tensor,
        # kernel, benchmark, allocation, or model operation is performed.
        result["cuda_available"] = bool(torch.cuda.is_available())
        result["device_count"] = int(torch.cuda.device_count())
    except Exception as exc:
        result.update({"cuda_available": None, "device_count": None, "query_error_type": type(exc).__name__})
    return result


def cuda_identity(torch_record: dict[str, Any], nvcc_record: dict[str, Any]) -> dict[str, Any]:
    candidates: list[dict[str, str]] = []
    env_value = os.environ.get("CUDA_VERSION")
    if env_value:
        candidates.append({"source": "CUDA_VERSION", "value": env_value})
    torch_value = torch_record.get("cuda_build")
    if isinstance(torch_value, str) and torch_value:
        candidates.append({"source": "torch.version.cuda", "value": torch_value})
    for path in ("/usr/local/cuda/version.json", "/usr/local/cuda/version.txt"):
        payload = read_bytes(path)
        if payload:
            text = payload.decode("utf-8", errors="replace")
            match = CUDA_VERSION_RE.search(text)
            if match:
                candidates.append({"source": path, "value": f"{match.group(1)}.{match.group(2)}"})
    nvcc_text = nvcc_record.get("stdout_utf8", "")
    match = CUDA_VERSION_RE.search(nvcc_text)
    if match:
        candidates.append({"source": "nvcc --version", "value": f"{match.group(1)}.{match.group(2)}"})
    selected = candidates[0]["value"] if candidates else None
    return {
        "runtime_version": selected,
        "runtime_status": "OBSERVED" if selected else "UNAVAILABLE",
        "sources": candidates,
        "torch_build": torch_value,
        "nvcc": {key: value for key, value in nvcc_record.items() if key != "stdout_utf8"},
    }


def storage_identity(path_text: str) -> dict[str, Any]:
    path = Path(path_text)
    exists = path.exists()
    record: dict[str, Any] = {
        "path": path_text,
        "exists": exists,
        "mounted": os.path.ismount(path_text) if exists else False,
        "is_symlink": path.is_symlink(),
        "realpath": os.path.realpath(path_text) if exists else None,
        "mount_identity_sha256": None,
        "total_bytes": None,
        "free_bytes": None,
    }
    if not exists or path.is_symlink():
        return record
    try:
        usage = shutil.disk_usage(path)
        record.update({"total_bytes": usage.total, "free_bytes": usage.free})
    except OSError:
        pass
    mountinfo = read_bytes("/proc/self/mountinfo")
    if mountinfo:
        lines = [line for line in mountinfo.decode("utf-8", errors="replace").splitlines() if f" {path_text} " in line]
        if lines:
            record["mount_identity_sha256"] = sha256_bytes((path_text + "\n" + "\n".join(lines)).encode())
    return record


def environment_probe() -> dict[str, Any]:
    gpu_command = command_record(GPU_QUERY)
    nvcc_command = command_record(["nvcc", "--version"])
    torch_record = torch_identity()
    packages = {name: distribution_identity(name) for name in PACKAGE_NAMES}
    python_path = sys.executable or shutil.which("python3")
    timeout_path = shutil.which("timeout")
    os_release = read_bytes("/etc/os-release")
    container_image = os.environ.get("MOE_PHASE7_CONTAINER_IMAGE") or "UNAVAILABLE"
    container_digest = os.environ.get("MOE_PHASE7_CONTAINER_DIGEST") or "UNAVAILABLE"
    return {
        "schema_version": SCHEMA_VERSION,
        "capture_status": "COMPLETE",
        "captured_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "execution_mode": "READ_ONLY_DISCOVERY",
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
            "image_identity": container_image,
        },
        "host": {
            "hostname": platform.node() or None,
            "architecture": platform.machine() or None,
            "os_release": os_release.decode("utf-8", errors="replace") if os_release is not None else None,
            "os_release_sha256": sha256_bytes(os_release) if os_release is not None else None,
            "kernel_release": platform.release() or None,
            "boot_id": (read_bytes("/proc/sys/kernel/random/boot_id") or b"").decode("utf-8", errors="replace").strip() or None,
            "python": executable_identity(python_path, version=platform.python_version()),
            "timeout": executable_identity(timeout_path),
        },
        "gpu": parse_gpu(gpu_command),
        "runtime": {
            "container_image": container_image,
            "container_digest": container_digest,
            "container_digest_status": "OBSERVED" if container_digest != "UNAVAILABLE" else "UNAVAILABLE",
            "cuda": cuda_identity(torch_record, nvcc_command),
            "driver": next((device.get("driver_version") for device in parse_gpu(gpu_command)["devices"] if device.get("driver_version")), None),
            "torch": torch_record,
            "packages": packages,
            "backends": [],
        },
        "storage": {"vault": storage_identity("/vault"), "workspace": storage_identity("/workspace")},
        "environment_presence": {name: name in os.environ for name in ("CUDA_VISIBLE_DEVICES", "HF_HOME", "HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "LD_LIBRARY_PATH")},
        "observed_commands": [GPU_QUERY, ["nvcc", "--version"]],
        "prohibitions": {
            "remote_writes": False,
            "package_install": False,
            "model_access": False,
            "inference": False,
            "cuda_benchmark": False,
            "gpu_workload": False,
            "network_access": False,
        },
    }


def main() -> int:
    result = environment_probe()
    sys.stdout.write(canonical_bytes(result).decode("utf-8") + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

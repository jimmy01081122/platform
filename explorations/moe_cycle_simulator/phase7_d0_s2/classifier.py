"""Fail-closed classification for D0-S2 probe records."""

from __future__ import annotations

import re
from typing import Any


TARGET_GPU = "NVIDIA RTX PRO 6000 Blackwell Workstation Edition"
MIN_MEMORY_BYTES = 96_000_000_000
MIN_VAULT_BYTES = 200 * 1024**3
MIN_DRIVER = (575, 51, 3)
MIN_CUDA = (12, 8)
REQUIRED_PACKAGES = ("vllm", "torch", "transformers", "tokenizers", "huggingface_hub")
VERSION_RE = re.compile(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?")


def _version(value: Any) -> tuple[int, int, int] | None:
    if not isinstance(value, str):
        return None
    match = VERSION_RE.match(value.strip())
    if not match:
        return None
    return tuple(int(match.group(index) or 0) for index in range(1, 4))


def _at_least(value: Any, minimum: tuple[int, int, int]) -> bool:
    parsed = _version(value)
    return parsed is not None and parsed >= minimum


def _finding(findings: list[str], key: str, condition: bool) -> None:
    if not condition:
        findings.append(key)


def classify_probe(probe: dict[str, Any]) -> dict[str, Any]:
    """Classify only facts required before Gate M; no authority is granted."""

    blocking: list[str] = []
    observational: list[str] = []
    passed: list[str] = []

    gpu = probe.get("gpu") if isinstance(probe.get("gpu"), dict) else {}
    devices = gpu.get("devices") if isinstance(gpu.get("devices"), list) else []
    _finding(blocking, "GPU_QUERY_NOT_COMPLETE", gpu.get("query_status") == "COMPLETE")
    _finding(blocking, "GPU_COUNT_NOT_ONE", gpu.get("count") == 1 and len(devices) == 1)
    if len(devices) == 1 and isinstance(devices[0], dict):
        device = devices[0]
        _finding(blocking, "GPU_EXACT_PRODUCT_MISMATCH", device.get("name") == TARGET_GPU)
        _finding(blocking, "GPU_MEMORY_BELOW_96GB", isinstance(device.get("memory_total_bytes"), int) and device["memory_total_bytes"] >= MIN_MEMORY_BYTES)
        _finding(blocking, "NVIDIA_DRIVER_BELOW_R575_FLOOR", _at_least(device.get("driver_version"), MIN_DRIVER))

    runtime = probe.get("runtime") if isinstance(probe.get("runtime"), dict) else {}
    cuda = runtime.get("cuda") if isinstance(runtime.get("cuda"), dict) else {}
    _finding(blocking, "CUDA_RUNTIME_NOT_OBSERVED", cuda.get("runtime_status") == "OBSERVED")
    _finding(blocking, "CUDA_RUNTIME_BELOW_12_8", _at_least(cuda.get("runtime_version"), MIN_CUDA))
    torch = runtime.get("torch") if isinstance(runtime.get("torch"), dict) else {}
    _finding(blocking, "PYTORCH_IMPORT_NOT_COMPLETE", torch.get("import_status") == "COMPLETE")
    _finding(blocking, "PYTORCH_CUDA_BUILD_NOT_OBSERVED", bool(torch.get("cuda_build")))
    _finding(blocking, "PYTORCH_CUDA_NOT_AVAILABLE", torch.get("cuda_available") is True)

    packages = runtime.get("packages") if isinstance(runtime.get("packages"), dict) else {}
    for name in REQUIRED_PACKAGES:
        package = packages.get(name) if isinstance(packages.get(name), dict) else {}
        _finding(blocking, f"{name.upper()}_NOT_COMPLETE", package.get("status") == "COMPLETE" and package.get("present") is True)
        _finding(blocking, f"{name.upper()}_DISTRIBUTION_HASH_NOT_OBSERVED", bool(package.get("distribution_sha256")))

    python = (probe.get("host") or {}).get("python") if isinstance(probe.get("host"), dict) else {}
    _finding(blocking, "PYTHON_EXECUTABLE_HASH_NOT_OBSERVED", bool(python.get("sha256")))

    vault = (probe.get("storage") or {}).get("vault") if isinstance(probe.get("storage"), dict) else {}
    _finding(blocking, "VAULT_NOT_MOUNTED", vault.get("mounted") is True and vault.get("is_symlink") is False)
    _finding(blocking, "VAULT_FREE_SPACE_BELOW_200_GIB", isinstance(vault.get("free_bytes"), int) and vault["free_bytes"] >= MIN_VAULT_BYTES)

    image = runtime.get("container_image")
    digest_status = runtime.get("container_digest_status")
    if image == "UNAVAILABLE":
        observational.append("CONTAINER_IMAGE_IDENTITY_UNAVAILABLE")
    else:
        passed.append("CONTAINER_IMAGE_IDENTITY_OBSERVED")
    if digest_status == "UNAVAILABLE" or runtime.get("container_digest") == "UNAVAILABLE":
        observational.append("CONTAINER_DIGEST_UNAVAILABLE_NONBLOCKING")
    else:
        passed.append("CONTAINER_DIGEST_OBSERVED")

    instance = probe.get("instance") if isinstance(probe.get("instance"), dict) else {}
    if not all(instance.get(field) for field in ("principal", "environment_label")):
        observational.append("PROVIDER_INSTANCE_METADATA_UNAVAILABLE")
    else:
        passed.append("PROVIDER_INSTANCE_METADATA_OBSERVED")
    observational.append("HANDOFF_AND_DEADLINE_TIME_FIELDS_OBSERVATIONAL")

    if not blocking:
        passed.extend(
            [
                "GPU_TARGET_AND_CAPACITY_OBSERVED",
                "CUDA_PYTORCH_RUNTIME_OBSERVED",
                "VLLM_AND_HF_DISTRIBUTIONS_HASHED",
                "PYTHON_EXECUTABLE_HASHED",
                "VAULT_CAPACITY_OBSERVED",
            ]
        )
    return {
        "schema_version": "moe-simulator-phase7-gputw-d0-s2-classification-v1",
        "d0_stage": "D0-S2",
        "d0_status": "READY_FOR_GATE_M_APPLICATION" if not blocking else "INCOMPLETE_NOT_READY",
        "environment_eligibility": "READY_FOR_GATE_M_APPLICATION" if not blocking else "NOT_READY",
        "formal_status": "NONFORMAL_DISCOVERY_PROVENANCE",
        "classification_basis": "RUNTIME_IDENTITY_AND_CAPACITY_DISCOVERY_ONLY",
        "passed_observations": sorted(set(passed)),
        "blocking_findings": sorted(set(blocking)),
        "observational_findings": sorted(set(observational)),
        "promotable": False,
        "next_legal_action": (
            "STOP_AND_SUBMIT_FRESH_D0_RESULT_FOR_OWNER_GATE_M_REVIEW"
            if not blocking
            else "STOP_AND_REVIEW_D0_S2_DISCOVERY_GAPS"
        ),
        "authority": {"d0": "NOT_AUTHORIZED", "gate_m": "NOT_AUTHORIZED", "m0": "NOT_AUTHORIZED", "gpu": "NONE"},
        "retry_allowed": False,
        "resume_allowed": False,
        "model_download_performed": False,
        "gpu_workload_performed": False,
    }

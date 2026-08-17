#!/usr/bin/env python3
"""Capture and compare the approved host/GPU/software identity before M0 work."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[5]))

from explorations.moe_cycle_simulator.phase7.application.executor.common import (  # noqa: E402
    M0Error,
    file_sha256,
    load_json,
    require_materialization_unlock,
    require_unlock,
    semantic_sha256,
    validate_contract,
    write_new_json,
)
from explorations.moe_cycle_simulator.phase7.application.executor.package_ledger import (  # noqa: E402
    build as build_application_ledger,
)
from explorations.moe_cycle_simulator.phase7.application.executor.runtime_attestation import (  # noqa: E402
    validate_runtime_attestation,
)
from explorations.moe_cycle_simulator.phase7.application.executor.vllm_runtime_adapter import (  # noqa: E402
    load_adapter_contract,
)
from explorations.moe_cycle_simulator.phase7.application.executor.storage_identity import (  # noqa: E402
    validate_mount_identity,
)


def command(argv: list[str]) -> dict[str, Any]:
    completed = subprocess.run(
        argv,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
    )
    if completed.returncode != 0:
        raise M0Error(f"preflight command failed: {argv!r}, rc={completed.returncode}")
    return {
        "argv": argv,
        "returncode": completed.returncode,
        "stdout_sha256": hashlib.sha256(completed.stdout).hexdigest(),
        "stderr_sha256": hashlib.sha256(completed.stderr).hexdigest(),
        "stdout_utf8": completed.stdout.decode("utf-8", errors="strict"),
    }


def host_memory_bytes() -> int:
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        if line.startswith("MemTotal:"):
            return int(line.split()[1]) * 1024
    raise M0Error("MemTotal is unavailable")


def cpu_model() -> str:
    for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
        if line.lower().startswith("model name"):
            return line.split(":", 1)[1].strip()
    raise M0Error("CPU model is unavailable")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("materialization", "execution"), required=True)
    parser.add_argument("--application-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    application = args.application_dir.resolve(strict=True)
    contract = load_json(application / "m0_execution_contract.json")
    environment = load_json(application / "environment_manifest.template.json")
    validate_contract(contract)
    runtime_attestation_evidence: dict[str, Any] = {}
    if args.mode == "materialization":
        require_materialization_unlock(contract)
    else:
        require_unlock(contract)
        runtime = load_json(application / "runtime_variant.template.json")
        adapter = load_adapter_contract(runtime)
        build_attestation = validate_runtime_attestation(runtime)
        runtime_attestation_evidence = {
            "vllm_source_git_commit": runtime["runtime"]["git_commit"],
            "installed_distribution_ledger_sha256": build_attestation[
                "installed_distribution"
            ]["ledger_sha256"],
            "build_attestation_file_sha256": runtime["runtime_attestation"][
                "build_attestation_file_sha256"
            ],
            "container_sbom_sha256": build_attestation["container"][
                "sbom_sha256"
            ],
            "runtime_adapter_contract_sha256": runtime[
                "runtime_adapter_contract"
            ]["file_sha256"],
            "runtime_adapter_id": adapter["adapter_id"],
        }
    if args.output.exists():
        raise M0Error("preflight evidence output already exists")

    gpu_command = command(
        [
            "nvidia-smi",
            "--query-gpu=name,memory.total,uuid,pci.bus_id,driver_version,compute_mode,mig.mode.current",
            "--format=csv,noheader,nounits",
        ]
    )
    rows = [row for row in gpu_command["stdout_utf8"].splitlines() if row.strip()]
    if len(rows) != 1:
        raise M0Error("preflight requires exactly one visible GPU")
    fields = [item.strip() for item in rows[0].split(",")]
    if len(fields) != 7:
        raise M0Error("unexpected GPU preflight field count")
    observed_gpu = {
        "name": fields[0],
        "total_memory_bytes": int(fields[1]) * 1024 * 1024,
        "uuid": fields[2],
        "pci_bus_id": fields[3],
        "driver_version": fields[4],
        "compute_mode": fields[5],
        "mig_mode": fields[6],
    }
    frozen_gpu = environment["gpu"]
    frozen_software = environment["software"]
    expected = {
        "name": frozen_gpu["exact_product_name"],
        "uuid": frozen_gpu["uuid"],
        "pci_bus_id": frozen_gpu["pci_bus_id"],
        "driver_version": frozen_software["driver_version"],
        "compute_mode": frozen_gpu["compute_mode"],
        "mig_mode": frozen_gpu["mig_mode"],
    }
    for key, value in expected.items():
        observed = observed_gpu[key]
        if key == "pci_bus_id":
            observed, value = observed.lower(), value.lower()
        if observed != value:
            raise M0Error(f"frozen/observed GPU identity mismatch: {key}")
    if (
        observed_gpu["total_memory_bytes"] < contract["target"]["minimum_memory_bytes"]
        or observed_gpu["total_memory_bytes"] < int(frozen_gpu["total_memory_bytes"])
        or frozen_gpu["exclusive_allocation_confirmed"] is not True
    ):
        raise M0Error("GPU capacity/exclusive-allocation gate failed")

    os_release_path = Path("/etc/os-release")
    os_release = os_release_path.read_text(encoding="utf-8")
    host = environment["host"]
    observed_host = {
        "os_release": os_release,
        "os_release_sha256": file_sha256(os_release_path),
        "kernel_release": platform.release(),
        "cpu_model": cpu_model(),
        "logical_cpu_count": os.cpu_count(),
        "host_memory_bytes": host_memory_bytes(),
        "working_directory": str(Path(host["working_directory"]).resolve(strict=True)),
    }
    for key in (
        "os_release",
        "os_release_sha256",
        "kernel_release",
        "cpu_model",
        "logical_cpu_count",
        "host_memory_bytes",
        "working_directory",
    ):
        if observed_host[key] != host[key]:
            raise M0Error(f"frozen/observed host mismatch: {key}")
    free_storage = shutil.disk_usage(observed_host["working_directory"]).free
    vault_identity = validate_mount_identity(
        host["persistent_mount"],
        host["vault_mount_identity_sha256"],
    )
    project_root = Path(observed_host["working_directory"])
    try:
        project_root.relative_to(Path(host["persistent_mount"]))
    except ValueError as exc:
        raise M0Error("working directory escaped the persistent mount") from exc
    floor_field = (
        "minimum_free_bytes_before_materialization"
        if args.mode == "materialization"
        else "minimum_free_bytes_before_execution"
    )
    if free_storage < int(host[floor_field]):
        raise M0Error("free working storage is below the frozen floor")

    packages = {
        name: importlib.metadata.version(name)
        for name in ("vllm", "torch", "transformers", "huggingface_hub")
    }
    package_fields = {
        "vllm": "vllm_version",
        "torch": "torch_version",
        "transformers": "transformers_version",
        "huggingface_hub": "huggingface_hub_version",
    }
    for name, field in package_fields.items():
        if packages[name] != frozen_software[field]:
            raise M0Error(f"frozen/observed package mismatch: {name}")
    if platform.python_version() != frozen_software["python_version"]:
        raise M0Error("frozen/observed Python version mismatch")
    import torch

    if torch.version.cuda != frozen_software["cuda_runtime_version"]:
        raise M0Error("frozen/observed CUDA runtime version mismatch")
    nvcc_command = command(["nvcc", "--version"])
    if frozen_software["cuda_toolkit_version"] not in nvcc_command["stdout_utf8"]:
        raise M0Error("frozen CUDA toolkit version is absent from nvcc evidence")
    container_digest = os.environ.get("MOE_PHASE7_CONTAINER_DIGEST")
    if container_digest != frozen_software["container_digest"]:
        raise M0Error("container digest environment attestation mismatch")

    egress_path = Path(
        environment["network"]["qualification_egress_evidence_path"]
    ).resolve(strict=True)
    if (
        file_sha256(egress_path)
        != environment["network"]["qualification_egress_enforcement_sha256"]
    ):
        raise M0Error("qualification egress-enforcement evidence mismatch")
    evidence = {
        "schema_version": "moe-simulator-phase7-m0-preflight-evidence-v1",
        "mode": args.mode,
        "status": "PASS",
        "application_ledger_sha256": build_application_ledger(application)[
            "ledger_sha256"
        ],
        "gpu": observed_gpu,
        "host": {
            **observed_host,
            "free_working_storage_bytes": free_storage,
            "vault_mount_identity": vault_identity,
        },
        "software": {
            "python_version": platform.python_version(),
            **{f"{name}_version": value for name, value in packages.items()},
            "cuda_runtime_version": torch.version.cuda,
            "cuda_toolkit_version": frozen_software["cuda_toolkit_version"],
            "container_digest": container_digest,
            **runtime_attestation_evidence,
        },
        "network": {
            "qualification_egress_enforcement": environment["network"][
                "qualification_egress_enforcement"
            ],
            "evidence_path": str(egress_path),
            "evidence_sha256": file_sha256(egress_path),
        },
        "raw_commands": [gpu_command, nvcc_command],
    }
    evidence["evidence_sha256"] = semantic_sha256(evidence)
    write_new_json(args.output, evidence)
    print(evidence["evidence_sha256"])
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except M0Error as exc:
        raise SystemExit(f"HARD-STOP: {exc}") from exc

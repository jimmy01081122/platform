#!/usr/bin/env python3
"""Strict D0 plan, approval, SSH, and probe validation primitives."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

from explorations.moe_cycle_simulator.phase7.application.executor.allocation import (
    validate_allocation_window,
)
from explorations.moe_cycle_simulator.phase7.application.executor.common import (
    M0Error,
    SHA256_RE,
    file_sha256,
    semantic_sha256,
)


EXPECTED_GPU = "NVIDIA RTX PRO 6000 Blackwell Workstation Edition"
SAFE_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
SAFE_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
SAFE_SESSION_RE = re.compile(r"^phase7-d0-[a-z0-9][a-z0-9._-]{7,96}$")


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise M0Error(f"{label} key closure mismatch")


def validate_disclosure_plan(
    plan: Mapping[str, Any],
    *,
    application_dir: Path,
    verify_files: bool,
) -> None:
    _exact_keys(
        plan,
        {
            "schema_version",
            "application_id",
            "status",
            "fresh_session_required",
            "resume_allowed",
            "retry_allowed",
            "target",
            "ssh",
            "probe",
            "allocation_window",
            "storage_expectation",
            "output",
            "authority",
        },
        "D0 plan",
    )
    if (
        plan["schema_version"] != "moe-simulator-phase7-d0-plan-v1"
        or plan["application_id"]
        != "mixtral-rtxpro6000-bf16-m0-r12-20260729"
        or plan["status"] not in {"DRAFT_WITH_BLOCKING_FIELDS", "FROZEN"}
        or plan["fresh_session_required"] is not True
        or plan["resume_allowed"] is not False
        or plan["retry_allowed"] is not False
        or plan["target"]
        != {
            "provider": "GPUtw",
            "gpu_count": 1,
            "exact_product_name": EXPECTED_GPU,
            "minimum_memory_bytes": 96_000_000_000,
        }
    ):
        raise M0Error("D0 plan identity or authority changed")
    ssh = plan["ssh"]
    _exact_keys(
        ssh,
        {
            "executable",
            "executable_sha256",
            "host",
            "port",
            "username",
            "known_hosts_file",
            "known_hosts_file_sha256",
            "runtime_known_hosts_relative_path",
            "host_key_algorithm",
            "host_public_key_blob_sha256",
            "openssh_fingerprint",
            "credential_storage",
        },
        "D0 SSH",
    )
    if (
        not isinstance(ssh["host"], str)
        or SAFE_HOST_RE.fullmatch(ssh["host"]) is None
        or isinstance(ssh["port"], bool)
        or not isinstance(ssh["port"], int)
        or not 1 <= ssh["port"] <= 65535
        or not isinstance(ssh["username"], str)
        or SAFE_USER_RE.fullmatch(ssh["username"]) is None
        or ssh["host_key_algorithm"] != "ssh-ed25519"
        or ssh["credential_storage"] != "EXTERNAL_NOT_RECORDED"
        or ssh["runtime_known_hosts_relative_path"]
        != "disclosure_inputs/known_hosts"
    ):
        raise M0Error("D0 SSH endpoint is invalid")
    probe = plan["probe"]
    _exact_keys(
        probe,
        {
            "source_relative_path",
            "source_sha256",
            "controller_relative_path",
            "controller_sha256",
            "remote_argv",
            "exact_ssh_argv_sha256",
            "timeout_seconds",
            "max_stdout_bytes",
            "max_stderr_bytes",
            "transport",
        },
        "D0 probe",
    )
    remote_argv = probe["remote_argv"]
    digest_assignment = (
        remote_argv[1]
        if isinstance(remote_argv, list) and len(remote_argv) == 6
        else ""
    )
    digest_value = digest_assignment.removeprefix(
        "MOE_PHASE7_CONTAINER_DIGEST="
    )
    digest_is_draft = digest_value == "[BLOCKING:PROVIDER_ATTESTED_CONTAINER_DIGEST]"
    if (
        probe["source_relative_path"] != "executor/environment_probe.py"
        or probe["controller_relative_path"] != "executor/disclosure_driver.py"
        or remote_argv[:1] != ["env"]
        or remote_argv[2:] != ["python3", "-I", "-B", "-"]
        or not digest_assignment.startswith("MOE_PHASE7_CONTAINER_DIGEST=")
        or (
            plan["status"] == "FROZEN"
            and not re.fullmatch(r"sha256:[0-9a-f]{64}", digest_value)
        )
        or (
            plan["status"] != "FROZEN"
            and not (
                digest_is_draft
                or re.fullmatch(r"sha256:[0-9a-f]{64}", digest_value)
            )
        )
        or probe["transport"] != "PYTHON_STDLIB_SOURCE_OVER_STDIN"
        or probe["timeout_seconds"] != 120
        or probe["max_stdout_bytes"] != 1_048_576
        or probe["max_stderr_bytes"] != 262_144
    ):
        raise M0Error("D0 probe scope changed")
    storage = plan["storage_expectation"]
    if storage != {
        "persistent_mount": "/vault",
        "ephemeral_mount": "/workspace",
        "persistent_project_prefix": "/vault/flow",
        "minimum_persistent_free_bytes": 300_000_000_000,
        "remote_write_test": "NOT_AUTHORIZED_IN_D0",
        "credentials_in_persistent_storage": "FORBIDDEN",
        "credentials_in_workspace": "FORBIDDEN",
    }:
        raise M0Error("D0 Vault/workspace contract changed")
    output = plan["output"]
    _exact_keys(
        output,
        {"local_evidence_root", "terminal_sealing"},
        "D0 output",
    )
    if output["terminal_sealing"] != "REQUIRED":
        raise M0Error("D0 evidence sealing must remain required")
    authority = plan["authority"]
    if authority != {
        "required_unlock": "OWNER_DELEGATED_EXACT_D0_COMMAND",
        "remote_file_write": False,
        "download": False,
        "package_install": False,
        "model_access": False,
        "gpu_compute": False,
        "allowed_remote_commands": ["nvidia-smi"],
        "allowed_remote_files": [
            "/etc/os-release",
            "/proc/cpuinfo",
            "/proc/meminfo",
            "/proc/sys/kernel/random/boot_id",
        ],
    }:
        raise M0Error("D0 authority expanded")
    validate_allocation_window(plan["allocation_window"])
    if not verify_files:
        return
    executable = Path(ssh["executable"])
    known_hosts = Path(ssh["known_hosts_file"])
    if (
        not executable.is_absolute()
        or not executable.is_file()
        or executable.is_symlink()
        or file_sha256(executable) != ssh["executable_sha256"]
    ):
        raise M0Error("D0 SSH executable identity mismatch")
    if (
        not known_hosts.is_absolute()
        or not known_hosts.is_file()
        or known_hosts.is_symlink()
        or file_sha256(known_hosts) != ssh["known_hosts_file_sha256"]
    ):
        raise M0Error("D0 known_hosts identity mismatch")
    for relative_key, hash_key in (
        ("source_relative_path", "source_sha256"),
        ("controller_relative_path", "controller_sha256"),
    ):
        candidate = application_dir / probe[relative_key]
        if (
            not candidate.is_file()
            or candidate.is_symlink()
            or file_sha256(candidate) != probe[hash_key]
        ):
            raise M0Error(f"D0 bound file differs: {relative_key}")


def build_ssh_argv(plan: Mapping[str, Any]) -> list[str]:
    ssh = plan["ssh"]
    runtime_known_hosts = (
        Path(plan["output"]["local_evidence_root"])
        / ssh["runtime_known_hosts_relative_path"]
    )
    return [
        ssh["executable"],
        "-F",
        "/dev/null",
        "-T",
        "-p",
        str(ssh["port"]),
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={runtime_known_hosts}",
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        "ConnectTimeout=15",
        "-o",
        "ConnectionAttempts=1",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=2",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        "ForwardAgent=no",
        "-o",
        "ProxyCommand=none",
        "-o",
        "ProxyJump=none",
        "-o",
        "RequestTTY=no",
        "-o",
        "LogLevel=ERROR",
        "--",
        f"{ssh['username']}@{ssh['host']}",
        *plan["probe"]["remote_argv"],
    ]


def validate_known_hosts(
    plan: Mapping[str, Any], payload: bytes | None = None
) -> dict[str, str]:
    ssh = plan["ssh"]
    path = Path(ssh["known_hosts_file"])
    expected_host = (
        ssh["host"] if ssh["port"] == 22 else f"[{ssh['host']}]:{ssh['port']}"
    )
    rows = []
    text = (
        path.read_text(encoding="utf-8")
        if payload is None
        else payload.decode("utf-8", errors="strict")
    )
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped and not stripped.startswith("#"):
            rows.append(stripped)
    if len(rows) != 1:
        raise M0Error("D0 requires a dedicated one-key known_hosts file")
    fields = rows[0].split()
    if (
        len(fields) != 3
        or fields[0] != expected_host
        or fields[1] != ssh["host_key_algorithm"]
    ):
        raise M0Error("D0 known_hosts endpoint or key algorithm differs")
    try:
        blob = base64.b64decode(fields[2], validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise M0Error("D0 known_hosts public key is invalid") from exc
    digest = hashlib.sha256(blob).digest()
    blob_hex = digest.hex()
    fingerprint = "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")
    if (
        blob_hex != ssh["host_public_key_blob_sha256"]
        or fingerprint != ssh["openssh_fingerprint"]
    ):
        raise M0Error("D0 known_hosts public-key binding differs")
    return {
        "host_field": fields[0],
        "algorithm": fields[1],
        "public_key_blob_sha256": blob_hex,
        "openssh_fingerprint": fingerprint,
    }


def validate_disclosure_approval(
    approval: Mapping[str, Any],
    *,
    plan: Mapping[str, Any],
    plan_path: Path,
    application_ledger_sha256: str,
    owner_authority_record_sha256: str,
    plan_file_sha256: str | None = None,
) -> None:
    _exact_keys(
        approval,
        {
            "schema_version",
            "application_id",
            "disclosure_session_id",
            "approval_id",
            "approval_token_sha256",
            "used_once_registry_path",
            "application_ledger_sha256",
            "d0_plan_sha256",
            "probe_source_sha256",
            "controller_sha256",
            "exact_ssh_argv_sha256",
            "approved_ssh_host_key_sha256",
            "approved_local_evidence_root",
            "allocation_window",
            "owner_authority_record_sha256",
            "decision",
            "approved_at_utc",
            "owner_identity",
            "authority",
            "not_authorized",
        },
        "D0 approval",
    )
    session_id = approval["disclosure_session_id"]
    if not isinstance(session_id, str) or SAFE_SESSION_RE.fullmatch(session_id) is None:
        raise M0Error("D0 session ID is invalid")
    for field in (
        "approval_token_sha256",
        "application_ledger_sha256",
        "d0_plan_sha256",
        "probe_source_sha256",
        "controller_sha256",
        "exact_ssh_argv_sha256",
        "approved_ssh_host_key_sha256",
        "owner_authority_record_sha256",
    ):
        if not isinstance(approval[field], str) or SHA256_RE.fullmatch(
            approval[field]
        ) is None:
            raise M0Error(f"D0 approval has invalid {field}")
    if (
        approval["schema_version"] != "moe-simulator-phase7-d0-approval-v1"
        or approval["application_id"] != plan["application_id"]
        or approval["decision"] != "APPROVE"
        or approval["application_ledger_sha256"] != application_ledger_sha256
        or approval["d0_plan_sha256"]
        != (plan_file_sha256 or file_sha256(plan_path))
        or approval["probe_source_sha256"] != plan["probe"]["source_sha256"]
        or approval["controller_sha256"] != plan["probe"]["controller_sha256"]
        or approval["exact_ssh_argv_sha256"]
        != semantic_sha256(build_ssh_argv(plan))
        or approval["exact_ssh_argv_sha256"]
        != plan["probe"]["exact_ssh_argv_sha256"]
        or approval["approved_ssh_host_key_sha256"]
        != plan["ssh"]["host_public_key_blob_sha256"]
        or approval["approved_local_evidence_root"]
        != plan["output"]["local_evidence_root"]
        or approval["allocation_window"] != plan["allocation_window"]
        or approval["owner_authority_record_sha256"]
        != owner_authority_record_sha256
    ):
        raise M0Error("D0 approval binding mismatch")
    validate_allocation_window(approval["allocation_window"])
    if approval["authority"] != [
        "one exact SSH connection",
        "exact host-key verification",
        "read-only host GPU runtime and mount disclosure",
        "local evidence capture and sealing",
    ]:
        raise M0Error("D0 approval authority differs")
    required_forbidden = {
        "remote file write",
        "model download",
        "package installation",
        "model materialization",
        "CUDA workload",
        "vLLM model load",
        "M0",
        "M1",
        "M2",
        "M3",
        "M4",
        "retry",
        "resume",
        "allocation extension",
        "additional cost",
    }
    if set(approval["not_authorized"]) != required_forbidden:
        raise M0Error("D0 approval prohibition set differs")


def strict_probe_json(payload: bytes) -> dict[str, Any]:
    try:
        text = payload.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_float=_reject_float,
            parse_constant=_reject_float,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise M0Error("D0 probe stdout is not one strict UTF-8 JSON value") from exc
    if not isinstance(value, dict):
        raise M0Error("D0 probe result root must be an object")
    validate_probe_result(value)
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise M0Error(f"duplicate D0 JSON key: {key}")
        result[key] = value
    return result


def _reject_float(value: str) -> None:
    raise M0Error(f"D0 floating-point JSON value is forbidden: {value}")


def validate_probe_result(value: Mapping[str, Any]) -> None:
    _exact_keys(
        value,
        {
            "schema_version",
            "capture_status",
            "captured_at_utc",
            "host",
            "gpu",
            "mounts",
            "software",
            "environment_presence",
            "prohibitions",
        },
        "D0 probe result",
    )
    if (
        value["schema_version"] != "moe-simulator-phase7-d0-probe-result-v1"
        or value["capture_status"] != "COMPLETE"
        or value["prohibitions"]
        != {
            "remote_file_write_performed": False,
            "download_performed": False,
            "install_performed": False,
            "model_access_performed": False,
            "gpu_workload_performed": False,
            "secret_values_recorded": False,
        }
    ):
        raise M0Error("D0 probe claim boundary differs")
    gpu = value["gpu"]
    if (
        not isinstance(gpu, dict)
        or set(gpu) != {"query_status", "device_count", "devices", "command"}
        or not isinstance(gpu["device_count"], int)
        or gpu["device_count"] < 0
        or not isinstance(gpu["devices"], list)
        or gpu["device_count"] != len(gpu["devices"])
    ):
        raise M0Error("D0 GPU disclosure structure is invalid")
    mounts = value["mounts"]
    if not isinstance(mounts, dict) or set(mounts) != {
        "persistent",
        "ephemeral",
    }:
        raise M0Error("D0 mount disclosure structure is invalid")
    for mount in mounts.values():
        if (
            not isinstance(mount, dict)
            or set(mount)
            != {
                "path",
                "exists",
                "realpath",
                "is_mount",
                "is_symlink",
                "device_id",
                "mode_octal",
                "owner_uid",
                "owner_gid",
                "total_bytes",
                "free_bytes",
                "mount_identity",
            }
        ):
            raise M0Error("D0 mount record key closure differs")
        for field in ("total_bytes", "free_bytes"):
            if mount[field] is not None and (
                isinstance(mount[field], bool)
                or not isinstance(mount[field], int)
                or mount[field] < 0
            ):
                raise M0Error("D0 mount capacity is invalid")
        identity = mount["mount_identity"]
        if identity is not None and (
            not isinstance(identity, dict)
            or not isinstance(identity.get("mount_identity_sha256"), str)
            or SHA256_RE.fullmatch(identity["mount_identity_sha256"]) is None
            or identity.get("mount_point") != mount["path"]
            or identity.get("device_id") != mount["device_id"]
        ):
            raise M0Error("D0 mount identity is invalid")


def classify_environment(
    result: Mapping[str, Any], plan: Mapping[str, Any]
) -> tuple[str, list[str]]:
    findings: list[str] = []
    gpu = result["gpu"]
    devices = gpu["devices"]
    if gpu["query_status"] != "COMPLETE" or len(devices) != 1:
        findings.append("EXACTLY_ONE_GPU_NOT_DISCLOSED")
    else:
        device = devices[0]
        if device.get("name") != EXPECTED_GPU:
            findings.append("GPU_PRODUCT_MISMATCH")
        if device.get("total_memory_bytes", 0) < 96_000_000_000:
            findings.append("GPU_MEMORY_BELOW_FLOOR")
    persistent = result["mounts"]["persistent"]
    if (
        persistent["path"] != "/vault"
        or persistent["exists"] is not True
        or persistent["realpath"] != "/vault"
        or persistent["is_mount"] is not True
        or persistent["is_symlink"] is not False
        or persistent["mount_identity"] is None
    ):
        findings.append("VAULT_MOUNT_NOT_CONFIRMED")
    elif (
        persistent["free_bytes"]
        < plan["storage_expectation"]["minimum_persistent_free_bytes"]
    ):
        findings.append("VAULT_FREE_SPACE_BELOW_FLOOR")
    ephemeral = result["mounts"]["ephemeral"]
    if ephemeral["path"] != "/workspace" or ephemeral["exists"] is not True:
        findings.append("WORKSPACE_NOT_AVAILABLE")
    packages = result["software"]["packages"]
    required_packages = {
        "vllm": "VLLM_NOT_INSTALLED",
        "torch": "TORCH_NOT_INSTALLED",
        "transformers": "TRANSFORMERS_NOT_INSTALLED",
        "huggingface_hub": "HUGGINGFACE_HUB_NOT_INSTALLED",
        "tokenizers": "TOKENIZERS_NOT_INSTALLED",
    }
    for name, finding in required_packages.items():
        if packages.get(name) is None:
            findings.append(finding)
    if result["software"]["commands"].get("nvcc") is None:
        findings.append("NVCC_NOT_INSTALLED")
    container_digest = result["software"]["container_digest_attestation"]
    expected_digest = plan["probe"]["remote_argv"][1].split("=", 1)[1]
    if not isinstance(container_digest, str) or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", container_digest
    ):
        findings.append("CONTAINER_DIGEST_NOT_ATTESTED")
    elif container_digest != expected_digest:
        findings.append("CONTAINER_DIGEST_ATTESTATION_MISMATCH")
    return (
        "READY_FOR_MATERIALIZATION_APPLICATION" if not findings else "NOT_READY",
        findings,
    )

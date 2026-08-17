#!/usr/bin/env python3
"""Run one prospective D0-S3 read-only probe over an owner-supplied SSH endpoint.

This adapter is deliberately outside the CPU-only D0-S2 package identity.  It
is an operational transport wrapper: the endpoint is supplied at invocation,
the probe bytes are captured once, and every terminal result is retained as a
new non-resumable evidence run.  It does not install, download, load, infer,
benchmark, or write on the remote host.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_ROOT = REPO_ROOT / "explorations/moe_cycle_simulator/phase7_d0_s2"
PROBE_PATH = PACKAGE_ROOT / "probe.py"
HISTORICAL_BOOT_ID = "e7e50bde-d257-4eb3-ab18-d48511385bde"
HISTORICAL_ENDPOINT = "pod-a92587c7-d439-42b1-b305-0843acb46d38@ssh.gputw.ai"
ENDPOINT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}@[A-Za-z0-9][A-Za-z0-9.-]{0,253}$")
MAX_STDOUT = 1 * 1024 * 1024
MAX_STDERR = 256 * 1024
REMOTE_TIMEOUT_SECONDS = 300

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest_file(path: str | None) -> str | None:
    if not path:
        return None
    try:
        target = Path(os.path.realpath(path))
        if not target.is_file():
            return None
        return sha256_bytes(target.read_bytes())
    except OSError:
        return None


def write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def write_json(path: Path, value: Any) -> bytes:
    payload = canonical_bytes(value) + b"\n"
    write_bytes(path, payload)
    return payload


def git_identity() -> dict[str, str | None]:
    result: dict[str, str | None] = {"commit": None, "tree": None, "method": "UNAVAILABLE"}
    for key, argv in (("commit", ["git", "rev-parse", "HEAD"]), ("tree", ["git", "rev-parse", "HEAD^{tree}"])):
        completed = subprocess.run(argv, cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False, text=True)
        if completed.returncode == 0:
            result[key] = completed.stdout.strip()
    if result["commit"] and result["tree"]:
        result["method"] = "GIT_CHECKOUT"
    return result


def build_ledger(root: Path, excluded: set[str]) -> dict[str, Any]:
    members: list[dict[str, Any]] = []
    for path in sorted((item for item in root.rglob("*") if item.is_file()), key=lambda item: item.relative_to(root).as_posix().encode()):
        relative = path.relative_to(root).as_posix()
        if relative in excluded:
            continue
        payload = path.read_bytes()
        members.append({"path": relative, "size_bytes": len(payload), "sha256": sha256_bytes(payload)})
    rows = [(item["path"], item["sha256"]) for item in members]
    return {
        "schema_version": "moe-simulator-phase7-gputw-d0-s2-remote-evidence-ledger-v1",
        "run_id": root.name,
        "evidence_class": "OWNER_DIRECT_OPERATIONAL_SSH_WAIVER_NONFORMAL_DISCOVERY_PROVENANCE",
        "member_count": len(members),
        "members": members,
        "ledger_sha256": sha256_bytes(canonical_bytes(rows)),
        "terminal_status": "COMPLETE",
        "retry_allowed": False,
        "resume_allowed": False,
        "remote_writes": False,
        "model_accessed": False,
        "gpu_workload_performed": False,
    }


def base_manifest(run_id: str, endpoint: str, identity: dict[str, Any], started: str) -> dict[str, Any]:
    runner_sha256 = digest_file(str(Path(__file__).resolve()))
    return {
        "schema_version": "moe-simulator-phase7-gputw-d0-s3-remote-run-v1",
        "run_id": run_id,
        "stage": "D0-S3",
        "status": "COMPLETE",
        "evidence_class": "OWNER_DIRECT_OPERATIONAL_SSH_WAIVER_NONFORMAL_DISCOVERY_PROVENANCE",
        "endpoint": endpoint,
        "port": 2222,
        "source_identity": identity,
        "runner_sha256": runner_sha256,
        "started_at_utc": started,
        "authority": {"d0": "NOT_AUTHORIZED", "gate_m": "NOT_AUTHORIZED", "m0": "NOT_AUTHORIZED", "gpu": "NONE"},
        "retry_allowed": False,
        "resume_allowed": False,
        "remote_writes": False,
        "package_install": False,
        "model_accessed": False,
        "inference": False,
        "cuda_benchmark": False,
        "gpu_workload_performed": False,
    }


def run(output: Path, endpoint: str) -> int:
    if not ENDPOINT_RE.fullmatch(endpoint):
        raise ValueError("endpoint must be a simple user@host identity; shell syntax is forbidden")
    if output.exists():
        raise RuntimeError(f"refusing to overwrite existing evidence root: {output}")
    output.mkdir(parents=True)
    (output / "artifacts").mkdir()
    (output / "logs").mkdir()
    (output / "environment").mkdir()

    run_id = output.name
    started = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    probe_bytes = PROBE_PATH.read_bytes()
    ssh_path = shutil.which("ssh") or "/usr/bin/ssh"
    ssh_argv = [
        ssh_path,
        "-F", "/dev/null",
        "-T",
        "-p", "2222",
        "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=no",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "GlobalKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=15",
        "-o", "ConnectionAttempts=1",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=2",
        "-o", "ClearAllForwardings=yes",
        "-o", "ForwardAgent=no",
        "-o", "ProxyCommand=none",
        "-o", "ProxyJump=none",
        "-o", "RequestTTY=no",
        "-o", "ControlMaster=no",
        "-o", "LogLevel=ERROR",
        "--",
        endpoint,
        "env",
        "MOE_PHASE7_CONTAINER_DIGEST=UNAVAILABLE",
        "python3",
        "-I",
        "-B",
        "-",
    ]
    command_record = {
        "schema_version": "moe-simulator-phase7-gputw-d0-s3-remote-command-v1",
        "argv": ssh_argv,
        "stdin_source": "explorations/moe_cycle_simulator/phase7_d0_s2/probe.py",
        "stdin_sha256": sha256_bytes(probe_bytes),
        "ssh_executable": ssh_path,
        "ssh_executable_sha256": digest_file(ssh_path),
        "runner_sha256": digest_file(str(Path(__file__).resolve())),
        "owner_direct_operational_ssh_waiver": True,
        "host_key_provenance": "HOST_KEY_PROVENANCE_NOT_REAUTHENTICATED_FOR_NEW_SESSION",
        "identity_resolution": "DEFAULT_LOCAL_OPENSSH_IDENTITY",
        "remote_writes": False,
        "package_install": False,
        "model_access": False,
        "inference": False,
        "cuda_benchmark": False,
        "gpu_workload": False,
    }
    write_json(output / "artifacts/command.json", command_record)
    write_bytes(output / "logs/command.log", (json.dumps(ssh_argv, ensure_ascii=False, separators=(",", ":")) + "\n").encode())

    env = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"}
    started_ns = time.monotonic_ns()
    try:
        completed = subprocess.run(
            ssh_argv,
            input=probe_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=REPO_ROOT,
            env=env,
            check=False,
            timeout=REMOTE_TIMEOUT_SECONDS,
        )
        transport_error = None
    except subprocess.TimeoutExpired as exc:
        completed = subprocess.CompletedProcess(ssh_argv, returncode=124, stdout=exc.stdout or b"", stderr=exc.stderr or b"timeout")
        transport_error = "REMOTE_TRANSPORT_TIMEOUT"
    except OSError as exc:
        completed = subprocess.CompletedProcess(ssh_argv, returncode=127, stdout=b"", stderr=str(exc).encode())
        transport_error = "LOCAL_SSH_EXECUTION_ERROR"
    elapsed_ns = time.monotonic_ns() - started_ns
    write_bytes(output / "logs/stdout.log", completed.stdout)
    write_bytes(output / "logs/stderr.log", completed.stderr)

    identity = git_identity()
    write_json(output / "environment/tool_versions.json", {
        "python": platform.python_version(),
        "local_ssh_path": ssh_path,
        "local_ssh_sha256": digest_file(ssh_path),
        "probe_sha256": sha256_bytes(probe_bytes),
        "runner_sha256": digest_file(str(Path(__file__).resolve())),
        "source_identity": identity,
    })
    write_bytes(output / "resolved_config.yaml", b"stage: D0-S3\nmode: READ_ONLY_DISCOVERY\nsame_instance_allowed: true\nboot_id_reuse: recorded_nonblocking\nfresh_evidence_namespace: required\nremote_writes: false\npackage_install: false\nmodel_access: false\ninference: false\ncuda_benchmark: false\ngpu_workload: false\nnetwork_access: true\nowner_ssh_waiver: true\n")

    classification: dict[str, Any] | None = None
    freshness: dict[str, Any] = {
        "historical_endpoint": HISTORICAL_ENDPOINT,
        "supplied_endpoint": endpoint,
        "same_endpoint_as_historical": endpoint == HISTORICAL_ENDPOINT,
        "historical_boot_id": HISTORICAL_BOOT_ID,
        "observed_boot_id": None,
        "freshness_status": "NOT_OBSERVED",
        "fresh_session_required": True,
        "fresh_evidence_namespace_required": True,
        "same_instance_allowed": True,
        "boot_id_reuse_policy": "RECORDED_NONBLOCKING",
        "promotion_blocking": False,
        "resume_allowed": False,
    }
    failure: str | None = transport_error
    if failure is None and completed.returncode == 0 and len(completed.stdout) <= MAX_STDOUT and len(completed.stderr) <= MAX_STDERR:
        try:
            from explorations.moe_cycle_simulator.phase7_d0_s2.classifier import classify_probe
            from explorations.moe_cycle_simulator.phase7_d0_s2.validate_d0_s2 import load_json, validate_schema

            probe = json.loads(completed.stdout.decode("utf-8"))
            validate_schema(probe, load_json(PACKAGE_ROOT / "schemas/probe.schema.json"))
            classification = classify_probe(probe)
            validate_schema(classification, load_json(PACKAGE_ROOT / "schemas/classification.schema.json"))
            write_json(output / "artifacts/probe.json", probe)
            write_json(output / "artifacts/classification.json", classification)
            observed_boot = (probe.get("host") or {}).get("boot_id")
            freshness["observed_boot_id"] = observed_boot
            if observed_boot and observed_boot != HISTORICAL_BOOT_ID:
                freshness["freshness_status"] = "FRESH_BOOT_ID_OBSERVED"
            elif observed_boot == HISTORICAL_BOOT_ID:
                freshness["freshness_status"] = "REUSED_HISTORICAL_BOOT_ID"
            else:
                freshness["freshness_status"] = "BOOT_ID_UNAVAILABLE"
        except Exception as exc:
            failure = f"REMOTE_PROBE_SCHEMA_OR_JSON_INVALID:{type(exc).__name__}:{exc}"
    elif failure is None:
        failure = "REMOTE_TRANSPORT_NONZERO_OR_OUTPUT_LIMIT"
    write_json(output / "artifacts/session_freshness.json", freshness)

    blocking_count = len(classification["blocking_findings"]) if classification else 1
    d0_status = "READY_FOR_GATE_M_APPLICATION" if failure is None and blocking_count == 0 else "INCOMPLETE_NOT_READY"
    metrics = {
        "schema_version": "moe-simulator-phase7-gputw-d0-s3-remote-metrics-v1",
        "d0_status": d0_status,
        "transport_returncode": completed.returncode,
        "transport_error": failure,
        "elapsed_monotonic_ns": elapsed_ns,
        "stdout_bytes": len(completed.stdout),
        "stderr_bytes": len(completed.stderr),
        "stdout_sha256": sha256_bytes(completed.stdout),
        "stderr_sha256": sha256_bytes(completed.stderr),
        "classification_blocking_count": len(classification["blocking_findings"]) if classification else None,
        "freshness_status": freshness["freshness_status"],
        "freshness_promotion_blocking": False,
        "authority": {"d0": "NOT_AUTHORIZED", "gate_m": "NOT_AUTHORIZED", "m0": "NOT_AUTHORIZED", "gpu": "NONE"},
        "remote_writes": False,
        "package_install": False,
        "model_accessed": False,
        "inference": False,
        "cuda_benchmark": False,
        "gpu_workload_performed": False,
        "retry_allowed": False,
        "resume_allowed": False,
    }
    write_json(output / "metrics.json", metrics)
    if failure is not None:
        write_json(output / "failure.json", {"schema_version": "moe-simulator-phase7-gputw-d0-s3-remote-failure-v1", "terminal_status": "INCOMPLETE", "failure": failure, "retry_allowed": False, "resume_allowed": False, "gpu_workload_performed": False})
    manifest = base_manifest(run_id, endpoint, identity, started)
    manifest.update({"status": d0_status, "transport_returncode": completed.returncode, "freshness_status": freshness["freshness_status"], "failure": failure, "next_legal_action": "STOP_AND_REQUEST_GATE_M_REVIEW" if d0_status == "READY_FOR_GATE_M_APPLICATION" else "STOP_AND_RELEASE_PROVIDER"})
    write_json(output / "manifest.json", manifest)
    write_json(output / "evidence_ledger.json", build_ledger(output, {"evidence_ledger.json"}))
    return 0 if failure is None and d0_status == "READY_FOR_GATE_M_APPLICATION" else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        return run(args.output.resolve(), args.endpoint)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"D0-S2 remote runner refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

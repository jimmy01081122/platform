#!/usr/bin/env python3
"""Run one exact, read-only D0 SSH disclosure and seal local evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[5]))

from explorations.moe_cycle_simulator.phase7.application.executor.allocation import (  # noqa: E402
    D0_STAGE_SECONDS,
    M0_STAGE_SECONDS,
    MATERIALIZATION_STAGE_SECONDS,
    require_remaining_budget,
)
from explorations.moe_cycle_simulator.phase7.application.executor.authority import (  # noqa: E402
    retain_authority,
    validate_retained_authority,
    write_exact_new,
)
from explorations.moe_cycle_simulator.phase7.application.executor.common import (  # noqa: E402
    M0Error,
    file_sha256,
    load_json_bytes,
    semantic_sha256,
    write_new_json,
)
from explorations.moe_cycle_simulator.phase7.application.executor.d0_finalize import (  # noqa: E402
    seal_d0_terminal,
)
from explorations.moe_cycle_simulator.phase7.application.executor.disclosure import (  # noqa: E402
    build_ssh_argv,
    classify_environment,
    strict_probe_json,
    validate_disclosure_approval,
    validate_disclosure_plan,
    validate_known_hosts,
)
from explorations.moe_cycle_simulator.phase7.application.executor.package_ledger import (  # noqa: E402
    build as build_application_ledger,
)
from explorations.moe_cycle_simulator.phase7.application.executor.process_tree import (  # noqa: E402
    ProcessTreeContainment,
    enable_child_subreaper,
)


ACTIVE_PROCESS: subprocess.Popen[bytes] | None = None
ACTIVE_CONTAINMENT: ProcessTreeContainment | None = None


class D0Interrupted(M0Error):
    """The bounded D0 controller received an outer signal."""


def terminate_active() -> dict[str, object]:
    global ACTIVE_CONTAINMENT, ACTIVE_PROCESS
    if ACTIVE_CONTAINMENT is None:
        return {"status": "NOT_STARTED"}
    cleanup = ACTIVE_CONTAINMENT.terminate(
        term_grace_seconds=10,
        kill_grace_seconds=10,
    )
    if ACTIVE_PROCESS is not None:
        try:
            ACTIVE_PROCESS.wait(timeout=1)
        except subprocess.TimeoutExpired as exc:
            raise M0Error("D0 direct SSH child survived containment cleanup") from exc
    ACTIVE_PROCESS = None
    ACTIVE_CONTAINMENT = None
    return cleanup


def signal_handler(signum: int, _frame: Any) -> None:
    terminate_active()
    raise D0Interrupted(f"D0 controller received signal {signum}")


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def consume_approval(
    approval: dict[str, Any], approval_path: Path, approval_bytes: bytes
) -> Path:
    registry = Path(approval["used_once_registry_path"])
    if not registry.is_absolute() or registry.name in {"", ".", ".."}:
        raise M0Error("D0 one-shot registry path must be absolute")
    parent = registry.parent.resolve(strict=True)
    if parent != registry.parent or parent.is_symlink():
        raise M0Error("D0 registry parent must be a real directory")
    payload = {
        "schema_version": "moe-simulator-phase7-used-d0-approval-v1",
        "approval_id": approval["approval_id"],
        "approval_token_sha256": approval["approval_token_sha256"],
        "disclosure_session_id": approval["disclosure_session_id"],
        "approval_file_sha256": hashlib.sha256(approval_bytes).hexdigest(),
    }
    try:
        descriptor = os.open(
            str(registry),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o400,
        )
    except FileExistsError as exc:
        raise M0Error("D0 approval was already consumed") from exc
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    return registry


def run_ssh(
    argv: list[str],
    *,
    probe_source: bytes,
    timeout_seconds: int,
) -> tuple[int, bytes, bytes, bool, int]:
    global ACTIVE_CONTAINMENT, ACTIVE_PROCESS
    started = time.monotonic_ns()
    containment = ProcessTreeContainment()
    ACTIVE_CONTAINMENT = containment
    process = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    ACTIVE_PROCESS = process
    containment.attach(process.pid)
    timed_out = False
    try:
        stdout, stderr = process.communicate(
            input=probe_source,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        timed_out = True
        terminate_active()
        stdout, stderr = process.communicate(timeout=1)
    finally:
        if ACTIVE_CONTAINMENT is not None:
            ACTIVE_CONTAINMENT.assert_clean(
                term_grace_seconds=10,
                kill_grace_seconds=10,
            )
        ACTIVE_PROCESS = None
        ACTIVE_CONTAINMENT = None
    return (
        process.returncode if process.returncode is not None else -signal.SIGKILL,
        stdout,
        stderr,
        timed_out,
        time.monotonic_ns() - started,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--application-dir", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    args = parser.parse_args()
    if os.environ.get("MOE_PHASE7_D0_UNLOCK") != "OWNER_DELEGATED_EXACT_D0_COMMAND":
        raise M0Error("missing exact D0 second-factor unlock")
    application = args.application_dir.resolve(strict=True)
    plan_path = application / "environment_disclosure_plan.template.json"
    approval_path = application / "environment_disclosure_approval.template.json"
    owner_path = application / "owner_environment_decision_20260729.json"
    plan_bytes = plan_path.read_bytes()
    approval_bytes = approval_path.read_bytes()
    owner_bytes = owner_path.read_bytes()
    probe_path = application / "executor/environment_probe.py"
    probe_bytes = probe_path.read_bytes()
    known_hosts_bytes = Path(
        load_json_bytes(plan_bytes, str(plan_path))["ssh"]["known_hosts_file"]
    ).read_bytes()
    plan = load_json_bytes(plan_bytes, str(plan_path))
    approval = load_json_bytes(approval_bytes, str(approval_path))
    package = build_application_ledger(application)
    validate_disclosure_plan(plan, application_dir=application, verify_files=True)
    validate_known_hosts(plan, known_hosts_bytes)
    if (
        hashlib.sha256(probe_bytes).hexdigest() != plan["probe"]["source_sha256"]
        or hashlib.sha256(known_hosts_bytes).hexdigest()
        != plan["ssh"]["known_hosts_file_sha256"]
    ):
        raise M0Error("captured D0 probe or known_hosts bytes differ")
    owner_hash = hashlib.sha256(owner_bytes).hexdigest()
    validate_disclosure_approval(
        approval,
        plan=plan,
        plan_path=plan_path,
        application_ledger_sha256=package["ledger_sha256"],
        owner_authority_record_sha256=owner_hash,
        plan_file_sha256=hashlib.sha256(plan_bytes).hexdigest(),
    )
    package_members = {item["path"]: item["sha256"] for item in package["members"]}
    captured_package_files = {
        "environment_disclosure_plan.template.json": hashlib.sha256(plan_bytes).hexdigest(),
        "executor/environment_probe.py": hashlib.sha256(probe_bytes).hexdigest(),
        "owner_environment_decision_20260729.json": owner_hash,
    }
    if any(package_members.get(path) != digest for path, digest in captured_package_files.items()):
        raise M0Error("captured D0 bytes differ from the approved package ledger")
    require_remaining_budget(
        plan["allocation_window"],
        stage_outer_seconds=D0_STAGE_SECONDS,
        downstream_reserve_seconds=(
            MATERIALIZATION_STAGE_SECONDS + M0_STAGE_SECONDS
        ),
    )
    root = args.evidence_root
    if str(root) != approval["approved_local_evidence_root"]:
        raise M0Error("D0 evidence root differs from approval")
    if not root.is_absolute() or root.name in {"", ".", ".."}:
        raise M0Error("D0 evidence root must be an absolute fresh path")
    parent = root.parent.resolve(strict=True)
    if parent != root.parent or parent.is_symlink():
        raise M0Error("D0 evidence parent must be a real directory")
    root.mkdir(mode=0o700, exist_ok=False)
    started_utc = utc_now()
    started_monotonic = time.monotonic_ns()
    authority: dict[str, Any] | None = None
    enable_child_subreaper()
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    try:
        registry = consume_approval(approval, approval_path, approval_bytes)
        authority = retain_authority(
            application=application,
            approval_path=approval_path,
            registry_path=registry,
            evidence_root=root,
            expected_application_ledger_sha256=package["ledger_sha256"],
            approval_bytes=approval_bytes,
            package_ledger=package,
        )
        inputs = root / "disclosure_inputs"
        inputs.mkdir(mode=0o700)
        for source, name in (
            (None, "plan.json"),
            (
                None,
                "environment_probe.py",
            ),
            (None, "known_hosts"),
            (None, "owner_environment_decision.json"),
        ):
            payload = {
                "plan.json": plan_bytes,
                "environment_probe.py": probe_bytes,
                "known_hosts": known_hosts_bytes,
                "owner_environment_decision.json": owner_bytes,
            }[name]
            write_exact_new(inputs / name, payload)
        if file_sha256(inputs / "known_hosts") != plan["ssh"]["known_hosts_file_sha256"]:
            raise M0Error("retained D0 known_hosts bytes differ")
        argv = build_ssh_argv(plan)
        returncode, stdout, stderr, timed_out, elapsed = run_ssh(
            argv,
            probe_source=probe_bytes,
            timeout_seconds=plan["probe"]["timeout_seconds"],
        )
        if (
            len(stdout) > plan["probe"]["max_stdout_bytes"]
            or len(stderr) > plan["probe"]["max_stderr_bytes"]
        ):
            raise M0Error("D0 SSH output exceeds frozen size ceiling")
        write_exact_new(root / "ssh.stdout.json", stdout)
        write_exact_new(root / "ssh.stderr.log", stderr)
        if timed_out:
            raise TimeoutError("D0 SSH probe exceeded its frozen timeout")
        if returncode != 0:
            raise M0Error(f"D0 SSH probe failed with return code {returncode}")
        probe_result = strict_probe_json(stdout)
        eligibility, findings = classify_environment(probe_result, plan)
        authority = validate_retained_authority(
            evidence_root=root,
            require_package_match=True,
        )
        result = {
            "schema_version": "moe-simulator-phase7-d0-result-v1",
            "application_id": plan["application_id"],
            "disclosure_session_id": approval["disclosure_session_id"],
            "disclosure_status": "COMPLETE",
            "environment_eligibility": eligibility,
            "eligibility_findings": findings,
            "authority_evidence_sha256": semantic_sha256(authority),
            "plan_file_sha256": hashlib.sha256(plan_bytes).hexdigest(),
            "approval_file_sha256": hashlib.sha256(approval_bytes).hexdigest(),
            "probe_file_sha256": plan["probe"]["source_sha256"],
            "exact_ssh_argv_sha256": semantic_sha256(argv),
            "timing": {
                "controller_start_utc": started_utc,
                "controller_end_utc": utc_now(),
                "elapsed_monotonic_ns": time.monotonic_ns() - started_monotonic,
                "lease_start_utc": plan["allocation_window"]["lease_start_utc"],
                "lease_deadline_utc": plan["allocation_window"][
                    "lease_deadline_utc"
                ],
                "ssh_elapsed_monotonic_ns": elapsed,
            },
            "ssh": {
                "endpoint": {
                    "host": plan["ssh"]["host"],
                    "port": plan["ssh"]["port"],
                    "username": plan["ssh"]["username"],
                },
                "host_public_key_blob_sha256": plan["ssh"][
                    "host_public_key_blob_sha256"
                ],
                "returncode": returncode,
                "stdout_sha256": file_sha256(root / "ssh.stdout.json"),
                "stderr_sha256": file_sha256(root / "ssh.stderr.log"),
            },
            "probe_result_sha256": semantic_sha256(probe_result),
            "vault_mount_identity_sha256": probe_result["mounts"]["persistent"][
                "mount_identity"
            ]["mount_identity_sha256"],
            "prohibitions": probe_result["prohibitions"],
            "next_legal_action": (
                "FREEZE_ENVIRONMENT_AND_PREPARE_MATERIALIZATION"
                if eligibility == "READY_FOR_MATERIALIZATION_APPLICATION"
                else "HARD_STOP_REVIEW_ENVIRONMENT_FINDINGS"
            ),
        }
        write_new_json(root / "d0_result.json", result)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        seal_d0_terminal(root, "COMPLETE")
        print(semantic_sha256(result))
        return 0
    except Exception as exc:
        failure = {
            "schema_version": "moe-simulator-phase7-d0-failure-v1",
            "disclosure_status": (
                "INCOMPLETE"
                if isinstance(exc, (TimeoutError, KeyboardInterrupt, D0Interrupted))
                else "FAILED"
            ),
            "failure_type": type(exc).__name__,
            "failure": str(exc),
            "controller_start_utc": started_utc,
            "controller_end_utc": utc_now(),
            "elapsed_monotonic_ns": time.monotonic_ns() - started_monotonic,
            "authority_evidence_sha256": (
                semantic_sha256(authority) if authority is not None else None
            ),
            "retry_allowed": False,
            "resume_allowed": False,
            "gpu_workload_performed": False,
        }
        if authority is None and (root / "authority").exists():
            try:
                authority = validate_retained_authority(
                    evidence_root=root,
                    require_package_match=False,
                )
                failure["authority_evidence_sha256"] = semantic_sha256(authority)
            except Exception:
                pass
        write_new_json(root / "d0_failure.json", failure)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        seal_d0_terminal(root, failure["disclosure_status"])
        if isinstance(exc, M0Error):
            raise
        raise M0Error(str(exc)) from exc


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except M0Error as exc:
        raise SystemExit(f"HARD-STOP: {exc}") from exc

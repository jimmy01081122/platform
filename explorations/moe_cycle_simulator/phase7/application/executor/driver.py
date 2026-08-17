#!/usr/bin/env python3
"""Execute one approved fresh M0 session and preserve every failure."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[5]))

from explorations.moe_cycle_simulator.phase7.application.executor.common import (  # noqa: E402
    M0Error,
    file_sha256,
    load_json,
    require_unlock,
    semantic_sha256,
    validate_contract,
    validate_runtime,
    validate_session_id,
    write_new_json,
)
from explorations.moe_cycle_simulator.phase7.application.executor.finalize import (  # noqa: E402
    seal_terminal_session,
)
from explorations.moe_cycle_simulator.phase7.application.executor.process_tree import (  # noqa: E402
    ProcessTreeContainment,
    enable_child_subreaper,
)
from explorations.moe_cycle_simulator.phase7.application.executor.authority import (  # noqa: E402
    retain_authority,
    validate_retained_authority,
)
from explorations.moe_cycle_simulator.phase7.application.executor.allocation import (  # noqa: E402
    M0_STAGE_SECONDS,
    require_remaining_budget,
)


ACTIVE_PROCESS: subprocess.Popen[bytes] | None = None
ACTIVE_CONTAINMENT: ProcessTreeContainment | None = None
TERMINATING = False
DRIVER_TERM_GRACE_SECONDS = 60.0
DRIVER_KILL_GRACE_SECONDS = 10.0


class CommandTimeout(M0Error):
    """A bounded command exceeded its prospective frozen deadline."""


class DriverInterrupted(M0Error):
    """The outer driver received an operator or timeout signal."""


def validate_m0_entry_parent(
    application: Path,
    approval: dict[str, Any],
    runtime: dict[str, Any],
) -> None:
    """Reject absent, drifted, or ineligible Gate M evidence before GPU entry."""

    gate_m_parent_path = application / "gate_m_parent_evidence.template.json"
    from explorations.moe_cycle_simulator.phase7.application.executor.gate_m_parent import (
        validate_m0_model_binding,
        validate_parent_file,
    )

    parent = validate_parent_file(
        gate_m_parent_path,
        verify_live=True,
        expected_file_sha256=approval.get("gate_m_parent_evidence_file_sha256"),
    )
    validate_m0_model_binding(parent, runtime)


def terminate_active() -> None:
    global ACTIVE_CONTAINMENT, ACTIVE_PROCESS, TERMINATING
    if TERMINATING:
        return
    TERMINATING = True
    process = ACTIVE_PROCESS
    completed = False
    try:
        if ACTIVE_CONTAINMENT is not None:
            ACTIVE_CONTAINMENT.terminate(
                term_grace_seconds=DRIVER_TERM_GRACE_SECONDS,
                kill_grace_seconds=DRIVER_KILL_GRACE_SECONDS,
            )
        if process is not None:
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                raise M0Error("direct child survived process-tree cleanup")
        completed = True
    finally:
        if completed:
            ACTIVE_PROCESS = None
            ACTIVE_CONTAINMENT = None
        TERMINATING = False


def signal_handler(signum: int, _frame: Any) -> None:
    try:
        terminate_active()
    except Exception as exc:
        raise DriverInterrupted(
            f"driver received signal {signum}; initial cleanup failed: {exc}"
        ) from exc
    raise DriverInterrupted(f"driver received signal {signum}")


def execution_environment(runtime: dict[str, Any]) -> dict[str, str]:
    safe = {
        "PATH",
        "LD_LIBRARY_PATH",
        "CUDA_HOME",
        "VIRTUAL_ENV",
        "LANG",
        "LC_ALL",
        "TZ",
        "MOE_PHASE7_EXECUTION_UNLOCK",
        "MOE_PHASE7_CONTAINER_DIGEST",
    }
    result = {key: value for key, value in os.environ.items() if key in safe}
    forbidden = {"PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE", "PYTHONSTARTUP"}
    if forbidden & set(runtime["command_environment"]):
        raise M0Error("unbound Python import-path environment is forbidden")
    result.update(runtime["command_environment"])
    return result


def run_streamed(
    name: str,
    argv: list[str],
    *,
    cwd: Path,
    logs: Path,
    environment: dict[str, str],
    timeout_seconds: int,
) -> dict[str, Any]:
    global ACTIVE_CONTAINMENT, ACTIVE_PROCESS
    if (
        not argv
        or any(not isinstance(item, str) or not item for item in argv)
        or any(item.startswith("[BLOCKING:") for item in argv)
    ):
        raise M0Error(f"{name} command is unresolved")
    stdout_path = logs / f"{name}.stdout.log"
    stderr_path = logs / f"{name}.stderr.log"
    started = time.monotonic_ns()
    timed_out = False
    cleanup: dict[str, object] = {"status": "NOT_STARTED"}
    with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
        containment = ProcessTreeContainment()
        ACTIVE_CONTAINMENT = containment
        try:
            ACTIVE_PROCESS = subprocess.Popen(
                argv,
                cwd=cwd,
                env=environment,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )
            child_pid = ACTIVE_PROCESS.pid
            containment.attach(child_pid)
            try:
                returncode = ACTIVE_PROCESS.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                returncode = None
        finally:
            cleanup = containment.assert_clean(
                term_grace_seconds=DRIVER_TERM_GRACE_SECONDS,
                kill_grace_seconds=DRIVER_KILL_GRACE_SECONDS,
            )
            if ACTIVE_PROCESS is not None:
                try:
                    returncode = ACTIVE_PROCESS.wait(timeout=1)
                except subprocess.TimeoutExpired as exc:
                    raise M0Error(
                        "direct child survived process-tree final cleanup"
                    ) from exc
            stdout.flush()
            stderr.flush()
            os.fsync(stdout.fileno())
            os.fsync(stderr.fileno())
            ACTIVE_PROCESS = None
            ACTIVE_CONTAINMENT = None
    result = {
        "name": name,
        "argv": argv,
        "argv_sha256": semantic_sha256(argv),
        "child_pid": child_pid,
        "returncode": returncode,
        "timed_out": timed_out,
        "elapsed_monotonic_ns": time.monotonic_ns() - started,
        "stdout_sha256": file_sha256(stdout_path),
        "stderr_sha256": file_sha256(stderr_path),
        "process_tree_cleanup": cleanup,
    }
    if timed_out:
        error = CommandTimeout(
            f"{name} failed: rc={returncode}, timed_out={timed_out}"
        )
        error.command_record = result  # type: ignore[attr-defined]
        raise error
    if returncode != 0:
        error = M0Error(f"{name} failed: rc={returncode}, timed_out={timed_out}")
        error.command_record = result  # type: ignore[attr-defined]
        raise error
    return result


def consume_approval(approval: dict[str, Any]) -> Path:
    registry = Path(approval["used_once_registry_path"])
    if not registry.is_absolute() or registry.name in {"", ".", ".."}:
        raise M0Error("one-shot approval registry path must be absolute")
    parent = registry.parent.resolve(strict=True)
    if parent.is_symlink():
        raise M0Error("approval registry parent cannot be a symlink")
    payload = {
        "schema_version": "moe-simulator-phase7-used-approval-v1",
        "approval_id": approval["approval_id"],
        "approval_token_sha256": approval["approval_token_sha256"],
        "approved_session_id": approval["approved_session_id"],
        "approval_file_sha256": approval["_file_sha256"],
    }
    try:
        descriptor = os.open(
            str(parent / registry.name),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o400,
        )
    except FileExistsError as exc:
        raise M0Error("one-shot execution approval was already consumed") from exc
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    return parent / registry.name


def _error_record(label: str, exc: BaseException) -> dict[str, str]:
    return {
        "stage": label,
        "error_type": type(exc).__name__,
        "error": str(exc),
    }


def seal_driver_failure(
    *,
    session: Path,
    session_id: str,
    original_error: Exception,
    commands: list[dict[str, Any]],
    authority_evidence: dict[str, Any] | None,
) -> str:
    """Aggregate cleanup/authority errors before publishing immutable failure."""

    cleanup_errors: list[dict[str, str]] = []
    cleanup_completed = False
    for attempt in (1, 2):
        try:
            terminate_active()
            cleanup_completed = True
            break
        except Exception as cleanup_exc:
            cleanup_errors.append(
                _error_record(f"process_tree_cleanup_attempt_{attempt}", cleanup_exc)
            )

    authority_errors: list[dict[str, str]] = []
    if authority_evidence is None and (session / "authority").exists():
        try:
            authority_evidence = validate_retained_authority(
                evidence_root=session,
                require_package_match=False,
            )
        except Exception as authority_exc:
            authority_errors.append(
                _error_record("retained_authority_revalidation", authority_exc)
            )

    failed_command = getattr(original_error, "command_record", None)
    if failed_command is not None:
        commands.append(failed_command)
    outcome = (
        "INCOMPLETE"
        if (
            isinstance(original_error, (CommandTimeout, DriverInterrupted))
            or not cleanup_completed
        )
        else "FAIL"
    )
    failure = {
        "schema_version": "moe-simulator-phase7-m0-driver-failure-v1",
        "session_id": session_id,
        "status": f"{outcome}_IMMUTABLE",
        "terminal_outcome": outcome,
        "failure": str(original_error),
        "failure_type": type(original_error).__name__,
        "completed_commands": commands,
        "cleanup": {
            "completed": cleanup_completed,
            "errors": cleanup_errors,
        },
        "authority_validation_errors": authority_errors,
        "authority_evidence_sha256": (
            semantic_sha256(authority_evidence)
            if authority_evidence is not None
            else None
        ),
        "resume_allowed": False,
        "retry_allowed": False,
    }
    write_new_json(session / "driver_failure.json", failure)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    seal_terminal_session(session, session_id, outcome)
    return outcome


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--application-dir", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    stage_started = time.monotonic()
    application = args.application_dir.resolve(strict=True)
    output_root = args.output_root.resolve(strict=True)
    if not output_root.is_dir() or output_root.is_symlink():
        raise M0Error("output root must be an existing real directory")
    contract_path = application / "m0_execution_contract.json"
    runtime_path = application / "runtime_variant.template.json"
    approval_path = application / "approval.template.json"
    contract = load_json(contract_path)
    runtime = load_json(runtime_path)
    approval = load_json(approval_path)
    approval["_file_sha256"] = file_sha256(approval_path)
    validate_contract(contract)
    require_unlock(contract)
    validate_runtime(runtime, contract)
    validate_m0_entry_parent(application, approval, runtime)
    session_id = approval["approved_session_id"]
    validate_session_id(session_id)

    validator = subprocess.run(
        [
            sys.executable,
            str(application / "validate_application.py"),
            "--mode",
            "execution-ready",
            "--application-dir",
            str(application),
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if validator.returncode != 0:
        raise M0Error(f"execution-ready validation failed: {validator.stderr.strip()}")
    require_remaining_budget(
        approval["allocation_window"],
        stage_outer_seconds=M0_STAGE_SECONDS,
        downstream_reserve_seconds=0,
    )
    session = output_root / session_id
    try:
        session.mkdir(mode=0o700, exist_ok=False)
    except FileExistsError as exc:
        raise M0Error("fresh session already exists; resume/retry is forbidden") from exc
    logs = session / "logs"
    logs.mkdir(mode=0o700)
    commands: list[dict[str, Any]] = []
    environment = execution_environment(runtime)
    deadline = stage_started + contract["timeouts"]["m0_work_seconds"]
    enable_child_subreaper()
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    authority_evidence: dict[str, Any] | None = None
    try:
        registry_path = consume_approval(approval)
        authority_evidence = retain_authority(
            application=application,
            approval_path=approval_path,
            registry_path=registry_path,
            evidence_root=session,
            expected_application_ledger_sha256=approval[
                "application_ledger_sha256"
            ],
        )
        preflight = [
            sys.executable,
            str(Path(__file__).with_name("preflight.py").resolve(strict=True)),
            "--mode",
            "execution",
            "--application-dir",
            str(application),
            "--output",
            str(session / "preflight_evidence.json"),
        ]
        commands.append(
            run_streamed(
                "preflight",
                preflight,
                cwd=application,
                logs=logs,
                environment=environment,
                timeout_seconds=min(600, max(1, int(deadline - time.monotonic()))),
            )
        )
        commands.append(
            run_streamed(
                "qualification",
                runtime["commands"]["qualification_command_argv"],
                cwd=application,
                logs=logs,
                environment=environment,
                timeout_seconds=max(1, int(deadline - time.monotonic())),
            )
        )
        commands.append(
            run_streamed(
                "audit",
                runtime["commands"]["audit_command_argv"],
                cwd=application,
                logs=logs,
                environment=environment,
                timeout_seconds=min(
                    contract["timeouts"]["audit_seconds"],
                    max(1, int(deadline - time.monotonic())),
                ),
            )
        )
        result = load_json(session / "m0_result.json")
        if (
            result.get("session_id") != session_id
            or result.get("verdict") != "PASS"
            or result.get("findings") != []
        ):
            raise M0Error("audit did not produce the exact M0 PASS result")
        write_new_json(
            session / "driver_commands.json",
            {
                "commands": commands,
                "authority_evidence_sha256": semantic_sha256(authority_evidence),
                "m0_result_file_sha256": file_sha256(session / "m0_result.json"),
            },
        )
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        seal_terminal_session(session, session_id, "PASS")
        return 0
    except Exception as exc:
        try:
            seal_driver_failure(
                session=session,
                session_id=session_id,
                original_error=exc,
                commands=commands,
                authority_evidence=authority_evidence,
            )
        except Exception as sealing_exc:
            raise M0Error(
                f"{exc}; terminal evidence sealing also failed: {sealing_exc}"
            ) from sealing_exc
        if isinstance(exc, M0Error):
            raise
        raise M0Error(str(exc)) from exc


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except M0Error as exc:
        raise SystemExit(f"HARD-STOP: {exc}") from exc

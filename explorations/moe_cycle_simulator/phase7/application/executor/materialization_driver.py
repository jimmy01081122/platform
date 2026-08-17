#!/usr/bin/env python3
"""Execute one approved environment-preflight and pinned materialization stage."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import sys
import time
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[5]))

from explorations.moe_cycle_simulator.phase7.application.executor.common import (  # noqa: E402
    M0Error,
    exact_regular_file_set,
    file_sha256,
    load_json,
    require_materialization_unlock,
    semantic_sha256,
    validate_contract,
    validate_fresh_target,
    validate_materialization_plan,
    verify_model_ledger,
    write_new_json,
)
from explorations.moe_cycle_simulator.phase7.application.executor.driver import (  # noqa: E402
    run_streamed,
    signal_handler,
)
from explorations.moe_cycle_simulator.phase7.application.executor.authority import (  # noqa: E402
    retain_authority,
    validate_retained_authority,
)
from explorations.moe_cycle_simulator.phase7.application.executor.allocation import (  # noqa: E402
    M0_STAGE_SECONDS,
    MATERIALIZATION_STAGE_SECONDS,
    MATERIALIZATION_WORK_SECONDS,
    require_remaining_budget,
)
from explorations.moe_cycle_simulator.phase7.application.executor.process_tree import (  # noqa: E402
    enable_child_subreaper,
)
from explorations.moe_cycle_simulator.phase7.application.executor.deployment_bundle import (  # noqa: E402
    _rename_noreplace,
)


MATERIALIZATION_STATUS = {
    "COMPLETE_HARD_STOP": "MATERIALIZATION_COMPLETE_HARD_STOP\n",
    "FAIL_OR_INCOMPLETE_IMMUTABLE": (
        "MATERIALIZATION_FAIL_OR_INCOMPLETE_IMMUTABLE_NO_RETRY\n"
    ),
}


def _remaining_work_seconds(work_deadline: float, ceiling: int, label: str) -> int:
    remaining = int(work_deadline - time.monotonic())
    if remaining <= 0:
        raise M0Error(f"materialization work deadline expired before {label}")
    return min(ceiling, remaining)


def consume_approval(approval: dict) -> Path:
    registry = Path(approval["used_once_registry_path"])
    if not registry.is_absolute() or registry.name in {"", ".", ".."}:
        raise M0Error("materialization approval registry path must be absolute")
    parent = registry.parent.resolve(strict=True)
    if parent != registry.parent or parent.is_symlink():
        raise M0Error("materialization approval registry parent is unsafe")
    registry = parent / registry.name
    payload = {
        "schema_version": "moe-simulator-phase7-used-materialization-approval-v1",
        "approval_id": approval["approval_id"],
        "approval_token_sha256": approval["approval_token_sha256"],
        "approval_file_sha256": approval["_file_sha256"],
    }
    try:
        descriptor = os.open(
            str(parent / registry.name),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o400,
        )
    except FileExistsError as exc:
        raise M0Error("materialization approval was already consumed") from exc
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    _fsync_directory(parent)
    return registry


def seal_tree(root: Path) -> None:
    if not root.exists() or root.is_symlink():
        return
    if root.is_file():
        root.chmod(0o444)
        return
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_symlink():
            raise M0Error(f"materialization evidence symlink is forbidden: {path}")
        if path.is_file():
            path.chmod(0o444)
        elif path.is_dir():
            path.chmod(0o555)
    root.chmod(0o555)


def build_stage_ledger(root: Path, terminal_status: str) -> dict:
    if terminal_status not in MATERIALIZATION_STATUS:
        raise M0Error("invalid materialization terminal status")
    members = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise M0Error(f"materialization evidence symlink is forbidden: {path}")
        relative = path.relative_to(root).as_posix()
        if relative in {
            "evidence_ledger.json",
            "materialization_status.txt",
            ".evidence_ledger.json.staged",
            ".materialization_status.txt.staged",
        }:
            if relative in {"evidence_ledger.json", "materialization_status.txt"}:
                raise M0Error("materialization terminal artifact exists before sealing")
            raise M0Error(f"stale materialization staging artifact: {relative}")
        if not path.is_file():
            continue
        members.append(
            {
                "path": relative,
                "size_bytes": path.stat().st_size,
                "sha256": file_sha256(path),
            }
        )
    marker = MATERIALIZATION_STATUS[terminal_status].encode("utf-8")
    members.append(
        {
            "path": "materialization_status.txt",
            "size_bytes": len(marker),
            "sha256": hashlib.sha256(marker).hexdigest(),
        }
    )
    members.sort(key=lambda item: item["path"])
    if not members:
        raise M0Error("materialization evidence ledger is empty")
    ledger = {
        "schema_version": "moe-simulator-phase7-materialization-evidence-ledger-v2",
        "terminal_status": terminal_status,
        "terminal_marker": MATERIALIZATION_STATUS[terminal_status].strip(),
        "member_count": len(members),
        "members": members,
    }
    ledger["ledger_sha256"] = semantic_sha256(ledger)
    return ledger


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_new_bytes(path: Path, payload: bytes) -> None:
    descriptor = os.open(
        str(path),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o400,
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _remove_if_present(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _validate_materialization_terminal(root: Path, terminal_status: str) -> dict:
    terminal_name = (
        "stage_result.json"
        if terminal_status == "COMPLETE_HARD_STOP"
        else "stage_failure.json"
    )
    opposite = (
        "stage_failure.json"
        if terminal_status == "COMPLETE_HARD_STOP"
        else "stage_result.json"
    )
    if (root / opposite).exists():
        raise M0Error("materialization success/failure records are mutually exclusive")
    terminal = load_json(root / terminal_name)
    if terminal.get("status") != terminal_status:
        raise M0Error("materialization terminal record/status mismatch")
    if terminal.get("gpu_workload_performed") is not False:
        raise M0Error("materialization terminal record claims a GPU workload")
    if terminal_status != "COMPLETE_HARD_STOP" and (
        terminal.get("resume_allowed") is not False
        or terminal.get("retry_allowed") is not False
    ):
        raise M0Error("materialization failure permits retry or resume")
    authority_dir = root / "authority"
    if terminal_status == "COMPLETE_HARD_STOP" and not authority_dir.exists():
        raise M0Error("materialization success requires retained authority")
    if authority_dir.exists():
        retained = validate_retained_authority(
            evidence_root=root,
            require_package_match=terminal_status == "COMPLETE_HARD_STOP",
        )
        if terminal.get("authority_evidence_sha256") != semantic_sha256(retained):
            raise M0Error("materialization terminal record does not bind authority")
    elif terminal.get("authority_evidence_sha256") is not None:
        raise M0Error("materialization terminal record names absent authority")
    return terminal


def verify_materialization_terminal(root: Path) -> dict:
    root = root.resolve(strict=True)
    ledger = load_json(root / "evidence_ledger.json")
    if set(ledger) != {
        "schema_version",
        "terminal_status",
        "terminal_marker",
        "member_count",
        "members",
        "ledger_sha256",
    }:
        raise M0Error("materialization ledger key closure mismatch")
    digest_input = dict(ledger)
    claimed = digest_input.pop("ledger_sha256", None)
    status = ledger.get("terminal_status")
    if (
        ledger.get("schema_version")
        != "moe-simulator-phase7-materialization-evidence-ledger-v2"
        or status not in MATERIALIZATION_STATUS
        or ledger.get("terminal_marker") != MATERIALIZATION_STATUS[status].strip()
        or claimed != semantic_sha256(digest_input)
    ):
        raise M0Error("materialization ledger identity or digest differs")
    members = ledger.get("members")
    if not isinstance(members, list):
        raise M0Error("materialization ledger members are not a list")
    paths = [item.get("path") for item in members if isinstance(item, dict)]
    if (
        len(paths) != len(members)
        or ledger.get("member_count") != len(members)
        or paths != sorted(paths)
        or len(paths) != len(set(paths))
        or "evidence_ledger.json" in paths
        or "materialization_status.txt" not in paths
    ):
        raise M0Error("materialization ledger member closure differs")
    actual = exact_regular_file_set(
        root, excluded_root_files={"evidence_ledger.json"}
    )
    if set(paths) != actual:
        raise M0Error("materialization ledger is not an exact file set")
    for item in members:
        if set(item) != {"path", "size_bytes", "sha256"}:
            raise M0Error("materialization ledger member keys differ")
        path = root / item["path"]
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size != item["size_bytes"]
            or file_sha256(path) != item["sha256"]
            or path.stat().st_mode & 0o222
        ):
            raise M0Error(
                f"materialization member differs or is writable: {item['path']}"
            )
    for path in (root, *root.rglob("*")):
        if path.is_dir() and path.stat().st_mode & 0o222:
            raise M0Error(f"materialization directory remains writable: {path}")
    marker = (root / "materialization_status.txt").read_text(encoding="utf-8")
    if marker != MATERIALIZATION_STATUS[status]:
        raise M0Error("materialization terminal marker differs")
    _validate_materialization_terminal(root, status)
    return ledger


def seal_materialization_terminal(root: Path, terminal_status: str) -> dict:
    root = root.resolve(strict=True)
    _validate_materialization_terminal(root, terminal_status)
    ledger = build_stage_ledger(root, terminal_status)
    staged_ledger = root / ".evidence_ledger.json.staged"
    staged_status = root / ".materialization_status.txt.staged"
    final_ledger = root / "evidence_ledger.json"
    final_status = root / "materialization_status.txt"
    ledger_payload = (
        json.dumps(ledger, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2)
        + "\n"
    ).encode("utf-8")
    try:
        _write_new_bytes(staged_ledger, ledger_payload)
        _write_new_bytes(
            staged_status, MATERIALIZATION_STATUS[terminal_status].encode("utf-8")
        )
        _fsync_directory(root)
        for path in sorted(root.rglob("*"), reverse=True):
            if path in {staged_ledger, staged_status}:
                continue
            if path.is_symlink():
                raise M0Error(f"materialization evidence symlink is forbidden: {path}")
            path.chmod(0o444 if path.is_file() else 0o555)
        _rename_noreplace(staged_ledger, final_ledger)
        _fsync_directory(root)
        _rename_noreplace(staged_status, final_status)
        _fsync_directory(root)
        root.chmod(0o555)
    except Exception:
        root.chmod(0o700)
        for path in (staged_ledger, staged_status, final_ledger, final_status):
            _remove_if_present(path)
        _fsync_directory(root)
        raise
    verify_materialization_terminal(root)
    return ledger


def _sealing_error(stage: str, exc: BaseException) -> dict[str, str]:
    return {
        "stage": stage,
        "error_type": type(exc).__name__,
        "error": str(exc),
    }


def seal_materialization_failure(
    *,
    root: Path,
    snapshot: Path,
    failure: dict,
) -> None:
    """Attempt every failure artifact and report, rather than swallow, seal errors."""

    sealing_errors: list[dict[str, str]] = []
    failure_path = root / "stage_failure.json"
    ledger_path = root / "evidence_ledger.json"
    try:
        authority_dir = root / "authority"
        if authority_dir.exists():
            retained = validate_retained_authority(
                evidence_root=root,
                require_package_match=False,
            )
            if failure.get("authority_evidence_sha256") != semantic_sha256(retained):
                raise M0Error(
                    "materialization failure does not bind retained authority"
                )
        elif failure.get("authority_evidence_sha256") is not None:
            raise M0Error("materialization failure names absent authority")
    except Exception as exc:
        sealing_errors.append(
            _sealing_error("revalidate_retained_authority", exc)
        )
    try:
        write_new_json(failure_path, failure)
    except Exception as exc:
        sealing_errors.append(_sealing_error("write_stage_failure", exc))
    try:
        seal_tree(snapshot)
    except Exception as exc:
        sealing_errors.append(_sealing_error("seal_materialized_snapshot", exc))
    try:
        if not sealing_errors:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            seal_materialization_terminal(
                root, "FAIL_OR_INCOMPLETE_IMMUTABLE"
            )
    except Exception as exc:
        sealing_errors.append(_sealing_error("seal_materialization_terminal", exc))

    if not sealing_errors:
        return

    # A failed sealing attempt is not an immutable terminal artifact. Remove any
    # stale ledger claim and retain a machine-readable account when possible.
    recovery_errors: list[dict[str, str]] = []
    try:
        root.chmod(0o700)
        if ledger_path.exists():
            ledger_path.unlink()
    except Exception as exc:
        recovery_errors.append(_sealing_error("remove_stale_failure_ledger", exc))
    error_record = {
        "schema_version": "moe-simulator-phase7-materialization-sealing-failure-v1",
        "status": "SEALING_FAILED_NOT_IMMUTABLE",
        "sealing_errors": sealing_errors,
        "recovery_errors": recovery_errors,
        "resume_allowed": False,
        "retry_allowed": False,
    }
    try:
        write_new_json(root / "sealing_failure.json", error_record)
    except Exception as exc:
        recovery_errors.append(_sealing_error("write_sealing_failure_record", exc))
    raise M0Error(
        "materialization failure evidence sealing failed: "
        + json.dumps(
            {
                "sealing_errors": sealing_errors,
                "recovery_errors": recovery_errors,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--application-dir", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument(
        "--work-seconds",
        type=int,
        default=MATERIALIZATION_WORK_SECONDS,
        help=(
            "bounded inner work deadline; may only reduce the frozen "
            "materialization work allowance"
        ),
    )
    args = parser.parse_args()
    if (
        isinstance(args.work_seconds, bool)
        or args.work_seconds <= 0
        or args.work_seconds > MATERIALIZATION_WORK_SECONDS
    ):
        raise M0Error(
            "materialization work seconds must be positive and must not exceed "
            "the frozen allowance"
        )
    work_deadline = time.monotonic() + args.work_seconds
    application = args.application_dir.resolve(strict=True)
    contract_path = application / "m0_execution_contract.json"
    plan_path = application / "materialization_plan.template.json"
    approval_path = application / "materialization_approval.template.json"
    contract = load_json(contract_path)
    plan = load_json(plan_path)
    approval = load_json(approval_path)
    approval["_file_sha256"] = file_sha256(approval_path)
    validate_contract(contract)
    require_materialization_unlock(contract)
    validate_materialization_plan(plan, contract)
    for label, value in plan["paths"].items():
        validate_fresh_target(Path(value), label)
    require_remaining_budget(
        approval["allocation_window"],
        stage_outer_seconds=MATERIALIZATION_STAGE_SECONDS,
        downstream_reserve_seconds=M0_STAGE_SECONDS,
    )
    root = args.evidence_root
    if not root.is_absolute():
        raise M0Error("materialization evidence root must be absolute")
    parent = root.parent.resolve(strict=True)
    if parent.is_symlink():
        raise M0Error("materialization evidence parent must not be a symlink")
    root = parent / root.name
    root.mkdir(exist_ok=False)
    logs = root / "logs"
    logs.mkdir()
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    environment = {
        key: value
        for key, value in os.environ.items()
        if key
        in {
            "PATH",
            "PYTHONPATH",
            "LD_LIBRARY_PATH",
            "CUDA_HOME",
            "VIRTUAL_ENV",
            "LANG",
            "LC_ALL",
            "TZ",
            "MOE_PHASE7_MATERIALIZATION_UNLOCK",
            "MOE_PHASE7_CONTAINER_DIGEST",
        }
    }
    commands = []
    authority_evidence: dict | None = None
    enable_child_subreaper()
    try:
        registry_path = consume_approval(approval)
        authority_evidence = retain_authority(
            application=application,
            approval_path=approval_path,
            registry_path=registry_path,
            evidence_root=root,
            expected_application_ledger_sha256=approval[
                "application_ledger_sha256"
            ],
        )
        commands.append(
            run_streamed(
                "application_validation",
                [
                    sys.executable,
                    str(application / "validate_application.py"),
                    "--mode",
                    "materialization-ready",
                    "--application-dir",
                    str(application),
                ],
                cwd=application,
                logs=logs,
                environment=environment,
                timeout_seconds=_remaining_work_seconds(
                    work_deadline, 300, "application validation"
                ),
            )
        )
        commands.append(
            run_streamed(
                "preflight",
                [
                    sys.executable,
                    str(Path(__file__).with_name("preflight.py").resolve(strict=True)),
                    "--mode",
                    "materialization",
                    "--application-dir",
                    str(application),
                    "--output",
                    str(root / "preflight_evidence.json"),
                ],
                cwd=application,
                logs=logs,
                environment=environment,
                timeout_seconds=_remaining_work_seconds(
                    work_deadline, 300, "preflight"
                ),
            )
        )
        commands.append(
            run_streamed(
                "materialize",
                plan["command_argv"],
                cwd=application,
                logs=logs,
                environment=environment,
                timeout_seconds=_remaining_work_seconds(
                    work_deadline,
                    contract["timeouts"]["materialization_seconds"],
                    "materialization",
                ),
            )
        )
        commands.append(
            run_streamed(
                "prompt_fixture",
                plan["prompt_fixture_command_argv"],
                cwd=application,
                logs=logs,
                environment=environment,
                timeout_seconds=_remaining_work_seconds(
                    work_deadline, 300, "prompt fixture"
                ),
            )
        )
        result_path = Path(plan["paths"]["materialization_result"]).resolve(strict=True)
        ledger_path = Path(plan["paths"]["model_ledger"]).resolve(strict=True)
        fixture_path = Path(plan["paths"]["capacity_prompt_fixture"]).resolve(
            strict=True
        )
        snapshot = Path(plan["paths"]["snapshot"]).resolve(strict=True)
        result = load_json(result_path)
        ledger = load_json(ledger_path)
        fixture = load_json(fixture_path)
        verify_model_ledger(snapshot, ledger, contract=contract)
        if (
            result.get("status") != "COMPLETE"
            or result.get("model_ledger_sha256") != ledger["ledger_sha256"]
            or result.get("gpu_workload_performed") is not False
            or fixture.get("model_ledger_sha256") != ledger["ledger_sha256"]
            or fixture.get("token_count") != contract["probe"]["input_tokens"]
            or fixture.get("token_ids_sha256")
            != semantic_sha256(fixture.get("token_ids"))
        ):
            raise M0Error("materialization result/ledger binding mismatch")
        authority_evidence = validate_retained_authority(
            evidence_root=root,
            require_package_match=True,
        )
        stage_result = {
            "schema_version": "moe-simulator-phase7-materialization-stage-result-v1",
            "status": "COMPLETE_HARD_STOP",
            "application_ledger_sha256": approval["application_ledger_sha256"],
            "authority_evidence_sha256": semantic_sha256(authority_evidence),
            "materialization_plan_sha256": file_sha256(plan_path),
            "materialization_result_sha256": file_sha256(result_path),
            "model_ledger_sha256": ledger["ledger_sha256"],
            "capacity_prompt_fixture_sha256": file_sha256(fixture_path),
            "capacity_prompt_token_ids_sha256": fixture["token_ids_sha256"],
            "commands": commands,
            "gpu_workload_performed": False,
            "next_legal_action": "FREEZE_RUNTIME_AND_REQUEST_SEPARATE_EXACT_M0_EXECUTION_APPROVAL",
        }
        write_new_json(root / "stage_result.json", stage_result)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        for path in (snapshot, result_path, ledger_path, fixture_path):
            seal_tree(path)
        seal_materialization_terminal(root, "COMPLETE_HARD_STOP")
        print(semantic_sha256(stage_result))
        return 0
    except Exception as exc:
        failed_command = getattr(exc, "command_record", None)
        if failed_command is not None:
            commands.append(failed_command)
        authority_validation_errors: list[dict[str, str]] = []
        if authority_evidence is None and (root / "authority").exists():
            try:
                authority_evidence = validate_retained_authority(
                    evidence_root=root,
                    require_package_match=False,
                )
            except Exception as authority_exc:
                authority_validation_errors.append(
                    _sealing_error(
                        "retained_authority_revalidation",
                        authority_exc,
                    )
                )
        failure = {
            "schema_version": "moe-simulator-phase7-materialization-stage-failure-v1",
            "status": "FAIL_OR_INCOMPLETE_IMMUTABLE",
            "failure": str(exc),
            "completed_commands": commands,
            "authority_evidence_sha256": (
                semantic_sha256(authority_evidence)
                if authority_evidence is not None
                else None
            ),
            "authority_validation_errors": authority_validation_errors,
            "gpu_workload_performed": False,
            "retry_allowed": False,
            "resume_allowed": False,
        }
        try:
            seal_materialization_failure(
                root=root,
                snapshot=Path(plan["paths"]["snapshot"]),
                failure=failure,
            )
        except Exception as sealing_exc:
            raise M0Error(f"{exc}; {sealing_exc}") from sealing_exc
        if isinstance(exc, M0Error):
            raise
        raise M0Error(str(exc)) from exc


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except M0Error as exc:
        raise SystemExit(f"HARD-STOP: {exc}") from exc

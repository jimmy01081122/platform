#!/usr/bin/env python3
"""Supervise three independent, fail-closed M0 vLLM process launches."""

from __future__ import annotations

import argparse
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

from explorations.moe_cycle_simulator.phase7.application.executor.common import (  # noqa: E402
    M0Error,
    file_sha256,
    load_json,
    require_unlock,
    semantic_sha256,
    validate_contract,
    validate_probe_record,
    validate_runtime,
    validate_session_id,
    verify_model_ledger,
    write_new_json,
)
from explorations.moe_cycle_simulator.phase7.application.executor.process_tree import (  # noqa: E402
    ProcessTreeContainment,
    enable_child_subreaper,
)


ACTIVE_CHILD: subprocess.Popen[bytes] | None = None
ACTIVE_CONTAINMENT: ProcessTreeContainment | None = None
TERMINATING = False


def terminate_active_child() -> None:
    global ACTIVE_CHILD, ACTIVE_CONTAINMENT, TERMINATING
    if TERMINATING:
        return
    TERMINATING = True
    try:
        if ACTIVE_CONTAINMENT is not None:
            ACTIVE_CONTAINMENT.terminate()
        if ACTIVE_CHILD is not None:
            try:
                ACTIVE_CHILD.wait(timeout=1)
            except subprocess.TimeoutExpired as exc:
                raise M0Error("vLLM child survived process-tree cleanup") from exc
    finally:
        TERMINATING = False


def signal_handler(signum: int, _frame: Any) -> None:
    terminate_active_child()
    raise M0Error(f"qualification received signal {signum}")


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def gpu_used_bytes() -> int:
    completed = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=memory.used",
            "--format=csv,noheader,nounits",
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    rows = [item.strip() for item in completed.stdout.splitlines() if item.strip()]
    if completed.returncode != 0 or len(rows) != 1:
        raise M0Error("cannot capture single-GPU memory baseline")
    try:
        return int(rows[0]) * 1024 * 1024
    except ValueError as exc:
        raise M0Error("invalid GPU memory baseline") from exc


def compute_processes() -> list[dict[str, Any]]:
    completed = subprocess.run(
        [
            "nvidia-smi",
            "--query-compute-apps=pid,gpu_uuid,used_gpu_memory",
            "--format=csv,noheader,nounits",
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode != 0:
        raise M0Error("cannot query residual GPU processes")
    records = []
    for row in completed.stdout.splitlines():
        if not row.strip():
            continue
        fields = [item.strip() for item in row.split(",")]
        if len(fields) != 3:
            raise M0Error("unexpected compute-process evidence")
        records.append(
            {
                "pid": int(fields[0]),
                "gpu_uuid": fields[1],
                "used_gpu_memory_bytes": int(fields[2]) * 1024 * 1024,
            }
        )
    return records


def child_environment(runtime: dict[str, Any]) -> dict[str, str]:
    safe_names = {
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
    environment = {
        key: value for key, value in os.environ.items() if key in safe_names
    }
    forbidden = {"PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE", "PYTHONSTARTUP"}
    if forbidden & set(runtime["command_environment"]):
        raise M0Error("unbound Python import-path environment is forbidden")
    for key, value in runtime["command_environment"].items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise M0Error("command environment must contain strings")
        if any(marker in key.upper() for marker in ("TOKEN", "SECRET", "PASSWORD", "KEY")):
            raise M0Error(f"secret-like command environment key is forbidden: {key}")
        environment[key] = value
    required = {
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_DATASETS_OFFLINE": "1",
        "CUDA_VISIBLE_DEVICES": "0",
        "PYTHONNOUSERSITE": "1",
    }
    if any(environment.get(key) != value for key, value in required.items()):
        raise M0Error("qualification offline/CUDA environment is not frozen")
    return environment


def isolated_python_command(script: Path, arguments: list[str]) -> list[str]:
    resolved = script.resolve(strict=True)
    if not resolved.is_file() or resolved.is_symlink():
        raise M0Error("isolated Python entrypoint is unsafe")
    if any(not isinstance(item, str) or not item for item in arguments):
        raise M0Error("isolated Python arguments are invalid")
    return [sys.executable, "-I", str(resolved), *arguments]


def wait_for_cleanup(baseline: int, tolerance: int) -> dict[str, Any]:
    deadline = time.monotonic() + 30
    last_processes: list[dict[str, Any]] = []
    last_used = -1
    while time.monotonic() < deadline:
        last_processes = compute_processes()
        last_used = gpu_used_bytes()
        if not last_processes and last_used <= baseline + tolerance:
            return {
                "status": "PASS",
                "residual_processes": [],
                "baseline_used_memory_bytes": baseline,
                "post_cleanup_used_memory_bytes": last_used,
                "tolerance_bytes": tolerance,
            }
        time.sleep(1)
    raise M0Error(
        "vLLM cleanup did not return to the frozen boundary; "
        f"residual={last_processes}, used={last_used}, baseline={baseline}"
    )


def run_child(
    argv: list[str],
    *,
    environment: dict[str, str],
    stdout_path: Path,
    stderr_path: Path,
    timeout_seconds: int,
) -> tuple[int, int, bool, dict[str, object]]:
    global ACTIVE_CHILD, ACTIVE_CONTAINMENT
    started = time.monotonic_ns()
    timed_out = False
    cleanup: dict[str, object] = {"status": "NOT_STARTED"}
    with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
        containment = ProcessTreeContainment()
        ACTIVE_CONTAINMENT = containment
        try:
            ACTIVE_CHILD = subprocess.Popen(
                argv,
                env=environment,
                stdout=stdout,
                stderr=stderr,
                # The qualification process already owns the isolated session
                # created by driver.run_streamed. Keeping the launch in that
                # process group gives the outer driver a second containment
                # boundary; daemonized workers are still caught by subreaping.
                start_new_session=False,
            )
            pid = ACTIVE_CHILD.pid
            containment.attach(pid)
            try:
                returncode = ACTIVE_CHILD.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                returncode = None
        finally:
            cleanup = containment.assert_clean()
            if ACTIVE_CHILD is not None:
                try:
                    returncode = ACTIVE_CHILD.wait(timeout=1)
                except subprocess.TimeoutExpired as exc:
                    raise M0Error(
                        "direct vLLM child survived process-tree final cleanup"
                    ) from exc
            stdout.flush()
            stderr.flush()
            os.fsync(stdout.fileno())
            os.fsync(stderr.fileno())
            ACTIVE_CHILD = None
            ACTIVE_CONTAINMENT = None
    return returncode, pid, timed_out, cleanup


def validate_backend_log_evidence(
    stdout_path: Path, stderr_path: Path, runtime: dict[str, Any]
) -> dict[str, Any]:
    try:
        combined = (
            stdout_path.read_bytes() + b"\n" + stderr_path.read_bytes()
        ).decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise M0Error("vLLM startup logs are not strict UTF-8") from exc
    markers = runtime["backend_evidence_contract"]["required_utf8_markers"]
    missing = [name for name, marker in markers.items() if marker not in combined]
    if missing:
        raise M0Error(f"required resolved-backend log evidence is absent: {missing}")
    return {
        "source": runtime["backend_evidence_contract"]["source"],
        "required_utf8_markers": markers,
        "all_markers_observed": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--model-ledger", type=Path, required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    contract = load_json(args.contract)
    runtime = load_json(args.runtime)
    model_ledger = load_json(args.model_ledger)
    validate_contract(contract)
    require_unlock(contract)
    validate_runtime(runtime, contract)
    if runtime["model"]["model_file_ledger_sha256"] != model_ledger.get(
        "ledger_sha256"
    ):
        raise M0Error("runtime does not bind the supplied model ledger")
    validate_session_id(args.session_id)
    snapshot = Path(runtime["model"]["local_snapshot_path"]).resolve(strict=True)
    verify_model_ledger(snapshot, model_ledger, contract=contract)
    args.output.mkdir(parents=True, exist_ok=False)

    contract_hash = file_sha256(args.contract)
    runtime_hash = file_sha256(args.runtime)
    environment = child_environment(runtime)
    environment_hash = semantic_sha256(environment)
    launch_results: list[dict[str, Any]] = []
    enable_child_subreaper()
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    try:
        if compute_processes():
            raise M0Error("pre-existing GPU compute process violates exclusive M0")
        for index in range(1, contract["probe"]["repetitions"] + 1):
            verify_model_ledger(snapshot, model_ledger, contract=contract)
            launch_dir = args.output / f"launch-{index}"
            launch_dir.mkdir(exist_ok=False)
            probe_path = launch_dir / "probe.json"
            stdout_path = launch_dir / "stdout.log"
            stderr_path = launch_dir / "stderr.log"
            baseline = gpu_used_bytes()
            child_argv = isolated_python_command(
                Path(__file__).with_name("single_launch.py"),
                [
                    "--contract",
                    str(args.contract.resolve()),
                    "--runtime",
                    str(args.runtime.resolve()),
                    "--model-ledger",
                    str(args.model_ledger.resolve()),
                    "--session-id",
                    args.session_id,
                    "--launch-index",
                    str(index),
                    "--output",
                    str(probe_path),
                ],
            )
            started_at = utc_now()
            returncode: int | None = None
            child_pid: int | None = None
            timed_out = False
            process_cleanup: dict[str, object] = {"status": "NOT_STARTED"}
            backend_evidence: dict[str, Any] = {
                "all_markers_observed": False,
                "status": "NOT_VALIDATED",
            }
            launch_error: Exception | None = None
            try:
                (
                    returncode,
                    child_pid,
                    timed_out,
                    process_cleanup,
                ) = run_child(
                    child_argv,
                    environment=environment,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                    timeout_seconds=contract["timeouts"]["single_launch_seconds"],
                )
                backend_evidence = validate_backend_log_evidence(
                    stdout_path, stderr_path, runtime
                )
            except Exception as exc:
                launch_error = exc
            finally:
                try:
                    cleanup = wait_for_cleanup(
                        baseline,
                        contract["probe"]["cleanup_memory_tolerance_bytes"],
                    )
                except Exception as cleanup_exc:
                    cleanup = {
                        "status": "FAIL",
                        "failure": str(cleanup_exc),
                    }
                    if launch_error is None:
                        launch_error = cleanup_exc
            result = {
                "launch_index": index,
                "child_pid": child_pid,
                "started_at_utc": started_at,
                "finished_at_utc": utc_now(),
                "returncode": returncode,
                "timed_out": timed_out,
                "argv": child_argv,
                "environment_sha256": environment_hash,
                "stdout_sha256": file_sha256(stdout_path)
                if stdout_path.is_file()
                else None,
                "stderr_sha256": file_sha256(stderr_path)
                if stderr_path.is_file()
                else None,
                "backend_evidence": backend_evidence,
                "process_tree_cleanup": process_cleanup,
                "cleanup": cleanup,
                "probe_path": probe_path.relative_to(args.output).as_posix(),
            }
            launch_results.append(result)
            if launch_error is not None:
                raise launch_error
            if timed_out or returncode != 0 or not probe_path.is_file():
                raise M0Error(
                    f"fresh launch {index} failed: rc={returncode}, timeout={timed_out}"
                )
            record = load_json(probe_path)
            validate_probe_record(
                record,
                contract_sha256=contract_hash,
                runtime_sha256=runtime_hash,
                model_ledger_sha256=model_ledger["ledger_sha256"],
                launch_index=index,
                session_id=args.session_id,
                contract=contract,
            )
        identities = [
            (
                load_json(args.output / item["probe_path"])["process_identity"]["boot_id"],
                load_json(args.output / item["probe_path"])["process_identity"]["start_ticks"],
                load_json(args.output / item["probe_path"])["process_identity"]["nonce"],
            )
            for item in launch_results
        ]
        if len(set(identities)) != contract["probe"]["repetitions"]:
            raise M0Error("fresh launch process identities are not unique")
        summary = {
            "schema_version": "moe-simulator-phase7-m0-qualification-summary-v1",
            "status": "COMPLETE",
            "session_id": args.session_id,
            "contract_sha256": contract_hash,
            "runtime_variant_sha256": runtime_hash,
            "model_ledger_sha256": model_ledger["ledger_sha256"],
            "environment_sha256": environment_hash,
            "launch_count": len(launch_results),
            "launches": launch_results,
            "fresh_process_identity_count": len(set(identities)),
            "retry_used": False,
            "resume_used": False,
        }
        write_new_json(args.output / "qualification_summary.json", summary)
        print(semantic_sha256(summary))
        return 0
    except Exception as exc:
        failure = {
            "schema_version": "moe-simulator-phase7-m0-qualification-failure-v1",
            "status": "FAIL_OR_INCOMPLETE_IMMUTABLE",
            "session_id": args.session_id,
            "failure": str(exc),
            "completed_launches": launch_results,
            "retry_allowed": False,
            "resume_allowed": False,
        }
        write_new_json(args.output / "qualification_failure.json", failure)
        if isinstance(exc, M0Error):
            raise
        raise M0Error(str(exc)) from exc
    finally:
        terminate_active_child()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except M0Error as exc:
        raise SystemExit(f"HARD-STOP: {exc}") from exc

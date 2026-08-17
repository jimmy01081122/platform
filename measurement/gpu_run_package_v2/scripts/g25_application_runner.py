#!/usr/bin/env python3
"""Hash-bound, fail-closed G2.5 local RTX 3050 application dispatcher."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_ROOT.parent
sys.path.insert(0, str(PACKAGE_ROOT))

from adapters.models.granite_moe.snapshot import (  # noqa: E402
    RUNTIME_PAYLOAD_CONTRACT_PATH,
    SnapshotValidationError,
    validate_exact_snapshot,
)
from scheduler.execution_lock import ExecutionLease  # noqa: E402
from scheduler.g25_application import (  # noqa: E402
    ApprovalExpectations,
    load_and_validate_approval,
    load_and_validate_evaluation_record,
    load_and_validate_review_record,
    RUNTIME_INVENTORY_PATH,
    RUNTIME_PYTHON,
    query_dynamic_preflight,
    run_dynamic_preflight,
    verify_runtime_inventory,
)
from scheduler.g25_deadlines import G25DeadlineTracker  # noqa: E402
from scheduler.g25_cgroup_v2 import CgroupV2Controller  # noqa: E402
from scheduler.g25_session import (  # noqa: E402
    G25SessionStore,
    audit_finalized_application,
    audit_partial_session,
    session_file_inventory,
    qualification_evidence_inventory,
    write_external_seal_anchor,
)
from scheduler.g25_snapshot import (  # noqa: E402
    audit_package_snapshot,
    freeze_package_snapshot,
    verify_package_ledger,
)
from scheduler.g25_runtime_closure import (  # noqa: E402
    SYSTEM_CLOSURE_PATH,
    verify_live_loaded_closure,
)
from scheduler.store import atomic_json, fsync_directory  # noqa: E402
from scripts.g25_qualification import (  # noqa: E402
    CONTRACT_PATH,
    PILOT_ARTIFACTS_PATH,
    PILOT_MATRIX_PATH,
    PILOT_SESSION_PATH,
    PROFILE_MAP_PATH,
    SESSION_SCHEMA,
    _manifest_selections,
    audit_session,
    build_cell_row,
    build_ledger,
    build_worker_argv,
    build_worker_descriptor,
    canonical_hash,
    cell_identity,
    classify_worker_evidence,
    invoke_worker_evidence_process,
    load_contract,
    load_profile_map,
    normalize_worker_process_result,
    qualification_ceilings,
    qualification_instances,
    resolve_task_profile,
    select_common_ceiling,
    sha256_file,
    validate_schema,
)

SESSION_ID = "granite-c1a-g25-qualification-r1-20260719"
APPROVAL_SCHEMA_PATH = PACKAGE_ROOT / "schemas/g25_gpu_pilot_approval.schema.json"
REVIEW_SCHEMA_PATH = PACKAGE_ROOT / "schemas/g25_same_source_review.schema.json"
EVALUATION_SCHEMA_PATH = PACKAGE_ROOT / "schemas/g25_5_6sol_evaluation.schema.json"
MODEL_INVENTORY_PATH = (
    PACKAGE_ROOT / "configs/model_snapshots/granite-3.1-1b-a400m-instruct/"
    "0da7a48b0276d500ce5922fd2b33944091fc6c09/inventory.json"
)
MODEL_SNAPSHOT_ROOT = (
    PACKAGE_ROOT / "models/snapshots/granite-3.1-1b-a400m-instruct/"
    "0da7a48b0276d500ce5922fd2b33944091fc6c09"
)
SAMPLE_MANIFEST_PATH = (
    PACKAGE_ROOT / "configs/test_suites/granite_c1/sample_manifest.jsonl"
)
BENCHMARK_QUALITY_PATH = PACKAGE_ROOT / "scripts/benchmark_quality.py"


class ApplicationBlocked(RuntimeError):
    pass


def boottime() -> float:
    return float(time.clock_gettime(time.CLOCK_BOOTTIME))


def exact_application_argv(
    *, approval_record: Path, review_record: Path, review_tag: str,
    evaluation_record: Path, model_snapshot: Path,
) -> list[str]:
    from scheduler.g25_cgroup_v2 import build_systemd_run_argv
    from scheduler.g25_runtime_closure import build_attested_python_argv

    inner = build_attested_python_argv(
        "projectctl",
        [
            "qualification", "start",
            "--approval-record", str(approval_record.resolve()),
            "--review-record", str(review_record.resolve()),
            "--evaluation-record", str(evaluation_record.resolve()),
            "--review-tag", review_tag,
            "--model-snapshot", str(model_snapshot.resolve()),
        ],
        package_root=PACKAGE_ROOT,
        python_executable=RUNTIME_PYTHON,
    )
    environment = [
        f"G25_RUNTIME_ROOT={PACKAGE_ROOT / '.benchmark-runtime'}",
        "PYTHONNOUSERSITE=1", "PYTHONDONTWRITEBYTECODE=1",
        "PATH=/usr/bin:/bin:/usr/lib/wsl/lib",
        "CUDA_VISIBLE_DEVICES=GPU-4d160805-02d8-24aa-ef6a-2685832658a3",
        "HF_HUB_OFFLINE=1", "TRANSFORMERS_OFFLINE=1",
        "CUBLAS_WORKSPACE_CONFIG=:4096:8", "CUDA_LAUNCH_BLOCKING=1",
        "PYTHONHASHSEED=0",
        f"XDG_RUNTIME_DIR=/run/user/{os.getuid()}",
        f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{os.getuid()}/bus",
        "LC_ALL=C", "LANG=C",
    ]
    timeout_argv = [
        "/usr/bin/timeout", "--signal=TERM", "--kill-after=30s", "7500s",
        "/usr/bin/env", "-i", *environment, *inner,
    ]
    return build_systemd_run_argv(timeout_argv)


def _approved_timeout_argv(expected_argv: Sequence[str]) -> list[str]:
    expected = list(expected_argv)
    try:
        separator = expected.index("--")
    except ValueError as error:
        raise ApplicationBlocked("approved command lacks the systemd service separator") from error
    timeout_argv = expected[separator + 1 :]
    if not timeout_argv or timeout_argv[0] != "/usr/bin/timeout":
        raise ApplicationBlocked("approved command lacks the frozen timeout child")
    return timeout_argv


def verify_outer_timeout_parent(expected_argv: Sequence[str]) -> dict[str, Any]:
    """Observe the timeout child while retaining the full approved service argv."""
    try:
        raw = Path(f"/proc/{os.getppid()}/cmdline").read_bytes()
    except OSError as exc:
        raise ApplicationBlocked(f"outer timeout parent cannot be verified: {exc}") from exc
    argv = [item.decode("utf-8", errors="replace") for item in raw.split(b"\0") if item]
    expected = list(expected_argv)
    expected_timeout = _approved_timeout_argv(expected)
    if argv != expected_timeout:
        raise ApplicationBlocked(
            "observed outer process argv differs from the owner-approved exact command"
        )
    try:
        executable = Path(f"/proc/{os.getppid()}/exe").resolve(strict=True)
        expected_executable = Path(expected_timeout[0]).resolve(strict=True)
    except OSError as exc:
        raise ApplicationBlocked(f"outer timeout executable cannot be verified: {exc}") from exc
    if executable != expected_executable:
        raise ApplicationBlocked("observed outer executable differs from approved timeout")
    try:
        raw_stat = Path(f"/proc/{os.getppid()}/stat").read_text(encoding="utf-8")
        closing = raw_stat.rfind(")")
        if closing < 0:
            raise ValueError("process stat command field is malformed")
        fields_after_command = raw_stat[closing + 2 :].split()
        # fields_after_command begins at /proc stat field 3; starttime is field 22.
        start_ticks = int(fields_after_command[19])
        ticks_per_second = int(os.sysconf("SC_CLK_TCK"))
        observed_boottime = float(time.clock_gettime(time.CLOCK_BOOTTIME))
        started_boottime = start_ticks / ticks_per_second
    except (OSError, ValueError, IndexError) as exc:
        raise ApplicationBlocked(f"outer timeout start time cannot be verified: {exc}") from exc
    if (
        ticks_per_second <= 0
        or not 0 <= started_boottime <= observed_boottime
        or observed_boottime - started_boottime >= 7500
    ):
        raise ApplicationBlocked("outer timeout start time is invalid or already expired")
    return {
        "schema_version": "g25-outer-timeout-observation-v2",
        "pid": os.getppid(),
        "executable": str(executable),
        "argv": expected,
        "argv_sha256": canonical_hash(expected),
        "observed_timeout_argv": argv,
        "observed_timeout_argv_sha256": canonical_hash(argv),
        "clock": "CLOCK_BOOTTIME",
        "clock_ticks_per_second": ticks_per_second,
        "start_ticks": start_ticks,
        "started_boottime_seconds": started_boottime,
        "observed_boottime_seconds": observed_boottime,
    }


def _git(args: Sequence[str]) -> str:
    result = subprocess.run(
        ["/usr/bin/git", *args], cwd=REPO_ROOT, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if result.returncode != 0:
        raise ApplicationBlocked(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def resolve_review_target(tag: str) -> dict[str, str]:
    if not tag or _git(["cat-file", "-t", tag]) != "tag":
        raise ApplicationBlocked("G2.5 application review target must be an annotated tag")
    commit = _git(["rev-parse", f"{tag}^{{commit}}"])
    tree = _git(["rev-parse", f"{commit}^{{tree}}"])
    reviewed_package_tree = _git([
        "rev-parse", f"{commit}:gpu_run_package_v2"
    ])
    current_package_tree = _git(["rev-parse", "HEAD:gpu_run_package_v2"])
    if reviewed_package_tree != current_package_tree:
        raise ApplicationBlocked(
            "current GPU package differs from the same-source reviewed package tree"
        )
    if _git(["status", "--porcelain"]):
        raise ApplicationBlocked("G2.5 application requires a clean reviewed worktree")
    return {
        "annotated_tag": tag,
        "tag_object": _git(["rev-parse", tag]),
        "commit": commit,
        "tree": tree,
        "package_tree": reviewed_package_tree,
    }


def package_identity() -> dict[str, Any]:
    rows = verify_package_ledger(PACKAGE_ROOT)
    manifest = json.loads((PACKAGE_ROOT / "package_manifest.json").read_text(encoding="utf-8"))
    inventory = manifest.get("file_inventory") if isinstance(manifest, dict) else None
    if not isinstance(inventory, dict) or not isinstance(inventory.get("files"), list):
        raise ApplicationBlocked("package manifest lacks a valid exact file inventory")
    files = inventory["files"]
    if (
        inventory.get("file_count") != len(files)
        or len(files) != len(set(files))
        or set(row["path"] for row in rows) != set(files) - {"checksums.txt"}
        or "checksums.txt" not in files
    ):
        raise ApplicationBlocked("package manifest and checksum ledger file sets differ")
    return {
        "inventory_count": len(files),
        "checksum_entry_count": len(rows),
        "checksums_sha256": sha256_file(PACKAGE_ROOT / "checksums.txt"),
        "package_manifest_sha256": sha256_file(PACKAGE_ROOT / "package_manifest.json"),
    }


def source_bindings() -> dict[str, str]:
    from scheduler.g25_historical_evidence import (
        verify_historical_evidence_archive,
    )

    identity = package_identity()
    model_snapshot = validate_model_snapshot(MODEL_SNAPSHOT_ROOT)
    historical = verify_historical_evidence_archive()
    bindings = {
        "package_checksum_ledger_sha256": identity["checksums_sha256"],
        "package_manifest_sha256": identity["package_manifest_sha256"],
        "runtime_inventory_sha256": sha256_file(RUNTIME_INVENTORY_PATH),
        "requirements_lock_sha256": sha256_file(PACKAGE_ROOT / "requirements.lock"),
        "pilot_session_contract_sha256": sha256_file(PILOT_SESSION_PATH),
        "matrix_sha256": sha256_file(PILOT_MATRIX_PATH),
        "generation_profile_sha256": sha256_file(PROFILE_MAP_PATH),
        "expected_artifacts_sha256": sha256_file(PILOT_ARTIFACTS_PATH),
        "qualification_runner_sha256": sha256_file(
            PACKAGE_ROOT / "scripts/g25_qualification.py"
        ),
        "qualification_worker_sha256": sha256_file(
            PACKAGE_ROOT / "scripts/g25_worker.py"
        ),
        "isolated_bootstrap_sha256": sha256_file(
            PACKAGE_ROOT / "scripts/g25_isolated_bootstrap.py"
        ),
        "snapshot_inventory_sha256": sha256_file(MODEL_INVENTORY_PATH),
        "model_runtime_payload_contract_sha256": sha256_file(
            RUNTIME_PAYLOAD_CONTRACT_PATH
        ),
        "model_snapshot_verifier_sha256": sha256_file(
            PACKAGE_ROOT / "adapters/models/granite_moe/snapshot.py"
        ),
        "model_snapshot_payload_identity_sha256": model_snapshot[
            "payload_identity_sha256"
        ],
        "model_snapshot_files_inventory_sha256": model_snapshot[
            "files_inventory_sha256"
        ],
        "c1_evaluator_sha256": sha256_file(
            PACKAGE_ROOT / "scripts/c1_evaluator.py"
        ),
        "benchmark_quality_sha256": sha256_file(
            BENCHMARK_QUALITY_PATH
        ),
        "sample_manifest_sha256": sha256_file(SAMPLE_MANIFEST_PATH),
        "approval_schema_sha256": sha256_file(APPROVAL_SCHEMA_PATH),
        "application_runner_sha256": sha256_file(Path(__file__)),
        "application_scheduler_sha256": sha256_file(
            PACKAGE_ROOT / "scheduler/g25_application.py"
        ),
        "deadline_tracker_sha256": sha256_file(
            PACKAGE_ROOT / "scheduler/g25_deadlines.py"
        ),
        "system_closure_sha256": sha256_file(SYSTEM_CLOSURE_PATH),
        "runtime_closure_verifier_sha256": sha256_file(
            PACKAGE_ROOT / "scheduler/g25_runtime_closure.py"
        ),
        "worker_lifetime_guard_sha256": sha256_file(
            PACKAGE_ROOT / "scheduler/g25_worker_lifetime.py"
        ),
        "session_store_sha256": sha256_file(PACKAGE_ROOT / "scheduler/g25_session.py"),
        "snapshot_auditor_sha256": sha256_file(
            PACKAGE_ROOT / "scheduler/g25_snapshot.py"
        ),
        "worker_descriptor_schema_sha256": sha256_file(
            PACKAGE_ROOT / "schemas/g25_worker_descriptor.schema.json"
        ),
        "same_source_review_schema_sha256": sha256_file(REVIEW_SCHEMA_PATH),
        "evaluation_schema_sha256": sha256_file(EVALUATION_SCHEMA_PATH),
        "terminal_schema_sha256": sha256_file(
            PACKAGE_ROOT / "schemas/g25_application_terminal.schema.json"
        ),
        "final_seal_schema_sha256": sha256_file(
            PACKAGE_ROOT / "schemas/g25_application_final_seal.schema.json"
        ),
        "parent_output_replay_schema_sha256": sha256_file(
            PACKAGE_ROOT / "schemas/g25_parent_output_replay.schema.json"
        ),
        "session_file_inventory_schema_sha256": sha256_file(
            PACKAGE_ROOT / "schemas/g25_session_file_inventory.schema.json"
        ),
        "external_seal_anchor_schema_sha256": sha256_file(
            PACKAGE_ROOT / "schemas/g25_external_seal_anchor.schema.json"
        ),
        **historical,
    }
    qualification = load_contract()
    pilot = json.loads(PILOT_SESSION_PATH.read_text(encoding="utf-8"))
    r4 = qualification["r4_immutability"]
    expected_historical = {
        "r3_session_sha256": pilot["preflight"]["r3_session_sha256"],
        "r4_session_sha256": r4["session_sha256"],
        "r4_suite_snapshot_sha256": r4["suite_snapshot_sha256"],
        "r4_journal_sha256": r4["journal_sha256"],
        "r4_failed_state_sha256": r4["failed_state_sha256"],
        "r4_failure_quality_sha256": r4["failure_quality_sha256"],
    }
    if any(bindings[key] != value for key, value in expected_historical.items()):
        raise ApplicationBlocked("R3/R4 immutable full-hash evidence gate failed")
    return bindings


def application_bindings(
    review_record: Path, evaluation_record: Path,
) -> dict[str, str]:
    return {
        **source_bindings(),
        "same_source_review_sha256": sha256_file(review_record),
        "evaluation_record_sha256": sha256_file(evaluation_record),
    }


def validate_model_snapshot(path: Path) -> dict[str, Any]:
    try:
        inventory = validate_exact_snapshot(path, required_root=MODEL_SNAPSHOT_ROOT)
    except SnapshotValidationError as error:
        raise ApplicationBlocked(f"model snapshot exact-set gate failed: {error}") from error
    inventory["frozen_metadata_inventory_sha256"] = sha256_file(MODEL_INVENTORY_PATH)
    # Retain this established field name for downstream approval/session schemas.
    inventory["inventory_sha256"] = inventory["payload_identity_sha256"]
    return inventory


def assert_model_snapshot_unchanged(original: Mapping[str, Any]) -> dict[str, Any]:
    current = validate_model_snapshot(Path(str(original.get("absolute_path", ""))))
    if current != dict(original):
        raise ApplicationBlocked("model snapshot changed after reviewed prevalidation")
    return current


def configure_parent_determinism() -> None:
    """Set torch controls; environment values are verified, never silently filled."""
    required = {
        "CUBLAS_WORKSPACE_CONFIG": ":4096:8",
        "CUDA_LAUNCH_BLOCKING": "1",
        "PYTHONHASHSEED": "0",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
    }
    drift = {key: os.environ.get(key) for key, value in required.items()
             if os.environ.get(key) != value}
    if drift:
        raise ApplicationBlocked(f"required offline/deterministic environment differs: {drift}")
    import torch

    torch.use_deterministic_algorithms(True)
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True
    matmul = torch.backends.cuda.matmul
    if hasattr(matmul, "allow_bf16_reduced_precision_reduction"):
        matmul.allow_bf16_reduced_precision_reduction = False
    if hasattr(matmul, "allow_fp16_reduced_precision_reduction"):
        matmul.allow_fp16_reduced_precision_reduction = False


def prevalidate_application(
    *, approval_record: Path, review_record: Path, review_tag: str,
    evaluation_record: Path, model_snapshot: Path, now_epoch: float | None = None,
) -> tuple[
    dict[str, Any], dict[str, str], dict[str, str], dict[str, str], list[str],
    dict[str, Any], dict[str, Any], dict[str, Any], str,
]:
    verify_runtime_inventory(
        verify_record_files=True, verify_exact_trees=True, require_isolated=True
    )
    target = resolve_review_target(review_tag)
    base_bindings = source_bindings()
    argv = exact_application_argv(
        approval_record=approval_record, review_record=review_record,
        evaluation_record=evaluation_record, review_tag=review_tag,
        model_snapshot=model_snapshot,
    )
    review = load_and_validate_review_record(
        review_record.resolve(), target=target, source_bindings=base_bindings,
        package_identity=package_identity(), expected_argv=tuple(argv),
    )
    review_sha256 = sha256_file(review_record)
    evaluation = load_and_validate_evaluation_record(
        evaluation_record.resolve(), target=target, source_bindings=base_bindings,
        review_sha256=review_sha256, expected_argv=tuple(argv),
    )
    bindings = {
        **base_bindings,
        "same_source_review_sha256": review_sha256,
        "evaluation_record_sha256": sha256_file(evaluation_record),
    }
    expectations = ApprovalExpectations(
        argv=tuple(argv), annotated_tag=target["annotated_tag"],
        tag_object=target["tag_object"], commit=target["commit"],
        tree=target["tree"], package_tree=target["package_tree"], bindings=bindings,
        session_id=SESSION_ID,
    )
    approval = load_and_validate_approval(
        approval_record.resolve(), expectations,
        now_epoch=time.time() if now_epoch is None else now_epoch,
    )
    if approval.get("review") != {
        "document_sha256": review_sha256,
        "architecture": "GO", "model": "GO", "trace": "GO", "blockers": [],
    }:
        raise ApplicationBlocked("owner approval differs from parsed three-role GO record")
    evaluation_sha256 = sha256_file(evaluation_record)
    if approval.get("evaluation_gate") != {
        "evaluator": "5.6sol", "evaluator_model": "gpt-5.6-sol",
        "document_sha256": evaluation_sha256, "verdict": "GO", "blockers": []
    }:
        raise ApplicationBlocked("owner approval lacks the required 5.6sol GO gate")
    model_inventory = validate_model_snapshot(model_snapshot)
    return (
        approval, target, base_bindings, bindings, argv,
        review, evaluation, model_inventory, sha256_file(approval_record),
    )


def _expected_plan() -> list[dict[str, Any]]:
    contract = load_contract()
    selections = _manifest_selections(contract)
    profile_hash = sha256_file(PROFILE_MAP_PATH)
    plan = []
    for ceiling in qualification_ceilings(contract):
        config_hash = canonical_hash(
            resolve_task_profile(load_profile_map(), "T1", ceiling=ceiling)
        )
        for instance in qualification_instances(contract):
            selection = selections[instance]
            plan.append({
                "instance_id": instance,
                "ceiling": ceiling,
                "cell_id": cell_identity(
                    SESSION_ID, instance, selection["sample_id"], ceiling,
                    profile_hash, config_hash,
                ),
            })
    if len(plan) != 12:
        raise ApplicationBlocked("frozen G2.5 plan is not exactly 12 cells")
    return plan


def _write_log(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        data = value.encode("utf-8", errors="replace")
        offset = 0
        while offset < len(data):
            offset += os.write(descriptor, data[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    fsync_directory(path.parent)


def _artifact_descriptor(root: Path, relative: str) -> dict[str, Any]:
    path = root / relative
    return {"path": relative, "bytes": path.stat().st_size, "sha256": sha256_file(path)}


def _terminal(
    store: G25SessionStore, *, disposition: str, reason: str,
    gpu_cells: int, application_audit: Mapping[str, Any],
    qualification_audit: Mapping[str, Any] | None,
    selection_pass: bool,
    deadline_ok: bool,
    qualification_audit_artifact: str,
) -> dict[str, Any]:
    state = store.session_state()
    application_clean = bool(
        application_audit.get("status") == "COMPLETE_SHAPE_AUDITED"
        and application_audit.get("ledger_eligible") is True
        and not application_audit.get("findings")
    )
    qualification_clean = bool(
        qualification_audit is not None
        and qualification_audit.get("status") == "complete"
        and not qualification_audit.get("findings")
    )
    qualification_pass = bool(
        selection_pass
        and disposition == "EXECUTION_COMPLETE"
        and state.get("state") == "TERMINAL_COMPLETE"
        and application_clean
        and qualification_clean
        and deadline_ok
    )
    terminal = {
        "schema_version": "g25-application-terminal-v2",
        "session_id": store.session_id,
        "disposition": disposition,
        "reason": reason,
        "gpu_cells": gpu_cells,
        "gpu_used": gpu_cells > 0,
        "application_audit_sha256": canonical_hash(application_audit),
        "qualification_audit_sha256": (
            canonical_hash(qualification_audit) if qualification_audit is not None else None
        ),
        "qualification_audit_artifact": qualification_audit_artifact,
        "session_state": state.get("state"),
        "journal_head_sha256": state.get("last_event_sha256"),
        "session_state_sha256": sha256_file(store.session_state_path),
        "selection_pass": selection_pass,
        "deadline_ok": deadline_ok,
        "application_audit_clean": application_clean,
        "qualification_audit_clean": qualification_clean,
        "qualification_pass": qualification_pass,
        "formal_g3_r5_authorized": False,
        "paid_gpu_authorized": False,
        "resume": False,
        "retry_failed": False,
    }
    atomic_json(store.root / "terminal.json", terminal)
    return terminal


def _seal_terminal(store: G25SessionStore, qualification_audit_artifact: str) -> dict[str, Any]:
    terminal = json.loads((store.root / "terminal.json").read_text(encoding="utf-8"))
    transition_head = terminal["journal_head_sha256"]
    atomic_json(
        store.root / "qualification_evidence_inventory.json",
        qualification_evidence_inventory(store.root),
    )
    artifacts = {
        "terminal": _artifact_descriptor(store.root, "terminal.json"),
        "application_audit": _artifact_descriptor(store.root, "application_audit.json"),
        "qualification_audit": _artifact_descriptor(
            store.root, qualification_audit_artifact
        ),
        "session_state": _artifact_descriptor(store.root, "session_state.json"),
        "qualification_evidence_inventory": _artifact_descriptor(
            store.root, "qualification_evidence_inventory.json"
        ),
    }
    payload = {
        "terminal_transition_head_sha256": transition_head,
        "artifacts": artifacts,
    }
    event = store.append_event("TERMINAL_BOUND", payload=payload)
    recursive_inventory = session_file_inventory(store.root)
    atomic_json(store.root / "session_file_inventory.json", recursive_inventory)
    seal = {
        "schema_version": "g25-application-final-seal-v2",
        "session_id": store.session_id,
        "terminal_transition_head_sha256": transition_head,
        "terminal_bound_event_sha256": event["event_sha256"],
        "terminal_bound_payload_sha256": canonical_hash(payload),
        "artifacts": artifacts,
        "session_file_inventory": _artifact_descriptor(
            store.root, "session_file_inventory.json"
        ),
        "session_file_inventory_entries_sha256": recursive_inventory[
            "entries_sha256"
        ],
        "external_anchor_required": True,
    }
    atomic_json(store.root / "final_seal.json", seal)
    return seal


def execute_application(
    *,
    output_root: Path,
    run_root: Path,
    approval_record: Path,
    review_record: Path,
    evaluation_record: Path,
    review_tag: str,
    model_snapshot: Path,
    lease: ExecutionLease,
    provider: Callable[[Path], Mapping[str, Any]] = query_dynamic_preflight,
    clock: Callable[[], float] = time.time,
    monotonic: Callable[[], float] = boottime,
    worker_invoker: Callable[..., dict[str, Any]] = invoke_worker_evidence_process,
    outer_timeout_verifier: Callable[[Sequence[str]], Mapping[str, Any]] = verify_outer_timeout_parent,
    containment_factory: Callable[[], CgroupV2Controller] = (
        CgroupV2Controller.discover_and_preflight
    ),
) -> tuple[int, dict[str, Any]]:
    # Bind the internal clock to the already-running approved outer timeout before
    # any validation, lock wait, session creation or GPU discovery consumes time.
    expected_argv = exact_application_argv(
        approval_record=approval_record, review_record=review_record,
        evaluation_record=evaluation_record, review_tag=review_tag,
        model_snapshot=model_snapshot,
    )
    outer_observation = dict(outer_timeout_verifier(expected_argv))
    if (
        outer_observation.get("schema_version")
        != "g25-outer-timeout-observation-v2"
        or outer_observation.get("clock") != "CLOCK_BOOTTIME"
        or outer_observation.get("argv") != expected_argv
        or outer_observation.get("argv_sha256") != canonical_hash(expected_argv)
    ):
        raise ApplicationBlocked("outer timeout verifier did not return exact start evidence")
    started_boottime = float(outer_observation["started_boottime_seconds"])
    tracker = G25DeadlineTracker(monotonic, started=started_boottime)
    if tracker.hard_deadline_reached():
        raise ApplicationBlocked("7200-second internal deadline expired before validation")

    # Approval and source checks happen before session creation or GPU discovery.
    (
        approval, target, original_source_bindings, original_bindings, argv,
        review, evaluation, model_inventory, approval_sha256,
    ) = prevalidate_application(
        approval_record=approval_record, review_record=review_record,
        evaluation_record=evaluation_record, review_tag=review_tag,
        model_snapshot=model_snapshot, now_epoch=clock(),
    )
    if argv != expected_argv:
        raise ApplicationBlocked("prevalidated argv differs from observed outer command")
    if tracker.hard_deadline_reached():
        raise ApplicationBlocked("7200-second internal deadline expired during validation")
    lease.assert_active()
    # Recheck all high-risk bindings inside the package-wide lease (TOCTOU gate).
    if (
        application_bindings(review_record, evaluation_record) != original_bindings
        or sha256_file(approval_record) != approval_sha256
        or resolve_review_target(review_tag) != target
    ):
        raise ApplicationBlocked("application source bindings changed inside GPU lease")
    assert_model_snapshot_unchanged(model_inventory)
    plan = _expected_plan()
    store = G25SessionStore.create(
        output_root, SESSION_ID, [row["cell_id"] for row in plan],
        clock=clock, monotonic=monotonic, started_monotonic=started_boottime,
    )
    gpu_cells = 0
    qualification_audit: dict[str, Any] | None = None
    qualification_audit_artifact = "qualification_audit_failure.json"
    selection_pass = False
    application_audit: dict[str, Any] = {
        "status": "PARTIAL_AUDIT", "findings": ["application audit not reached"],
        "ledger_eligible": False,
    }
    reason = "application did not reach dispatch"
    disposition = "INCOMPLETE"
    containment_controller: CgroupV2Controller | None = None
    try:
        authorization = {
            "schema_version": "g25-application-authorization-v1",
            "session_id": SESSION_ID,
            "review_target": target,
            "source_bindings": original_source_bindings,
            "same_source_review": {
                "review_id": review["review_id"],
                "sha256": original_bindings["same_source_review_sha256"],
            },
            "evaluation": {
                "evaluation_id": evaluation["evaluation_id"],
                "evaluator": evaluation["evaluator"],
                "sha256": original_bindings["evaluation_record_sha256"],
            },
            "owner_approval": {
                "approval_id": approval["approval_id"],
                "approved_by": approval["approved_by"],
                "issued_at_epoch": approval["issued_at_epoch"],
                "expires_at_epoch": approval["expires_at_epoch"],
                "sha256": approval_sha256,
            },
            "exact_command": {
                "argv": argv,
                "argv_sha256": canonical_hash(argv),
            },
            "outer_timeout_observation": outer_observation,
            "model_snapshot_inventory_sha256": model_inventory["inventory_sha256"],
        }
        atomic_json(store.root / "authorization_manifest.json", authorization)
        auth_descriptor = {
            "path": "authorization_manifest.json",
            "bytes": (store.root / "authorization_manifest.json").stat().st_size,
            "sha256": sha256_file(store.root / "authorization_manifest.json"),
        }
        store.append_event("AUTHORIZATION_BOUND", payload=auth_descriptor)
        store.transition_session("PREFLIGHTING")
        snapshot = freeze_package_snapshot(PACKAGE_ROOT, store.root)
        store.append_event("PACKAGE_SNAPSHOT_FROZEN", payload={
            "path": "snapshots/inventory.json",
            "bytes": (store.root / "snapshots/inventory.json").stat().st_size,
            "sha256": sha256_file(store.root / "snapshots/inventory.json"),
            "inventory_sha256": snapshot["inventory_sha256"],
            "source_checksums_sha256": snapshot["source_checksums_sha256"],
            "file_count": snapshot["file_count"],
        })
        containment_controller = containment_factory()
        containment_value = containment_controller.evidence
        containment_evidence = (
            asdict(containment_value)
            if is_dataclass(containment_value)
            else dict(containment_value)
        )
        atomic_json(store.root / "application_cgroup.json", containment_evidence)
        store.append_event("APPLICATION_CGROUP_BOUND", payload={
            "path": "application_cgroup.json",
            "bytes": (store.root / "application_cgroup.json").stat().st_size,
            "sha256": sha256_file(store.root / "application_cgroup.json"),
        })
        configure_parent_determinism()
        preflight = run_dynamic_preflight(provider, run_root, approval)
        preflight["runtime_closure"] = verify_live_loaded_closure(
            "parent_preflight"
        )
        atomic_json(store.root / "dynamic_preflight.json", preflight)
        atomic_json(store.root / "model_snapshot_inventory.json", model_inventory)
        store.append_event("MODEL_SNAPSHOT_BOUND", payload={
            "path": "model_snapshot_inventory.json",
            "bytes": (store.root / "model_snapshot_inventory.json").stat().st_size,
            "sha256": sha256_file(store.root / "model_snapshot_inventory.json"),
            "inventory_sha256": model_inventory["inventory_sha256"],
        })
        store.append_event("DYNAMIC_PREFLIGHT_BOUND", payload={
            "path": "dynamic_preflight.json",
            "bytes": (store.root / "dynamic_preflight.json").stat().st_size,
            "sha256": sha256_file(store.root / "dynamic_preflight.json"),
        })
        store.transition_session("READY")
        store.transition_session("DISPATCHING")
        (store.root / "descriptors").mkdir()
        (store.root / "cells").mkdir()
        (store.root / "worker_io").mkdir()
        cells: list[dict[str, Any]] = []
        for item in plan:
            lease.assert_active()
            assert_model_snapshot_unchanged(model_inventory)
            if not tracker.may_dispatch():
                raise ApplicationBlocked("5790-second latest-new-dispatch boundary reached")
            if (
                application_bindings(review_record, evaluation_record) != original_bindings
                or sha256_file(approval_record) != approval_sha256
                or resolve_review_target(review_tag) != target
            ):
                raise ApplicationBlocked("package or review hash drift before cell dispatch")
            current = run_dynamic_preflight(provider, run_root, approval)
            current_runtime_closure = verify_live_loaded_closure(
                "parent_preflight"
            )
            if current["gpu"] != preflight["gpu"]:
                raise ApplicationBlocked("GPU identity or capacity drift before cell dispatch")
            descriptor = build_worker_descriptor(
                session_id=SESSION_ID, instance_id=item["instance_id"],
                ceiling=item["ceiling"],
                model_snapshot_inventory_sha256=model_inventory["inventory_sha256"],
                device_identity={
                    "kind": "cuda", "name": preflight["gpu"]["name"],
                    "uuid": preflight["gpu"]["uuid"],
                    "pci_bus_id": preflight["gpu"]["pci_bus_id"],
                },
            )
            descriptor_path = store.root / "descriptors" / f"{item['cell_id']}.json"
            atomic_json(descriptor_path, descriptor)
            io_root = store.root / "worker_io" / item["cell_id"]
            io_root.mkdir()
            atomic_json(
                io_root / "parent_runtime_closure.json",
                current_runtime_closure,
            )
            evidence_path = io_root / "worker_evidence.json"
            worker_argv = build_worker_argv(
                descriptor_path.resolve(), evidence_path.resolve(),
                model_snapshot.resolve(),
                package_snapshot_root=store.root / "snapshots/package",
                python_executable=RUNTIME_PYTHON,
            )
            if not tracker.may_dispatch():
                raise ApplicationBlocked(
                    "5790-second latest-new-dispatch boundary reached immediately before worker"
                )
            if tracker.execution_cutoff_reached():
                raise ApplicationBlocked("6300-second execution cutoff reached before worker")
            store.transition_cell(item["cell_id"], "DISPATCHED")
            store.transition_cell(item["cell_id"], "RUNNING")
            containment = containment_controller.prepare_cell(item["cell_id"])
            supervisor = worker_invoker(
                worker_argv, evidence_path, lease=lease,
                containment=containment, timeout_seconds=480
            )
            if supervisor.get("supervisor_result") is True:
                gpu_cells += 1
            _write_log(io_root / "stdout.log", str(supervisor.get("stdout") or ""))
            _write_log(io_root / "stderr.log", str(supervisor.get("stderr") or ""))
            supervisor_path = io_root / "supervisor.json"
            atomic_json(supervisor_path, {
                key: value for key, value in supervisor.items()
                if key not in {"stdout", "stderr", "evidence_payload"}
            })
            io_manifest = {
                "schema_version": "g25-worker-io-manifest-v1",
                "session_id": SESSION_ID,
                "cell_id": item["cell_id"],
                "artifacts": {
                    "cell_descriptor": {
                        "bytes": descriptor_path.stat().st_size,
                        "sha256": sha256_file(descriptor_path),
                    },
                    "worker_evidence": {
                        "bytes": evidence_path.stat().st_size
                        if evidence_path.is_file() else None,
                        "sha256": sha256_file(evidence_path)
                        if evidence_path.is_file() else None,
                    },
                    "supervisor": {
                        "bytes": supervisor_path.stat().st_size,
                        "sha256": sha256_file(supervisor_path),
                    },
                    "stdout_log": {
                        "bytes": (io_root / "stdout.log").stat().st_size,
                        "sha256": sha256_file(io_root / "stdout.log"),
                    },
                    "stderr_log": {
                        "bytes": (io_root / "stderr.log").stat().st_size,
                        "sha256": sha256_file(io_root / "stderr.log"),
                    },
                    "parent_runtime_closure": {
                        "bytes": (io_root / "parent_runtime_closure.json").stat().st_size,
                        "sha256": sha256_file(
                            io_root / "parent_runtime_closure.json"
                        ),
                    },
                },
            }
            atomic_json(io_root / "io_manifest.json", io_manifest)
            supervisor["io_manifest_sha256"] = sha256_file(io_root / "io_manifest.json")
            store.transition_cell(item["cell_id"], "PROCESS_EXITED")
            selection = _manifest_selections(load_contract())[item["instance_id"]]
            evidence = normalize_worker_process_result(
                supervisor, selection=selection, ceiling=item["ceiling"]
            )
            raw_descriptor = store.write_raw(item["cell_id"], evidence)
            row = build_cell_row(
                session_id=SESSION_ID, selection=selection,
                ceiling=item["ceiling"],
                profile_sha256=sha256_file(PROFILE_MAP_PATH),
                generation_config_sha256=descriptor["generation_config_sha256"],
                evidence=evidence, evidence_descriptor=raw_descriptor,
                synthetic_session=False,
            )
            atomic_json(store.root / "cells" / f"{item['cell_id']}.json", row)
            store.transition_cell(
                item["cell_id"], "CLASSIFIED",
                classification=row["qualification_class"],
                reason=row["classification_reason"],
            )
            store.transition_cell(item["cell_id"], "RECORDED")
            cells.append(row)
            if tracker.execution_cutoff_reached():
                raise ApplicationBlocked(
                    "6300-second execution cutoff reached after active worker termination"
                )
        ledger = build_ledger(
            SESSION_ID, cells, contract=load_contract(),
            profile_sha256=sha256_file(PROFILE_MAP_PATH), synthetic_session=False,
        )
        verdict = select_common_ceiling(ledger)
        atomic_json(store.root / "ledger.json", ledger)
        atomic_json(store.root / "verdict.json", verdict)
        session = {
            "schema_version": "g25-qualification-session-v1",
            "session_id": SESSION_ID,
            "status": "qualification_execution_complete",
            "evidence_role": "termination_qualification_non_formal",
            "synthetic": False,
            "gpu_used": True,
            "formal_c1_evidence": False,
            "contract_sha256": sha256_file(CONTRACT_PATH),
            "profile_map_sha256": sha256_file(PROFILE_MAP_PATH),
            "candidate_manifest_sha256": load_contract()["frozen_inputs"][
                "candidate_manifest_sha256"
            ],
            "execution_identity_set_sha256": ledger["execution_identity_set_sha256"],
            "expected_cell_count": 12,
            "completed_cell_count": 12,
            "artifacts": {
                "ledger": sha256_file(store.root / "ledger.json"),
                "verdict": sha256_file(store.root / "verdict.json"),
            },
            "selected_common_ceiling": verdict["selected_common_ceiling"],
            "gpu_pilot_authorized": False,
        }
        validate_schema(SESSION_SCHEMA, session)
        atomic_json(store.root / "session.json", session)
        disposition = "EXECUTION_COMPLETE"
        reason = verdict["status"]
        selection_pass = verdict["selected_common_ceiling"] is not None
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        disposition = "INCOMPLETE_HARD_STOP"
    finally:
        if containment_controller is not None:
            try:
                containment_controller.emergency_drain_all()
                containment_controller.assert_all_cells_empty()
                containment_controller.close()
            except Exception as exc:
                disposition = "INCOMPLETE_HARD_STOP"
                reason = (
                    "cgroup containment cleanup failed: "
                    f"{type(exc).__name__}: {exc}"
                )
        store.mark_unfinished_cells(reason)
        current = store.session_state()["state"]
        if current not in {"FINALIZING", "AUDITING", "SEALING", "TERMINAL_COMPLETE",
                           "TERMINAL_INCOMPLETE", "TERMINAL_FAILED"}:
            store.transition_session("FINALIZING", reason=reason)
        if store.session_state()["state"] == "FINALIZING":
            try:
                lease.assert_active()
                verify_runtime_inventory(
                    verify_record_files=True,
                    verify_exact_trees=True,
                    require_isolated=True,
                )
                final_runtime_closure = verify_live_loaded_closure(
                    "parent_finalization"
                )
                atomic_json(
                    store.root / "parent_final_runtime_closure.json",
                    final_runtime_closure,
                )
                verify_package_ledger(PACKAGE_ROOT)
                if (
                    application_bindings(review_record, evaluation_record) != original_bindings
                    or sha256_file(approval_record) != approval_sha256
                    or resolve_review_target(review_tag) != target
                ):
                    raise ApplicationBlocked(
                        "authorization or reviewed source drift at finalization"
                    )
                assert_model_snapshot_unchanged(model_inventory)
                snapshot_findings = audit_package_snapshot(store.root)
                if snapshot_findings:
                    raise ApplicationBlocked("; ".join(snapshot_findings))
            except Exception as exc:
                disposition = "INCOMPLETE_HARD_STOP"
                reason = f"post-execution verification failed: {type(exc).__name__}: {exc}"
            store.transition_session("AUDITING", reason=reason)
        if (store.root / "session.json").is_file():
            try:
                qualification_audit = audit_session(store.root)
                atomic_json(store.root / "audit.json", qualification_audit)
                qualification_audit_artifact = "audit.json"
                if (
                    qualification_audit.get("status") != "complete"
                    or qualification_audit.get("findings")
                ):
                    disposition = "INCOMPLETE_HARD_STOP"
                    reason = "qualification audit failed"
            except Exception as exc:
                qualification_audit = {
                    "schema_version": "g25-qualification-audit-failure-v1",
                    "status": "incomplete",
                    "findings": [f"{type(exc).__name__}: {exc}"],
                    "qualification_pass": False,
                }
                atomic_json(
                    store.root / "qualification_audit_failure.json",
                    qualification_audit,
                )
                disposition = "INCOMPLETE_HARD_STOP"
                reason = "qualification audit raised an exception"
        if qualification_audit is None:
            qualification_audit = {
                "schema_version": "g25-qualification-audit-failure-v1",
                "status": "incomplete",
                "findings": ["qualification session artifacts were not complete"],
                "qualification_pass": False,
            }
            atomic_json(
                store.root / "qualification_audit_failure.json", qualification_audit
            )
        audit_before_terminal = audit_partial_session(store.root)
        if audit_before_terminal.get("findings"):
            disposition = "INCOMPLETE_HARD_STOP"
            reason = "pre-terminal application audit failed"
        if tracker.hard_deadline_reached():
            disposition = "INCOMPLETE_HARD_STOP"
            reason = "7200-second internal hard deadline reached after audit"
        if store.session_state()["state"] == "AUDITING":
            store.transition_session("SEALING", reason=reason)
        application_audit = audit_partial_session(store.root)
        atomic_json(store.root / "application_audit.json", application_audit)
        if application_audit.get("findings"):
            disposition = "INCOMPLETE_HARD_STOP"
            reason = "final application audit failed"
        deadline_ok = not tracker.hard_deadline_reached()
        if not deadline_ok:
            disposition = "INCOMPLETE_HARD_STOP"
            reason = "7200-second internal hard deadline reached after final audit"
        qualification_clean = bool(
            qualification_audit is not None
            and qualification_audit.get("status") == "complete"
            and not qualification_audit.get("findings")
        )
        terminal_state = (
            "TERMINAL_COMPLETE"
            if (
                disposition == "EXECUTION_COMPLETE"
                and application_audit.get("ledger_eligible") is True
                and not application_audit.get("findings")
                and qualification_clean
                and deadline_ok
            )
            else "TERMINAL_INCOMPLETE"
        )
        store.transition_session(terminal_state, reason=reason)
        terminal = _terminal(
            store, disposition=disposition, reason=reason,
            gpu_cells=gpu_cells, application_audit=application_audit,
            qualification_audit=qualification_audit,
            selection_pass=selection_pass,
            deadline_ok=deadline_ok,
            qualification_audit_artifact=qualification_audit_artifact,
        )
        seal = _seal_terminal(store, qualification_audit_artifact)
        anchor_receipt: dict[str, Any] | None = None
        anchor_error: str | None = None
        try:
            anchor_receipt = write_external_seal_anchor(store.root, seal)
        except Exception as exc:
            anchor_error = f"{type(exc).__name__}: {exc}"
        final_audit = audit_finalized_application(
            store.root,
            seal_anchor=(
                Path(anchor_receipt["path"]) if anchor_receipt is not None else None
            ),
            expected_anchor_sha256=(
                anchor_receipt["sha256"] if anchor_receipt is not None else None
            ),
        )
        report = {**terminal, "final_audit": final_audit}
        report["seal_anchor"] = (
            {
                "path": anchor_receipt["path"],
                "sha256": anchor_receipt["sha256"],
            }
            if anchor_receipt is not None else None
        )
        report["seal_anchor_error"] = anchor_error
        report["qualification_pass"] = final_audit["qualification_pass"]
    return (0 if report["qualification_pass"] else 20), report

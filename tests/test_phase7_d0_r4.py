"""CPU-only adversarial tests for the prospective Phase 7 D0-R4 repair."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from explorations.moe_cycle_simulator.phase7_d0_r4.controller import (
    D0R4Error,
    PACKAGE_ROOT,
    TransportResult,
    build_ssh_argv,
    build_command_preimage,
    file_sha256,
    load_package,
    run_bounded_process,
    run_session,
    semantic_sha256,
    validate_schema,
    verify_terminal,
    _validate_dependency_manifest,
    _load_schema,
)


def _probe(*, omit: str | None = None) -> dict:
    value = {
        "schema_version": "moe-simulator-phase7-gputw-d0-r4-probe-v1",
        "capture_status": "COMPLETE",
        "captured_at_utc": "2026-08-09T08:00:00Z",
        "provider": {"name": "GPUtw.ai", "instance_id": None, "instance_state": None},
        "instance": {"principal": None, "catalog_gpu_id": None, "node_id": None, "environment_label": None},
        "host": {
            "hostname": None,
            "os_release": None,
            "kernel_release": None,
            "boot_id": None,
            "python": {"path": None, "version": None, "sha256": None},
            "timeout": {"path": None, "version": None, "sha256": None},
        },
        "gpu": {"query_status": "UNAVAILABLE", "count": None, "devices": []},
        "runtime": {
            "container_image": None,
            "container_digest": None,
            "cuda": None,
            "driver": None,
            "vllm": {"present": False, "version": None, "path": None, "distribution_sha256": None},
            "backends": [],
        },
        "storage": {
            "vault": {"path": "/vault", "mounted": False, "mount_identity_sha256": None, "total_bytes": None, "free_bytes": None},
            "workspace": {"path": "/workspace", "mounted": False, "mount_identity_sha256": None, "total_bytes": None, "free_bytes": None},
        },
        "observed_commands": [],
        "prohibitions": {"remote_writes": False, "package_install": False, "model_access": False, "inference": False, "cuda_benchmark": False, "gpu_workload": False},
    }
    if omit is not None:
        value.pop(omit)
    return value


def _approval(tmp_path: Path) -> tuple[dict, Path, Path, object]:
    package = load_package(PACKAGE_ROOT)
    session_id = "d0-r4-test-session"
    evidence_root = tmp_path / session_id
    registry = tmp_path / "registry" / "used.approval"
    registry.parent.mkdir()
    known_hosts = tmp_path / "known_hosts"
    blob = b"synthetic-host-key-blob"
    encoded_blob = base64.b64encode(blob).decode()
    known_hosts.write_bytes(f"[synthetic.example]:2222 ssh-ed25519 {encoded_blob}\n".encode())
    executable = Path(sys.executable).resolve()
    owner_authority = {
        "schema_version": "moe-simulator-phase7-gputw-d0-r4-owner-authority-v1",
        "decision_id": "owner-authority-test",
        "decision_status": "APPROVED",
        "selected_environment": {
            "environment_family": "vLLM Inference Server",
            "exact_image_or_template_identity": "synthetic-template-v2",
            "change_from_d0_r1": "REJECT_UNAPPROVED_UBUNTU_CUDA_LABEL",
        },
        "cost_authority": {
            "maximum_runtime_seconds": 28800,
            "prepaid_entitlement": {"original_prepaid_seconds": 21600, "additional_prepaid_seconds": 7200, "total_prepaid_seconds": 28800, "status": "FULLY_PREPAID_BEFORE_R4_FREEZE"},
            "maximum_additional_spend": {"amount": "0", "currency": "TWD"},
            "compute": {"cap_amount": "0", "cap_currency": "TWD", "classification": "ZERO_SPEND"},
            "storage": {"cap_amount": "0", "cap_currency": "TWD", "classification": "ZERO_SPEND"},
            "port": {"cap_amount": "0", "cap_currency": "TWD", "classification": "ZERO_SPEND"},
            "top_up": False,
            "extension": False,
            "extension_beyond_28800": False,
            "provider_grace_credit_seconds": 0,
            "historical_300_twd_record": "NOT_DISCRETIONARY",
        },
        "time_origin": {
            "semantic": "FRESH_SSH_HANDOFF",
            "control_plane_start_utc": "2026-08-09T08:00:00Z",
            "lease_start_utc": "2026-08-09T08:00:00Z",
            "lease_deadline_utc": "2026-08-09T16:00:00Z",
            "status": "VERIFIED",
        },
        "authority": {"d0": "APPROVED_FOR_ONE_SHOT_DISCOVERY", "gate_m": "NOT_AUTHORIZED", "m0": "NOT_AUTHORIZED", "gpu": "NONE"},
        "private_credentials": "FORBIDDEN_AND_NOT_RECORDED",
    }
    owner_path = tmp_path / "owner_authority.json"
    owner_path.write_text(json.dumps(owner_authority, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    owner_hash = hashlib.sha256(owner_path.read_bytes()).hexdigest()
    review_authority = {
        "schema_version": "moe-simulator-phase7-gputw-d0-r4-review-authority-v1",
        "candidate_id": "moe-simulator-phase7-gputw-d0-r4-test",
        "reviewed_commit": "a" * 40,
        "reviewed_tree": "b" * 40,
        "candidate_ledger_sha256": "c" * 64,
        "application_identity_file_sha256": hashlib.sha256((PACKAGE_ROOT / "application_identity.json").read_bytes()).hexdigest(),
        "application_identity_semantic_sha256": package.application_identity_sha256,
        "application_ledger_file_sha256": package.application_ledger_sha256,
        "reviewer_verdicts": {"Architecture/System": "GO", "Model/Benchmark": "GO", "Trace/Provenance": "GO"},
        "blockers": [],
        "side_effects": False,
    }
    review_path = tmp_path / "review_authority.json"
    review_path.write_text(json.dumps(review_authority, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    approval = {
        "schema_version": "moe-simulator-phase7-gputw-d0-r4-approval-v1",
        "application_id": package.overlay["application_id"],
        "approval_id": "approval-d0-r4-test",
        "session_id": session_id,
        "decision": "APPROVE",
        "execution_mode": "DISCOVERY_ONLY",
        "reviewed_commit": "a" * 40,
        "reviewed_tree": "b" * 40,
        "reviewed_candidate_ledger_sha256": "c" * 64,
        "review_closure_input": {"path": str(review_path), "sha256": hashlib.sha256(review_path.read_bytes()).hexdigest()},
        "owner_authority_sha256": owner_hash,
        "owner_authority_input": {"path": str(owner_path), "sha256": owner_hash},
        "application_identity_sha256": package.application_identity_sha256,
        "application_ledger_sha256": package.application_ledger_sha256,
        "lease": {
            "start_trigger": "FRESH_OWNER_SSH_HANDOFF",
            "control_plane_start_utc": "2026-08-09T08:00:00Z",
            "lease_start_utc": "2026-08-09T08:00:00Z",
            "lease_deadline_utc": "2026-08-09T16:00:00Z",
            "total_seconds": 28800,
            "prepaid_entitlement": {"original_prepaid_seconds": 21600, "additional_prepaid_seconds": 7200, "total_prepaid_seconds": 28800, "status": "FULLY_PREPAID_BEFORE_R4_FREEZE"},
            "stage_bounds_seconds": {"D0": 300, "Gate_M": 5400, "M0": 14400, "release": 900},
            "bounded_work_seconds": 21000,
            "shared_transition_slack_seconds": 7800,
            "maximum_additional_spend_amount": "0",
            "maximum_additional_spend_currency": "TWD",
            "extension_allowed": False,
            "extension_beyond_28800_allowed": False,
            "top_up_allowed": False,
            "provider_grace_credit_seconds": 0,
        },
        "endpoint": {"host": "synthetic.example", "port": 2222, "username": "test-principal", "instance_id": "test-instance"},
        "host_key": {
            "known_hosts_path": str(known_hosts),
            "known_hosts_sha256": hashlib.sha256(known_hosts.read_bytes()).hexdigest(),
            "host_key_blob_sha256": hashlib.sha256(blob).hexdigest(),
            "fingerprint": "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("="),
            "provenance": "OFFICIAL_PROVIDER_CONFIRMATION",
            "provenance_artifact_path": str(tmp_path / "host_key_confirmation.json"),
            "provenance_artifact_sha256": "0" * 64,
        },
        "credential": {
            "selector_kind": "SSH_AGENT_SOCKET",
            "selector": str(tmp_path / "agent.sock"),
            "client_public_key_fingerprint": "SHA256:" + base64.b64encode(hashlib.sha256(b"synthetic-client-key-blob").digest()).decode().rstrip("="),
            "agent_query_executable_path": "/usr/bin/ssh-add",
            "agent_query_executable_sha256": hashlib.sha256(Path("/usr/bin/ssh-add").read_bytes()).hexdigest(),
            "private_key_material": "FORBIDDEN_NOT_RECORDED",
        },
        "local_ssh": {"executable_path": str(executable), "executable_sha256": file_sha256(executable)},
        "runtime": {"approved_container_digest": None, "digest_status": "UNOBSERVED_DISCOVERY_ONLY", "discovery_only_reason": "A missing immutable digest cannot be promoted"},
        "evidence": {"evidence_root": str(evidence_root), "one_shot_registry_path": str(registry), "terminal_sealing": "REQUIRED"},
        "command_binding_sha256": "0" * 64,
        "remote_payload_sha256": hashlib.sha256(package.probe_source).hexdigest(),
        "authority": ["one exact read-only discovery D0 execution", "local bounded evidence capture", "atomic terminal sealing"],
        "not_authorized": ["remote write", "package install", "model download", "model access", "GPU workload", "Gate M", "M0", "retry", "resume", "extension", "additional spend"],
    }
    provenance = {
        "schema_version": "gpu-tw-host-key-confirmation-v1",
        "source_id": "synthetic-provider-confirmation",
        "confirmed_at_utc": "2026-08-09T07:59:00Z",
        "endpoint": "[synthetic.example]:2222",
        "known_hosts_sha256": approval["host_key"]["known_hosts_sha256"],
        "host_key_blob_sha256": approval["host_key"]["host_key_blob_sha256"],
        "fingerprint": approval["host_key"]["fingerprint"],
    }
    provenance_path = Path(approval["host_key"]["provenance_artifact_path"])
    provenance_path.write_text(json.dumps(provenance, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    approval["host_key"]["provenance_artifact_sha256"] = hashlib.sha256(provenance_path.read_bytes()).hexdigest()
    listing = _agent_listing(approval)
    selected_key = b"ssh-ed25519 " + base64.b64encode(b"synthetic-client-key-blob") + b"\n"
    preimage = build_command_preimage(
        package,
        approval,
        known_hosts=known_hosts.read_bytes(),
        ssh_executable=executable.read_bytes(),
        review_closure=review_path.read_bytes(),
        host_provenance=provenance_path.read_bytes(),
        agent_listing=listing,
        selected_public_key=selected_key,
    )
    approval["command_binding_sha256"] = semantic_sha256(preimage)
    approval_path = tmp_path / "approval.json"
    approval_path.write_text(json.dumps(approval, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    return approval, approval_path, evidence_root, package


def _agent_listing(_approval: dict) -> bytes:
    blob = base64.b64encode(b"synthetic-client-key-blob").decode()
    return f"ssh-ed25519 {blob} d0-r4-test\n".encode()


def _rebind_command(approval: dict, package: object) -> None:
    listing = _agent_listing(approval)
    selected_key = b"ssh-ed25519 " + base64.b64encode(b"synthetic-client-key-blob") + b"\n"
    preimage = build_command_preimage(
        package,
        approval,
        known_hosts=Path(approval["host_key"]["known_hosts_path"]).read_bytes(),
        ssh_executable=Path(approval["local_ssh"]["executable_path"]).read_bytes(),
        review_closure=Path(approval["review_closure_input"]["path"]).read_bytes(),
        host_provenance=Path(approval["host_key"]["provenance_artifact_path"]).read_bytes(),
        agent_listing=listing,
        selected_public_key=selected_key,
    )
    approval["command_binding_sha256"] = semantic_sha256(preimage)


def _run(*args: object, **kwargs: object) -> dict:
    now = kwargs.pop("now_utc", datetime(2026, 8, 9, 8, 0, tzinfo=timezone.utc))
    provider = kwargs.pop("agent_listing_provider", _agent_listing)
    return run_session(
        *args,
        now_utc=now,
        agent_listing_provider=provider,
        **kwargs,
    )


def test_package_is_cpu_only_and_non_promotable() -> None:
    package = load_package(PACKAGE_ROOT)
    assert package.overlay["promotion"] == {
        "execution_mode": "DISCOVERY_ONLY",
        "promotable": False,
        "reason": "CONTAINER_DIGEST_AND_OWNER_LIVE_INPUTS_UNOBSERVED",
        "promotion_requires_new_application": True,
    }
    assert package.overlay["authority"]["gpu_authority"] == "NONE"


def test_probe_schema_rejects_omission() -> None:
    schema = _load_schema("schemas/probe.schema.json")
    validate_schema(_probe(), schema)
    with pytest.raises(D0R4Error, match="missing required property runtime"):
        validate_schema(_probe(omit="runtime"), schema)


def test_command_preimage_rejects_drift(tmp_path: Path) -> None:
    approval, approval_path, evidence_root, package = _approval(tmp_path)
    approval["command_binding_sha256"] = "0" * 64
    approval_path.write_text(json.dumps(approval, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    with pytest.raises(D0R4Error, match="COMMAND_PREIMAGE_DRIFT"):
        _run(PACKAGE_ROOT, approval_path, evidence_root, runner=lambda *_args: pytest.fail("transport must not start"))


def test_ambient_key_is_not_used(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    approval, _approval_path, _evidence_root, _package = _approval(tmp_path)
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/ambient-agent.sock")
    argv, env_overrides = build_ssh_argv(approval)
    assert argv
    assert env_overrides["SSH_AUTH_SOCK"] == approval["credential"]["selector"]
    assert env_overrides["SSH_AUTH_SOCK"] != os.environ["SSH_AUTH_SOCK"]


def test_host_key_provenance_drift_is_blocked(tmp_path: Path) -> None:
    approval, approval_path, evidence_root, _package = _approval(tmp_path)
    approval["host_key"]["provenance"] = "PERSONAL_KNOWN_HOSTS"
    approval_path.write_text(json.dumps(approval, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    with pytest.raises(D0R4Error, match="provenance"):
        _run(PACKAGE_ROOT, approval_path, evidence_root, runner=lambda *_args: pytest.fail("transport must not start"))


def test_host_key_provenance_artifact_drift_is_blocked(tmp_path: Path) -> None:
    approval, approval_path, evidence_root, _package = _approval(tmp_path)
    provenance_path = Path(approval["host_key"]["provenance_artifact_path"])
    provenance_path.write_bytes(provenance_path.read_bytes() + b"\n")
    with pytest.raises(D0R4Error, match="provenance artifact drifted"):
        _run(PACKAGE_ROOT, approval_path, evidence_root, runner=lambda *_args: pytest.fail("transport must not start"))


def test_transitive_dependency_drift_is_blocked() -> None:
    package = load_package(PACKAGE_ROOT)
    dependency = json.loads((PACKAGE_ROOT / "dependency_manifest.json").read_text(encoding="utf-8"))
    dependency["members"][0]["sha256"] = "0" * 64
    with pytest.raises(D0R4Error, match="transitive dependency drift"):
        _validate_dependency_manifest(package.package_bytes, dependency)


def test_discovery_result_binds_all_identities_and_seals(tmp_path: Path) -> None:
    _approval_value, approval_path, evidence_root, _package = _approval(tmp_path)
    probe_bytes = json.dumps(_probe(), sort_keys=True, separators=(",", ":")).encode() + b"\n"

    def fake_runner(*_args: object) -> TransportResult:
        return TransportResult(0, probe_bytes, b"", False, False, 123)

    result = _run(PACKAGE_ROOT, approval_path, evidence_root, runner=fake_runner)
    assert result["terminal_status"] == "COMPLETE"
    assert result["environment_eligibility"] == "NOT_READY"
    assert "CONTAINER_DIGEST_UNOBSERVED" in result["eligibility_findings"]
    assert result["application_identity_sha256"]
    assert result["result_evidence_identity_sha256"]
    ledger = verify_terminal(evidence_root)
    assert ledger["application_identity_sha256"] == result["application_identity_sha256"]
    assert ledger["result_evidence_identity_sha256"] == result["result_evidence_identity_sha256"]
    assert (evidence_root / "terminal_status.txt").read_text(encoding="utf-8") == "D0_R4_COMPLETE_AUDITED\n"

    broken_result = dict(result)
    broken_result.pop("result_evidence_identity_sha256")
    with pytest.raises(D0R4Error, match="missing required property result_evidence_identity_sha256"):
        validate_schema(broken_result, _load_schema("schemas/result.schema.json"))


def test_one_shot_registry_blocks_replay(tmp_path: Path) -> None:
    _approval_value, approval_path, evidence_root, _package = _approval(tmp_path)
    probe_bytes = json.dumps(_probe(), sort_keys=True, separators=(",", ":")).encode() + b"\n"
    fake = lambda *_args: TransportResult(0, probe_bytes, b"", False, False, 1)
    _run(PACKAGE_ROOT, approval_path, evidence_root, runner=fake)
    with pytest.raises(D0R4Error):
        _run(PACKAGE_ROOT, approval_path, evidence_root, runner=fake)


def test_bounded_process_timeout_is_terminal() -> None:
    result = run_bounded_process(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        b"",
        env_overrides={"SSH_AUTH_SOCK": "/tmp/explicit-agent.sock"},
        timeout_seconds=1,
        max_stdout_bytes=1024,
        max_stderr_bytes=1024,
    )
    assert result.timed_out is True
    assert result.output_limited is False


def test_setsid_descendant_is_killed_after_parent_exits() -> None:
    child_code = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    parent_code = "import subprocess,sys,time; p=subprocess.Popen([sys.executable, '-c', %r], start_new_session=True); print(p.pid, flush=True); time.sleep(60)" % child_code
    result = run_bounded_process(
        [sys.executable, "-c", parent_code],
        b"",
        env_overrides={},
        timeout_seconds=1,
        max_stdout_bytes=1024,
        max_stderr_bytes=1024,
    )
    child_pid = int(result.stdout.strip())
    assert result.timed_out is True
    assert not Path(f"/proc/{child_pid}").exists()


def test_evidence_root_is_bound_before_one_shot_consumption(tmp_path: Path) -> None:
    _approval_value, approval_path, evidence_root, _package = _approval(tmp_path)
    redirected = tmp_path / "missing-parent" / evidence_root.name
    registry = Path(json.loads(approval_path.read_text(encoding="utf-8"))["evidence"]["one_shot_registry_path"])
    with pytest.raises(D0R4Error, match="evidence root"):
        _run(PACKAGE_ROOT, approval_path, redirected, runner=lambda *_args: pytest.fail("transport must not start"))
    assert not registry.exists()
    assert not redirected.exists()


def test_expired_lease_is_rejected_before_transport(tmp_path: Path) -> None:
    _approval_value, approval_path, evidence_root, _package = _approval(tmp_path)
    with pytest.raises(D0R4Error, match="currently active"):
        _run(
            PACKAGE_ROOT,
            approval_path,
            evidence_root,
            now_utc=datetime(2026, 8, 9, 16, 0, tzinfo=timezone.utc),
            runner=lambda *_args: pytest.fail("transport must not start"),
        )


def test_selected_agent_must_contain_approved_fingerprint(tmp_path: Path) -> None:
    _approval_value, approval_path, evidence_root, _package = _approval(tmp_path)
    wrong_listing = lambda _approval: b"ssh-ed25519 d3Jvbmc= wrong-key\n"
    with pytest.raises(D0R4Error, match="must match exactly one"):
        _run(
            PACKAGE_ROOT,
            approval_path,
            evidence_root,
            agent_listing_provider=wrong_listing,
            runner=lambda *_args: pytest.fail("transport must not start"),
        )


def test_reviewed_commit_must_equal_external_review_authority(tmp_path: Path) -> None:
    approval, approval_path, evidence_root, package = _approval(tmp_path)
    approval["reviewed_commit"] = "d" * 40
    _rebind_command(approval, package)
    approval_path.write_text(json.dumps(approval, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    with pytest.raises(D0R4Error, match="differs from review closure"):
        _run(PACKAGE_ROOT, approval_path, evidence_root, runner=lambda *_args: pytest.fail("transport must not start"))


def test_reviewed_candidate_ledger_must_equal_external_review_authority(tmp_path: Path) -> None:
    approval, approval_path, evidence_root, package = _approval(tmp_path)
    approval["reviewed_candidate_ledger_sha256"] = "d" * 64
    _rebind_command(approval, package)
    approval_path.write_text(json.dumps(approval, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    with pytest.raises(D0R4Error, match="candidate ledger differs"):
        _run(PACKAGE_ROOT, approval_path, evidence_root, runner=lambda *_args: pytest.fail("transport must not start"))


def test_owner_control_plane_time_origin_is_cross_bound(tmp_path: Path) -> None:
    approval, approval_path, evidence_root, package = _approval(tmp_path)
    owner_path = Path(approval["owner_authority_input"]["path"])
    owner = json.loads(owner_path.read_text(encoding="utf-8"))
    owner["time_origin"]["control_plane_start_utc"] = "2026-08-09T07:00:00Z"
    owner_path.write_text(json.dumps(owner, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    owner_hash = hashlib.sha256(owner_path.read_bytes()).hexdigest()
    approval["owner_authority_sha256"] = owner_hash
    approval["owner_authority_input"]["sha256"] = owner_hash
    _rebind_command(approval, package)
    approval_path.write_text(json.dumps(approval, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    with pytest.raises(D0R4Error, match="control-plane start mismatch"):
        _run(PACKAGE_ROOT, approval_path, evidence_root, runner=lambda *_args: pytest.fail("transport must not start"))


def test_package_exact_set_rejects_import_shadow(tmp_path: Path) -> None:
    copied = tmp_path / "phase7_d0_r4"
    shutil.copytree(PACKAGE_ROOT, copied)
    (copied / "json.py").write_text("raise RuntimeError('shadow executed')\n", encoding="utf-8")
    with pytest.raises(D0R4Error, match="exact-set mismatch"):
        load_package(copied)


def test_immediate_leader_exit_detached_child_is_reaped() -> None:
    child_code = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    parent_code = (
        "import subprocess,sys; "
        "p=subprocess.Popen([sys.executable,'-c',%r],start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
        "print(p.pid,flush=True)"
    ) % child_code
    result = run_bounded_process(
        [sys.executable, "-c", parent_code], b"", env_overrides={},
        timeout_seconds=2, max_stdout_bytes=1024, max_stderr_bytes=1024,
    )
    child_pid = int(result.stdout.strip())
    assert not Path(f"/proc/{child_pid}").exists()


def test_transport_uses_captured_host_and_selected_public_key(tmp_path: Path) -> None:
    approval, approval_path, evidence_root, _package = _approval(tmp_path)
    original_known_hosts = Path(approval["host_key"]["known_hosts_path"])
    probe_bytes = json.dumps(_probe(), sort_keys=True, separators=(",", ":")).encode() + b"\n"

    def runner(argv: list[str], *_args: object) -> TransportResult:
        assert _args[2] == 280
        joined = "\n".join(argv)
        assert f"UserKnownHostsFile={evidence_root / 'inputs' / 'known_hosts'}" in joined
        assert f"IdentityFile={evidence_root / 'inputs' / 'selected_public_key.pub'}" in joined
        original_known_hosts.write_text("replaced after capture\n", encoding="utf-8")
        assert (evidence_root / "inputs" / "known_hosts").read_bytes() != original_known_hosts.read_bytes()
        assert (evidence_root / "inputs" / "selected_public_key.pub").read_bytes().startswith(b"ssh-ed25519 ")
        return TransportResult(0, probe_bytes, b"", False, False, 1)

    result = _run(PACKAGE_ROOT, approval_path, evidence_root, runner=runner)
    assert result["terminal_status"] == "COMPLETE"


def test_terminal_verifier_rederives_semantic_result_identity(tmp_path: Path) -> None:
    _approval_value, approval_path, evidence_root, _package = _approval(tmp_path)
    probe_bytes = json.dumps(_probe(), sort_keys=True, separators=(",", ":")).encode() + b"\n"
    _run(PACKAGE_ROOT, approval_path, evidence_root, runner=lambda *_args: TransportResult(0, probe_bytes, b"", False, False, 1))

    for path in [evidence_root, *evidence_root.rglob("*")]:
        if path.is_dir():
            path.chmod(0o755)
        elif path.is_file():
            path.chmod(0o644)
    result_path = evidence_root / "d0_result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["reviewed_commit"] = "f" * 40
    result_payload = json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
    result_path.write_bytes(result_payload)
    ledger_path = evidence_root / "terminal_ledger.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    for item in ledger["members"]:
        if item["path"] == "d0_result.json":
            item["size_bytes"] = len(result_payload)
            item["sha256"] = hashlib.sha256(result_payload).hexdigest()
    unsigned = dict(ledger)
    unsigned.pop("ledger_sha256")
    ledger["ledger_sha256"] = semantic_sha256(unsigned)
    ledger_path.write_text(json.dumps(ledger, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    for path in [*evidence_root.rglob("*"), evidence_root]:
        path.chmod(0o555 if path.is_dir() else 0o444)
    with pytest.raises(D0R4Error, match="review authority identity mismatch|semantic identity mismatch"):
        verify_terminal(evidence_root)

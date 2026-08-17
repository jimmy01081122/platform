#!/usr/bin/env python3
"""Create one auditable Phase 7 CPU/mock framework-validation run."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SHA1_RE = re.compile(r"^[0-9a-f]{40}$")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def run(
    argv: list[str],
    *,
    cwd: Path,
    expected: int = 0,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    completed = subprocess.run(
        argv,
        cwd=cwd,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != expected:
        raise RuntimeError(
            f"command returned {completed.returncode}, expected {expected}: {argv!r}"
        )
    return completed


def resolve_source_identity(
    repo: Path,
    *,
    explicit_commit_sha1: str | None,
    explicit_tree_sha1: str | None,
) -> tuple[str, str, str]:
    """Resolve a Git source identity or require an exact archive binding."""

    if (explicit_commit_sha1 is None) != (explicit_tree_sha1 is None):
        raise ValueError("source commit and tree SHA-1 must be supplied together")
    if explicit_commit_sha1 is not None and explicit_tree_sha1 is not None:
        if SHA1_RE.fullmatch(explicit_commit_sha1) is None:
            raise ValueError("source commit SHA-1 must be 40 lowercase hex characters")
        if SHA1_RE.fullmatch(explicit_tree_sha1) is None:
            raise ValueError("source tree SHA-1 must be 40 lowercase hex characters")
        observed_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if observed_commit.returncode == 0:
            commit_sha1 = observed_commit.stdout.decode("utf-8").strip()
            tree_sha1 = run(
                ["git", "rev-parse", "HEAD^{tree}"], cwd=repo
            ).stdout.decode("utf-8").strip()
            if commit_sha1 != explicit_commit_sha1 or tree_sha1 != explicit_tree_sha1:
                raise ValueError("explicit source identity does not match Git checkout")
            return commit_sha1, tree_sha1, "EXPLICIT_VERIFIED_GIT_CHECKOUT"
        return explicit_commit_sha1, explicit_tree_sha1, "EXPLICIT_ARCHIVE_BINDING"

    commit_sha1 = run(
        ["git", "rev-parse", "HEAD"], cwd=repo
    ).stdout.decode("utf-8").strip()
    tree_sha1 = run(
        ["git", "rev-parse", "HEAD^{tree}"], cwd=repo
    ).stdout.decode("utf-8").strip()
    if SHA1_RE.fullmatch(commit_sha1) is None or SHA1_RE.fullmatch(tree_sha1) is None:
        raise ValueError("Git returned a malformed source identity")
    return commit_sha1, tree_sha1, "GIT_CHECKOUT"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-commit-sha1")
    parser.add_argument("--source-tree-sha1")
    args = parser.parse_args()

    repo = args.repo_root.resolve(strict=True)
    source_commit_sha1, source_tree_sha1, source_identity_method = (
        resolve_source_identity(
            repo,
            explicit_commit_sha1=args.source_commit_sha1,
            explicit_tree_sha1=args.source_tree_sha1,
        )
    )
    sys.path.insert(0, str(repo))
    from explorations.moe_cycle_simulator.phase7.promotion import (
        build_artifact_ledger,
        verify_artifact_ledger,
    )

    output = args.output.resolve()
    if output.exists():
        raise SystemExit("refusing to overwrite an existing run")
    output.mkdir(parents=True)
    logs = output / "logs"
    artifacts = output / "artifacts"
    environment_dir = output / "environment"
    logs.mkdir()
    artifacts.mkdir()
    environment_dir.mkdir()

    phase7 = repo / "explorations/moe_cycle_simulator/phase7"
    application = phase7 / "application"
    fixture = phase7 / "fixtures/mock_vllm_trace.json"
    adapter_output = artifacts / "mock_adapter_output.json"
    commands: list[dict[str, Any]] = []

    def checked(
        name: str,
        argv: list[str],
        *,
        expected: int = 0,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        completed = subprocess.run(
            argv,
            cwd=repo,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        (logs / f"{name}.stdout.log").write_bytes(completed.stdout)
        (logs / f"{name}.stderr.log").write_bytes(completed.stderr)
        commands.append(
            {
                "name": name,
                "argv": argv,
                "expected_returncode": expected,
                "observed_returncode": completed.returncode,
            }
        )
        if completed.returncode != expected:
            raise RuntimeError(
                f"command returned {completed.returncode}, expected {expected}: {argv!r}"
            )
        return completed

    try:
        unit_result = checked(
            "unit_tests",
            [
                sys.executable,
                "-m",
                "unittest",
                "discover",
                "-v",
                "-s",
                str(phase7 / "tests"),
                "-p",
                "test_*.py",
            ],
        )
        match = re.search(rb"Ran ([0-9]+) tests?", unit_result.stderr)
        if match is None:
            raise RuntimeError("cannot derive unittest count")
        unit_test_count = int(match.group(1))
        checked(
            "adapter_convert",
            [
                sys.executable,
                str(phase7 / "adapters/vllm_mock_adapter.py"),
                "--input",
                str(fixture),
                "--output",
                str(adapter_output),
            ],
        )
        checked(
            "adapter_replay",
            [
                sys.executable,
                str(phase7 / "adapters/vllm_mock_adapter.py"),
                "--input",
                str(fixture),
                "--validate-output",
                str(adapter_output),
            ],
        )
        checked(
            "application_draft",
            [
                sys.executable,
                str(application / "validate_application.py"),
                "--mode",
                "draft",
                "--application-dir",
                str(application),
            ],
        )
        disclosure_rejected = checked(
            "application_disclosure_ready_negative",
            [
                sys.executable,
                str(application / "validate_application.py"),
                "--mode",
                "disclosure-ready",
                "--application-dir",
                str(application),
            ],
            expected=1,
        )
        if b"unresolved disclosure fields" not in disclosure_rejected.stderr:
            raise RuntimeError("disclosure-ready rejection was not caused by blockers")
        materialization_rejected = checked(
            "application_materialization_ready_negative",
            [
                sys.executable,
                str(application / "validate_application.py"),
                "--mode",
                "materialization-ready",
                "--application-dir",
                str(application),
            ],
            expected=1,
        )
        if b"unresolved materialization fields" not in materialization_rejected.stderr:
            raise RuntimeError(
                "materialization-ready rejection was not caused by blockers"
            )
        gate_m_rejected = checked(
            "application_gate_m_ready_negative",
            [
                sys.executable,
                str(application / "validate_application.py"),
                "--mode",
                "gate-m-ready",
                "--application-dir",
                str(application),
                "--external-approval",
                str(application / "deployment_approval.external.template.json"),
            ],
            expected=1,
        )
        if b"unresolved Gate M fields" not in gate_m_rejected.stderr:
            raise RuntimeError("Gate M rejection was not caused by blockers")
        rejected = checked(
            "application_execution_ready_negative",
            [
                sys.executable,
                str(application / "validate_application.py"),
                "--mode",
                "execution-ready",
                "--application-dir",
                str(application),
            ],
            expected=1,
        )
        if b"unresolved blocking fields" not in rejected.stderr:
            raise RuntimeError("execution-ready rejection was not caused by blockers")
        checked(
            "preflight_locked_negative",
            ["bash", str(application / "preflight_m0.template.sh"), str(application)],
            expected=64,
            environment={
                key: value
                for key, value in os.environ.items()
                if key not in {"MOE_PHASE7_EXECUTION_UNLOCK", "CUDA_VISIBLE_DEVICES"}
            },
        )
        checked(
            "runner_locked_negative",
            [
                "bash",
                str(application / "run_m0.template.sh"),
                str(application),
                str(output / "forbidden-remote-output"),
            ],
            expected=64,
            environment={
                key: value
                for key, value in os.environ.items()
                if key not in {"MOE_PHASE7_EXECUTION_UNLOCK", "CUDA_VISIBLE_DEVICES"}
            },
        )
        checked(
            "materializer_locked_negative",
            [
                "bash",
                str(application / "materialize_m0.template.sh"),
                str(application),
            ],
            expected=64,
            environment={
                key: value
                for key, value in os.environ.items()
                if key
                not in {
                    "MOE_PHASE7_MATERIALIZATION_UNLOCK",
                    "MOE_PHASE7_EXECUTION_UNLOCK",
                    "CUDA_VISIBLE_DEVICES",
                }
            },
        )
        checked(
            "disclosure_locked_negative",
            [
                "bash",
                str(application / "disclose_environment.template.sh"),
                str(application),
                str(output / "forbidden-d0-output"),
            ],
            expected=64,
            environment={
                key: value
                for key, value in os.environ.items()
                if key not in {"MOE_PHASE7_D0_UNLOCK", "CUDA_VISIBLE_DEVICES"}
            },
        )
        checked(
            "gate_m_deployment_locked_negative",
            [
                "bash",
                str(application / "deploy_gate_m.template.sh"),
                str(application),
                str(application / "deployment_approval.external.template.json"),
                str(output / "forbidden-gate-m-output"),
            ],
            expected=64,
            environment={
                key: value
                for key, value in os.environ.items()
                if key not in {"MOE_PHASE7_DEPLOYMENT_UNLOCK", "CUDA_VISIBLE_DEVICES"}
            },
        )
        checked(
            "preflight_shell_syntax",
            ["bash", "-n", str(application / "preflight_m0.template.sh")],
        )
        checked(
            "runner_shell_syntax",
            ["bash", "-n", str(application / "run_m0.template.sh")],
        )
        checked(
            "materializer_shell_syntax",
            ["bash", "-n", str(application / "materialize_m0.template.sh")],
        )
        checked(
            "disclosure_shell_syntax",
            ["bash", "-n", str(application / "disclose_environment.template.sh")],
        )
        checked(
            "gate_m_deployment_shell_syntax",
            ["bash", "-n", str(application / "deploy_gate_m.template.sh")],
        )
        checked(
            "application_package_ledger",
            [
                sys.executable,
                str(application / "executor/package_ledger.py"),
                "--root",
                str(application),
                "--output",
                str(artifacts / "application_package_ledger.json"),
            ],
        )
        status = "PASS"
        failure = None
    except Exception as exc:  # Preserve a failed run instead of overwriting it.
        status = "FAIL"
        failure = str(exc)

    config = repo / "experiments/specs/moe_cycle_simulator_phase7_cpu_framework.yaml"
    (output / "resolved_config.yaml").write_bytes(config.read_bytes())
    write_json(logs / "command.log", {"commands": commands})
    (logs / "stdout.log").write_text(
        f"phase7_cpu_framework_status={status}\n", encoding="utf-8"
    )
    (logs / "stderr.log").write_text(
        "" if failure is None else failure + "\n", encoding="utf-8"
    )

    adapter_value = (
        json.loads(adapter_output.read_text(encoding="utf-8"))
        if adapter_output.exists()
        else None
    )
    metrics = {
        "schema_version": "moe-phase7-cpu-framework-metrics-v1",
        "status": status,
        "evidence_class": "SYNTHETIC_CPU_MOCK",
        "unit_tests": {"passed": unit_test_count, "failed": 0}
        if status == "PASS"
        else None,
        "adapter_output_root": (
            adapter_value["semantic_hashes"]["adapter_output_root"]
            if adapter_value is not None
            else None
        ),
        "application_draft": "PASS_FAIL_CLOSED" if status == "PASS" else None,
        "environment_disclosure_ready": "REJECTED_UNRESOLVED_BLOCKERS"
        if status == "PASS"
        else None,
        "allocation_contract": "SIX_HOUR_PREPAID_NO_EXTENSION_ZERO_ADDITIONAL_COST"
        if status == "PASS"
        else None,
        "persistent_storage_contract": "VAULT_REQUIRED"
        if status == "PASS"
        else None,
        "stage_envelope_contract": "21000_OF_21600_SECONDS_INCLUDING_KILL_GRACE"
        if status == "PASS"
        else None,
        "d0_process_containment": "CPU_ADVERSARIAL_PASS"
        if status == "PASS"
        else None,
        "d0_terminal_exact_set_replay": "PASS"
        if status == "PASS"
        else None,
        "materialization_ready": "REJECTED_UNRESOLVED_BLOCKERS"
        if status == "PASS"
        else None,
        "gate_m_ready": "REJECTED_UNRESOLVED_BLOCKERS"
        if status == "PASS"
        else None,
        "gate_m_single_ssh_bounded_export_replay": "CPU_INTEGRATION_PASS"
        if status == "PASS"
        else None,
        "m0_parent_gate": "FAIL_CLOSED_PENDING_COMPLETE_M0_ELIGIBLE_PARENT"
        if status == "PASS"
        else None,
        "execution_ready": "REJECTED_UNRESOLVED_BLOCKERS"
        if status == "PASS"
        else None,
        "gpu_used": False,
        "gpu_queried": False,
        "model_downloaded": False,
        "network_used": False,
        "formal_promotion": False,
        "full_run_ledger": "CANONICAL_V0_EXACT_SET",
        "failure": failure,
    }
    write_json(output / "metrics.json", metrics)

    write_json(
        environment_dir / "tool_versions.json",
        {
            "python": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "source_commit_sha1": source_commit_sha1,
            "source_tree_sha1": source_tree_sha1,
            "source_identity_method": source_identity_method,
            "gpu_inventory_queried": False,
        },
    )
    write_json(
        output / "manifest.json",
        {
            "schema_version": "edgeflow-run-manifest-v1",
            "experiment_id": "moe_cycle_simulator_phase7_cpu_framework",
            "stage": "S2",
            "status": status,
            "started_and_completed_at_utc": datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
            "evidence_class": "SYNTHETIC_CPU_MOCK",
            "source_revision_before_checkpoint": source_commit_sha1,
            "source_tree_sha1": source_tree_sha1,
            "source_identity_method": source_identity_method,
            "resolved_config": "resolved_config.yaml",
            "metrics": "metrics.json",
            "artifact_ledger": "artifacts/artifact_ledger.json",
            "authority": "CPU_MOCK_FRAMEWORK_VALIDATION_ONLY",
            "gpu_authority": "NONE",
            "failure_class": None if status == "PASS" else "CPU_VALIDATION_FAILURE",
            "claim_boundary": (
                "Validates local Phase 7 contracts and safety gates only; it is not "
                "Mixtral, vLLM, GPU, measured observability, or formal promotion evidence."
            ),
        },
    )
    ledger_path = artifacts / "artifact_ledger.json"
    relative_files = [
        path.relative_to(output).as_posix()
        for path in sorted(output.rglob("*"))
        if path.is_file() and path != ledger_path
    ]
    ledger_session_id = re.sub(r"[^a-z0-9._-]", "-", output.name.lower())
    ledger = build_artifact_ledger(
        output,
        relative_files,
        stage="V0",
        session_id=ledger_session_id,
    )
    write_json(ledger_path, ledger)
    verify_artifact_ledger(output, ledger)
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

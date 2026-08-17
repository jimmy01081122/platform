from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
APPLICATION = REPO / "explorations/moe_cycle_simulator/phase7/application"
sys.path.insert(0, str(REPO))

from explorations.moe_cycle_simulator.phase7.application.executor.allocation import (  # noqa: E402
    D0_STAGE_SECONDS,
    M0_STAGE_SECONDS,
    MATERIALIZATION_STAGE_SECONDS,
    RELEASE_RESERVE_SECONDS,
    TOTAL_ALLOCATION_SECONDS,
    require_remaining_budget,
    validate_allocation_window,
)
from explorations.moe_cycle_simulator.phase7.application.executor.disclosure_driver import (  # noqa: E402
    run_ssh,
)
from explorations.moe_cycle_simulator.phase7.application.executor.storage_identity import (  # noqa: E402
    mount_identity,
    validate_mount_identity,
)
from explorations.moe_cycle_simulator.phase7.application.executor.common import (  # noqa: E402
    M0Error,
    load_json,
    semantic_sha256,
    write_new_json,
)
from explorations.moe_cycle_simulator.phase7.application.executor.authority import (  # noqa: E402
    retain_authority,
)
from explorations.moe_cycle_simulator.phase7.application.executor.d0_finalize import (  # noqa: E402
    seal_d0_terminal,
    verify_d0_terminal,
)
from explorations.moe_cycle_simulator.phase7.application.executor.package_ledger import (  # noqa: E402
    build as build_application_ledger,
)
from explorations.moe_cycle_simulator.phase7.application.executor.disclosure import (  # noqa: E402
    build_ssh_argv,
    classify_environment,
    strict_probe_json,
)


def allocation() -> dict:
    return {
        "start_trigger": "OWNER_RELEASES_FRESH_SSH_HANDOFF",
        "lease_start_utc": "2026-07-29T01:00:00Z",
        "lease_deadline_utc": "2026-07-29T07:00:00Z",
        "total_seconds": 21600,
        "billing_mode": "PREPAID_FIXED_WINDOW",
        "extension_allowed": False,
        "additional_cost_allowed": False,
        "maximum_additional_spend_amount": "0",
        "maximum_additional_spend_currency": "TWD",
        "release_reserve_seconds": 900,
    }


def mount(path: str, *, free: int, is_mount: bool = True) -> dict:
    return {
        "path": path,
        "exists": True,
        "realpath": path,
        "is_mount": is_mount,
        "is_symlink": False,
        "device_id": 1,
        "mode_octal": "0755",
        "owner_uid": 0,
        "owner_gid": 0,
        "total_bytes": 1_000_000_000_000,
        "free_bytes": free,
        "mount_identity": {
            "mount_id": 1,
            "parent_id": 0,
            "major_minor": "0:1",
            "root": "/",
            "mount_point": path,
            "mount_options": ["rw"],
            "filesystem_type": "ext4",
            "mount_source": "/dev/test",
            "super_options": ["rw"],
            "device_id": 1,
            "boot_id": "test",
            "mountinfo_sha256": "a" * 64,
            "mount_identity_sha256": "b" * 64,
        },
    }


def probe() -> dict:
    return {
        "schema_version": "moe-simulator-phase7-d0-probe-result-v1",
        "capture_status": "COMPLETE",
        "captured_at_utc": "2026-07-29T01:00:01Z",
        "host": {},
        "gpu": {
            "query_status": "COMPLETE",
            "device_count": 1,
            "devices": [{
                "name": "NVIDIA RTX PRO 6000 Blackwell Workstation Edition",
                "total_memory_bytes": 100_000_000_000,
            }],
            "command": {},
        },
        "mounts": {
            "persistent": mount("/vault", free=400_000_000_000),
            "ephemeral": mount("/workspace", free=100_000_000_000),
        },
        "software": {
            "packages": {
                "vllm": "1",
                "torch": "1",
                "transformers": "1",
                "huggingface_hub": "1",
                "tokenizers": "1",
            },
            "commands": {"nvcc": "/usr/local/cuda/bin/nvcc"},
            "container_digest_attestation": "sha256:" + "a" * 64,
        },
        "environment_presence": {},
        "prohibitions": {
            "remote_file_write_performed": False,
            "download_performed": False,
            "install_performed": False,
            "model_access_performed": False,
            "gpu_workload_performed": False,
            "secret_values_recorded": False,
        },
    }


class EnvironmentDisclosureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = load_json(
            APPLICATION / "environment_disclosure_plan.template.json"
        )
        self.plan["ssh"].update({
            "executable": "/usr/bin/ssh",
            "host": "example.invalid",
            "username": "owner",
            "known_hosts_file": "/tmp/dedicated-known-hosts",
        })
        self.plan["allocation_window"] = allocation()
        self.plan["probe"]["remote_argv"][1] = (
            "MOE_PHASE7_CONTAINER_DIGEST=sha256:" + "a" * 64
        )

    def test_six_hour_window_and_stage_budget(self) -> None:
        start, deadline = validate_allocation_window(allocation())
        self.assertEqual(int((deadline - start).total_seconds()), 21600)
        remaining = require_remaining_budget(
            allocation(),
            stage_outer_seconds=300,
            downstream_reserve_seconds=19800,
            now=datetime(2026, 7, 29, 1, 9, 59, tzinfo=timezone.utc),
        )
        self.assertEqual(remaining, 21001)
        self.assertLessEqual(
            D0_STAGE_SECONDS
            + MATERIALIZATION_STAGE_SECONDS
            + M0_STAGE_SECONDS
            + RELEASE_RESERVE_SECONDS,
            TOTAL_ALLOCATION_SECONDS,
        )
        with self.assertRaises(M0Error):
            require_remaining_budget(
                allocation(),
                stage_outer_seconds=300,
                downstream_reserve_seconds=19800,
                now=datetime(2026, 7, 29, 1, 10, 1, tzinfo=timezone.utc),
            )

    def test_allocation_rejects_extension_or_extra_cost(self) -> None:
        for field, value in (
            ("extension_allowed", True),
            ("additional_cost_allowed", True),
            ("maximum_additional_spend_amount", "1"),
        ):
            changed = allocation()
            changed[field] = value
            with self.assertRaises(M0Error):
                validate_allocation_window(changed)

    def test_ssh_argv_ignores_user_config_and_forwards_nothing(self) -> None:
        argv = build_ssh_argv(self.plan)
        self.assertEqual(argv[1:3], ["-F", "/dev/null"])
        joined = "\n".join(argv)
        for expected in (
            "StrictHostKeyChecking=yes",
            "ClearAllForwardings=yes",
            "ForwardAgent=no",
            "ProxyCommand=none",
            "ProxyJump=none",
        ):
            self.assertIn(expected, joined)
        self.assertEqual(argv[-4:], ["python3", "-I", "-B", "-"])
        self.assertIn("MOE_PHASE7_CONTAINER_DIGEST=sha256:" + "a" * 64, argv)
        self.assertIn(
            "[BLOCKING:FRESH_ABSOLUTE_LOCAL_D0_EVIDENCE_ROOT]/disclosure_inputs/known_hosts",
            joined,
        )

    def test_stage_wrappers_include_kill_grace_inside_envelope(self) -> None:
        expected = {
            "disclose_environment.template.sh": "timeout --signal=TERM --kill-after=60s 240",
            "materialize_m0.template.sh": "timeout --signal=TERM --kill-after=600s 4800",
            "run_m0.template.sh": "timeout --signal=TERM --kill-after=1200s 13200",
        }
        for name, command in expected.items():
            self.assertIn(command, (APPLICATION / name).read_text(encoding="utf-8"))

    def test_strict_probe_rejects_float_duplicate_and_banner(self) -> None:
        valid = json.dumps(probe(), sort_keys=True).encode()
        self.assertEqual(strict_probe_json(valid)["capture_status"], "COMPLETE")
        for invalid in (
            b'{"x":1.0}',
            b'{"x":1,"x":2}',
            b'banner\\n{}',
        ):
            with self.assertRaises(M0Error):
                strict_probe_json(invalid)

    def test_eligibility_is_separate_from_disclosure_completion(self) -> None:
        status, findings = classify_environment(probe(), self.plan)
        self.assertEqual(status, "READY_FOR_MATERIALIZATION_APPLICATION")
        self.assertEqual(findings, [])
        missing_runtime = copy.deepcopy(probe())
        missing_runtime["software"]["packages"]["vllm"] = None
        status, findings = classify_environment(missing_runtime, self.plan)
        self.assertEqual(status, "NOT_READY")
        self.assertIn("VLLM_NOT_INSTALLED", findings)
        for package, finding in (
            ("transformers", "TRANSFORMERS_NOT_INSTALLED"),
            ("huggingface_hub", "HUGGINGFACE_HUB_NOT_INSTALLED"),
            ("tokenizers", "TOKENIZERS_NOT_INSTALLED"),
        ):
            missing = copy.deepcopy(probe())
            missing["software"]["packages"][package] = None
            status, findings = classify_environment(missing, self.plan)
            self.assertEqual(status, "NOT_READY")
            self.assertIn(finding, findings)
        missing_nvcc = copy.deepcopy(probe())
        missing_nvcc["software"]["commands"]["nvcc"] = None
        status, findings = classify_environment(missing_nvcc, self.plan)
        self.assertEqual(status, "NOT_READY")
        self.assertIn("NVCC_NOT_INSTALLED", findings)

    def test_vault_must_be_real_mount_with_capacity(self) -> None:
        for changed, expected in (
            ({"is_mount": False}, "VAULT_MOUNT_NOT_CONFIRMED"),
            ({"free_bytes": 1}, "VAULT_FREE_SPACE_BELOW_FLOOR"),
        ):
            value = probe()
            value["mounts"]["persistent"].update(changed)
            status, findings = classify_environment(value, self.plan)
            self.assertEqual(status, "NOT_READY")
            self.assertIn(expected, findings)

    def test_mount_identity_rejects_drift(self) -> None:
        observed = mount_identity("/")
        self.assertEqual(
            validate_mount_identity("/", observed["mount_identity_sha256"]),
            observed,
        )
        with self.assertRaises(M0Error):
            validate_mount_identity("/", "0" * 64)

    def test_d0_timeout_contains_setsid_descendant(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pid_path = Path(directory) / "pid"
            code = (
                "import pathlib,subprocess,time;"
                f"p=subprocess.Popen(['sleep','60'],start_new_session=True);"
                f"pathlib.Path({str(pid_path)!r}).write_text(str(p.pid));"
                "time.sleep(60)"
            )
            returncode, _stdout, _stderr, timed_out, _elapsed = run_ssh(
                [sys.executable, "-c", code],
                probe_source=b"",
                timeout_seconds=1,
            )
            self.assertTrue(timed_out)
            self.assertNotEqual(returncode, 0)
            pid = int(pid_path.read_text(encoding="utf-8"))
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_d0_failure_is_exact_set_sealed_and_replayable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "phase7-d0-failure-test"
            root.mkdir()
            write_new_json(
                root / "d0_failure.json",
                {
                    "schema_version": "moe-simulator-phase7-d0-failure-v1",
                    "disclosure_status": "INCOMPLETE",
                    "failure_type": "D0Interrupted",
                    "failure": "test",
                    "controller_start_utc": "2026-07-29T01:00:00Z",
                    "controller_end_utc": "2026-07-29T01:00:01Z",
                    "elapsed_monotonic_ns": 1,
                    "authority_evidence_sha256": None,
                    "retry_allowed": False,
                    "resume_allowed": False,
                    "gpu_workload_performed": False,
                },
            )
            seal_d0_terminal(root, "INCOMPLETE")
            ledger = verify_d0_terminal(root)
            self.assertEqual(ledger["terminal_status"], "INCOMPLETE")
            self.assertEqual(
                (root / "d0_status.txt").read_text(encoding="utf-8"),
                "D0_INCOMPLETE_IMMUTABLE_NO_RETRY\n",
            )
            root.chmod(0o700)

    def test_d0_complete_binds_retained_authority_and_detects_tamper(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            root = temporary / "phase7-d0-complete-test"
            root.mkdir()
            package = build_application_ledger(APPLICATION)
            approval = load_json(
                APPLICATION / "environment_disclosure_approval.template.json"
            )
            approval["application_ledger_sha256"] = package["ledger_sha256"]
            approval_path = temporary / "approval.json"
            approval_path.write_text(
                json.dumps(approval, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            approval_bytes = approval_path.read_bytes()
            registry = temporary / "used.json"
            registry.write_text(
                json.dumps(
                    {
                        "schema_version": "test",
                        "approval_id": approval["approval_id"],
                        "approval_token_sha256": approval["approval_token_sha256"],
                        "approval_file_sha256": hashlib.sha256(approval_bytes).hexdigest(),
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            authority = retain_authority(
                application=APPLICATION,
                approval_path=approval_path,
                registry_path=registry,
                evidence_root=root,
                expected_application_ledger_sha256=package["ledger_sha256"],
                approval_bytes=approval_bytes,
                package_ledger=package,
            )
            write_new_json(
                root / "d0_result.json",
                {
                    "schema_version": "moe-simulator-phase7-d0-result-v1",
                    "application_id": "test",
                    "disclosure_session_id": "phase7-d0-complete-test",
                    "disclosure_status": "COMPLETE",
                    "environment_eligibility": "NOT_READY",
                    "eligibility_findings": ["TEST"],
                    "authority_evidence_sha256": semantic_sha256(authority),
                    "plan_file_sha256": "a" * 64,
                    "approval_file_sha256": "b" * 64,
                    "probe_file_sha256": "c" * 64,
                    "exact_ssh_argv_sha256": "d" * 64,
                    "timing": {
                        "controller_start_utc": "2026-07-29T01:00:00Z",
                        "controller_end_utc": "2026-07-29T01:00:01Z",
                        "elapsed_monotonic_ns": 1,
                        "lease_start_utc": "2026-07-29T01:00:00Z",
                        "lease_deadline_utc": "2026-07-29T07:00:00Z",
                        "ssh_elapsed_monotonic_ns": 1,
                    },
                    "ssh": {
                        "endpoint": {
                            "host": "example.invalid",
                            "port": 2222,
                            "username": "owner",
                        },
                        "host_public_key_blob_sha256": "1" * 64,
                        "returncode": 0,
                        "stdout_sha256": "2" * 64,
                        "stderr_sha256": "3" * 64,
                    },
                    "probe_result_sha256": "e" * 64,
                    "vault_mount_identity_sha256": "f" * 64,
                    "prohibitions": {},
                    "next_legal_action": "TEST",
                },
            )
            seal_d0_terminal(root, "COMPLETE")
            verify_d0_terminal(root)
            target = root / "d0_result.json"
            target.chmod(0o600)
            target.write_text("{}\n", encoding="utf-8")
            with self.assertRaises(M0Error):
                verify_d0_terminal(root)
            root.chmod(0o700)


if __name__ == "__main__":
    unittest.main()

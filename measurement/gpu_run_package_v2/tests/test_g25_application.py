from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import jsonschema

from scheduler.g25_application import (
    APPROVAL_SCHEMA_PATH,
    BF16_PROBE_TIMEOUT_SECONDS,
    NVIDIA_SMI_TIMEOUT_SECONDS,
    ApprovalExpectations,
    ApprovalValidationError,
    DynamicPreflightError,
    DecisionRecordValidationError,
    canonical_hash,
    load_and_validate_approval,
    load_and_validate_evaluation_record,
    load_and_validate_review_record,
    run_dynamic_preflight,
    query_dynamic_preflight,
    validate_approval_record,
    validate_dynamic_preflight,
    verify_runtime_inventory,
    _exact_tree_fingerprint,
    _verify_exact_tree,
)
from scheduler.g25_runtime_closure import build_attested_python_argv


HEX64 = "a" * 64
HEX40 = "b" * 40


def approval_fixture() -> dict:
    argv = [
        "python3", "scripts/projectctl.py", "qualification", "start",
        "--approval-record", "/approval.json", "--model-snapshot", "/snapshot",
    ]
    binding_keys = (
        "package_checksum_ledger_sha256", "package_manifest_sha256",
        "runtime_inventory_sha256", "requirements_lock_sha256",
        "pilot_session_contract_sha256", "matrix_sha256",
        "generation_profile_sha256", "expected_artifacts_sha256",
        "qualification_runner_sha256", "qualification_worker_sha256",
        "isolated_bootstrap_sha256",
        "snapshot_inventory_sha256", "model_runtime_payload_contract_sha256",
        "model_snapshot_verifier_sha256",
        "model_snapshot_payload_identity_sha256",
        "model_snapshot_files_inventory_sha256",
        "c1_evaluator_sha256", "benchmark_quality_sha256",
        "sample_manifest_sha256",
        "approval_schema_sha256", "application_runner_sha256",
        "application_scheduler_sha256", "deadline_tracker_sha256",
        "system_closure_sha256", "runtime_closure_verifier_sha256",
        "worker_lifetime_guard_sha256",
        "session_store_sha256", "snapshot_auditor_sha256",
        "worker_descriptor_schema_sha256", "same_source_review_schema_sha256",
        "evaluation_schema_sha256", "r3_session_sha256", "r4_session_sha256",
        "terminal_schema_sha256", "final_seal_schema_sha256",
        "parent_output_replay_schema_sha256",
        "session_file_inventory_schema_sha256",
        "external_seal_anchor_schema_sha256",
        "r4_suite_snapshot_sha256", "r4_journal_sha256",
        "r4_failed_state_sha256", "r4_failure_quality_sha256",
        "same_source_review_sha256", "evaluation_record_sha256",
    )
    bindings = {
        key: format(index % 16, "x") * 64
        for index, key in enumerate(binding_keys)
    }
    return {
        "schema_version": "g25-gpu-pilot-owner-approval-v1",
        "approval_id": "owner-g25-r1-fixture",
        "decision": "APPROVE_EXACT_G25_GPU_PILOT_COMMAND",
        "approved_by": "fixture-owner",
        "issued_at_epoch": 100.0,
        "expires_at_epoch": 200.0,
        "session_id": "granite-c1a-g25-qualification-r1-20260719",
        "exact_command": {"argv": argv, "argv_sha256": canonical_hash(argv)},
        "review_target": {
            "annotated_tag": "g25-review-fixture", "tag_object": "a" * 40,
            "commit": HEX40, "tree": "c" * 40, "package_tree": "d" * 40,
        },
        "review": {
            "document_sha256": bindings["same_source_review_sha256"],
            "architecture": "GO", "model": "GO", "trace": "GO", "blockers": [],
        },
        "evaluation_gate": {
            "evaluator": "5.6sol", "evaluator_model": "gpt-5.6-sol",
            "document_sha256": bindings["evaluation_record_sha256"],
            "verdict": "GO", "blockers": [],
        },
        "bindings": bindings,
        "hardware": {
            "name": "NVIDIA GeForce RTX 3050",
            "uuid": "GPU-4d160805-02d8-24aa-ef6a-2685832658a3",
            "pci_bus_id": "00000000:01:00.0",
            "total_vram_bytes": 6442450944,
            "minimum_free_vram_bytes": 5000000000,
            "minimum_free_disk_bytes": 8589934592,
            "precision": "bf16",
        },
        "runtime": runtime_fixture(),
        "environment": {
            "cuda_visible_devices": "GPU-4d160805-02d8-24aa-ef6a-2685832658a3",
            "hf_hub_offline": "1", "transformers_offline": "1",
            "cublas_workspace_config": ":4096:8", "cuda_launch_blocking": "1",
            "pythonhashseed": "0", "lc_all": "C", "lang": "C",
        },
        "deadlines": {
            "cell_timeout_seconds": 480, "term_grace_seconds": 30,
            "latest_new_dispatch_elapsed_seconds": 5790,
            "execution_cutoff_elapsed_seconds": 6300,
            "session_hard_deadline_seconds": 7200,
            "outer_command_timeout_seconds": 7500,
            "outer_kill_after_seconds": 30,
        },
        "scope": {
            "logical_pass": "P0_ONLY", "expected_cells": 12,
            "ceilings": [256, 384, 512], "fresh_session": True,
            "resume": False, "retry_failed": False, "routing_capture": False,
            "profiler": False, "formal_c1_evidence": False,
            "formal_g3_r5": False, "paid_gpu": False, "archive_release": False,
        },
    }


def expectations(record: dict) -> ApprovalExpectations:
    return ApprovalExpectations(
        argv=tuple(record["exact_command"]["argv"]),
        annotated_tag=record["review_target"]["annotated_tag"],
        tag_object=record["review_target"]["tag_object"],
        commit=record["review_target"]["commit"], tree=record["review_target"]["tree"],
        package_tree=record["review_target"]["package_tree"],
        bindings=record["bindings"], session_id=record["session_id"],
    )


def facts_fixture() -> dict:
    return {
        "gpus": [{
            "index": 0, "name": "NVIDIA GeForce RTX 3050",
            "uuid": "GPU-4d160805-02d8-24aa-ef6a-2685832658a3",
            "pci_bus_id": "00000000:01:00.0", "total_vram_bytes": 6442450944,
            "free_vram_bytes": 6000000000, "bf16_supported": True,
        }],
        "compute_processes": [], "disk_free_bytes": 10 * 1024**3,
        "cuda_visible_devices": "GPU-4d160805-02d8-24aa-ef6a-2685832658a3",
        "runtime": runtime_fixture(),
        "offline": {"hf_hub_offline": "1", "transformers_offline": "1"},
        "determinism": {
            "cublas_workspace_config": ":4096:8", "cuda_launch_blocking": "1",
            "pythonhashseed": "0", "lc_all": "C", "lang": "C",
            "torch_deterministic_algorithms": True,
            "matmul_allow_tf32": False, "cudnn_allow_tf32": False,
            "cudnn_benchmark": False, "cudnn_deterministic": True,
            "bf16_reduced_precision_reduction": False,
            "fp16_reduced_precision_reduction": False,
        },
    }


def runtime_fixture() -> dict:
    root = (
        "/home/a/flow/edge_hetero_exploration_workspace/"
        "edge_hetero_exploration_workspace/gpu_run_package_v2/.benchmark-runtime"
    )
    return {
        "python": "3.10.12",
        "torch": "2.7.1+cu128",
        "transformers": "4.47.0",
        "pyyaml": "6.0.2",
        "jsonschema": "4.24.0",
        "cuda": "12.8",
        "python_executable": "/usr/bin/python3",
        "python_realpath": "/usr/bin/python3.10",
        "pythonpath": None,
        "python_no_user_site": "1",
        "python_dont_write_bytecode": "1",
        "python_isolated": True,
        "python_no_site": True,
        "python_ignore_environment": True,
        "runtime_inventory_sha256": (
            "c3282bb8a6531ac442172278489eff391b0c5d042f610203081930ba873792d3"
        ),
        "requirements_lock_sha256": (
            "88f36bc6a9af2e78f6ef1d744b512d2ef093769a2e252667a16bd842de523fed"
        ),
        "runtime_tree_sha256": (
            "179cc8fa7f0598b956e77ffcecf9f336ebc7ddd61c785c48e28d0a39140b4625"
        ),
        "stdlib_tree_sha256": (
            "fabc15b299143df7560a170b358a671347984d808b49efe61b06483f6dc12e5e"
        ),
        "driver_runtime_tree_sha256": (
            "eacd617d22d8762d600624820796d3d851a2002ac641868213084104251205b5"
        ),
        "system_files_sha256": (
            "f4c7e80e83400a789390a963e593dff3ee9ac6d72d5d168bef8ce0dce53c54ce"
        ),
        "system_closure_sha256": (
            "cb81a3c638df8a5e6ed32a7210f3f7d0140e8c982e816b135fc23ec6b7c19ed5"
        ),
        "static_dependency_edges_sha256": (
            "c497fd6849f348ef1a534a38e570750e46762759d999ead08b43f02329b7af2f"
        ),
        "module_files": {
            "jsonschema": f"{root}/jsonschema/__init__.py",
            "torch": f"{root}/torch/__init__.py",
            "transformers": f"{root}/transformers/__init__.py",
            "yaml": f"{root}/yaml/__init__.py",
        },
    }


class G25ApplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.approval = approval_fixture()
        self.expected = expectations(self.approval)

    def test_strict_schema_and_valid_record(self):
        schema = json.loads(APPROVAL_SCHEMA_PATH.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(schema).validate(self.approval)
        self.assertEqual(
            self.approval,
            validate_approval_record(self.approval, self.expected, now_epoch=150),
        )
        extra = copy.deepcopy(self.approval)
        extra["unreviewed_escape"] = True
        with self.assertRaises(ApprovalValidationError):
            validate_approval_record(extra, self.expected, now_epoch=150)

    def test_command_hash_and_actual_argv_are_both_authoritative(self):
        changed = copy.deepcopy(self.approval)
        changed["exact_command"]["argv"].append("--unreviewed")
        with self.assertRaisesRegex(ApprovalValidationError, "hash"):
            validate_approval_record(changed, self.expected, now_epoch=150)
        changed["exact_command"]["argv_sha256"] = canonical_hash(
            changed["exact_command"]["argv"]
        )
        with self.assertRaisesRegex(ApprovalValidationError, "invoked command"):
            validate_approval_record(changed, self.expected, now_epoch=150)

    def test_target_binding_and_review_hash_drift_fail_closed(self):
        changed_expected = ApprovalExpectations(
            argv=self.expected.argv, annotated_tag="different-tag",
            tag_object=self.expected.tag_object, commit=self.expected.commit,
            tree=self.expected.tree, package_tree=self.expected.package_tree,
            bindings=self.expected.bindings,
        )
        with self.assertRaisesRegex(ApprovalValidationError, "tag"):
            validate_approval_record(self.approval, changed_expected, now_epoch=150)
        changed = copy.deepcopy(self.approval)
        changed["review"]["document_sha256"] = "c" * 64
        with self.assertRaisesRegex(ApprovalValidationError, "review document"):
            validate_approval_record(changed, self.expected, now_epoch=150)

    def test_56sol_go_is_a_strict_required_gate(self):
        for gate in (
            {"evaluator": "5.6sol", "verdict": "NO-GO", "blockers": ["x"]},
            {"evaluator": "other", "verdict": "GO", "blockers": []},
        ):
            changed = copy.deepcopy(self.approval)
            changed["evaluation_gate"] = gate
            with self.assertRaises(ApprovalValidationError):
                validate_approval_record(changed, self.expected, now_epoch=150)

    def test_package_binding_drift_and_expiry_fail_closed(self):
        altered = dict(self.expected.bindings)
        altered["matrix_sha256"] = "f" * 64
        changed_expected = ApprovalExpectations(
            argv=self.expected.argv, annotated_tag=self.expected.annotated_tag,
            tag_object=self.expected.tag_object, commit=self.expected.commit,
            tree=self.expected.tree, package_tree=self.expected.package_tree,
            bindings=altered,
        )
        with self.assertRaisesRegex(ApprovalValidationError, "bindings"):
            validate_approval_record(self.approval, changed_expected, now_epoch=150)
        for now in (99, 200):
            with self.subTest(now=now), self.assertRaisesRegex(
                ApprovalValidationError, "currently valid"
            ):
                validate_approval_record(self.approval, self.expected, now_epoch=now)

    def test_file_loader_rejects_symlink_and_loads_regular_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = root / "approval.json"
            record.write_text(json.dumps(self.approval), encoding="utf-8")
            self.assertEqual(
                self.approval,
                load_and_validate_approval(record, self.expected, now_epoch=150),
            )
            link = root / "approval-link.json"
            link.symlink_to(record)
            with self.assertRaisesRegex(ApprovalValidationError, "non-symlink"):
                load_and_validate_approval(link, self.expected, now_epoch=150)

    def test_independent_review_and_56sol_records_are_parsed_and_same_hash_bound(self):
        target = dict(self.approval["review_target"])
        source_bindings = {
            key: value for key, value in self.approval["bindings"].items()
            if key not in {"same_source_review_sha256", "evaluation_record_sha256"}
        }
        package_identity = {
            "inventory_count": 221,
            "checksum_entry_count": 220,
            "checksums_sha256": source_bindings["package_checksum_ledger_sha256"],
            "package_manifest_sha256": source_bindings["package_manifest_sha256"],
        }
        review = {
            "schema_version": "g25-same-source-review-v1",
            "review_id": "review-fixture",
            "issued_at_epoch": 150.0,
            "review_target": target,
            "package_identity": package_identity,
            "source_bindings": source_bindings,
            "exact_command": {
                "argv": list(self.expected.argv),
                "argv_sha256": canonical_hash(list(self.expected.argv)),
            },
            "roles": {
                name: {"reviewer_id": f"{name}-reviewer", "verdict": "GO", "blockers": []}
                for name in ("architecture_system", "model_benchmark", "trace_provenance")
            },
            "overall": {"verdict": "GO", "blockers": []},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            review_path = root / "review.json"
            review_path.write_text(json.dumps(review), encoding="utf-8")
            self.assertEqual(
                review,
                load_and_validate_review_record(
                    review_path, target=target, source_bindings=source_bindings,
                    package_identity=package_identity, expected_argv=self.expected.argv,
                ),
            )
            review_sha256 = hashlib.sha256(review_path.read_bytes()).hexdigest()
            evaluation = {
                "schema_version": "g25-5.6sol-evaluation-v1",
                "evaluation_id": "evaluation-fixture",
                "evaluated_at_epoch": 151.0,
                "evaluator": {"gate_alias": "5.6sol", "model": "gpt-5.6-sol"},
                "review_target": target,
                "same_source_review_sha256": review_sha256,
                "source_bindings_sha256": canonical_hash(source_bindings),
                "package_checksum_ledger_sha256": source_bindings[
                    "package_checksum_ledger_sha256"
                ],
                "exact_command": {
                    "argv": list(self.expected.argv),
                    "argv_sha256": canonical_hash(list(self.expected.argv)),
                },
                "verdict": "GO",
                "blockers": [],
            }
            evaluation_path = root / "evaluation.json"
            evaluation_path.write_text(json.dumps(evaluation), encoding="utf-8")
            self.assertEqual(
                evaluation,
                load_and_validate_evaluation_record(
                    evaluation_path, target=target, source_bindings=source_bindings,
                    review_sha256=review_sha256, expected_argv=self.expected.argv,
                ),
            )
            tampered = copy.deepcopy(evaluation)
            tampered["exact_command"]["argv"].append("--unauthorized")
            tampered["exact_command"]["argv_sha256"] = canonical_hash(
                tampered["exact_command"]["argv"]
            )
            evaluation_path.write_text(json.dumps(tampered), encoding="utf-8")
            with self.assertRaisesRegex(
                DecisionRecordValidationError, "exact application argv"
            ):
                load_and_validate_evaluation_record(
                    evaluation_path, target=target, source_bindings=source_bindings,
                    review_sha256=review_sha256, expected_argv=self.expected.argv,
                )

    def test_dynamic_preflight_uses_only_explicit_provider(self):
        calls = []
        def provider(root):
            calls.append(root)
            return facts_fixture()
        report = run_dynamic_preflight(provider, Path("/cpu-fixture"), self.approval)
        self.assertEqual("pass", report["status"])
        self.assertEqual([Path("/cpu-fixture")], calls)
        with self.assertRaisesRegex(DynamicPreflightError, "explicit"):
            run_dynamic_preflight(None, Path("/cpu-fixture"), self.approval)  # type: ignore[arg-type]

    def test_dynamic_hardware_failures_are_independently_blocking(self):
        mutations = (
            ("uuid", "GPU-other"), ("pci_bus_id", "00000000:02:00.0"),
            ("total_vram_bytes", 1), ("free_vram_bytes", 4999999999),
            ("bf16_supported", False),
        )
        for key, value in mutations:
            with self.subTest(key=key):
                facts = facts_fixture()
                facts["gpus"][0][key] = value
                with self.assertRaises(DynamicPreflightError):
                    validate_dynamic_preflight(facts, self.approval)
        for key, value in (("compute_processes", [123]), ("disk_free_bytes", 1)):
            facts = facts_fixture()
            facts[key] = value
            with self.subTest(key=key), self.assertRaises(DynamicPreflightError):
                validate_dynamic_preflight(facts, self.approval)

    def test_runtime_offline_and_determinism_drift_fail_closed(self):
        cases = []
        runtime = facts_fixture(); runtime["runtime"]["torch"] = "different"; cases.append(runtime)
        offline = facts_fixture(); offline["offline"]["hf_hub_offline"] = "0"; cases.append(offline)
        deterministic = facts_fixture(); deterministic["determinism"]["matmul_allow_tf32"] = True; cases.append(deterministic)
        visible = facts_fixture(); visible["cuda_visible_devices"] = "0"; cases.append(visible)
        for index, facts in enumerate(cases):
            with self.subTest(index=index), self.assertRaises(DynamicPreflightError):
                validate_dynamic_preflight(facts, self.approval)

    def test_unknown_or_missing_dynamic_fact_is_rejected(self):
        extra = facts_fixture(); extra["unreviewed"] = True
        missing = facts_fixture(); missing["gpus"][0].pop("uuid")
        for facts in (extra, missing):
            with self.assertRaises(DynamicPreflightError):
                validate_dynamic_preflight(facts, self.approval)

    def test_provider_failure_is_wrapped_fail_closed(self):
        def failed(_root):
            raise RuntimeError("fixture failure")
        with self.assertRaisesRegex(DynamicPreflightError, "provider failed"):
            run_dynamic_preflight(failed, Path("/cpu-fixture"), self.approval)

    def test_real_fact_provider_uses_isolated_bf16_probe_then_process_query(self):
        responses = [
            subprocess.CompletedProcess(
                [], 0,
                "0, NVIDIA GeForce RTX 3050, GPU-test, 00000000:01:00.0, 6144, 6000\n",
                "",
            ),
            subprocess.CompletedProcess([], 0, "1\n", ""),
            subprocess.CompletedProcess([], 0, "", ""),
        ]
        with (
            patch("scheduler.g25_application.subprocess.run", side_effect=responses) as run,
            patch("scheduler.g25_application.shutil.disk_usage") as disk,
            patch(
                "scheduler.g25_application.verify_runtime_inventory",
                return_value={
                    "python_version": "3.10.12",
                    "runtime_inventory_sha256": runtime_fixture()[
                        "runtime_inventory_sha256"
                    ],
                    "requirements_lock_sha256": runtime_fixture()[
                        "requirements_lock_sha256"
                    ],
                    "runtime_tree_sha256": runtime_fixture()[
                        "runtime_tree_sha256"
                    ],
                    "stdlib_tree_sha256": runtime_fixture()[
                        "stdlib_tree_sha256"
                    ],
                    "driver_runtime_tree_sha256": runtime_fixture()[
                        "driver_runtime_tree_sha256"
                    ],
                    "system_files_sha256": runtime_fixture()[
                        "system_files_sha256"
                    ],
                    "system_closure_sha256": runtime_fixture()[
                        "system_closure_sha256"
                    ],
                    "static_dependency_edges_sha256": runtime_fixture()[
                        "static_dependency_edges_sha256"
                    ],
                },
            ),
        ):
            disk.return_value.free = 10 * 1024**3
            facts = query_dynamic_preflight(Path("/cpu-fixture"))
        self.assertEqual(3, run.call_count)
        self.assertEqual(
            [
                NVIDIA_SMI_TIMEOUT_SECONDS,
                BF16_PROBE_TIMEOUT_SECONDS,
                NVIDIA_SMI_TIMEOUT_SECONDS,
            ],
            [call.kwargs["timeout"] for call in run.call_args_list],
        )
        self.assertTrue(facts["gpus"][0]["bf16_supported"])
        self.assertEqual([], facts["compute_processes"])

    def test_package_local_runtime_inventory_verifies_all_pinned_record_files(self):
        runtime_root = APPROVAL_SCHEMA_PATH.parents[1] / ".benchmark-runtime"
        if not runtime_root.is_dir():
            self.skipTest(
                "private package runtime is intentionally not bundled in source clean-room"
            )
        attestation = verify_runtime_inventory(verify_record_files=True)
        self.assertEqual("/usr/bin/python3.10", attestation["python_realpath"])
        self.assertEqual("2.7.1+cu128", attestation["distribution_versions"]["torch"])
        self.assertGreater(attestation["verified_record_file_count"], 15000)
        runtime_root = Path(attestation["runtime_root"])
        for module_path in attestation["import_roots"].values():
            Path(module_path).relative_to(runtime_root)

    def test_exact_tree_inventory_rejects_extra_file_and_content_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "bound.txt").write_text("bound\n", encoding="utf-8")
            expected = _exact_tree_fingerprint(root)
            _verify_exact_tree(root, expected, "fixture")
            (root / "extra.txt").write_text("extra\n", encoding="utf-8")
            with self.assertRaisesRegex(DynamicPreflightError, "exact file set"):
                _verify_exact_tree(root, expected, "fixture")
            (root / "extra.txt").unlink()
            (root / "bound.txt").write_text("drift\n", encoding="utf-8")
            with self.assertRaisesRegex(DynamicPreflightError, "exact file set"):
                _verify_exact_tree(root, expected, "fixture")

    def test_isolated_bootstrap_verifies_runtime_before_projectctl_plan(self):
        package = APPROVAL_SCHEMA_PATH.parents[1]
        runtime_root = package / ".benchmark-runtime"
        if not runtime_root.is_dir():
            self.skipTest(
                "private package runtime is intentionally not bundled in source clean-room"
            )
        result = subprocess.run(
            build_attested_python_argv(
                "projectctl",
                ["qualification", "plan"],
                package_root=package,
                python_executable=Path("/usr/bin/python3"),
            ),
            cwd=package,
            env={
                "G25_RUNTIME_ROOT": str(runtime_root),
                "PYTHONNOUSERSITE": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PATH": "/usr/bin:/bin",
            },
            text=True,
            capture_output=True,
            check=False,
            timeout=180,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        plan = json.loads(result.stdout)
        self.assertEqual("g25-gpu-pilot-plan-v1", plan["schema_version"])
        self.assertFalse(plan["gpu_used"])
        self.assertFalse(plan["gpu_authorized"])

    def test_malformed_nonempty_compute_process_rows_fail_closed(self):
        gpu = subprocess.CompletedProcess(
            [], 0,
            "0, NVIDIA GeForce RTX 3050, GPU-test, 00000000:01:00.0, 6144, 6000\n",
            "",
        )
        bf16 = subprocess.CompletedProcess([], 0, "1\n", "")
        for row in ("N/A\n", "12x\n", "0\n", "42\n42\n"):
            with self.subTest(row=row), patch(
                "scheduler.g25_application.subprocess.run",
                side_effect=[gpu, bf16, subprocess.CompletedProcess([], 0, row, "")],
            ):
                with self.assertRaisesRegex(RuntimeError, "process"):
                    query_dynamic_preflight(Path("/cpu-fixture"))

    def test_real_fact_provider_fails_closed_when_any_query_times_out(self):
        success = [
            subprocess.CompletedProcess(
                [], 0,
                "0, NVIDIA GeForce RTX 3050, GPU-test, 00000000:01:00.0, 6144, 6000\n",
                "",
            ),
            subprocess.CompletedProcess([], 0, "1\n", ""),
            subprocess.CompletedProcess([], 0, "", ""),
        ]
        cases = (
            (0, "nvidia-smi GPU query", NVIDIA_SMI_TIMEOUT_SECONDS),
            (1, "BF16 capability query", BF16_PROBE_TIMEOUT_SECONDS),
            (2, "nvidia-smi process query", NVIDIA_SMI_TIMEOUT_SECONDS),
        )
        for index, label, timeout in cases:
            responses = list(success)
            responses[index] = subprocess.TimeoutExpired(["fixture"], timeout)
            with self.subTest(query=label), patch(
                "scheduler.g25_application.subprocess.run", side_effect=responses
            ):
                with self.assertRaisesRegex(RuntimeError, f"{label} timed out"):
                    query_dynamic_preflight(Path("/cpu-fixture"))

    def test_query_timeout_is_wrapped_by_dynamic_preflight_gate(self):
        with patch(
            "scheduler.g25_application.subprocess.run",
            side_effect=subprocess.TimeoutExpired(
                ["nvidia-smi"], NVIDIA_SMI_TIMEOUT_SECONDS
            ),
        ):
            with self.assertRaisesRegex(
                DynamicPreflightError, "provider failed: RuntimeError:.*timed out"
            ):
                run_dynamic_preflight(
                    query_dynamic_preflight,
                    Path("/cpu-fixture"),
                    self.approval,
                )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import base64
import hashlib
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[4]
APPLICATION = REPO / "explorations/moe_cycle_simulator/phase7/application"
sys.path.insert(0, str(REPO))

from explorations.moe_cycle_simulator.phase7.application.executor.common import (  # noqa: E402
    M0Error,
    canonical_bytes,
    exact_regular_file_set,
    file_sha256,
    semantic_sha256,
    write_new_json,
)
from explorations.moe_cycle_simulator.phase7.application.executor.deployment import (  # noqa: E402
    PREPARED_DIRECTORIES,
    build_deployment_ssh_argv,
    validate_deployment_approval,
    validate_deployment_plan,
)
from explorations.moe_cycle_simulator.phase7.application.executor.deployment_bundle import (  # noqa: E402
    _receipt_object,
    _validated_bundle,
    write_bundle,
)
from explorations.moe_cycle_simulator.phase7.application.executor.deployment_finalize import (  # noqa: E402
    verify_deployment_terminal,
)
from explorations.moe_cycle_simulator.phase7.application.executor.deployment_controller import (  # noqa: E402
    run_ssh,
)
from explorations.moe_cycle_simulator.phase7.application.executor.gate_m_local_replay import (  # noqa: E402
    run_bounded_decoder_process,
)
from explorations.moe_cycle_simulator.phase7.application.executor.package_ledger import (  # noqa: E402
    build as build_application_ledger,
)
from explorations.moe_cycle_simulator.phase7.application.executor.authority import (  # noqa: E402
    retain_authority,
)
from explorations.moe_cycle_simulator.phase7.application.executor.materialization_driver import (  # noqa: E402
    seal_materialization_terminal,
)
from explorations.moe_cycle_simulator.phase7.application.executor.runtime_provenance import (  # noqa: E402
    seal_runtime_provenance,
)
from explorations.moe_cycle_simulator.phase7.application.executor.gate_m_parent import (  # noqa: E402
    build_parent_from_local_terminal,
    validate_gate_m_parent,
)
from explorations.moe_cycle_simulator.phase7.application.validate_application import (  # noqa: E402
    validate_gate_m_ready,
)


def write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


class DeploymentControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp(prefix="phase7-deployment-controller-"))
        self.addCleanup(self._cleanup)
        self.application = self.base / "application"
        shutil.copytree(
            APPLICATION,
            self.application,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        self.fake_ssh = self.base / "fake-ssh"
        self.fake_ssh.write_text(
            "#!/usr/bin/env python3\n"
            "import base64, hashlib, json, os, sys\n"
            "payload = sys.stdin.buffer.read()\n"
            "bundle = json.loads(payload)\n"
            "members = {item['path']: item for item in bundle['members']}\n"
            "plan = json.loads(base64.b64decode(members['deployment_plan.template.json']['content_base64']))\n"
            "bundle_sha = hashlib.sha256(payload).hexdigest()\n"
            "receipt = {'schema_version':'moe-simulator-phase7-deployment-receipt-v1','installation_status':'COMPLETE','allowed_root':'/vault','target':plan['storage']['application_target'],'bundle_root_name':bundle['root_name'],'bundle_sha256':bundle_sha,'package_ledger':bundle['package_ledger'],'member_count':bundle['member_count'],'total_payload_bytes':bundle['total_payload_bytes'],'sealed_file_modes':['0444','0555'],'sealed_directory_mode':'0555'}\n"
            "canonical = lambda value: json.dumps(value,ensure_ascii=False,allow_nan=False,sort_keys=True,separators=(',',':')).encode()\n"
            "payloads = json.load(open(os.environ['MOE_PHASE7_FAKE_PROJECTION'],encoding='utf-8'))\n"
            "payloads['deployment/deployment_receipt.json'] = base64.b64encode(canonical(receipt)).decode()\n"
            "decoded = {path:base64.b64decode(value) for path,value in payloads.items()}\n"
            "export_members = [{'path':path,'size_bytes':len(value),'sha256':hashlib.sha256(value).hexdigest(),'mode_octal':'0444'} for path,value in sorted(decoded.items())]\n"
            "manifest = {'schema_version':'moe-simulator-phase7-gate-m-export-manifest-v1','status':'COMPLETE_REPLAYED','member_count':len(export_members),'total_size_bytes':sum(len(value) for value in decoded.values()),'members':export_members,'model_weights_included':False,'credentials_included':False}\n"
            "manifest['manifest_sha256'] = hashlib.sha256(canonical(manifest)).hexdigest()\n"
            "status_payload = canonical({'schema_version':'moe-simulator-phase7-gate-m-export-status-v1','status':'GATE_M_EXPORT_COMPLETE_REPLAYED','manifest_sha256':manifest['manifest_sha256'],'member_count':manifest['member_count'],'total_size_bytes':manifest['total_size_bytes']})+b'\\n'\n"
            "materialization_ledger = json.loads(decoded['materialization/evidence_ledger.json'])\n"
            "runtime_ledger = json.loads(decoded['runtime/evidence_ledger.json'])\n"
            "model_ledger = json.loads(decoded['model/model_ledger.json'])\n"
            "runtime_status = runtime_ledger['terminal_status']\n"
            "runtime_record = 'runtime/runtime_provenance.json' if runtime_status == 'COMPLETE' else 'runtime/runtime_provenance_failure.json'\n"
            "remote_status = 'REMOTE_COMPLETE_PROVENANCE_ELIGIBLE' if runtime_status == 'COMPLETE' else 'REMOTE_COMPLETE_BLOCKED_PROVENANCE'\n"
            "next_action = 'LOCAL_EXPORT_REPLAY_REQUIRED_BEFORE_M0_ELIGIBILITY' if runtime_status == 'COMPLETE' else 'NO_M0_APPLICATION_PROVIDER_PROVENANCE_REQUIRED'\n"
            "summary = {'schema_version':'moe-simulator-phase7-gate-m-remote-summary-v1','status':remote_status,'application_ledger_sha256':bundle['package_ledger']['ledger_sha256'],'deployment_bundle_sha256':bundle_sha,'deployment_receipt_sha256':hashlib.sha256(canonical(receipt)).hexdigest(),'materialization_evidence_ledger_sha256':materialization_ledger['ledger_sha256'],'model_ledger_sha256':model_ledger['ledger_sha256'],'capacity_prompt_fixture_sha256':hashlib.sha256(decoded['fixtures/capacity_prompt_fixture.json']).hexdigest(),'runtime_provenance_ledger_sha256':runtime_ledger['ledger_sha256'],'runtime_provenance_record_sha256':hashlib.sha256(decoded[runtime_record]).hexdigest(),'export_manifest_sha256':manifest['manifest_sha256'],'export_commit_marker_sha256':hashlib.sha256(status_payload).hexdigest(),'driver_stdout_sha256':'3'*64,'driver_stderr_sha256':'4'*64,'remote_command_executables':{'timeout':{'path':plan['bootstrap']['remote_timeout_executable'],'sha256':plan['bootstrap']['remote_timeout_executable_sha256']},'python':{'path':plan['bootstrap']['remote_python_executable'],'sha256':plan['bootstrap']['remote_python_executable_sha256']}},'process_cleanup':{'status':'CLEAN','surviving_pids':[]},'phase_timing':{'materialization_start_monotonic_ns':1,'materialization_end_monotonic_ns':2,'materialization_deadline_monotonic_ns':3,'runtime_provenance_start_monotonic_ns':2,'runtime_provenance_end_monotonic_ns':3,'runtime_provenance_deadline_monotonic_ns':4,'export_start_monotonic_ns':3,'export_end_monotonic_ns':4,'export_deadline_monotonic_ns':5},'runtime_provenance_status':runtime_status,'export_status':'REMOTE_COMPLETE_LOCAL_REPLAY_REQUIRED','gpu_workload_performed':False,'next_legal_action':next_action}\n"
            "transport_members = [{**item,'payload_base64':payloads[item['path']]} for item in export_members]\n"
            "envelope = {'schema_version':'moe-simulator-phase7-gate-m-export-transport-v1','remote_summary':summary,'export_manifest':manifest,'export_status_payload_base64':base64.b64encode(status_payload).decode(),'raw_payload_size_bytes':sum(len(value) for value in decoded.values())+len(status_payload),'members':transport_members}\n"
            "envelope['transport_sha256'] = hashlib.sha256(canonical(envelope)).hexdigest()\n"
            "transport = canonical(envelope)\n"
            "header = b'MOE_GATE_M_EXPORT_V1 '+str(len(transport)).encode()+b' '+hashlib.sha256(transport).hexdigest().encode()+b'\\n'\n"
            "sys.stdout.buffer.write(header+transport)\n",
            encoding="utf-8",
        )
        self.fake_ssh.chmod(0o755)
        self.remote_timeout = Path(shutil.which("timeout") or "").resolve(strict=True)
        self.remote_python = Path(sys.executable).resolve(strict=True)
        self.known_hosts = self.base / "known_hosts"
        key_blob = b"phase7-test-ed25519-public-key"
        key_text = base64.b64encode(key_blob).decode("ascii")
        self.known_hosts.write_text(
            f"[server.test]:2222 ssh-ed25519 {key_text}\n", encoding="utf-8"
        )
        self.host_key_sha = hashlib.sha256(key_blob).hexdigest()
        self.host_fingerprint = "SHA256:" + base64.b64encode(
            hashlib.sha256(key_blob).digest()
        ).decode("ascii").rstrip("=")
        self.evidence = self.base / "deployment-evidence"
        self.registry = self.base / "deployment-registry.json"
        self.approval_id = "owner-gate-m-deploy-test0001"
        self.approval_token_sha256 = "2" * 64
        now = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=1)
        self.allocation = {
            "start_trigger": "OWNER_RELEASES_FRESH_SSH_HANDOFF",
            "lease_start_utc": now.isoformat().replace("+00:00", "Z"),
            "lease_deadline_utc": (now + timedelta(seconds=21600))
            .isoformat()
            .replace("+00:00", "Z"),
            "total_seconds": 21600,
            "billing_mode": "PREPAID_FIXED_WINDOW",
            "extension_allowed": False,
            "additional_cost_allowed": False,
            "maximum_additional_spend_amount": "0",
            "maximum_additional_spend_currency": "TWD",
            "release_reserve_seconds": 900,
        }
        self.vault_identity = "1" * 64
        self.d0 = self.base / "d0_result.json"
        write_json(
            self.d0,
            {
                "disclosure_status": "COMPLETE",
                "environment_eligibility": "READY_FOR_MATERIALIZATION_APPLICATION",
                "vault_mount_identity_sha256": self.vault_identity,
            },
        )
        self.plan = self._freeze_plan()
        self.plan_path = self.application / "deployment_plan.template.json"
        write_json(self.plan_path, self.plan)
        self._freeze_materialization_projection()
        self.fake_projection = self._build_fake_projection_payloads(
            "blocked", "BLOCKED"
        )
        self.fake_projection_eligible = self._build_fake_projection_payloads(
            "eligible", "COMPLETE"
        )
        self.bundle = self.base / "application.bundle.json"
        write_bundle(self.application, self.bundle)
        self.approval = self._approval()
        self.approval_path = self.base / "deployment-approval.json"
        write_json(self.approval_path, self.approval)

    def _cleanup(self) -> None:
        if not self.base.exists():
            return
        for directory, directories, files in os.walk(
            self.base, topdown=True, followlinks=False
        ):
            root = Path(directory)
            if not root.is_symlink():
                os.chmod(root, 0o700)
            for name in files:
                path = root / name
                if not path.is_symlink():
                    os.chmod(path, 0o600)
            for name in directories:
                path = root / name
                if not path.is_symlink():
                    os.chmod(path, 0o700)
        shutil.rmtree(self.base)

    def _freeze_plan(self) -> dict:
        plan = json.loads(
            (self.application / "deployment_plan.template.json").read_text(
                encoding="utf-8"
            )
        )
        project = Path("/vault/flow-mixtral-rtxpro6000-r12-testdeploy01")
        plan["status"] = "FROZEN"
        plan["ssh"].update(
            {
                "executable": str(self.fake_ssh),
                "executable_sha256": file_sha256(self.fake_ssh),
                "host": "server.test",
                "username": "pod_user",
                "known_hosts_file": str(self.known_hosts),
                "known_hosts_file_sha256": file_sha256(self.known_hosts),
                "host_public_key_blob_sha256": self.host_key_sha,
                "openssh_fingerprint": self.host_fingerprint,
            }
        )
        bootstrap = plan["bootstrap"]
        bootstrap["source_sha256"] = file_sha256(
            self.application / bootstrap["source_relative_path"]
        )
        bootstrap["deployment_bootstrap_sha256"] = file_sha256(
            self.application / bootstrap["deployment_bootstrap_relative_path"]
        )
        bootstrap["controller_sha256"] = file_sha256(
            self.application / bootstrap["controller_relative_path"]
        )
        bootstrap["remote_controller_sha256"] = file_sha256(
            self.application / bootstrap["remote_controller_relative_path"]
        )
        bootstrap["runtime_provenance_sha256"] = file_sha256(
            self.application / bootstrap["runtime_provenance_relative_path"]
        )
        bootstrap["exporter_sha256"] = file_sha256(
            self.application / bootstrap["exporter_relative_path"]
        )
        bootstrap["local_decoder_sha256"] = file_sha256(
            self.application / bootstrap["local_decoder_relative_path"]
        )
        bootstrap["remote_timeout_executable"] = str(self.remote_timeout)
        bootstrap["remote_timeout_executable_sha256"] = file_sha256(
            self.remote_timeout
        )
        bootstrap["remote_python_executable"] = str(self.remote_python)
        bootstrap["remote_python_executable_sha256"] = file_sha256(
            self.remote_python
        )
        plan["storage"].update(
            {
                "expected_mount_identity_sha256": self.vault_identity,
                "project_root": str(project),
                "incoming_bundle": str(project / "incoming/application.bundle.json"),
                "application_target": str(
                    project
                    / "packages/materialization/repo/explorations/moe_cycle_simulator/phase7/application"
                ),
                "deployment_receipt": str(
                    project / "packages/materialization/deployment_receipt.json"
                ),
            }
        )
        plan["allocation_window"] = self.allocation
        plan["output"]["local_evidence_root"] = str(self.evidence)
        bootstrap["ssh_argv_template_sha256"] = semantic_sha256(
            build_deployment_ssh_argv(plan, self.application)
        )
        return plan

    def _freeze_materialization_projection(self) -> None:
        project = Path(self.plan["storage"]["project_root"])
        plan_path = self.application / "materialization_plan.template.json"
        materialization = json.loads(plan_path.read_text(encoding="utf-8"))
        materialization["status"] = "FROZEN"
        materialization["materializer"]["version"] = "test-huggingface-hub"
        materialization["tokenizer_builder"]["version"] = "test-transformers"
        materialization["paths"] = {
            "snapshot": str(project / "model/snapshot"),
            "model_ledger": str(project / "model/ledger/model_ledger.json"),
            "materialization_result": str(
                project / "model/ledger/materialization_result.json"
            ),
            "capacity_prompt_fixture": str(project / "fixtures/capacity_prompt.json"),
        }
        materialization["storage_contract"]["persistent_project_root"] = str(
            project
        )
        materialization["deployment"] = {
            "application_target": self.plan["storage"]["application_target"],
            "deployment_receipt": self.plan["storage"]["deployment_receipt"],
        }
        materialization["command_argv"] = ["/usr/bin/true"]
        materialization["prompt_fixture_command_argv"] = ["/usr/bin/true"]
        materialization["runtime_provenance"].update(
            {
                "output_root": str(project / "evidence/runtime-provenance"),
                "command_argv": ["/usr/bin/true"],
            }
        )
        write_json(plan_path, materialization)

        package = build_application_ledger(self.application)
        projection_path = self.application / "materialization_approval.template.json"
        projection = json.loads(projection_path.read_text(encoding="utf-8"))
        projection.update(
            {
                "approval_id": self.approval_id,
                "approval_token_sha256": self.approval_token_sha256,
                "used_once_registry_path": str(
                    project / "authority/registries/gate-m-consumption.json"
                ),
                "application_ledger_sha256": package["ledger_sha256"],
                "materialization_plan_sha256": file_sha256(plan_path),
                "exact_materialization_commands_sha256": semantic_sha256(
                    {
                        "materialize": materialization["command_argv"],
                        "prompt_fixture": materialization[
                            "prompt_fixture_command_argv"
                        ],
                        "runtime_provenance": materialization[
                            "runtime_provenance"
                        ]["command_argv"],
                    }
                ),
                "approved_ssh_host_key_sha256": self.host_key_sha,
                "approved_d0_result_sha256": file_sha256(self.d0),
                "approved_vault_mount_identity_sha256": self.vault_identity,
                "approved_deployment_project_root": str(project),
                "approved_application_target": self.plan["storage"][
                    "application_target"
                ],
                "approved_deployment_receipt_path": self.plan["storage"][
                    "deployment_receipt"
                ],
                "allocation_window": self.allocation,
                "owner_authority_record_sha256": file_sha256(
                    self.application / "owner_environment_decision_20260729.json"
                ),
                "application_decision": "APPROVE",
                "materialization_decision": "APPROVE",
                "approved_at_utc": self.allocation["lease_start_utc"],
                "owner_identity": "test-owner",
            }
        )
        write_json(projection_path, projection)

    def _approval(self) -> dict:
        payload = self.bundle.read_bytes()
        bundle, _ = _validated_bundle(payload)
        target = Path(self.plan["storage"]["application_target"])
        receipt = _receipt_object(
            allowed_root=Path("/vault"),
            target=target,
            bundle=bundle,
            bundle_sha256=hashlib.sha256(payload).hexdigest(),
        )
        approval = json.loads(
            (
                self.application / "deployment_approval.external.template.json"
            ).read_text(encoding="utf-8")
        )
        approval.update(
            {
                "gate_m_session_id": "phase7-gate-m-deploy-test0001",
                "approval_id": self.approval_id,
                "approval_token_sha256": self.approval_token_sha256,
                "used_once_registry_path": str(self.registry),
                "application_ledger_sha256": bundle["package_ledger"][
                    "ledger_sha256"
                ],
                "deployment_plan_sha256": file_sha256(self.plan_path),
                "bootstrap_source_sha256": self.plan["bootstrap"]["source_sha256"],
                "deployment_bootstrap_source_sha256": self.plan["bootstrap"][
                    "deployment_bootstrap_sha256"
                ],
                "controller_sha256": self.plan["bootstrap"]["controller_sha256"],
                "remote_controller_sha256": self.plan["bootstrap"][
                    "remote_controller_sha256"
                ],
                "runtime_provenance_sha256": self.plan["bootstrap"][
                    "runtime_provenance_sha256"
                ],
                "exporter_sha256": self.plan["bootstrap"]["exporter_sha256"],
                "local_decoder_sha256": self.plan["bootstrap"][
                    "local_decoder_sha256"
                ],
                "approved_remote_timeout_executable": self.plan["bootstrap"][
                    "remote_timeout_executable"
                ],
                "approved_remote_timeout_executable_sha256": self.plan[
                    "bootstrap"
                ]["remote_timeout_executable_sha256"],
                "approved_remote_python_executable": self.plan["bootstrap"][
                    "remote_python_executable"
                ],
                "approved_remote_python_executable_sha256": self.plan[
                    "bootstrap"
                ]["remote_python_executable_sha256"],
                "approved_d0_result_path": str(self.d0),
                "approved_d0_result_sha256": file_sha256(self.d0),
                "approved_vault_mount_identity_sha256": self.vault_identity,
                "approved_ssh_host_key_sha256": self.host_key_sha,
                "allocation_window": self.allocation,
                "approved_local_evidence_root": str(self.evidence),
                "owner_authority_record_sha256": file_sha256(
                    self.application / "owner_environment_decision_20260729.json"
                ),
                "decision": "APPROVE",
                "approved_at_utc": self.allocation["lease_start_utc"],
                "owner_identity": "test-owner",
            }
        )
        approval["bundle"] = {
            "local_path": str(self.bundle),
            "size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "package_ledger_sha256": bundle["package_ledger"]["ledger_sha256"],
            "included_materialization_approval_sha256": {
                item["path"]: item["sha256"] for item in bundle["members"]
            }["materialization_approval.template.json"],
            "expected_deployment_receipt_sha256": hashlib.sha256(
                canonical_bytes(receipt)
            ).hexdigest(),
        }
        approval["exact_deployment_ssh_argv_sha256"] = semantic_sha256(
            build_deployment_ssh_argv(
                self.plan,
                self.application,
                bundle_size=len(payload),
                bundle_sha256=approval["bundle"]["sha256"],
            )
        )
        return approval

    def _build_fake_projection_payloads(self, suffix: str, runtime_status: str) -> Path:
        source = self.base / f"fake-projection-source-{suffix}"
        source.mkdir()
        materialization = source / "materialization"
        materialization.mkdir()
        approval_path = self.application / "materialization_approval.template.json"
        approval = json.loads(approval_path.read_text(encoding="utf-8"))
        registry = source / "materialization-consumption.json"
        write_json(
            registry,
            {
                "schema_version": "moe-simulator-phase7-used-materialization-approval-v1",
                "approval_id": approval["approval_id"],
                "approval_token_sha256": approval["approval_token_sha256"],
                "approval_file_sha256": file_sha256(approval_path),
            },
        )
        authority = retain_authority(
            application=self.application,
            approval_path=approval_path,
            registry_path=registry,
            evidence_root=materialization,
            expected_application_ledger_sha256=approval[
                "application_ledger_sha256"
            ],
        )
        model_ledger = {"ledger_sha256": "1" * 64}
        fixture = {
            "model_ledger_sha256": "1" * 64,
            "token_ids": [],
            "token_ids_sha256": semantic_sha256([]),
        }
        model_path = source / "model_ledger.json"
        fixture_path = source / "capacity_prompt_fixture.json"
        write_new_json(model_path, model_ledger)
        write_new_json(fixture_path, fixture)
        write_new_json(
            materialization / "stage_result.json",
            {
                "status": "COMPLETE_HARD_STOP",
                "gpu_workload_performed": False,
                "authority_evidence_sha256": semantic_sha256(authority),
                "model_ledger_sha256": model_ledger["ledger_sha256"],
                "capacity_prompt_fixture_sha256": file_sha256(fixture_path),
            },
        )
        seal_materialization_terminal(materialization, "COMPLETE_HARD_STOP")
        runtime = source / "runtime"
        runtime.mkdir()
        runtime_record = (
            "runtime_provenance.json"
            if runtime_status == "COMPLETE"
            else "runtime_provenance_failure.json"
        )
        write_new_json(runtime / runtime_record, {"status": runtime_status})
        seal_runtime_provenance(runtime, runtime_status)
        payloads: dict[str, str] = {}
        for prefix, root in (
            ("materialization", materialization),
            ("runtime", runtime),
        ):
            for relative in sorted(exact_regular_file_set(root)):
                payloads[f"{prefix}/{relative}"] = base64.b64encode(
                    (root / relative).read_bytes()
                ).decode("ascii")
        payloads.update(
            {
                "model/model_ledger.json": base64.b64encode(
                    model_path.read_bytes()
                ).decode("ascii"),
                "fixtures/capacity_prompt_fixture.json": base64.b64encode(
                    fixture_path.read_bytes()
                ).decode("ascii"),
                "authority/materialization_authority_projection.json": base64.b64encode(
                    approval_path.read_bytes()
                ).decode("ascii"),
                "d0/d0_binding.json": base64.b64encode(b"{}\n").decode("ascii"),
                "logs/command_log_inventory.json": base64.b64encode(b"{}\n").decode(
                    "ascii"
                ),
            }
        )
        output = self.base / f"fake-projection-payloads-{suffix}.json"
        write_json(output, payloads)
        return output

    def _validate(self, approval: dict | None = None) -> dict:
        value = self.approval if approval is None else approval
        temporary = self.approval_path
        if approval is not None:
            temporary = self.base / "changed-approval.json"
            write_json(temporary, value)
        return validate_deployment_approval(
            value,
            approval_path=temporary,
            plan=self.plan,
            plan_path=self.plan_path,
            application_dir=self.application,
            owner_authority_record_sha256=file_sha256(
                self.application / "owner_environment_decision_20260729.json"
            ),
        )

    def test_frozen_plan_and_external_approval_bind_complete_bundle(self) -> None:
        validate_deployment_plan(
            self.plan, application_dir=self.application, verify_files=True
        )
        result = self._validate()
        self.assertEqual(
            result["bundle"]["package_ledger"],
            build_application_ledger(self.application),
        )
        self.assertEqual(
            result["expected_receipt_sha256"],
            self.approval["bundle"]["expected_deployment_receipt_sha256"],
        )
        remote_tokens = shlex.split(
            build_deployment_ssh_argv(
                self.plan,
                self.application,
                bundle_size=self.approval["bundle"]["size_bytes"],
                bundle_sha256=self.approval["bundle"]["sha256"],
            )[-1]
        )
        self.assertEqual(remote_tokens[0], str(self.remote_timeout))
        self.assertEqual(remote_tokens[4], str(self.remote_python))
        documents = {
            "application": {},
            "environment": {},
            "materialization": {},
            "materialization_approval": {},
            "deployment_plan": self.plan,
        }
        with patch(
            "explorations.moe_cycle_simulator.phase7.application.validate_application.validate_materialization_ready"
        ):
            validate_gate_m_ready(
                self.application,
                documents,
                self.approval_path,
            )

    def test_approval_rejects_bundle_d0_receipt_and_argv_drift(self) -> None:
        mutations = []
        for path, value in (
            (("bundle", "sha256"), "0" * 64),
            (("bundle", "expected_deployment_receipt_sha256"), "0" * 64),
            (("approved_d0_result_sha256",), "0" * 64),
            (("exact_deployment_ssh_argv_sha256",), "0" * 64),
        ):
            changed = json.loads(json.dumps(self.approval))
            cursor = changed
            for key in path[:-1]:
                cursor = cursor[key]
            cursor[path[-1]] = value
            mutations.append(changed)
        for changed in mutations:
            with self.subTest():
                with self.assertRaises(M0Error):
                    self._validate(changed)

    def test_controller_consumes_once_and_seals_exact_local_evidence(self) -> None:
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in {"MOE_PHASE7_DEPLOYMENT_UNLOCK", "CUDA_VISIBLE_DEVICES"}
        }
        environment["MOE_PHASE7_DEPLOYMENT_UNLOCK"] = (
            "OWNER_APPROVED_EXACT_GATE_M_DEPLOYMENT_COMMAND"
        )
        environment["MOE_PHASE7_FAKE_PROJECTION"] = str(self.fake_projection)
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "explorations.moe_cycle_simulator.phase7.application.executor.deployment_controller",
                "--application-dir",
                str(self.application),
                "--approval",
                str(self.approval_path),
                "--evidence-root",
                str(self.evidence),
            ],
            cwd=REPO,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        ledger = verify_deployment_terminal(self.evidence)
        self.assertEqual(ledger["terminal_status"], "COMPLETE")
        self.assertTrue(self.registry.is_file())
        self.assertEqual(stat.S_IMODE(self.evidence.stat().st_mode), 0o555)
        result = json.loads(
            (self.evidence / "deployment_result.json").read_text(encoding="utf-8")
        )
        self.assertFalse(result["gpu_workload_performed"])
        self.assertTrue(result["model_downloaded"])
        self.assertEqual(
            result["gate_m_status"], "COMPLETE_M0_BLOCKED_PROVENANCE"
        )

    def test_locked_controller_rejects_before_registry_or_evidence(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "explorations.moe_cycle_simulator.phase7.application.executor.deployment_controller",
                "--application-dir",
                str(self.application),
                "--approval",
                str(self.approval_path),
                "--evidence-root",
                str(self.evidence),
            ],
            cwd=REPO,
            env={
                key: value
                for key, value in os.environ.items()
                if key not in {"MOE_PHASE7_DEPLOYMENT_UNLOCK", "CUDA_VISIBLE_DEVICES"}
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertFalse(self.registry.exists())
        self.assertFalse(self.evidence.exists())

    def test_controller_promotes_only_complete_semantic_local_replay(self) -> None:
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in {"MOE_PHASE7_DEPLOYMENT_UNLOCK", "CUDA_VISIBLE_DEVICES"}
        }
        environment.update(
            {
                "MOE_PHASE7_DEPLOYMENT_UNLOCK": (
                    "OWNER_APPROVED_EXACT_GATE_M_DEPLOYMENT_COMMAND"
                ),
                "MOE_PHASE7_FAKE_PROJECTION": str(
                    self.fake_projection_eligible
                ),
            }
        )
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "explorations.moe_cycle_simulator.phase7.application.executor.deployment_controller",
                "--application-dir",
                str(self.application),
                "--approval",
                str(self.approval_path),
                "--evidence-root",
                str(self.evidence),
            ],
            cwd=REPO,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        result = json.loads(
            (self.evidence / "deployment_result.json").read_text(encoding="utf-8")
        )
        self.assertEqual(result["gate_m_status"], "COMPLETE_M0_ELIGIBLE")
        self.assertEqual(result["export_status"], "COMPLETE_REPLAYED")
        self.assertEqual(result["next_legal_action"], "REQUEST_NEW_M0_APPLICATION")
        verify_deployment_terminal(self.evidence)
        review = self.base / "same-hash-review.json"
        write_json(
            review,
            {
                "schema_version": "moe-simulator-phase7-review-aggregate-v1",
                "source_commit_sha1": "1" * 40,
                "source_tree_sha1": "2" * 40,
                "application_ledger_sha256": result[
                    "application_ledger_sha256"
                ],
                "roles": {
                    "Architecture/System": "GO",
                    "Model/Benchmark": "GO",
                    "Trace/Provenance": "GO",
                },
                "verdict": "GO",
                "blockers": [],
            },
        )
        parent = build_parent_from_local_terminal(
            evidence_root=self.evidence,
            same_hash_review_aggregate=review,
        )
        self.assertEqual(
            validate_gate_m_parent(parent, verify_live=False)["status"],
            "COMPLETE_M0_ELIGIBLE",
        )
        for location, value in (
            (("status",), "COMPLETE_M0_BLOCKED_PROVENANCE"),
            (("source", "trace_provenance"), "MODIFY"),
            (("local_terminal", "transport_sha256"), "0" * 64),
        ):
            changed = json.loads(json.dumps(parent))
            cursor = changed
            for key in location[:-1]:
                cursor = cursor[key]
            cursor[location[-1]] = value
            with self.assertRaises(M0Error):
                validate_gate_m_parent(changed, verify_live=False)

    def test_ssh_streaming_bound_terminates_before_unbounded_capture(self) -> None:
        spool = self.base / "bounded-spool"
        spool.mkdir()
        stdout = spool / "stdout.log"
        stderr = spool / "stderr.log"
        started = time.monotonic()
        with self.assertRaisesRegex(M0Error, "stdout exceeded"):
            run_ssh(
                [
                    sys.executable,
                    "-c",
                    "import sys,time;sys.stdin.buffer.read();sys.stdout.buffer.write(b'x'*1048576);sys.stdout.buffer.flush();time.sleep(30)",
                ],
                bundle=b"bounded-input",
                deadline_monotonic_ns=time.monotonic_ns() + 5_000_000_000,
                stdout_path=stdout,
                stderr_path=stderr,
                max_stdout_bytes=1024,
                max_stderr_bytes=1024,
            )
        self.assertLess(time.monotonic() - started, 5)
        self.assertLessEqual(stdout.stat().st_size, 1024)
        self.assertEqual(stat.S_IMODE(stdout.stat().st_mode), 0o400)

    def test_local_decoder_rlimit_fails_closed_before_eligibility_write(self) -> None:
        evidence = self.base / "decoder-oom-evidence"
        evidence.mkdir()
        decoder = self.base / "adversarial-decoder.py"
        marker = evidence / "eligibility.marker"
        decoder.write_text(
            "from pathlib import Path\n"
            "payload = bytearray(256 * 1024 * 1024)\n"
            f"Path({str(marker)!r}).write_text('SHOULD_NOT_EXIST')\n",
            encoding="utf-8",
        )
        deadline = time.monotonic_ns() + 5_000_000_000
        started = time.monotonic()
        with self.assertRaisesRegex(M0Error, "failed within its enforced boundary"):
            run_bounded_decoder_process(
                [str(self.remote_python), str(decoder)],
                decoder_source=decoder.resolve(strict=True),
                decoder_source_sha256=file_sha256(decoder),
                address_space_bytes=64 * 1024 * 1024,
                deadline_monotonic_ns=deadline,
                stdout_path=evidence / "stdout.log",
                stderr_path=evidence / "stderr.log",
                execution_record_path=evidence / "execution.json",
            )
        self.assertLess(time.monotonic() - started, 5)
        self.assertFalse(marker.exists())
        execution = json.loads(
            (evidence / "execution.json").read_text(encoding="utf-8")
        )
        self.assertEqual(execution["status"], "FAILED")
        self.assertEqual(execution["rlimit_as_bytes"], 64 * 1024 * 1024)
        self.assertFalse(execution["timed_out"])
        self.assertEqual(execution["process_tree_cleanup"]["status"], "CLEAN")
        self.assertEqual(
            stat.S_IMODE((evidence / "execution.json").stat().st_mode), 0o400
        )

    def test_local_decoder_log_bound_fails_before_unbounded_spool(self) -> None:
        evidence = self.base / "decoder-log-evidence"
        evidence.mkdir()
        decoder = self.base / "noisy-decoder.py"
        decoder.write_text(
            "import os,time\n"
            "os.write(1, b'x' * 1048576)\n"
            "time.sleep(30)\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(M0Error, "log_limit_exceeded=stdout"):
            run_bounded_decoder_process(
                [str(self.remote_python), str(decoder)],
                decoder_source=decoder.resolve(strict=True),
                decoder_source_sha256=file_sha256(decoder),
                address_space_bytes=128 * 1024 * 1024,
                deadline_monotonic_ns=time.monotonic_ns() + 5_000_000_000,
                stdout_path=evidence / "stdout.log",
                stderr_path=evidence / "stderr.log",
                execution_record_path=evidence / "execution.json",
                max_log_bytes=1024,
            )
        self.assertEqual((evidence / "stdout.log").stat().st_size, 1024)
        execution = json.loads(
            (evidence / "execution.json").read_text(encoding="utf-8")
        )
        self.assertEqual(execution["status"], "FAILED")
        self.assertEqual(execution["log_limit_exceeded"], "stdout")
        self.assertEqual(execution["stdout_size_bytes"], 1024)
        self.assertEqual(execution["process_tree_cleanup"]["status"], "CLEAN")


if __name__ == "__main__":
    unittest.main()

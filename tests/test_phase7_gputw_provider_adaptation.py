"""CPU-only regression tests for the Phase 7 GPUtw.ai contract overlay."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "explorations/moe_cycle_simulator/phase7_provider_adaptation/gputw_r1"
MODULE_SPEC = importlib.util.spec_from_file_location(
    "phase7_gputw_provider_adaptation_validator",
    PACKAGE / "validate_provider_adaptation.py",
)
assert MODULE_SPEC and MODULE_SPEC.loader
validator = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(validator)


@pytest.fixture
def contract() -> dict:
    return validator.load_json(PACKAGE / "provider_adaptation.json")


def test_package_validator_passes_without_live_io() -> None:
    result = validator.validate_package()
    assert result["status"] == "PASS"
    assert result["network_free"] is True
    assert result["gpu_authority"] == "NONE"
    assert result["known_hosts_status"] == "REQUIRED_NOT_PRESENT"


def test_owner_budget_is_not_a_provider_lease(contract: dict) -> None:
    envelope = contract["owner_execution_envelope"]
    assert envelope["owner_execution_budget_seconds"] == 21600
    assert envelope["owner_execution_budget_type"] == "HARD_CAP"
    assert envelope["semantic_role"] == "OWNER_IMPOSED_EXECUTION_ENVELOPE"
    assert envelope["provider_lease_seconds"] == "NONE_OBSERVED"
    assert contract["provider_semantics"]["provider_fixed_lease_seconds"] == "NONE_OBSERVED"
    assert envelope["bounded_work_seconds"] == 21000
    assert envelope["remaining_slack_seconds"] == 600


def test_provider_grace_is_not_required(contract: dict) -> None:
    provider = contract["provider_semantics"]
    envelope = contract["owner_execution_envelope"]
    assert provider["provider_grace_period"] == "NOT_APPLICABLE"
    assert envelope["provider_grace_credit"] == "NONE"
    assert envelope["stage_bounds_are_provider_guarantees"] is False


def test_identity_separates_gateway_from_instance(contract: dict) -> None:
    identity = contract["identity_model"]
    assert set(identity) == {
        "provider_identity",
        "instance_identity",
        "ssh_gateway_identity",
        "client_authentication",
        "server_authentication",
    }
    assert identity["ssh_gateway_identity"] == {
        "host": "ssh.gputw.ai",
        "port": 2222,
        "principal_template": "pod-<instance-id>",
        "identity_semantics": "GATEWAY_HOST_AND_INSTANCE_PRINCIPAL",
        "ip_only_identity": False,
    }
    assert identity["instance_identity"]["instance_id"] is None


def test_client_key_provisioning_is_complete_without_private_credentials(contract: dict) -> None:
    client = contract["identity_model"]["client_authentication"]
    assert client["account_public_key_registered"] == "CONFIRMED_BY_OWNER"
    assert client["credential_provisioning_required"] is False
    assert client["workflow_must_generate_keys"] is False
    assert client["workflow_must_upload_keys"] is False
    assert client["workflow_must_modify_provider_credentials"] is False
    assert "FORBIDDEN" in client["private_key_material_policy"]
    assert validator._contains_secret_material(contract) == []


def test_unresolved_gateway_host_key_blocks_d0_ssh(contract: dict) -> None:
    server = contract["identity_model"]["server_authentication"]
    assert server["gateway_host_key_status"] == "UNRESOLVED"
    assert server["d0_ssh_authority"] == "NONE"
    assert validator.d0_ssh_authorized(contract) is False
    with pytest.raises(validator.ContractError):
        validator.validate_contract({**contract, "identity_model": {**contract["identity_model"], "server_authentication": {**server, "gateway_host_key_status": "TRUSTED"}}})


def test_keyscan_observation_cannot_establish_trust(contract: dict) -> None:
    governance = contract["host_key_governance"]
    assert governance["ssh_keyscan_policy"] == "MAY_COLLECT_OBSERVATION_MUST_NOT_ESTABLISH_TRUST_BY_ITSELF"
    assert governance["existing_known_hosts_entry_policy"] == "MAY_BE_OBSERVED_DOES_NOT_ESTABLISH_TRUST_PROVENANCE"
    altered = copy.deepcopy(contract)
    altered["host_key_governance"]["ssh_keyscan_policy"] = "TRUSTED"
    with pytest.raises(validator.ContractError):
        validator.validate_contract(altered)


def test_formal_d0_requires_project_specific_checksum_bound_known_hosts(contract: dict) -> None:
    artifact = contract["host_key_governance"]["project_specific_artifact"]
    assert artifact["formal_path"] == "phase7/application/known_hosts.gputw"
    assert artifact["status"] == "REQUIRED_NOT_PRESENT"
    assert artifact["checksum_bound_before_formal_d0"] is True
    assert artifact["personal_known_hosts_file_allowed"] is False
    assert contract["host_key_governance"]["strict_ssh_options"] == {
        "user_known_hosts_file": "PROJECT_SPECIFIC_FROZEN_ARTIFACT",
        "strict_host_key_checking": "yes",
        "connection_allowed_while_unresolved": False,
    }


def test_project_known_hosts_binding_requires_trusted_provenance(tmp_path: Path) -> None:
    artifact = tmp_path / "known_hosts.gputw"
    artifact.write_text(
        "[ssh.gputw.ai]:2222 ssh-ed25519 AAAACPUONLYTESTKEY\n",
        encoding="utf-8",
    )
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    bound = validator.validate_project_known_hosts_artifact(
        artifact, digest, "AUTHENTICATED_PROVIDER_DASHBOARD"
    )
    assert bound["sha256"] == digest
    with pytest.raises(validator.ContractError):
        validator.validate_project_known_hosts_artifact(
            artifact, digest, "SSH_KEYSCAN_ONLY"
        )


def test_runtime_image_supports_template_and_custom_image_without_selecting_one(contract: dict) -> None:
    runtime = contract["runtime_image_identity"]
    assert runtime["selected_mode"] == "UNRESOLVED"
    assert runtime["supported_modes"] == ["TEMPLATE", "CUSTOM_IMAGE"]
    assert runtime["template"]["mode"] == "TEMPLATE"
    assert runtime["custom_image"]["mode"] == "CUSTOM_IMAGE"
    assert runtime["template"]["template_id"] is None
    assert runtime["custom_image"]["immutable_image_reference"] is None
    assert "MUTABLE_TAG" in runtime["mutable_tag_policy"]


def test_vault_and_instance_local_storage_are_distinct(contract: dict) -> None:
    storage = contract["storage_semantics"]
    assert storage["persistent_domain"]["path"] == "/vault"
    assert storage["persistent_domain"]["live_measurement_status"] == "D0_LIVE_FIELD_REQUIRED"
    assert storage["persistent_domain"]["actual_mount_identity"] is None
    assert storage["instance_local_domain"]["persistence_semantics"] == "TIED_TO_INSTANCE_OR_NODE_LIFECYCLE"
    assert storage["instance_local_domain"]["persistence_assumption"] == "FORBIDDEN"


def test_stop_terminates_compute_authority_and_restart_requires_revalidation(contract: dict) -> None:
    lifecycle = contract["lifecycle_and_billing_authority"]
    assert lifecycle["STOP"]["ends_future_compute_billing"] is True
    assert lifecycle["STOP"]["compute_authority_after_stop"] == "NONE"
    assert lifecycle["RESTART"]["previous_d0_live_authority_inherited"] is False
    assert lifecycle["RESTART"]["requires_live_environment_revalidation"] is True
    assert lifecycle["RESTART"]["revalidation_required_before_use"] is True
    assert lifecycle["RESTART"]["stale_d0_evidence_action"] == "FAIL_CLOSED"


def test_cost_caps_and_billable_ports_are_owner_controls(contract: dict) -> None:
    cost = contract["cost_control"]
    for key in (
        "owner_compute_cost_cap",
        "owner_storage_cost_cap",
        "owner_port_cost_cap",
        "owner_total_cost_cap",
    ):
        assert cost[key]["status"] == "OWNER_INPUT_REQUIRED"
        assert cost[key]["amount"] is None
    assert cost["additional_billable_ports_default"] == "FORBIDDEN_UNLESS_EXPLICITLY_APPROVED"
    assert cost["price_refresh_required_before_instance_creation"] is True


def test_canonical_m0_remains_bf16_without_automatic_fallback(contract: dict) -> None:
    m0 = contract["canonical_m0"]
    assert m0["model"] == "mistralai/Mixtral-8x7B-Instruct-v0.1"
    assert m0["precision"] == "BF16"
    assert m0["runtime"] == "vLLM"
    assert m0["platform_candidate"] == "RTX PRO 6000 WS 96GB"
    assert m0["quantization"] == "NONE"
    assert m0["cpu_offload"] == "NONE"
    assert m0["automatic_fp8_selection"] is False
    assert m0["automatic_quantization_selection"] is False
    assert m0["automatic_cpu_offload_selection"] is False


def test_validation_records_no_live_action_and_no_network_code() -> None:
    contract = validator.load_json(PACKAGE / "provider_adaptation.json")
    results = contract["validation_results"]
    for key in (
        "gpu_instance_created",
        "ssh_connection_attempted",
        "ssh_keyscan_attempted",
        "d0_executed",
        "gate_m_executed",
        "m0_executed",
        "gpu_queried",
        "model_downloaded",
        "network_used",
    ):
        assert results[key] is False
    source = (PACKAGE / "validate_provider_adaptation.py").read_text(encoding="utf-8")
    assert "subprocess" not in source
    assert "socket" not in source
    assert "paramiko" not in source


def test_manifest_and_review_preserve_frozen_boundary_and_pending_review() -> None:
    manifest = json.loads((PACKAGE / "provider_adaptation_manifest.json").read_text(encoding="utf-8"))
    review = json.loads((PACKAGE / "review_request.json").read_text(encoding="utf-8"))
    assert manifest["base_r12_r5"]["frozen_candidate_commit"] == "da3d33238365664435664e6ae1d18714e6b68ee2"
    assert manifest["base_r12_r5"]["review_closure_commit"] == "87c8a866e44387100dadf9a087cf55a37c7cc9e0"
    assert review["base_r12_r5"]["frozen_candidate_commit"] == manifest["base_r12_r5"]["frozen_candidate_commit"]
    assert review["verdict"] == "PENDING"
    assert "GPU instance creation" in review["does_not_authorize"]


def test_json_schema_accepts_contract() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((PACKAGE / "provider_adaptation.schema.json").read_text(encoding="utf-8"))
    contract = json.loads((PACKAGE / "provider_adaptation.json").read_text(encoding="utf-8"))
    validator_class = getattr(jsonschema, "Draft202012Validator", None)
    if validator_class is None:
        validator_class = jsonschema.Draft7Validator
    validator_class.check_schema(schema)
    validator_class(schema).validate(contract)

#!/usr/bin/env python3
"""Validate the CPU-only GPUtw.ai provider adaptation contract.

This validator intentionally has no network, SSH, GPU or provider-client code.
It validates only prospective governance data and fails closed while the
gateway host key and live instance inputs are unresolved.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parent
CONTRACT_PATH = PACKAGE_ROOT / "provider_adaptation.json"
MANIFEST_PATH = PACKAGE_ROOT / "provider_adaptation_manifest.json"
SCHEMA_PATH = PACKAGE_ROOT / "provider_adaptation.schema.json"
REVIEW_PATH = PACKAGE_ROOT / "review_request.json"
KNOWN_HOSTS_TEMPLATE_PATH = PACKAGE_ROOT / "known_hosts.gputw.template"
CHECKSUMS_PATH = PACKAGE_ROOT / "checksums.sha256"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
TRUSTED_HOST_KEY_SOURCES = frozenset(
    {
        "AUTHENTICATED_PROVIDER_DASHBOARD",
        "OFFICIAL_PROVIDER_SUPPORT_RESPONSE",
        "OFFICIAL_PROVIDER_DOCUMENTATION",
        "INDEPENDENTLY_AUTHENTICATED_PROVIDER_CHANNEL",
    }
)
CHECKSUM_MEMBERS = (
    "__init__.py",
    "known_hosts.gputw.template",
    "provider_adaptation.json",
    "provider_adaptation.schema.json",
    "provider_adaptation_manifest.json",
    "review_request.json",
    "validate_provider_adaptation.py",
)


class ContractError(RuntimeError):
    """Raised when the provider adaptation is unsafe or malformed."""


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ContractError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicates,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ContractError(f"non-finite JSON value: {token}")
            ),
        )
    except ContractError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot load {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractError(f"{path}: JSON root must be an object")
    return value


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def _contains_secret_material(value: Any, location: str = "$") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            lowered = key.casefold()
            if any(token in lowered for token in ("private_key", "secret", "token", "password")):
                if child not in (None, False, "", "FORBIDDEN_IN_REPOSITORY_EVIDENCE_LOGS_MANIFESTS_AND_ARTIFACTS"):
                    found.append(f"{location}.{key}")
            found.extend(_contains_secret_material(child, f"{location}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_contains_secret_material(child, f"{location}[{index}]"))
    elif isinstance(value, str) and "-----BEGIN" in value:
        found.append(location)
    return found


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def d0_ssh_authorized(contract: dict[str, Any]) -> bool:
    """Return whether the contract permits D0 SSH authority.

    A provider-adaptation contract can never grant D0 by itself. This helper
    makes the unresolved host-key fail-closed rule explicit for tests and
    downstream governance tooling.
    """

    server = contract["identity_model"]["server_authentication"]
    host_key = contract["host_key_governance"]
    return bool(
        server["gateway_host_key_status"] == "TRUSTED"
        and server["trusted_gateway_host_key_sha256"]
        and server["trusted_provenance"]
        and server["d0_ssh_authority"] == "D0_ONLY_AFTER_SEPARATE_APPROVAL"
        and host_key["project_specific_artifact"]["status"] == "CHECKSUM_BOUND"
        and host_key["strict_ssh_options"]["connection_allowed_while_unresolved"] is False
    )


def validate_project_known_hosts_artifact(
    path: Path, expected_sha256: str, trusted_provenance: str
) -> dict[str, str]:
    """Validate a future project-specific GPUtw known-hosts artifact.

    This is deliberately a byte-level check only. It does not discover a host
    key or turn ``ssh-keyscan`` output into trust. The caller must supply a
    digest and provenance obtained through an independently authenticated
    provider channel.
    """

    require(path.name == "known_hosts.gputw", "formal known-hosts artifact has the wrong filename")
    require(path.is_file() and not path.is_symlink(), "formal known-hosts artifact must be a regular non-symlink file")
    require(SHA256_RE.fullmatch(expected_sha256) is not None, "expected known-hosts SHA-256 is malformed")
    require(trusted_provenance in TRUSTED_HOST_KEY_SOURCES, "known-hosts provenance is not independently trusted")
    raw = path.read_bytes()
    require(_file_sha256(path) == expected_sha256, "known-hosts artifact checksum mismatch")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractError("known-hosts artifact is not UTF-8") from exc
    entries = [line for line in text.splitlines() if line.strip()]
    require(len(entries) == 1, "known-hosts artifact must contain exactly one gateway entry")
    fields = entries[0].split()
    require(len(fields) == 3, "known-hosts gateway entry must contain host, algorithm and key")
    require(fields[0] == "[ssh.gputw.ai]:2222", "known-hosts entry is not the GPUtw gateway")
    require(fields[1].startswith("ssh-"), "known-hosts entry does not contain an SSH host-key algorithm")
    require(fields[2] and not fields[2].startswith("#"), "known-hosts entry does not contain a host-key blob")
    return {"path": str(path), "sha256": expected_sha256, "trusted_provenance": trusted_provenance}


def validate_contract(contract: dict[str, Any]) -> None:
    require(
        contract["schema_version"] == "moe-simulator-phase7-gputw-provider-adaptation-v1",
        "unexpected contract schema version",
    )

    adaptation = contract["adaptation"]
    require(adaptation["revision"] == "R1", "adaptation revision must be R1")
    require(adaptation["status"] == "CPU_CONTRACT_IMPLEMENTED", "contract is not CPU-only implemented")
    require(adaptation["review_status"] == "PENDING", "review status must remain pending")
    base = adaptation["base_r12_r5"]
    require(COMMIT_RE.fullmatch(base["frozen_candidate_commit"]) is not None, "invalid R12-R5 candidate commit")
    require(base["frozen_candidate_commit"] == "da3d33238365664435664e6ae1d18714e6b68ee2", "wrong R12-R5 candidate commit")
    require(base["review_closure_commit"] == "87c8a866e44387100dadf9a087cf55a37c7cc9e0", "wrong R12-R5 closure commit")
    require(base["review_verdict"] == "GO/GO/GO", "R12-R5 verdict changed")
    require(base["frozen_candidate_immutable"] is True, "frozen candidate must remain immutable")
    require(base["historical_evidence_immutable"] is True, "historical evidence must remain immutable")

    state = contract["state"]
    require(state["r12_r5"] == "IMMUTABLE_GO/GO/GO", "R12-R5 state is not immutable GO/GO/GO")
    require(state["provider_adaptation"] == "CPU_CONTRACT_IMPLEMENTED", "provider adaptation state changed")
    require(state["provider_adaptation_review"] == "PENDING", "provider adaptation review must be pending")
    require(state["d0_execution"] == "NOT_AUTHORIZED", "D0 execution is not allowed")
    require(state["gate_m"] == "NOT_AUTHORIZED", "Gate M is not allowed")
    require(state["m0"] == "NOT_AUTHORIZED", "M0 is not allowed")
    require(state["gpu_authority"] == "NONE", "GPU authority must be NONE")

    provider = contract["provider_semantics"]
    require(provider["provider"] == "GPUtw.ai", "provider must be GPUtw.ai")
    require(provider["execution_environment"] == "DEDICATED_GPU_CONTAINER_INSTANCE", "wrong execution environment")
    require(provider["billing_model"] == "PREPAID_METERED", "GPUtw billing model changed")
    require(provider["compute_billing"] == "ACCRUES_WHILE_INSTANCE_IS_RUNNING", "wrong compute billing semantics")
    require(provider["billing_termination_actions"] == ["STOP", "DELETE"], "wrong billing termination actions")
    require(provider["stop_or_delete_ends_future_compute_billing"] is True, "STOP/DELETE must terminate future compute billing")
    require(provider["provider_fixed_lease_seconds"] == "NONE_OBSERVED", "21600 must not be a provider lease")
    require(provider["provider_grace_period"] == "NOT_APPLICABLE", "provider grace must not be required")

    envelope = contract["owner_execution_envelope"]
    require(envelope["owner_execution_budget_seconds"] == 21600, "owner hard cap must be 21600 seconds")
    require(envelope["owner_execution_budget_type"] == "HARD_CAP", "owner budget must be a hard cap")
    require(envelope["semantic_role"] == "OWNER_IMPOSED_EXECUTION_ENVELOPE", "wrong budget semantic role")
    require(envelope["provider_lease_semantics"] == "NOT_A_PROVIDER_LEASE", "owner budget was modeled as provider lease")
    require(envelope["provider_lease_seconds"] == "NONE_OBSERVED", "provider lease seconds must be unobserved")
    require(envelope["provider_grace_credit"] == "NONE", "provider grace cannot receive formal credit")
    stages = envelope["stage_bounds_seconds"]
    require(stages == {"D0": 300, "Gate M": 5400, "M0": 14400, "release": 900}, "stage bounds changed")
    require(sum(stages.values()) == envelope["bounded_work_seconds"] == 21000, "bounded stage arithmetic changed")
    require(envelope["remaining_slack_seconds"] == 600, "owner-envelope slack changed")
    require(envelope["bounded_work_seconds"] + envelope["remaining_slack_seconds"] == envelope["owner_execution_budget_seconds"], "owner-envelope arithmetic does not close")
    require(envelope["stage_bounds_are_provider_guarantees"] is False, "stage bounds must not be provider guarantees")

    identity = contract["identity_model"]
    require(set(identity) == {"provider_identity", "instance_identity", "ssh_gateway_identity", "client_authentication", "server_authentication"}, "identity classes must remain separated")
    require(identity["provider_identity"]["provider"] == "GPUtw.ai", "provider identity changed")
    instance = identity["instance_identity"]
    require(instance["instance_id"] is None and instance["catalog_gpu_id"] is None, "live instance identity was invented")
    require(instance["instance_state"] == "UNOBSERVED", "instance state must remain unobserved")
    gateway = identity["ssh_gateway_identity"]
    require(gateway["host"] == "ssh.gputw.ai" and gateway["port"] == 2222, "wrong GPUtw SSH gateway")
    require(gateway["principal_template"] == "pod-<instance-id>", "instance principal must be modeled")
    require(gateway["ip_only_identity"] is False, "gateway identity must not be IP-only")
    client = identity["client_authentication"]
    require(client["method"] == "SSH_PUBLIC_KEY", "wrong client authentication method")
    require(client["account_public_key_registered"] == "CONFIRMED_BY_OWNER", "owner-confirmed key state missing")
    for key in ("credential_provisioning_required", "workflow_must_generate_keys", "workflow_must_upload_keys", "workflow_must_modify_provider_credentials"):
        require(client[key] is False, f"client credential provisioning unexpectedly required: {key}")
    require(client["private_key_material_policy"].startswith("FORBIDDEN"), "private-key repository policy missing")
    server = identity["server_authentication"]
    require(server["gateway_host_key_status"] == "UNRESOLVED", "gateway host key must remain unresolved")
    require(server["trusted_gateway_host_key_sha256"] is None and server["trusted_provenance"] is None, "untrusted host key data was promoted")
    require(server["d0_ssh_authority"] == "NONE", "unresolved host key must yield no D0 SSH authority")

    host_key = contract["host_key_governance"]
    require(host_key["gateway_endpoint"] == "[ssh.gputw.ai]:2222", "wrong host-key endpoint")
    require(host_key["existing_known_hosts_entry_policy"] == "MAY_BE_OBSERVED_DOES_NOT_ESTABLISH_TRUST_PROVENANCE", "known_hosts observation policy weakened")
    require(host_key["ssh_keyscan_policy"] == "MAY_COLLECT_OBSERVATION_MUST_NOT_ESTABLISH_TRUST_BY_ITSELF", "ssh-keyscan trust policy weakened")
    require(len(host_key["trusted_host_key_sources"]) >= 3, "trusted host-key source set is incomplete")
    artifact = host_key["project_specific_artifact"]
    require(artifact["status"] == "REQUIRED_NOT_PRESENT", "a live known-hosts artifact was invented")
    require(artifact["checksum_bound_before_formal_d0"] is True, "known-hosts checksum binding is required")
    require(artifact["personal_known_hosts_file_allowed"] is False, "personal known_hosts use must be forbidden")
    strict = host_key["strict_ssh_options"]
    require(strict["user_known_hosts_file"] == "PROJECT_SPECIFIC_FROZEN_ARTIFACT", "formal D0 must use a project-specific known-hosts file")
    require(strict["strict_host_key_checking"] == "yes", "strict host-key checking is required")
    require(strict["connection_allowed_while_unresolved"] is False, "unresolved host key must block connection")
    require(host_key["unresolved_behavior"]["d0_ssh_authority"] == "NONE", "unresolved host key authority changed")
    require(d0_ssh_authorized(contract) is False, "the current unresolved contract cannot authorize D0 SSH")

    storage = contract["storage_semantics"]
    persistent = storage["persistent_domain"]
    require(persistent["path"] == "/vault", "persistent storage domain must be /vault")
    require(persistent["actual_mount_identity"] is None and persistent["capacity_bytes"] is None and persistent["free_bytes"] is None, "live /vault measurements were invented")
    require(persistent["live_measurement_status"] == "D0_LIVE_FIELD_REQUIRED", "Vault live-field status changed")
    local = storage["instance_local_domain"]
    require(local["persistence_semantics"] == "TIED_TO_INSTANCE_OR_NODE_LIFECYCLE", "instance-local persistence semantics changed")
    require(local["persistence_assumption"] == "FORBIDDEN", "instance-local persistence must not be assumed")
    require(storage["credentials_in_either_domain"] == "FORBIDDEN", "credential storage policy changed")

    runtime = contract["runtime_image_identity"]
    require(runtime["selected_mode"] == "UNRESOLVED", "runtime image was prematurely selected")
    require(runtime["supported_modes"] == ["TEMPLATE", "CUSTOM_IMAGE"], "runtime image modes changed")
    require(runtime["template"]["mode"] == "TEMPLATE" and runtime["custom_image"]["mode"] == "CUSTOM_IMAGE", "template/custom-image branches missing")
    require(runtime["template"]["template_id"] is None and runtime["custom_image"]["immutable_image_reference"] is None, "runtime image identity was invented")
    require("MUTABLE_TAG" in runtime["mutable_tag_policy"], "mutable tags must not be trusted")

    lifecycle = contract["lifecycle_and_billing_authority"]
    stop = lifecycle["STOP"]
    require(stop["permitted"] is True and stop["ends_future_compute_billing"] is True and stop["compute_authority_after_stop"] == "NONE", "STOP authority semantics changed")
    restart = lifecycle["RESTART"]
    require(restart["requires_live_environment_revalidation"] is True and restart["revalidation_required_before_use"] is True, "RESTART revalidation is required")
    require(restart["stale_d0_evidence_action"] == "FAIL_CLOSED", "stale D0 evidence must fail closed")

    cost = contract["cost_control"]
    for name in ("owner_compute_cost_cap", "owner_storage_cost_cap", "owner_port_cost_cap", "owner_total_cost_cap"):
        require(name in cost, f"missing cost cap: {name}")
        require(cost[name]["status"] == "OWNER_INPUT_REQUIRED", f"cost cap prematurely frozen: {name}")
        require(cost[name]["amount"] is None and cost[name]["currency"] is None, f"live cost data invented: {name}")
    require(cost["additional_billable_ports_default"] == "FORBIDDEN_UNLESS_EXPLICITLY_APPROVED", "billable port default changed")
    require(cost["price_refresh_required_before_instance_creation"] is True, "price refresh is required")
    require(cost["availability_refresh_required_before_instance_creation"] is True, "availability refresh is required")

    m0 = contract["canonical_m0"]
    require(m0["platform_candidate"] == "RTX PRO 6000 WS 96GB", "canonical platform changed")
    require(m0["model"] == "mistralai/Mixtral-8x7B-Instruct-v0.1", "canonical model changed")
    require(m0["precision"] == "BF16" and m0["runtime"] == "vLLM", "canonical precision/runtime changed")
    require(m0["quantization"] == "NONE" and m0["cpu_offload"] == "NONE", "canonical M0 fallback state changed")
    for key in ("automatic_fp8_selection", "automatic_quantization_selection", "automatic_cpu_offload_selection"):
        require(m0[key] is False, f"automatic fallback enabled: {key}")
    require("PRESERVE_CANONICAL_BF16_FAILURE" in m0["capacity_or_runtime_failure_policy"], "canonical failure policy weakened")
    require("SEPARATE_IDENTITY" in m0["runtime_variant_policy"], "runtime variants are not separated")

    validation = contract["validation_results"]
    require(validation["validation_mode"] == "CPU_ONLY_NETWORK_FREE", "validation mode must be CPU-only and network-free")
    for key in ("gpu_instance_created", "ssh_connection_attempted", "ssh_keyscan_attempted", "d0_executed", "gate_m_executed", "m0_executed", "gpu_queried", "model_downloaded", "network_used"):
        require(validation[key] is False, f"forbidden live action was recorded: {key}")
    require(not _contains_secret_material(contract), "private credential material is present in the contract")


def validate_manifest(manifest: dict[str, Any]) -> None:
    require(manifest["schema_version"] == "moe-simulator-phase7-gputw-provider-adaptation-manifest-v1", "unexpected manifest schema version")
    require(manifest["package_id"] == "moe-simulator-phase7-gputw-provider-adaptation-r1", "unexpected package id")
    require(manifest["adaptation_revision"] == "R1", "manifest revision changed")
    require(manifest["status"] == "CPU_CONTRACT_IMPLEMENTED_REVIEW_PENDING", "manifest status changed")
    require(manifest["base_r12_r5"]["frozen_candidate_commit"] == "da3d33238365664435664e6ae1d18714e6b68ee2", "manifest base candidate changed")
    require(manifest["base_r12_r5"]["review_closure_commit"] == "87c8a866e44387100dadf9a087cf55a37c7cc9e0", "manifest closure commit changed")
    require(manifest["base_r12_r5"]["immutable"] is True, "manifest must preserve frozen boundary")
    for key in ("contract_path", "schema_path", "validator_path", "review_request_path", "known_hosts_template_path", "checksums_path"):
        path = PACKAGE_ROOT / manifest[key]
        require(path.exists() and path.is_file(), f"manifest artifact missing: {manifest[key]}")
    require(manifest["future_formal_known_hosts_path"] == "phase7/application/known_hosts.gputw", "future known-hosts path changed")
    require(manifest["checksum_policy"]["package_files_must_be_checksum_bound_before_formal_d0"] is True, "checksum policy changed")
    require(manifest["checksum_policy"]["observed_keyscan_bytes_are_not_trust_provenance"] is True, "keyscan policy changed")
    require(manifest["checksum_policy"]["private_credentials_are_excluded"] is True, "credential exclusion policy changed")
    require(manifest["execution_authority"] == {"gpu_authority": "NONE", "d0": "NOT_AUTHORIZED", "gate_m": "NOT_AUTHORIZED", "m0": "NOT_AUTHORIZED"}, "execution authority changed")


def validate_checksums() -> None:
    try:
        lines = CHECKSUMS_PATH.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ContractError(f"cannot load package checksums: {exc}") from exc
    listed: dict[str, str] = {}
    for line in lines:
        require("  " in line, "malformed package checksum line")
        digest, relative = line.split("  ", 1)
        require(SHA256_RE.fullmatch(digest) is not None, f"malformed package checksum: {relative}")
        require(relative not in listed, f"duplicate package checksum: {relative}")
        listed[relative] = digest
    require(tuple(listed) == CHECKSUM_MEMBERS, "package checksum members are not the exact sorted set")
    for relative, expected in listed.items():
        path = PACKAGE_ROOT / relative
        require(path.is_file() and not path.is_symlink(), f"package checksum member is missing: {relative}")
        require(_file_sha256(path) == expected, f"package checksum mismatch: {relative}")


def validate_review_request(review: dict[str, Any]) -> None:
    require(review["schema_version"] == "moe-simulator-phase7-gputw-provider-adaptation-review-request-v1", "unexpected review schema version")
    require(review["adaptation_revision"] == "R1", "review revision changed")
    require(review["verdict"] == "PENDING", "review must remain pending")
    require(len(review["required_review_roles"]) == 3, "three review roles are required")
    require("provider-adaptation review has not been recorded" in review["blockers"], "review blocker missing")
    require("D0" in " ".join(review["does_not_authorize"]), "review request must not authorize D0")


def validate_package() -> dict[str, Any]:
    contract = load_json(CONTRACT_PATH)
    manifest = load_json(MANIFEST_PATH)
    review = load_json(REVIEW_PATH)
    validate_contract(contract)
    validate_manifest(manifest)
    validate_review_request(review)
    validate_checksums()
    require(KNOWN_HOSTS_TEMPLATE_PATH.read_text(encoding="utf-8").startswith("# GPUtw gateway known-hosts template"), "known-hosts template changed")
    return {
        "status": "PASS",
        "package_id": manifest["package_id"],
        "adaptation_revision": "R1",
        "review": "PENDING",
        "d0_ssh_authority": "NONE",
        "d0_execution": "NOT_AUTHORIZED",
        "gate_m": "NOT_AUTHORIZED",
        "m0": "NOT_AUTHORIZED",
        "gpu_authority": "NONE",
        "network_free": True,
        "contract_sha256": _file_sha256(CONTRACT_PATH),
        "package_checksums_sha256": _file_sha256(CHECKSUMS_PATH),
        "known_hosts_status": "REQUIRED_NOT_PRESENT",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-dir", type=Path, default=PACKAGE_ROOT)
    args = parser.parse_args(argv)
    if args.package_dir.resolve() != PACKAGE_ROOT.resolve():
        print(json.dumps({"status": "FAIL", "error": "this R1 validator only accepts its immutable package directory"}, indent=2))
        return 2
    try:
        result = validate_package()
    except (ContractError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}, indent=2, sort_keys=True))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

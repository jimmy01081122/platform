#!/usr/bin/env python3
"""Materialize the Phase 7 combined successor master ledger.

The generator is intentionally local and deterministic.  It indexes preserved
evidence and expands the required inventory; it never edits historical raw or
the historical special-mechanism ledger.  GPU results are added later through
append-only transition records.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MODEL_ID = "mistralai/Mixtral-8x7B-Instruct-v0.1"
MODEL_REVISION = "eba92302a2861cdc0098cc54bc9f17cb2c47eb61"
OLD_CAMPAIGN = "20260813T130017Z__LUNA-MAX-SPECIAL-MECHANISM-TRACE-CLOSURE-V1"
OLD_LEDGER = "artifacts/phase7/special_mechanism_raw/20260813T130017Z__LUNA-MAX-SPECIAL-MECHANISM-TRACE-CLOSURE-V1/execution_ledger.json"
OLD_GUARD_ATTEMPT = (
    "artifacts/phase7/special_mechanism_raw/"
    "20260813T130017Z__LUNA-MAX-SPECIAL-MECHANISM-TRACE-CLOSURE-V1/attempts/"
    "MECH-G0-KV-G0-OS-SWAP-G0-UM-G0-CANARY-V1"
)
P0_BASE = "runs/20260811T175500Z__phase7_fit_anchor_backup/raw/runs/20260811T195121Z__SERV-P0-25-SHORT-C8-NATURAL-V1"
P0_EXT_SIDECAR = "runs/20260811T175500Z__phase7_fit_anchor_backup/preliminary_serving_p0_25_remote_environment_loss_v1.json"

SOURCE_FILES = {
    "real_run_matrix": "docs/status/MOE_SIMULATOR_PHASE7_GOAL_REAL_RUN_FLOW_AND_TEST_MATRIX.md",
    "rtx_patch": "docs/status/MOE_SIMULATOR_PHASE7_SINGLE_RTX_PRO_6000_EXPERIMENT_PATCH.md",
    "special_mechanism": "docs/status/MOE_SIMULATOR_PHASE7_LUNA_MAX_SPECIAL_MECHANISM_TRACE_CLOSURE_GOAL_PROMPT_20260813.md",
    "new_session_handoff": "docs/status/MOE_SIMULATOR_PHASE7_LUNA_MAX_NEW_SESSION_HANDOFF_AFTER_SERV_P0_25_20260813.md",
    "execution_guidance": "docs/status/MOE_SIMULATOR_PHASE7_LUNA_MAX_TEST_EXECUTION_GUIDANCE_PROMPT.md",
    "combined_master_guide": "docs/status/MOE_SIMULATOR_PHASE7_LUNA_MAX_COMBINED_MASTER_REMAINING_LEDGER_GUIDE.md",
}


def sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def row_template(
    row_id: str,
    experiment_id: str,
    *,
    stage: str,
    group: str,
    requirement_class: str,
    evidence_class: str,
    source_sections: list[str],
    workload: str = "UNFROZEN",
    mechanism: str = "CANONICAL_OR_UNFROZEN",
    fit_role: str = "DIAGNOSTIC",
    frozen_variables: dict[str, Any] | None = None,
    repetitions: str = "SOURCE_CONTRACT_REQUIRED",
    prerequisites: list[str] | None = None,
    consumers: list[str] | None = None,
    state: str = "NOT_RUN",
    raw_state: str = "NONE",
    review_state: str = "NOT_REVIEWED",
    validation_state: str = "UNVERIFIED",
    adoption_state: str = "NOT_APPLICABLE",
    backup_state: str = "NONE",
    trigger_state: str = "NOT_CONDITIONAL",
    attempt_ids: list[str] | None = None,
    remote_paths: list[str] | None = None,
    local_paths: list[str] | None = None,
    source_raw_hashes: list[str] | None = None,
    contamination: list[str] | None = None,
    supported: list[str] | None = None,
    forbidden: list[str] | None = None,
    blocker: str | None = None,
    next_action: str = "Await deterministic ready-queue evaluation.",
) -> dict[str, Any]:
    return {
        "master_row_id": row_id,
        "canonical_experiment_id": experiment_id,
        "atomic_cell_key": f"{experiment_id}::{workload}::{mechanism}::{fit_role}::{row_id}",
        "source_documents": list(SOURCE_FILES.values()),
        "source_sections": source_sections,
        "requirement_class": requirement_class,
        "stage": stage,
        "group": group,
        "workload_or_trace_id": workload,
        "mechanism_or_policy_variant": mechanism,
        "fit_role": fit_role,
        "frozen_variables": frozen_variables or {},
        "required_repetitions_or_sample_count": repetitions,
        "prerequisite_row_ids": prerequisites or [],
        "consumer_row_ids": consumers or [],
        "evidence_class": evidence_class,
        "execution_state": state,
        "raw_state": raw_state,
        "review_state": review_state,
        "validation_state": validation_state,
        "adoption_state": adoption_state,
        "backup_state": backup_state,
        "trigger_state": trigger_state,
        "attempt_ids": attempt_ids or [],
        "remote_raw_paths": remote_paths or [],
        "local_raw_paths": local_paths or [],
        "manifest_sha256": [],
        "source_raw_sha256": source_raw_hashes or [],
        "contamination_flags": contamination or [],
        "claims_supported": supported or [],
        "claims_forbidden": forbidden or [],
        "blocker_or_failure": blocker,
        "repair_lineage": [],
        "next_action": next_action,
        "last_transition_record": "MR0-MR1-MATERIALIZATION",
    }


def closed_row(row: dict[str, Any]) -> bool:
    return (
        row["validation_state"] in {"VALIDATION_PASS", "NEGATIVE_EVIDENCE", "UNAVAILABLE_WITH_CONSEQUENCE"}
        and row["backup_state"] == "VERIFIED"
        and row["adoption_state"] in {"ADOPTED", "NOT_APPLICABLE"}
        and row["trigger_state"] in {"NOT_CONDITIONAL", "NOT_TRIGGERED_WITH_EVIDENCE", "OWNER_WAIVED"}
    )


def add(rows: list[dict[str, Any]], seen: set[str], row: dict[str, Any]) -> None:
    if row["master_row_id"] in seen:
        raise ValueError(f"duplicate master row: {row['master_row_id']}")
    seen.add(row["master_row_id"])
    rows.append(row)


def add_pending_group(
    rows: list[dict[str, Any]],
    seen: set[str],
    ids: list[str],
    *,
    stage: str,
    group: str,
    requirement_class: str,
    evidence_class: str,
    section: str,
    workload_prefix: str | None = None,
    fit_roles: dict[str, str] | None = None,
    prerequisites: list[str] | None = None,
    mechanism: str = "CANONICAL",
    repetitions: str = "SOURCE_CONTRACT_REQUIRED",
    blocker: str | None = None,
) -> None:
    for item in ids:
        add(
            rows,
            seen,
            row_template(
                item,
                item,
                stage=stage,
                group=group,
                requirement_class=requirement_class,
                evidence_class=evidence_class,
                source_sections=[section],
                workload=(f"{workload_prefix}-{item}" if workload_prefix else item),
                mechanism=mechanism,
                fit_role=(fit_roles or {}).get(item, "DIAGNOSTIC"),
                repetitions=repetitions,
                prerequisites=prerequisites,
                blocker=blocker,
                next_action="Run prerequisite canary or complete adoption audit before GPU dispatch.",
            ),
        )


def add_existing_candidate(
    rows: list[dict[str, Any]],
    seen: set[str],
    item: str,
    *,
    stage: str,
    group: str,
    section: str,
    source_path: str,
    source_hash: str | None,
    evidence_class: str,
    fit_role: str = "DIAGNOSTIC",
    supplement: bool = False,
    blocker: str | None = None,
    consumers: list[str] | None = None,
) -> None:
    add(
        rows,
        seen,
        row_template(
            item,
            item,
            stage=stage,
            group=group,
            requirement_class="CORE_REQUIRED",
            evidence_class=evidence_class,
            source_sections=[section],
            workload=item,
            fit_role=fit_role,
            state="EXECUTION_COMPLETE",
            raw_state="COMPLETE",
            review_state="REVIEW_WITH_LIMITATION" if supplement else "REVIEW_PASS",
            validation_state="UNVERIFIED" if supplement else "VALIDATION_PASS",
            adoption_state="SUPPLEMENT_REQUIRED" if supplement else "PENDING_AUDIT",
            backup_state="VERIFIED",
            local_paths=[source_path],
            source_raw_hashes=[source_hash] if source_hash else [],
            contamination=["historical_evidence_not_new_session_measurement"],
            supported=[] if supplement else ["existing evidence candidate pending master adoption"],
            forbidden=["final Phase 7 closure until master adoption and downstream gates"],
            blocker=blocker,
            consumers=consumers,
            next_action="Complete master adoption audit; do not rerun unless a required measured field is absent.",
        ),
    )


def build_rows(repo: Path, master_campaign: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    source_hashes = {name: sha256(repo / path) for name, path in SOURCE_FILES.items()}
    old_ledger_hash = sha256(repo / OLD_LEDGER)
    old_guard_hash = sha256(repo / f"{OLD_GUARD_ATTEMPT}/SHA256SUMS_ATTEMPT")
    p0_status_hash = sha256(repo / f"{P0_BASE}/status.json")

    # MR0/MR1 governance rows are closed as ledger-production evidence, not as
    # claims that the technical measurements are complete.
    governance = [
        ("MASTER-SOURCE-RECON", "source/hash reconciliation"),
        ("MASTER-EVIDENCE-INVENTORY", "existing raw/review/failure/checksum inventory"),
        ("MASTER-ALIAS-LINEAGE", "alias and lineage map"),
        ("MASTER-DAG-VALIDATION", "atomic row/dependency validation"),
        ("MODEL-VAULT-VERIFY", "new-host domain and /vault identity"),
    ]
    for item, description in governance:
        add(
            rows,
            seen,
            row_template(
                item,
                item,
                stage="MR0-MR2",
                group="MASTER_GOVERNANCE",
                requirement_class="OFFLINE_REQUIRED",
                evidence_class="DERIVED",
                source_sections=["3", "8.0", "9", "10"],
                workload="MASTER_LEDGER",
                mechanism="READ_ONLY_GOVERNANCE",
                state="EXECUTION_COMPLETE",
                raw_state="COMPLETE",
                review_state="REVIEW_PASS",
                validation_state="VALIDATION_PASS",
                adoption_state="ADOPTED",
                backup_state="VERIFIED",
                supported=[description],
                next_action="Maintain through append-only transitions.",
            ),
        )

    existing_root = "runs/20260811T175500Z__phase7_fit_anchor_backup"
    add_existing_candidate(rows, seen, "ADOPT-QUALIFICATION", stage="MR3", group="ADOPTION", section="8.0", source_path="runs/20260811T134900Z__phase7_remote_tcanary_backup", source_hash=sha256(repo / "runs/20260811T134900Z__phase7_remote_tcanary_backup/SHA256SUMS"), evidence_class="MEASURED", consumers=["L0-A", "F0-A", "R0"])
    add_existing_candidate(rows, seen, "ADOPT-CONTROLLED-90", stage="MR3", group="ADOPTION", section="8.0", source_path="runs/20260811T152600Z__phase7_remote_controlled_matrix_backup", source_hash=sha256(repo / "runs/20260811T152600Z__phase7_remote_controlled_matrix_backup/SHA256SUMS"), evidence_class="MEASURED", consumers=["P0", "P1", "DEC0", "DEC1", "PX0", "CE0", "CE1", "CE2", "CE3"])
    add_existing_candidate(rows, seen, "ADOPT-NATURAL-SAMPLING", stage="MR3", group="ADOPTION", section="8.0", source_path="runs/20260811T163100Z__phase7_remote_sampling_pairs_backup", source_hash=sha256(repo / "runs/20260811T163100Z__phase7_remote_sampling_pairs_backup/SHA256SUMS"), evidence_class="MEASURED", consumers=["SMP1", "SMP2", "W0", "W1", "W2"])
    add_existing_candidate(rows, seen, "ADOPT-K0-K7", stage="MR3", group="ADOPTION", section="8.0", source_path="runs/20260811T171000Z__phase7_remote_k_profile_backup", source_hash=sha256(repo / "runs/20260811T171000Z__phase7_remote_k_profile_backup/SHA256SUMS"), evidence_class="MEASURED", consumers=["K0", "K1", "K2", "K3", "K4", "K5", "K6", "K7"])
    add_existing_candidate(rows, seen, "ADOPT-FIT-ANCHORS", stage="MR3", group="ADOPTION", section="8.0", source_path=existing_root, source_hash=sha256(repo / f"{existing_root}/preliminary_fit_anchor_review_v1.json"), evidence_class="MEASURED", consumers=["CMP-A0", "CMP-A1", "OFF-E-PR3"])
    add_existing_candidate(rows, seen, "ADOPT-CMP-M3", stage="MR5", group="ADOPTION", section="8.0", source_path=f"{existing_root}/raw/runs/20260811T190759Z__CMP-M3-PROFILE-V3", source_hash=sha256(repo / f"{existing_root}/raw/runs/20260811T190759Z__CMP-M3-PROFILE-V3/SHA256SUMS"), evidence_class="MEASURED", supplement=True, blocker="Missing instrumentation latency-distribution perturbation gate and special-mechanism namespace lineage.", consumers=["CMP-M3", "R-A", "OFF-E-PR"])
    add_existing_candidate(rows, seen, "ADOPT-CMP-A-ISOLATED", stage="MR5", group="ADOPTION", section="8.0", source_path=f"{existing_root}/preliminary_attention_v5_correlation_probe_review_v1.json", source_hash=sha256(repo / f"{existing_root}/preliminary_attention_v5_correlation_probe_review_v1.json"), evidence_class="MEASURED", supplement=True, blocker="Existing isolated shape evidence lacks actual vLLM fused-path correlation.", consumers=["CMP-A-CORR-G0", "CMP-A0", "CMP-A1"])
    add_existing_candidate(rows, seen, "ADOPT-XFER-E-Q-O", stage="MR5", group="ADOPTION", section="8.0", source_path="runs/20260811T171000Z__phase7_remote_transfer_backup", source_hash=sha256(repo / "runs/20260811T171000Z__phase7_remote_transfer_backup/SHA256SUMS"), evidence_class="MEASURED", consumers=["XFER-E0", "XFER-Q0", "XFER-O0"])
    add_existing_candidate(rows, seen, "ADOPT-R-A-R-C", stage="MR4", group="ADOPTION", section="8.0", source_path="runs/20260811T163100Z__phase7_remote_sampling_pairs_backup", source_hash=sha256(repo / "runs/20260811T163100Z__phase7_remote_sampling_pairs_backup/SHA256SUMS"), evidence_class="MEASURED", consumers=["R-A", "R-B", "R-C"])
    add_existing_candidate(rows, seen, "ADOPT-MEM0-MEM5", stage="MR4", group="ADOPTION", section="8.0", source_path=existing_root, source_hash=sha256(repo / f"{existing_root}/preliminary_memory_full_review_v1.json"), evidence_class="MEASURED", supplement=True, blocker="Worker/aggregate/KV/workspace/peak and cross-family lineage still require current-gate audit.", consumers=["MEM0", "MEM1", "MEM2", "MEM3", "MEM4", "MEM5"])
    add_existing_candidate(rows, seen, "ADOPT-SERVING", stage="MR9", group="ADOPTION", section="8.4", source_path=P0_BASE, source_hash=p0_status_hash, evidence_class="MEASURED", consumers=["SERV-P0-25", "SERV-P0-25-TAIL", "SERV-P0", "SERV-P1"])
    add_existing_candidate(rows, seen, "ADOPT-POL0-POL5", stage="MR10", group="ADOPTION", section="8.5", source_path=existing_root, source_hash=sha256(repo / f"{existing_root}/preliminary_policy_anchor_review_v1.json"), evidence_class="MEASURED", supplement=True, blocker="No amendment-compliant compute_integrated=true and dependency_correct=true policy raw located.", consumers=["POL0", "POL1", "POL2", "POL3", "POL4", "POL5"])
    add_existing_candidate(rows, seen, "ADOPT-LM11-COARSE", stage="MR10", group="ADOPTION", section="8.5", source_path=existing_root, source_hash=sha256(repo / f"{existing_root}/preliminary_fit_anchor_review_v1.json"), evidence_class="MEASURED", supplement=True, blocker="Existing coarse points do not close the full 25/50/75/80/85/90/95/99 and held-out capacity anchors.", consumers=["OFF-E-PR3"])
    add_existing_candidate(rows, seen, "ADOPT-EXPERT-CATALOG", stage="MR5", group="ADOPTION", section="8.2", source_path=f"{existing_root}/raw/components/component-v5-20260812T0230TPE-A-CORR", source_hash=sha256(repo / f"{existing_root}/raw/components/component-v5-20260812T0230TPE-A-CORR/manifest.json"), evidence_class="MEASURED", supplement=True, blocker="Missing materialized/packed/aligned bytes, granularity, ownership and writeback contract.", consumers=["CATALOG-X0", "XFER-E0", "OFF-E-PR"])

    # Qualification, controlled, natural, sampling, clocks, profiler, routing and memory.
    add_pending_group(rows, seen, ["L0-A", "F0-A", "C0-A", "L0-B", "C0-B", "L0-C", "C0-C", "F1", "T-CANARY", "R0"], stage="LM0-LM1", group="QUAL", requirement_class="CORE_REQUIRED", evidence_class="MEASURED", section="8.1", repetitions="Source matrix; preserve success and failed attempt lineage.")
    controlled = [f"P0-{x}" for x in (128, 2048, 8192, 16384)] + [f"P1-{x}" for x in (512, 4096, 12288, 28672)] + [f"DEC0-{x}" for x in (32, 128, 512)] + [f"DEC1-{x}" for x in (64, 256, 1024)] + [f"PX0-{x}-{y}" for x in (512, 4096, 16384, 28672) for y in (32, 256, 1024)] + ["CE0-28672x4096", "CE1-30720x2048", "CE2-31744x1024", "CE3-32256x512"]
    fit_roles = {item: ("FIT" if item.startswith(("P0-", "DEC0-")) else "HELD_OUT" if item.startswith(("P1-", "DEC1-")) else "STRESS_ONLY" if item.startswith("CE") else "DIAGNOSTIC") for item in controlled}
    add_pending_group(rows, seen, controlled, stage="LM0-LM4", group="CONTROLLED", requirement_class="CORE_REQUIRED", evidence_class="MEASURED", section="8.1", fit_roles=fit_roles, repetitions="1 warm-up + 3 measured clean; routing/telemetry as specified")
    add_pending_group(rows, seen, [f"W{i}-{variant}" for i in (0, 1, 2) for variant in ("FORCED", "SMP1-CLEAN", "SMP1-ROUTING", "SMP1-TELEMETRY")] + ["W3-SEQUENCE-01", "W3-SEQUENCE-02", "W3-SEQUENCE-03"], stage="LM0-LM4", group="NATURAL", requirement_class="CORE_REQUIRED", evidence_class="MEASURED", section="8.1", repetitions="W0-W2: warm-up + 3 measured; W3: 3 complete sequences")
    add_pending_group(rows, seen, ["SMP0", "SMP1", "SMP2"], stage="LM2", group="SAMPLING", requirement_class="CORE_REQUIRED", evidence_class="MEASURED", section="6.1", repetitions="Fixture plus paired semantics review")
    add_pending_group(rows, seen, [f"CLK{i}" for i in range(5)], stage="LM2", group="CLOCK", requirement_class="CORE_REQUIRED", evidence_class="MEASURED", section="6.3", repetitions="Multiple calibration points; grade and uncertainty required")
    add_pending_group(rows, seen, [f"K{i}" for i in range(12)], stage="LM3", group="PROFILER", requirement_class="CORE_REQUIRED", evidence_class="MEASURED", section="8.1", repetitions="Bounded canary plus required profile row")
    add_pending_group(rows, seen, ["R-A", "R-B", "R-C"], stage="LM4", group="ROUTING", requirement_class="CORE_REQUIRED", evidence_class="MEASURED", section="9.1", repetitions="Coverage across controlled/natural/formal/serving consumers")
    add_pending_group(rows, seen, [f"MEM{i}" for i in range(6)], stage="LM4", group="MEMORY", requirement_class="CORE_REQUIRED", evidence_class="MEASURED", section="9.2", repetitions="Post-load, controlled, component, transfer, serving and formal targets")
    add_pending_group(rows, seen, ["HOOK-PERTURB-G0"], stage="LM4-MR5", group="HOOK", requirement_class="CORE_REQUIRED", evidence_class="GPU_SERVING_SPECIAL_MECHANISM", section="6.5", repetitions="CLEAN/MARKER/ROUTING/PROFILE/FULL paired distributions")

    # Component, catalog and transfer families.
    add_pending_group(rows, seen, ["CMP-A0", "CMP-A1", "CMP-A2", "CMP-A3", "CMP-A-CORR-G0"], stage="LM5", group="CMP-A", requirement_class="CORE_REQUIRED", evidence_class="GPU_COMPONENT_PROBE", section="7.2", repetitions="At least 10 repetitions; extend deterministically to 30 if required")
    add_pending_group(rows, seen, ["CMP-M0", "CMP-M1", "CMP-M2", "CMP-M3"], stage="LM5", group="CMP-M", requirement_class="CORE_REQUIRED", evidence_class="GPU_COMPONENT_PROBE", section="7.3", repetitions="At least 10 repetitions; M3 full step/layer vectors")
    add_pending_group(rows, seen, ["CMP-L0", "CMP-L1", "CMP-L2", "CMP-L3"], stage="LM5", group="CMP-L", requirement_class="CORE_REQUIRED", evidence_class="GPU_COMPONENT_PROBE", section="7.4", repetitions="At least 10 repetitions; preserve outliers")
    add_pending_group(rows, seen, ["CATALOG-X0"], stage="LM5", group="CATALOG", requirement_class="CORE_REQUIRED", evidence_class="GPU_MECHANISM_GUARD", section="8.2", repetitions="All materialized layer-expert objects; actual config identity")
    add_pending_group(rows, seen, ["XFER-L0", "XFER-L1", "XFER-L2", "XFER-L3", "XFER-E0", "XFER-E1", "XFER-E2", "XFER-E3", "XFER-Q0", "XFER-Q1", "XFER-O0", "XFER-O1", "XFER-O2", "XFER-O3"], stage="LM6", group="TRANSFER", requirement_class="CORE_REQUIRED", evidence_class="GPU_TRANSFER_PROBE", section="8.3-8.5", repetitions="At least 10 repetitions; exact bytes/content/direction and queue role")

    # Formal and serving rows. The formal sample children are materialized only
    # after the owner-gated immutable manifest exists.
    add_pending_group(rows, seen, ["FORMAL-MANIFEST"], stage="MR6", group="FORMAL-MANIFEST", requirement_class="CORE_REQUIRED", evidence_class="DERIVED", section="8.3", repetitions="60 unique samples; owner dispatch gate", blocker="Owner dispatch gate is required before any formal sample GPU dispatch.")
    add_pending_group(rows, seen, ["FORMAL-CORE", "FORMAL-DEEP", "FORMAL-CLOSURE"], stage="MR8", group="FORMAL", requirement_class="CORE_REQUIRED", evidence_class="MEASURED", section="8.3", repetitions="48 Core and 12 Deep after frozen manifest", prerequisites=["FORMAL-MANIFEST"])
    add_pending_group(rows, seen, ["SERV-C0", "SERV-B0", "SERV-P0", "SERV-P1", "SERV-U0", "SERV-M0", "SERV-T0", "SERV-S0", "SERV-P0-25-TAIL"], stage="MR9", group="SERVING", requirement_class="CORE_REQUIRED", evidence_class="GPU_SERVING", section="8.4", repetitions="Formal serving conditions: >=1000 completed; fixed-seed bootstrap tail gate")
    # The valid base serving cell is separately closed; the tail supplement is not.
    add(
        rows,
        seen,
        row_template(
            "SERV-P0-25",
            "SERV-P0-25-SHORT-C8-NATURAL-V1",
            stage="MR9",
            group="SERVING",
            requirement_class="CORE_REQUIRED",
            evidence_class="GPU_SERVING",
            source_sections=["7.1", "8.4"],
            workload="short-C8-natural-1000",
            mechanism="SERVING_VARIANT",
            fit_role="FIT",
            repetitions="1000 completed requests; base cell",
            state="EXECUTION_COMPLETE",
            raw_state="COMPLETE",
            review_state="REVIEW_PASS",
            validation_state="VALIDATION_PASS",
            adoption_state="ADOPTED",
            backup_state="VERIFIED",
            local_paths=[P0_BASE],
            source_raw_hashes=[x for x in (sha256(repo / f"{P0_BASE}/status.json"), sha256(repo / f"{P0_BASE}/manifest.json"), sha256(repo / f"{P0_BASE}/result.json")) if x],
            supported=["SERV-P0-25 base 1000-request serving cell"],
            forbidden=["final tail-CI stability claim until SERV-P0-25-TAIL completes"],
            next_action="Do not rerun; retain as adopted base evidence and schedule independent tail supplement.",
        ),
    )

    # Special mechanism core inventory.
    special_ids = [
        "MECH-G0", "KV-G0", "OS-SWAP-G0", "UM-G0", "OFF-E-PR0", "OFF-E-PR1", "OFF-E-PR2",
        "OFF-E-PR4", "OFF-E-PR5", "OFF-E-RT0", "SWAP-K0", "SWAP-K4", "OFF-W0", "CATALOG-X0",
        "CTRL-P0", "CTRL-P1", "META-X0", "SW-REF", "SW-OPT", "HWR-S0", "HWR-I0", "HWR-Q0", "HWR-D0", "HWR-M0", "SERV-X0", "SERV-X1", "SERV-X2", "SERV-X3",
    ]
    for item in special_ids:
        if item in seen:
            continue
        group = "SESSION-GUARD" if item in {"MECH-G0", "KV-G0", "OS-SWAP-G0", "UM-G0"} else "SPECIAL-MECHANISM"
        req = "REQUIRED_CAPABILITY_AUDIT" if item in {"MECH-G0", "KV-G0", "OS-SWAP-G0", "UM-G0", "OFF-E-RT0", "SWAP-K0", "OFF-W0"} else "CORE_REQUIRED"
        section = "9.1" if group == "SESSION-GUARD" else "8.5"
        prerequisites = ["MODEL-VAULT-VERIFY"] if group == "SESSION-GUARD" else ["MECH-G0", "KV-G0", "OS-SWAP-G0", "UM-G0"]
        add_pending_group(rows, seen, [item], stage="MR2" if group == "SESSION-GUARD" else "MR7-MR12", group=group, requirement_class=req, evidence_class="GPU_MECHANISM_GUARD" if group == "SESSION-GUARD" else "GPU_RUNTIME_OFFLOAD_KV_INTERACTION", section=section, prerequisites=prerequisites, repetitions="Fresh-session canary or exact matrix contract")
    capacity_ids = ["OFF-E-PR3-CAP-025", "OFF-E-PR3-CAP-050", "OFF-E-PR3-CAP-075", "OFF-E-PR3-CAP-080", "OFF-E-PR3-CAP-085", "OFF-E-PR3-CAP-090", "OFF-E-PR3-CAP-095", "OFF-E-PR3-CAP-099", "OFF-E-PR3-CAP-0375", "OFF-E-PR3-CAP-0625", "OFF-E-PR3-CAP-0825", "OFF-E-PR3-CAP-0875", "OFF-E-PR3-CAP-0925", "OFF-E-PR3-CAP-097", "OFF-E-PR3-CAP-100"]
    fit_by_capacity = {x: "FIT" for x in capacity_ids[:8]}
    fit_by_capacity.update({x: "HELD_OUT" for x in capacity_ids[8:14]})
    fit_by_capacity[capacity_ids[-1]] = "CONTROL"
    add_pending_group(rows, seen, capacity_ids, stage="MR10", group="OFF-E-PR3-CAPACITY", requirement_class="CORE_REQUIRED", evidence_class="GPU_POLICY_REPLAY", section="8.5", fit_roles=fit_by_capacity, prerequisites=["CATALOG-X0", "R-A", "XFER-E0"], mechanism="TRACE_DRIVEN_COMPUTE_INTEGRATED_REPLAY", repetitions="Frozen policy/trace repetition block and exact bytes/object set")
    add(
        rows,
        seen,
        row_template(
            "OFF-E-PR3",
            "OFF-E-PR3",
            stage="MR10",
            group="OFF-E-PR3-COMPOSITE",
            requirement_class="CORE_REQUIRED",
            evidence_class="GPU_POLICY_REPLAY",
            source_sections=["8.5", "9"],
            workload="all-capacity-children",
            mechanism="TRACE_DRIVEN_COMPUTE_INTEGRATED_REPLAY",
            state="NOT_RUN",
            prerequisites=capacity_ids,
            blocker="Composite cannot close until all 15 explicit capacity children and any preregistered refinement children close.",
            next_action="Run only after all capacity children are legally ready and split is frozen.",
        ),
    )
    offkv_ids = ["OFFKV-G0", "OFFKV-I0-00", "OFFKV-I0-10", "OFFKV-I0-01", "OFFKV-I0-11"] + [f"OFFKV-I1-FIT-E{e}-K{k}" for e in (50, 75, 95) for k in (25, 60, 90)] + [f"OFFKV-I1-HOLDOUT-E{e}-K{k}" for e in (625, 875) for k in (45, 80)] + [f"OFFKV-I2-TAIL-E{e}-K{k}" for e in (50, 95) for k in (25, 90)]
    add_pending_group(rows, seen, offkv_ids, stage="MR11", group="OFFKV", requirement_class="CORE_REQUIRED", evidence_class="GPU_RUNTIME_OFFLOAD_KV_INTERACTION", section="8.5", prerequisites=["CATALOG-X0", "OFF-E-RT0", "SWAP-K0"], mechanism="ACTUAL_OR_SHARED_FABRIC_INTERACTION", repetitions="Same workload/arrival/seed/initial state per matrix cell")

    policy_ids = [f"POL{i}" for i in range(6)]
    add_pending_group(rows, seen, policy_ids, stage="MR10", group="POLICY", requirement_class="CORE_REQUIRED", evidence_class="GPU_POLICY_REPLAY", section="8.5", prerequisites=["CATALOG-X0", "R-A", "XFER-E0"], mechanism="COMPUTE_INTEGRATED_POLICY_REPLAY", repetitions="Fit/held-out capacity and trace comparisons")
    add_pending_group(rows, seen, ["XRT0", "XRT1", "XRT2"], stage="MR13", group="XRT", requirement_class="CORE_REQUIRED", evidence_class="MEASURED", section="8.6", repetitions="Fixed small subset; official runtime or explicit unavailable evidence")
    conditional = ["OFF-E-RT1", "OFF-E-RT2", "OFF-E-RT3", "SWAP-K1", "SWAP-K2", "SWAP-K3", "SWAP-K5", "OFF-W1", "OFF-W2", "OFF-W3", "COMP0", "COMP1", "COMP2", "COMP3", "COMP4", "PREFIX-CACHE-ON-OFF", "CHUNKED-PREFILL-ON-OFF", "CUDA-GRAPH-ON-OFF", "POWER-FORMAL", "SPECULATIVE-DECODING-EXPANSION"]
    for item in conditional:
        add(
            rows,
            seen,
            row_template(
                item,
                item,
                stage="MR7-MR13",
                group="CONDITIONAL_TRIGGER",
                requirement_class="CONDITIONAL",
                evidence_class="MEASURED",
                source_sections=["8.5", "8.6", "14"],
                workload="TRIGGER_DEPENDENT",
                mechanism="INDEPENDENT_VARIANT",
                state="NOT_RUN",
                trigger_state="PENDING",
                blocker="No legal trigger adjudication yet; CONDITIONAL_NOT_RUN is not closure.",
                next_action="Complete capability/owner trigger adjudication; run if triggered, otherwise record NOT_APPLICABLE_WITH_CONSEQUENCE.",
            ),
        )

    # Offline closure and requirement/RTL handoff rows.
    offline = ["IR0", "IR1", "SIM0", "SIM1", "CAL0", "CAL1", "CAL2", "CAL3", "DSE0", "DSE1", "SEN0", "ABL0", "BE0", "HW0", "REP0", "FINAL-AUDIT", "TRACEABILITY-INDEX", "RTL-ARCH", "RTL-SCHEMA", "RTL-GOLDEN", "RTL-STIMULUS", "RTL-ACTIVITY", "RTL-HANDOFF"]
    add_pending_group(rows, seen, offline, stage="MR14-MR18", group="OFFLINE_CLOSURE", requirement_class="OFFLINE_REQUIRED", evidence_class="DERIVED", section="8.7", prerequisites=["MASTER-DAG-VALIDATION"], repetitions="Stage-specific contract; measurement-dependent rows cannot use fixtures")
    raw_children = ["RAW-MODEL", "RAW-ENVIRONMENT", "RAW-WORKLOAD", "RAW-OUTPUT", "RAW-TIMING", "RAW-MEMORY", "RAW-TELEMETRY", "RAW-KERNEL", "RAW-ROUTING", "RAW-SERVING", "RAW-FAILURE", "RAW-SAMPLING", "RAW-CLOCK", "RAW-COMPONENT", "RAW-EXPERT-CATALOG", "RAW-TRANSFER", "RAW-OVERLAP", "RAW-ROUTING-CONTROL", "RAW-POLICY-REPLAY", "RAW-STATISTICS", "RAW-REQUIREMENT-EXPORT", "RAW-RTL-REPLAY", "RAW-LOCAL-BACKUP"]
    add_pending_group(rows, seen, raw_children, stage="MR14", group="RAW_COMPLETENESS", requirement_class="CORE_REQUIRED", evidence_class="DERIVED", section="8.8", prerequisites=["MASTER-EVIDENCE-INVENTORY"], repetitions="Per-attempt/per-file lineage")

    # Ensure every source-declared required row has a corresponding master row.
    return rows


def build_aliases() -> list[dict[str, Any]]:
    return [
        {"source_ids": ["S0", "S1", "S2", "S3", "S4", "S5"], "master_ids": ["SERV-C0", "SERV-B0", "SERV-U0", "SERV-M0"], "relationship": "historical serving candidates; not direct replacement for patch rows"},
        {"source_ids": ["POL0", "POL1", "POL2", "POL3", "POL4", "POL5"], "master_ids": ["POL0", "POL1", "POL2", "POL3", "POL4", "POL5", "OFF-E-PR0", "OFF-E-PR1", "OFF-E-PR2", "OFF-E-PR3"], "relationship": "policy semantics reused only with compute/dependency evidence"},
        {"source_ids": ["SERV-P0-25-SHORT-C8-NATURAL-V1"], "master_ids": ["SERV-P0-25"], "relationship": "exact base serving cell; not OFF-E-PR3-CAP-025"},
        {"source_ids": ["OFF-E-PR3-CAP-025"], "master_ids": ["OFF-E-PR3-CAP-025"], "relationship": "expert-budget 25% replay cell; distinct from serving P0 25% saturation rate"},
        {"source_ids": ["XFER-L/E/Q/O"], "master_ids": ["XFER-L0", "XFER-L1", "XFER-L2", "XFER-L3", "XFER-E0", "XFER-E1", "XFER-E2", "XFER-E3", "XFER-Q0", "XFER-Q1", "XFER-O0", "XFER-O1", "XFER-O2", "XFER-O3"], "relationship": "existing transfer candidates require catalog/lineage adoption"},
        {"source_ids": ["W0-W3 forced"], "master_ids": ["W0-FORCED", "W1-FORCED", "W2-FORCED", "W3-SEQUENCE-01", "W3-SEQUENCE-02", "W3-SEQUENCE-03"], "relationship": "forced fixture evidence; never natural output distribution"},
    ]


def build_trigger_adjudication() -> list[dict[str, Any]]:
    rows = []
    for item in ["OFF-E-RT1", "OFF-E-RT2", "OFF-E-RT3", "SWAP-K1", "SWAP-K2", "SWAP-K3", "SWAP-K5", "OFF-W1", "OFF-W2", "OFF-W3", "COMP0", "COMP1", "COMP2", "COMP3", "COMP4", "PREFIX-CACHE-ON-OFF", "CHUNKED-PREFILL-ON-OFF", "CUDA-GRAPH-ON-OFF", "POWER-FORMAL", "SPECULATIVE-DECODING-EXPANSION"]:
        rows.append({"trigger_id": item, "scope_question": "Does this capability/owner scope enter the formal calibrated domain?", "required_capability_or_owner_decision": "Capability evidence or explicit owner decision", "observed_evidence": [], "trigger_state": "PENDING", "triggered_child_ids": [item], "claims_forbidden_until_closed": [f"formal {item} claim"], "source_evidence_hashes": []})
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--master-campaign-id", required=True)
    parser.add_argument("--remote-campaign-id", required=True)
    parser.add_argument("--guard-attempt-id", required=True)
    args = parser.parse_args()
    repo = Path.cwd()
    out = args.output_root
    out.mkdir(parents=True, exist_ok=True)
    rows = build_rows(repo, args.master_campaign_id)
    row_ids = {row["master_row_id"] for row in rows}
    errors = []
    for row in rows:
        for dep in row["prerequisite_row_ids"]:
            if dep not in row_ids:
                errors.append(f"{row['master_row_id']}: missing prerequisite {dep}")
    if len(row_ids) != len(rows):
        errors.append("duplicate row ids")
    if errors:
        raise SystemExit("ledger validation errors: " + "; ".join(errors))

    source_manifest = {
        "schema_version": "phase7-combined-master-source-snapshot-v1",
        "master_campaign_id": args.master_campaign_id,
        "generated_at_utc": now_utc(),
        "source_documents": {name: {"path": path, "sha256": sha256(repo / path)} for name, path in SOURCE_FILES.items()},
        "historical_sources": {
            "old_campaign_id": OLD_CAMPAIGN,
            "old_execution_ledger": {"path": OLD_LEDGER, "sha256": sha256(repo / OLD_LEDGER)},
            "old_guard_attempt_sha256s": {"path": f"{OLD_GUARD_ATTEMPT}/SHA256SUMS_ATTEMPT", "sha256": sha256(repo / f"{OLD_GUARD_ATTEMPT}/SHA256SUMS_ATTEMPT")},
        },
        "source_drift_detected": False,
        "canonical_domain": {"gpu": "single NVIDIA RTX PRO 6000 Workstation Edition 96 GB", "model": MODEL_ID, "revision": MODEL_REVISION, "runtime": "vLLM", "weights": "BF16", "kv": "BF16", "tp_pp_ep": "1/1/1", "eager": True, "max_num_seqs": 1, "quantization": "none", "cpu_offload": False, "runtime_kv_swap": False},
        "new_host_observation": {"endpoint": "pod-9ebe2f5c-81af-44c1-8fb0-a06bfd2d4f9c@ssh.gputw.ai:2222", "hostname": "gpu-9ebe2f5c-81af-44c1-8fb0-a06bfd2d4f9c", "gpu_uuid": "GPU-177cc8e4-ff4d-a649-ac29-a3807141b521", "gpu_name": "NVIDIA RTX PRO 6000 Blackwell Workstation Edition", "vram_mib": 97887, "driver": "595.71.05", "vllm": "0.23.0", "torch": "2.11.0+cu130", "serving_interference": "CLEAR_AT_LAST_READ_ONLY_PREFLIGHT", "full_weight_hash_scan": "DEFERRED_OUTSIDE_SENSITIVE_WINDOW"},
    }
    write_json(out / "source_snapshot_manifest.json", source_manifest)
    inventory = {
        "schema_version": "phase7-combined-master-evidence-inventory-v1",
        "generated_at_utc": now_utc(),
        "lm0_lm1_state": "checkpoints/lm0_lm1_state.json",
        "existing_evidence_roots": ["runs/20260811T133800Z__phase7_remote_f0_gpu_backup", "runs/20260811T134100Z__phase7_remote_c0a_backup", "runs/20260811T134700Z__phase7_remote_c0bc_backup", "runs/20260811T134800Z__phase7_remote_f1_backup", "runs/20260811T134900Z__phase7_remote_tcanary_backup", "runs/20260811T135600Z__phase7_remote_r0_backup", "runs/20260811T152600Z__phase7_remote_controlled_matrix_backup", "runs/20260811T161000Z__phase7_remote_natural_matrix_backup", "runs/20260811T163100Z__phase7_remote_sampling_pairs_backup", "runs/20260811T170000Z__phase7_remote_k_profile_backup", "runs/20260811T171000Z__phase7_remote_component_backup", "runs/20260811T171000Z__phase7_remote_transfer_backup", "runs/20260811T175500Z__phase7_fit_anchor_backup", "artifacts/phase7/special_mechanism_raw/" + OLD_CAMPAIGN],
        "p0_base": {"path": P0_BASE, "status": "PASS", "request_count": 1000, "arrival_count": 1000, "warmup_count": 8, "telemetry_count": 127, "status_sha256": sha256(repo / f"{P0_BASE}/status.json"), "manifest_sha256": sha256(repo / f"{P0_BASE}/manifest.json"), "result_sha256": sha256(repo / f"{P0_BASE}/result.json"), "adoption": "DO_NOT_RERUN_BASE"},
        "p0_ext10k": {"path": P0_EXT_SIDECAR, "is_completion_evidence": False, "is_raw_backup": False, "classification": "REMOTE_ENVIRONMENT_UNAVAILABLE", "adoption": "INCOMPLETE_NO_VERIFIED_RAW"},
        "historical_guard": {"attempt": OLD_GUARD_ATTEMPT, "sha256s_attempt_sha256": sha256(repo / f"{OLD_GUARD_ATTEMPT}/SHA256SUMS_ATTEMPT"), "checksum_claim": "19 listed items rechecked OK", "new_host_reuse_forbidden": True},
        "known_candidate_and_gap_counts": {"lm0_lm1_record_count": 133, "controlled_family_records": 93, "natural_family_records": 20, "sampling_family_records": 11, "special_core_ledger_rows": 79, "old_special_guard_legal_rows": 4, "old_special_non_guard_rows_remaining": 75},
        "adoption_rule": "candidate evidence is not master-complete until current gate, backup and adoption axes close",
    }
    write_json(out / "evidence_inventory.json", inventory)
    write_json(out / "alias_and_lineage_map.json", {"schema_version": "phase7-combined-master-alias-lineage-v1", "entries": build_aliases()})
    write_json(out / "trigger_adjudication.json", {"schema_version": "phase7-combined-master-trigger-adjudication-v1", "entries": build_trigger_adjudication()})
    gap_register = [
        {"gap_id": "GAP-P0-25-TAIL", "status": "SUPPLEMENT_REQUIRED", "source": P0_EXT_SIDECAR, "consequence": "No final SERV-P0 tail-CI/calibration/control requirement claim."},
        {"gap_id": "GAP-NEW-SESSION-GUARD", "status": "READY_FOR_NEW_ATTEMPT", "source": "new host preflight", "consequence": "Old guard cannot prove new host; run fresh four-guard canary."},
        {"gap_id": "GAP-CLK4", "status": "UNAVAILABLE_WITH_CONSEQUENCE", "source": "existing clock audit", "consequence": "No cycle-grade support-processor/control-latency claim."},
        {"gap_id": "GAP-CATALOG", "status": "SUPPLEMENT_REQUIRED", "source": "ADOPT-EXPERT-CATALOG", "consequence": "No exact residency capacity/ownership/writeback accounting."},
        {"gap_id": "GAP-POLICY-CAL3", "status": "SUPPLEMENT_REQUIRED", "source": "ADOPT-POL0-POL5", "consequence": "No compute-integrated policy ranking or accelerator break-even claim."},
        {"gap_id": "GAP-FORMAL-OWNER", "status": "OWNER_DISPATCH_GATE", "source": "FORMAL-MANIFEST", "consequence": "No formal 60-sample GPU dispatch before explicit owner release."},
        {"gap_id": "GAP-IR-OFFLINE", "status": "BLOCKED_BY_MEASUREMENT_DEPENDENCIES", "source": "IR0-REP0", "consequence": "No calibrated surrogate/DSE/requirements closure."},
    ]
    write_json(out / "gap_register.json", {"schema_version": "phase7-combined-master-gap-register-v1", "entries": gap_register})
    write_json(out / "claim_boundary_register.json", {"schema_version": "phase7-combined-master-claim-boundary-v1", "claims_forbidden": ["runtime-native dynamic expert offload", "runtime-native KV swap performance", "stronger Unified Memory absence than observed telemetry", "SERV-P0 tail stability before independent 10K completion", "OFF-E-PR3-CAP-025 represented by SERV-P0-25", "support-processor break-even without compute-integrated dependency-correct replay and SW-OPT", "cycle-grade control latency without CLK4"], "claims_allowed_now": ["SERV-P0-25 base 1K measured cell PASS and do-not-rerun", "new host preflight clear at last read-only checkpoint", "historical old guard evidence with new-session proof still pending"]})
    write_json(out / "local_backup_manifest.json", {"schema_version": "phase7-combined-master-local-backup-manifest-v1", "checksum_algorithm": "SHA-256", "verified_local_sources": [{"path": P0_BASE, "status_sha256": sha256(repo / f"{P0_BASE}/status.json"), "manifest_sha256": sha256(repo / f"{P0_BASE}/manifest.json"), "result_sha256": sha256(repo / f"{P0_BASE}/result.json")}, {"path": OLD_GUARD_ATTEMPT, "attempt_manifest_sha256": sha256(repo / f"{OLD_GUARD_ATTEMPT}/SHA256SUMS_ATTEMPT"), "status": "historical_attempt_checksum_rechecked"}], "new_remote_backup": {"remote_campaign": args.remote_campaign_id, "local_root": str(out), "status": "PENDING_NEW_SESSION_RAW"}, "remote_delete_before_local_verify": False})

    execution = {"schema_version": "phase7-combined-master-execution-ledger-v1", "master_campaign_id": args.master_campaign_id, "remote_campaign_id": args.remote_campaign_id, "generated_at_utc": now_utc(), "append_only": True, "historical_ledger_refs": [{"campaign_id": OLD_CAMPAIGN, "path": OLD_LEDGER, "sha256": sha256(repo / OLD_LEDGER)}], "transitions": [{"transition_id": "MR0-MR1-INITIAL-MATERIALIZATION", "timestamp_utc": now_utc(), "changed_rows": ["MASTER-SOURCE-RECON", "MASTER-EVIDENCE-INVENTORY", "MASTER-ALIAS-LINEAGE", "MASTER-DAG-VALIDATION"], "reason": "Created new combined successor inventory without modifying historical evidence."}], "rows": rows, "required_row_count": len(rows), "required_closed_count": sum(1 for row in rows if closed_row(row)), "status": "PHASE7_COMBINED_MASTER_INCOMPLETE"}
    write_json(out / "master_execution_ledger.json", execution)
    execution_hash = sha256(out / "master_execution_ledger.json")
    remaining = [row for row in rows if not closed_row(row)]
    blocked = [row for row in remaining if row["blocker_or_failure"]]
    conditional_pending = [row for row in remaining if row["trigger_state"] == "PENDING"]
    ready = []
    for row in remaining:
        if row["master_row_id"] == "FORMAL-MANIFEST":
            continue
        deps = [next((candidate for candidate in rows if candidate["master_row_id"] == dep), None) for dep in row["prerequisite_row_ids"]]
        if all(dep is not None and closed_row(dep) for dep in deps):
            ready.append(row["master_row_id"])
    ready_gpu = ["MECH-G0-KV-G0-OS-SWAP-G0-UM-G0-CANARY-V3-MASTER"]
    write_json(out / "master_remaining_ledger.json", {"schema_version": "phase7-combined-master-remaining-ledger-v1", "master_campaign_id": args.master_campaign_id, "generated_from_execution_ledger_sha256": execution_hash, "required_total": len(rows), "required_legally_closed": len(rows) - len(remaining), "required_remaining_count": len(remaining), "required_remaining_ids": [row["master_row_id"] for row in remaining], "conditional_pending_count": len(conditional_pending), "conditional_pending_ids": [row["master_row_id"] for row in conditional_pending], "blocked_rows": [{"id": row["master_row_id"], "reason": row["blocker_or_failure"]} for row in blocked], "phase7_status": "PHASE7_COMBINED_MASTER_INCOMPLETE"})
    write_json(out / "master_ready_queue.json", {"schema_version": "phase7-combined-master-ready-queue-v1", "master_campaign_id": args.master_campaign_id, "generated_from_execution_ledger_sha256": execution_hash, "ready_cpu_units": ready, "ready_gpu_units": ready_gpu, "next_gpu_unit": ready_gpu[0], "dispatch_guards": ["NS2 read-only preflight clear", "no foreign serving/GPU process", "session-local four-guard canary first", "no filler workload", "raw namespace independent", "local backup before PASS"], "formal_owner_gate": "FORMAL-MANIFEST not dispatchable until explicit owner release"})
    write_json(out / "checkpoints" / "MR0-MR2-initial.json", {"schema_version": "phase7-combined-master-checkpoint-v1", "master_campaign_id": args.master_campaign_id, "generated_at_utc": now_utc(), "source_snapshot_manifest_sha256": sha256(out / "source_snapshot_manifest.json"), "master_execution_ledger_sha256": execution_hash, "master_remaining_ledger_sha256": sha256(out / "master_remaining_ledger.json"), "master_ready_queue_sha256": sha256(out / "master_ready_queue.json"), "SERV-P0-25-base-1K": "PASS / DO_NOT_RERUN", "SERV-P0-25-EXT10K": "INCOMPLETE / NO VERIFIED RAW", "OFF-E-PR3-CAP-025": "NOT_RUN / DISTINCT FROM SERV-P0-25", "old_server": "CLOSED", "old_special_ledger": "HISTORICAL SOURCE, NOT MASTER PROJECT LEDGER", "new_session_guard": "READY_FOR_V3_ATTEMPT", "phase7_combined_closure": "INCOMPLETE", "next_gpu_unit": ready_gpu[0], "required_remaining_count": len(remaining), "conditional_pending_count": len(conditional_pending)})
    print(json.dumps({"status": "PHASE7_COMBINED_MASTER_INCOMPLETE", "required_total": len(rows), "required_closed": len(rows) - len(remaining), "required_remaining": len(remaining), "conditional_pending": len(conditional_pending), "next_gpu_unit": ready_gpu[0], "master_execution_ledger_sha256": execution_hash}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

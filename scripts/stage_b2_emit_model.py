#!/usr/bin/env python3
"""Stage B2 deliverable emitter: dump the candidate-processor model + validation run.

Pure CPU. Produces a runs/<run_id>/ with:
  * manifest.json / metrics.json / environment/tool_versions.json
  * artifacts/resource_sweep.json      -- the nine swept parameters + a sample point
  * artifacts/abi_registry.json        -- registered vs reserved backends (anti-forgery)
  * artifacts/reference_mock_paths.json-- the five ABI paths, exercised
  * artifacts/attachment_points.json   -- A1..A6 (three things each; A2/A6 unmeasured)
  * artifacts/fidelity_audit.json      -- proof nothing is MEASURED_SURROGATE

Makes NO benefit/break-even claim (that is C1). A2/A6 carry no performance conclusion.
"""
from __future__ import annotations

import json
import platform
import subprocess
import sys
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import yaml  # noqa: E402

from accelerator.abi import BackendNotRegistered, Backpressure, Transaction  # noqa: E402
from accelerator.attachment_points import default_attachment_points, unmeasured_points  # noqa: E402
from accelerator.backends import default_registry  # noqa: E402
from accelerator.backends.reserved import RESERVED  # noqa: E402
from accelerator.fidelity import Fidelity, require_accelerator_fidelity  # noqa: E402
from accelerator.resource_model import PARAM_NAMES, ResourceModel, ResourceSweep  # noqa: E402

CONFIG = ROOT / "configs/accelerator/resource_model_default.yaml"


def _sample_model() -> ResourceModel:
    return ResourceModel(
        pipeline_latency_cycles=2,
        issue_width=2,
        local_sram_capacity_bytes=2_097_152,
        memory_bandwidth_bytes_per_s=28_298_591_668,
        queue_depth=4,
        operations_per_cycle=4,
        clock_frequency_hz=Fraction(500_000_000, 1),
        area_proxy_um2=4902.0,
        power_proxy_mw=50.0,
    )


def resource_sweep_report() -> dict:
    config = yaml.safe_load(CONFIG.read_text())
    sweep = ResourceSweep.from_config(config)
    return {
        "param_names": list(PARAM_NAMES),
        "num_params": len(PARAM_NAMES),
        "axis_sizes": {name: len(sweep.axes[name]) for name in PARAM_NAMES},
        "product_size": sweep.size(),
        "max_points": sweep.max_points,
        "all_scannable": all(len(sweep.axes[name]) >= 1 for name in PARAM_NAMES),
        "sample_point": _sample_model().to_dict(),
        "config_path": str(CONFIG.relative_to(ROOT)),
    }


def abi_registry_report() -> dict:
    reg = default_registry()
    refused = {}
    for name in list(RESERVED) + ["gpu-cycle-missing"]:
        try:
            reg.create(name, _sample_model(), Fidelity.ANALYTICAL)
            refused[name] = "ERROR: was NOT refused"
        except BackendNotRegistered as exc:
            refused[name] = f"refused: {str(exc)[:80]}"
    return {
        "verbs": ["reset", "can_accept", "submit", "advance", "poll_completions", "snapshot_counters"],
        "registered": reg.registered_names(),
        "reserved_unregistered": reg.reserved_names(),
        "unregistered_dispatch_refused": refused,
        "all_reserved_refused": all("refused:" in v for v in refused.values()),
    }


def reference_mock_paths_report() -> dict:
    reg = default_registry()
    out: dict = {}

    # 1. transaction adapter
    m = reg.create("REFERENCE_MOCK", _sample_model(), Fidelity.ANALYTICAL)
    m.submit(Transaction(1, "A1", work_bytes=0, op_count=8))
    out["transaction_adapter"] = {"accepted": m.snapshot_counters().accepted == 1}

    # 2. clock stepping
    m2 = reg.create("REFERENCE_MOCK", _sample_model(), Fidelity.ANALYTICAL)
    m2.advance(5)
    out["clock_stepping"] = {
        "cycles_advanced": m2.snapshot_counters().cycles_advanced,
        "time_fs": m2.snapshot_counters().time_fs,
        "cycle_period_fs": m2.resources.cycle_period_fs,
        "ok": m2.snapshot_counters().time_fs == 5 * m2.resources.cycle_period_fs,
    }

    # 3. backpressure
    m3 = reg.create(
        "REFERENCE_MOCK",
        ResourceModel(
            pipeline_latency_cycles=10, issue_width=1, local_sram_capacity_bytes=65536,
            memory_bandwidth_bytes_per_s=28_298_591_668, queue_depth=2, operations_per_cycle=4,
            clock_frequency_hz=Fraction(500_000_000, 1), area_proxy_um2=4902.0, power_proxy_mw=50.0,
        ),
        Fidelity.ANALYTICAL,
    )
    for i in range(2):
        m3.submit(Transaction(i, "A3", 0, 1))
    bp_raised = False
    try:
        m3.submit(Transaction(99, "A3", 0, 1))
    except Backpressure:
        bp_raised = True
    out["backpressure"] = {
        "can_accept_when_full": m3.can_accept(Transaction(99, "A3", 0, 1)),
        "submit_raised": bp_raised,
        "backpressure_events": m3.snapshot_counters().backpressure_events,
        "ok": (not m3.can_accept(Transaction(99, "A3", 0, 1))) and bp_raised,
    }

    # 4. completion
    m4 = reg.create(
        "REFERENCE_MOCK",
        ResourceModel(
            pipeline_latency_cycles=3, issue_width=2, local_sram_capacity_bytes=65536,
            memory_bandwidth_bytes_per_s=28_298_591_668, queue_depth=8, operations_per_cycle=100,
            clock_frequency_hz=Fraction(500_000_000, 1), area_proxy_um2=4902.0, power_proxy_mw=50.0,
        ),
        Fidelity.ANALYTICAL,
    )
    for i in range(2):
        m4.submit(Transaction(i, "A1", 0, 1))
    m4.advance(1)
    mid = m4.poll_completions()
    m4.advance(3)
    comps = m4.poll_completions()
    out["completion"] = {
        "empty_before_latency": mid == [],
        "completed_ids": [c.txn_id for c in comps],
        "ok": mid == [] and [c.txn_id for c in comps] == [0, 1],
    }

    # 5. counter
    m5 = reg.create("REFERENCE_MOCK", _sample_model(), Fidelity.ANALYTICAL)
    for i in range(3):
        m5.submit(Transaction(i, "A1", 0, 1))
    m5.advance(20)
    snap = m5.snapshot_counters()
    out["counter"] = {
        "submitted": snap.submitted, "accepted": snap.accepted, "completed": snap.completed,
        "ok": snap.submitted == 3 and snap.completed == 3,
    }

    out["all_five_paths_ok"] = all(v["ok"] for k, v in out.items() if isinstance(v, dict) and "ok" in v) and out["transaction_adapter"]["accepted"]
    return out


def attachment_points_report() -> dict:
    pts = default_attachment_points()
    return {
        "points": {pid: p.to_dict() for pid, p in pts.items()},
        "unmeasured": unmeasured_points(pts),
        "primary_defined_A1_A4": all(
            pts[pid].work_unit.baseline_cost_model
            and pts[pid].accelerator_cost.expression
            and pts[pid].transfer_cost.expression
            for pid in ("A1", "A2", "A3", "A4")
        ),
    }


def fidelity_audit_report() -> dict:
    pts = default_attachment_points()
    labels = set()
    offenders = []
    for pid, p in pts.items():
        for cm in (p.work_unit.provenance, p.accelerator_cost.provenance, p.transfer_cost.provenance):
            require_accelerator_fidelity(cm.fidelity)  # raises if forbidden
            labels.add(cm.fidelity.value)
            if cm.fidelity.value == "MEASURED_SURROGATE":
                offenders.append(pid)
    return {
        "labels_used": sorted(labels),
        "measured_surrogate_count": len(offenders),
        "offenders": offenders,
        "no_measured_surrogate": len(offenders) == 0,
    }


def main() -> int:
    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}__stage_b2_accelerator_model"
    run_dir = ROOT / "runs" / run_id
    (run_dir / "artifacts").mkdir(parents=True, exist_ok=True)
    (run_dir / "environment").mkdir(parents=True, exist_ok=True)

    reports = {
        "resource_sweep": resource_sweep_report(),
        "abi_registry": abi_registry_report(),
        "reference_mock_paths": reference_mock_paths_report(),
        "attachment_points": attachment_points_report(),
        "fidelity_audit": fidelity_audit_report(),
    }
    for name, data in reports.items():
        (run_dir / "artifacts" / f"{name}.json").write_text(
            json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    try:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip()
    except Exception:
        commit = "unknown"

    manifest = {
        "stage": "B2",
        "run_id": run_id,
        "experiment_id": "stage_b2_accelerator_model",
        "classification": "parametric candidate-processor model + six-verb ABI + attachment points A1-A6 (CPU-only)",
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "command": ["python", "scripts/stage_b2_emit_model.py"],
        "git": {"code_commit": commit},
        "fidelity_rule": "all accelerator/ components ANALYTICAL or PROJECTED; never MEASURED_SURROGATE",
        "claim_boundary": "no accelerator benefit/break-even claim (C1); A2/A6 carry no performance conclusion (no measurement)",
        "summary": {
            "resource_params": reports["resource_sweep"]["num_params"],
            "registered_backends": reports["abi_registry"]["registered"],
            "all_reserved_refused": reports["abi_registry"]["all_reserved_refused"],
            "reference_mock_all_five_paths_ok": reports["reference_mock_paths"]["all_five_paths_ok"],
            "unmeasured_points": reports["attachment_points"]["unmeasured"],
            "no_measured_surrogate": reports["fidelity_audit"]["no_measured_surrogate"],
        },
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    metrics = {
        "resource_sweep_product_size": reports["resource_sweep"]["product_size"],
        "reference_mock_all_five_paths_ok": reports["reference_mock_paths"]["all_five_paths_ok"],
        "all_reserved_refused": reports["abi_registry"]["all_reserved_refused"],
        "primary_A1_A4_defined": reports["attachment_points"]["primary_defined_A1_A4"],
        "unmeasured_points": reports["attachment_points"]["unmeasured"],
        "no_measured_surrogate": reports["fidelity_audit"]["no_measured_surrogate"],
    }
    (run_dir / "metrics.json").write_text(json.dumps(metrics, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    tool_versions = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "pyyaml": yaml.__version__,
    }
    (run_dir / "environment" / "tool_versions.json").write_text(
        json.dumps(tool_versions, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    print(f"wrote {run_dir.relative_to(ROOT)}")
    print(json.dumps(metrics, indent=2, sort_keys=True))
    ok = (
        metrics["reference_mock_all_five_paths_ok"]
        and metrics["all_reserved_refused"]
        and metrics["primary_A1_A4_defined"]
        and metrics["no_measured_surrogate"]
        and metrics["unmeasured_points"] == ["A2", "A6"]
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

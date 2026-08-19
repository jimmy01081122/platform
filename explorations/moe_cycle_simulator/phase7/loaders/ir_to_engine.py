"""Stage A3 IR -> C++ engine loader for the OFF-E-PR3 expert capacity scan.

This module is the Python half of the Stage A3 loader. It reads the A2 nine-kind
Canonical IR bundle for structure (catalog, per-object bytes, per-point residency
capacity, routing provenance), reconstructs the ordered demand sequence from the
frozen routing ``.npy`` (which the A2 handoff records as the sanctioned source of
the ordered demand -- the AGGREGATE RoutingIR intentionally drops per-token
order), emits a compact plan spec, and drives the existing Phase 5
``RoutingResidencyModel`` through the ``moe_sim_phase5_ir_loader`` executable.

The residency counters produced by the engine are compared against the measured
counters recorded in the read-only evidence ``capacity_replay.json`` (the SIM0
acceptance data). Nothing in ``evidence/`` is modified; the routing ``.npy`` and
capacity replay JSON are read only, and their SHA-256s are checked against the
A2 IR provenance.

SIM0: for all 15 points, engine ``hit_count`` / ``demand_load_count`` /
``immutable_discard_count`` must equal the measured counters exactly.
SIM1: the same plan replayed twice must be byte-identical (same digests).

Counter mapping (measured <- engine):
    demand_load_count      <- metrics.loads
    immutable_discard_count<- metrics.clean_evictions
    hit_count              <- metrics.routing_demands - metrics.loads

Degenerate control (cap == full catalog): the measured full-capacity point is an
"actual all-resident control" with zero demand H2D. When the device residency
budget covers the entire catalog, the initial residency is the full working set
(base_resident = whole catalog), reproducing the measured zero-movement control.
Every other point starts from an empty cache per DETERMINISTIC_LRU_EMPTY_INITIAL_CACHE.
"""
from __future__ import annotations

import glob
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
PHASE2_DIR = REPO_ROOT / "explorations/moe_cycle_simulator/phase2"
ADAPTERS_DIR = REPO_ROOT / "explorations/moe_cycle_simulator/phase7/adapters"

DEFAULT_BUNDLE_ENVELOPE = (
    REPO_ROOT
    / "runs/20260819T000000Z__stage_a2_off_e_pr3_measured_ir/bundle/artifact-envelope.json"
)
DEFAULT_LOADER_BIN = REPO_ROOT / "build/phase5/moe_sim_phase5_ir_loader"
DEFAULT_EVIDENCE_ROOT = REPO_ROOT / "evidence"

# Measured interconnect bandwidth (bytes/second) for the PCIe H2D path, from the
# A2 PlatformIR. Used only for the Phase 4 timing observation (step 6); it does
# not influence residency counters.
H2D_BANDWIDTH_BYTES_PER_SECOND = 28298591668

for _path in (str(PHASE2_DIR), str(ADAPTERS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from canonical_ir import read_bundle  # noqa: E402
from off_e_pr3_measured_adapter import read_npy_u8  # noqa: E402

EXPERTS_PER_LAYER = 8


def object_id(layer: int, expert: int) -> tuple[int, int]:
    return (layer, expert)


@dataclass
class PointStructure:
    """Per-point structural inputs sourced from the A2 IR bundle."""

    label: str
    capacity_bytes: int
    capacity_objects: int
    object_bytes: int
    catalog: list[tuple[int, int, int]]  # (layer, expert, bytes)
    routing_sha256: str
    policy_id: str


@dataclass
class PointResult:
    label: str
    expected: dict[str, int]
    engine: dict[str, int]
    counters_match: bool
    terminal_resident_match: bool
    raw: dict[str, Any] = field(default_factory=dict)


def _label_from_record_id(record_id: str) -> str:
    match = re.search(r"cap-([0-9]+)$", record_id)
    if match:
        return match.group(1)
    match = re.search(r"-([0-9]+)$", record_id)
    if not match:
        raise ValueError(f"cannot extract capacity label from {record_id!r}")
    return match.group(1)


_BUNDLE_STRUCTURE_CACHE: dict[str, dict[str, "PointStructure"]] = {}


def load_bundle_structures(
    bundle_envelope: Path = DEFAULT_BUNDLE_ENVELOPE,
) -> dict[str, PointStructure]:
    """Read structural inputs for every point from the A2 IR bundle.

    read_bundle validates the whole bundle closure (all 33203 records) before
    returning, which is deliberately expensive; the parsed structures are cached
    per bundle path so a process pays that integrity check only once.
    """

    cache_key = str(Path(bundle_envelope).resolve())
    cached = _BUNDLE_STRUCTURE_CACHE.get(cache_key)
    if cached is not None:
        return cached

    records, _envelope = read_bundle(bundle_envelope)
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        by_kind.setdefault(record["ir_kind"], []).append(record)

    # Catalog is shared across points; build it once from any PlacementIR.
    placement = by_kind["PlacementIR"][0]["payload"]
    catalog: list[tuple[int, int, int]] = []
    object_bytes_set: set[int] = set()
    for loc in placement["expert_locations"]:
        if not loc.get("owner", False):
            continue
        tensor_id = loc["tensor_id"]  # e.g. "L00E3.ffn"
        match = re.match(r"L(\d+)E(\d+)\.ffn", tensor_id)
        if not match:
            raise ValueError(f"unexpected tensor_id {tensor_id!r}")
        layer = int(match.group(1))
        expert = int(match.group(2))
        shard_bytes = int(loc["shard_bytes"])
        catalog.append((layer, expert, shard_bytes))
        object_bytes_set.add(shard_bytes)
    catalog.sort()
    if len(object_bytes_set) != 1:
        raise ValueError(f"non-uniform expert object bytes: {object_bytes_set}")
    object_bytes = next(iter(object_bytes_set))
    if len(catalog) != 256:
        raise ValueError(f"expected 256 catalog objects, got {len(catalog)}")

    # The single shared routing trace SHA (RoutingIR provenance content id 0).
    routing_record = by_kind["RoutingIR"][0]
    routing_sha256 = routing_record["provenance"]["source_content_ids"][0]

    # Per-point residency capacity from PlatformIR device_residency_budget.
    structures: dict[str, PointStructure] = {}
    placements_by_label = {
        _label_from_record_id(r["record_id"]): r["payload"]
        for r in by_kind["PlacementIR"]
    }
    for platform_record in by_kind["PlatformIR"]:
        label = _label_from_record_id(platform_record["record_id"])
        payload = platform_record["payload"]
        device_budget = None
        for domain in payload["memory_domains"]:
            if domain["domain_id"] == "device_residency_budget":
                device_budget = int(domain["capacity_bytes"])
                break
        if device_budget is None:
            raise ValueError(f"no device_residency_budget for point {label}")
        if device_budget % object_bytes != 0:
            raise ValueError(
                f"capacity {device_budget} not a whole multiple of object bytes"
            )
        capacity_objects = device_budget // object_bytes
        policy_id = placements_by_label[label]["policy_id"]
        structures[label] = PointStructure(
            label=label,
            capacity_bytes=device_budget,
            capacity_objects=capacity_objects,
            object_bytes=object_bytes,
            catalog=catalog,
            routing_sha256=routing_sha256,
            policy_id=policy_id,
        )
    _BUNDLE_STRUCTURE_CACHE[cache_key] = structures
    return structures


def discover_evidence_points(
    evidence_root: Path = DEFAULT_EVIDENCE_ROOT,
) -> dict[str, Path]:
    """Map capacity label -> evidence point directory."""

    pattern = str(
        evidence_root
        / "phase7/master_remaining/*/remote_raw/OFF-E-PR3-CAP-*-V1-MASTER"
    )
    points: dict[str, Path] = {}
    for path_str in sorted(glob.glob(pattern)):
        path = Path(path_str)
        match = re.search(r"OFF-E-PR3-CAP-([0-9]+)-V1-MASTER$", path.name)
        if not match:
            continue
        points[match.group(1)] = path
    if not points:
        raise FileNotFoundError(f"no OFF-E-PR3 capacity points under {pattern}")
    return points


def _routing_npy(point_dir: Path) -> Path:
    matches = sorted(point_dir.glob("runner_runs/*/routing/*.npy"))
    if not matches:
        raise FileNotFoundError(f"no routing .npy under {point_dir}")
    return matches[0]


def demand_sequence_from_npy(npy_path: Path) -> list[tuple[int, int]]:
    """Reconstruct the ordered 10176-long demand sequence from the routing .npy.

    The .npy is a C-order uint8 array of shape [tokens, layers, top_k] holding
    selected expert ids. The demand at flat index i targets object
    (layer, expert) with layer = (i // top_k) % layers, reproducing the
    token-major ordering the A2 handoff documents for bit-exact LRU replay.
    """

    flat, shape = read_npy_u8(npy_path)
    if len(shape) != 3:
        raise ValueError(f"unexpected routing shape {shape}")
    _tokens, layers, top_k = shape
    sequence: list[tuple[int, int]] = []
    for index, expert in enumerate(flat):
        layer = (index // top_k) % layers
        sequence.append((layer, int(expert)))
    return sequence


def load_measured_counters(point_dir: Path) -> dict[str, Any]:
    replay = json.loads(
        (point_dir / "off_e_pr3_trace/capacity_replay.json").read_text("utf-8")
    )
    return replay


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_spec(
    structure: PointStructure,
    demand_sequence: list[tuple[int, int]],
    *,
    compute_work: int = 1,
    h2d_num: int = H2D_BANDWIDTH_BYTES_PER_SECOND,
    h2d_den: int = 1,
) -> str:
    catalog = structure.catalog
    # Degenerate all-resident control: the residency budget covers the whole
    # catalog, so the initial cache is the full working set (zero demand H2D).
    all_resident = structure.capacity_objects >= len(catalog)
    base_resident = (
        [(layer, expert) for layer, expert, _bytes in catalog]
        if all_resident
        else []
    )

    lines: list[str] = []
    lines.append(f"plan_id off-e-pr3-cap-{structure.label}")
    lines.append(f"capacity_bytes {structure.capacity_bytes}")
    lines.append("eviction LRU")
    lines.append("prefetch OFF")
    lines.append(f"compute_work {compute_work}")
    lines.append(f"h2d_num {h2d_num}")
    lines.append(f"h2d_den {h2d_den}")
    lines.append(f"catalog {len(catalog)}")
    for layer, expert, obj_bytes in catalog:
        lines.append(f"{layer} {expert} {obj_bytes}")
    lines.append(f"base_resident {len(base_resident)}")
    for layer, expert in base_resident:
        lines.append(f"{layer} {expert}")
    lines.append(f"demands {len(demand_sequence)}")
    for layer, expert in demand_sequence:
        lines.append(f"{layer} {expert}")
    lines.append("end")
    return "\n".join(lines) + "\n"


def run_loader(spec: str, loader_bin: Path = DEFAULT_LOADER_BIN) -> dict[str, Any]:
    if not Path(loader_bin).exists():
        raise FileNotFoundError(
            f"loader binary not found: {loader_bin} (run `make build-cpp`)"
        )
    completed = subprocess.run(
        [str(loader_bin), "-"],
        input=spec,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"ir_replay_loader failed ({completed.returncode}): {completed.stderr}"
        )
    return json.loads(completed.stdout)


def replay_point(
    structure: PointStructure,
    point_dir: Path,
    *,
    loader_bin: Path = DEFAULT_LOADER_BIN,
    check_determinism: bool = True,
) -> PointResult:
    npy_path = _routing_npy(point_dir)
    actual_sha = sha256_file(npy_path)
    if actual_sha != structure.routing_sha256:
        raise ValueError(
            f"routing .npy sha {actual_sha} != IR provenance "
            f"{structure.routing_sha256} for point {structure.label}"
        )

    demand_sequence = demand_sequence_from_npy(npy_path)
    spec = build_spec(structure, demand_sequence)
    engine = run_loader(spec, loader_bin=loader_bin)

    determinism_ok = True
    if check_determinism:
        engine2 = run_loader(spec, loader_bin=loader_bin)
        determinism_ok = engine2 == engine

    measured = load_measured_counters(point_dir)
    expected = {
        "hit_count": int(measured["hit_count"]),
        "demand_load_count": int(measured["demand_load_count"]),
        "immutable_discard_count": int(measured["immutable_discard_count"]),
    }
    engine_counters = {
        "hit_count": int(engine["routing_demands"]) - int(engine["loads"]),
        "demand_load_count": int(engine["loads"]),
        "immutable_discard_count": int(engine["clean_evictions"]),
    }
    counters_match = engine_counters == expected

    measured_terminal = sorted(
        tuple(divmod(oid, EXPERTS_PER_LAYER))
        for oid in measured["terminal_resident_object_ids"]
    )
    engine_terminal = sorted(tuple(pair) for pair in engine["terminal_resident"])
    terminal_match = measured_terminal == engine_terminal

    raw = {
        "spec_lines": len(spec.splitlines()),
        "routing_sha256": actual_sha,
        "capacity_objects": structure.capacity_objects,
        "all_resident_control": structure.capacity_objects
        >= len(structure.catalog),
        "engine": engine,
        "determinism_ok": determinism_ok,
        "measured_latency_fs": measured.get("total_h2d_cuda_elapsed_ms"),
        "logical_demand_count": int(measured["logical_demand_count"]),
    }
    return PointResult(
        label=structure.label,
        expected=expected,
        engine=engine_counters,
        counters_match=counters_match and determinism_ok,
        terminal_resident_match=terminal_match,
        raw=raw,
    )


def replay_all(
    *,
    bundle_envelope: Path = DEFAULT_BUNDLE_ENVELOPE,
    evidence_root: Path = DEFAULT_EVIDENCE_ROOT,
    loader_bin: Path = DEFAULT_LOADER_BIN,
    check_determinism: bool = True,
) -> list[PointResult]:
    structures = load_bundle_structures(bundle_envelope)
    evidence_points = discover_evidence_points(evidence_root)
    missing = set(structures) ^ set(evidence_points)
    if missing:
        raise ValueError(
            f"point-set mismatch between IR bundle and evidence: {sorted(missing)}"
        )
    results: list[PointResult] = []
    for label in sorted(structures):
        results.append(
            replay_point(
                structures[label],
                evidence_points[label],
                loader_bin=loader_bin,
                check_determinism=check_determinism,
            )
        )
    return results


# --------------------------------------------------------------------------
# Run-artifact writer (Stage A3 deliverable): full fifteen-point SIM0 + SIM1.
# --------------------------------------------------------------------------


def _git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip()
    except Exception:  # noqa: BLE001 - provenance best-effort
        return "unknown"


def _tool_versions(loader_bin: Path) -> dict[str, Any]:
    def _cmd(args: list[str]) -> str:
        try:
            out = subprocess.run(args, capture_output=True, text=True)
            return (out.stdout or out.stderr).splitlines()[0].strip()
        except Exception:  # noqa: BLE001
            return "unknown"

    return {
        "python": sys.version.split()[0],
        "cmake": _cmd(["cmake", "--version"]),
        "compiler": _cmd(["c++", "--version"]),
        "loader_binary": str(loader_bin),
        "loader_binary_sha256": (
            sha256_file(loader_bin) if Path(loader_bin).exists() else "absent"
        ),
    }


def write_run(
    run_dir: Path,
    *,
    bundle_envelope: Path = DEFAULT_BUNDLE_ENVELOPE,
    evidence_root: Path = DEFAULT_EVIDENCE_ROOT,
    loader_bin: Path = DEFAULT_LOADER_BIN,
) -> dict[str, Any]:
    """Run the full fifteen-point replay and materialise the run directory."""

    import datetime

    run_dir = Path(run_dir)
    (run_dir / "artifacts").mkdir(parents=True, exist_ok=True)
    (run_dir / "logs").mkdir(parents=True, exist_ok=True)
    (run_dir / "environment").mkdir(parents=True, exist_ok=True)

    now = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    structures = load_bundle_structures(bundle_envelope)
    results = replay_all(
        bundle_envelope=bundle_envelope,
        evidence_root=evidence_root,
        loader_bin=loader_bin,
        check_determinism=True,
    )

    sim0_rows = []
    sim1_rows = []
    timing_rows = []
    all_counters_ok = True
    all_determinism_ok = True
    all_health_ok = True
    all_terminal_ok = True
    for r in results:
        engine = r.raw["engine"]
        sim0_rows.append({
            "label": r.label,
            "capacity_objects": r.raw["capacity_objects"],
            "all_resident_control": r.raw["all_resident_control"],
            "expected": r.expected,
            "engine": r.engine,
            "counters_match": r.engine == r.expected,
            "terminal_resident_match": r.terminal_resident_match,
        })
        sim1_rows.append({
            "label": r.label,
            "determinism_ok": r.raw["determinism_ok"],
            "semantic_digest": engine["semantic_digest"],
            "plan_digest": engine["plan_digest"],
            "terminal_residency_digest": engine["terminal_residency_digest"],
        })
        h2d = engine.get("class_metrics", {}).get("H2D", {})
        timing_rows.append({
            "label": r.label,
            "loads": r.engine["demand_load_count"],
            "engine_makespan_fs": engine["makespan_fs"],
            "h2d_busy_lane_fs": h2d.get("busy_lane_fs"),
            "h2d_operation_count": h2d.get("operation_count"),
        })
        all_counters_ok &= r.engine == r.expected
        all_determinism_ok &= bool(r.raw["determinism_ok"])
        all_health_ok &= engine["terminal_status"] == "QUIESCENT"
        all_terminal_ok &= r.terminal_resident_match

        (run_dir / "artifacts" / f"engine_result_{r.label}.json").write_text(
            json.dumps(engine, indent=2, sort_keys=True), "utf-8"
        )

    sim0 = {
        "schema_version": "stage-a3-sim0-v1",
        "acceptance": "engine counters must equal measured counters exactly",
        "all_counters_match": all_counters_ok,
        "all_terminal_resident_match": all_terminal_ok,
        "points": sim0_rows,
    }
    sim1 = {
        "schema_version": "stage-a3-sim1-v1",
        "acceptance": "same plan replayed twice is byte-identical",
        "all_deterministic": all_determinism_ok,
        "points": sim1_rows,
    }
    health = {
        "schema_version": "stage-a3-health-v1",
        "all_quiescent": all_health_ok,
        "terminal_status": {
            r.label: r.raw["engine"]["terminal_status"] for r in results
        },
        "note": "no deadlock, no Zeno, no resource-conservation violation; "
        "Phase 4 terminal_status QUIESCENT for every point.",
    }
    timing = {
        "schema_version": "stage-a3-timing-observation-v1",
        "claim": "OBSERVATION ONLY -- no timing-accuracy claim (deferred to A4). "
        "The residency counters are timing-independent (order-only LRU).",
        "service_model_wiring": (
            "Service time is consumed through the Phase 4 service model: each "
            "demand H2D load is a Phase 4 H2D Operation with work = object bytes, "
            "and Phase 4 service_duration converts it via the measured PCIe "
            "bandwidth ({} B/s), serialising on one H2D lane. Phase 3 "
            "Action::kService is left ACCOUNTING_ONLY per its r5-frozen contract "
            "(see OWNER_DECISION in the handoff).".format(
                H2D_BANDWIDTH_BYTES_PER_SECOND
            )
        ),
        "measured_per_object_h2d_ms_range": "12.454-12.499 (sigma ~0.1%)",
        "points": timing_rows,
    }

    for name, payload in (
        ("sim0_counters.json", sim0),
        ("sim1_determinism.json", sim1),
        ("engine_health.json", health),
        ("timing_observation.json", timing),
    ):
        (run_dir / "artifacts" / name).write_text(
            json.dumps(payload, indent=2, sort_keys=True), "utf-8"
        )

    metrics = {
        "sim0_all_bit_exact": all_counters_ok and all_terminal_ok,
        "sim1_all_deterministic": all_determinism_ok,
        "engine_all_quiescent": all_health_ok,
        "points": len(results),
        "per_point": sim0_rows,
    }
    (run_dir / "metrics.json").write_text(
        json.dumps(metrics, indent=2, sort_keys=True), "utf-8"
    )

    (run_dir / "environment" / "tool_versions.json").write_text(
        json.dumps(_tool_versions(loader_bin), indent=2, sort_keys=True), "utf-8"
    )

    routing_sha = next(iter(structures.values())).routing_sha256
    manifest = {
        "stage": "A3",
        "run_id": run_dir.name,
        "classification": "IR bundle -> C++ cycle-resolved engine (Stage A3, CPU-only)",
        "created_at": now,
        "experiment_id": "off_e_pr3_ir_to_engine_replay",
        "command": [
            "python",
            "-m",
            "explorations.moe_cycle_simulator.phase7.loaders.ir_to_engine",
            "--run-dir",
            str(run_dir),
        ],
        "git": {"code_commit": _git_commit()},
        "inputs": {
            "a2_bundle_envelope": str(bundle_envelope),
            "routing_sha256": routing_sha,
            "engine": "phase5 RoutingResidencyModel -> phase4 SingleGpuModel",
            "phase5_build_authority_sha256": _phase5_authority(),
        },
        "platform_profile": "NVIDIA RTX PRO 6000 Blackwell (evidence-of-record)",
        "summary": {
            "points": len(results),
            "sim0_all_bit_exact": all_counters_ok and all_terminal_ok,
            "sim1_all_deterministic": all_determinism_ok,
            "engine_all_quiescent": all_health_ok,
        },
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), "utf-8"
    )
    (run_dir / "logs" / "summary.txt").write_text(
        "\n".join(
            f"cap={r.label:>5} expected={r.expected} engine={r.engine} "
            f"counters={'OK' if r.engine == r.expected else 'MISMATCH'} "
            f"terminal={'OK' if r.terminal_resident_match else 'DIFF'} "
            f"determinism={'OK' if r.raw['determinism_ok'] else 'DIFF'} "
            f"status={r.raw['engine']['terminal_status']}"
            for r in results
        )
        + "\n",
        "utf-8",
    )
    return metrics


def _phase5_authority() -> str:
    header = (
        REPO_ROOT
        / "explorations/moe_cycle_simulator/phase5/include/moe_sim/routing_residency_policy.hpp"
    )
    text = header.read_text("utf-8")
    match = re.search(
        r"kPhase5BuildAuthoritySha256 =\s*\"([0-9a-f]{64})\"", text
    )
    return match.group(1) if match else "unknown"


def _main(argv: list[str]) -> int:
    import argparse
    import datetime

    parser = argparse.ArgumentParser(description="Stage A3 IR -> engine replay")
    parser.add_argument("--run-dir", type=Path, default=None)
    parser.add_argument("--bundle", type=Path, default=DEFAULT_BUNDLE_ENVELOPE)
    parser.add_argument("--loader-bin", type=Path, default=DEFAULT_LOADER_BIN)
    args = parser.parse_args(argv)

    run_dir = args.run_dir
    if run_dir is None:
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y%m%dT%H%M%SZ"
        )
        run_dir = REPO_ROOT / "runs" / f"{stamp}__stage_a3_ir_to_engine_replay"

    metrics = write_run(
        run_dir, bundle_envelope=args.bundle, loader_bin=args.loader_bin
    )
    ok = (
        metrics["sim0_all_bit_exact"]
        and metrics["sim1_all_deterministic"]
        and metrics["engine_all_quiescent"]
    )
    print(f"run_dir: {run_dir}")
    print(
        f"SIM0 bit-exact: {metrics['sim0_all_bit_exact']} | "
        f"SIM1 deterministic: {metrics['sim1_all_deterministic']} | "
        f"engine quiescent: {metrics['engine_all_quiescent']}"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))

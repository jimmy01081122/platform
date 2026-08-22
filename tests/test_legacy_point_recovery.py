"""Tests for the one-time frozen-harness evaluation-point recovery (P-020 (a)).

Discipline mirrored from tests/test_gpu_prep.py: the normal fixture is accepted
and the failure fixture RAISES -- a recovery tool that silently drops or guesses
an unrecoverable operand shape would reintroduce exactly the defect (GAP-4) it
exists to work around.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from measurement.parsers.common import ValidationError
from measurement.parsers.component_eval_parser import validate as component_validate
from measurement.parsers.legacy_component_point_recovery import (
    DIRECT,
    RECOVERED,
    build_points,
    recover,
    recover_expert_tokens,
)

FIXTURES = Path(__file__).parent / "fixtures" / "gpu_prep"
PASS_FIXTURE = FIXTURES / "legacy_recovery_pass.json"
FAIL_FIXTURE = FIXTURES / "legacy_recovery_fail_no_expert_tokens.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def test_recovered_points_satisfy_enforce_gap4():
    """The whole point: output must pass the parser's GAP-4 enforcement."""
    recovered = recover(PASS_FIXTURE)
    root = component_validate(recovered, enforce_gap4=True)
    assert root["_gap4_affected_points"] == []


def test_every_component_point_carries_expert_tokens_directly():
    points = build_points(_load(PASS_FIXTURE))
    component = [p for p in points if p["metric"] == "component_latency"]
    assert component, "fixture must exercise the component branch"
    for point in component:
        assert "expert_tokens" in point["features"]
        assert isinstance(point["features"]["expert_tokens"], int)


def test_recovered_values_match_the_case_string():
    points = build_points(_load(PASS_FIXTURE))
    by_source = {p["source_record_id"]: p for p in points
                 if p["metric"] == "component_latency"}
    assert by_source["calibration-fixture-gemm-0003"]["features"]["expert_tokens"] == 704
    assert by_source["calibration-fixture-sel-0004"]["features"]["expert_tokens"] == 32


def test_component_points_are_labelled_as_a_legacy_join():
    """The recovery path must be self-identifying, never indistinguishable
    from a probe that carried the shape directly."""
    points = build_points(_load(PASS_FIXTURE))
    for point in points:
        if point["metric"] == "component_latency":
            assert point["derivation"] == RECOVERED
            assert point["recovered_from"] == "case_string"
        else:
            # PCIe/replay features are structured fields already -- no join.
            assert point["derivation"] == DIRECT


def test_all_metric_families_are_recovered():
    points = build_points(_load(PASS_FIXTURE))
    metrics = {p["metric"] for p in points}
    assert metrics == {
        "pcie_transfer_latency", "component_latency",
        "moe_replay_tpot", "moe_replay_throughput",
    }
    # environment-probe rows (cpu_runtime) yield no point, as in the frozen harness
    assert not any(p["source_record_id"].endswith("cpu-0006") for p in points)


def test_pcie_points_carry_bytes_and_direction():
    points = build_points(_load(PASS_FIXTURE))
    pcie = [p for p in points if p["metric"] == "pcie_transfer_latency"]
    assert len(pcie) == 2
    for point in pcie:
        assert "bytes" in point["features"]
        assert "direction" in point["features"]
        assert "copy_streams" in point["features"]


def test_point_ids_are_deterministic_and_unique():
    first = build_points(_load(PASS_FIXTURE))
    second = build_points(_load(PASS_FIXTURE))
    ids = [p["point_id"] for p in first]
    assert ids == [p["point_id"] for p in second]
    assert len(ids) == len(set(ids))


# --- failure cases: must RAISE, never guess ------------------------------


def test_unrecoverable_shape_raises():
    with pytest.raises(ValidationError, match="expert_tokens"):
        build_points(_load(FAIL_FIXTURE))


def test_recover_expert_tokens_rejects_missing_term():
    with pytest.raises(ValidationError, match="unrecoverable"):
        recover_expert_tokens("phase=prefill,concurrency=1", "rec-1")


def test_recover_expert_tokens_rejects_non_string_case():
    with pytest.raises(ValidationError):
        recover_expert_tokens(None, "rec-1")


def test_refuses_result_that_already_has_points(tmp_path):
    payload = _load(PASS_FIXTURE)
    payload["evaluation_points"] = [{"metric": "component_latency"}]
    path = tmp_path / "already.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValidationError, match="already carries"):
        recover(path)


def test_wrong_schema_version_raises(tmp_path):
    payload = _load(PASS_FIXTURE)
    payload["schema_version"] = "some-other-schema-v9"
    path = tmp_path / "wrong_schema.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValidationError, match="schema_version"):
        recover(path)


def test_recovery_metadata_records_source_hash_and_counts():
    recovered = recover(PASS_FIXTURE)
    meta = recovered["evaluation_point_recovery"]
    assert meta["points_legacy_join_recovered"] == 2
    assert meta["points_total"] == len(recovered["evaluation_points"])
    assert len(meta["source_sha256"]) == 64
    assert "MUST NOT be the standard" in meta["claim_boundary"]


def test_raw_benchmarks_are_not_mutated():
    """Raw is read-only (root spec §9.4); recovery is additive only."""
    before = _load(PASS_FIXTURE)
    recovered = recover(PASS_FIXTURE)
    assert recovered["raw_benchmarks"] == before["raw_benchmarks"]
    assert _load(PASS_FIXTURE) == before

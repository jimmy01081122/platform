"""Stage B2 acceptance tests for the parametric candidate-processor model.

Covers, one test group per guide §5 acceptance row:
  * reference mock: transaction adapter / clock stepping / backpressure /
    completion / counter (five paths)
  * anti-forgery: an unregistered backend is refused, not silently substituted
  * fidelity audit: nothing in accelerator/ is MEASURED_SURROGATE; the gate rejects it
  * attachment points: A1..A4 each define work-unit / accelerator-cost / transfer-cost
  * no-evidence points: A2 and A6 are marked unmeasured and forbid perf conclusions
  * scannable: all nine resource parameters sweep from config
"""
from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import pytest
import yaml

from accelerator.abi import (
    BackendNotRegistered,
    Backpressure,
    Transaction,
)
from accelerator.attachment_points import (
    GRANULARITIES,
    default_attachment_points,
    unmeasured_points,
)
from accelerator.backends import default_registry
from accelerator.backends.reserved import RESERVED, RtlTraceReplayBackend
from accelerator.fidelity import (
    FORBIDDEN_IN_ACCELERATOR,
    Fidelity,
    FidelityViolation,
    Provenance,
    require_accelerator_fidelity,
)
from accelerator.resource_model import (
    PARAM_NAMES,
    ResourceModel,
    ResourceSweep,
)

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs/accelerator/resource_model_default.yaml"


def _model(**overrides) -> ResourceModel:
    base = dict(
        pipeline_latency_cycles=2,
        issue_width=1,
        local_sram_capacity_bytes=2_097_152,
        memory_bandwidth_bytes_per_s=28_298_591_668,
        queue_depth=4,
        operations_per_cycle=4,
        clock_frequency_hz=Fraction(500_000_000, 1),
        area_proxy_um2=4902.0,
        power_proxy_mw=50.0,
    )
    base.update(overrides)
    return ResourceModel(**base)


# --- reference mock: five ABI paths ------------------------------------------

def _mock(**overrides):
    reg = default_registry()
    return reg.create("REFERENCE_MOCK", _model(**overrides), Fidelity.ANALYTICAL)


def test_reference_mock_transaction_adapter_path() -> None:
    """submit() adapts a work unit into an internal in-flight transaction."""
    mock = _mock()
    txn = Transaction(txn_id=1, attachment_point="A1", work_bytes=0, op_count=8)
    assert mock.can_accept(txn)
    mock.submit(txn)
    assert mock.snapshot_counters().accepted == 1
    # not yet completed until the clock advances
    assert mock.poll_completions() == []


def test_reference_mock_clock_stepping_path() -> None:
    """advance() moves the integer-fs clock via the rational clock domain."""
    mock = _mock(clock_frequency_hz=Fraction(1_000_000_000, 1))  # 1 GHz -> 1e6 fs/cycle
    assert mock.resources.cycle_period_fs == 1_000_000
    mock.advance(5)
    c = mock.snapshot_counters()
    assert c.cycles_advanced == 5
    assert c.time_fs == 5 * 1_000_000


def test_reference_mock_backpressure_path() -> None:
    """can_accept() reflects a full queue; submit() past capacity raises + counts."""
    mock = _mock(queue_depth=2, issue_width=1, pipeline_latency_cycles=10)
    for i in range(2):
        mock.submit(Transaction(txn_id=i, attachment_point="A3", work_bytes=0, op_count=1))
    full = Transaction(txn_id=99, attachment_point="A3", work_bytes=0, op_count=1)
    assert mock.can_accept(full) is False
    with pytest.raises(Backpressure):
        mock.submit(full)
    assert mock.snapshot_counters().backpressure_events == 1


def test_reference_mock_completion_path() -> None:
    """advance() eventually retires submitted work; poll returns it in submit order."""
    mock = _mock(pipeline_latency_cycles=3, issue_width=2, operations_per_cycle=100)
    for i in range(2):
        mock.submit(Transaction(txn_id=i, attachment_point="A1", work_bytes=0, op_count=1))
    mock.advance(1)          # both issue at cycle 0
    assert mock.poll_completions() == []  # latency 3 not yet elapsed
    mock.advance(3)
    comps = mock.poll_completions()
    assert [c.txn_id for c in comps] == [0, 1]
    assert all(c.latency_fs > 0 for c in comps)


def test_reference_mock_counter_path() -> None:
    """snapshot_counters() returns a stable cumulative snapshot (a copy)."""
    mock = _mock(pipeline_latency_cycles=1, issue_width=4, operations_per_cycle=100)
    for i in range(3):
        mock.submit(Transaction(txn_id=i, attachment_point="A1", work_bytes=0, op_count=1))
    mock.advance(5)
    snap = mock.snapshot_counters()
    assert snap.submitted == 3 and snap.accepted == 3 and snap.completed == 3
    # snapshot is a copy: further work does not mutate the earlier snapshot
    mock.submit(Transaction(txn_id=9, attachment_point="A1", work_bytes=0, op_count=1))
    assert snap.submitted == 3


def test_reference_mock_reset_clears_state() -> None:
    mock = _mock()
    mock.submit(Transaction(txn_id=1, attachment_point="A1", work_bytes=0, op_count=1))
    mock.advance(10)
    mock.reset()
    c = mock.snapshot_counters()
    assert c.submitted == 0 and c.completed == 0 and c.cycles_advanced == 0


# --- anti-forgery: unregistered backend refused ------------------------------

def test_registered_backends_are_the_three_expected() -> None:
    reg = default_registry()
    assert set(reg.registered_names()) == {
        "FUNCTIONAL_POLICY",
        "CYCLE_RESOLVED_MODEL",
        "REFERENCE_MOCK",
    }


def test_reserved_backends_are_declared_but_not_registered() -> None:
    reg = default_registry()
    assert set(reg.reserved_names()) == set(RESERVED)
    for name in RESERVED:
        assert not reg.is_registered(name)


@pytest.mark.parametrize("name", sorted(RESERVED))
def test_unregistered_backend_is_refused_not_substituted(name: str) -> None:
    """The three RTL/cosim backends are reserved; dispatching one must raise."""
    reg = default_registry()
    with pytest.raises(BackendNotRegistered, match="not registered"):
        reg.create(name, _model(), Fidelity.ANALYTICAL)


def test_wholly_unknown_backend_is_refused() -> None:
    reg = default_registry()
    with pytest.raises(BackendNotRegistered, match="cannot be silently substituted"):
        reg.create("gpu-cycle-missing", _model(), Fidelity.ANALYTICAL)


def test_reserved_backend_cannot_be_constructed() -> None:
    with pytest.raises(NotImplementedError):
        RtlTraceReplayBackend(_model(), Fidelity.ANALYTICAL)


# --- fidelity audit: no MEASURED_SURROGATE anywhere ---------------------------

def test_measured_surrogate_is_forbidden_in_accelerator() -> None:
    assert "MEASURED_SURROGATE" in FORBIDDEN_IN_ACCELERATOR
    with pytest.raises(FidelityViolation, match="forbidden"):
        require_accelerator_fidelity("MEASURED_SURROGATE")


@pytest.mark.parametrize("label", ["ANALYTICAL", "PROJECTED"])
def test_allowed_fidelities_pass(label: str) -> None:
    assert require_accelerator_fidelity(label).value == label


def test_backend_rejects_measured_surrogate_fidelity() -> None:
    reg = default_registry()
    with pytest.raises(FidelityViolation):
        reg.create("REFERENCE_MOCK", _model(), "MEASURED_SURROGATE")


def test_provenance_cannot_be_measured() -> None:
    with pytest.raises(FidelityViolation):
        Provenance(fidelity=Fidelity.ANALYTICAL, evidence_refs=(), claim_limit="x", measured=True)


def test_no_attachment_point_component_is_measured_surrogate() -> None:
    """Fidelity audit: every attachment-point cost model is ANALYTICAL/PROJECTED."""
    for point in default_attachment_points().values():
        for cm in (point.accelerator_cost, point.transfer_cost, None):
            if cm is None:
                continue
            assert cm.provenance.fidelity in (Fidelity.ANALYTICAL, Fidelity.PROJECTED)
            assert cm.provenance.fidelity.value != "MEASURED_SURROGATE"
        assert point.work_unit.provenance.fidelity in (Fidelity.ANALYTICAL, Fidelity.PROJECTED)


# --- attachment points: A1..A4 define the three things -----------------------

def test_all_six_attachment_points_present() -> None:
    pts = default_attachment_points()
    assert set(pts) == {"A1", "A2", "A3", "A4", "A5", "A6"}


@pytest.mark.parametrize("pid", ["A1", "A2", "A3", "A4"])
def test_primary_points_define_three_things(pid: str) -> None:
    p = default_attachment_points()[pid]
    assert p.priority == "primary"
    # 1. work unit + baseline cost
    assert p.work_unit.description and p.work_unit.baseline_cost_model
    assert p.work_unit.granularity in GRANULARITIES
    # 2. accelerator cost model
    assert p.accelerator_cost.form and p.accelerator_cost.expression
    # 3. transfer cost (the often-forgotten one)
    assert p.transfer_cost.form and p.transfer_cost.expression


def test_secondary_points_are_a5_a6() -> None:
    pts = default_attachment_points()
    assert pts["A5"].priority == "secondary"
    assert pts["A6"].priority == "secondary"


# --- no-evidence points: A2 and A6 -------------------------------------------

def test_a2_and_a6_are_unmeasured() -> None:
    assert unmeasured_points() == ["A2", "A6"]


@pytest.mark.parametrize("pid", ["A2", "A6"])
def test_unmeasured_points_forbid_performance_conclusions(pid: str) -> None:
    p = default_attachment_points()[pid]
    assert p.measured is False
    assert p.performance_conclusion_allowed() is False
    # their cost models are PROJECTED with an explicit no-evidence claim limit
    assert p.accelerator_cost.provenance.fidelity == Fidelity.PROJECTED
    assert "NO on-device measurement" in p.accelerator_cost.provenance.claim_limit


def test_measured_flag_cannot_be_set_on_a2() -> None:
    """Guard: A2/A6 cannot be constructed as measured=True."""
    from accelerator.attachment_points import AttachmentPoint, CostModel, WorkUnit

    proj = default_attachment_points()["A2"].work_unit.provenance
    with pytest.raises(ValueError, match="NO measurement"):
        AttachmentPoint(
            point_id="A2",
            function="x",
            priority="primary",
            measured=True,
            work_unit=WorkUnit("d", "per_token", "o", "c", proj),
            accelerator_cost=CostModel("f", "e", proj),
            transfer_cost=CostModel("f", "e", proj),
        )


# --- scannable: nine resource parameters from config -------------------------

def test_nine_param_names() -> None:
    assert len(PARAM_NAMES) == 9


def test_resource_sweep_from_config_scans_all_nine() -> None:
    config = yaml.safe_load(CONFIG.read_text())
    sweep = ResourceSweep.from_config(config)
    # every one of the nine axes has at least one value
    for name in PARAM_NAMES:
        assert len(sweep.axes[name]) >= 1
    # at least one axis is genuinely swept (>1 value) so the sweep is non-trivial
    assert any(len(sweep.axes[name]) > 1 for name in PARAM_NAMES)
    first = next(iter(sweep))
    assert isinstance(first, ResourceModel)
    assert first.fidelity == Fidelity.ANALYTICAL


def test_resource_sweep_guardrail_rejects_explosion() -> None:
    axes = {name: [1] for name in PARAM_NAMES}
    axes["issue_width"] = [1, 2, 4]
    axes["queue_depth"] = [1, 2, 4, 8]
    # positive fields need valid values
    axes["memory_bandwidth_bytes_per_s"] = [1_000_000_000]
    axes["operations_per_cycle"] = [1, 2]
    axes["clock_frequency_hz"] = ["100000000/1"]
    axes["area_proxy_um2"] = [1.0]
    axes["power_proxy_mw"] = [1.0]
    axes["pipeline_latency_cycles"] = [1]
    axes["local_sram_capacity_bytes"] = [1]
    sweep = ResourceSweep(axes=axes, max_points=5)
    with pytest.raises(ValueError, match="max_points"):
        list(sweep)


def test_clock_period_is_integer_fs_no_drift() -> None:
    m = _model(clock_frequency_hz=Fraction(3, 1))  # 3 Hz -> period 1/3 s
    # floor(1e15/3) fs, exact integer, matches engine edge_time floor convention
    assert m.cycle_period_fs == (1 * 10**15) // 3
    assert m.cycles_to_fs(3) == 3 * m.cycle_period_fs


def test_cycle_resolved_adds_bandwidth_term_over_functional() -> None:
    """CYCLE_RESOLVED_MODEL charges transfer time that FUNCTIONAL_POLICY omits."""
    reg = default_registry()
    m = _model(pipeline_latency_cycles=1, operations_per_cycle=100, issue_width=1)
    big = Transaction(txn_id=1, attachment_point="A3", work_bytes=352_321_536, op_count=1)

    fp = reg.create("FUNCTIONAL_POLICY", m, Fidelity.ANALYTICAL)
    cr = reg.create("CYCLE_RESOLVED_MODEL", m, Fidelity.ANALYTICAL)
    assert cr.service_cycles(big) > fp.service_cycles(big)

"""TRACK_GPU_PREP PREP-3: V2-GAP-A multi-object aggregate probe + parser tests.

All pure CPU (no GPU / torch / PCIe / serving). Verifies:
  * the probe runs its full path against the mock backend and stamps the result
    ``cpu_smoke_test_not_measurement``;
  * the real GPU backend refuses loudly when CUDA is unavailable;
  * the parser accepts a well-formed result and RAISES on each failure fixture
    (non-physical aggregate; the intra-object S/copy_streams axis being borrowed
    for multi-object concurrency) -- loud failure, never a silent skip;
  * V2-GAP-A's IR evaluation-point projection is left PENDING (not guessed),
    because the S>1 production semantics it exists to define is still UNSUPPORTED.
"""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from types import SimpleNamespace
from pathlib import Path

import pytest

from measurement.probes import multistream_aggregate_probe as probe
from measurement.probes.aggregate_backend import (
    MockAggregateBackend, TorchAggregateBackend, regime_of,
)
from measurement.probes.mock_backend import BackendError
from measurement.parsers import ValidationError
from measurement.parsers import multistream_aggregate_parser as parser

FIXTURES = Path(__file__).parent / "fixtures" / "gpu_prep"


# --------------------------------------------------------------------------- #
# Probe CPU smoke test
# --------------------------------------------------------------------------- #

def test_aggregate_probe_cpu_smoke(tmp_path):
    out = tmp_path / "agg.json"
    rc = probe.main([
        "--backend", "mock_aggregate", "--out", str(out),
        "--num-objects", "1,2,4,8", "--object-bytes", "65536,2097152,352321536",
        "--direction", "h2d,d2h", "--repeats", "5",
    ])
    assert rc == 0
    result = json.loads(out.read_text())
    assert result["evidence"] == "cpu_smoke_test_not_measurement"
    assert result["concurrency_axis"] == "num_objects"
    # V2-GAP-A IR projection is not determinable yet -> PENDING, never guessed
    assert result["ir_evaluation_point_fields"] == "PENDING_S_GT_1_SEMANTICS"
    assert result["production_stream_semantics_status"] == "UNSUPPORTED_UNTIL_MEASURED"
    # 4 N x 3 sizes x 2 directions = 24 cells, all self-consistent
    assert len(result["cells"]) == 24
    parser.validate(result)


def test_aggregate_probe_fixes_copy_streams_to_one(tmp_path):
    """Every cell must be S=1 (num_objects is the axis, NOT copy_streams)."""
    out = tmp_path / "agg.json"
    probe.main(["--backend", "mock_aggregate", "--out", str(out),
                "--num-objects", "1,4", "--object-bytes", "2097152",
                "--direction", "h2d", "--repeats", "5"])
    result = json.loads(out.read_text())
    assert all(c["copy_streams"] == 1 for c in result["cells"])


def test_aggregate_probe_envelope_holds(tmp_path):
    """Mock aggregate obeys max(per_object) <= aggregate <= sum(per_object)."""
    out = tmp_path / "agg.json"
    probe.main(["--backend", "mock_aggregate", "--out", str(out),
                "--num-objects", "1,2,4,8", "--object-bytes", "65536,352321536",
                "--direction", "h2d,d2h", "--repeats", "5"])
    result = json.loads(out.read_text())
    for c in result["cells"]:
        agg = c["aggregate_completion_ms_mean"]
        assert c["max_per_object_ms"] <= agg + 1e-9
        assert agg <= c["sum_per_object_ms"] + 1e-9


def test_aggregate_probe_refuses_gpu_backend_without_cuda(tmp_path, monkeypatch):
    fake_torch = SimpleNamespace(
        cuda=SimpleNamespace(is_available=lambda: False)
    )
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    with pytest.raises(BackendError):
        probe.run(probe.parse_args(
            ["--backend", "gpu", "--out", str(tmp_path / "x.json")]))


def test_gpu_backend_keeps_num_objects_separate_from_copy_streams():
    assert TorchAggregateBackend.name == "gpu"


class _FakeEvent:
    def __init__(self, cuda):
        self.cuda = cuda
        self.timestamp = None

    def record(self, stream):
        self.timestamp = stream.clock

    def elapsed_time(self, other):
        return other.timestamp - self.timestamp


class _FakeStream:
    def __init__(self, cuda, transfer_ms=0.0):
        self.cuda = cuda
        self.transfer_ms = transfer_ms
        self.clock = 0.0

    def wait_event(self, event):
        self.clock = max(self.clock, event.timestamp)


class _FakeBuffer:
    def __init__(self, cuda):
        self.cuda = cuda

    def copy_(self, _source, *, non_blocking):
        assert non_blocking is True
        stream = self.cuda.active_stream
        stream.clock += stream.transfer_ms


class _FakeCuda:
    """Small event/stream clock used to test CUDA orchestration on CPU."""

    def __init__(self):
        self.coordinator = _FakeStream(self)
        self.active_stream = self.coordinator
        self.next_transfer = 0
        self.synchronize_calls = 0

    def is_available(self):
        return True

    def Event(self, *, enable_timing):
        assert enable_timing is True
        return _FakeEvent(self)

    def Stream(self):
        self.next_transfer += 1
        return _FakeStream(self, float(self.next_transfer))

    def current_stream(self):
        return self.coordinator

    @contextmanager
    def stream(self, stream):
        previous = self.active_stream
        self.active_stream = stream
        try:
            yield
        finally:
            self.active_stream = previous

    def synchronize(self):
        self.synchronize_calls += 1

    def empty_cache(self):
        pass


class _FakeTorch:
    uint8 = "uint8"

    def __init__(self):
        self.cuda = _FakeCuda()

    def empty(self, object_bytes, *, dtype, pin_memory=False, device=None):
        assert object_bytes > 0
        assert dtype == self.uint8
        assert pin_memory is True or device == "cuda"
        return _FakeBuffer(self.cuda)


@pytest.mark.parametrize("direction", ["h2d", "d2h"])
def test_gpu_backend_measures_independent_streams_and_wait_all(direction):
    fake_torch = _FakeTorch()
    backend = TorchAggregateBackend(
        _torch=fake_torch,
        warmup_repeats=0,
    )

    cell = backend.measure_cell(
        num_objects=2,
        object_bytes=65536,
        direction=direction,
        repeats=5,
    )

    assert cell["copy_streams"] == 1
    assert cell["aggregate_completion_ms_samples"] == [2.0] * 5
    assert cell["aggregate_completion_ms_mean"] == 2.0
    assert cell["per_object_ms"] == [1.0, 2.0]
    assert cell["max_per_object_ms"] == 2.0
    assert cell["sum_per_object_ms"] == 3.0
    # One allocation barrier plus one synchronization for each repeat.
    assert fake_torch.cuda.synchronize_calls == 6


def test_regime_classification_matches_prereg_boundaries():
    assert regime_of(65536) == "small"        # <= 1 MiB
    assert regime_of(2097152) == "transition"  # KV block, between the fences
    assert regime_of(352321536) == "bulk"      # >= 21 MiB, expert object


def test_backend_rejects_bad_axes():
    b = MockAggregateBackend()
    for bad in [
        dict(num_objects=0, object_bytes=4096, direction="h2d", repeats=5),
        dict(num_objects=1, object_bytes=0, direction="h2d", repeats=5),
        dict(num_objects=1, object_bytes=4096, direction="p2p", repeats=5),
        dict(num_objects=1, object_bytes=4096, direction="h2d", repeats=0),
    ]:
        with pytest.raises(BackendError):
            b.measure_cell(**bad)


# --------------------------------------------------------------------------- #
# Parser fixtures: normal accepted, every failure RAISES
# --------------------------------------------------------------------------- #

def test_aggregate_parser_pass_fixture():
    ok = json.loads((FIXTURES / "multistream_aggregate_pass.json").read_text())
    root = parser.validate(ok)
    assert root["concurrency_axis"] == "num_objects"


def test_aggregate_parser_rejects_nonphysical_aggregate():
    bad = json.loads(
        (FIXTURES / "multistream_aggregate_fail_nonphysical.json").read_text())
    with pytest.raises(ValidationError, match="non-physical"):
        parser.validate(bad)


def test_aggregate_parser_rejects_stream_axis_misuse():
    bad = json.loads(
        (FIXTURES / "multistream_aggregate_fail_stream_axis.json").read_text())
    with pytest.raises(ValidationError, match="copy_streams"):
        parser.validate(bad)


def test_aggregate_parser_rejects_wrong_axis_label():
    ok = json.loads((FIXTURES / "multistream_aggregate_pass.json").read_text())
    ok["concurrency_axis"] = "copy_streams"
    with pytest.raises(ValidationError, match="concurrency_axis"):
        parser.validate(ok)


def test_aggregate_parser_rejects_sample_count_mismatch():
    ok = json.loads((FIXTURES / "multistream_aggregate_pass.json").read_text())
    ok["cells"][0]["aggregate_completion_ms_samples"] = [0.074898286]  # != repeats
    with pytest.raises(ValidationError, match="samples"):
        parser.validate(ok)


def test_aggregate_parser_rejects_per_object_length_mismatch():
    ok = json.loads((FIXTURES / "multistream_aggregate_pass.json").read_text())
    ok["cells"][1]["per_object_ms"] = [13.55, 13.55]  # num_objects is 4
    with pytest.raises(ValidationError, match="per_object_ms"):
        parser.validate(ok)

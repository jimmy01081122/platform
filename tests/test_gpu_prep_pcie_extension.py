"""PCIe extension probe + backend + parser tests (bidirectional concurrency,
S-axis crossover densification, the declared-never-measured 4096-byte cell,
and nvidia-smi PCIe-link capture).

All pure CPU (no GPU / torch / PCIe). Verifies:
  * the probe runs its full path against the mock backend and stamps the
    result ``cpu_smoke_test_not_measurement``;
  * ``--axis`` correctly selects which cell groups get populated, while
    ``pcie_link_environment`` is always captured regardless;
  * the real GPU backend refuses loudly when CUDA is unavailable, and (via a
    minimal fake-CUDA clock harness, same technique as
    tests/test_gpu_prep_v2gapa.py) that its bidirectional and stream-split
    primitives implement the wait-on-every-leg-before-recording-the-joint-
    event discipline correctly rather than charging only a coordinator
    dispatch delay;
  * ``capture_pcie_link_info`` issues the exact nvidia-smi argv the task
    requires and never fabricates a reading when the command is unavailable;
  * the parser accepts a well-formed result and RAISES on every failure mode
    (non-physical bidirectional joint completion; a bidirectional joint
    faster than the fastest theoretical bound; a stream-split latency faster
    than any plausible PCIe link; a stream-split chunk_bytes that does not
    match its own copy_streams/object_bytes; a tampered pcie_link_environment
    argv) -- loud failure, never a silent skip;
  * deterministic_point_id is actually deterministic and actually sensitive
    to its inputs.
"""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from types import SimpleNamespace
from pathlib import Path

import pytest

from measurement.probes import pcie_extension_probe as probe
from measurement.probes import pcie_extension_backend as backend_mod
from measurement.probes.pcie_extension_backend import (
    MockPcieExtensionBackend, TorchPcieExtensionBackend, gap_label_of,
    capture_pcie_link_info, PCIE_LINK_ARGV,
)
from measurement.probes.mock_backend import BackendError
from measurement.parsers import ValidationError
from measurement.parsers import pcie_extension_parser as parser

FIXTURES = Path(__file__).parent / "fixtures" / "gpu_prep"


# --------------------------------------------------------------------------- #
# Probe CPU smoke test
# --------------------------------------------------------------------------- #

def test_pcie_extension_probe_cpu_smoke(tmp_path):
    out = tmp_path / "pcie_ext.json"
    rc = probe.main([
        "--backend", "mock_pcie_extension", "--out", str(out),
        "--bidirectional-object-bytes", "65536,2097152,352321536",
        "--stream-split-bytes", "4096,2097152,3145728,5242880,8388608,13631488",
        "--copy-streams", "1,2,4", "--direction", "h2d,d2h", "--repeats", "5",
    ])
    assert rc == 0
    result = json.loads(out.read_text())
    assert result["evidence"] == "cpu_smoke_test_not_measurement"
    assert result["axis"] == "all"
    assert result["ir_evaluation_point_fields"] == "PENDING_PCIE_EXTENSION_SEMANTICS"
    assert result["production_stream_semantics_status"] == "UNSUPPORTED_UNTIL_MEASURED"
    # 3 bidirectional-axis sizes
    assert len(result["bidirectional_cells"]) == 3
    # 2 directions x 6 stream-split sizes x 3 copy_streams values
    assert len(result["stream_split_cells"]) == 36
    assert result["pcie_link_environment"]["argv"] == PCIE_LINK_ARGV
    assert result["pcie_link_environment"]["status"] in ("ok", "unavailable")
    parser.validate(result)


def test_pcie_extension_probe_axis_selection(tmp_path):
    out = tmp_path / "bidir_only.json"
    probe.main(["--backend", "mock_pcie_extension", "--axis", "bidirectional",
                "--out", str(out)])
    result = json.loads(out.read_text())
    assert result["axis"] == "bidirectional"
    assert len(result["bidirectional_cells"]) == 3
    assert result["stream_split_cells"] == []
    # pcie_link_environment is always captured, regardless of --axis.
    assert result["pcie_link_environment"]["argv"] == PCIE_LINK_ARGV

    out2 = tmp_path / "stream_split_only.json"
    probe.main(["--backend", "mock_pcie_extension", "--axis", "stream_split",
                "--out", str(out2)])
    result2 = json.loads(out2.read_text())
    assert result2["bidirectional_cells"] == []
    assert len(result2["stream_split_cells"]) > 0

    out3 = tmp_path / "pcie_link_only.json"
    probe.main(["--backend", "mock_pcie_extension", "--axis", "pcie_link",
                "--out", str(out3)])
    result3 = json.loads(out3.read_text())
    assert result3["bidirectional_cells"] == []
    assert result3["stream_split_cells"] == []
    assert result3["pcie_link_environment"]["argv"] == PCIE_LINK_ARGV
    parser.validate(result3)


def test_pcie_extension_probe_envelope_holds(tmp_path):
    out = tmp_path / "pcie_ext.json"
    probe.main(["--backend", "mock_pcie_extension", "--out", str(out),
                "--bidirectional-object-bytes", "65536,352321536",
                "--stream-split-bytes", "4096,2097152,13631488",
                "--copy-streams", "1,2,4", "--direction", "h2d,d2h",
                "--repeats", "5"])
    result = json.loads(out.read_text())
    for c in result["bidirectional_cells"]:
        joint = c["joint_completion_ms_mean"]
        assert max(c["h2d_ms_mean"], c["d2h_ms_mean"]) <= joint + 1e-9
        assert joint <= c["h2d_ms_mean"] + c["d2h_ms_mean"] + 1e-9
        assert joint >= max(c["h2d_baseline_ms_mean"], c["d2h_baseline_ms_mean"]) - 1e-9
    for c in result["stream_split_cells"]:
        assert c["latency_ms_mean"] > 0
        assert c["chunk_bytes"] * c["copy_streams"] >= c["object_bytes"]
    parser.validate(result)


def test_pcie_extension_probe_refuses_gpu_backend_without_cuda(tmp_path, monkeypatch):
    fake_torch = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False))
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    with pytest.raises(BackendError):
        probe.run(probe.parse_args(
            ["--backend", "gpu", "--out", str(tmp_path / "x.json")]))


def test_gpu_backend_name_and_registry():
    assert TorchPcieExtensionBackend.name == "gpu"
    assert backend_mod.resolve_backend("mock_pcie_extension") is MockPcieExtensionBackend
    assert backend_mod.resolve_backend("gpu") is TorchPcieExtensionBackend
    with pytest.raises(BackendError):
        backend_mod.resolve_backend("not_a_real_backend")


def test_mock_backend_rejects_bad_bidirectional_axes():
    b = MockPcieExtensionBackend()
    for bad in [
        dict(object_bytes=0, repeats=5),
        dict(object_bytes=4096, repeats=0),
    ]:
        with pytest.raises(BackendError):
            b.measure_bidirectional_cell(**bad)


def test_mock_backend_rejects_bad_stream_split_axes():
    b = MockPcieExtensionBackend()
    for bad in [
        dict(object_bytes=0, copy_streams=1, direction="h2d", repeats=5),
        dict(object_bytes=4096, copy_streams=0, direction="h2d", repeats=5),
        dict(object_bytes=4096, copy_streams=1, direction="p2p", repeats=5),
        dict(object_bytes=4096, copy_streams=1, direction="h2d", repeats=0),
    ]:
        with pytest.raises(BackendError):
            b.measure_stream_split_cell(**bad)


# --------------------------------------------------------------------------- #
# gap_label_of: provenance labeling for the shared stream-split primitive
# --------------------------------------------------------------------------- #

def test_gap_label_of_declared_small_cell():
    assert gap_label_of(4096) == "declared_never_measured_4096"


def test_gap_label_of_crossover_interior_points():
    for b in (2097152, 3145728, 5242880, 8388608, 13631488):
        assert gap_label_of(b) == "crossover_densification"


def test_gap_label_of_frozen_grid_and_other_points_are_custom():
    # The frozen harness's own boundary points are NOT interior densification
    # points (strict inequality), and anything outside the band is unlabeled.
    for b in (65536, 1048576, 22020096, 44040192, 88080384):
        assert gap_label_of(b) == "custom_sweep_point"


# --------------------------------------------------------------------------- #
# deterministic_point_id: deterministic AND input-sensitive
# --------------------------------------------------------------------------- #

def test_deterministic_point_id_is_stable_and_input_sensitive():
    a = probe.deterministic_point_id("bidir", "gpu", 65536)
    b = probe.deterministic_point_id("bidir", "gpu", 65536)
    c = probe.deterministic_point_id("bidir", "gpu", 2097152)
    d = probe.deterministic_point_id("streamsplit", "gpu", 65536)
    assert a == b
    assert a != c
    assert a != d
    assert a.startswith("bidir-")
    assert d.startswith("streamsplit-")


# --------------------------------------------------------------------------- #
# axis 4: nvidia-smi PCIe-link argv capture (mockable subprocess boundary)
# --------------------------------------------------------------------------- #

class _RecordingRunner:
    """Injectable command_runner: records every argv it is called with."""

    def __init__(self, result=None, exc=None):
        self.calls: list[list[str]] = []
        self._result = result
        self._exc = exc

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if self._exc is not None:
            raise self._exc
        return self._result


def _completed(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_pcie_link_argv_matches_the_required_command():
    # Pinned independently of PCIE_LINK_ARGV's own construction, so a typo in
    # that constant would still be caught here.
    assert PCIE_LINK_ARGV == [
        "nvidia-smi",
        "--query-gpu=pcie.link.gen.current,pcie.link.gen.max,"
        "pcie.link.width.current,pcie.link.width.max",
        "--format=csv,noheader",
    ]


def test_capture_pcie_link_info_issues_exact_argv_and_parses_ok():
    runner = _RecordingRunner(result=_completed(stdout="4, 5, 16, 16\n"))
    info = capture_pcie_link_info(command_runner=runner)
    assert runner.calls == [PCIE_LINK_ARGV]
    assert info["argv"] == PCIE_LINK_ARGV
    assert info["status"] == "ok"
    assert info["pcie_link_gen_current"] == "4"
    assert info["pcie_link_gen_max"] == "5"
    assert info["pcie_link_width_current"] == "16"
    assert info["pcie_link_width_max"] == "16"


def test_capture_pcie_link_info_handles_missing_binary_without_fabricating():
    runner = _RecordingRunner(exc=FileNotFoundError("nvidia-smi not found"))
    info = capture_pcie_link_info(command_runner=runner)
    assert runner.calls == [PCIE_LINK_ARGV]
    assert info["status"] == "unavailable"
    assert "reason" in info and info["reason"]
    assert "pcie_link_gen_current" not in info


def test_capture_pcie_link_info_handles_nonzero_returncode():
    runner = _RecordingRunner(result=_completed(returncode=1, stderr="no devices"))
    info = capture_pcie_link_info(command_runner=runner)
    assert info["status"] == "unavailable"
    assert "no devices" in info["reason"]


def test_capture_pcie_link_info_handles_malformed_row():
    runner = _RecordingRunner(result=_completed(stdout="4, 5\n"))  # only 2 fields
    info = capture_pcie_link_info(command_runner=runner)
    assert info["status"] == "unavailable"
    assert "unexpected nvidia-smi row" in info["reason"]


def test_capture_pcie_link_info_real_default_runs_on_this_host(tmp_path):
    """Exercises the REAL subprocess.run default (no injected runner) -- the
    mock/CPU backend path this function is called from unconditionally, with
    no GPU or torch required. Must not raise regardless of whether nvidia-smi
    is present; status must be "ok" or "unavailable", never fabricated.
    """
    info = capture_pcie_link_info()
    assert info["argv"] == PCIE_LINK_ARGV
    assert info["status"] in ("ok", "unavailable")


# --------------------------------------------------------------------------- #
# Fake-CUDA clock harness (same technique as tests/test_gpu_prep_v2gapa.py):
# each Stream gets a fixed per-copy transfer_ms by creation order; Event
# timestamps are stream clock snapshots; elapsed_time is a plain difference.
# Lets the coordinator wait-then-record-distinct-event discipline be checked
# without any real GPU.
# --------------------------------------------------------------------------- #

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

    def wait_stream(self, other):
        self.clock = max(self.clock, other.clock)


class _FakeBuffer:
    def __init__(self, cuda):
        self.cuda = cuda

    def copy_(self, _source, *, non_blocking):
        assert non_blocking is True
        stream = self.cuda.active_stream
        stream.clock += stream.transfer_ms

    def __getitem__(self, _slice):
        # The fake has no real memory/regions; slicing a chunk out for a
        # stream-split copy is equivalent, for this clock model, to copying
        # the whole (fake) buffer on that stream.
        return self


class _FakeCuda:
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


def test_gpu_backend_bidirectional_waits_on_both_legs_before_joint_event():
    """Verifies the P-022-style discipline directly: joint_completion_ms must
    equal the SLOWER of the two concurrently-issued legs, not merely the
    coordinator's own dispatch delay. Streams are created in this order
    inside measure_bidirectional_cell: stream_h2d (transfer_ms=1.0),
    stream_d2h (transfer_ms=2.0), baseline_stream (transfer_ms=3.0) -- fixed
    by the fake harness's creation-order-indexed transfer_ms.
    """
    fake_torch = _FakeTorch()
    backend = TorchPcieExtensionBackend(_torch=fake_torch, warmup_repeats=0)

    cell = backend.measure_bidirectional_cell(object_bytes=65536, repeats=1)

    assert cell["object_bytes"] == 65536
    assert cell["h2d_ms_mean"] == 1.0
    assert cell["d2h_ms_mean"] == 2.0
    # joint must track the SLOWER leg (2.0), never the faster leg and never
    # some smaller coordinator-only dispatch value -- this is exactly what a
    # "recorded the joint event before waiting on both legs" bug would break.
    assert cell["joint_completion_ms_mean"] == 2.0
    assert cell["h2d_baseline_ms_mean"] == 3.0
    assert cell["d2h_baseline_ms_mean"] == 3.0
    assert cell["joint_over_max_ratio"] == pytest.approx(2.0 / 3.0)
    assert cell["joint_over_sum_ratio"] == pytest.approx(2.0 / 6.0)
    # 1 allocation barrier + 3 measurement calls (h2d baseline, d2h baseline,
    # bidirectional) each ending in one synchronize().
    assert fake_torch.cuda.synchronize_calls == 4


def test_gpu_backend_stream_split_waits_on_every_chunk_stream():
    """copy_streams=2 with per-stream transfer_ms [1.0, 2.0] (creation
    order): the coordinator must wait for BOTH chunk streams, so latency
    tracks the slower stream (2.0), not the faster one issued first.
    """
    fake_torch = _FakeTorch()
    backend = TorchPcieExtensionBackend(_torch=fake_torch, warmup_repeats=0)

    cell = backend.measure_stream_split_cell(
        object_bytes=4096, copy_streams=2, direction="h2d", repeats=1)

    assert cell["chunk_bytes"] == 2048
    assert cell["copy_streams"] == 2
    assert cell["latency_ms_mean"] == 2.0
    assert cell["gap_label"] == "declared_never_measured_4096"
    # 1 allocation barrier + 1 measurement call.
    assert fake_torch.cuda.synchronize_calls == 2


def test_gpu_backend_stream_split_single_stream_matches_its_own_transfer():
    fake_torch = _FakeTorch()
    backend = TorchPcieExtensionBackend(_torch=fake_torch, warmup_repeats=0)

    cell = backend.measure_stream_split_cell(
        object_bytes=2097152, copy_streams=1, direction="d2h", repeats=1)

    assert cell["chunk_bytes"] == 2097152
    assert cell["latency_ms_mean"] == 1.0  # only stream created -> transfer_ms=1.0


# --------------------------------------------------------------------------- #
# Parser fixtures: normal accepted, every failure RAISES
# --------------------------------------------------------------------------- #

def test_pcie_extension_parser_pass_fixture():
    ok = json.loads((FIXTURES / "pcie_extension_pass.json").read_text())
    root = parser.validate(ok)
    assert root["axis"] == "all"
    assert len(root["bidirectional_cells"]) == 2
    assert len(root["stream_split_cells"]) == 3


def test_pcie_extension_parser_rejects_nonphysical_bidirectional():
    bad = json.loads(
        (FIXTURES / "pcie_extension_fail_nonphysical_bidirectional.json").read_text())
    with pytest.raises(ValidationError, match="non-physical"):
        parser.validate(bad)


def test_pcie_extension_parser_rejects_impossible_bandwidth():
    bad = json.loads(
        (FIXTURES / "pcie_extension_fail_impossible_bandwidth.json").read_text())
    with pytest.raises(ValidationError, match="plausible PCIe link"):
        parser.validate(bad)


def test_pcie_extension_parser_rejects_joint_below_baseline_floor():
    ok = json.loads((FIXTURES / "pcie_extension_pass.json").read_text())
    cell = ok["bidirectional_cells"][0]
    # Force joint below max(baselines) while keeping the *concurrent* legs'
    # own envelope satisfied, to isolate the baseline-floor check.
    cell["h2d_ms_mean"] = 0.001
    cell["h2d_ms_samples"] = [0.001] * 5
    cell["d2h_ms_mean"] = 0.001
    cell["d2h_ms_samples"] = [0.001] * 5
    cell["joint_completion_ms_mean"] = 0.001
    cell["joint_completion_ms_samples"] = [0.001] * 5
    with pytest.raises(ValidationError, match="fastest theoretical bound"):
        parser.validate(ok)


def test_pcie_extension_parser_rejects_bad_chunk_accounting():
    ok = json.loads((FIXTURES / "pcie_extension_pass.json").read_text())
    ok["stream_split_cells"][0]["chunk_bytes"] = 999999
    with pytest.raises(ValidationError, match="chunk_bytes"):
        parser.validate(ok)


def test_pcie_extension_parser_rejects_tampered_pcie_link_argv():
    ok = json.loads((FIXTURES / "pcie_extension_pass.json").read_text())
    ok["pcie_link_environment"]["argv"] = ["nvidia-smi", "--query-gpu=name"]
    with pytest.raises(ValidationError, match="argv"):
        parser.validate(ok)


def test_pcie_extension_parser_rejects_ok_status_missing_fields():
    ok = json.loads((FIXTURES / "pcie_extension_pass.json").read_text())
    del ok["pcie_link_environment"]["pcie_link_gen_max"]
    with pytest.raises(ValidationError, match="pcie_link_gen_max"):
        parser.validate(ok)


def test_pcie_extension_parser_rejects_wrong_axis_label():
    ok = json.loads((FIXTURES / "pcie_extension_pass.json").read_text())
    ok["axis"] = "not_a_real_axis"
    with pytest.raises(ValidationError, match="axis"):
        parser.validate(ok)


def test_pcie_extension_parser_rejects_sample_count_mismatch():
    ok = json.loads((FIXTURES / "pcie_extension_pass.json").read_text())
    ok["stream_split_cells"][0]["latency_ms_samples"] = [0.006]  # != repeats(5)
    with pytest.raises(ValidationError, match="latency_ms_samples"):
        parser.validate(ok)


def test_pcie_extension_parser_rejects_unknown_gap_label():
    ok = json.loads((FIXTURES / "pcie_extension_pass.json").read_text())
    ok["stream_split_cells"][0]["gap_label"] = "made_up_label"
    with pytest.raises(ValidationError, match="gap_label"):
        parser.validate(ok)

"""Unit tests for the S1 canonical converter and validators."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from edgeflow import canonical as C  # noqa: E402
from edgeflow import validate as V  # noqa: E402

FIXTURE = ROOT / "data/fixtures/moe_switch8_mbpp_s64_bs8_len64"

# A tiny hand-crafted deterministic input with a known expected canonical form.
TINY_CSV = """batch_id,dataset_name,stage,router_layer_index,router_module_name,expert_id,assigned_tokens,total_tokens,load_fraction,expert_capacity,overflow_tokens
0,toy,encoder,1,enc.b1.router,0,3,4,0.75,4,0
0,toy,encoder,1,enc.b1.router,1,1,4,0.25,4,0
0,toy,decoder,1,dec.b1.router,0,2,4,0.5,4,0
0,toy,decoder,1,dec.b1.router,2,2,4,0.5,4,1
"""

TINY_META = {"model_name": "toy/switch", "dataset_name": "toy", "num_experts": 4}


def _write_tiny(tmp_path: Path) -> Path:
    p = tmp_path / "batch_expert_load_trace.csv"
    p.write_text(TINY_CSV, encoding="utf-8")
    return p


def test_tiny_conversion_deterministic(tmp_path):
    raw = _write_tiny(tmp_path)
    rows = C.load_batch_expert_load(raw)
    assert len(rows) == 4
    src = {"raw_file": str(raw), "raw_sha256": "x", "converter_version": C.CONVERTER_VERSION}
    events = C.to_canonical_events(rows, TINY_META, "toy", src)
    # 4 non-zero rows -> 4 events
    assert len(events) == 4
    # Two layer-groups: encoder layer1 (step 0), decoder layer1 (step 1)
    steps = {e["attributes"]["layer_step"] for e in events}
    assert steps == {0, 1}
    # Encoder group before decoder group (timestamp monotonic per batch)
    enc = [e for e in events if e["attributes"]["stage"] == "encoder"]
    dec = [e for e in events if e["attributes"]["stage"] == "decoder"]
    assert all(e["timestamp"] == 0.0 for e in enc)
    assert all(e["timestamp"] == 1.0 for e in dec)
    # Decoder events depend on encoder events (sequential DAG)
    enc_ids = {e["event_id"] for e in enc}
    assert all(set(d["dependencies"]) == enc_ids for d in dec)
    # Determinism: second run identical
    events2 = C.to_canonical_events(rows, TINY_META, "toy", src)
    assert events == events2


def test_tiny_characterization():
    raw_rows = [
        C.BatchExpertRow(0, "encoder", 1, "r", 0, 3, 4, 4, 0),
        C.BatchExpertRow(0, "encoder", 1, "r", 1, 1, 4, 4, 0),
        C.BatchExpertRow(0, "decoder", 1, "r", 0, 2, 4, 4, 0),
        C.BatchExpertRow(0, "decoder", 1, "r", 2, 2, 4, 4, 1),
    ]
    src = {"raw_file": "x", "raw_sha256": "x", "converter_version": C.CONVERTER_VERSION}
    events = C.to_canonical_events(raw_rows, TINY_META, "toy", src)
    char = C.characterize(events)
    assert char["num_events"] == 4
    assert char["num_layer_groups"] == 2
    assert char["num_experts"] == 4
    assert char["total_assigned_tokens"] == 8
    assert char["total_overflow_tokens"] == 1
    # encoder {0,1} vs decoder {0,2}: jaccard = |{0}|/|{0,1,2}| = 1/3
    assert char["consecutive_group_expert_jaccard_mean"] == pytest.approx(1 / 3)


def test_tiny_schema_and_ordering(tmp_path):
    raw = _write_tiny(tmp_path)
    rows = C.load_batch_expert_load(raw)
    src = {"raw_file": str(raw), "raw_sha256": "x", "converter_version": C.CONVERTER_VERSION}
    events = C.to_canonical_events(rows, TINY_META, "toy", src)
    n_valid, errors = V.validate_events(events)
    assert errors == []
    assert n_valid == len(events)
    assert V.validate_ordering(events) == []


def test_zero_load_excluded(tmp_path):
    csv_text = TINY_CSV + "0,toy,decoder,1,dec.b1.router,3,0,4,0.0,4,0\n"
    p = tmp_path / "z.csv"
    p.write_text(csv_text, encoding="utf-8")
    rows = C.load_batch_expert_load(p)
    src = {"raw_file": "x", "raw_sha256": "x", "converter_version": C.CONVERTER_VERSION}
    events = C.to_canonical_events(rows, TINY_META, "toy", src)
    # zero-load expert excluded by default
    assert len(events) == 4
    events_incl = C.to_canonical_events(rows, TINY_META, "toy", src, include_zero_load=True)
    assert len(events_incl) == 5


@pytest.mark.skipif(not FIXTURE.exists(), reason="committed fixture missing")
def test_committed_fixture_integrity():
    reg = json.loads((ROOT / "data/registry/sources.yaml").read_text(encoding="utf-8")) if False else None
    # Verify the fixture sha256 matches the registry-recorded value.
    recorded = "9833ed2e2ea00bba11163110d870da69982b9827a1160351965957388af87f95"
    actual = C.sha256_file(FIXTURE / "batch_expert_load_trace.csv")
    assert actual == recorded


@pytest.mark.skipif(not FIXTURE.exists(), reason="committed fixture missing")
def test_committed_fixture_conversion():
    meta = json.loads((FIXTURE / "run_metadata.json").read_text(encoding="utf-8"))
    rows = C.load_batch_expert_load(FIXTURE / "batch_expert_load_trace.csv")
    src = C.build_source_info(FIXTURE / "batch_expert_load_trace.csv", meta)
    events = C.to_canonical_events(rows, meta, "moe_mbpp_s64_bs8_len64", src)
    n_valid, errors = V.validate_events(events)
    assert errors == []
    assert n_valid == len(events) > 0
    assert V.validate_ordering(events) == []
    char = C.characterize(events)
    assert char["num_experts"] == 8
    assert char["num_batches"] == 8
    # Measured property: nearly all experts active per layer at this batch size.
    assert char["active_experts_per_group_mean"] > 7.0

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PHASE1_ROOT = Path(__file__).resolve().parents[1]
SIM_ROOT = PHASE1_ROOT.parent
sys.path.insert(0, str(SIM_ROOT / "tools"))
sys.path.insert(0, str(PHASE1_ROOT))

from run_spike import (  # noqa: E402
    SHAPE_EVENT,
    canonicalize,
    require_unique_ids,
    run,
    write_checksum_ledger,
)
from validate_phase0 import ValidationFailure  # noqa: E402
from validate_phase1 import (  # noqa: E402
    EXPECTED_RUN_FILES,
    validate_ledger,
    validate_run,
)

FIXTURE = PHASE1_ROOT / "fixtures" / "mock_runtime_trace.json"


def write_fixture(path: Path, value: dict) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def test_fixture_covers_required_shapes_and_independent_ranks() -> None:
    hashes, alignments, events, routing = canonicalize(FIXTURE)
    assert len(events) == 10
    assert len(alignments) == 4
    assert len(routing) == 1
    assert {item["source_rank"] for item in alignments} >= {0, 1}
    assert {event["attributes"]["mock_shape"] for event in events} == set(
        SHAPE_EVENT
    )
    assert len(hashes["event_file_semantic_rows"]) == 10


def test_canonicalization_is_deterministic() -> None:
    first = canonicalize(FIXTURE)
    second = canonicalize(FIXTURE)
    assert first == second


def test_unknown_source_clock_fails_closed(tmp_path: Path) -> None:
    value = json.loads(FIXTURE.read_text(encoding="utf-8"))
    value["raw_events"][0]["source_clock_id"] = "unknown-clock"
    fixture = tmp_path / "unknown.json"
    write_fixture(fixture, value)
    with pytest.raises(ValidationFailure):
        canonicalize(fixture)


def test_causal_time_regression_fails_closed(tmp_path: Path) -> None:
    value = json.loads(FIXTURE.read_text(encoding="utf-8"))
    value["raw_events"][7]["source_timestamp"] = "1"
    fixture = tmp_path / "regression.json"
    write_fixture(fixture, value)
    with pytest.raises(ValueError):
        canonicalize(fixture)


def test_duplicate_json_key_fails_closed(tmp_path: Path) -> None:
    duplicate = FIXTURE.read_text(encoding="utf-8").replace(
        '"fixture_id": "phase1-multiclock-multirank-runtime-shape",',
        '"fixture_id": "a", "fixture_id": "b",',
        1,
    )
    fixture = tmp_path / "duplicate.json"
    fixture.write_text(duplicate, encoding="utf-8")
    with pytest.raises(ValidationFailure):
        canonicalize(fixture)


def test_float_input_fails_closed(tmp_path: Path) -> None:
    value = json.loads(FIXTURE.read_text(encoding="utf-8"))
    value["raw_events"][0]["attributes"]["invalid_float"] = 0.5
    fixture = tmp_path / "float.json"
    write_fixture(fixture, value)
    with pytest.raises(ValidationFailure):
        canonicalize(fixture)


def test_fresh_run_replay_and_existing_directory_rejection(
    tmp_path: Path,
) -> None:
    output = tmp_path / "phase1-run"
    run(FIXTURE, output, ["python3", "run_spike.py"])
    validate_run(output, skip_ledger=True)
    with pytest.raises(RuntimeError):
        run(FIXTURE, output, ["python3", "run_spike.py"])


def test_tampered_run_fails_ledger(tmp_path: Path) -> None:
    output = tmp_path / "phase1-run"
    run(FIXTURE, output, ["python3", "run_spike.py"])
    metrics = json.loads((output / "metrics.json").read_text(encoding="utf-8"))
    metrics["gpu_used"] = True
    write_fixture(output / "metrics.json", metrics)
    with pytest.raises(ValidationFailure):
        validate_run(output)


def test_duplicate_source_clock_fails_before_output(tmp_path: Path) -> None:
    value = json.loads(FIXTURE.read_text(encoding="utf-8"))
    value["source_clocks"].append(copy.deepcopy(value["source_clocks"][0]))
    fixture = tmp_path / "duplicate-clock.json"
    write_fixture(fixture, value)
    output = tmp_path / "must-not-exist"
    with pytest.raises(ValidationFailure):
        run(fixture, output, ["python3", "run_spike.py"])
    assert not output.exists()


def test_event_rank_must_match_source_clock(tmp_path: Path) -> None:
    value = json.loads(FIXTURE.read_text(encoding="utf-8"))
    value["raw_events"][3]["rank"] = 1
    fixture = tmp_path / "rank-mismatch.json"
    write_fixture(fixture, value)
    with pytest.raises(ValidationFailure):
        canonicalize(fixture)


def test_duplicate_alignment_id_guard() -> None:
    with pytest.raises(ValidationFailure):
        require_unique_ids(
            [{"alignment_id": "same"}, {"alignment_id": "same"}],
            "alignment_id",
        )


def make_inventory_tree(root: Path) -> None:
    for relative in EXPECTED_RUN_FILES:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n", encoding="utf-8")
    write_checksum_ledger(root)


def test_inventory_rejects_coherently_reledgered_extra_file(
    tmp_path: Path,
) -> None:
    make_inventory_tree(tmp_path)
    (tmp_path / "extra.txt").write_text("extra\n", encoding="utf-8")
    write_checksum_ledger(tmp_path)
    with pytest.raises(ValidationFailure):
        validate_ledger(tmp_path)


def test_inventory_rejects_coherently_reledgered_missing_file(
    tmp_path: Path,
) -> None:
    make_inventory_tree(tmp_path)
    (tmp_path / "metrics.json").unlink()
    write_checksum_ledger(tmp_path)
    with pytest.raises(ValidationFailure):
        validate_ledger(tmp_path)


def test_inventory_rejects_symlink(tmp_path: Path) -> None:
    make_inventory_tree(tmp_path)
    target = tmp_path / "metrics.json"
    target.unlink()
    target.symlink_to("manifest.json")
    with pytest.raises((RuntimeError, ValidationFailure)):
        write_checksum_ledger(tmp_path)


def test_inventory_rejects_fifo(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO is unavailable on this platform")
    make_inventory_tree(tmp_path)
    target = tmp_path / "metrics.json"
    target.unlink()
    os.mkfifo(target)
    try:
        with pytest.raises((RuntimeError, ValidationFailure)):
            write_checksum_ledger(tmp_path)
    finally:
        target.unlink()


def test_dependency_preflight_failure_creates_no_run(tmp_path: Path) -> None:
    dependency_root = tmp_path / "empty-dependencies"
    dependency_root.mkdir()
    output = tmp_path / "must-not-exist"
    result = subprocess.run(
        [
            sys.executable,
            str(PHASE1_ROOT / "run_phase1_suite.py"),
            "--dependency-root",
            str(dependency_root),
            "--output",
            str(output),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert not output.exists()

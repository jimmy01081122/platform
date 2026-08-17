"""Smoke and policy tests for the temporary storage preflight."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "storage_preflight", ROOT / "scripts" / "storage_preflight.py"
)
assert SPEC and SPEC.loader
storage_preflight = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = storage_preflight
SPEC.loader.exec_module(storage_preflight)


@pytest.fixture
def tiny_config():
    return storage_preflight.BenchmarkConfig(
        sequential_mib=1,
        block_kib=64,
        random_ops=8,
        random_block_kib=4,
        metadata_files=8,
        parser_rows=40,
    )


def test_target_smoke_cleans_temporary_files(tmp_path, tiny_config):
    target = tmp_path / "target"
    target.mkdir()

    result = storage_preflight.benchmark_target("test", target, tiny_config)

    assert result["temporary_path_cleaned"] is True
    assert list(target.iterdir()) == []
    assert result["metrics"]["sequential"]["read_mib_s"] > 0
    assert result["metrics"]["metadata"]["stat_ops_s"] > 0
    assert result["metrics"]["random_io"]["read_iops"] > 0
    assert result["metrics"]["dataset_parser"]["rows"] == tiny_config.parser_rows


def test_mountinfo_octal_escapes_are_decoded():
    assert storage_preflight.decode_mount_field(r"E:\134data\040set") == "E:\\data set"


@pytest.mark.parametrize(
    ("ratios", "expected"),
    [
        (
            {
                "sequential_read": 0.90,
                "dataset_parser": 0.80,
                "metadata_create": 0.60,
                "metadata_stat": 0.70,
                "random_read": 0.55,
                "random_write": 0.65,
            },
            "read_directly",
        ),
        (
            {
                "sequential_read": 0.70,
                "dataset_parser": 0.65,
                "metadata_create": 0.20,
                "metadata_stat": 0.30,
                "random_read": 0.25,
                "random_write": 0.35,
            },
            "stream_and_cache",
        ),
        (
            {
                "sequential_read": 0.45,
                "dataset_parser": 0.40,
                "metadata_create": 0.20,
                "metadata_stat": 0.30,
                "random_read": 0.25,
                "random_write": 0.35,
            },
            "copy_active_subset",
        ),
    ],
)
def test_decision_thresholds(ratios, expected):
    assert storage_preflight.decide(ratios)["key"] == expected

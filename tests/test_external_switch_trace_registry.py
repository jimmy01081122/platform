from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/audit_external_switch_traces.py"
REGISTRY = ROOT / "data/registry/switch_colab_trace_readonly_v1.yaml"
SCHEMA = ROOT / "schemas/external_trace_registry.schema.json"
EXTERNAL_ROOT = Path("/home/a/prototype/trace_data")
EXTERNAL_INVENTORY = Path("/home/a/prototype/data/manifests/TRACE_INVENTORY.csv")

SPEC = importlib.util.spec_from_file_location("audit_external_switch_traces", SCRIPT)
assert SPEC and SPEC.loader
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


def test_registry_schema_and_required_semantics():
    registry = AUDIT.load_registry(REGISTRY)
    assert AUDIT.validate_registry_schema(registry, SCHEMA) == []
    assert registry["complete"] is False
    assert registry["read_only"] is True
    assert registry["source"]["access"] == "read_only"
    assert registry["inventory"]["file_count"] == 1527
    assert registry["inventory"]["total_bytes"] == 11159735788
    assert {item["num_experts"] for item in registry["corpus"]["models"]} == {8, 16, 32}
    assert set(registry["candidate_mapping"]) == {"T1", "T3", "T6"}
    assert "runner-up" in registry["semantics"]["runner_up"]
    assert "not autoregressive" in registry["semantics"]["teacher_forced_decode"]
    assert "no measured timestamps" in registry["semantics"]["hardware_event"]
    assert len(registry["missing_provenance_fields"]) >= 20


def test_content_set_hash_is_order_independent():
    rows = [
        {"relative_path": "b", "bytes": "2", "sha256": "b" * 64},
        {"relative_path": "a", "bytes": "1", "sha256": "a" * 64},
    ]
    forward = AUDIT.content_set_sha256(rows)
    reverse = AUDIT.content_set_sha256(reversed(rows))
    assert forward == reverse
    expected = hashlib.sha256(
        b"a\0" + b"1\0" + b"a" * 64 + b"\n"
        + b"b\0" + b"2\0" + b"b" * 64 + b"\n"
    ).hexdigest()
    assert forward == expected


def test_quick_and_full_file_audit_are_read_only(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    payload = source / "trace.csv"
    payload.write_bytes(b"x,y\n1,2\n")
    before = (payload.read_bytes(), payload.stat().st_mtime_ns)
    rows = [{
        "relative_path": "trace.csv",
        "bytes": str(payload.stat().st_size),
        "sha256": hashlib.sha256(payload.read_bytes()).hexdigest(),
    }]

    quick_bytes, quick_hashes, quick_errors = AUDIT.audit_files(source, rows, "quick")
    full_bytes, full_hashes, full_errors = AUDIT.audit_files(source, rows, "full")

    assert (quick_bytes, quick_hashes, quick_errors) == (1, 0, [])
    assert (full_bytes, full_hashes, full_errors) == (1, 1, [])
    assert (payload.read_bytes(), payload.stat().st_mtime_ns) == before


def test_schema_relation_rejects_missing_column():
    observed = '[{"name":"batch_id","observed_types":["int"]}]'
    rows = [{
        "relative_path": "run/router_token_trace.csv",
        "kind": "event_trace",
        "observed_schema": observed,
    }]
    trace_types = [{
        "name": "router_token",
        "file_name": "router_token_trace.csv",
        "schema_relation": {
            "inventory_kind": "event_trace",
            "required_columns": ["batch_id", "top1_expert"],
        },
    }]
    counts, errors = AUDIT.audit_schema_relations(rows, trace_types)
    assert counts == {"router_token": 1}
    assert len(errors) == 1
    assert "top1_expert" in errors[0]


def test_cli_accepts_root_and_inventory_overrides(tmp_path):
    root = tmp_path / "traces"
    inventory = tmp_path / "inventory.csv"
    args = AUDIT.parse_args([
        "--root", str(root),
        "--inventory", str(inventory),
        "--mode", "full",
    ])
    assert args.root == root
    assert args.inventory == inventory
    assert args.mode == "full"


@pytest.mark.skipif(
    not EXTERNAL_ROOT.is_dir() or not EXTERNAL_INVENTORY.is_file(),
    reason="external read-only Switch corpus is unavailable",
)
def test_real_external_root_quick_audit():
    result = AUDIT.run_audit(
        REGISTRY, SCHEMA, EXTERNAL_ROOT, EXTERNAL_INVENTORY, "quick"
    )
    assert result["status"] == "pass", result["errors"]
    assert result["files_stat_checked"] == 1527
    assert result["files_sha256_checked"] == 0
    assert result["corpus"]["run_count"] == 168

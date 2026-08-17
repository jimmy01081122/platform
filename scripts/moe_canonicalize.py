#!/usr/bin/env python3
"""Convert downloaded raw HF MoE traces -> canonical moe-routing-v1 JSONL.

Reads the download registry (data/registry/hf_downloads.json), converts each
raw query file into a per-query JSONL of moe-routing-v1 records, validates a
sample of records against schemas/moe_routing.schema.json, and writes a manifest
(committed) with per-query dims + sha256 of the canonical output.

The per-query JSONL is large and git-ignored; it is deterministically regenerated
from raw. The manifest and downstream stats are the committed artifacts.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from edgeflow.moe_routing import (  # noqa: E402
    ROUTING_SCHEMA_VERSION, CONVERTER_VERSION, infer_dims, to_canonical_records, sha256_file,
)

REGISTRY = ROOT / "data" / "registry" / "hf_downloads.json"
RAW_ROOT = ROOT / "data" / "raw"
OUT_ROOT = ROOT / "data" / "canonical" / "moe_routing_v1"


def _validate_record(rec: dict, schema: dict) -> list[str]:
    """Lightweight structural validation (no external deps)."""
    errs: list[str] = []
    for k in schema["required"]:
        if k not in rec:
            errs.append(f"missing {k}")
    if rec.get("schema_version") != ROUTING_SCHEMA_VERSION:
        errs.append("bad schema_version")
    if rec.get("phase") not in ("prefill", "decode"):
        errs.append("bad phase")
    se = rec.get("selected_experts")
    if not isinstance(se, list) or (se and not isinstance(se[0], list)):
        errs.append("selected_experts not 2D")
    if rec.get("num_tokens") != len(se):
        errs.append("num_tokens != len(selected_experts)")
    return errs


def main() -> int:
    reg = json.loads(REGISTRY.read_text())
    schema = json.loads((ROOT / "schemas" / "moe_routing.schema.json").read_text())
    dataset = reg["dataset"]
    src_rev = reg["source_revision"]
    manifest = {
        "schema_version": ROUTING_SCHEMA_VERSION,
        "converter_version": CONVERTER_VERSION,
        "dataset": dataset,
        "source_revision": src_rev,
        "queries": [],
    }
    total_records = 0
    total_errs = 0
    for entry in reg["files"]:
        raw = RAW_ROOT / entry["path"]
        if not raw.exists():
            print(f"WARN missing raw {raw}", file=sys.stderr)
            continue
        dims = infer_dims(raw)
        source_meta = {
            "dataset": dataset,
            "model": entry["variant"],
            "benchmark": entry["benchmark"],
            "subject": entry["subject"],
            "query_id": entry["query_id"],
            "source_revision": src_rev,
        }
        rel = Path(entry["path"]).with_suffix(".jsonl")
        out = OUT_ROOT / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        n = 0
        sample_errs: list[str] = []
        with open(out, "w") as f:
            for rec in to_canonical_records(raw, source_meta, num_experts=dims["num_experts_observed"]):
                if n < 50:
                    sample_errs += _validate_record(rec, schema)
                f.write(json.dumps(rec) + "\n")
                n += 1
        total_records += n
        total_errs += len(sample_errs)
        manifest["queries"].append({
            "model": entry["variant"],
            "benchmark": entry["benchmark"],
            "subject": entry["subject"],
            "query_id": entry["query_id"],
            "raw_sha256": entry["sha256"],
            "canonical_path": str(rel),
            "canonical_sha256": sha256_file(out),
            "records": n,
            "dims": dims,
            "validation_errors": len(sample_errs),
        })
        print(f"  {entry['variant']}/{entry['benchmark']}/{entry['subject']}/{entry['query_id']}: "
              f"{n} records, layers={dims['num_layers']}, top_k={dims['top_k']}, "
              f"experts={dims['num_experts_observed']}, steps={dims['num_steps']}, errs={len(sample_errs)}")

    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    (OUT_ROOT / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    print(f"\nqueries={len(manifest['queries'])} total_records={total_records} sample_validation_errors={total_errs}")
    print(f"manifest: {OUT_ROOT / 'manifest.json'}")
    return 1 if total_errs else 0


if __name__ == "__main__":
    raise SystemExit(main())

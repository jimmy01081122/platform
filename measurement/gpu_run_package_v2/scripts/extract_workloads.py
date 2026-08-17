#!/usr/bin/env python3
"""Extract deterministic representative MoE windows from canonical JSONL traces."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def load_config(path: Path) -> dict:
    try:
        import yaml
    except ImportError as exc:
        raise SystemExit("PyYAML is required; install requirements.lock") from exc
    return yaml.safe_load(path.read_text())


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def extract(trace: Path, window_steps: int) -> dict:
    rows: list[dict] = []
    prefill_row: dict | None = None
    first_decode_layer: int | None = None
    with trace.open() as f:
        for line_no, line in enumerate(f, 1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"invalid JSONL {trace}:{line_no}: {exc}") from exc
            required = {"phase", "step_index", "layer_id", "num_tokens", "top_k",
                        "num_experts", "selected_experts"}
            missing = required - row.keys()
            if missing:
                raise SystemExit(f"{trace}:{line_no} missing {sorted(missing)}")
            if row["phase"] == "prefill" and prefill_row is None:
                prefill_row = row
            if row["phase"] == "decode":
                if first_decode_layer is None:
                    first_decode_layer = int(row["layer_id"])
                if int(row["layer_id"]) == first_decode_layer:
                    rows.append(row)
            if len(rows) >= window_steps:
                break
    if len(rows) < window_steps:
        raise SystemExit(
            f"{trace}: need {window_steps} decode layer-0 records, found {len(rows)}"
        )
    if prefill_row is None:
        raise SystemExit(f"{trace}: no prefill record found")
    return {
        "num_experts": rows[0]["num_experts"],
        "top_k": rows[0]["top_k"],
        "layer_id": rows[0]["layer_id"],
        "prefill_step": {
            "step_index": prefill_row["step_index"],
            "layer_id": prefill_row["layer_id"],
            "num_tokens": prefill_row["num_tokens"],
            "selected_experts": prefill_row["selected_experts"],
        },
        "window_steps": len(rows),
        "steps": [
            {
                "step_index": r["step_index"],
                "num_tokens": r["num_tokens"],
                "selected_experts": r["selected_experts"],
            }
            for r in rows
        ],
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--splits", type=Path, default=Path("configs/splits.yaml"))
    p.add_argument("--source-root", type=Path)
    p.add_argument("--output", type=Path, default=Path("workloads/windows.json"))
    p.add_argument(
        "--window-steps",
        type=int,
        default=None,
        help="override all split window sizes; otherwise use each split's window_steps",
    )
    args = p.parse_args()
    cfg = load_config(args.splits)
    package = args.splits.resolve().parents[1]
    source = args.source_root or (package / cfg["source_root"]).resolve()
    manifest = (package / cfg["source_manifest"]).resolve()
    if not manifest.is_file():
        raise SystemExit(f"canonical manifest missing: {manifest}")
    index = {q["canonical_path"]: q for q in json.loads(manifest.read_text())["queries"]}
    output = {"schema_version": "moe-window-workload-v1",
              "source_manifest": cfg["source_manifest"],
              "source_manifest_sha256": sha256(manifest),
              "source_revision": json.loads(manifest.read_text())["source_revision"],
              "model": cfg["model"],
              "splits": {}}
    for split in ("calibration", "validation", "holdout"):
        output["splits"][split] = []
        window_steps = args.window_steps or int(cfg[split]["window_steps"])
        for selector in cfg[split]["queries"]:
            query_id = str(selector["query_id"])
            benchmark = selector["benchmark"]
            subject = selector["subject"]
            rel = f'{cfg["model"]}/{benchmark}/{subject}/{query_id}.jsonl'
            path = source / rel
            if not path.is_file() or rel not in index:
                raise SystemExit(f"required canonical trace missing/unregistered: {path}")
            actual = sha256(path)
            expected = index[rel]["canonical_sha256"]
            if actual != expected:
                raise SystemExit(f"canonical checksum mismatch: {path}")
            window = extract(path, window_steps)
            window.update({"workload_id": f"{split}-{benchmark}-{subject}-q{query_id}",
                           "benchmark": benchmark, "subject": subject, "query_id": query_id,
                           "canonical_path": rel, "canonical_sha256": actual})
            output["splits"][split].append(window)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2) + "\n")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Stratified, incremental, provenance-tracked downloader for the HF MoE trace.

Policy (data/registry/sources.yaml):
  * Full download of the remote trace is FORBIDDEN.
  * Only a small stratified subset is fetched into data/raw/ under a byte cap.
  * Every fetched file is recorded with sha256 + source_revision in the registry.

Token handling (never printed):
  Resolved from, in order: $HF_TOKEN, $HUGGINGFACE_TOKEN, ~/.cache/huggingface/token.
  Only sent as a Bearer header to huggingface.co.

Usage:
  python scripts/hf_sample_download.py --config configs/sampling/round1.json
  python scripts/hf_sample_download.py --config configs/sampling/round1.json --dry-run
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = "https://huggingface.co/api/datasets/{id}/tree/main/{path}"
RESOLVE = "https://huggingface.co/datasets/{id}/resolve/main/{path}"
REGISTRY = ROOT / "data" / "registry" / "hf_downloads.json"
RAW_ROOT = ROOT / "data" / "raw"


def get_token() -> str:
    for env in ("HF_TOKEN", "HUGGINGFACE_TOKEN", "HUGGING_FACE_HUB_TOKEN"):
        v = os.environ.get(env)
        if v:
            return v.strip()
    cache = Path.home() / ".cache" / "huggingface" / "token"
    if cache.exists():
        return cache.read_text().strip()
    return ""


def _req(url: str, token: str) -> urllib.request.Request:
    r = urllib.request.Request(url)
    if token:
        r.add_header("Authorization", f"Bearer {token}")
    r.add_header("User-Agent", "edgehetero-sampler/1.0")
    return r


def list_dir(dataset: str, path: str, token: str) -> list[dict]:
    """List entries in a dataset directory via the HF tree API (paginated)."""
    out: list[dict] = []
    cursor = ""
    base = API.format(id=dataset, path=urllib.parse.quote(path))
    while True:
        url = base + (f"?cursor={urllib.parse.quote(cursor)}&limit=1000" if cursor else "?limit=1000")
        with urllib.request.urlopen(_req(url, token), timeout=30) as resp:
            link = resp.headers.get("Link", "")
            data = json.loads(resp.read().decode())
        out.extend(data)
        # HF paginates via Link: <...cursor=..>; rel="next"
        if 'rel="next"' in link:
            # extract cursor param
            import re
            m = re.search(r"cursor=([^&>]+)", link)
            cursor = urllib.parse.unquote(m.group(1)) if m else ""
            if not cursor:
                break
        else:
            break
    return out


def resolve_queries(dataset: str, subject_path: str, n: int, token: str) -> list[str]:
    """Return up to n query file paths under a subject dir, deterministic by index."""
    entries = [e for e in list_dir(dataset, subject_path, token)
               if e.get("type") == "file" and e.get("path", "").endswith(".json")]

    def idx(e):
        stem = Path(e["path"]).stem
        return int(stem) if stem.isdigit() else 1 << 30
    entries.sort(key=idx)
    return [e["path"] for e in entries[:n]]


def download(dataset: str, path: str, token: str, dest: Path) -> tuple[int, str]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = RESOLVE.format(id=dataset, path=urllib.parse.quote(path))
    h = hashlib.sha256()
    size = 0
    with urllib.request.urlopen(_req(url, token), timeout=120) as resp, open(dest, "wb") as f:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
            size += len(chunk)
    return size, h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text())
    dataset = cfg["dataset"]
    variants = cfg["variants"]
    domains = cfg["domains"]
    spd = int(cfg.get("subjects_per_domain", 1))
    qps = int(cfg.get("queries_per_subject", 3))
    max_bytes = int(cfg.get("max_bytes", 2 * 1024**3))
    token = get_token()
    if not token:
        print("ERROR: no HF token found (HF_TOKEN or ~/.cache/huggingface/token)", file=sys.stderr)
        return 2

    structure = json.loads((ROOT / "data" / "registry" / "dataset_structure.json").read_text())
    src_rev = structure.get("source_revision", "unknown")
    mv = structure["model_variants"]

    registry = {"dataset": dataset, "source_revision": src_rev, "config": args.config, "files": []}
    if REGISTRY.exists():
        try:
            registry = json.loads(REGISTRY.read_text())
        except Exception:
            pass
    known = {f["path"] for f in registry.get("files", [])}
    registry.setdefault("files", [])

    plan = []
    for variant in variants:
        vinfo = mv.get(variant)
        if not vinfo:
            print(f"WARN: variant not in structure: {variant}", file=sys.stderr)
            continue
        for dom in domains:
            dinfo = vinfo.get(dom)
            if not dinfo:
                print(f"skip {variant}/{dom}: not present", file=sys.stderr)
                continue
            subjects = sorted(dinfo["subjects"].keys())[:spd]
            for subj in subjects:
                subject_path = f"{variant}/{dom}/{subj}"
                qpaths = resolve_queries(dataset, subject_path, qps, token)
                for qp in qpaths:
                    plan.append((variant, dom, subj, qp))

    print(f"planned files: {len(plan)}")
    total = 0
    fetched = 0
    for variant, dom, subj, qp in plan:
        dest = RAW_ROOT / qp
        if qp in known and dest.exists():
            print(f"  skip (already registered): {qp}")
            continue
        if args.dry_run:
            print(f"  would fetch: {qp}")
            continue
        if total >= max_bytes:
            print(f"  STOP: byte cap {max_bytes} reached", file=sys.stderr)
            break
        for attempt in range(3):
            try:
                size, digest = download(dataset, qp, token, dest)
                break
            except Exception as e:
                if attempt == 2:
                    print(f"  FAIL {qp}: {e}", file=sys.stderr)
                    size, digest = 0, ""
                else:
                    time.sleep(1.5 * (attempt + 1))
        if not digest:
            continue
        total += size
        fetched += 1
        registry["files"].append({
            "path": qp, "variant": variant, "benchmark": dom, "subject": subj,
            "query_id": Path(qp).stem, "bytes": size, "sha256": digest,
            "source_revision": src_rev,
        })
        known.add(qp)
        print(f"  fetched {qp} ({size} bytes)")

    if not args.dry_run:
        REGISTRY.write_text(json.dumps(registry, indent=2, ensure_ascii=False))
        print(f"registry updated: {REGISTRY} ({len(registry['files'])} files total, {fetched} new, {total} new bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

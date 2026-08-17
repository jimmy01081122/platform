#!/usr/bin/env python3
"""Calibrate the P-I effective-bandwidth/latency knobs with REAL DRAM timing.

Runs Ramulator2 (cycle-level DRAM) on the actual MoE expert-transfer access
pattern (large contiguous streaming) with and without co-running compute
contention, at LPDDR5-6400 channel counts whose JEDEC peaks bracket the P-I
bandwidth sweep (2ch=25.6, 4ch=51.2, 8ch=102.4 GB/s). Replaces the *guessed*
analytic contention_sweep {0.4,0.6,0.8} in configs/platform/p_i_integrated.json
with a DRAM-timing-measured effective fraction + measured read latency.

For a mix trace with random fraction f, the transfer-available bandwidth is
achieved_total * (1-f) (the streaming share of the shared channel), and the
DRAM-timing contention fraction = transfer_available / JEDEC_peak.

Output: data/canonical/moe_routing_v1/mem_timing.json (+ .md).
Runs the simulator in the pinned edgehetero-mem:1 image.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "canonical" / "moe_routing_v1"
IMG = "edgehetero-mem:1"

# Per-channel calibration only: LoadStoreTrace injects ONE request/cycle (a single
# stream), so it saturates exactly one channel; >1 channel would be injection-limited
# (a frontend artifact, not DRAM timing). Aggregate P-I bandwidth = n_channels x the
# per-channel achieved value (each channel driven by its own copy-engine/DMA stream).
CHANNELS = [1]                # LPDDR5-6400 x16: 12.8 GB/s JEDEC peak per channel
PATTERNS = ["seq", "mix:0.25", "mix:0.5", "mix:0.75", "rand"]
N = 120000
SPAN = 128 * 1024 * 1024


def run_all() -> list[dict]:
    # one container: generate each trace then run it, emit one json line per run
    script = ["set -e", "cd /work", "mkdir -p mem/out"]
    runs = []
    for ch in CHANNELS:
        for pat in PATTERNS:
            tag = f"lpddr5_{ch}ch_{pat.replace(':', '')}"
            tr = f"mem/out/{tag}.trace"
            script.append(
                f"python3 mem/gen_trace.py --pattern '{pat}' --n {N} --span {SPAN} --out {tr} >/dev/null")
            script.append(
                f"echo -n 'RESULT {ch} {pat} '; "
                f"python3 mem/run_ramulator.py --std LPDDR5_6400 --channels {ch} --trace {tr}")
            script.append(f"rm -f {tr}")
            runs.append((ch, pat))
    cmd = ["docker", "run", "--rm", "-v", f"{ROOT}:/work", "-w", "/work", IMG,
           "bash", "-lc", "\n".join(script)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stdout[-3000:]); print(proc.stderr[-3000:])
        raise SystemExit("ramulator calibration run failed")
    results = []
    for line in proc.stdout.splitlines():
        if not line.startswith("RESULT "):
            continue
        _, ch, pat, js = line.split(" ", 3)
        d = json.loads(js)
        d["req_channels"] = int(ch)
        d["pattern"] = pat
        f = float(pat.split(":", 1)[1]) if pat.startswith("mix:") else (1.0 if pat == "rand" else 0.0)
        d["rand_fraction"] = f
        d["transfer_available_MBps"] = round(d["achieved_read_MBps"] * (1 - f), 1)
        d["dram_contention_fraction"] = round(d["transfer_available_MBps"] / d["peak_MBps"], 4)
        results.append(d)
    return results


def main() -> int:
    results = run_all()
    report = {
        "tool": "Ramulator2 (CMU-SAFARI) cycle-level DRAM",
        "image": IMG,
        "standard": "LPDDR5-6400 x16, 12.8 GB/s per-channel JEDEC peak (integrated CPU-GPU unified LPDDR)",
        "aggregate_model": "per-channel result; aggregate P-I bandwidth = n_channels x achieved "
                           "(2ch~25.6, 4ch~51.2, 8ch~102.4 GB/s peak), each channel fed by its own "
                           "copy-engine/DMA stream. Single LoadStoreTrace stream saturates one channel.",
        "access_model": "seq = contiguous expert-weight stream; mix:f = stream with "
                        "fraction f random compute traffic on the shared channel; "
                        "rand = worst-case scatter. transfer_available = achieved*(1-f).",
        "note": "DRAM-timing-measured contention fraction REPLACES the guessed analytic "
                "{0.4,0.6,0.8}; feeds scripts/w3_mem_recheck.py.",
        "runs": results,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "mem_timing.json").write_text(json.dumps(report, indent=2))
    _md(report)
    for r in results:
        print(f"{r['req_channels']}ch peak={r['peak_MBps']/1e3:.1f}GB/s {r['pattern']:<9} "
              f"achieved={r['achieved_read_MBps']/1e3:6.2f}GB/s eff={r['efficiency']:.3f} "
              f"xfer_avail={r['transfer_available_MBps']/1e3:6.2f}GB/s "
              f"contention={r['dram_contention_fraction']:.3f} "
              f"lat={r['avg_read_latency_cyc']:.0f}cyc rowhit={r['row_hit_rate']:.3f}")
    return 0


def _md(report):
    out = ROOT / "explorations" / "moe_orchestration" / "W3_MEM_TIMING.md"
    L = ["# W3 P-I memory-timing calibration (Ramulator2, cycle-level DRAM)\n",
         f"Tool: `{report['tool']}` in `{report['image']}`. Standard: {report['standard']}.\n",
         f"\n{report['access_model']}\n",
         "\n| ch | peak (GB/s) | pattern | achieved (GB/s) | efficiency | xfer-avail (GB/s) | contention frac | read lat (cyc) | row-hit |\n",
         "|---|---|---|---|---|---|---|---|---|\n"]
    for r in report["runs"]:
        L.append(f"| {r['req_channels']} | {r['peak_MBps']/1e3:.1f} | {r['pattern']} | "
                 f"{r['achieved_read_MBps']/1e3:.2f} | {r['efficiency']:.3f} | "
                 f"{r['transfer_available_MBps']/1e3:.2f} | {r['dram_contention_fraction']:.3f} | "
                 f"{r['avg_read_latency_cyc']:.0f} | {r['row_hit_rate']:.3f} |\n")
    L.append("\nSee `scripts/w3_mem_recheck.py` / the recheck section for the SW-vs-HW "
             "decision re-evaluation under these measured knobs.\n")
    out.write_text("".join(L))


if __name__ == "__main__":
    raise SystemExit(main())

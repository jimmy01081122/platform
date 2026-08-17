#!/usr/bin/env python3
"""Generate Ramulator2 LoadStoreTrace files for the MoE expert-transfer study.

Trace format (ramulator2 LoadStoreTrace): one access per line, `LD <addr>` /
`ST <addr>`; injected once per cycle, run stops after trace-length accepts.

Patterns:
  seq     : contiguous 64B-stride reads over `span` bytes -> models a large
            contiguous expert-weight streaming copy (row-buffer-hit dominated).
  rand    : uniform 64B-aligned reads over `span` bytes -> models compute-side
            scattered traffic (row-conflict dominated).
  mix:f   : interleave with fraction f (0..1) of accesses drawn from `rand`
            and (1-f) from `seq` -> transfer streaming under co-running compute
            contention on the SAME shared channel (P-I).

Deterministic: fixed seed, no wall-clock/env dependence.
"""
from __future__ import annotations

import argparse
import random

LINE = 64  # cache-line / access granularity (bytes)


def gen(pattern: str, n: int, span: int, seed: int) -> list[str]:
    rng = random.Random(seed)
    nslots = max(1, span // LINE)
    out: list[str] = []
    if pattern == "seq":
        for i in range(n):
            out.append(f"LD {(i % nslots) * LINE}")
    elif pattern == "rand":
        for _ in range(n):
            out.append(f"LD {rng.randrange(nslots) * LINE}")
    elif pattern.startswith("mix:"):
        f = float(pattern.split(":", 1)[1])
        seqptr = 0
        for _ in range(n):
            if rng.random() < f:
                out.append(f"LD {rng.randrange(nslots) * LINE}")
            else:
                out.append(f"LD {(seqptr % nslots) * LINE}")
                seqptr += 1
    else:
        raise SystemExit(f"unknown pattern {pattern}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pattern", required=True, help="seq | rand | mix:<f>")
    ap.add_argument("--n", type=int, default=100000)
    ap.add_argument("--span", type=int, default=128 * 1024 * 1024)
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    lines = gen(a.pattern, a.n, a.span, a.seed)
    with open(a.out, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"wrote {a.out} ({len(lines)} lines, pattern={a.pattern}, span={a.span})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# Firmware / software shared scheduler kernel

`scheduler.c` implements the frozen residency/prefetch decision (semantic_
revision=1), identical in semantics to `edgeflow.residency` and used for BOTH the
host-software build and the RV64 firmware build (cross-layer equivalence).

## Build & measure (dedicated Docker)

```bash
# build the dedicated toolchain image (not shared with other projects)
docker build -t edgehetero-fw:1 -f firmware/Dockerfile firmware

# export a demands file from a canonical trace
python3 scripts/export_demands.py --canonical <canonical.jsonl> --out /tmp/s4/demands.txt

# build native (software) + RV64 (firmware) and measure decision cost
docker run --rm -v "$PWD":/work -v /tmp/s4:/data -w /work edgehetero-fw:1 \
  bash firmware/build_and_measure.sh /data/demands.txt <capacity> <depth> <reps>
```

Native output reports `ns_per_step` (host x86 wall time). RV64 output reports
`instructions_per_step` from the `instret` CSR emulated by `qemu-riscv64` — a real
dynamic instruction count, not an estimate. Both print the same scheduler counters
so equivalence with the Python reference can be checked.

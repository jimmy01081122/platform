#!/usr/bin/env bash
# Build the shared scheduler as native (software) and RV64 (firmware) binaries,
# run both over a demands file, and emit machine-comparable JSON lines.
# Intended to run inside the dedicated firmware Docker image.
set -euo pipefail

SRC_DIR="${SRC_DIR:-/work/firmware}"
DEMANDS="${1:?usage: build_and_measure.sh <demands_file> <capacity> <depth> [reps]}"
CAP="${2:?capacity}"
DEPTH="${3:?depth}"
REPS="${4:-200}"
OUT_DIR="${OUT_DIR:-/work/out}"
mkdir -p "$OUT_DIR"

# native software build
gcc -O2 -std=c11 -o "$OUT_DIR/sched_native" \
    "$SRC_DIR/main.c" "$SRC_DIR/scheduler.c"

# RV64 firmware build (static so qemu-user needs no cross sysroot at runtime)
riscv64-linux-gnu-gcc -O2 -std=c11 -static -o "$OUT_DIR/sched_rv64" \
    "$SRC_DIR/main.c" "$SRC_DIR/scheduler.c"

echo "== toolchain versions ==" >&2
gcc --version | head -1 >&2
riscv64-linux-gnu-gcc --version | head -1 >&2
qemu-riscv64 --version | head -1 >&2

# run native
"$OUT_DIR/sched_native" "$DEMANDS" "$CAP" "$DEPTH" "$REPS"
# run firmware under qemu-user (reads instret CSR, emulated by qemu)
qemu-riscv64 "$OUT_DIR/sched_rv64" "$DEMANDS" "$CAP" "$DEPTH" "$REPS"

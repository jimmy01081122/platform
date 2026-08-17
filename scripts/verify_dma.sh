#!/usr/bin/env bash
# Verilator lint + assertion-enabled randomized DMA block regression.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMG="${RTL_IMAGE:-edgehetero-rtl:1}"
OUT="$ROOT/verification/out/dma_verilator"
OBJ="$OUT/obj_dir"
LOG="$OUT/regression.log"
mkdir -p "$OUT"
docker run --rm -v "$ROOT":/work "$IMG" \
  rm -rf /work/verification/out/dma_verilator/obj_dir \
         /work/verification/out/dma_verilator/engine_obj_dir \
         /work/verification/out/dma_verilator/coverage.dat \
         /work/verification/out/dma_verilator/coverage.info

docker run --rm --user "$(id -u):$(id -g)" \
  -v "$ROOT":/work -w /work "$IMG" bash -lc "
  set -euo pipefail
  verilator --version
  verilator --lint-only --assert -Wall -Wno-fatal \
    -Wno-DECLFILENAME -Wno-UNUSEDSIGNAL -Wno-IMPORTSTAR \
    --top-module dma_model_tb_top \
    rtl/common/moe_pkg.sv \
    rtl/interfaces/dma_model.sv \
    rtl/verification/dma_model_tb_top.sv \
    rtl/verification/ready_valid_fifo_assertions.sv \
    rtl/verification/dma_model_assert_bind.sv
  verilator --cc --exe --build -O2 --assert --coverage -Wno-fatal \
    -Wno-DECLFILENAME -Wno-UNUSEDSIGNAL -Wno-IMPORTSTAR \
    --top-module dma_model_tb_top -GLATENCY=2 -GDEPTH=4 -GLATENCY_JITTER=7 \
    --Mdir /work/verification/out/dma_verilator/obj_dir \
    -CFLAGS '-I/work/rtl/verification -O2' \
    rtl/common/moe_pkg.sv \
    rtl/interfaces/dma_model.sv \
    rtl/verification/dma_model_tb_top.sv \
    rtl/verification/ready_valid_fifo_assertions.sv \
    rtl/verification/dma_model_assert_bind.sv \
    /work/rtl/verification/tb_dma_model.cpp \
    -o Vdma_test
  cd /work/verification/out/dma_verilator
  ./obj_dir/Vdma_test
  verilator_coverage --write-info obj_dir/coverage.info obj_dir/coverage.dat
  cd /work
  verilator --cc --exe --build -O2 --assert -Wno-fatal \
    -Wno-DECLFILENAME -Wno-UNUSEDSIGNAL -Wno-IMPORTSTAR \
    -DMOE_MAX_EXPERTS=8 --top-module residency_engine_dv_top \
    --Mdir /work/verification/out/dma_verilator/engine_obj_dir \
    -CFLAGS '-I/work/rtl/verification -O2' \
    rtl/common/moe_pkg.sv \
    rtl/datapath/lru_victim.sv \
    rtl/datapath/residency_engine.sv \
    rtl/verification/ready_valid_fifo_assertions.sv \
    rtl/verification/residency_engine_dv_top.sv \
    /work/rtl/verification/tb_residency_dma.cpp \
    -o Vresidency_dma_test
  /work/verification/out/dma_verilator/engine_obj_dir/Vresidency_dma_test
" 2>&1 | tee "$LOG"

test "$(grep -c '"pass":true' "$LOG")" -eq 2
test -s "$OUT/obj_dir/coverage.dat"
test -s "$OUT/obj_dir/coverage.info"

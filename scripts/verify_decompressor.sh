#!/usr/bin/env bash
# Slice-2: build + run the streaming expert_decompressor Verilator tb and prove it is
# BIT-FOR-BIT equal to the golden integer dequant (edgeflow.expert_codec.decode_fixed)
# at every deployed code width NB in {2,4,8}. Runs inside edgehetero-rtl:1.
# Output: explorations/moe_orchestration/slice2_decompressor_verify.json
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUTDIR="$ROOT/explorations/moe_orchestration"
OUT="$OUTDIR/slice2_decompressor_verify.json"
mkdir -p "$OUTDIR"
NBS="${1:-2 4 8}"
WAIV="-Wno-DECLFILENAME -Wno-UNUSEDSIGNAL -Wno-IMPORTSTAR -Wno-UNUSEDPARAM -Wno-WIDTH"

echo "[" > "$OUT"
first=1
for NB in $NBS; do
  echo "== decompressor equivalence NB=$NB ==" >&2
  RES=$(docker run --rm -v "$ROOT":/work -w /work edgehetero-rtl:1 bash -lc "
    rm -rf /tmp/dec_$NB
    verilator --lint-only -Wall $WAIV -GNB=$NB \
      --top-module expert_decompressor_tb_top \
      /work/rtl/datapath/expert_decompressor.sv /work/rtl/top/expert_decompressor_tb_top.sv \
      >/tmp/lint_$NB.log 2>&1 || { echo '{\"nb\":$NB,\"pass\":false,\"stage\":\"lint\"}'; cat /tmp/lint_$NB.log >&2; exit 0; }
    verilator --cc --exe --build -O2 -Wno-fatal $WAIV -GNB=$NB \
      --top-module expert_decompressor_tb_top --Mdir /tmp/dec_$NB \
      -CFLAGS \"-O2 -DTB_NB=$NB\" \
      /work/rtl/datapath/expert_decompressor.sv /work/rtl/top/expert_decompressor_tb_top.sv \
      /work/rtl/verification/tb_expert_decompressor.cpp -o Vdec \
      >/tmp/build_$NB.log 2>&1 || { echo '{\"nb\":$NB,\"pass\":false,\"stage\":\"build\"}'; cat /tmp/build_$NB.log >&2; exit 0; }
    /tmp/dec_$NB/Vdec
  ")
  echo "  $RES" >&2
  [[ $first -eq 1 ]] && first=0 || echo "," >> "$OUT"
  printf '  %s' "$RES" >> "$OUT"
done
echo "" >> "$OUT"
echo "]" >> "$OUT"
echo "== wrote $OUT ==" >&2
cat "$OUT"

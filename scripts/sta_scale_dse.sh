#!/usr/bin/env bash
# Criterion-A hardware DSE: scale the buffered sequential-argmin residency engine
# to the expert counts the large-MoE workload requires (128/256/384) and measure
# real wire-load-aware Fmax (OpenSTA + Nangate45 fanout WLM) + cell area (yosys).
# Answers: does the engine still meet 200 MHz as MAX_EXPERTS grows to real models?
#
#   scripts/sta_scale_dse.sh [ME_LIST]   (default "32 128 256 384")
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/sta/out_scale"
LIB="/work/syn/lib/nangate45.lib"
WLM="${WLM:-5K_hvratio_1_1}"
TS="${TS:-16}"
BUFN="${BUFN:-16}"
PERIOD="${PERIOD:-5.0}"
ME_LIST="${1:-32 128 256 384}"
mkdir -p "$OUT"
CSV="$OUT/sta_scale.csv"
echo "max_experts,ts_w,fmax_mhz,area_um2,meets_200mhz" > "$CSV"

bash "$ROOT/scripts/fetch_pdk.sh" >/dev/null 2>&1 || true

for ME in $ME_LIST; do
  TAG="seqbuf_me${ME}"
  echo ">> STA scale ME=$ME (buffered sequential-argmin)" >&2

  if [[ ! -s "$OUT/netlist_${TAG}.v" ]]; then
    docker run --rm -v "$ROOT":/work -w /work edgehetero-syn:1 \
      bash sta/gen_netlist.sh "$ME" "$TS" /work/sta/out_scale "$TAG" "-DMOE_SEQ_ARGMIN" "$BUFN" \
      > "$OUT/gen_${TAG}.stdout" 2>&1 || { echo "$ME,$TS,,,gen_fail" >> "$CSV"; continue; }
  fi

  # cell area from yosys stat against the liberty
  docker run --rm -v "$ROOT":/work -w /work edgehetero-syn:1 \
    yosys -p "read_verilog /work/sta/out_scale/netlist_${TAG}.v; stat -liberty $LIB" \
    > "$OUT/area_${TAG}.log" 2>&1 || true
  AREA=$(grep -oE "Chip area for module.*: [0-9.]+" "$OUT/area_${TAG}.log" | tail -1 | grep -oE "[0-9.]+$")

  # wire-load-aware Fmax via OpenSTA
  docker run --rm -v "$ROOT":/work -w /work \
    -e LIB="$LIB" -e NETLIST="/work/sta/out_scale/netlist_${TAG}.v" \
    -e TOP="moe_residency_top" -e PERIOD="$PERIOD" -e WLM="$WLM" \
    edgehetero-sta:1 sta -no_init -exit /work/sta/run_sta.tcl \
    > "$OUT/sta_${TAG}.log" 2>&1 || true
  FMAX=$(grep -oE "FMAX_MHZ [0-9.]+" "$OUT/sta_${TAG}.log" | tail -1 | grep -oE "[0-9.]+")

  MEETS="unknown"
  if [[ -n "${FMAX:-}" ]]; then
    MEETS=$(awk -v f="$FMAX" 'BEGIN{print (f>=200)?"yes":"no"}')
  fi
  echo "$ME,$TS,${FMAX:-},${AREA:-},${MEETS}" >> "$CSV"
  echo "   ME=$ME Fmax=${FMAX:-NA} MHz area=${AREA:-NA} um2 meets200=${MEETS}" >&2
done

docker run --rm -v "$ROOT":/work -w /work edgehetero-syn:1 \
  chown -R "$(id -u):$(id -g)" /work/sta/out_scale >/dev/null 2>&1 || true
echo "== STA scale CSV ==" >&2
cat "$CSV"

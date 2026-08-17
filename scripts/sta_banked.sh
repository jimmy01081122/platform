#!/usr/bin/env bash
# Standalone STA of the BANKED sequential-argmin module across large N, to show the
# per-cycle critical path (and Fmax) is decoupled from N (unlike the flat N-wide
# victim mux). Emits a concrete-param top wrapper, sv2v->yosys(Nangate45)->OpenSTA
# wire-load Fmax + area. Args: pairs "N B" ... (default "128 8" "256 16" "384 16").
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/sta/out_banked"; mkdir -p "$OUT"
LIB="/work/syn/lib/nangate45.lib"; WLM="${WLM:-5K_hvratio_1_1}"
TS="${TS:-16}"; BUFN="${BUFN:-16}"; PERIOD="${PERIOD:-5.0}"
CSV="$OUT/sta_banked.csv"; echo "N,B,ts_w,fmax_mhz,area_um2,meets_200mhz" > "$CSV"
bash "$ROOT/scripts/fetch_pdk.sh" >/dev/null 2>&1 || true

run_one() {
  local N="$1" B="$2" TAG="bank_N${1}_B${2}"
  local GW; GW=$(python3 -c "import math;print(max(1,math.ceil(math.log2($N))))")
  cat > "$OUT/top_${TAG}.sv" <<EOF
module lru_banked_top (
  input  logic clk, rst_n, start,
  input  logic [${N}-1:0] valid,
  input  logic [${N}*${TS}-1:0] ts_flat,
  output logic busy, done, found,
  output logic [${GW}-1:0] victim
);
  logic [${N}-1:0][${TS}-1:0] ts;
  genvar g;
  generate for (g=0; g<${N}; g++) begin: u ; assign ts[g] = ts_flat[g*${TS} +: ${TS}]; end endgenerate
  lru_victim_banked #(.N(${N}), .TSW(${TS}), .B(${B})) uut (
    .clk(clk), .rst_n(rst_n), .start(start), .valid(valid), .ts(ts),
    .busy(busy), .done(done), .found(found), .victim(victim));
endmodule
EOF

  docker run --rm -v "$ROOT":/work -w /work edgehetero-syn:1 bash -lc "
    sv2v /work/rtl/datapath/lru_victim.sv /work/sta/out_banked/top_${TAG}.sv > /work/sta/out_banked/flat_${TAG}.v 2>/work/sta/out_banked/sv2v_${TAG}.log
    printf 'strash\ndch\nmap\nbuffer -N ${BUFN}\nupsize\ndnsize\ntopo\n' > /work/sta/out_banked/abc_${TAG}.do
    cat > /work/sta/out_banked/gen_${TAG}.ys <<YS
read_verilog /work/sta/out_banked/flat_${TAG}.v
synth -top lru_banked_top -flatten
dfflibmap -liberty ${LIB}
abc -liberty ${LIB} -script /work/sta/out_banked/abc_${TAG}.do
clean
stat -liberty ${LIB}
write_verilog -noattr /work/sta/out_banked/netlist_${TAG}.v
YS
    yosys -s /work/sta/out_banked/gen_${TAG}.ys > /work/sta/out_banked/gen_${TAG}.log 2>&1
  " || { echo "$N,$B,$TS,,,gen_fail" >> "$CSV"; return; }

  local AREA; AREA=$(grep -oE "Chip area for module.*: [0-9.]+" "$OUT/gen_${TAG}.log" | tail -1 | grep -oE "[0-9.]+$")
  docker run --rm -v "$ROOT":/work -w /work \
    -e LIB="$LIB" -e NETLIST="/work/sta/out_banked/netlist_${TAG}.v" \
    -e TOP="lru_banked_top" -e PERIOD="$PERIOD" -e WLM="$WLM" \
    edgehetero-sta:1 sta -no_init -exit /work/sta/run_sta.tcl > "$OUT/sta_${TAG}.log" 2>&1 || true
  local FMAX; FMAX=$(grep -oE "FMAX_MHZ [0-9.]+" "$OUT/sta_${TAG}.log" | tail -1 | grep -oE "[0-9.]+")
  local MEETS="unknown"
  [[ -n "${FMAX:-}" ]] && MEETS=$(awk -v f="$FMAX" 'BEGIN{print (f>=200)?"yes":"no"}')
  echo "$N,$B,$TS,${FMAX:-},${AREA:-},${MEETS}" >> "$CSV"
  echo "   N=$N B=$B Fmax=${FMAX:-NA} MHz area=${AREA:-NA} meets200=${MEETS}" >&2
}

if [[ $# -eq 0 ]]; then set -- "128 8" "256 16" "384 16"; fi
for pair in "$@"; do run_one $pair; done
docker run --rm -v "$ROOT":/work -w /work edgehetero-syn:1 chown -R "$(id -u):$(id -g)" /work/sta/out_banked >/dev/null 2>&1 || true
echo "== banked STA CSV =="; cat "$CSV"

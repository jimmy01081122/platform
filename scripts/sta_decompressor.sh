#!/usr/bin/env bash
# Standalone STA of the slice-2 streaming expert_decompressor: reg-to-reg Fmax + area
# (sv2v -> yosys/Nangate45 -> OpenSTA wire-load), swept over code width NB and lane
# count LANES. Answers the D-058 sizing question: does the block sustain the required
# OUTPUT bandwidth (~link_BW x r) at a plausible clock? Args: pairs "NB LANES" ...
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/sta/out_decomp"; mkdir -p "$OUT"
LIB="/work/syn/lib/nangate45.lib"; WLM="${WLM:-5K_hvratio_1_1}"
SCW="${SCW:-16}"; FRACW="${FRACW:-12}"; OUTW="${OUTW:-16}"; BUFN="${BUFN:-16}"; PERIOD="${PERIOD:-5.0}"
CSV="$OUT/sta_decomp.csv"
echo "NB,LANES,outw,fmax_mhz,area_um2,in_GBps_at_fmax,out_GBps_at_fmax,lanes_for_16GBps_in,meets_200mhz" > "$CSV"
bash "$ROOT/scripts/fetch_pdk.sh" >/dev/null 2>&1 || true

run_one() {
  local NB="$1" LANES="$2" TAG="dec_NB${1}_L${2}"
  cat > "$OUT/top_${TAG}.sv" <<EOF
module dec_top (
  input  logic clk, rst_n, in_valid,
  input  logic [${LANES}*${NB}-1:0] codes_packed,
  input  logic signed [${SCW}-1:0] scale_q,
  output logic out_valid,
  output logic [${LANES}*${OUTW}-1:0] out_packed
);
  expert_decompressor #(.NB(${NB}), .LANES(${LANES}), .SCW(${SCW}), .FRACW(${FRACW}), .OUTW(${OUTW})) uut (
    .clk(clk), .rst_n(rst_n), .in_valid(in_valid),
    .codes_packed(codes_packed), .scale_q(scale_q),
    .out_valid(out_valid), .out_packed(out_packed));
endmodule
EOF

  docker run --rm -v "$ROOT":/work -w /work edgehetero-syn:1 bash -lc "
    sv2v /work/rtl/datapath/expert_decompressor.sv /work/sta/out_decomp/top_${TAG}.sv > /work/sta/out_decomp/flat_${TAG}.v 2>/work/sta/out_decomp/sv2v_${TAG}.log
    printf 'strash\ndch\nmap\nbuffer -N ${BUFN}\nupsize\ndnsize\ntopo\n' > /work/sta/out_decomp/abc_${TAG}.do
    cat > /work/sta/out_decomp/gen_${TAG}.ys <<YS
read_verilog /work/sta/out_decomp/flat_${TAG}.v
synth -top dec_top -flatten
dfflibmap -liberty ${LIB}
abc -liberty ${LIB} -script /work/sta/out_decomp/abc_${TAG}.do
clean
stat -liberty ${LIB}
write_verilog -noattr /work/sta/out_decomp/netlist_${TAG}.v
YS
    yosys -s /work/sta/out_decomp/gen_${TAG}.ys > /work/sta/out_decomp/gen_${TAG}.log 2>&1
  " || { echo "$NB,$LANES,$OUTW,,,,gen_fail" >> "$CSV"; return; }

  local AREA; AREA=$(grep -oE "Chip area for module.*: [0-9.]+" "$OUT/gen_${TAG}.log" | tail -1 | grep -oE "[0-9.]+$")
  docker run --rm -v "$ROOT":/work -w /work \
    -e LIB="$LIB" -e NETLIST="/work/sta/out_decomp/netlist_${TAG}.v" \
    -e TOP="dec_top" -e PERIOD="$PERIOD" -e WLM="$WLM" \
    edgehetero-sta:1 sta -no_init -exit /work/sta/run_sta.tcl > "$OUT/sta_${TAG}.log" 2>&1 || true
  local FMAX; FMAX=$(grep -oE "FMAX_MHZ [0-9.]+" "$OUT/sta_${TAG}.log" | tail -1 | grep -oE "[0-9.]+")
  local MEETS="unknown" OUTBW="" INBW="" LANES16=""
  if [[ -n "${FMAX:-}" ]]; then
    MEETS=$(awk -v f="$FMAX" 'BEGIN{print (f>=200)?"yes":"no"}')
    # compressed INPUT consume bytes/s = LANES * NB/8 * Fmax ; OUTPUT = LANES * OUTW/8 * Fmax
    INBW=$(awk  -v l="$LANES" -v n="$NB"   -v f="$FMAX" 'BEGIN{printf "%.2f", l*(n/8.0)*f/1000.0}')
    OUTBW=$(awk -v l="$LANES" -v o="$OUTW" -v f="$FMAX" 'BEGIN{printf "%.1f",  l*(o/8.0)*f/1000.0}')
    # lanes needed for the compressed input to keep up with a 16 GB/s PCIe link
    LANES16=$(awk -v n="$NB" -v f="$FMAX" 'BEGIN{printf "%.0f", 16.0/((n/8.0)*f/1000.0)}')
  fi
  echo "$NB,$LANES,$OUTW,${FMAX:-},${AREA:-},${INBW:-},${OUTBW:-},${LANES16:-},${MEETS}" >> "$CSV"
  echo "   NB=$NB LANES=$LANES Fmax=${FMAX:-NA} MHz area=${AREA:-NA} in=${INBW:-NA} out=${OUTBW:-NA} GB/s lanes@16=${LANES16:-NA} meets200=${MEETS}" >&2
}

if [[ $# -eq 0 ]]; then set -- "4 8" "8 8" "4 16" "8 16"; fi
for pair in "$@"; do run_one $pair; done
docker run --rm -v "$ROOT":/work -w /work edgehetero-syn:1 chown -R "$(id -u):$(id -g)" /work/sta/out_decomp >/dev/null 2>&1 || true
echo "== decompressor STA CSV =="; cat "$CSV"

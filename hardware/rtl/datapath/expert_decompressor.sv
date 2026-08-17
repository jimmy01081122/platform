// Slice-2 streaming expert-weight DECOMPRESSOR datapath (candidate C6, D-057/D-058).
//
// Dequantizes a packed stream of signed int-NB codes into saturating fixed-point
// elements using a per-group fixed-point scale, LANES codes per cycle:
//
//   out[l] = sat_OUTW( (signed(code[l]) * signed(scale_q) + 2^(FRACW-1)) >>> FRACW )
//
// This is the INTEGER-EXACT analogue of edgeflow.expert_codec.decode_fixed, so the
// block is provable bit-for-bit against that golden reference (the same executable-
// reference -> RTL equivalence methodology as slice-1's residency engine). The
// bit-unpack + sign-extend + affine dequant is the throughput-critical front-end of
// the decompressor that must sustain ~link_BW x r of output (D-058 sizing); the
// downstream FP cast (if any) is a standard unit and out of scope here.
//
// 2-cycle registered latency (input flop -> combinational dequant -> output flop),
// giving a clean reg-to-reg timing path through the multiply for STA. Fixed-rate stream
// (always ready). Parameterizable code width NB (2/3/4/8), lane count LANES, scale
// width/frac SCW/FRACW, output width OUTW.
module expert_decompressor #(
  parameter int NB    = 4,     // code bits (signed two's-complement)
  parameter int LANES = 8,     // codes dequantized per cycle
  parameter int SCW   = 16,    // fixed-point scale width (signed)
  parameter int FRACW = 12,    // scale fractional bits (>=1)
  parameter int OUTW  = 16     // output element width (signed, saturating)
)(
  input  logic                  clk,
  input  logic                  rst_n,
  input  logic                  in_valid,
  input  logic [LANES*NB-1:0]   codes_packed,
  input  logic signed [SCW-1:0] scale_q,
  output logic                  out_valid,
  output logic [LANES*OUTW-1:0] out_packed
);
  // product headroom: NB + SCW covers code*scale; +1 for the round bias add
  localparam int PW = NB + SCW + 1;
  localparam logic signed [PW-1:0] OUT_MAX = (PW'(1) <<< (OUTW-1)) - PW'(1);
  localparam logic signed [PW-1:0] OUT_MIN = -(PW'(1) <<< (OUTW-1));
  localparam logic signed [PW-1:0] BIAS    = PW'(1) <<< (FRACW-1);

  // stage-1: register inputs
  logic                  v_q;
  logic [LANES*NB-1:0]   codes_q;
  logic signed [SCW-1:0] scale_qq;
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      v_q <= 1'b0; codes_q <= '0; scale_qq <= '0;
    end else begin
      v_q <= in_valid; codes_q <= codes_packed; scale_qq <= scale_q;
    end
  end

  // combinational dequant on the registered inputs
  logic [LANES*OUTW-1:0] out_c;
  always_comb begin
    for (int l = 0; l < LANES; l++) begin
      logic signed [NB-1:0]   code;
      logic signed [PW-1:0]   prod, shifted;
      logic signed [OUTW-1:0] sat;
      code    = codes_q[l*NB +: NB];
      prod    = ($signed(code) * $signed(scale_qq)) + BIAS;
      shifted = prod >>> FRACW;                       // arithmetic shift (floor)
      if (shifted > OUT_MAX)      sat = OUT_MAX[OUTW-1:0];
      else if (shifted < OUT_MIN) sat = OUT_MIN[OUTW-1:0];
      else                        sat = shifted[OUTW-1:0];
      out_c[l*OUTW +: OUTW] = sat;
    end
  end

  // stage-2: register outputs
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      out_valid  <= 1'b0;
      out_packed <= '0;
    end else begin
      out_valid  <= v_q;
      out_packed <= out_c;
    end
  end
endmodule

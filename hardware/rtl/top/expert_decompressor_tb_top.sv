// Verification / STA wrapper for the slice-2 streaming expert decompressor. Fixes the
// architecture params so a Verilator tb (bit-exact vs edgeflow.expert_codec.decode_fixed)
// and the STA flow share one concrete top. Override with -G<param>=<val>.
module expert_decompressor_tb_top #(
  parameter int NB    = 4,
  parameter int LANES = 8,
  parameter int SCW   = 16,
  parameter int FRACW = 12,
  parameter int OUTW  = 16
)(
  input  logic                  clk,
  input  logic                  rst_n,
  input  logic                  in_valid,
  input  logic [LANES*NB-1:0]   codes_packed,
  input  logic signed [SCW-1:0] scale_q,
  output logic                  out_valid,
  output logic [LANES*OUTW-1:0] out_packed
);
  expert_decompressor #(.NB(NB), .LANES(LANES), .SCW(SCW), .FRACW(FRACW), .OUTW(OUTW))
    u_dut (.clk(clk), .rst_n(rst_n), .in_valid(in_valid),
           .codes_packed(codes_packed), .scale_q(scale_q),
           .out_valid(out_valid), .out_packed(out_packed));
endmodule

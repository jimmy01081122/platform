// Verification wrapper: run the combinational reference and the BANKED sequential
// LRU-victim search on the SAME inputs so a testbench can prove identical victim
// selection (banked == comb == software reference) for large N.
module lru_victim_banked_tb_top #(
  parameter int N = 128,
  parameter int TSW = 16,
  parameter int B = 8
)(
  input  logic                 clk,
  input  logic                 rst_n,
  input  logic                 start,
  input  logic [N-1:0]         valid,
  input  logic [N*TSW-1:0]     ts_flat,
  output logic                 comb_found,
  output logic [$clog2(N)-1:0] comb_victim,
  output logic                 bank_busy,
  output logic                 bank_done,
  output logic                 bank_found,
  output logic [$clog2(N)-1:0] bank_victim
);
  logic [N-1:0][TSW-1:0] ts;
  genvar g;
  generate for (g = 0; g < N; g++) begin : g_unpack
    assign ts[g] = ts_flat[g*TSW +: TSW];
  end endgenerate

  lru_victim_comb #(.N(N), .TSW(TSW)) u_comb (
    .valid(valid), .ts(ts), .found(comb_found), .victim(comb_victim));

  lru_victim_banked #(.N(N), .TSW(TSW), .B(B)) u_bank (
    .clk(clk), .rst_n(rst_n), .start(start), .valid(valid), .ts(ts),
    .busy(bank_busy), .done(bank_done), .found(bank_found), .victim(bank_victim));
endmodule

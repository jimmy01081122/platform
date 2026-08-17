// Verification wrapper: run the combinational and sequential LRU-victim search on
// the SAME inputs so a testbench can prove identical victim selection.
module lru_victim_tb_top #(
  parameter int N = 32,
  parameter int TSW = 16
)(
  input  logic                 clk,
  input  logic                 rst_n,
  input  logic                 start,
  input  logic [N-1:0]         valid,
  input  logic [N*TSW-1:0]     ts_flat,
  output logic                 comb_found,
  output logic [$clog2(N)-1:0] comb_victim,
  output logic                 seq_busy,
  output logic                 seq_done,
  output logic                 seq_found,
  output logic [$clog2(N)-1:0] seq_victim
);
  logic [N-1:0][TSW-1:0] ts;
  genvar g;
  generate for (g = 0; g < N; g++) begin : g_unpack
    assign ts[g] = ts_flat[g*TSW +: TSW];
  end endgenerate

  lru_victim_comb #(.N(N), .TSW(TSW)) u_comb (
    .valid(valid), .ts(ts), .found(comb_found), .victim(comb_victim));

  lru_victim_seq #(.N(N), .TSW(TSW)) u_seq (
    .clk(clk), .rst_n(rst_n), .start(start), .valid(valid), .ts(ts),
    .busy(seq_busy), .done(seq_done), .found(seq_found), .victim(seq_victim));
endmodule

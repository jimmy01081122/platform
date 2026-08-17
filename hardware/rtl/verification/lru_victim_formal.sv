// Formal equivalence harness for the LRU victim search.
//
// Proves (via Yosys native `sat`, no external SMT solver) that the multi-cycle
// sequential argmins select the SAME victim as the single-cycle combinational
// reference `lru_victim_comb`, for ALL inputs -- upgrading the 2000-trial random
// equivalence (tb_lru_victim_banked.cpp) to a complete bounded proof.
//
// Method: the free primary inputs valid_i/ts_i are LATCHED at the first post-reset
// cycle into vq/tsq and never change (this both (a) satisfies the DUT contract that
// valid/ts are stable from start..done, and (b) lets one BMC to the FSM's bounded
// termination depth cover every possible input, since only the cycle-0 latched
// values are observable). A one-shot `start` launches the sequential DUT; at its
// `done` we assert equality with the combinational reference on the same inputs.
//
// Reset is generated INTERNALLY (initialized counter -> rstn low for 2 cycles) so
// the BMC has a well-defined initial state without treating rst_n as a free input.
//
// Select the sequential DUT with a define (default = banked):
//   +define+FORMAL_SEQ  -> lru_victim_seq   (else lru_victim_banked)
`ifndef FN
  `define FN 16
`endif
`ifndef FTSW
  `define FTSW 4
`endif
`ifndef FB
  `define FB 4
`endif
module lru_victim_formal #(
  parameter int N   = `FN,
  parameter int TSW = `FTSW,
  parameter int B   = `FB
)(
  input logic             clk,
  input logic [N-1:0]     valid_i,
  input logic [N*TSW-1:0] ts_i_flat
);
  localparam int IW = $clog2(N);

  logic [N-1:0][TSW-1:0] ts_i;
  genvar g;
  generate for (g = 0; g < N; g++) begin : g_unpack
    assign ts_i[g] = ts_i_flat[g*TSW +: TSW];
  end endgenerate

  // internal reset generator: rstn = 0 for the first two cycles, then 1 forever.
  logic [1:0] rstcnt = 2'b00;   // init value used as BMC step-0 state
  always_ff @(posedge clk) if (rstcnt != 2'b11) rstcnt <= rstcnt + 2'b01;
  wire rstn = (rstcnt >= 2'b10);

  // one-shot start + latch inputs stable at the first post-reset cycle
  logic                  started, start_p;
  logic [N-1:0]          vq;
  logic [N-1:0][TSW-1:0] tsq;
  always_ff @(posedge clk or negedge rstn) begin
    if (!rstn) begin
      started <= 1'b0; start_p <= 1'b0; vq <= '0; tsq <= '0;
    end else begin
      if (!started) begin
        started <= 1'b1; start_p <= 1'b1; vq <= valid_i; tsq <= ts_i;
      end else begin
        start_p <= 1'b0;
      end
    end
  end

  // combinational reference on the latched inputs
  logic          c_found;
  logic [IW-1:0] c_victim;
  lru_victim_comb #(.N(N), .TSW(TSW)) u_c (
    .valid(vq), .ts(tsq), .found(c_found), .victim(c_victim));

  // sequential DUT under proof
  logic          d_busy, d_done, d_found;
  logic [IW-1:0] d_victim;
`ifdef FORMAL_SEQ
  lru_victim_seq #(.N(N), .TSW(TSW)) u_d (
    .clk(clk), .rst_n(rstn), .start(start_p), .valid(vq), .ts(tsq),
    .busy(d_busy), .done(d_done), .found(d_found), .victim(d_victim));
`else
  lru_victim_banked #(.N(N), .TSW(TSW), .B(B)) u_d (
    .clk(clk), .rst_n(rstn), .start(start_p), .valid(vq), .ts(tsq),
    .busy(d_busy), .done(d_done), .found(d_found), .victim(d_victim));
`endif

  // Property as a kept signal (sv2v strips SV assertions, so we prove a signal with
  // Yosys `sat -prove prop_ok 1` instead): prop_ok holds unless, at the sequential
  // DUT's done, its selection disagrees with the combinational reference.
  (* keep *) wire prop_ok =
      !(rstn && d_done) ||
      ((d_found == c_found) && (!c_found || (d_victim == c_victim)));

  // observability: kept so a SAT reachability check can confirm `done` is reachable
  // (guards against a vacuous proof where the property is never actually exercised).
  (* keep *) wire d_done_obs = rstn & d_done;
endmodule

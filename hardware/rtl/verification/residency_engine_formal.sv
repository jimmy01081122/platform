// Formal safety-property harness for the full residency_engine (observable ports
// only -- no internal taps, so it holds for any argmin build). Proved with Yosys
// native `sat` BMC (minisat), same flow as lru_victim_formal (D-051).
//
// Config is fixed to a valid point (num_experts, capacity); the step interface and
// the DMA/consumer handshakes (in_valid, masks, out_ready, dma_req_ready) are FREE
// each cycle so the proof covers all environments. Reset is generated internally.
//
// Properties (kept signals, proved == 1 for all reachable states to the BMC depth):
//   prop_hs          : DMA request stability -- once dma_req_valid is asserted it stays
//                      asserted with the SAME expert/kind until dma_req_ready (no
//                      dropping/mutating an in-flight copy request; required to safely
//                      drive a real copy engine).
//   prop_mono        : event counters (misses, transfers) are monotonic non-decreasing.
//   prop_miss_le_xfer: demand misses never exceed transfers (every miss => a transfer).
//   prop_no_cfg_err  : with a valid fixed config, o_input_error is never raised.
`ifndef FNE
  `define FNE 8
`endif
`ifndef FCAP
  `define FCAP 4
`endif
module residency_engine_formal (
  input logic                          clk,
  input logic                          in_valid_i,
  input logic [moe_pkg::MAX_EXPERTS-1:0] cur_i,
  input logic [moe_pkg::MAX_EXPERTS-1:0] fut_i,
  input logic                          out_ready_i,
  input logic                          dma_ready_i
);
  import moe_pkg::*;

  // internal reset: low for 2 cycles then high (init value => defined BMC step 0)
  logic [1:0] rstcnt = 2'b00;
  always_ff @(posedge clk) if (rstcnt != 2'b11) rstcnt <= rstcnt + 2'b01;
  wire rstn = (rstcnt >= 2'b10);

  localparam int NE  = `FNE;
  localparam int CAP = `FCAP;
  wire [EID_W:0] cfg_ne  = NE[EID_W:0];
  wire [EID_W:0] cfg_cap = CAP[EID_W:0];

  logic             in_ready, out_valid, ierr;
  logic [CNT_W-1:0] mmiss, pfh, xfer, evict, waste;
  logic             dreq_valid;
  logic [EID_W-1:0] dreq_expert;
  dma_kind_e        dreq_kind;
  wire              dreq_ready = dma_ready_i;

  residency_engine u_eng (
    .clk(clk), .rst_n(rstn),
    .cfg_num_experts(cfg_ne), .cfg_capacity(cfg_cap),
    .in_valid(in_valid_i), .in_ready(in_ready),
    .in_cur_mask(cur_i), .in_fut_mask(fut_i),
    .out_valid(out_valid), .out_ready(out_ready_i),
    .o_demand_misses(mmiss), .o_prefetch_hits(pfh), .o_transfers(xfer),
    .o_evictions(evict), .o_wasted_prefetches(waste), .o_input_error(ierr),
    .dma_req_valid(dreq_valid), .dma_req_ready(dreq_ready),
    .dma_req_expert(dreq_expert), .dma_req_kind(dreq_kind));

  // one-cycle history for the temporal (stability/monotonicity) properties
  logic             past_valid, pv, pr;
  logic [EID_W-1:0] pe;
  dma_kind_e        pk;
  logic [CNT_W-1:0] p_miss, p_xfer;
  always_ff @(posedge clk or negedge rstn) begin
    if (!rstn) begin
      past_valid <= 1'b0; pv <= 1'b0; pr <= 1'b0; pe <= '0; pk <= DMA_DEMAND;
      p_miss <= '0; p_xfer <= '0;
    end else begin
      past_valid <= 1'b1;
      pv <= dreq_valid; pr <= dreq_ready; pe <= dreq_expert; pk <= dreq_kind;
      p_miss <= mmiss; p_xfer <= xfer;
    end
  end

  (* keep *) wire prop_hs =
      !past_valid || !(pv && !pr) ||
      (dreq_valid && (dreq_expert == pe) && (dreq_kind == pk));
  (* keep *) wire prop_mono =
      !past_valid || ((mmiss >= p_miss) && (xfer >= p_xfer));
  (* keep *) wire prop_miss_le_xfer = (mmiss <= xfer);
  (* keep *) wire prop_no_cfg_err   = !rstn || !ierr;

  (* keep *) wire prop_all =
      prop_hs && prop_mono && prop_miss_le_xfer && prop_no_cfg_err;

  // non-vacuity observability: a stalled handshake (valid & !ready) is reachable,
  // so prop_hs is actually exercised rather than trivially true.
  (* keep *) wire stall_obs = rstn & dreq_valid & ~dreq_ready;
endmodule

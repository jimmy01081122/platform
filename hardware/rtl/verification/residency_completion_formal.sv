// Small-parameter formal harness for the engine side of tagged DMA completion.
`ifndef FNE
  `define FNE 4
`endif
`ifndef FCAP
  `define FCAP 2
`endif

module residency_completion_formal (
  input logic                              clk,
  input logic                              in_valid_i,
  input logic [moe_pkg::MAX_EXPERTS-1:0]  cur_i,
  input logic [moe_pkg::MAX_EXPERTS-1:0]  fut_i,
  input logic                              out_ready_i,
  input logic                              req_ready_i,
  input logic                              cmpl_valid_i,
  input logic [moe_pkg::DMA_TAG_W-1:0]     cmpl_tag_i,
  input logic [moe_pkg::EID_W-1:0]         cmpl_expert_i,
  input moe_pkg::dma_kind_e                cmpl_kind_i
);
  import moe_pkg::*;
  localparam int NE = `FNE;
  localparam int CAP = `FCAP;

  logic [1:0] rstcnt = 2'b00;
  always_ff @(posedge clk)
    if (rstcnt != 2'b11) rstcnt <= rstcnt + 2'b01;
  wire rstn = (rstcnt >= 2'b10);

  logic in_ready, out_valid, input_error;
  logic [CNT_W-1:0] misses, hits, transfers, evictions, waste;
  logic req_valid, cmpl_ready;
  logic [DMA_TAG_W-1:0] req_tag;
  logic [EID_W-1:0] req_expert;
  dma_kind_e req_kind;

  residency_engine u_eng (
    .clk(clk), .rst_n(rstn),
    .cfg_num_experts(NE[EID_W:0]), .cfg_capacity(CAP[EID_W:0]),
    .in_valid(in_valid_i), .in_ready(in_ready),
    .in_cur_mask(cur_i), .in_fut_mask(fut_i),
    .out_valid(out_valid), .out_ready(out_ready_i),
    .o_demand_misses(misses), .o_prefetch_hits(hits),
    .o_transfers(transfers), .o_evictions(evictions),
    .o_wasted_prefetches(waste), .o_input_error(input_error),
    .dma_req_valid(req_valid), .dma_req_ready(req_ready_i),
    .dma_req_tag(req_tag), .dma_req_expert(req_expert),
    .dma_req_kind(req_kind),
    .dma_cmpl_valid(cmpl_valid_i), .dma_cmpl_ready(cmpl_ready),
    .dma_cmpl_tag(cmpl_tag_i), .dma_cmpl_expert(cmpl_expert_i),
    .dma_cmpl_kind(cmpl_kind_i)
  );

  wire req_fire = req_valid && req_ready_i;
  wire cmpl_fire = cmpl_valid_i && cmpl_ready;

  logic outstanding;
  logic [DMA_TAG_W-1:0] outstanding_tag;
  logic [EID_W-1:0] outstanding_expert;
  dma_kind_e outstanding_kind;
  logic [CNT_W-1:0] accepted, completed;

  always_ff @(posedge clk or negedge rstn) begin
    if (!rstn) begin
      outstanding <= 1'b0;
      outstanding_tag <= '0;
      outstanding_expert <= '0;
      outstanding_kind <= DMA_DEMAND;
      accepted <= '0;
      completed <= '0;
    end else begin
      if (req_fire) begin
        outstanding <= 1'b1;
        outstanding_tag <= req_tag;
        outstanding_expert <= req_expert;
        outstanding_kind <= req_kind;
        accepted <= accepted + 1'b1;
      end
      if (cmpl_fire) begin
        outstanding <= 1'b0;
        completed <= completed + 1'b1;
      end
    end
  end

  (* keep *) wire prop_single_outstanding = accepted == completed + outstanding;
  (* keep *) wire prop_completion_matches =
      !cmpl_fire ||
      (outstanding && cmpl_tag_i == outstanding_tag &&
       cmpl_expert_i == outstanding_expert &&
       cmpl_kind_i == outstanding_kind);
  (* keep *) wire prop_all =
      prop_single_outstanding && prop_completion_matches;

  (* keep *) wire request_obs = rstn && req_fire;
  (* keep *) wire completion_obs = rstn && cmpl_fire;
endmodule

import moe_pkg::*;

// Verification-only wrapper exposing the resident bitmap for the
// completion-before-resident checker. No synthesizable DUT port is changed.
module residency_engine_dv_top (
  input  logic                     clk,
  input  logic                     rst_n,
  input  logic [EID_W:0]           cfg_num_experts,
  input  logic [EID_W:0]           cfg_capacity,
  input  logic                     in_valid,
  output logic                     in_ready,
  input  logic [MAX_EXPERTS-1:0]   in_cur_mask,
  input  logic [MAX_EXPERTS-1:0]   in_fut_mask,
  output logic                     out_valid,
  input  logic                     out_ready,
  output logic [CNT_W-1:0]         o_demand_misses,
  output logic [CNT_W-1:0]         o_prefetch_hits,
  output logic [CNT_W-1:0]         o_transfers,
  output logic [CNT_W-1:0]         o_evictions,
  output logic [CNT_W-1:0]         o_wasted_prefetches,
  output logic                     o_input_error,
  output logic                     dma_req_valid,
  input  logic                     dma_req_ready,
  output logic [DMA_TAG_W-1:0]     dma_req_tag,
  output logic [EID_W-1:0]         dma_req_expert,
  output dma_kind_e                dma_req_kind,
  input  logic                     dma_cmpl_valid,
  output logic                     dma_cmpl_ready,
  input  logic [DMA_TAG_W-1:0]     dma_cmpl_tag,
  input  logic [EID_W-1:0]         dma_cmpl_expert,
  input  dma_kind_e                dma_cmpl_kind,
  output logic [MAX_EXPERTS-1:0]   dbg_resident
);
  residency_engine u_dut (
    .clk, .rst_n, .cfg_num_experts, .cfg_capacity,
    .in_valid, .in_ready, .in_cur_mask, .in_fut_mask,
    .out_valid, .out_ready, .o_demand_misses, .o_prefetch_hits,
    .o_transfers, .o_evictions, .o_wasted_prefetches, .o_input_error,
    .dma_req_valid, .dma_req_ready, .dma_req_tag, .dma_req_expert,
    .dma_req_kind, .dma_cmpl_valid, .dma_cmpl_ready, .dma_cmpl_tag,
    .dma_cmpl_expert, .dma_cmpl_kind
  );

  assign dbg_resident = u_dut.resident;

  completion_before_resident_assertions #(
    .NUM_EXPERTS(MAX_EXPERTS),
    .EID_W(EID_W)
  ) u_completion_before_resident (
    .clk(clk),
    .rst_n(rst_n),
    .completion_fire(dma_cmpl_valid && dma_cmpl_ready),
    .completion_expert(dma_cmpl_expert),
    .resident(dbg_resident)
  );
endmodule

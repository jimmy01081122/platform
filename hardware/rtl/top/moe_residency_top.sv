// Synthesis-elaboratable top: residency/prefetch engine + DMA interface model.
// Exposes the step transaction interface and cumulative counters. External
// CPU/GPU/DRAM/link are represented by dma_model (latency/capacity/backpressure).
import moe_pkg::*;

module moe_residency_top #(
  parameter int DMA_LATENCY = 8,
  parameter int DMA_DEPTH   = 4
)(
  input  logic                   clk,
  input  logic                   rst_n,
  input  logic [EID_W:0]         cfg_num_experts,
  input  logic [EID_W:0]         cfg_capacity,
  input  logic                   in_valid,
  output logic                   in_ready,
  input  logic [MAX_EXPERTS-1:0] in_cur_mask,
  input  logic [MAX_EXPERTS-1:0] in_fut_mask,
  output logic                   out_valid,
  input  logic                   out_ready,
  output logic [CNT_W-1:0]       o_demand_misses,
  output logic [CNT_W-1:0]       o_prefetch_hits,
  output logic [CNT_W-1:0]       o_transfers,
  output logic [CNT_W-1:0]       o_evictions,
  output logic [CNT_W-1:0]       o_wasted_prefetches,
  output logic                   o_input_error,
  output logic [CNT_W-1:0]       o_dma_completions,
  output logic [CNT_W-1:0]       o_dma_occupancy
);
  logic             dma_req_valid, dma_req_ready;
  logic [DMA_TAG_W-1:0] dma_req_tag;
  logic [EID_W-1:0] dma_req_expert;
  dma_kind_e        dma_req_kind;
  logic             cmpl_valid, cmpl_ready;
  logic [DMA_TAG_W-1:0] cmpl_tag;
  logic [EID_W-1:0] cmpl_expert;
  dma_kind_e        cmpl_kind;

  residency_engine u_eng (
    .clk, .rst_n,
    .cfg_num_experts, .cfg_capacity,
    .in_valid, .in_ready, .in_cur_mask, .in_fut_mask,
    .out_valid, .out_ready,
    .o_demand_misses, .o_prefetch_hits, .o_transfers,
    .o_evictions, .o_wasted_prefetches, .o_input_error,
    .dma_req_valid, .dma_req_ready, .dma_req_tag, .dma_req_expert, .dma_req_kind,
    .dma_cmpl_valid(cmpl_valid), .dma_cmpl_ready(cmpl_ready),
    .dma_cmpl_tag(cmpl_tag), .dma_cmpl_expert(cmpl_expert),
    .dma_cmpl_kind(cmpl_kind)
  );

  dma_model #(.LATENCY(DMA_LATENCY), .DEPTH(DMA_DEPTH)) u_dma (
    .clk, .rst_n,
    .req_valid(dma_req_valid), .req_ready(dma_req_ready),
    .req_tag(dma_req_tag), .req_expert(dma_req_expert), .req_kind(dma_req_kind),
    .cmpl_valid(cmpl_valid), .cmpl_ready(cmpl_ready),
    .cmpl_tag(cmpl_tag), .cmpl_expert(cmpl_expert), .cmpl_kind(cmpl_kind),
    .o_completions(o_dma_completions), .o_occupancy(o_dma_occupancy)
  );
endmodule

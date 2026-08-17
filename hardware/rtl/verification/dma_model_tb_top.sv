import moe_pkg::*;

// Verification-only wrapper. The occupancy observation is deliberately kept
// out of the synthesizable DMA interface.
module dma_model_tb_top #(
  parameter int LATENCY = 2,
  parameter int DEPTH = 4,
  parameter int LATENCY_JITTER = 7
) (
  input  logic                 clk,
  input  logic                 rst_n,
  input  logic                 req_valid,
  output logic                 req_ready,
  input  logic [DMA_TAG_W-1:0] req_tag,
  input  logic [EID_W-1:0]     req_expert,
  input  dma_kind_e            req_kind,
  output logic                 cmpl_valid,
  input  logic                 cmpl_ready,
  output logic [DMA_TAG_W-1:0] cmpl_tag,
  output logic [EID_W-1:0]     cmpl_expert,
  output dma_kind_e            cmpl_kind,
  output logic [CNT_W-1:0]     o_completions,
  output logic [CNT_W-1:0]     o_occupancy
);
  dma_model #(
    .LATENCY(LATENCY),
    .DEPTH(DEPTH),
    .LATENCY_JITTER(LATENCY_JITTER)
  ) u_dma (.*);
endmodule

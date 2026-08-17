import moe_pkg::*;

module dma_model_assert_adapter #(
  parameter int DEPTH = 4
) (
  input logic clk,
  input logic rst_n,
  input logic req_valid,
  input logic req_ready,
  input logic [DMA_TAG_W-1:0] req_tag,
  input logic [EID_W-1:0] req_expert,
  input dma_kind_e req_kind,
  input logic cmpl_valid,
  input logic cmpl_ready,
  input logic [DMA_TAG_W-1:0] cmpl_tag,
  input logic [EID_W-1:0] cmpl_expert,
  input dma_kind_e cmpl_kind,
  input logic [CNT_W-1:0] occupancy
);
  ready_valid_fifo_assertions #(
    .PAYLOAD_W(DMA_TAG_W + EID_W + 1),
    .DEPTH(DEPTH + 1),
    .OCC_W(CNT_W)
  ) u_dma_protocol_assertions (
    .clk(clk),
    .rst_n(rst_n),
    .in_valid(req_valid),
    .in_ready(req_ready),
    .in_payload({req_tag, req_expert, req_kind}),
    .out_valid(cmpl_valid),
    .out_ready(cmpl_ready),
    .out_payload({cmpl_tag, cmpl_expert, cmpl_kind}),
    .occupancy(occupancy)
  );
endmodule

// Keep the checker outside dma_model so the synthesizable model is unchanged.
bind dma_model dma_model_assert_adapter #(
  .DEPTH(DEPTH)
) u_dma_assert_adapter (
  .clk(clk),
  .rst_n(rst_n),
  .req_valid(req_valid),
  .req_ready(req_ready),
  .req_tag(req_tag),
  .req_expert(req_expert),
  .req_kind(req_kind),
  .cmpl_valid(cmpl_valid),
  .cmpl_ready(cmpl_ready),
  .cmpl_tag(cmpl_tag),
  .cmpl_expert(cmpl_expert),
  .cmpl_kind(cmpl_kind),
  .occupancy(o_occupancy)
);

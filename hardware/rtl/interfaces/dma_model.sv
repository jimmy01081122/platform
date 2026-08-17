// Simple DMA / memory interface model: finite outstanding capacity + fixed
// transfer latency + completion port + backpressure. Represents the external
// link/DMA (PCIe copy engine on P-D; shared-memory path on P-I). It does not
// move real bytes; it models request acceptance, latency, ordering, capacity,
// and backpressure so the engine's control path is exercised realistically.
import moe_pkg::*;

module dma_model #(
  parameter int LATENCY        = 8,
  parameter int DEPTH          = 4,
  parameter int LATENCY_JITTER = 0
)(
  input  logic             clk,
  input  logic             rst_n,
  input  logic             req_valid,
  output logic             req_ready,
  input  logic [DMA_TAG_W-1:0] req_tag,
  input  logic [EID_W-1:0] req_expert,
  input  dma_kind_e        req_kind,
  output logic             cmpl_valid,
  input  logic             cmpl_ready,
  output logic [DMA_TAG_W-1:0] cmpl_tag,
  output logic [EID_W-1:0] cmpl_expert,
  output dma_kind_e        cmpl_kind,
  output logic [CNT_W-1:0] o_completions,
  output logic [CNT_W-1:0] o_occupancy
);
  localparam int SLOT_W = (DEPTH <= 1) ? 1 : $clog2(DEPTH);

  logic [DEPTH-1:0]              busy;
  logic [DEPTH-1:0][DMA_TAG_W-1:0] slot_tag;
  logic [DEPTH-1:0][EID_W-1:0]  slot_e;
  dma_kind_e                     slot_kind [DEPTH-1:0];
  logic [DEPTH-1:0][31:0]       slot_t;
  logic [CNT_W-1:0]             completions;

  logic       free_found;
  logic [SLOT_W-1:0] free_idx;
  always_comb begin
    free_found = 1'b0; free_idx = '0;
    for (int k = DEPTH-1; k >= 0; k--) if (!busy[k]) begin
      free_found = 1'b1;
      free_idx = k[SLOT_W-1:0];
    end
  end

  logic       due_found;
  logic [SLOT_W-1:0] due_idx;
  always_comb begin
    due_found = 1'b0; due_idx = '0;
    for (int k = DEPTH-1; k >= 0; k--) if (busy[k] && (slot_t[k] == 0)) begin
      due_found = 1'b1;
      due_idx = k[SLOT_W-1:0];
    end
  end

  assign req_ready     = free_found;
  assign o_completions = completions;
  always_comb begin
    o_occupancy = {{(CNT_W-1){1'b0}}, cmpl_valid};
    for (int k = 0; k < DEPTH; k++)
      o_occupancy = o_occupancy + {{(CNT_W-1){1'b0}}, busy[k]};
  end

  integer i;
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      cmpl_valid <= 1'b0;
      cmpl_tag <= '0;
      cmpl_expert <= '0;
      cmpl_kind <= DMA_DEMAND;
      completions <= '0;
      for (i = 0; i < DEPTH; i++) begin
        busy[i] <= 1'b0;
        slot_tag[i] <= '0;
        slot_e[i] <= '0;
        slot_kind[i] <= DMA_DEMAND;
        slot_t[i] <= '0;
      end
    end else begin
      // The completion register is a one-entry ready/valid FIFO. It remains
      // stable under backpressure and can be replaced on the same cycle that
      // the current completion handshakes.
      if (cmpl_valid && cmpl_ready) begin
        cmpl_valid <= 1'b0;
        completions <= completions + 1'b1;
      end

      for (i = 0; i < DEPTH; i++) begin
        if (busy[i] && (slot_t[i] != 0))
          slot_t[i] <= slot_t[i] - 1'b1;
      end

      // Move exactly one mature slot into the completion FIFO. Other mature
      // slots stay busy and are selected on later cycles.
      if ((!cmpl_valid || cmpl_ready) && due_found) begin
        cmpl_valid  <= 1'b1;
        cmpl_tag    <= slot_tag[due_idx];
        cmpl_expert <= slot_e[due_idx];
        cmpl_kind   <= slot_kind[due_idx];
        busy[due_idx] <= 1'b0;
      end

      // accept a new request into a free slot
      if (req_valid && req_ready) begin
        busy[free_idx]   <= 1'b1;
        slot_tag[free_idx] <= req_tag;
        slot_e[free_idx] <= req_expert;
        slot_kind[free_idx] <= req_kind;
        if (LATENCY_JITTER > 0)
          slot_t[free_idx] <= LATENCY[31:0] +
                              ({{(32-DMA_TAG_W){1'b0}}, req_tag} %
                               (LATENCY_JITTER + 1));
        else
          slot_t[free_idx] <= LATENCY[31:0];
      end
    end
  end
endmodule

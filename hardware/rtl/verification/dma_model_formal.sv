// Small-parameter safety harness for dma_model.
//
// The formal build reduces TAG_W and tracks every possible tag. This proves
// no spurious/duplicate completion and exact payload matching under arbitrary
// request/completion backpressure within the bounded horizon.
module dma_model_formal (
  input logic                          clk,
  input logic                          req_valid_i,
  input logic [moe_pkg::DMA_TAG_W-1:0] req_tag_i,
  input logic [moe_pkg::EID_W-1:0]     req_expert_i,
  input moe_pkg::dma_kind_e            req_kind_i,
  input logic                          cmpl_ready_i
);
  import moe_pkg::*;
  localparam int FDEPTH = 3;
  localparam int FTAGS = 1 << DMA_TAG_W;

  logic [1:0] rstcnt = 2'b00;
  always_ff @(posedge clk)
    if (rstcnt != 2'b11) rstcnt <= rstcnt + 2'b01;
  wire rstn = (rstcnt >= 2'b10);

  logic req_ready;
  logic cmpl_valid;
  logic [DMA_TAG_W-1:0] cmpl_tag;
  logic [EID_W-1:0] cmpl_expert;
  dma_kind_e cmpl_kind;
  logic [CNT_W-1:0] completions;
  logic [CNT_W-1:0] occupancy;
  logic [CNT_W-1:0] accepted;
  wire [DMA_TAG_W-1:0] formal_req_tag =
      accepted[DMA_TAG_W-1:0];

  dma_model #(
    .LATENCY(2),
    .DEPTH(FDEPTH),
    .LATENCY_JITTER(2)
  ) u_dma (
    .clk(clk),
    .rst_n(rstn),
    .req_valid(req_valid_i),
    .req_ready(req_ready),
    .req_tag(formal_req_tag),
    .req_expert(req_expert_i),
    .req_kind(req_kind_i),
    .cmpl_valid(cmpl_valid),
    .cmpl_ready(cmpl_ready_i),
    .cmpl_tag(cmpl_tag),
    .cmpl_expert(cmpl_expert),
    .cmpl_kind(cmpl_kind),
    .o_completions(completions),
    .o_occupancy(occupancy)
  );

  wire req_fire = req_valid_i && req_ready;
  wire cmpl_fire = cmpl_valid && cmpl_ready_i;

  logic past_valid;
  logic p_stalled;
  logic [DMA_TAG_W-1:0] p_tag;
  logic [EID_W-1:0] p_expert;
  dma_kind_e p_kind;
  logic [FTAGS-1:0] outstanding;
  logic [FTAGS-1:0][EID_W-1:0] expected_expert;
  logic [FTAGS-1:0] expected_kind;
  logic tag_violation;
  logic [FTAGS-1:0] outstanding_next;

  always_comb begin
    outstanding_next = outstanding;
    if (cmpl_fire)
      outstanding_next[cmpl_tag] = 1'b0;
    // A tag may be retired and reissued in the same cycle.  Apply retirement
    // first so the newly accepted transaction remains outstanding.
    if (req_fire)
      outstanding_next[formal_req_tag] = 1'b1;
  end

  always_ff @(posedge clk or negedge rstn) begin
    if (!rstn) begin
      accepted <= '0;
      past_valid <= 1'b0;
      p_stalled <= 1'b0;
      p_tag <= '0;
      p_expert <= '0;
      p_kind <= DMA_DEMAND;
      outstanding <= '0;
      expected_expert <= '0;
      expected_kind <= '0;
      tag_violation <= 1'b0;
    end else begin
      outstanding <= outstanding_next;
      if (req_fire) begin
        accepted <= accepted + 1'b1;
        expected_expert[formal_req_tag] <= req_expert_i;
        expected_kind[formal_req_tag] <= req_kind_i;
      end
      if (cmpl_fire) begin
        if (!outstanding[cmpl_tag] ||
            cmpl_expert != expected_expert[cmpl_tag] ||
            cmpl_kind != expected_kind[cmpl_tag])
          tag_violation <= 1'b1;
      end
      past_valid <= 1'b1;
      p_stalled <= cmpl_valid && !cmpl_ready_i;
      p_tag <= cmpl_tag;
      p_expert <= cmpl_expert;
      p_kind <= cmpl_kind;
    end
  end

  (* keep *) wire prop_conservation =
      accepted == (completions + occupancy);
  (* keep *) wire prop_no_underflow = completions <= accepted;
  (* keep *) wire prop_completion_stable =
      !past_valid || !p_stalled ||
      (cmpl_valid && cmpl_tag == p_tag && cmpl_expert == p_expert &&
       cmpl_kind == p_kind);
  (* keep *) wire prop_watch_completion = !tag_violation;
  (* keep *) wire prop_all =
      prop_conservation && prop_no_underflow &&
      prop_completion_stable && prop_watch_completion;

  (* keep *) wire stall_obs = rstn && cmpl_valid && !cmpl_ready_i;
  (* keep *) wire watched_completion_obs = rstn && cmpl_fire;
endmodule

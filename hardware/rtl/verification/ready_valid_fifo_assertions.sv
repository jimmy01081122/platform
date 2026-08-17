// Bindable ready/valid FIFO protocol checker.
//
// `occupancy` counts every accepted item that has not completed its output
// handshake, including an item currently held on a stalled output.
module ready_valid_fifo_assertions #(
  parameter int PAYLOAD_W = 1,
  parameter int DEPTH = 1,
  parameter int OCC_W = (DEPTH < 2) ? 1 : $clog2(DEPTH + 1)
) (
  input logic                 clk,
  input logic                 rst_n,
  input logic                 in_valid,
  input logic                 in_ready,
  input logic [PAYLOAD_W-1:0] in_payload,
  input logic                 out_valid,
  input logic                 out_ready,
  input logic [PAYLOAD_W-1:0] out_payload,
  input logic [OCC_W-1:0]     occupancy
);
  logic [OCC_W-1:0] expected_occupancy;
  wire push = in_valid && in_ready;
  wire pop  = out_valid && out_ready;

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) expected_occupancy <= '0;
    else begin
      case ({push, pop})
        2'b10: expected_occupancy <= expected_occupancy + 1'b1;
        2'b01: expected_occupancy <= expected_occupancy - 1'b1;
        default: expected_occupancy <= expected_occupancy;
      endcase
    end
  end

  ap_input_stable:
    assert property (@(posedge clk) disable iff (!rst_n)
                     in_valid && !in_ready |=> in_valid &&
                     $stable(in_payload));
  ap_output_stable:
    assert property (@(posedge clk) disable iff (!rst_n)
                     out_valid && !out_ready |=> out_valid &&
                     $stable(out_payload));
  ap_no_underflow:
    assert property (@(posedge clk) disable iff (!rst_n)
                     pop |-> occupancy != 0);
  ap_no_overflow:
    assert property (@(posedge clk) disable iff (!rst_n)
                     push && !pop |-> occupancy < DEPTH);
  ap_occupancy_conservation:
    assert property (@(posedge clk) disable iff (!rst_n)
                     occupancy == expected_occupancy);

  cp_input_stall:
    cover property (@(posedge clk) disable iff (!rst_n)
                    in_valid && !in_ready);
  cp_output_stall:
    cover property (@(posedge clk) disable iff (!rst_n)
                    out_valid && !out_ready);
  cp_simultaneous_push_pop:
    cover property (@(posedge clk) disable iff (!rst_n)
                    push && pop);
  cp_full:
    cover property (@(posedge clk) disable iff (!rst_n)
                    occupancy == DEPTH);
endmodule

// Integration checker for the residency contract. A 0->1 resident transition
// must consume a prior completion credit for that expert; accepting a DMA
// request alone is not sufficient to make the expert resident.
module completion_before_resident_assertions #(
  parameter int NUM_EXPERTS = 4,
  parameter int EID_W = (NUM_EXPERTS < 2) ? 1 : $clog2(NUM_EXPERTS),
  parameter int CREDIT_W = 3
) (
  input logic                   clk,
  input logic                   rst_n,
  input logic                   completion_fire,
  input logic [EID_W-1:0]       completion_expert,
  input logic [NUM_EXPERTS-1:0] resident
);
  logic [NUM_EXPERTS-1:0] resident_q;
  logic [CREDIT_W-1:0] completion_credit [NUM_EXPERTS];

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      resident_q <= '0;
      for (int e = 0; e < NUM_EXPERTS; e++)
        completion_credit[e] <= '0;
    end else begin
      for (int e = 0; e < NUM_EXPERTS; e++) begin
        if (resident[e] && !resident_q[e]) begin
          assert ((completion_credit[e] != 0) ||
                  (completion_fire && completion_expert == e[EID_W-1:0]))
            else $error("resident[%0d] rose before DMA completion", e);
          if (!(completion_fire && completion_expert == e[EID_W-1:0]))
            completion_credit[e] <= completion_credit[e] - 1'b1;
        end
      end
      if (completion_fire && !resident[completion_expert])
        completion_credit[completion_expert] <=
            completion_credit[completion_expert] + 1'b1;
      resident_q <= resident;
    end
  end
endmodule

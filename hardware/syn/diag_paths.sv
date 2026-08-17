// Timing probes (NOT part of the design): isolate the engine's big combinational
// reductions so gate-level STA can pin the dominant path in the sequential-argmin
// engine (S6++ found ~6.26 ns > the 4.23 ns scan block -> a non-argmin path
// dominates). Combinational-only; abc stime gives input->output delay.
import moe_pkg::*;

// 32-wide input validation: any expert bit set at index >= cfg_num_experts.
module diag_input_bad (
  input  logic [MAX_EXPERTS-1:0] cur,
  input  logic [MAX_EXPERTS-1:0] fut,
  input  logic [EID_W:0]         ne,
  output logic                   bad
);
  always_comb begin
    bad = 1'b0;
    for (int k = 0; k < MAX_EXPERTS; k++)
      if ((k >= ne) && (cur[k] || fut[k])) bad = 1'b1;
  end
endmodule

// "an evictable (resident, non-protected, in-range) entry exists" OR-reduction,
// feeding pf_blocked -> need_load_pref -> dma_req_valid (a primary output).
module diag_has_evict (
  input  logic [MAX_EXPERTS-1:0] resident,
  input  logic [MAX_EXPERTS-1:0] fut,
  input  logic [EID_W:0]         ne,
  output logic                   he
);
  always_comb begin
    he = 1'b0;
    for (int k = 0; k < MAX_EXPERTS; k++)
      if ((k < ne) && resident[k] && !fut[k]) he = 1'b1;
  end
endmodule

// scan per-cycle core: last_used[si] mux (32:1 x TS_W) + compare vs running best.
module diag_scan_step (
  input  logic [MAX_EXPERTS-1:0][TS_W-1:0] last_used,
  input  logic [EID_W-1:0]                 si,
  input  logic [TS_W-1:0]                  best,
  input  logic                             found,
  input  logic [MAX_EXPERTS-1:0]           resident,
  output logic                             upd,
  output logic [TS_W-1:0]                  nbest,
  output logic [EID_W-1:0]                 nvidx
);
  always_comb begin
    upd = 1'b0; nbest = best; nvidx = si;
    if (resident[si] && (!found || last_used[si] < best)) begin
      upd = 1'b1; nbest = last_used[si]; nvidx = si;
    end
  end
endmodule

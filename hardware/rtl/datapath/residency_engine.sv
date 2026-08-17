// MoE expert residency/prefetch engine (full datapath, semantic_revision=1).
//
// Realizes the frozen algorithm (edgeflow.residency / firmware scheduler.c):
//   input step transaction {cur_mask, fut_mask}
//     -> decode & validation
//     -> residency tag table access (resident/pending/last_used)
//     -> demand phase (miss -> evict LRU -> issue DMA -> occupy slot)
//     -> prefetch phase (depth-1 via fut_mask, protective LRU)
//     -> DMA descriptor issue (with backpressure)
//     -> step-complete output transaction (cumulative counters)
//
// fut_mask = 0 gives the on-demand baseline; fut_mask = next-step experts gives
// depth-1 prefetch. capacity and num_experts are runtime-configurable. Counters
// preserve the software/firmware/reference golden values at each completed step.
// A load becomes resident, and any paired eviction commits, only after the tagged
// DMA completion handshakes; DMA latency therefore affects timing, not step results.
//
// LRU victim search (argmin of last_used over resident, non-protected entries):
//   * default build: single-cycle COMBINATIONAL argmin (throughput-optimal, but
//     the ~15-18 ns critical path at MAX_EXPERTS=32 -> S6+ timing bottleneck).
//   * `+define+MOE_SEQ_ARGMIN`: registered SEQUENTIAL scan (one entry/cycle) via
//     the S_DSCAN/S_PSCAN states -> short critical path (single mux+compare),
//     costs ~num_experts+1 extra cycles per eviction. IDENTICAL victim selection
//     (strict-less-than ascending scan -> lowest-index tie-break), so all event
//     counters stay bit-for-bit equal to the golden model.
// `+define+MOE_BANKED_ARGMIN`: like the sequential build, but the victim search is
//   the banked hierarchical argmin (rtl/datapath/lru_victim.sv, B=moe_pkg::BANKS
//   parallel banks + sequential B-way reduce). Per-cycle path ~ sqrt(N) instead of
//   N, so it holds 200 MHz at large MAX_EXPERTS where the flat scan cannot (see
//   explorations/moe_orchestration/W3_ENGINE_REDESIGN.md). Same victim selection.
import moe_pkg::*;

// MOE_HAS_SCAN = any build with a multi-cycle (registered) victim search: shares the
// registered result register, scan_stall gating and vic_ready consume with both the
// flat-sequential and the banked variants.
`ifdef MOE_SEQ_ARGMIN
  `define MOE_HAS_SCAN
`endif
`ifdef MOE_BANKED_ARGMIN
  `define MOE_HAS_SCAN
`endif

module residency_engine (
  input  logic                     clk,
  input  logic                     rst_n,

  input  logic [EID_W:0]           cfg_num_experts,   // <= MAX_EXPERTS
  input  logic [EID_W:0]           cfg_capacity,      // 1..num_experts

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
  input  dma_kind_e                dma_cmpl_kind
);

  typedef enum logic [3:0] {
    S_IDLE, S_DEMAND, S_DSCAN, S_DWAIT, S_PREFETCH, S_PSCAN, S_PWAIT,
    S_DONE, S_ERR
  } state_e;
  // NOTE (D-047, negative result): forcing one-hot FSM encoding was evaluated as the
  // textbook fix for the D-046 limiter (FSM state broadcast) but did NOT improve full-
  // engine Fmax. Apples-to-apples default-abc unbuffered STA at N=128: binary 23.92 MHz
  // vs one-hot 22.23 MHz (marginally worse); the buffered sign-off flow (abc `buffer`)
  // segfaults on the one-hot netlist in yosys 0.23. Root cause: the state net fanout is
  // structural to gating the N-wide combinational blocks, so re-encoding alone cannot
  // remove it; a real fix would restructure those blocks (register predicates / avoid
  // state-gating the N-wide reductions). Not pursued: the system is transfer-bound
  // (D-042), so binary at 129.7 MHz buffered is sufficient. State stays binary.
  state_e state;

  logic [MAX_EXPERTS-1:0] resident, pending;
  logic [MAX_EXPERTS-1:0][TS_W-1:0] last_used;   // packed 2D (synth-friendly)
  logic [TS_W-1:0]        time_ctr;
  logic [EID_W:0]         res_count;
  logic [EID_W:0]         idx;
  logic [MAX_EXPERTS-1:0] cur_q, fut_q;

  logic [CNT_W-1:0] c_miss, c_pfh, c_xfer, c_evict, c_waste;
  logic [DMA_TAG_W-1:0] next_dma_tag, wait_dma_tag;
  logic [EID_W-1:0]     wait_expert, wait_victim;
  logic                 wait_evict;

  // 5-bit index for bit-selecting the MAX_EXPERTS-wide masks/tables.
  logic [EID_W-1:0] ix;
  assign ix = idx[EID_W-1:0];

  // Cheap "an evictable (resident, non-protected, in-range) entry exists" checks.
  // Shallow OR-reduction (NOT the argmin) -> used for the prefetch-block decision
  // in BOTH builds; keeps find_victim off the pf_blocked path.
  logic has_evict_p;
  always_comb begin
    has_evict_p = 1'b0;
    for (int k = 0; k < MAX_EXPERTS; k++)
      if ((k < cfg_num_experts) && resident[k] && !fut_q[k]) has_evict_p = 1'b1;
  end

  // Victim providers (build-dependent).
  logic             vic_found_use_d, vic_found_use_p;
  logic [EID_W-1:0] vic_idx_use_d,   vic_idx_use_p;

`ifdef MOE_HAS_SCAN
  // Registered scan result (shared demand/prefetch; each eviction rescans with the
  // phase-appropriate protect mask). Common to the flat-sequential and banked builds.
  logic             vic_found_r, vic_ready;
  logic [EID_W-1:0] vic_idx_r;
  assign vic_found_use_d = vic_found_r;  assign vic_idx_use_d = vic_idx_r;
  assign vic_found_use_p = vic_found_r;  assign vic_idx_use_p = vic_idx_r;

`ifdef MOE_BANKED_ARGMIN
  // Banked hierarchical argmin: valid mask is phase-dependent (pf_scan selects the
  // prefetch protect = fut_q). last_used is read LIVE (stable during a scan -> no
  // eviction happens mid-search), matching the module's stable-input contract.
  logic                   pf_scan, bank_start, bank_busy, bank_done, bank_found;
  logic [EID_W-1:0]       bank_victim;
  logic [MAX_EXPERTS-1:0] bank_valid;
  always_comb begin
    for (int k = 0; k < MAX_EXPERTS; k++)
      bank_valid[k] = resident[k] && (k < cfg_num_experts) &&
                      (pf_scan ? !fut_q[k] : 1'b1);
  end
  lru_victim_banked #(.N(MAX_EXPERTS), .TSW(TS_W), .B(BANKS)) u_vic (
    .clk(clk), .rst_n(rst_n), .start(bank_start),
    .valid(bank_valid), .ts(last_used),
    .busy(bank_busy), .done(bank_done), .found(bank_found), .victim(bank_victim));
`ifdef MOE_BANK_DEBUG
  logic dbg_found; logic [EID_W-1:0] dbg_vidx; logic [TS_W-1:0] dbg_best;
  always_comb begin
    dbg_found = 1'b0; dbg_vidx = '0; dbg_best = '1;
    for (int k = 0; k < MAX_EXPERTS; k++)
      if (bank_valid[k] && (!dbg_found || last_used[k] < dbg_best)) begin
        dbg_found = 1'b1; dbg_best = last_used[k]; dbg_vidx = k[EID_W-1:0];
      end
  end
`endif
`else
  // Flat sequential scan state (one entry/cycle over last_used).
  logic [EID_W:0]   scan_i;
  logic [TS_W-1:0]  scan_best;
  logic             scan_found;
  logic [EID_W-1:0] scan_vidx;
  logic [EID_W-1:0] si;
  assign si = scan_i[EID_W-1:0];
`endif
`else
  // Single-cycle combinational argmin (two instances: demand + prefetch protect).
  function automatic void find_victim(
      input logic [MAX_EXPERTS-1:0] protect,
      output logic                  found,
      output logic [EID_W-1:0]      vidx);
    logic [TS_W-1:0] best;
    found = 1'b0; vidx = '0; best = '1;
    for (int k = 0; k < MAX_EXPERTS; k++) begin
      if ((k < cfg_num_experts) && resident[k] && !protect[k]) begin
        if (!found || last_used[k] < best) begin
          found = 1'b1; best = last_used[k]; vidx = k[EID_W-1:0];
        end
      end
    end
  endfunction

  logic vic_found_d, vic_found_p;
  logic [EID_W-1:0] vic_idx_d, vic_idx_p;
  always_comb begin
    find_victim('0,    vic_found_d, vic_idx_d);
    find_victim(fut_q, vic_found_p, vic_idx_p);
  end
  assign vic_found_use_d = vic_found_d;  assign vic_idx_use_d = vic_idx_d;
  assign vic_found_use_p = vic_found_p;  assign vic_idx_use_p = vic_idx_p;
`endif

  logic input_bad;
  always_comb begin
    input_bad = 1'b0;
    for (int k = 0; k < MAX_EXPERTS; k++)
      if ((k >= cfg_num_experts) && (in_cur_mask[k] || in_fut_mask[k])) input_bad = 1'b1;
  end

  assign in_ready            = (state == S_IDLE);
  assign out_valid           = (state == S_DONE) || (state == S_ERR);
  assign o_demand_misses     = c_miss;
  assign o_prefetch_hits     = c_pfh;
  assign o_transfers         = c_xfer;
  assign o_evictions         = c_evict;
  assign o_wasted_prefetches = c_waste;

  logic need_load_demand, need_load_pref, pf_blocked;
  assign need_load_demand = (state == S_DEMAND) && (idx < cfg_num_experts) &&
                            cur_q[ix] && !resident[ix];
  assign pf_blocked = (res_count == cfg_capacity) && !has_evict_p;
  assign need_load_pref = (state == S_PREFETCH) && (idx < cfg_num_experts) &&
                          fut_q[ix] && !resident[ix] && !pf_blocked;

  // In the sequential build, suppress the DMA request during the cycle(s) a
  // victim scan must run first (prevents a spurious handshake before eviction).
  logic scan_stall;
`ifdef MOE_HAS_SCAN
  assign scan_stall = ((need_load_demand || need_load_pref) &&
                       (res_count == cfg_capacity) && !vic_ready);
`else
  assign scan_stall = 1'b0;
`endif

  assign dma_req_valid  = (need_load_demand || need_load_pref) && !scan_stall;
  assign dma_req_tag    = next_dma_tag;
  assign dma_req_expert = ix;
  assign dma_req_kind   = need_load_demand ? DMA_DEMAND : DMA_PREFETCH;
  assign dma_cmpl_ready = ((state == S_DWAIT) || (state == S_PWAIT)) &&
                          (dma_cmpl_tag == wait_dma_tag) &&
                          (dma_cmpl_expert == wait_expert) &&
                          (dma_cmpl_kind ==
                           ((state == S_DWAIT) ? DMA_DEMAND : DMA_PREFETCH));

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      state <= S_IDLE; resident <= '0; pending <= '0; time_ctr <= '0;
      res_count <= '0; idx <= '0; cur_q <= '0; fut_q <= '0;
      c_miss <= '0; c_pfh <= '0; c_xfer <= '0; c_evict <= '0; c_waste <= '0;
      o_input_error <= 1'b0;
      last_used <= '0;
      next_dma_tag <= '0; wait_dma_tag <= '0; wait_expert <= '0;
      wait_victim <= '0; wait_evict <= 1'b0;
`ifdef MOE_HAS_SCAN
      vic_found_r <= 1'b0; vic_ready <= 1'b0; vic_idx_r <= '0;
`ifdef MOE_BANKED_ARGMIN
      bank_start <= 1'b0; pf_scan <= 1'b0;
`else
      scan_i <= '0; scan_best <= '1; scan_found <= 1'b0; scan_vidx <= '0;
`endif
`endif
    end else begin
      case (state)
        S_IDLE: begin
          if (in_valid) begin
            if (input_bad) begin
              o_input_error <= 1'b1; state <= S_ERR;
            end else begin
              cur_q <= in_cur_mask; fut_q <= in_fut_mask;
              idx <= '0; state <= S_DEMAND;
            end
          end
        end

        S_DEMAND: begin
          if (idx == cfg_num_experts) begin
            idx <= '0; state <= S_PREFETCH;
          end else if (!cur_q[ix]) begin
            idx <= idx + 1'b1;
          end else if (resident[ix]) begin
            if (pending[ix]) begin c_pfh <= c_pfh + 1'b1; pending[ix] <= 1'b0; end
            last_used[ix] <= time_ctr; time_ctr <= time_ctr + 1'b1;
            idx <= idx + 1'b1;
          end else begin
            // demand miss
`ifdef MOE_HAS_SCAN
            if ((res_count == cfg_capacity) && !vic_ready) begin
              // launch victim scan (protect = none)
`ifdef MOE_BANKED_ARGMIN
              bank_start <= 1'b1; pf_scan <= 1'b0;
`else
              scan_i <= '0; scan_best <= '1; scan_found <= 1'b0; scan_vidx <= '0;
`endif
              state <= S_DSCAN;
            end else
`endif
            if (dma_req_ready) begin
              c_miss <= c_miss + 1'b1;
              c_xfer <= c_xfer + 1'b1;
              wait_dma_tag <= next_dma_tag;
              next_dma_tag <= next_dma_tag + 1'b1;
              wait_expert <= ix;
              wait_evict <= (res_count == cfg_capacity) && vic_found_use_d;
              wait_victim <= vic_idx_use_d;
              state <= S_DWAIT;
`ifdef MOE_HAS_SCAN
              vic_ready <= 1'b0;   // consume the scan result
`endif
            end
          end
        end

        S_DWAIT: begin
          if (dma_cmpl_valid && dma_cmpl_ready) begin
            if (wait_evict) begin
              resident[wait_victim] <= 1'b0;
              if (pending[wait_victim]) begin
                pending[wait_victim] <= 1'b0;
                c_waste <= c_waste + 1'b1;
              end
              c_evict <= c_evict + 1'b1;
            end else begin
              res_count <= res_count + 1'b1;
            end
            resident[wait_expert] <= 1'b1;
            pending[wait_expert] <= 1'b0;
            last_used[wait_expert] <= time_ctr;
            time_ctr <= time_ctr + 1'b1;
            idx <= idx + 1'b1;
            state <= S_DEMAND;
          end
        end

`ifdef MOE_HAS_SCAN
        S_DSCAN: begin
`ifdef MOE_BANKED_ARGMIN
          bank_start <= 1'b0;         // 1-cycle start pulse
          if (bank_done) begin
`ifdef MOE_BANK_DEBUG
            if (bank_found !== dbg_found || (bank_found && bank_victim !== dbg_vidx))
              $display("BANKD_MISMATCH d bank(f=%0d,v=%0d) ref(f=%0d,v=%0d) valid=%b",
                       bank_found, bank_victim, dbg_found, dbg_vidx, bank_valid);
`endif
            vic_found_r <= bank_found; vic_idx_r <= bank_victim; vic_ready <= 1'b1;
            state <= S_DEMAND;
          end
`else
          if (scan_i == cfg_num_experts) begin
            vic_found_r <= scan_found; vic_idx_r <= scan_vidx; vic_ready <= 1'b1;
            state <= S_DEMAND;
          end else begin
            if (resident[si]) begin   // demand protect = none
              if (!scan_found || last_used[si] < scan_best) begin
                scan_found <= 1'b1; scan_best <= last_used[si]; scan_vidx <= si;
              end
            end
            scan_i <= scan_i + 1'b1;
          end
`endif
        end
`endif

        S_PREFETCH: begin
          if (idx == cfg_num_experts || pf_blocked) begin
            state <= S_DONE;
          end else if (!fut_q[ix] || resident[ix]) begin
            idx <= idx + 1'b1;
          end else begin
            // prefetch miss (victim guaranteed to exist when not pf_blocked)
`ifdef MOE_HAS_SCAN
            if ((res_count == cfg_capacity) && !vic_ready) begin
              // launch victim scan (protect = fut_q)
`ifdef MOE_BANKED_ARGMIN
              bank_start <= 1'b1; pf_scan <= 1'b1;
`else
              scan_i <= '0; scan_best <= '1; scan_found <= 1'b0; scan_vidx <= '0;
`endif
              state <= S_PSCAN;
            end else
`endif
            if (dma_req_ready) begin
              c_xfer <= c_xfer + 1'b1;
              wait_dma_tag <= next_dma_tag;
              next_dma_tag <= next_dma_tag + 1'b1;
              wait_expert <= ix;
              wait_evict <= (res_count == cfg_capacity) && vic_found_use_p;
              wait_victim <= vic_idx_use_p;
              state <= S_PWAIT;
`ifdef MOE_HAS_SCAN
              vic_ready <= 1'b0;   // consume the scan result
`endif
            end
          end
        end

        S_PWAIT: begin
          if (dma_cmpl_valid && dma_cmpl_ready) begin
            if (wait_evict) begin
              resident[wait_victim] <= 1'b0;
              if (pending[wait_victim]) begin
                pending[wait_victim] <= 1'b0;
                c_waste <= c_waste + 1'b1;
              end
              c_evict <= c_evict + 1'b1;
            end else begin
              res_count <= res_count + 1'b1;
            end
            resident[wait_expert] <= 1'b1;
            pending[wait_expert] <= 1'b1;
            last_used[wait_expert] <= time_ctr;
            time_ctr <= time_ctr + 1'b1;
            idx <= idx + 1'b1;
            state <= S_PREFETCH;
          end
        end

`ifdef MOE_HAS_SCAN
        S_PSCAN: begin
`ifdef MOE_BANKED_ARGMIN
          bank_start <= 1'b0;
          if (bank_done) begin
            vic_found_r <= bank_found; vic_idx_r <= bank_victim; vic_ready <= 1'b1;
            state <= S_PREFETCH;
          end
`else
          if (scan_i == cfg_num_experts) begin
            vic_found_r <= scan_found; vic_idx_r <= scan_vidx; vic_ready <= 1'b1;
            state <= S_PREFETCH;
          end else begin
            if (resident[si] && !fut_q[si]) begin   // prefetch protect = fut_q
              if (!scan_found || last_used[si] < scan_best) begin
                scan_found <= 1'b1; scan_best <= last_used[si]; scan_vidx <= si;
              end
            end
            scan_i <= scan_i + 1'b1;
          end
`endif
        end
`endif

        S_DONE: if (out_ready) state <= S_IDLE;
        S_ERR:  if (out_ready) begin state <= S_IDLE; o_input_error <= 1'b0; end
        default: state <= S_IDLE;
      endcase
    end
  end
endmodule

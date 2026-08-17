// LRU victim search (argmin of recency timestamp over resident, non-protected
// entries). Two implementations with IDENTICAL selection semantics:
//   * lru_victim_comb : single-cycle combinational argmin (throughput-optimal,
//                       long critical path -> the S6/S6+ timing bottleneck).
//   * lru_victim_seq  : registered sequential scan, one entry/cycle (frequency-
//                       optimal, short critical path; costs ~N cycles/search).
// Both break ties by lowest index (strict-less-than update while scanning 0..N-1),
// so they select the same victim as the engine's inline argmin.

module lru_victim_comb #(
  parameter int N = 32,
  parameter int TSW = 16
)(
  input  logic [N-1:0]          valid,          // resident & ~protect
  input  logic [N-1:0][TSW-1:0] ts,
  output logic                  found,
  output logic [$clog2(N)-1:0]  victim
);
  always_comb begin
    logic [TSW-1:0] best;
    found = 1'b0; victim = '0; best = '1;
    for (int k = 0; k < N; k++) begin
      if (valid[k] && (!found || ts[k] < best)) begin
        found = 1'b1; best = ts[k]; victim = k[$clog2(N)-1:0];
      end
    end
  end
endmodule


// lru_victim_banked : frequency-scalable sequential argmin for LARGE N. Splits the
// N-wide table into B banks of W=N/B slots. All banks scan in parallel, one local
// entry/cycle, so the per-cycle critical path is a single TSW compare + a W:1 read
// mux (INDEPENDENT of N when B scales with N) instead of the N:1 mux that made the
// flat seq engine's Fmax fall ~1/N (see W3_ENGINE_SCALING.md). After W cycles a
// one-shot B-way reduce (lowest ts, ties -> lowest bank = lowest global index)
// yields the SAME victim as lru_victim_comb. Latency = W+1 cycles (constant if W
// fixed); throughput cost is acceptable (expert transfer >> engine cycles, d*=1).
// Requires N % B == 0.
module lru_victim_banked #(
  parameter int N   = 128,
  parameter int TSW = 16,
  parameter int B   = 8
)(
  input  logic                  clk,
  input  logic                  rst_n,
  input  logic                  start,
  input  logic [N-1:0]          valid,
  input  logic [N-1:0][TSW-1:0] ts,
  output logic                  busy,
  output logic                  done,
  output logic                  found,
  output logic [$clog2(N)-1:0]  victim
);
  localparam int W   = N / B;             // slots per bank
  localparam int LW  = (W > 1) ? $clog2(W) : 1;   // local index width
  localparam int BW  = (B > 1) ? $clog2(B) : 1;   // bank index width
  localparam int GW  = $clog2(N);         // global index width

  // No private snapshot: the residency table (valid/ts) is read LIVE during the
  // scan. Contract: caller must hold valid/ts stable from `start` until `done`
  // (true in the engine -- no eviction happens mid victim-search). Snapshotting
  // into an O(N) register file was the real limiter: its shared load-enable net
  // fanned out to ~2N flops and, under the wire-load model, dominated Fmax
  // regardless of mux banking. Reading live keeps only O(B+sqrt(N)) state.
  logic [LW:0]           j;               // local scan counter 0..W
  logic [BW:0]           r;               // bank reduce counter 0..B
  logic [B-1:0]          fnd;             // per-bank found
  logic [B-1:0][TSW-1:0] bst;             // per-bank best ts
  logic [B-1:0][LW-1:0]  lidx;            // per-bank local victim index
  logic [TSW-1:0]        rb;              // running reduce best ts
  logic                  rf;              // running reduce found
  logic [GW-1:0]         rv;              // running reduce victim (global idx)
  logic                  running, reducing;

  assign busy = running | reducing;

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      running <= 1'b0; reducing <= 1'b0; done <= 1'b0;
      found <= 1'b0; victim <= '0; j <= '0; r <= '0;
      rf <= 1'b0; rb <= '1; rv <= '0;
      fnd <= '0; bst <= '{default:'1}; lidx <= '{default:'0};
    end else begin
      done <= 1'b0;
      if (!running && !reducing) begin
        if (start) begin
          j <= '0; r <= '0;
          fnd <= '0; bst <= '{default:'1}; lidx <= '{default:'0};
          rf <= 1'b0; rb <= '1; rv <= '0;
          found <= 1'b0; victim <= '0; running <= 1'b1;
        end
      end else if (running) begin
        // Phase 1 (SCAN): B parallel single-entry compares reading the LIVE table.
        // Per-cycle path ~ W:1 read mux + single TSW compare (tracks sqrt(N)).
        for (int b = 0; b < B; b++) begin
          automatic int gi = b * W + int'(j[LW-1:0]);
          if (valid[gi] && (!fnd[b] || ts[gi] < bst[b])) begin
            fnd[b]  <= 1'b1;
            bst[b]  <= ts[gi];
            lidx[b] <= j[LW-1:0];
          end
        end
        if (j == W[LW:0] - 1) begin
          running <= 1'b0; reducing <= 1'b1;
        end else begin
          j <= j + 1'b1;
        end
      end else begin
        // Phase 2 (REDUCE): one bank/cycle, single compare (lowest ts, ties ->
        // lowest bank = lowest global index). Per-cycle path ~ B:1 mux + compare.
        // Sequential (not a B-deep combinational chain), so it does not reintroduce
        // an N-scaled critical path. Choosing W~=B~=sqrt(N) minimises max(W,B).
        logic          nf;
        logic [TSW-1:0] nb;
        logic [GW-1:0]  nv;
        nf = rf; nb = rb; nv = rv;
        if (fnd[r[BW-1:0]] && (!rf || bst[r[BW-1:0]] < rb)) begin
          nf = 1'b1; nb = bst[r[BW-1:0]];
          nv = GW'(int'(r[BW-1:0]) * W + int'(lidx[r[BW-1:0]]));
        end
        rf <= nf; rb <= nb; rv <= nv;
        if (r == B[BW:0] - 1) begin
          found <= nf; victim <= nv; reducing <= 1'b0; done <= 1'b1;
        end else begin
          r <= r + 1'b1;
        end
      end
    end
  end
endmodule


module lru_victim_seq #(
  parameter int N = 32,
  parameter int TSW = 16
)(
  input  logic                  clk,
  input  logic                  rst_n,
  input  logic                  start,          // pulse: latch inputs, begin scan
  input  logic [N-1:0]          valid,
  input  logic [N-1:0][TSW-1:0] ts,
  output logic                  busy,
  output logic                  done,           // 1-cycle pulse when result ready
  output logic                  found,
  output logic [$clog2(N)-1:0]  victim
);
  localparam int IW = $clog2(N);
  logic [N-1:0]          v_q;
  logic [N-1:0][TSW-1:0] ts_q;
  logic [IW:0]           i;                      // 0..N
  logic [TSW-1:0]        best;
  logic                  running;

  assign busy = running;

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      running <= 1'b0; done <= 1'b0; found <= 1'b0; victim <= '0;
      i <= '0; best <= '1; v_q <= '0; ts_q <= '0;
    end else begin
      done <= 1'b0;
      if (!running) begin
        if (start) begin
          v_q <= valid; ts_q <= ts; i <= '0; best <= '1;
          found <= 1'b0; victim <= '0; running <= 1'b1;
        end
      end else begin
        // one entry per cycle: short combinational path (single TSW compare)
        if (v_q[i[IW-1:0]] && (!found || ts_q[i[IW-1:0]] < best)) begin
          found  <= 1'b1;
          best   <= ts_q[i[IW-1:0]];
          victim <= i[IW-1:0];
        end
        if (i == N[IW:0] - 1) begin
          running <= 1'b0; done <= 1'b1;
        end else begin
          i <= i + 1'b1;
        end
      end
    end
  end
endmodule

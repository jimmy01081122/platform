// Common parameters/types for the MoE expert residency/prefetch engine.
//
// MAX_EXPERTS (residency-table width) and TS_W (recency timestamp width) are the
// two DSE architecture knobs; override at compile time with
//   +define+MOE_MAX_EXPERTS=<n>  +define+MOE_TS_W=<w>
// Defaults keep the S5 verification configuration (32 experts, 32-bit timestamp).
`ifndef MOE_MAX_EXPERTS
  `define MOE_MAX_EXPERTS 32
`endif
`ifndef MOE_TS_W
  `define MOE_TS_W 32
`endif
// Victim-search bank count for the `+define+MOE_BANKED_ARGMIN` build. Must divide
// MAX_EXPERTS. Pick ~sqrt(MAX_EXPERTS) for best Fmax (e.g. 16 for 128/256, 24 for
// 384). Default 4 divides the S5 verification width (32).
`ifndef MOE_BANKS
  `define MOE_BANKS 4
`endif
`ifndef MOE_DMA_TAG_W
`define MOE_DMA_TAG_W 16
`endif

package moe_pkg;
  localparam int MAX_EXPERTS = `MOE_MAX_EXPERTS;
  localparam int EID_W       = $clog2(MAX_EXPERTS);
  localparam int TS_W        = `MOE_TS_W;   // recency timestamp width
  localparam int BANKS       = `MOE_BANKS;  // banked-argmin bank count
  localparam int CNT_W       = 32;          // event counters
  localparam int DMA_TAG_W   = `MOE_DMA_TAG_W; // request/completion correlation tag

  typedef enum logic [0:0] { DMA_DEMAND = 1'b0, DMA_PREFETCH = 1'b1 } dma_kind_e;
endpackage

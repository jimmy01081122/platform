module residency_engine (
	clk,
	rst_n,
	cfg_num_experts,
	cfg_capacity,
	in_valid,
	in_ready,
	in_cur_mask,
	in_fut_mask,
	out_valid,
	out_ready,
	o_demand_misses,
	o_prefetch_hits,
	o_transfers,
	o_evictions,
	o_wasted_prefetches,
	o_input_error,
	dma_req_valid,
	dma_req_ready,
	dma_req_expert,
	dma_req_kind
);
	reg _sv2v_0;
	input wire clk;
	input wire rst_n;
	localparam signed [31:0] moe_pkg_MAX_EXPERTS = 16;
	localparam signed [31:0] moe_pkg_EID_W = 4;
	input wire [moe_pkg_EID_W:0] cfg_num_experts;
	input wire [moe_pkg_EID_W:0] cfg_capacity;
	input wire in_valid;
	output wire in_ready;
	input wire [15:0] in_cur_mask;
	input wire [15:0] in_fut_mask;
	output wire out_valid;
	input wire out_ready;
	localparam signed [31:0] moe_pkg_CNT_W = 32;
	output wire [31:0] o_demand_misses;
	output wire [31:0] o_prefetch_hits;
	output wire [31:0] o_transfers;
	output wire [31:0] o_evictions;
	output wire [31:0] o_wasted_prefetches;
	output reg o_input_error;
	output wire dma_req_valid;
	input wire dma_req_ready;
	output wire [3:0] dma_req_expert;
	output wire [0:0] dma_req_kind;
	reg [2:0] state;
	reg [15:0] resident;
	reg [15:0] pending;
	localparam signed [31:0] moe_pkg_TS_W = 16;
	reg [(moe_pkg_MAX_EXPERTS * moe_pkg_TS_W) - 1:0] last_used;
	reg [15:0] time_ctr;
	reg [moe_pkg_EID_W:0] res_count;
	reg [moe_pkg_EID_W:0] idx;
	reg [15:0] cur_q;
	reg [15:0] fut_q;
	reg [31:0] c_miss;
	reg [31:0] c_pfh;
	reg [31:0] c_xfer;
	reg [31:0] c_evict;
	reg [31:0] c_waste;
	wire [3:0] ix;
	assign ix = idx[3:0];
	task automatic find_victim;
		input reg [15:0] protect;
		output reg found;
		output reg [3:0] vidx;
		reg [15:0] best;
		begin
			found = 1'b0;
			vidx = 1'sb0;
			best = 1'sb1;
			begin : sv2v_autoblock_1
				reg signed [31:0] k;
				for (k = 0; k < moe_pkg_MAX_EXPERTS; k = k + 1)
					if (((k < cfg_num_experts) && resident[k]) && !protect[k]) begin
						if (!found || (last_used[k * moe_pkg_TS_W+:moe_pkg_TS_W] < best)) begin
							found = 1'b1;
							best = last_used[k * moe_pkg_TS_W+:moe_pkg_TS_W];
							vidx = k[3:0];
						end
					end
			end
		end
	endtask
	wire vic_found_d;
	wire vic_found_p;
	wire [3:0] vic_idx_d;
	wire [3:0] vic_idx_p;
	always @(fut_q or last_used or last_used or resident or cfg_num_experts or last_used or last_used or resident or cfg_num_experts or _sv2v_0) begin
		if (_sv2v_0)
			;
		find_victim(1'sb0, vic_found_d, vic_idx_d);
		find_victim(fut_q, vic_found_p, vic_idx_p);
	end
	reg input_bad;
	always @(*) begin
		if (_sv2v_0)
			;
		input_bad = 1'b0;
		begin : sv2v_autoblock_2
			reg signed [31:0] k;
			for (k = 0; k < moe_pkg_MAX_EXPERTS; k = k + 1)
				if ((k >= cfg_num_experts) && (in_cur_mask[k] || in_fut_mask[k]))
					input_bad = 1'b1;
		end
	end
	assign in_ready = state == 3'd0;
	assign out_valid = (state == 3'd3) || (state == 3'd4);
	assign o_demand_misses = c_miss;
	assign o_prefetch_hits = c_pfh;
	assign o_transfers = c_xfer;
	assign o_evictions = c_evict;
	assign o_wasted_prefetches = c_waste;
	wire need_load_demand;
	wire need_load_pref;
	wire pf_blocked;
	assign need_load_demand = (((state == 3'd1) && (idx < cfg_num_experts)) && cur_q[ix]) && !resident[ix];
	assign pf_blocked = (res_count == cfg_capacity) && !vic_found_p;
	assign need_load_pref = ((((state == 3'd2) && (idx < cfg_num_experts)) && fut_q[ix]) && !resident[ix]) && !pf_blocked;
	assign dma_req_valid = need_load_demand || need_load_pref;
	assign dma_req_expert = ix;
	assign dma_req_kind = (need_load_demand ? 1'b0 : 1'b1);
	always @(posedge clk or negedge rst_n)
		if (!rst_n) begin
			state <= 3'd0;
			resident <= 1'sb0;
			pending <= 1'sb0;
			time_ctr <= 1'sb0;
			res_count <= 1'sb0;
			idx <= 1'sb0;
			cur_q <= 1'sb0;
			fut_q <= 1'sb0;
			c_miss <= 1'sb0;
			c_pfh <= 1'sb0;
			c_xfer <= 1'sb0;
			c_evict <= 1'sb0;
			c_waste <= 1'sb0;
			o_input_error <= 1'b0;
			last_used <= 1'sb0;
		end
		else
			case (state)
				3'd0:
					if (in_valid) begin
						if (input_bad) begin
							o_input_error <= 1'b1;
							state <= 3'd4;
						end
						else begin
							cur_q <= in_cur_mask;
							fut_q <= in_fut_mask;
							idx <= 1'sb0;
							state <= 3'd1;
						end
					end
				3'd1:
					if (idx == cfg_num_experts) begin
						idx <= 1'sb0;
						state <= 3'd2;
					end
					else if (!cur_q[ix])
						idx <= idx + 1'b1;
					else if (resident[ix]) begin
						if (pending[ix]) begin
							c_pfh <= c_pfh + 1'b1;
							pending[ix] <= 1'b0;
						end
						last_used[ix * moe_pkg_TS_W+:moe_pkg_TS_W] <= time_ctr;
						time_ctr <= time_ctr + 1'b1;
						idx <= idx + 1'b1;
					end
					else if (dma_req_ready) begin
						c_miss <= c_miss + 1'b1;
						c_xfer <= c_xfer + 1'b1;
						if (res_count == cfg_capacity) begin
							if (vic_found_d) begin
								resident[vic_idx_d] <= 1'b0;
								if (pending[vic_idx_d]) begin
									pending[vic_idx_d] <= 1'b0;
									c_waste <= c_waste + 1'b1;
								end
								c_evict <= c_evict + 1'b1;
							end
						end
						else
							res_count <= res_count + 1'b1;
						resident[ix] <= 1'b1;
						pending[ix] <= 1'b0;
						last_used[ix * moe_pkg_TS_W+:moe_pkg_TS_W] <= time_ctr;
						time_ctr <= time_ctr + 1'b1;
						idx <= idx + 1'b1;
					end
				3'd2:
					if ((idx == cfg_num_experts) || pf_blocked)
						state <= 3'd3;
					else if (!fut_q[ix] || resident[ix])
						idx <= idx + 1'b1;
					else if (dma_req_ready) begin
						c_xfer <= c_xfer + 1'b1;
						if (res_count == cfg_capacity) begin
							resident[vic_idx_p] <= 1'b0;
							if (pending[vic_idx_p]) begin
								pending[vic_idx_p] <= 1'b0;
								c_waste <= c_waste + 1'b1;
							end
							c_evict <= c_evict + 1'b1;
						end
						else
							res_count <= res_count + 1'b1;
						resident[ix] <= 1'b1;
						pending[ix] <= 1'b1;
						last_used[ix * moe_pkg_TS_W+:moe_pkg_TS_W] <= time_ctr;
						time_ctr <= time_ctr + 1'b1;
						idx <= idx + 1'b1;
					end
				3'd3:
					if (out_ready)
						state <= 3'd0;
				3'd4:
					if (out_ready) begin
						state <= 3'd0;
						o_input_error <= 1'b0;
					end
				default: state <= 3'd0;
			endcase
	initial _sv2v_0 = 0;
endmodule
module dma_model (
	clk,
	rst_n,
	req_valid,
	req_ready,
	req_expert,
	req_kind,
	cmpl_valid,
	cmpl_expert,
	o_completions
);
	reg _sv2v_0;
	parameter signed [31:0] LATENCY = 8;
	parameter signed [31:0] DEPTH = 4;
	input wire clk;
	input wire rst_n;
	input wire req_valid;
	output wire req_ready;
	localparam signed [31:0] moe_pkg_MAX_EXPERTS = 16;
	localparam signed [31:0] moe_pkg_EID_W = 4;
	input wire [3:0] req_expert;
	input wire [0:0] req_kind;
	output reg cmpl_valid;
	output reg [3:0] cmpl_expert;
	localparam signed [31:0] moe_pkg_CNT_W = 32;
	output wire [31:0] o_completions;
	reg [DEPTH - 1:0] busy;
	reg [(DEPTH * moe_pkg_EID_W) - 1:0] slot_e;
	reg [(DEPTH * 32) - 1:0] slot_t;
	reg [31:0] completions;
	reg free_found;
	reg [$clog2(DEPTH) - 1:0] free_idx;
	always @(*) begin
		if (_sv2v_0)
			;
		free_found = 1'b0;
		free_idx = 1'sb0;
		begin : sv2v_autoblock_1
			reg signed [31:0] k;
			for (k = DEPTH - 1; k >= 0; k = k - 1)
				if (!busy[k]) begin
					free_found = 1'b1;
					free_idx = k[$clog2(DEPTH) - 1:0];
				end
		end
	end
	assign req_ready = free_found;
	assign o_completions = completions;
	integer i;
	always @(posedge clk or negedge rst_n)
		if (!rst_n) begin
			cmpl_valid <= 1'b0;
			cmpl_expert <= 1'sb0;
			completions <= 1'sb0;
			for (i = 0; i < DEPTH; i = i + 1)
				begin
					busy[i] <= 1'b0;
					slot_e[i * moe_pkg_EID_W+:moe_pkg_EID_W] <= 1'sb0;
					slot_t[i * 32+:32] <= 1'sb0;
				end
		end
		else begin
			cmpl_valid <= 1'b0;
			for (i = 0; i < DEPTH; i = i + 1)
				if (busy[i]) begin
					if (slot_t[i * 32+:32] <= 1) begin
						busy[i] <= 1'b0;
						if (!cmpl_valid) begin
							cmpl_valid <= 1'b1;
							cmpl_expert <= slot_e[i * moe_pkg_EID_W+:moe_pkg_EID_W];
							completions <= completions + 1'b1;
						end
					end
					else
						slot_t[i * 32+:32] <= slot_t[i * 32+:32] - 1'b1;
				end
			if (req_valid && req_ready) begin
				busy[free_idx] <= 1'b1;
				slot_e[free_idx * moe_pkg_EID_W+:moe_pkg_EID_W] <= req_expert;
				slot_t[free_idx * 32+:32] <= LATENCY[31:0];
			end
		end
	initial _sv2v_0 = 0;
endmodule
module moe_residency_top (
	clk,
	rst_n,
	cfg_num_experts,
	cfg_capacity,
	in_valid,
	in_ready,
	in_cur_mask,
	in_fut_mask,
	out_valid,
	out_ready,
	o_demand_misses,
	o_prefetch_hits,
	o_transfers,
	o_evictions,
	o_wasted_prefetches,
	o_input_error,
	o_dma_completions
);
	parameter signed [31:0] DMA_LATENCY = 8;
	parameter signed [31:0] DMA_DEPTH = 4;
	input wire clk;
	input wire rst_n;
	localparam signed [31:0] moe_pkg_MAX_EXPERTS = 16;
	localparam signed [31:0] moe_pkg_EID_W = 4;
	input wire [moe_pkg_EID_W:0] cfg_num_experts;
	input wire [moe_pkg_EID_W:0] cfg_capacity;
	input wire in_valid;
	output wire in_ready;
	input wire [15:0] in_cur_mask;
	input wire [15:0] in_fut_mask;
	output wire out_valid;
	input wire out_ready;
	localparam signed [31:0] moe_pkg_CNT_W = 32;
	output wire [31:0] o_demand_misses;
	output wire [31:0] o_prefetch_hits;
	output wire [31:0] o_transfers;
	output wire [31:0] o_evictions;
	output wire [31:0] o_wasted_prefetches;
	output wire o_input_error;
	output wire [31:0] o_dma_completions;
	wire dma_req_valid;
	wire dma_req_ready;
	wire [3:0] dma_req_expert;
	wire [0:0] dma_req_kind;
	wire cmpl_valid;
	wire [3:0] cmpl_expert;
	residency_engine u_eng(
		.clk(clk),
		.rst_n(rst_n),
		.cfg_num_experts(cfg_num_experts),
		.cfg_capacity(cfg_capacity),
		.in_valid(in_valid),
		.in_ready(in_ready),
		.in_cur_mask(in_cur_mask),
		.in_fut_mask(in_fut_mask),
		.out_valid(out_valid),
		.out_ready(out_ready),
		.o_demand_misses(o_demand_misses),
		.o_prefetch_hits(o_prefetch_hits),
		.o_transfers(o_transfers),
		.o_evictions(o_evictions),
		.o_wasted_prefetches(o_wasted_prefetches),
		.o_input_error(o_input_error),
		.dma_req_valid(dma_req_valid),
		.dma_req_ready(dma_req_ready),
		.dma_req_expert(dma_req_expert),
		.dma_req_kind(dma_req_kind)
	);
	dma_model #(
		.LATENCY(DMA_LATENCY),
		.DEPTH(DMA_DEPTH)
	) u_dma(
		.clk(clk),
		.rst_n(rst_n),
		.req_valid(dma_req_valid),
		.req_ready(dma_req_ready),
		.req_expert(dma_req_expert),
		.req_kind(dma_req_kind),
		.cmpl_valid(cmpl_valid),
		.cmpl_expert(cmpl_expert),
		.o_completions(o_dma_completions)
	);
endmodule

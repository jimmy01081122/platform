// Verilator C++ scoreboard testbench for moe_residency_top.
//
// Drives step transactions derived from a demands file and compares the RTL's
// cumulative counters against the SAME frozen kernel used by software/firmware
// (firmware/scheduler.c) as the golden reference (transaction-level equivalence).
// Also exercises corner cases: invalid input, empty step, reset mid-stream.
#include <verilated.h>
#include "Vmoe_residency_top.h"
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cstddef>
#include <vector>
#include <string>

extern "C" {
#include "scheduler.h"
}

using std::vector;

// Residency-table width the DUT was built with (must match +define+MOE_MAX_EXPERTS);
// bounds the randomized-property expert count so we exercise the real large-N engine.
#ifndef TB_MAX_EXPERTS
#define TB_MAX_EXPERTS 32
#endif

// Width-agnostic mask writers: work for narrow (IData/QData) and wide (VlWide<N>)
// port signals, so the same tb drives MAX_EXPERTS = 32/64/128/256/384.
template<class T> static inline void mask_zero(T& m) { m = (T)0; }
template<class T> static inline void mask_set (T& m, int e) { m |= ((T)1u << e); }
template<std::size_t N> static inline void mask_zero(VlWide<N>& m) { for (std::size_t i = 0; i < N; i++) m[i] = 0; }
template<std::size_t N> static inline void mask_set (VlWide<N>& m, int e) { m[e >> 5] |= (1u << (e & 31)); }

static Vmoe_residency_top* dut = nullptr;
static vluint64_t main_time = 0;
static long g_ticks = 0;

static void tick() {
    dut->clk = 0; dut->eval();
    dut->clk = 1; dut->eval();
    main_time++; g_ticks++;
}

static void do_reset() {
    dut->rst_n = 0; dut->in_valid = 0; dut->out_ready = 1;
    mask_zero(dut->in_cur_mask); mask_zero(dut->in_fut_mask);
    for (int i = 0; i < 5; i++) tick();
    dut->rst_n = 1; tick();
}

// Run a list of steps (each = vector of expert ids). depth: 0=on-demand, 1=prefetch.
static void run_steps(const vector<vector<int>>& steps, int num_experts,
                      int capacity, int depth) {
    dut->cfg_num_experts = num_experts;
    dut->cfg_capacity = capacity;
    for (size_t s = 0; s < steps.size(); s++) {
        mask_zero(dut->in_cur_mask); mask_zero(dut->in_fut_mask);
        for (int e : steps[s]) mask_set(dut->in_cur_mask, e);
        if (depth > 0 && s + 1 < steps.size())
            for (int e : steps[s + 1]) mask_set(dut->in_fut_mask, e);
        // present transaction until accepted (in_ready)
        dut->in_valid = 1;
        int guard = 0;
        while (!(dut->in_ready)) { tick(); if (++guard > 1000000) { fprintf(stderr,"hang in_ready\n"); exit(3);} }
        tick();                       // accept cycle
        dut->in_valid = 0;
        guard = 0;
        while (!(dut->out_valid)) { tick(); if (++guard > 1000000) { fprintf(stderr,"hang out_valid\n"); exit(3);} }
        tick();                       // consume DONE (out_ready=1)
    }
}

// golden reference via the frozen C kernel
static SchedCounters golden(const vector<vector<int>>& steps, int num_experts,
                            int capacity, int depth) {
    static int experts[MAX_DEMANDS];
    static int offset[MAX_STEPS];
    static int count[MAX_STEPS];
    int di = 0;
    for (size_t s = 0; s < steps.size(); s++) {
        offset[s] = di; count[s] = (int)steps[s].size();
        for (int e : steps[s]) experts[di++] = e;
    }
    Demands d = { num_experts, (int)steps.size(), experts, offset, count };
    SchedCounters c; sched_run(&d, capacity, depth, &c);
    return c;
}

static bool cmp(const char* tag, int cap, int depth, const SchedCounters& g) {
    bool ok = (dut->o_demand_misses == (uint32_t)g.demand_misses)
           && (dut->o_prefetch_hits == (uint32_t)g.prefetch_hits)
           && (dut->o_transfers     == (uint32_t)g.transfers)
           && (dut->o_evictions     == (uint32_t)g.evictions)
           && (dut->o_wasted_prefetches == (uint32_t)g.wasted_prefetches);
    printf("{\"case\":\"%s\",\"capacity\":%d,\"depth\":%d,\"pass\":%s,"
           "\"rtl\":{\"miss\":%u,\"hit\":%u,\"xfer\":%u,\"evict\":%u,\"waste\":%u},"
           "\"golden\":{\"miss\":%ld,\"hit\":%ld,\"xfer\":%ld,\"evict\":%ld,\"waste\":%ld}}\n",
           tag, cap, depth, ok ? "true" : "false",
           dut->o_demand_misses, dut->o_prefetch_hits, dut->o_transfers,
           dut->o_evictions, dut->o_wasted_prefetches,
           g.demand_misses, g.prefetch_hits, g.transfers, g.evictions, g.wasted_prefetches);
    return ok;
}

int main(int argc, char** argv) {
    Verilated::commandArgs(argc, argv);
    if (argc < 3) { fprintf(stderr, "usage: %s <demands> <cap1,cap2,...>\n", argv[0]); return 2; }
    const char* path = argv[1];
    vector<int> caps;
    { char* s = strdup(argv[2]); for (char* t = strtok(s, ","); t; t = strtok(nullptr, ",")) caps.push_back(atoi(t)); free(s); }

    FILE* f = fopen(path, "r");
    if (!f) { fprintf(stderr, "cannot open %s\n", path); return 2; }
    int num_experts = 0, num_steps = 0;
    if (fscanf(f, "%d %d", &num_experts, &num_steps) != 2) return 2;
    vector<vector<int>> steps(num_steps);
    for (int s = 0; s < num_steps; s++) {
        int c = 0; if (fscanf(f, "%d", &c) != 1) return 2;
        steps[s].resize(c);
        for (int j = 0; j < c; j++) if (fscanf(f, "%d", &steps[s][j]) != 1) return 2;
    }
    fclose(f);

    dut = new Vmoe_residency_top;
    int fails = 0;

    // Main equivalence sweep: on-demand (depth 0) and prefetch (depth 1)
    for (int depth = 0; depth <= 1; depth++) {
        for (int cap : caps) {
            do_reset();
            long t0 = g_ticks;
            run_steps(steps, num_experts, cap, depth);
            long cyc = g_ticks - t0;
            SchedCounters g = golden(steps, num_experts, cap, depth);
            if (!cmp(depth ? "prefetch" : "on_demand", cap, depth, g)) fails++;
            printf("{\"case\":\"cycles\",\"capacity\":%d,\"depth\":%d,\"num_steps\":%d,"
                   "\"cycles_total\":%ld,\"cycles_per_step\":%.4f}\n",
                   cap, depth, (int)steps.size(), cyc,
                   steps.empty() ? 0.0 : (double)cyc / (double)steps.size());
        }
    }

    // Corner case: empty steps
    {
        do_reset();
        vector<vector<int>> empt = { {}, {}, {} };
        run_steps(empt, num_experts, caps[0], 1);
        SchedCounters g = golden(empt, num_experts, caps[0], 1);
        if (!cmp("empty_steps", caps[0], 1, g)) fails++;
    }

    // Corner case: invalid input (bit >= configured num_experts) -> o_input_error
    {
        do_reset();
        int ne_small = 8;                 // configure fewer experts than the table
        dut->cfg_num_experts = ne_small; dut->cfg_capacity = 4;
        mask_zero(dut->in_cur_mask); mask_set(dut->in_cur_mask, ne_small); // bit 8 out of range
        mask_zero(dut->in_fut_mask); dut->in_valid = 1;
        int guard = 0; while (!dut->in_ready) { tick(); if (++guard>1000) break; }
        tick(); dut->in_valid = 0;
        guard = 0; while (!dut->out_valid) { tick(); if (++guard>1000) break; }
        bool ok = (dut->o_input_error == 1);
        printf("{\"case\":\"invalid_input\",\"pass\":%s,\"o_input_error\":%u}\n",
               ok ? "true" : "false", dut->o_input_error);
        if (!ok) fails++;
        tick(); // consume
    }

    // Corner case: reset mid-stream clears counters
    {
        do_reset();
        vector<vector<int>> half(steps.begin(), steps.begin() + steps.size()/2);
        run_steps(half, num_experts, caps[0], 1);
        do_reset(); // reset
        bool ok = (dut->o_demand_misses == 0 && dut->o_transfers == 0);
        printf("{\"case\":\"reset_midstream\",\"pass\":%s,\"miss_after_reset\":%u}\n",
               ok ? "true" : "false", dut->o_demand_misses);
        if (!ok) fails++;
    }

    // Randomized property test: for random configs/traces the RTL must match the
    // golden reference exactly (transaction-level equivalence invariant).
    {
        srand(0xC0FFEE);
        const int RTL_NE_MAX = TB_MAX_EXPERTS;            // hardware residency-table width
        int rand_trials = 300, rand_fail = 0;
        for (int t = 0; t < rand_trials; t++) {
            int ne = 4 + rand() % (RTL_NE_MAX - 3);       // 4..MAX_EXPERTS
            int cap = 1 + rand() % ne;                    // 1..ne
            int depth = rand() % 2;                       // 0 or 1
            int ns = 1 + rand() % 60;                     // 1..60 steps
            vector<vector<int>> steps(ns);
            for (int s = 0; s < ns; s++) {
                for (int e = 0; e < ne; e++)
                    if (rand() % 3 == 0) steps[s].push_back(e); // ~1/3 active
            }
            do_reset();
            run_steps(steps, ne, cap, depth);
            SchedCounters g = golden(steps, ne, cap, depth);
            bool ok = (dut->o_demand_misses == (uint32_t)g.demand_misses)
                   && (dut->o_prefetch_hits == (uint32_t)g.prefetch_hits)
                   && (dut->o_transfers     == (uint32_t)g.transfers)
                   && (dut->o_evictions     == (uint32_t)g.evictions)
                   && (dut->o_wasted_prefetches == (uint32_t)g.wasted_prefetches);
            if (!ok) {
                rand_fail++;
                printf("{\"case\":\"random_fail\",\"trial\":%d,\"ne\":%d,\"cap\":%d,\"depth\":%d,"
                       "\"rtl_miss\":%u,\"g_miss\":%ld,\"rtl_xfer\":%u,\"g_xfer\":%ld}\n",
                       t, ne, cap, depth, dut->o_demand_misses, g.demand_misses,
                       dut->o_transfers, g.transfers);
            }
        }
        printf("{\"case\":\"randomized_property\",\"trials\":%d,\"pass\":%s,\"failures\":%d}\n",
               rand_trials, rand_fail ? "false" : "true", rand_fail);
        fails += rand_fail;
    }

    printf("{\"summary\":\"%s\",\"failures\":%d}\n", fails ? "FAIL" : "PASS", fails);
    delete dut;
    return fails ? 1 : 0;
}

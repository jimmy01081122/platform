// Verilator tb: prove lru_victim_banked selects the SAME victim as the
// combinational reference (and a software argmin) over randomized inputs, for
// large N. N/TSW/B are set via -DTB_N/-DTB_TSW/-DTB_B to match the -G params.
#include <verilated.h>
#include "Vlru_victim_banked_tb_top.h"
#include <cstdio>
#include <cstdlib>

#ifndef TB_N
#define TB_N 128
#endif
#ifndef TB_TSW
#define TB_TSW 16
#endif
#ifndef TB_B
#define TB_B 8
#endif

static Vlru_victim_banked_tb_top* dut = nullptr;
static void tick() { dut->clk = 0; dut->eval(); dut->clk = 1; dut->eval(); }

static const int N = TB_N, TSW = TB_TSW;

int main(int argc, char** argv) {
    Verilated::commandArgs(argc, argv);
    dut = new Vlru_victim_banked_tb_top;
    dut->rst_n = 0; dut->start = 0; dut->clk = 0; dut->eval();
    for (int i = 0; i < 3; i++) tick();
    dut->rst_n = 1; tick();

    srand(0x5EED);
    int trials = 2000, fails = 0;
    const int WORDS = (N * TSW) / 32;
    for (int t = 0; t < trials; t++) {
        // random valid bitmap (N bits) and ts[]
        uint32_t vbits[(N + 31) / 32] = {0};
        uint16_t ts[N];
        for (int k = 0; k < N; k++) {
            if (rand() & 1) vbits[k / 32] |= (1u << (k % 32));
            ts[k] = rand() & ((1u << TSW) - 1);
        }
        // software reference: argmin over valid, lowest index on ties
        int ref_found = 0, ref_victim = 0; uint32_t best = 0xFFFFFFFF;
        for (int k = 0; k < N; k++) {
            int vk = (vbits[k / 32] >> (k % 32)) & 1;
            if (vk && (!ref_found || ts[k] < best)) { ref_found = 1; best = ts[k]; ref_victim = k; }
        }
        // drive valid (VlWide or scalar) and ts_flat
        for (int w = 0; w < (N + 31) / 32; w++) dut->valid[w] = vbits[w];
        for (int w = 0; w < WORDS; w++)
            dut->ts_flat[w] = (uint32_t)ts[2 * w] | ((uint32_t)ts[2 * w + 1] << 16);
        dut->eval();
        int cf = dut->comb_found, cv = dut->comb_victim;
        // run banked search
        dut->start = 1; tick(); dut->start = 0;
        int guard = 0;
        while (!dut->bank_done) { tick(); if (++guard > 4 * N + 50) break; }
        int bf = dut->bank_found, bv = dut->bank_victim;

        bool ok = (cf == ref_found) && (bf == ref_found) &&
                  (!ref_found || (cv == ref_victim && bv == ref_victim));
        if (!ok) {
            fails++;
            if (fails <= 5)
                printf("{\"trial\":%d,\"ref\":[%d,%d],\"comb\":[%d,%d],\"bank\":[%d,%d]}\n",
                       t, ref_found, ref_victim, cf, cv, bf, bv);
        }
    }
    printf("{\"case\":\"lru_victim_banked_equiv\",\"N\":%d,\"B\":%d,\"trials\":%d,\"failures\":%d,\"pass\":%s}\n",
           N, TB_B, trials, fails, fails ? "false" : "true");
    delete dut;
    return fails ? 1 : 0;
}

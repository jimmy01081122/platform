// Verilator tb: prove lru_victim_seq selects the SAME victim as lru_victim_comb
// over randomized inputs (equivalence of the frequency-optimal sequential argmin
// against the throughput-optimal combinational argmin).
#include <verilated.h>
#include "Vlru_victim_tb_top.h"
#include <cstdio>
#include <cstdlib>

static Vlru_victim_tb_top* dut = nullptr;
static void tick() { dut->clk = 0; dut->eval(); dut->clk = 1; dut->eval(); }

// N=32, TSW=16 -> ts_flat is 512 bits = 16 x uint32 words
static const int N = 32, TSW = 16;

int main(int argc, char** argv) {
    Verilated::commandArgs(argc, argv);
    dut = new Vlru_victim_tb_top;
    dut->rst_n = 0; dut->start = 0; dut->clk = 0; dut->eval();
    for (int i = 0; i < 3; i++) tick();
    dut->rst_n = 1; tick();

    srand(0x5EED);
    int trials = 2000, fails = 0;
    for (int t = 0; t < trials; t++) {
        uint32_t valid = rand();                 // 32 valid bits
        uint16_t ts[N];
        for (int k = 0; k < N; k++) ts[k] = rand() & 0xFFFF;
        // reference argmin (lowest index on ties)
        int ref_found = 0, ref_victim = 0; uint16_t best = 0xFFFF;
        for (int k = 0; k < N; k++)
            if ((valid >> k) & 1) if (!ref_found || ts[k] < best) { ref_found = 1; best = ts[k]; ref_victim = k; }

        dut->valid = valid;
        for (int w = 0; w < (N * TSW) / 32; w++) {
            uint32_t word = (uint32_t)ts[2 * w] | ((uint32_t)ts[2 * w + 1] << 16);
            dut->ts_flat[w] = word;
        }
        dut->eval();
        // combinational result is immediate
        int cf = dut->comb_found, cv = dut->comb_victim;
        // run sequential search
        dut->start = 1; tick(); dut->start = 0;
        int guard = 0;
        while (!dut->seq_done) { tick(); if (++guard > 100) break; }
        int sf = dut->seq_found, sv = dut->seq_victim;

        bool ok = (cf == ref_found) && (sf == ref_found) &&
                  (!ref_found || (cv == ref_victim && sv == ref_victim));
        if (!ok) {
            fails++;
            if (fails <= 5)
                printf("{\"trial\":%d,\"ref\":[%d,%d],\"comb\":[%d,%d],\"seq\":[%d,%d]}\n",
                       t, ref_found, ref_victim, cf, cv, sf, sv);
        }
    }
    printf("{\"case\":\"lru_victim_equiv\",\"trials\":%d,\"failures\":%d,\"pass\":%s}\n",
           trials, fails, fails ? "false" : "true");
    delete dut;
    return fails ? 1 : 0;
}

// Verilator tb: prove the streaming expert_decompressor RTL is BIT-FOR-BIT identical to
// the golden integer dequant (edgeflow.expert_codec.decode_fixed) over randomized codes
// and scales. Fixed config LANES=8, SCW=16, FRACW=12, OUTW=16; code width NB swept via
// -DTB_NB (2/4/8) so equivalence is checked at every deployed code width.
#include <verilated.h>
#include "Vexpert_decompressor_tb_top.h"
#include <cstdio>
#include <cstdlib>
#include <cstdint>

#ifndef TB_NB
#define TB_NB 4
#endif
static const int NB = TB_NB, LANES = 8, FRACW = 12, OUTW = 16;

static Vexpert_decompressor_tb_top* dut = nullptr;
static void tick() { dut->clk = 0; dut->eval(); dut->clk = 1; dut->eval(); }

// golden: identical integer arithmetic to expert_codec.decode_fixed
static int64_t golden_lane(int64_t code, int64_t scale_q) {
    int64_t prod = code * scale_q + (1LL << (FRACW - 1));
    int64_t shifted = prod >> FRACW;                 // arithmetic (floor)
    int64_t lo = -(1LL << (OUTW - 1)), hi = (1LL << (OUTW - 1)) - 1;
    if (shifted > hi) shifted = hi;
    if (shifted < lo) shifted = lo;
    return shifted;
}

int main(int argc, char** argv) {
    Verilated::commandArgs(argc, argv);
    dut = new Vexpert_decompressor_tb_top;
    dut->rst_n = 0; dut->in_valid = 0; dut->clk = 0; dut->eval();
    for (int i = 0; i < 3; i++) tick();
    dut->rst_n = 1; tick();

    srand(0xC0DEC);
    const int trials = 5000;
    int fails = 0;
    const int codemask = (NB >= 64) ? -1 : ((1 << NB) - 1);
    const int64_t sign_bit = 1LL << (NB - 1);

    for (int t = 0; t < trials; t++) {
        // random packed codes (LANES*NB bits) + random signed 16-bit scale
        uint64_t packed = 0;
        int64_t codes[LANES];
        for (int l = 0; l < LANES; l++) {
            int cb = rand() & codemask;
            packed |= ((uint64_t)cb) << (l * NB);
            int64_t c = cb;
            if (c & sign_bit) c -= (1LL << NB);        // sign-extend NB-bit
            codes[l] = c;
        }
        int16_t scale_q = (int16_t)(rand() & 0xFFFF);

        dut->codes_packed = packed;                    // truncates to port width (ok)
        dut->scale_q = scale_q;
        dut->in_valid = 1;
        tick();                                        // edge 1: latch inputs
        dut->in_valid = 0;
        tick();                                        // edge 2: registered output ready (2-cycle latency)

        if (!dut->out_valid) { fails++; if (fails <= 5) printf("{\"t\":%d,\"err\":\"no_out_valid\"}\n", t); continue; }

        // read out_packed: 128 bits = 4 x uint32, each holding 2 int16 lanes
        for (int l = 0; l < LANES; l++) {
            uint32_t word = dut->out_packed[l / 2];
            int16_t got = (int16_t)((l & 1) ? (word >> 16) : (word & 0xFFFF));
            int64_t exp = golden_lane(codes[l], (int64_t)scale_q);
            if ((int64_t)got != exp) {
                fails++;
                if (fails <= 5)
                    printf("{\"t\":%d,\"lane\":%d,\"code\":%lld,\"scale\":%d,\"got\":%d,\"exp\":%lld}\n",
                           t, l, (long long)codes[l], scale_q, got, (long long)exp);
                break;
            }
        }
    }
    printf("{\"case\":\"expert_decompressor_equiv\",\"nb\":%d,\"lanes\":%d,\"fracw\":%d,\"outw\":%d,"
           "\"trials\":%d,\"failures\":%d,\"pass\":%s}\n",
           NB, LANES, FRACW, OUTW, trials, fails, fails ? "false" : "true");
    delete dut;
    return fails ? 1 : 0;
}

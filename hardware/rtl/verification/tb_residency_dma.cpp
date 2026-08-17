#include <verilated.h>
#include "Vresidency_engine_dv_top.h"

#include "dma_transaction_scoreboard.hpp"

#include <cstdint>
#include <cstdio>
#include <map>
#include <random>
#include <set>
#include <vector>

struct Step {
    uint32_t cur;
    uint32_t fut;
};

int main(int argc, char** argv) {
    Verilated::commandArgs(argc, argv);
    auto* dut = new Vresidency_engine_dv_top;
    DmaTransactionScoreboard scoreboard;
    VariableLatencyDmaResponder responder(1, 13, 3, 0x1a7e5eedu);
    std::mt19937 rng(0xc011ec7u);

    std::vector<Step> steps;
    for (int s = 0; s < 120; ++s) {
        uint32_t cur = 0;
        uint32_t fut = 0;
        for (int e = 0; e < 8; ++e) {
            if ((rng() % 5u) == 0u) cur |= 1u << e;
            if ((rng() % 6u) == 0u) fut |= 1u << e;
        }
        if (cur == 0) cur = 1u << (rng() % 8u);
        steps.push_back({cur, fut});
    }

    dut->clk = 0;
    dut->rst_n = 0;
    dut->cfg_num_experts = 8;
    dut->cfg_capacity = 4;
    dut->in_valid = 0;
    dut->in_cur_mask = 0;
    dut->in_fut_mask = 0;
    dut->out_ready = 0;
    dut->dma_req_ready = 0;
    dut->dma_cmpl_valid = 0;
    dut->dma_cmpl_tag = 0;
    dut->dma_cmpl_expert = 0;
    dut->dma_cmpl_kind = 0;

    uint64_t cycles = 0;
    auto tick = [&]() {
        dut->clk = 1;
        dut->eval();
        ++cycles;
        dut->clk = 0;
        dut->eval();
    };
    for (int i = 0; i < 4; ++i) tick();
    dut->rst_n = 1;
    tick();

    size_t next_step = 0;
    bool driving_step = false;
    size_t completed_steps = 0;
    uint32_t resident_q = dut->dbg_resident;
    uint64_t premature_resident = 0;
    uint64_t resident_rises = 0;
    uint64_t output_stalls = 0;
    uint64_t occupancy_checks = 0;
    uint64_t guard = 0;
    std::map<uint32_t, uint64_t> issue_cycle;
    std::set<uint64_t> observed_latencies;

    while ((completed_steps < steps.size() || !scoreboard.empty()) &&
           guard++ < 500000) {
        if (!driving_step && next_step < steps.size()) {
            driving_step = true;
            dut->in_cur_mask = steps[next_step].cur;
            dut->in_fut_mask = steps[next_step].fut;
        }
        dut->in_valid = driving_step;
        dut->out_ready = (rng() & 3u) != 0u;
        dut->dma_req_ready = responder.request_ready();
        dut->dma_cmpl_valid = responder.completion_valid();
        if (responder.completion_valid()) {
            const auto& c = responder.completion();
            dut->dma_cmpl_tag = c.tag;
            dut->dma_cmpl_expert = c.expert;
            dut->dma_cmpl_kind = c.kind;
        }
        dut->eval();

        const bool in_fire = dut->in_valid && dut->in_ready;
        const bool out_fire = dut->out_valid && dut->out_ready;
        const bool req_fire = dut->dma_req_valid && dut->dma_req_ready;
        const bool cmpl_fire = dut->dma_cmpl_valid && dut->dma_cmpl_ready;
        DmaTransaction req{};
        DmaTransaction cmpl{};

        if (dut->out_valid && !dut->out_ready) ++output_stalls;
        if (in_fire) {
            driving_step = false;
            ++next_step;
        }
        if (out_fire) ++completed_steps;
        if (req_fire) {
            req = {static_cast<uint32_t>(dut->dma_req_tag),
                   static_cast<uint32_t>(dut->dma_req_expert),
                   static_cast<uint32_t>(dut->dma_req_kind)};
            scoreboard.accept(req);
            issue_cycle[req.tag] = cycles;
            if (!responder.accept(req)) {
                std::fprintf(stderr, "responder rejected ready request\n");
                delete dut;
                return 1;
            }
        }
        if (cmpl_fire) {
            cmpl = {static_cast<uint32_t>(dut->dma_cmpl_tag),
                    static_cast<uint32_t>(dut->dma_cmpl_expert),
                    static_cast<uint32_t>(dut->dma_cmpl_kind)};
            scoreboard.complete(cmpl);
            observed_latencies.insert(cycles - issue_cycle[cmpl.tag]);
        }

        tick();

        const uint32_t resident_now = dut->dbg_resident;
        const uint32_t rose = resident_now & ~resident_q;
        if (rose) {
            resident_rises += __builtin_popcount(rose);
            const uint32_t allowed =
                cmpl_fire ? (1u << cmpl.expert) : 0u;
            if (rose & ~allowed) ++premature_resident;
        }
        resident_q = resident_now;

        responder.step(cmpl_fire);
        scoreboard.check_occupancy(responder.occupancy());
        ++occupancy_checks;
    }

    const bool exercised = scoreboard.accepted() != 0 &&
                           resident_rises != 0 && output_stalls != 0 &&
                           observed_latencies.size() >= 3;
    const bool pass = completed_steps == steps.size() && scoreboard.empty() &&
                      scoreboard.ok() && premature_resident == 0 &&
                      exercised && !Verilated::gotFinish();

    for (const auto& error : scoreboard.errors())
        std::fprintf(stderr, "SCOREBOARD: %s\n", error.c_str());
    std::printf(
        "{\"test\":\"residency_variable_latency\",\"seed\":%u,"
        "\"steps\":%zu,\"completed_steps\":%zu,\"requests\":%llu,"
        "\"completions\":%llu,\"cycles\":%llu,\"latencies_observed\":%zu,"
        "\"output_stall_cycles\":%llu,\"resident_rises\":%llu,"
        "\"premature_resident\":%llu,\"occupancy_checks\":%llu,"
        "\"pass\":%s}\n",
        0xc011ec7u, steps.size(), completed_steps,
        static_cast<unsigned long long>(scoreboard.accepted()),
        static_cast<unsigned long long>(scoreboard.completed()),
        static_cast<unsigned long long>(cycles), observed_latencies.size(),
        static_cast<unsigned long long>(output_stalls),
        static_cast<unsigned long long>(resident_rises),
        static_cast<unsigned long long>(premature_resident),
        static_cast<unsigned long long>(occupancy_checks),
        pass ? "true" : "false");

    delete dut;
    return pass ? 0 : 1;
}

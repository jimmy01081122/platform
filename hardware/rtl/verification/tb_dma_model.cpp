#include <verilated.h>
#include <verilated_cov.h>
#include "Vdma_model_tb_top.h"

#include "dma_transaction_scoreboard.hpp"

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <random>

static Vdma_model_tb_top* dut;
static uint64_t cycles;

static void eval_low() {
    dut->clk = 0;
    dut->eval();
}

static void rising_edge() {
    dut->clk = 1;
    dut->eval();
    ++cycles;
    dut->clk = 0;
    dut->eval();
}

static DmaTransaction request_payload() {
    return {static_cast<uint32_t>(dut->req_tag),
            static_cast<uint32_t>(dut->req_expert),
            static_cast<uint32_t>(dut->req_kind)};
}

static DmaTransaction completion_payload() {
    return {static_cast<uint32_t>(dut->cmpl_tag),
            static_cast<uint32_t>(dut->cmpl_expert),
            static_cast<uint32_t>(dut->cmpl_kind)};
}

int main(int argc, char** argv) {
    Verilated::commandArgs(argc, argv);
    dut = new Vdma_model_tb_top;
    DmaTransactionScoreboard scoreboard;
    std::mt19937 rng(0x5eed1234u);

    dut->rst_n = 0;
    dut->req_valid = 0;
    dut->req_tag = 0;
    dut->req_expert = 0;
    dut->req_kind = 0;
    dut->cmpl_ready = 0;
    for (int i = 0; i < 4; ++i) rising_edge();
    dut->rst_n = 1;
    rising_edge();

    constexpr uint32_t kRequests = 500;
    uint32_t next_tag = 1;
    bool driving_request = false;
    DmaTransaction driven{};
    bool held_completion = false;
    DmaTransaction held{};
    uint64_t req_stall_cycles = 0;
    uint64_t cmpl_stall_cycles = 0;
    uint64_t simultaneous_cycles = 0;
    uint64_t occupancy_checks = 0;
    uint64_t stability_errors = 0;
    uint64_t guard = 0;

    while (scoreboard.completed() < kRequests && guard++ < 200000) {
        if (!driving_request && next_tag <= kRequests &&
            ((rng() & 3u) != 0u)) {
            driven = {next_tag++, static_cast<uint32_t>(rng() & 7u),
                      static_cast<uint32_t>(rng() & 1u)};
            driving_request = true;
        }

        dut->req_valid = driving_request;
        if (driving_request) {
            dut->req_tag = driven.tag;
            dut->req_expert = driven.expert;
            dut->req_kind = driven.kind;
        }

        // Deterministic stall windows guarantee that output stability is
        // exercised; pseudorandom ready adds varied backpressure elsewhere.
        const bool forced_stall = ((cycles % 37u) >= 11u &&
                                   (cycles % 37u) <= 15u);
        dut->cmpl_ready = !forced_stall && ((rng() & 3u) != 0u);
        eval_low();

        const bool req_fire = dut->req_valid && dut->req_ready;
        const bool cmpl_fire = dut->cmpl_valid && dut->cmpl_ready;
        if (dut->req_valid && !dut->req_ready) ++req_stall_cycles;
        if (dut->cmpl_valid && !dut->cmpl_ready) ++cmpl_stall_cycles;
        if (req_fire && cmpl_fire) ++simultaneous_cycles;

        if (held_completion) {
            const DmaTransaction now = completion_payload();
            if (!dut->cmpl_valid || !(now == held)) ++stability_errors;
        }

        if (req_fire) {
            scoreboard.accept(request_payload());
            driving_request = false;
        }
        if (cmpl_fire) scoreboard.complete(completion_payload());

        held_completion = dut->cmpl_valid && !dut->cmpl_ready;
        if (held_completion) held = completion_payload();

        rising_edge();
        scoreboard.check_occupancy(dut->o_occupancy);
        ++occupancy_checks;
    }

    const bool drained = scoreboard.completed() == kRequests &&
                         scoreboard.empty();
    const bool counters_match =
        static_cast<uint64_t>(dut->o_completions) == scoreboard.completed();
    const bool exercised = req_stall_cycles != 0 && cmpl_stall_cycles != 0 &&
                           simultaneous_cycles != 0;
    const bool pass = drained && scoreboard.ok() && counters_match &&
                      stability_errors == 0 && exercised &&
                      !Verilated::gotFinish();

    for (const auto& error : scoreboard.errors())
        std::fprintf(stderr, "SCOREBOARD: %s\n", error.c_str());

    std::printf(
        "{\"test\":\"dma_model_random\",\"seed\":%u,\"requests\":%llu,"
        "\"completions\":%llu,\"cycles\":%llu,\"req_stall_cycles\":%llu,"
        "\"cmpl_stall_cycles\":%llu,\"simultaneous_cycles\":%llu,"
        "\"occupancy_checks\":%llu,\"stability_errors\":%llu,"
        "\"counter_match\":%s,\"drained\":%s,\"pass\":%s}\n",
        0x5eed1234u,
        static_cast<unsigned long long>(scoreboard.accepted()),
        static_cast<unsigned long long>(scoreboard.completed()),
        static_cast<unsigned long long>(cycles),
        static_cast<unsigned long long>(req_stall_cycles),
        static_cast<unsigned long long>(cmpl_stall_cycles),
        static_cast<unsigned long long>(simultaneous_cycles),
        static_cast<unsigned long long>(occupancy_checks),
        static_cast<unsigned long long>(stability_errors),
        counters_match ? "true" : "false", drained ? "true" : "false",
        pass ? "true" : "false");

    VerilatedCov::write("obj_dir/coverage.dat");
    delete dut;
    return pass ? 0 : 1;
}

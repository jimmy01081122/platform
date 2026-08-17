// Reusable transaction-level DMA verification utilities.
//
// The scoreboard is intentionally independent of Verilator-generated types so
// it can be reused by block and integration testbenches.
#pragma once

#include <algorithm>
#include <cstdint>
#include <deque>
#include <map>
#include <random>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

struct DmaTransaction {
    uint32_t tag;
    uint32_t expert;
    uint32_t kind;

    bool operator==(const DmaTransaction& rhs) const {
        return tag == rhs.tag && expert == rhs.expert && kind == rhs.kind;
    }
};

class DmaTransactionScoreboard {
  public:
    void reset() {
        outstanding_.clear();
        seen_completed_.clear();
        accepted_ = 0;
        completed_ = 0;
        errors_.clear();
    }

    void accept(const DmaTransaction& txn) {
        ++accepted_;
        if (outstanding_.count(txn.tag) || seen_completed_.count(txn.tag)) {
            fail("duplicate request tag", txn);
            return;
        }
        outstanding_.emplace(txn.tag, txn);
    }

    void complete(const DmaTransaction& actual) {
        ++completed_;
        const auto it = outstanding_.find(actual.tag);
        if (it == outstanding_.end()) {
            fail(seen_completed_.count(actual.tag) ? "duplicate completion"
                                                   : "completion without request",
                 actual);
            return;
        }
        if (!(it->second == actual)) {
            std::ostringstream os;
            os << "completion payload mismatch tag=" << actual.tag
               << " expected(expert=" << it->second.expert
               << ",kind=" << it->second.kind << ") actual(expert="
               << actual.expert << ",kind=" << actual.kind << ")";
            errors_.push_back(os.str());
        }
        seen_completed_.emplace(actual.tag, actual);
        outstanding_.erase(it);
    }

    bool check_occupancy(uint32_t dut_occupancy) {
        if (dut_occupancy == outstanding_.size()) return true;
        std::ostringstream os;
        os << "occupancy mismatch expected=" << outstanding_.size()
           << " actual=" << dut_occupancy;
        errors_.push_back(os.str());
        return false;
    }

    bool empty() const { return outstanding_.empty(); }
    bool ok() const { return errors_.empty(); }
    uint64_t accepted() const { return accepted_; }
    uint64_t completed() const { return completed_; }
    size_t outstanding() const { return outstanding_.size(); }
    const std::vector<std::string>& errors() const { return errors_; }

  private:
    void fail(const char* reason, const DmaTransaction& txn) {
        std::ostringstream os;
        os << reason << " tag=" << txn.tag << " expert=" << txn.expert
           << " kind=" << txn.kind;
        errors_.push_back(os.str());
    }

    std::map<uint32_t, DmaTransaction> outstanding_;
    std::map<uint32_t, DmaTransaction> seen_completed_;
    uint64_t accepted_ = 0;
    uint64_t completed_ = 0;
    std::vector<std::string> errors_;
};

// A cycle-stepped, variable-latency responder. Requests may mature out of
// order, but the selected completion remains stable until ready is asserted.
class VariableLatencyDmaResponder {
  public:
    VariableLatencyDmaResponder(uint32_t min_latency, uint32_t max_latency,
                                uint32_t capacity, uint32_t seed)
        : min_latency_(min_latency),
          max_latency_(max_latency),
          capacity_(capacity),
          rng_(seed),
          latency_dist_(min_latency, max_latency) {
        if (min_latency > max_latency || capacity == 0)
            throw std::invalid_argument("invalid DMA responder configuration");
    }

    void reset() {
        entries_.clear();
        output_valid_ = false;
        cycle_ = 0;
    }

    bool request_ready() const { return occupancy() < capacity_; }

    bool accept(const DmaTransaction& txn) {
        if (!request_ready()) return false;
        entries_.push_back({txn, cycle_ + latency_dist_(rng_)});
        return true;
    }

    void step(bool completion_ready) {
        if (output_valid_ && completion_ready) output_valid_ = false;
        if (!output_valid_) {
            auto due = std::min_element(
                entries_.begin(), entries_.end(),
                [](const Entry& a, const Entry& b) {
                    if (a.due_cycle != b.due_cycle)
                        return a.due_cycle < b.due_cycle;
                    return a.txn.tag < b.txn.tag;
                });
            if (due != entries_.end() && due->due_cycle <= cycle_) {
                output_ = due->txn;
                output_valid_ = true;
                entries_.erase(due);
            }
        }
        ++cycle_;
    }

    bool completion_valid() const { return output_valid_; }
    const DmaTransaction& completion() const { return output_; }
    size_t occupancy() const { return entries_.size() + output_valid_; }
    uint64_t cycle() const { return cycle_; }

  private:
    struct Entry {
        DmaTransaction txn;
        uint64_t due_cycle;
    };

    uint32_t min_latency_;
    uint32_t max_latency_;
    size_t capacity_;
    std::mt19937 rng_;
    std::uniform_int_distribution<uint32_t> latency_dist_;
    std::deque<Entry> entries_;
    DmaTransaction output_{};
    bool output_valid_ = false;
    uint64_t cycle_ = 0;
};

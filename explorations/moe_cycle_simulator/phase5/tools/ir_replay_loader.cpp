// Stage A3 IR -> engine loader.
//
// Reads a compact residency plan spec (emitted by the Python IR loader from an
// A2 nine-kind Canonical IR bundle plus the frozen routing .npy) and drives the
// existing Phase 5 RoutingResidencyModel (which in turn drives the Phase 4
// single-GPU service-time model). It emits the residency counters, terminal
// residency, semantic digests and Phase 4 timing observations as a single JSON
// object on stdout.
//
// This is a loader/harness only: it constructs Phase 5 PolicyPlan inputs and
// reads back results. It does not alter any engine algorithm. Residency
// semantics (LRU eviction, clean/immutable discard) are entirely the existing
// Phase 5 policy.
//
// Spec grammar (whitespace/newline separated tokens):
//   plan_id <token>
//   capacity_bytes <decimal>
//   eviction LRU|FIFO
//   prefetch OFF|HINT
//   compute_work <decimal>          # per-expert compute work (>=1)
//   h2d_num <decimal>               # H2D throughput numerator (bytes/second)
//   h2d_den <decimal>               # H2D throughput denominator
//   catalog <N> then N lines: <layer> <expert> <bytes>
//   base_resident <M> then M lines: <layer> <expert>
//   demands <D> then D lines: <layer> <expert>   # top_k = 1, order = sequence
//   end
//
// All integers are non-negative decimal. The loader assigns each demand a
// strictly increasing route_key time equal to its zero-based sequence index, so
// the Phase 5 canonical demand ordering reproduces the input demand order (the
// token-major flatten of the routing .npy).

#include "moe_sim/routing_residency_policy.hpp"

#include <cstdint>
#include <fstream>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

namespace {

using moe_sim::U128;
using moe_sim::to_decimal;
using moe_sim::phase4::Fidelity;
using moe_sim::phase4::RangeStatus;
using moe_sim::phase4::ServiceClass;
using moe_sim::phase4::ServiceProfile;
using moe_sim::phase4::SingleGpuPlatform;
using moe_sim::phase5::EvictionPolicy;
using moe_sim::phase5::ExecutionMode;
using moe_sim::phase5::ExpertKey;
using moe_sim::phase5::ExpertRecord;
using moe_sim::phase5::PolicyPlan;
using moe_sim::phase5::PolicyResult;
using moe_sim::phase5::PrefetchMode;
using moe_sim::phase5::RoutingResidencyModel;
using moe_sim::phase5::TokenDemand;

// The Phase 3 ledger sha the Phase 4 platform validation pins. This mirrors the
// value used by the Phase 5 test platform; it is a build-authority constant,
// not a measured or tunable parameter.
constexpr char kPhase3Ledger[] =
    "c4b9209d95bbf91c607d65a70062e3bbb03a5892807ce08d8a4a370000535e42";

struct Spec {
  std::string plan_id;
  U128 capacity_bytes{0};
  EvictionPolicy eviction{EvictionPolicy::kLru};
  PrefetchMode prefetch{PrefetchMode::kOff};
  U128 compute_work{1};
  U128 h2d_num{1};
  U128 h2d_den{1};
  std::vector<ExpertRecord> catalog;
  std::vector<ExpertKey> base_resident;
  std::vector<ExpertKey> demands;  // one expert per demand (top_k = 1)
};

[[noreturn]] void fail(const std::string& message) {
  throw std::runtime_error(message);
}

std::string next_token(std::istream& in, const char* what) {
  std::string token;
  if (!(in >> token)) {
    fail(std::string{"unexpected end of spec while reading "} + what);
  }
  return token;
}

U128 next_u128(std::istream& in, const char* what) {
  return moe_sim::parse_u128(next_token(in, what));
}

std::uint32_t next_u32(std::istream& in, const char* what) {
  const U128 value = next_u128(in, what);
  if (value > U128{0xFFFFFFFFULL}) {
    fail(std::string{"value out of 32-bit range for "} + what);
  }
  return static_cast<std::uint32_t>(value);
}

std::size_t next_size(std::istream& in, const char* what) {
  const U128 value = next_u128(in, what);
  if (value > U128{100'000'000ULL}) {
    fail(std::string{"count out of range for "} + what);
  }
  return static_cast<std::size_t>(value);
}

Spec read_spec(std::istream& in) {
  Spec spec;
  bool saw_end = false;
  std::string keyword;
  while (in >> keyword) {
    if (keyword == "plan_id") {
      spec.plan_id = next_token(in, "plan_id");
    } else if (keyword == "capacity_bytes") {
      spec.capacity_bytes = next_u128(in, "capacity_bytes");
    } else if (keyword == "eviction") {
      const std::string value = next_token(in, "eviction");
      if (value == "LRU") {
        spec.eviction = EvictionPolicy::kLru;
      } else if (value == "FIFO") {
        spec.eviction = EvictionPolicy::kFifo;
      } else {
        fail("unknown eviction policy: " + value);
      }
    } else if (keyword == "prefetch") {
      const std::string value = next_token(in, "prefetch");
      if (value == "OFF") {
        spec.prefetch = PrefetchMode::kOff;
      } else if (value == "HINT") {
        spec.prefetch = PrefetchMode::kHint;
      } else {
        fail("unknown prefetch mode: " + value);
      }
    } else if (keyword == "compute_work") {
      spec.compute_work = next_u128(in, "compute_work");
    } else if (keyword == "h2d_num") {
      spec.h2d_num = next_u128(in, "h2d_num");
    } else if (keyword == "h2d_den") {
      spec.h2d_den = next_u128(in, "h2d_den");
    } else if (keyword == "catalog") {
      const std::size_t count = next_size(in, "catalog count");
      spec.catalog.reserve(count);
      for (std::size_t index = 0; index < count; ++index) {
        const std::uint32_t layer = next_u32(in, "catalog layer");
        const std::uint32_t expert = next_u32(in, "catalog expert");
        const U128 bytes = next_u128(in, "catalog bytes");
        spec.catalog.push_back(ExpertRecord{{layer, expert}, bytes});
      }
    } else if (keyword == "base_resident") {
      const std::size_t count = next_size(in, "base_resident count");
      spec.base_resident.reserve(count);
      for (std::size_t index = 0; index < count; ++index) {
        const std::uint32_t layer = next_u32(in, "base_resident layer");
        const std::uint32_t expert = next_u32(in, "base_resident expert");
        spec.base_resident.push_back(ExpertKey{layer, expert});
      }
    } else if (keyword == "demands") {
      const std::size_t count = next_size(in, "demands count");
      spec.demands.reserve(count);
      for (std::size_t index = 0; index < count; ++index) {
        const std::uint32_t layer = next_u32(in, "demand layer");
        const std::uint32_t expert = next_u32(in, "demand expert");
        spec.demands.push_back(ExpertKey{layer, expert});
      }
    } else if (keyword == "end") {
      saw_end = true;
      break;
    } else {
      fail("unknown spec keyword: " + keyword);
    }
  }
  if (!saw_end) {
    fail("spec missing terminating 'end'");
  }
  if (spec.plan_id.empty()) {
    fail("spec missing plan_id");
  }
  if (spec.catalog.empty()) {
    fail("spec missing catalog");
  }
  if (spec.demands.empty()) {
    fail("spec missing demands");
  }
  return spec;
}

ServiceProfile profile(
    const std::string& id,
    ServiceClass service_class,
    bool shared,
    U128 numerator,
    U128 denominator) {
  return {
      id,
      service_class,
      1,
      numerator,
      denominator,
      U128{0},
      shared,
      Fidelity::kAnalyticFirstOrder,
      RangeStatus::kRangeUnknown};
}

SingleGpuPlatform build_platform(const Spec& spec) {
  // The compute/memory/D2H services are not on the measured OFF-E-PR3 timing
  // path; they carry a unit-rate functional profile. The H2D service carries
  // the measured interconnect bandwidth from PlatformIR so the observed Phase 4
  // makespan reflects the real per-object transfer time. None of these affect
  // the residency counters, which are order-only.
  const U128 unit_rate{1'000'000'000'000'000ULL};
  return {
      "phase5-ir-replay",
      1,
      moe_sim::Clock{
          "phase5-clock", 1'000'000'000'000'000ULL, 1, U128{0}, 0, 0},
      {
          profile("compute-v1", ServiceClass::kCompute, false, unit_rate,
                  U128{1}),
          profile("memory-v1", ServiceClass::kMemory, true, unit_rate, U128{1}),
          profile("h2d-v1", ServiceClass::kH2D, true, spec.h2d_num,
                  spec.h2d_den),
          profile("d2h-v1", ServiceClass::kD2H, true, unit_rate, U128{1}),
      },
      kPhase3Ledger,
      std::string(moe_sim::phase4::kPhase4BuildAuthoritySha256),
      std::string(moe_sim::phase4::kPhase4ModelContractSha256),
      std::string(moe_sim::phase4::kPhase4CheckpointSchemaSha256)};
}

PolicyPlan build_plan(const Spec& spec) {
  PolicyPlan plan;
  plan.plan_id = spec.plan_id;
  plan.execution_mode = ExecutionMode::kTraceCompiledNonAdaptive;
  plan.prefetch_mode = spec.prefetch;
  plan.eviction_policy = spec.eviction;
  plan.capacity_bytes = spec.capacity_bytes;
  plan.reserved_nonexpert_bytes = U128{0};
  plan.catalog = spec.catalog;
  plan.base_resident = spec.base_resident;
  plan.hints = {};
  plan.authority = {
      std::string(moe_sim::phase5::kPhase5BuildAuthoritySha256),
      std::string(moe_sim::phase5::kPhase5PolicyContractSha256),
      std::string(moe_sim::phase5::kPhase5CheckpointSchemaSha256)};

  plan.demands.reserve(spec.demands.size());
  for (std::size_t index = 0; index < spec.demands.size(); ++index) {
    const ExpertKey expert = spec.demands[index];
    const std::string tag = "d" + std::to_string(index);
    moe_sim::EventKey route_key;
    route_key.time_fs = U128{static_cast<std::uint64_t>(index)};
    route_key.event_priority = 100;
    route_key.request_id = std::string{"off-e-pr3"};
    route_key.token_index = static_cast<std::uint64_t>(index);
    route_key.layer_index = expert.layer;
    route_key.component_id = "router";
    route_key.event_id = "route-" + tag;

    TokenDemand demand;
    demand.demand_id = tag;
    demand.route_key = route_key;
    demand.top_k = 1;
    demand.selected_experts = {expert};
    demand.routing_provenance = "off-e-pr3-routing-npy";
    demand.compute_work_per_expert = spec.compute_work;
    plan.demands.push_back(std::move(demand));
  }
  return plan;
}

std::string class_name(ServiceClass value) {
  switch (value) {
    case ServiceClass::kCompute:
      return "COMPUTE";
    case ServiceClass::kMemory:
      return "MEMORY";
    case ServiceClass::kH2D:
      return "H2D";
    case ServiceClass::kD2H:
      return "D2H";
  }
  return "UNKNOWN";
}

std::string status_name(moe_sim::TerminalStatus value) {
  switch (value) {
    case moe_sim::TerminalStatus::kRunning:
      return "RUNNING";
    case moe_sim::TerminalStatus::kQuiescent:
      return "QUIESCENT";
    case moe_sim::TerminalStatus::kDeadlock:
      return "DEADLOCK";
    case moe_sim::TerminalStatus::kZeno:
      return "ZENO";
    case moe_sim::TerminalStatus::kFailed:
      return "FAILED";
  }
  return "UNKNOWN";
}

void emit_json(std::ostream& out, const PolicyResult& result) {
  const auto& m = result.metrics;
  out << "{";
  out << "\"plan_digest\":\"" << result.plan_digest << "\",";
  out << "\"semantic_digest\":\"" << result.semantic_digest << "\",";
  out << "\"terminal_residency_digest\":\""
      << result.terminal_residency_digest << "\",";
  out << "\"phase4_semantic_digest\":\"" << result.phase4_semantic_digest
      << "\",";
  out << "\"routing_demands\":" << m.routing_demands << ",";
  out << "\"assignments\":" << m.assignments << ",";
  out << "\"loads\":" << m.loads << ",";
  out << "\"clean_evictions\":" << m.clean_evictions << ",";
  out << "\"prefetch_loads\":" << m.prefetch_loads << ",";
  out << "\"useful_prefetches\":" << m.useful_prefetches << ",";
  out << "\"wasted_prefetches\":" << m.wasted_prefetches << ",";
  out << "\"ignored_hints\":" << m.ignored_hints << ",";
  out << "\"replay_action_lookups\":" << m.replay_action_lookups << ",";
  out << "\"peak_resident_bytes\":\"" << to_decimal(m.peak_resident_bytes)
      << "\",";
  out << "\"terminal_resident_bytes\":\""
      << to_decimal(result.terminal_resident_bytes) << "\",";
  out << "\"terminal_resident_count\":" << result.terminal_resident.size()
      << ",";
  out << "\"terminal_resident\":[";
  bool first = true;
  for (const auto& key : result.terminal_resident) {
    if (!first) out << ",";
    first = false;
    out << "[" << key.layer << "," << key.expert << "]";
  }
  out << "],";
  const auto& schedule = result.phase4_result;
  out << "\"makespan_fs\":\"" << to_decimal(schedule.makespan_fs) << "\",";
  out << "\"terminal_status\":\"" << status_name(schedule.terminal_status)
      << "\",";
  out << "\"trace_size\":" << schedule.trace.size() << ",";
  out << "\"class_metrics\":{";
  first = true;
  for (const auto& [service_class, metrics] : schedule.class_metrics) {
    if (!first) out << ",";
    first = false;
    out << "\"" << class_name(service_class) << "\":{";
    out << "\"operation_count\":" << metrics.operation_count << ",";
    out << "\"busy_lane_fs\":\"" << to_decimal(metrics.busy_lane_fs) << "\",";
    out << "\"queue_delay_fs\":\"" << to_decimal(metrics.queue_delay_fs)
        << "\"}";
  }
  out << "}";
  out << "}";
  out << "\n";
}

}  // namespace

int main(int argc, char** argv) {
  try {
    Spec spec;
    if (argc >= 2 && std::string{argv[1]} != "-") {
      std::ifstream file(argv[1]);
      if (!file) {
        std::cerr << "cannot open spec file: " << argv[1] << "\n";
        return 2;
      }
      spec = read_spec(file);
    } else {
      spec = read_spec(std::cin);
    }
    RoutingResidencyModel model(build_platform(spec), build_plan(spec));
    const PolicyResult result = model.run_until_quiescent();
    emit_json(std::cout, result);
    return 0;
  } catch (const std::exception& error) {
    std::cerr << "ir_replay_loader error: " << error.what() << "\n";
    return 1;
  }
}

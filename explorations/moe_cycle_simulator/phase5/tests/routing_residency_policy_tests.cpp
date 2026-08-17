#include "moe_sim/routing_residency_policy.hpp"

#include <algorithm>
#include <chrono>
#include <iostream>
#include <set>
#include <string>
#include <vector>

namespace {

using moe_sim::EngineError;
using moe_sim::EventKey;
using moe_sim::TerminalStatus;
using moe_sim::U128;
using moe_sim::phase4::Fidelity;
using moe_sim::phase4::RangeStatus;
using moe_sim::phase4::ServiceClass;
using moe_sim::phase4::ServiceProfile;
using moe_sim::phase4::SingleGpuPlatform;
using moe_sim::phase5::ActionKind;
using moe_sim::phase5::EvictionPolicy;
using moe_sim::phase5::ExecutionMode;
using moe_sim::phase5::ExpertKey;
using moe_sim::phase5::ExpertRecord;
using moe_sim::phase5::PolicyPlan;
using moe_sim::phase5::PrefetchHint;
using moe_sim::phase5::PrefetchMode;
using moe_sim::phase5::RoutingResidencyModel;
using moe_sim::phase5::TokenDemand;

int failures = 0;

void require(bool condition, const std::string& message) {
  if (!condition) {
    ++failures;
    std::cerr << "FAIL: " << message << "\n";
  }
}

template <typename Callable>
void require_throws(Callable&& callable, const std::string& needle) {
  try {
    callable();
    require(false, "expected EngineError containing " + needle);
  } catch (const EngineError& error) {
    require(
        std::string{error.what()}.find(needle) != std::string::npos,
        "unexpected EngineError: " + std::string{error.what()});
  }
}

ServiceProfile profile(
    const std::string& id,
    ServiceClass service_class,
    bool shared) {
  return {
      id,
      service_class,
      1,
      U128{1'000'000'000'000'000ULL},
      U128{1},
      U128{0},
      shared,
      Fidelity::kAnalyticFirstOrder,
      RangeStatus::kRangeUnknown};
}

SingleGpuPlatform platform() {
  return {
      "phase5-cpu-synthetic",
      1,
      moe_sim::Clock{
          "phase5-clock", 1'000'000'000'000'000ULL, 1, U128{0}, 0, 0},
      {
          profile("compute-v1", ServiceClass::kCompute, false),
          profile("memory-v1", ServiceClass::kMemory, true),
          profile("h2d-v1", ServiceClass::kH2D, true),
          profile("d2h-v1", ServiceClass::kD2H, true),
      },
      "c4b9209d95bbf91c607d65a70062e3bbb03a5892807ce08d8a4a370000535e42",
      std::string(moe_sim::phase4::kPhase4BuildAuthoritySha256),
      std::string(moe_sim::phase4::kPhase4ModelContractSha256),
      std::string(moe_sim::phase4::kPhase4CheckpointSchemaSha256)};
}

EventKey key(
    const std::string& id, std::uint64_t time, std::uint64_t token) {
  return {
      U128{time}, 100, std::string{"request-1"}, token, std::uint32_t{0},
      "router", id};
}

TokenDemand demand(
    const std::string& id,
    std::uint64_t time,
    std::uint64_t token,
    std::vector<ExpertKey> selected) {
  return {
      id,
      key("route-" + id, time, token),
      static_cast<std::uint32_t>(selected.size()),
      std::move(selected),
      "synthetic-routing-provenance-" + id,
      U128{3}};
}

PolicyPlan base_plan(EvictionPolicy eviction = EvictionPolicy::kLru) {
  return {
      "phase5-policy-plan",
      ExecutionMode::kTraceCompiledNonAdaptive,
      PrefetchMode::kOff,
      eviction,
      U128{20},
      U128{0},
      {
          ExpertRecord{{0, 0}, U128{10}},
          ExpertRecord{{0, 1}, U128{10}},
          ExpertRecord{{0, 2}, U128{10}},
          ExpertRecord{{0, 3}, U128{10}},
      },
      {{0, 0}, {0, 1}},
      {
          demand("d0", 10, 0, {{0, 0}}),
          demand("d1", 20, 1, {{0, 2}}),
      },
      {},
      {
          std::string(moe_sim::phase5::kPhase5BuildAuthoritySha256),
          std::string(moe_sim::phase5::kPhase5PolicyContractSha256),
          std::string(moe_sim::phase5::kPhase5CheckpointSchemaSha256),
      }};
}

const auto& action(
    const moe_sim::phase5::CompiledPlan& compiled,
    ActionKind kind,
    const std::string& source) {
  return *std::find_if(
      compiled.actions.begin(), compiled.actions.end(),
      [&](const auto& item) {
        return item.kind == kind && item.source_id == source;
      });
}

void test_compile_replay_and_conservation() {
  RoutingResidencyModel model(platform(), base_plan());
  const auto& compiled = model.compiled_plan();
  const auto& route = action(compiled, ActionKind::kRouteBarrier, "d1");
  const auto& evict = action(compiled, ActionKind::kCleanEvict, "d1");
  const auto& load = action(compiled, ActionKind::kH2DLoad, "d1");
  const auto& compute =
      action(compiled, ActionKind::kComputeAssignment, "d1");
  require(
      evict.service_class == ServiceClass::kMemory &&
          load.service_class == ServiceClass::kH2D &&
          compute.service_class == ServiceClass::kCompute,
      "evict/load/assignment use explicit Phase4 service classes");
  require(
      load.dependencies == std::vector<std::string>{evict.action_id} &&
          route.dependencies == std::vector<std::string>{load.action_id} &&
          compute.dependencies == std::vector<std::string>{route.action_id},
      "residency chain joins at route barrier before compute");
  require(
      !evict.dependencies.empty() &&
          load.dependencies == std::vector<std::string>{evict.action_id} &&
          evict.expert != compute.expert,
      "eviction is causal and never selects a required expert");
  require(
      evict.expert == std::optional<ExpertKey>{{0, 1}},
      "LRU victim is not the required expert");

  const auto result = model.run_until_quiescent();
  require(
      result.metrics.routing_demands == 2 &&
          result.metrics.assignments == 2 &&
          result.metrics.loads == 1 &&
          result.metrics.clean_evictions == 1,
      "routing, assignment and residency actions conserve counts");
  require(
      result.terminal_resident ==
              std::set<ExpertKey>{{0, 0}, {0, 2}} &&
          result.terminal_resident_bytes == 20 &&
          result.metrics.peak_resident_bytes <= 20,
      "terminal LRU residency and capacity conservation");
  require(
      result.phase4_result.trace.size() == compiled.actions.size() * 2 &&
          result.metrics.replay_action_lookups ==
              result.phase4_result.trace.size() &&
          result.semantic_digest.size() == 64 &&
          result.plan_digest.size() == 64,
      "complete replay produces semantic digests");
  for (const auto& item : compiled.actions) {
    const auto scheduled = std::find_if(
        result.phase4_result.entries.begin(),
        result.phase4_result.entries.end(),
        [&](const auto& entry) { return entry.operation_id == item.action_id; });
    require(
        scheduled != result.phase4_result.entries.end(),
        "every compiled action is scheduled exactly once");
    if (scheduled == result.phase4_result.entries.end()) continue;
    for (const auto& dependency : item.dependencies) {
      const auto predecessor = std::find_if(
          result.phase4_result.entries.begin(),
          result.phase4_result.entries.end(),
          [&](const auto& entry) {
            return entry.operation_id == dependency;
          });
      require(
          predecessor != result.phase4_result.entries.end() &&
              predecessor->end_fs <= scheduled->start_fs,
          "scheduled action respects compiled causality");
    }
  }
}

void test_determinism_and_policy_ablation() {
  PolicyPlan canonical = base_plan();
  RoutingResidencyModel first(platform(), canonical);
  const auto first_result = first.run_until_quiescent();
  std::reverse(canonical.catalog.begin(), canonical.catalog.end());
  std::reverse(canonical.base_resident.begin(), canonical.base_resident.end());
  std::reverse(canonical.demands.begin(), canonical.demands.end());
  RoutingResidencyModel permuted(platform(), canonical);
  const auto permuted_result = permuted.run_until_quiescent();
  require(
      first_result.plan_digest == permuted_result.plan_digest &&
          first_result.semantic_digest == permuted_result.semantic_digest,
      "canonical input permutation is deterministic");

  RoutingResidencyModel fifo(platform(), base_plan(EvictionPolicy::kFifo));
  const auto fifo_result = fifo.run_until_quiescent();
  require(
      fifo_result.terminal_resident ==
          std::set<ExpertKey>{{0, 1}, {0, 2}},
      "FIFO ablation evicts oldest load instead of least recently used");
  require(
      fifo_result.plan_digest != first_result.plan_digest,
      "policy choice is bound into plan digest");
}

PolicyPlan hint_plan(PrefetchMode mode) {
  PolicyPlan plan = base_plan();
  plan.prefetch_mode = mode;
  plan.demands = {
      demand("d0", 10, 0, {{0, 0}}),
      demand("d1", 30, 1, {{0, 2}}),
      demand("d2", 50, 2, {{0, 0}}),
      demand("d-mid", 60, 3, {{0, 1}}),
      demand("d3", 80, 4, {{0, 3}}),
  };
  plan.hints = {
      PrefetchHint{
          "h-useful", key("hint-useful", 20, 1), "d1", {0, 2},
          "synthetic-hint-useful"},
      PrefetchHint{
          "h-wasted", key("hint-wasted", 40, 2), "d3", {0, 3},
          "synthetic-hint-wasted"},
  };
  return plan;
}

void test_hint_useful_wasted_and_off() {
  RoutingResidencyModel hint(platform(), hint_plan(PrefetchMode::kHint));
  const auto result = hint.run_until_quiescent();
  require(
      result.metrics.prefetch_loads == 2 &&
          result.metrics.useful_prefetches == 1 &&
          result.metrics.wasted_prefetches == 1 &&
          result.metrics.ignored_hints == 0,
      "hint policy classifies useful and wasted prefetch loads");
  RoutingResidencyModel off(platform(), hint_plan(PrefetchMode::kOff));
  const auto off_result = off.run_until_quiescent();
  require(
      off_result.metrics.prefetch_loads == 0 &&
          off_result.metrics.useful_prefetches == 0 &&
          off_result.metrics.wasted_prefetches == 0 &&
          off_result.metrics.ignored_hints == 2,
      "OFF policy records explicit arrived hints without prefetching");
}

void test_live_checkpoint_object_and_wire() {
  const PolicyPlan plan = hint_plan(PrefetchMode::kHint);
  RoutingResidencyModel continuous(platform(), plan);
  for (int index = 0; index < 7; ++index) {
    const auto trace = continuous.step();
    require(!trace.operation_id.empty(), "checkpoint prefix is mid-run");
  }
  const auto object = continuous.checkpoint();
  const std::string wire = continuous.serialize_checkpoint();
  require(
      object.trace_prefix_size ==
              object.phase4_checkpoint.result.trace.size() &&
          object.phase4_checkpoint.terminal_status ==
              TerminalStatus::kRunning,
      "checkpoint binds the live Phase4 prefix");

  RoutingResidencyModel restored =
      RoutingResidencyModel::restore(platform(), plan, object);
  RoutingResidencyModel wire_restored =
      RoutingResidencyModel::restore_serialized(platform(), plan, wire);
  const auto continuous_result = continuous.run_until_quiescent();
  const auto restored_result = restored.run_until_quiescent();
  const auto wire_result = wire_restored.run_until_quiescent();
  require(
      continuous_result.semantic_digest == restored_result.semantic_digest &&
          continuous_result.semantic_digest == wire_result.semantic_digest &&
          continuous_result.phase4_semantic_digest ==
              restored_result.phase4_semantic_digest,
      "continuous, object-restored and wire-restored results are exact");

  auto tampered = object;
  tampered.plan_digest = std::string(64, '0');
  require_throws(
      [&] {
        static_cast<void>(
            RoutingResidencyModel::restore(platform(), plan, tampered));
      },
      "plan/prefix");
  tampered = object;
  tampered.residency_digest = std::string(64, '0');
  require_throws(
      [&] {
        static_cast<void>(
            RoutingResidencyModel::restore(platform(), plan, tampered));
      },
      "residency");
  tampered = object;
  ++tampered.phase4_checkpoint.global_time_fs;
  require_throws(
      [&] {
        static_cast<void>(
            RoutingResidencyModel::restore(platform(), plan, tampered));
      },
      "checkpoint");

  std::string corrupt = wire;
  corrupt[corrupt.size() / 2] =
      corrupt[corrupt.size() / 2] == 'x' ? 'y' : 'x';
  require_throws(
      [&] {
        static_cast<void>(
            RoutingResidencyModel::restore_serialized(
                platform(), plan, corrupt));
      },
      "digest");
  require_throws(
      [&] {
        static_cast<void>(
            RoutingResidencyModel::restore_serialized(
                platform(), plan, wire + "x"));
      },
      "boundary");
}

void test_fail_closed_inputs_and_capacity() {
  PolicyPlan invalid = base_plan();
  invalid.demands[0].selected_experts[0] = {0, 99};
  require_throws(
      [&] { static_cast<void>(RoutingResidencyModel::compile(invalid)); },
      "selected expert");

  invalid = base_plan();
  invalid.demands[0].selected_experts.push_back({0, 0});
  invalid.demands[0].top_k = 2;
  require_throws(
      [&] { static_cast<void>(RoutingResidencyModel::compile(invalid)); },
      "selected expert");

  invalid = base_plan();
  invalid.demands[0].top_k = 2;
  require_throws(
      [&] { static_cast<void>(RoutingResidencyModel::compile(invalid)); },
      "token demand");

  invalid = base_plan();
  invalid.demands[0].route_key.token_index.reset();
  require_throws(
      [&] { static_cast<void>(RoutingResidencyModel::compile(invalid)); },
      "token demand");

  invalid = base_plan();
  invalid.capacity_bytes = 10;
  invalid.base_resident = {{0, 0}};
  invalid.demands = {demand("wide", 10, 0, {{0, 0}, {0, 1}})};
  require_throws(
      [&] { static_cast<void>(RoutingResidencyModel::compile(invalid)); },
      "required expert set");

  invalid = base_plan();
  invalid.capacity_bytes = 10;
  require_throws(
      [&] { static_cast<void>(RoutingResidencyModel::compile(invalid)); },
      "base allocation");

  invalid = base_plan();
  invalid.reserved_nonexpert_bytes = 1;
  require_throws(
      [&] { static_cast<void>(RoutingResidencyModel::compile(invalid)); },
      "base allocation");

  PolicyPlan reserved = base_plan();
  reserved.capacity_bytes = 25;
  reserved.reserved_nonexpert_bytes = 5;
  RoutingResidencyModel reserved_model(platform(), std::move(reserved));
  const auto reserved_result = reserved_model.run_until_quiescent();
  require(
      reserved_result.metrics.peak_resident_bytes == 20,
      "reserved nonexpert bytes share the authoritative total capacity");

  invalid = base_plan();
  invalid.execution_mode = static_cast<ExecutionMode>(99);
  require_throws(
      [&] { static_cast<void>(RoutingResidencyModel::compile(invalid)); },
      "plan boundary");

  invalid = base_plan();
  invalid.prefetch_mode = static_cast<PrefetchMode>(99);
  require_throws(
      [&] { static_cast<void>(RoutingResidencyModel::compile(invalid)); },
      "plan boundary");

  invalid = base_plan();
  invalid.authority.phase5_build_sha256 = std::string(64, '0');
  require_throws(
      [&] { static_cast<void>(RoutingResidencyModel::compile(invalid)); },
      "plan boundary");

  invalid = hint_plan(PrefetchMode::kHint);
  invalid.hints[0].arrival_key.time_fs = 31;
  require_throws(
      [&] { static_cast<void>(RoutingResidencyModel::compile(invalid)); },
      "precede");

  invalid = hint_plan(PrefetchMode::kHint);
  invalid.hints[0].expert = {0, 3};
  require_throws(
      [&] { static_cast<void>(RoutingResidencyModel::compile(invalid)); },
      "target expert");

  invalid = base_plan();
  invalid.catalog.push_back(invalid.catalog.front());
  require_throws(
      [&] { static_cast<void>(RoutingResidencyModel::compile(invalid)); },
      "catalog");
}

void test_scale_replay_index() {
  PolicyPlan plan = base_plan();
  plan.capacity_bytes = 10;
  plan.catalog = {ExpertRecord{{0, 0}, U128{10}}};
  plan.base_resident = {{0, 0}};
  plan.demands.clear();
  for (std::uint64_t index = 0; index < 1000; ++index) {
    plan.demands.push_back(
        demand(
            "scale-" + std::to_string(index), index + 1, index, {{0, 0}}));
  }
  const auto begin = std::chrono::steady_clock::now();
  RoutingResidencyModel model(platform(), std::move(plan));
  const auto result = model.run_until_quiescent();
  const auto elapsed = std::chrono::duration_cast<std::chrono::seconds>(
      std::chrono::steady_clock::now() - begin);
  require(
      result.metrics.routing_demands == 1000 &&
          result.metrics.assignments == 1000 &&
          result.metrics.replay_action_lookups == 4000,
      "1000-demand fixture uses one indexed lookup per trace event");
  require(
      elapsed.count() < 10,
      "1000-demand CPU synthetic replay operational envelope");
}

}  // namespace

int main() {
  test_compile_replay_and_conservation();
  test_determinism_and_policy_ablation();
  test_hint_useful_wasted_and_off();
  test_live_checkpoint_object_and_wire();
  test_fail_closed_inputs_and_capacity();
  test_scale_replay_index();
  if (failures != 0) {
    std::cerr << failures << " checks failed\n";
    return 1;
  }
  std::cout << "PHASE5_ROUTING_RESIDENCY_TESTS: PASS\n";
  return 0;
}

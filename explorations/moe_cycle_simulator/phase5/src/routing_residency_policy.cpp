#include "moe_sim/routing_residency_policy.hpp"

#include <algorithm>
#include <charconv>
#include <limits>
#include <sstream>
#include <tuple>
#include <utility>

namespace moe_sim::phase5 {
namespace {

using boost::multiprecision::cpp_int;

constexpr std::size_t kMaximumItems = 100'000;

struct ResidentMeta {
  std::uint64_t load_sequence;
  std::uint64_t last_use_sequence;
};

std::string execution_name(ExecutionMode value) {
  if (value == ExecutionMode::kTraceCompiledNonAdaptive) {
    return "TRACE_COMPILED_NON_ADAPTIVE";
  }
  throw EngineError("invalid Phase 5 execution mode");
}

std::string prefetch_name(PrefetchMode value) {
  switch (value) {
    case PrefetchMode::kOff: return "OFF";
    case PrefetchMode::kHint: return "HINT";
  }
  throw EngineError("invalid Phase 5 prefetch mode");
}

std::string eviction_name(EvictionPolicy value) {
  switch (value) {
    case EvictionPolicy::kLru: return "LRU";
    case EvictionPolicy::kFifo: return "FIFO";
  }
  throw EngineError("invalid Phase 5 eviction policy");
}

std::string action_name(ActionKind value) {
  switch (value) {
    case ActionKind::kHintBarrier: return "HINT_BARRIER";
    case ActionKind::kRouteBarrier: return "ROUTE_BARRIER";
    case ActionKind::kCleanEvict: return "CLEAN_EVICT";
    case ActionKind::kH2DLoad: return "H2D_LOAD";
    case ActionKind::kComputeAssignment: return "COMPUTE_ASSIGNMENT";
  }
  throw EngineError("invalid Phase 5 action kind");
}

std::string class_name(phase4::ServiceClass value) {
  switch (value) {
    case phase4::ServiceClass::kCompute: return "COMPUTE";
    case phase4::ServiceClass::kMemory: return "MEMORY";
    case phase4::ServiceClass::kH2D: return "H2D";
    case phase4::ServiceClass::kD2H: return "D2H";
  }
  throw EngineError("invalid Phase 5 service class");
}

void put(std::ostringstream& stream, const std::string& value) {
  stream << value.size() << ':' << value;
}

void put_expert(std::ostringstream& stream, const ExpertKey& key) {
  put(stream, std::to_string(key.layer));
  put(stream, std::to_string(key.expert));
}

void put_event_key(std::ostringstream& stream, const EventKey& key) {
  put(stream, to_decimal(key.time_fs));
  put(stream, std::to_string(key.event_priority));
  put(stream, key.request_id.has_value() ? "1" : "0");
  put(stream, key.request_id.value_or(""));
  put(stream, key.token_index.has_value() ? "1" : "0");
  put(
      stream,
      key.token_index.has_value() ? std::to_string(*key.token_index) : "");
  put(stream, key.layer_index.has_value() ? "1" : "0");
  put(
      stream,
      key.layer_index.has_value() ? std::to_string(*key.layer_index) : "");
  put(stream, key.component_id);
  put(stream, key.event_id);
}

U128 checked_add(const U128& left, const U128& right, const char* label) {
  const cpp_int value = cpp_int{left} + cpp_int{right};
  const cpp_int maximum = (cpp_int{1} << 128) - 1;
  if (value > maximum) {
    throw EngineError(std::string{"Phase 5 "} + label + " overflow");
  }
  return static_cast<U128>(value);
}

U128 checked_sub(const U128& left, const U128& right, const char* label) {
  if (right > left) {
    throw EngineError(std::string{"Phase 5 "} + label + " underflow");
  }
  return left - right;
}

std::size_t parse_size(const std::string& value, const char* label) {
  std::size_t parsed = 0;
  const auto result =
      std::from_chars(value.data(), value.data() + value.size(), parsed);
  if (result.ec != std::errc{} ||
      result.ptr != value.data() + value.size()) {
    throw EngineError(std::string{"invalid Phase 5 "} + label);
  }
  return parsed;
}

std::string take(const std::string& body, std::size_t& cursor) {
  const std::size_t colon = body.find(':', cursor);
  if (colon == std::string::npos) {
    throw EngineError("invalid Phase 5 checkpoint token");
  }
  const std::size_t length =
      parse_size(body.substr(cursor, colon - cursor), "token length");
  const std::size_t begin = colon + 1;
  if (length > body.size() - begin) {
    throw EngineError("invalid Phase 5 checkpoint token boundary");
  }
  cursor = begin + length;
  return body.substr(begin, length);
}

std::map<ExpertKey, U128> catalog_map(const PolicyPlan& plan) {
  std::map<ExpertKey, U128> result;
  for (const auto& record : plan.catalog) {
    result.emplace(record.key, record.bytes);
  }
  return result;
}

std::string canonical_plan(const PolicyPlan& input) {
  PolicyPlan plan = input;
  std::sort(
      plan.catalog.begin(), plan.catalog.end(),
      [](const auto& left, const auto& right) {
        return left.key < right.key;
      });
  std::sort(plan.base_resident.begin(), plan.base_resident.end());
  std::sort(
      plan.demands.begin(), plan.demands.end(),
      [](const auto& left, const auto& right) {
        return std::tuple{left.route_key, left.demand_id} <
               std::tuple{right.route_key, right.demand_id};
      });
  std::sort(
      plan.hints.begin(), plan.hints.end(),
      [](const auto& left, const auto& right) {
        return std::tuple{left.arrival_key, left.hint_id} <
               std::tuple{right.arrival_key, right.hint_id};
      });
  std::ostringstream stream;
  put(stream, "moe-phase5-plan-v1");
  put(stream, plan.plan_id);
  put(stream, execution_name(plan.execution_mode));
  put(stream, prefetch_name(plan.prefetch_mode));
  put(stream, eviction_name(plan.eviction_policy));
  put(stream, to_decimal(plan.capacity_bytes));
  put(stream, to_decimal(plan.reserved_nonexpert_bytes));
  put(stream, plan.authority.phase5_build_sha256);
  put(stream, plan.authority.policy_contract_sha256);
  put(stream, plan.authority.checkpoint_schema_sha256);
  put(stream, std::to_string(plan.catalog.size()));
  for (const auto& record : plan.catalog) {
    put_expert(stream, record.key);
    put(stream, to_decimal(record.bytes));
  }
  put(stream, std::to_string(plan.base_resident.size()));
  for (const auto& key : plan.base_resident) put_expert(stream, key);
  put(stream, std::to_string(plan.demands.size()));
  for (const auto& demand : plan.demands) {
    put(stream, demand.demand_id);
    put_event_key(stream, demand.route_key);
    put(stream, std::to_string(demand.top_k));
    put(stream, std::to_string(demand.selected_experts.size()));
    for (const auto& key : demand.selected_experts) put_expert(stream, key);
    put(stream, demand.routing_provenance);
    put(stream, to_decimal(demand.compute_work_per_expert));
  }
  put(stream, std::to_string(plan.hints.size()));
  for (const auto& hint : plan.hints) {
    put(stream, hint.hint_id);
    put_event_key(stream, hint.arrival_key);
    put(stream, hint.target_demand_id);
    put_expert(stream, hint.expert);
    put(stream, hint.provenance);
  }
  return stream.str();
}

std::string action_id(
    const std::string& plan_digest,
    std::size_t sequence,
    ActionKind kind,
    const std::optional<ExpertKey>& expert,
    const std::string& source_id,
    const std::vector<std::string>& dependencies,
    phase4::ServiceClass service_class,
    const U128& work,
    const U128& release,
    bool prefetch_load) {
  std::ostringstream stream;
  put(stream, "moe-phase5-compiled-action-v1");
  put(stream, plan_digest);
  put(stream, std::to_string(sequence));
  put(stream, action_name(kind));
  put(stream, expert.has_value() ? "1" : "0");
  if (expert.has_value()) put_expert(stream, *expert);
  put(stream, source_id);
  put(stream, std::to_string(dependencies.size()));
  for (const auto& dependency : dependencies) put(stream, dependency);
  put(stream, class_name(service_class));
  put(stream, to_decimal(work));
  put(stream, to_decimal(release));
  put(stream, prefetch_load ? "1" : "0");
  return sha256_bytes(stream.str());
}

const TokenDemand& demand_by_id(
    const PolicyPlan& plan, const std::string& id) {
  const auto found = std::find_if(
      plan.demands.begin(), plan.demands.end(),
      [&](const auto& demand) { return demand.demand_id == id; });
  if (found == plan.demands.end()) {
    throw EngineError("unknown Phase 5 demand");
  }
  return *found;
}

}  // namespace

CompiledPlan RoutingResidencyModel::compile(const PolicyPlan& input) {
  if (input.plan_id.empty() ||
      input.execution_mode != ExecutionMode::kTraceCompiledNonAdaptive ||
      (input.prefetch_mode != PrefetchMode::kOff &&
       input.prefetch_mode != PrefetchMode::kHint) ||
      (input.eviction_policy != EvictionPolicy::kLru &&
       input.eviction_policy != EvictionPolicy::kFifo) ||
      input.capacity_bytes == 0 ||
      input.reserved_nonexpert_bytes > input.capacity_bytes ||
      input.authority.phase5_build_sha256 !=
          kPhase5BuildAuthoritySha256 ||
      input.authority.policy_contract_sha256 !=
          kPhase5PolicyContractSha256 ||
      input.authority.checkpoint_schema_sha256 !=
          kPhase5CheckpointSchemaSha256 ||
      input.catalog.empty() ||
      input.catalog.size() + input.demands.size() + input.hints.size() >
          kMaximumItems) {
    throw EngineError("invalid Phase 5 plan boundary");
  }
  const U128 expert_capacity =
      input.capacity_bytes - input.reserved_nonexpert_bytes;
  if (expert_capacity == 0) {
    throw EngineError("invalid Phase 5 expert capacity");
  }
  PolicyPlan plan = input;
  std::sort(
      plan.catalog.begin(), plan.catalog.end(),
      [](const auto& left, const auto& right) {
        return left.key < right.key;
      });
  std::sort(plan.base_resident.begin(), plan.base_resident.end());
  std::sort(
      plan.demands.begin(), plan.demands.end(),
      [](const auto& left, const auto& right) {
        return std::tuple{left.route_key, left.demand_id} <
               std::tuple{right.route_key, right.demand_id};
      });
  std::sort(
      plan.hints.begin(), plan.hints.end(),
      [](const auto& left, const auto& right) {
        return std::tuple{left.arrival_key, left.hint_id} <
               std::tuple{right.arrival_key, right.hint_id};
      });

  std::map<ExpertKey, U128> bytes;
  for (const auto& record : plan.catalog) {
    if (record.bytes == 0 || record.bytes > expert_capacity ||
        !bytes.emplace(record.key, record.bytes).second) {
      throw EngineError("invalid or duplicate Phase 5 expert catalog");
    }
  }
  std::set<ExpertKey> base_keys;
  U128 resident_bytes{0};
  for (const auto& key : plan.base_resident) {
    const auto found = bytes.find(key);
    if (found == bytes.end() || !base_keys.insert(key).second) {
      throw EngineError("unknown or duplicate Phase 5 base allocation");
    }
    resident_bytes =
        checked_add(resident_bytes, found->second, "base allocation");
    if (resident_bytes > expert_capacity) {
      throw EngineError("Phase 5 base allocation exceeds capacity");
    }
  }

  std::set<std::string> demand_ids;
  std::set<EventKey> input_keys;
  for (const auto& demand : plan.demands) {
    std::set<ExpertKey> selected;
    if (demand.demand_id.empty() ||
        !demand_ids.insert(demand.demand_id).second ||
        demand.top_k == 0 ||
        demand.top_k != demand.selected_experts.size() ||
        demand.routing_provenance.empty() ||
        demand.compute_work_per_expert == 0 ||
        demand.route_key.event_priority != 100 ||
        !demand.route_key.request_id.has_value() ||
        !demand.route_key.token_index.has_value() ||
        !demand.route_key.layer_index.has_value() ||
        demand.route_key.component_id.empty() ||
        demand.route_key.event_id.empty() ||
        !input_keys.insert(demand.route_key).second) {
      throw EngineError("invalid or duplicate Phase 5 token demand");
    }
    U128 required_bytes{0};
    for (const auto& key : demand.selected_experts) {
      const auto found = bytes.find(key);
      if (found == bytes.end() ||
          key.layer != *demand.route_key.layer_index ||
          !selected.insert(key).second) {
        throw EngineError("unknown or duplicate Phase 5 selected expert");
      }
      required_bytes =
          checked_add(required_bytes, found->second, "required bytes");
    }
    if (required_bytes > expert_capacity) {
      throw EngineError("Phase 5 required expert set exceeds capacity");
    }
  }
  std::set<std::string> hint_ids;
  for (const auto& hint : plan.hints) {
    if (hint.hint_id.empty() || hint.provenance.empty() ||
        !hint_ids.insert(hint.hint_id).second ||
        !bytes.contains(hint.expert) ||
        hint.arrival_key.event_priority != 100 ||
        !hint.arrival_key.request_id.has_value() ||
        !hint.arrival_key.token_index.has_value() ||
        !hint.arrival_key.layer_index.has_value() ||
        *hint.arrival_key.layer_index != hint.expert.layer ||
        hint.arrival_key.component_id.empty() ||
        hint.arrival_key.event_id.empty() ||
        !input_keys.insert(hint.arrival_key).second) {
      throw EngineError("invalid or duplicate Phase 5 prefetch hint");
    }
    const TokenDemand& target = demand_by_id(plan, hint.target_demand_id);
    if (!(hint.arrival_key < target.route_key) ||
        std::find(
            target.selected_experts.begin(), target.selected_experts.end(),
            hint.expert) == target.selected_experts.end()) {
      throw EngineError(
          "Phase 5 hint must precede and select a target expert");
    }
  }

  CompiledPlan output;
  output.plan_digest = sha256_bytes(canonical_plan(plan));
  std::map<ExpertKey, ResidentMeta> resident;
  std::uint64_t sequence = 0;
  for (const auto& key : plan.base_resident) {
    resident.emplace(key, ResidentMeta{sequence, sequence});
    ++sequence;
  }
  std::vector<std::string> previous_tails;

  auto add_action = [&](
                        ActionKind kind,
                        std::optional<ExpertKey> expert,
                        const std::string& source_id,
                        std::vector<std::string> dependencies,
                        phase4::ServiceClass service_class,
                        U128 work,
                        U128 release,
                        bool prefetch_load,
                        const EventKey& source_key) -> std::string {
    std::sort(dependencies.begin(), dependencies.end());
    const std::string id = action_id(
        output.plan_digest, output.actions.size(), kind, expert, source_id,
        dependencies, service_class, work, release, prefetch_load);
    EventKey key = source_key;
    key.time_fs = release;
    key.event_priority =
        service_class == phase4::ServiceClass::kH2D ||
                service_class == phase4::ServiceClass::kD2H
            ? 90U
            : 100U;
    key.component_id = "phase5-policy";
    key.event_id = id;
    output.actions.push_back(
        {id, kind, expert, source_id, dependencies, service_class, work,
         release, prefetch_load});
    output.operations.push_back(
        {key, std::move(dependencies), service_class, work});
    return id;
  };

  auto choose_victim = [&](const std::set<ExpertKey>& protected_keys) {
    std::optional<ExpertKey> victim;
    for (const auto& [key, meta] : resident) {
      if (protected_keys.contains(key)) continue;
      if (!victim.has_value()) {
        victim = key;
        continue;
      }
      const auto& current = resident.at(*victim);
      const auto candidate_rank =
          plan.eviction_policy == EvictionPolicy::kLru
          ? std::tuple{meta.last_use_sequence, meta.load_sequence, key}
          : std::tuple{meta.load_sequence, meta.last_use_sequence, key};
      const auto current_rank =
          plan.eviction_policy == EvictionPolicy::kLru
          ? std::tuple{
                current.last_use_sequence, current.load_sequence, *victim}
          : std::tuple{
                current.load_sequence, current.last_use_sequence, *victim};
      if (candidate_rank < current_rank) victim = key;
    }
    if (!victim.has_value()) {
      throw EngineError("Phase 5 capacity has no legal clean victim");
    }
    return *victim;
  };

  auto evict_until = [&](
                         U128 additional,
                         const std::set<ExpertKey>& protected_keys,
                         std::vector<std::string>& tails,
                         const std::string& source_id,
                         const EventKey& source_key) {
    while (checked_add(resident_bytes, additional, "planned capacity") >
           expert_capacity) {
      const ExpertKey victim = choose_victim(protected_keys);
      const std::string evict_id = add_action(
          ActionKind::kCleanEvict, victim, source_id, tails,
          phase4::ServiceClass::kMemory, U128{1}, source_key.time_fs, false,
          source_key);
      tails = {evict_id};
      resident_bytes =
          checked_sub(resident_bytes, bytes.at(victim), "planned resident");
      resident.erase(victim);
    }
  };

  struct Item {
    EventKey key;
    bool hint;
    std::size_t index;
  };
  std::vector<Item> items;
  items.reserve(plan.demands.size() + plan.hints.size());
  for (std::size_t index = 0; index < plan.hints.size(); ++index) {
    items.push_back({plan.hints[index].arrival_key, true, index});
  }
  for (std::size_t index = 0; index < plan.demands.size(); ++index) {
    items.push_back({plan.demands[index].route_key, false, index});
  }
  std::sort(
      items.begin(), items.end(), [&](const auto& left, const auto& right) {
        const auto left_id = left.hint ? plan.hints[left.index].hint_id
                                       : plan.demands[left.index].demand_id;
        const auto right_id = right.hint ? plan.hints[right.index].hint_id
                                         : plan.demands[right.index].demand_id;
        return std::tuple{left.key, left.hint, left_id} <
               std::tuple{right.key, right.hint, right_id};
      });

  for (const Item& item : items) {
    if (item.hint) {
      const PrefetchHint& hint = plan.hints[item.index];
      std::vector<std::string> tails{
          add_action(
              ActionKind::kHintBarrier, std::nullopt, hint.hint_id,
              previous_tails, phase4::ServiceClass::kMemory, U128{1},
              hint.arrival_key.time_fs, false, hint.arrival_key)};
      if (plan.prefetch_mode == PrefetchMode::kHint &&
          !resident.contains(hint.expert)) {
        evict_until(
            bytes.at(hint.expert), {hint.expert}, tails, hint.hint_id,
            hint.arrival_key);
        const std::string load_id = add_action(
            ActionKind::kH2DLoad, hint.expert, hint.hint_id, tails,
            phase4::ServiceClass::kH2D, bytes.at(hint.expert),
            hint.arrival_key.time_fs, true, hint.arrival_key);
        tails = {load_id};
        resident_bytes =
            checked_add(resident_bytes, bytes.at(hint.expert), "hint load");
        resident.emplace(
            hint.expert, ResidentMeta{sequence, sequence});
        ++sequence;
      }
      previous_tails = std::move(tails);
      continue;
    }

    const TokenDemand& demand = plan.demands[item.index];
    std::vector<std::string> tails = previous_tails;
    const std::set<ExpertKey> required(
        demand.selected_experts.begin(), demand.selected_experts.end());
    U128 additional{0};
    for (const auto& key : demand.selected_experts) {
      if (!resident.contains(key)) {
        additional =
            checked_add(additional, bytes.at(key), "demand loads");
      }
    }
    evict_until(
        additional, required, tails, demand.demand_id, demand.route_key);
    for (const auto& key : demand.selected_experts) {
      if (!resident.contains(key)) {
        const std::string load_id = add_action(
            ActionKind::kH2DLoad, key, demand.demand_id, tails,
            phase4::ServiceClass::kH2D, bytes.at(key),
            demand.route_key.time_fs, false, demand.route_key);
        tails = {load_id};
        resident_bytes =
            checked_add(resident_bytes, bytes.at(key), "demand load");
        resident.emplace(key, ResidentMeta{sequence, sequence});
        ++sequence;
      }
    }
    const std::string route_id = add_action(
        ActionKind::kRouteBarrier, std::nullopt, demand.demand_id, tails,
        phase4::ServiceClass::kMemory, U128{1}, demand.route_key.time_fs,
        false, demand.route_key);
    tails = {route_id};
    std::vector<std::string> compute_tails;
    for (const auto& key : demand.selected_experts) {
      compute_tails.push_back(add_action(
          ActionKind::kComputeAssignment, key, demand.demand_id, tails,
          phase4::ServiceClass::kCompute, demand.compute_work_per_expert,
          demand.route_key.time_fs, false, demand.route_key));
      resident.at(key).last_use_sequence = sequence;
      ++sequence;
      ++output.expected_assignments;
    }
    previous_tails = std::move(compute_tails);
  }
  for (const auto& [key, metadata] : resident) {
    static_cast<void>(metadata);
    output.expected_terminal_resident.insert(key);
  }
  output.expected_terminal_resident_bytes = resident_bytes;
  return output;
}

RoutingResidencyModel::RoutingResidencyModel(
    phase4::SingleGpuPlatform platform, PolicyPlan plan)
    : platform_(std::move(platform)),
      plan_(std::move(plan)),
      compiled_(compile(plan_)),
      core_(std::in_place, platform_, compiled_.operations) {
  build_replay_indexes();
  replay_ = initial_replay();
}

void RoutingResidencyModel::build_replay_indexes() {
  action_index_.clear();
  prefetch_sources_.clear();
  catalog_bytes_ = catalog_map(plan_);
  for (std::size_t index = 0; index < compiled_.actions.size(); ++index) {
    const auto& action = compiled_.actions[index];
    if (!action_index_.emplace(action.action_id, index).second) {
      throw EngineError("duplicate Phase 5 compiled action identifier");
    }
    if (action.kind == ActionKind::kH2DLoad && action.prefetch_load) {
      prefetch_sources_.insert(action.source_id);
    }
  }
}

RoutingResidencyModel::ReplayState RoutingResidencyModel::initial_replay()
    const {
  ReplayState state;
  for (const auto& key : plan_.base_resident) {
    if (!state.resident.insert(key).second) {
      throw EngineError("duplicate Phase 5 replay base resident");
    }
    state.resident_bytes =
        checked_add(
            state.resident_bytes, catalog_bytes_.at(key), "replay base");
  }
  state.metrics.peak_resident_bytes = state.resident_bytes;
  return state;
}

void RoutingResidencyModel::replay_one(const phase4::TraceEntry& trace) {
  const auto found_action = action_index_.find(trace.operation_id);
  ++replay_.metrics.replay_action_lookups;
  if (found_action == action_index_.end() ||
      !trace.generated_key.has_value() ||
      trace.generated_key->event_id != trace.generated_event_id) {
    throw EngineError("Phase 5 trace references unknown action");
  }
  const auto& action = compiled_.actions.at(found_action->second);
  const bool start = trace.kind == phase4::TraceKind::kStart;
  if (start) {
    if (!replay_.started.insert(action.action_id).second ||
        replay_.completed.contains(action.action_id)) {
      throw EngineError("Phase 5 duplicate or reordered action start");
    }
    if (action.kind == ActionKind::kCleanEvict) {
      if (!action.expert.has_value() ||
          !replay_.resident.contains(*action.expert) ||
          replay_.pins[*action.expert] != 0) {
        throw EngineError("Phase 5 illegal pinned clean eviction");
      }
    } else if (action.kind == ActionKind::kH2DLoad) {
      if (!action.expert.has_value() ||
          replay_.resident.contains(*action.expert)) {
        throw EngineError("Phase 5 duplicate resident load");
      }
    } else if (action.kind == ActionKind::kComputeAssignment) {
      if (!action.expert.has_value() ||
          !replay_.resident.contains(*action.expert) ||
          replay_.pins[*action.expert] == 0) {
        throw EngineError("Phase 5 compute lacks resident pinned expert");
      }
    }
    ++processed_trace_;
    return;
  }
  if (trace.kind != phase4::TraceKind::kComplete ||
      !replay_.started.contains(action.action_id) ||
      !replay_.completed.insert(action.action_id).second) {
    throw EngineError("Phase 5 completion lacks unique start");
  }
  switch (action.kind) {
    case ActionKind::kHintBarrier: {
      const bool has_load = prefetch_sources_.contains(action.source_id);
      if (!has_load) ++replay_.metrics.ignored_hints;
      break;
    }
    case ActionKind::kRouteBarrier: {
      const TokenDemand& demand = demand_by_id(plan_, action.source_id);
      for (const auto& key : demand.selected_experts) {
        if (!replay_.resident.contains(key) || replay_.pins[key] != 0) {
          throw EngineError(
              "Phase 5 route barrier lacks all unpinned residents");
        }
      }
      for (const auto& key : demand.selected_experts) ++replay_.pins[key];
      ++replay_.metrics.routing_demands;
      break;
    }
    case ActionKind::kCleanEvict: {
      const ExpertKey key = *action.expert;
      if (!replay_.resident.erase(key)) {
        throw EngineError("Phase 5 clean eviction lacks resident expert");
      }
      replay_.resident_bytes =
          checked_sub(
              replay_.resident_bytes, catalog_bytes_.at(key),
              "replay evict");
      const auto prefetched = replay_.pending_prefetches.find(key);
      if (prefetched != replay_.pending_prefetches.end()) {
        ++replay_.metrics.wasted_prefetches;
        replay_.pending_prefetches.erase(prefetched);
      }
      ++replay_.metrics.clean_evictions;
      break;
    }
    case ActionKind::kH2DLoad: {
      const ExpertKey key = *action.expert;
      if (!replay_.resident.insert(key).second) {
        throw EngineError("Phase 5 load completion duplicates resident");
      }
      replay_.resident_bytes =
          checked_add(
              replay_.resident_bytes, catalog_bytes_.at(key),
              "replay load");
      if (checked_add(
              replay_.resident_bytes, plan_.reserved_nonexpert_bytes,
              "total replay capacity") > plan_.capacity_bytes) {
        throw EngineError("Phase 5 replay exceeds capacity");
      }
      replay_.metrics.peak_resident_bytes =
          std::max(
              replay_.metrics.peak_resident_bytes, replay_.resident_bytes);
      ++replay_.metrics.loads;
      if (action.prefetch_load) {
        if (!replay_.pending_prefetches
                 .emplace(key, action.source_id)
                 .second) {
          throw EngineError("Phase 5 duplicate pending prefetch");
        }
        ++replay_.metrics.prefetch_loads;
      }
      break;
    }
    case ActionKind::kComputeAssignment: {
      const ExpertKey key = *action.expert;
      auto& pin = replay_.pins[key];
      if (pin == 0) {
        throw EngineError("Phase 5 assignment pin underflow");
      }
      --pin;
      ++replay_.metrics.assignments;
      const auto prefetched = replay_.pending_prefetches.find(key);
      if (prefetched != replay_.pending_prefetches.end()) {
        ++replay_.metrics.useful_prefetches;
        replay_.pending_prefetches.erase(prefetched);
      }
      break;
    }
  }
  ++processed_trace_;
}

void RoutingResidencyModel::replay_existing_prefix() {
  if (!core_.has_value()) {
    throw EngineError("Phase 5 core is unavailable");
  }
  const auto& trace = core_->result().trace;
  if (processed_trace_ > trace.size()) {
    throw EngineError("Phase 5 replay cursor exceeds trace");
  }
  while (processed_trace_ < trace.size()) {
    replay_one(trace[processed_trace_]);
  }
}

phase4::TraceEntry RoutingResidencyModel::step() {
  if (!core_.has_value() || finalized_) {
    throw EngineError("cannot step terminal Phase 5 model");
  }
  const phase4::TraceEntry trace = core_->step();
  if (!trace.operation_id.empty()) {
    replay_one(trace);
  } else {
    replay_existing_prefix();
  }
  return trace;
}

PolicyResult RoutingResidencyModel::run_until_quiescent() {
  if (finalized_) {
    return finish_result();
  }
  while (core_->result().terminal_status == TerminalStatus::kRunning) {
    static_cast<void>(step());
  }
  replay_existing_prefix();
  return finish_result();
}

std::string RoutingResidencyModel::replay_digest(
    const ReplayState& state) const {
  std::ostringstream stream;
  put(stream, "moe-phase5-residency-state-v1");
  put(stream, compiled_.plan_digest);
  put(stream, std::to_string(processed_trace_));
  put(stream, to_decimal(state.resident_bytes));
  put(stream, std::to_string(state.resident.size()));
  for (const auto& key : state.resident) put_expert(stream, key);
  put(stream, std::to_string(state.pins.size()));
  for (const auto& [key, count] : state.pins) {
    put_expert(stream, key);
    put(stream, std::to_string(count));
  }
  put(stream, std::to_string(state.pending_prefetches.size()));
  for (const auto& [key, hint] : state.pending_prefetches) {
    put_expert(stream, key);
    put(stream, hint);
  }
  put(stream, std::to_string(state.started.size()));
  for (const auto& id : state.started) put(stream, id);
  put(stream, std::to_string(state.completed.size()));
  for (const auto& id : state.completed) put(stream, id);
  const auto& metrics = state.metrics;
  put(stream, std::to_string(metrics.routing_demands));
  put(stream, std::to_string(metrics.assignments));
  put(stream, std::to_string(metrics.loads));
  put(stream, std::to_string(metrics.clean_evictions));
  put(stream, std::to_string(metrics.prefetch_loads));
  put(stream, std::to_string(metrics.useful_prefetches));
  put(stream, std::to_string(metrics.wasted_prefetches));
  put(stream, std::to_string(metrics.ignored_hints));
  put(stream, std::to_string(metrics.replay_action_lookups));
  put(stream, to_decimal(metrics.peak_resident_bytes));
  return sha256_bytes(stream.str());
}

PolicyResult RoutingResidencyModel::finish_result() {
  if (!core_.has_value() ||
      core_->result().terminal_status != TerminalStatus::kQuiescent ||
      processed_trace_ != core_->result().trace.size()) {
    throw EngineError("Phase 5 cannot finalize incomplete execution");
  }
  if (!finalized_) {
    replay_.metrics.wasted_prefetches +=
        replay_.pending_prefetches.size();
    replay_.pending_prefetches.clear();
    finalized_ = true;
  }
  if (replay_.started.size() != compiled_.actions.size() ||
      replay_.completed.size() != compiled_.actions.size()) {
    throw EngineError("Phase 5 terminal action conservation failure");
  }
  if (replay_.metrics.assignments != compiled_.expected_assignments) {
    throw EngineError(
        "Phase 5 terminal assignment conservation failure: actual=" +
        std::to_string(replay_.metrics.assignments) +
        " expected=" + std::to_string(compiled_.expected_assignments));
  }
  if (replay_.resident != compiled_.expected_terminal_resident ||
      replay_.resident_bytes != compiled_.expected_terminal_resident_bytes) {
    throw EngineError("Phase 5 terminal residency conservation failure");
  }
  if (replay_.metrics.useful_prefetches +
          replay_.metrics.wasted_prefetches !=
      replay_.metrics.prefetch_loads) {
    throw EngineError("Phase 5 terminal prefetch conservation failure");
  }
  if (std::any_of(
          replay_.pins.begin(), replay_.pins.end(),
          [](const auto& item) { return item.second != 0; })) {
    throw EngineError("Phase 5 terminal pin conservation failure");
  }
  const auto& core_result = core_->result();
  if (core_result.entries.size() != compiled_.actions.size() ||
      core_result.trace.size() != compiled_.actions.size() * 2 ||
      core_result.semantic_digest.empty()) {
    throw EngineError("Phase 5 incomplete Phase 4 evidence");
  }
  std::map<std::string, const phase4::ScheduleEntry*> entries;
  for (const auto& entry : core_result.entries) {
    if (!entries.emplace(entry.operation_id, &entry).second) {
      throw EngineError("Phase 5 duplicate schedule entry");
    }
  }
  for (const auto& action : compiled_.actions) {
    const auto found = entries.find(action.action_id);
    if (found == entries.end() ||
        found->second->service_class != action.service_class) {
      throw EngineError("Phase 5 action/schedule mismatch");
    }
    for (const auto& dependency : action.dependencies) {
      const auto dependency_entry = entries.find(dependency);
      if (dependency_entry == entries.end() ||
          dependency_entry->second->end_fs > found->second->start_fs) {
        throw EngineError("Phase 5 action causality violation");
      }
    }
  }
  const std::string residency = replay_digest(replay_);
  std::ostringstream canonical;
  put(canonical, "moe-phase5-result-v1");
  put(canonical, compiled_.plan_digest);
  put(canonical, core_result.semantic_digest);
  put(canonical, residency);
  put(canonical, to_decimal(replay_.resident_bytes));
  put(canonical, std::to_string(replay_.metrics.routing_demands));
  put(canonical, std::to_string(replay_.metrics.assignments));
  put(canonical, std::to_string(replay_.metrics.loads));
  put(canonical, std::to_string(replay_.metrics.clean_evictions));
  put(canonical, std::to_string(replay_.metrics.prefetch_loads));
  put(canonical, std::to_string(replay_.metrics.useful_prefetches));
  put(canonical, std::to_string(replay_.metrics.wasted_prefetches));
  put(canonical, std::to_string(replay_.metrics.ignored_hints));
  put(canonical, std::to_string(replay_.metrics.replay_action_lookups));
  put(canonical, to_decimal(replay_.metrics.peak_resident_bytes));
  for (const auto& key : replay_.resident) put_expert(canonical, key);
  return {
      compiled_.plan_digest,
      core_result.semantic_digest,
      residency,
      sha256_bytes(canonical.str()),
      replay_.resident,
      replay_.resident_bytes,
      replay_.metrics,
      core_result};
}

const phase4::ScheduleResult& RoutingResidencyModel::phase4_result() const {
  if (!core_.has_value()) {
    throw EngineError("Phase 5 core is unavailable");
  }
  return core_->result();
}

std::string RoutingResidencyModel::checkpoint_state_digest(
    std::size_t prefix,
    const std::string& residency_digest,
    const std::string& phase4_state_digest) const {
  std::ostringstream stream;
  put(stream, "moe-phase5-checkpoint-state-v1");
  put(stream, compiled_.plan_digest);
  put(stream, std::to_string(prefix));
  put(stream, residency_digest);
  put(stream, phase4_state_digest);
  return sha256_bytes(stream.str());
}

PolicyCheckpoint RoutingResidencyModel::checkpoint() const {
  if (!core_.has_value() || finalized_ ||
      processed_trace_ != core_->result().trace.size()) {
    throw EngineError("Phase 5 checkpoint state is inconsistent");
  }
  const phase4::Checkpoint core_checkpoint = core_->checkpoint();
  const std::string residency = replay_digest(replay_);
  const std::string digest = checkpoint_state_digest(
      processed_trace_, residency, core_checkpoint.state_digest);
  return {
      "phase5-checkpoint-v1", compiled_.plan_digest, processed_trace_,
      residency, core_checkpoint, digest};
}

std::string RoutingResidencyModel::serialize_checkpoint() const {
  const PolicyCheckpoint value = checkpoint();
  const std::string core_wire = core_->serialize_checkpoint();
  std::ostringstream body;
  put(body, value.plan_digest);
  put(body, std::to_string(value.trace_prefix_size));
  put(body, value.residency_digest);
  put(body, value.phase4_checkpoint.state_digest);
  put(body, value.state_digest);
  put(body, core_wire);
  const std::string body_bytes = body.str();
  std::ostringstream wire;
  wire << "moe-phase5-checkpoint-v1\n"
       << body_bytes.size() << '\n'
       << body_bytes
       << sha256_bytes(body_bytes) << '\n';
  return wire.str();
}

RoutingResidencyModel RoutingResidencyModel::restore(
    phase4::SingleGpuPlatform platform,
    PolicyPlan plan,
    const PolicyCheckpoint& checkpoint) {
  RoutingResidencyModel restored(std::move(platform), std::move(plan));
  if (checkpoint.schema_version != "phase5-checkpoint-v1" ||
      checkpoint.plan_digest != restored.compiled_.plan_digest ||
      checkpoint.trace_prefix_size !=
          checkpoint.phase4_checkpoint.result.trace.size()) {
    throw EngineError("Phase 5 checkpoint plan/prefix mismatch");
  }
  restored.core_ = phase4::SingleGpuModel::restore(
      restored.platform_, restored.compiled_.operations,
      checkpoint.phase4_checkpoint);
  restored.replay_ = restored.initial_replay();
  restored.processed_trace_ = 0;
  restored.replay_existing_prefix();
  const std::string residency = restored.replay_digest(restored.replay_);
  const std::string digest = restored.checkpoint_state_digest(
      restored.processed_trace_, residency,
      checkpoint.phase4_checkpoint.state_digest);
  if (residency != checkpoint.residency_digest ||
      digest != checkpoint.state_digest) {
    throw EngineError("Phase 5 checkpoint residency digest mismatch");
  }
  return restored;
}

RoutingResidencyModel RoutingResidencyModel::restore_serialized(
    phase4::SingleGpuPlatform platform,
    PolicyPlan plan,
    const std::string& bytes) {
  constexpr std::string_view magic = "moe-phase5-checkpoint-v1\n";
  if (!bytes.starts_with(magic)) {
    throw EngineError("invalid Phase 5 checkpoint wire header");
  }
  const std::size_t size_end = bytes.find('\n', magic.size());
  if (size_end == std::string::npos) {
    throw EngineError("invalid Phase 5 checkpoint wire size");
  }
  const std::size_t body_size = parse_size(
      bytes.substr(magic.size(), size_end - magic.size()), "wire body size");
  const std::size_t body_begin = size_end + 1;
  if (body_size > bytes.size() - body_begin ||
      bytes.size() - body_begin - body_size != 65 ||
      bytes.back() != '\n') {
    throw EngineError("invalid Phase 5 checkpoint wire boundary");
  }
  const std::string body = bytes.substr(body_begin, body_size);
  const std::string digest = bytes.substr(body_begin + body_size, 64);
  if (digest != sha256_bytes(body)) {
    throw EngineError("Phase 5 checkpoint wire digest mismatch");
  }
  std::size_t cursor = 0;
  const std::string plan_digest = take(body, cursor);
  const std::size_t prefix = parse_size(take(body, cursor), "trace prefix");
  const std::string residency_digest = take(body, cursor);
  const std::string phase4_state_digest = take(body, cursor);
  const std::string state_digest = take(body, cursor);
  const std::string core_wire = take(body, cursor);
  if (cursor != body.size()) {
    throw EngineError("invalid Phase 5 checkpoint trailing body");
  }

  RoutingResidencyModel restored(std::move(platform), std::move(plan));
  if (plan_digest != restored.compiled_.plan_digest) {
    throw EngineError("Phase 5 checkpoint plan digest mismatch");
  }
  restored.core_ = phase4::SingleGpuModel::restore_serialized(
      restored.platform_, restored.compiled_.operations, core_wire);
  if (restored.core_->state_digest() != phase4_state_digest ||
      restored.core_->result().trace.size() != prefix) {
    throw EngineError("Phase 5 checkpoint Phase 4 binding mismatch");
  }
  restored.replay_ = restored.initial_replay();
  restored.processed_trace_ = 0;
  restored.replay_existing_prefix();
  const std::string actual_residency =
      restored.replay_digest(restored.replay_);
  const std::string actual_state = restored.checkpoint_state_digest(
      prefix, actual_residency, phase4_state_digest);
  if (actual_residency != residency_digest ||
      actual_state != state_digest) {
    throw EngineError("Phase 5 checkpoint replay mismatch");
  }
  return restored;
}

}  // namespace moe_sim::phase5

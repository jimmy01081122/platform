#include "moe_sim/single_gpu_model.hpp"

#include <algorithm>
#include <charconv>
#include <set>
#include <sstream>
#include <tuple>

namespace moe_sim::phase4 {
namespace {

using boost::multiprecision::cpp_int;
constexpr std::uint64_t kFsPerSecond = 1'000'000'000'000'000ULL;
constexpr std::uint64_t kMaxOperations = 100'000;
constexpr std::uint64_t kMaxLanes = 65'536;
constexpr char kPhase3Ledger[] =
    "c4b9209d95bbf91c607d65a70062e3bbb03a5892807ce08d8a4a370000535e42";

U128 checked_u128(const cpp_int& value, const std::string& name) {
  const cpp_int maximum = (cpp_int{1} << 128) - 1;
  if (value < 0 || value > maximum) {
    throw EngineError(name + " exceeds unsigned 128-bit range");
  }
  return static_cast<U128>(value);
}

U128 checked_add(const U128& a, const U128& b, const std::string& name) {
  return checked_u128(cpp_int{a} + cpp_int{b}, name);
}

std::string class_name(ServiceClass value) {
  switch (value) {
    case ServiceClass::kCompute: return "COMPUTE";
    case ServiceClass::kMemory: return "MEMORY";
    case ServiceClass::kH2D: return "H2D";
    case ServiceClass::kD2H: return "D2H";
  }
  throw EngineError("invalid service class");
}

std::string fidelity_name(Fidelity value) {
  switch (value) {
    case Fidelity::kAnalyticFirstOrder: return "ANALYTIC_FIRST_ORDER";
    case Fidelity::kFunctionalOnly: return "FUNCTIONAL_ONLY";
  }
  throw EngineError("invalid fidelity");
}

std::string state_name(OperationState value) {
  switch (value) {
    case OperationState::kPending: return "PENDING";
    case OperationState::kInFlight: return "IN_FLIGHT";
    case OperationState::kComplete: return "COMPLETE";
  }
  throw EngineError("invalid operation state");
}

std::string trace_name(TraceKind value) {
  switch (value) {
    case TraceKind::kStart: return "START";
    case TraceKind::kComplete: return "COMPLETE";
  }
  throw EngineError("invalid trace kind");
}

void put(std::ostringstream& stream, const std::string& value) {
  stream << value.size() << ':' << value;
}

void put_key(std::ostringstream& stream, const EventKey& key) {
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

}  // namespace

SingleGpuModel::SingleGpuModel(
    SingleGpuPlatform platform, std::vector<Operation> operations)
    : platform_(std::move(platform)), operations_(std::move(operations)) {
  std::sort(
      platform_.profiles.begin(), platform_.profiles.end(),
      [](const auto& a, const auto& b) {
        return std::tuple{a.service_class, a.profile_id} <
               std::tuple{b.service_class, b.profile_id};
      });
  for (auto& operation : operations_) {
    std::sort(operation.dependencies.begin(), operation.dependencies.end());
  }
  std::sort(
      operations_.begin(), operations_.end(),
      [](const auto& a, const auto& b) { return a.key < b.key; });
  validate();
  for (std::size_t index = 0; index < operations_.size(); ++index) {
    const auto& operation = operations_[index];
    states_.emplace(operation.key.event_id, OperationState::kPending);
    operation_index_.emplace(operation.key.event_id, index);
    remaining_dependencies_.emplace(
        operation.key.event_id, operation.dependencies.size());
  }
  for (std::size_t index = 0; index < operations_.size(); ++index) {
    const auto& operation = operations_[index];
    future_arrivals_.emplace(operation.key, index);
    for (const auto& dependency : operation.dependencies) {
      dependents_[dependency].push_back(index);
    }
  }
  for (const auto& item : platform_.profiles) {
    primary_busy_[item.service_class] =
        std::vector<bool>(item.lanes, false);
  }
  shared_busy_.assign(platform_.shared_fabric_lanes, false);
  update_clock_cursor();
  activate_arrivals();
}

void SingleGpuModel::validate() const {
  platform_.completion_clock.validate();
  if (platform_.platform_id.empty() ||
      platform_.phase3_ledger_sha256 != kPhase3Ledger ||
      platform_.phase4_build_sha256 != kPhase4BuildAuthoritySha256 ||
      platform_.model_contract_sha256 != kPhase4ModelContractSha256 ||
      platform_.checkpoint_schema_sha256 != kPhase4CheckpointSchemaSha256 ||
      platform_.shared_fabric_lanes == 0 ||
      platform_.shared_fabric_lanes > kMaxLanes ||
      operations_.size() > kMaxOperations) {
    throw EngineError("invalid Phase 4 platform boundary");
  }
  std::set<ServiceClass> classes;
  for (const auto& item : platform_.profiles) {
    const bool valid_fidelity =
        item.fidelity == Fidelity::kAnalyticFirstOrder ||
        item.fidelity == Fidelity::kFunctionalOnly;
    if (item.profile_id.empty() || item.lanes == 0 ||
        item.lanes > kMaxLanes ||
        item.throughput_numerator_per_second == 0 ||
        item.throughput_denominator == 0 ||
        !valid_fidelity ||
        item.range_status != RangeStatus::kRangeUnknown ||
        !classes.insert(item.service_class).second) {
      throw EngineError("invalid or duplicate service profile");
    }
    if ((item.service_class == ServiceClass::kMemory ||
         item.service_class == ServiceClass::kH2D ||
         item.service_class == ServiceClass::kD2H) &&
        !item.uses_shared_fabric) {
      throw EngineError("memory and copy services require shared fabric");
    }
  }
  const std::set<ServiceClass> required{
      ServiceClass::kCompute, ServiceClass::kMemory,
      ServiceClass::kH2D, ServiceClass::kD2H};
  if (classes != required) {
    throw EngineError("incomplete service profile set");
  }
  std::map<std::string, const Operation*> by_id;
  std::set<EventKey> keys;
  for (const auto& operation : operations_) {
    const bool valid_class =
        operation.service_class == ServiceClass::kCompute ||
        operation.service_class == ServiceClass::kMemory ||
        operation.service_class == ServiceClass::kH2D ||
        operation.service_class == ServiceClass::kD2H;
    const std::uint32_t required_priority =
        operation.service_class == ServiceClass::kH2D ||
                operation.service_class == ServiceClass::kD2H
            ? 90
            : 100;
    if (operation.key.event_id.empty() ||
        operation.key.component_id.empty() || operation.work == 0 ||
        !valid_class || operation.key.event_priority != required_priority ||
        !keys.insert(operation.key).second ||
        !by_id.emplace(operation.key.event_id, &operation).second) {
      throw EngineError("invalid or duplicate Phase 4 operation");
    }
  }
  for (const auto& operation : operations_) {
    std::set<std::string> dependencies;
    for (const auto& dependency : operation.dependencies) {
      const auto found = by_id.find(dependency);
      if (!dependencies.insert(dependency).second ||
          found == by_id.end() || dependency == operation.key.event_id) {
        throw EngineError("invalid Phase 4 dependency");
      }
    }
  }
  std::map<std::string, std::uint64_t> indegree;
  std::map<std::string, std::vector<std::string>> outgoing;
  for (const auto& operation : operations_) {
    indegree[operation.key.event_id] = operation.dependencies.size();
    for (const auto& dependency : operation.dependencies) {
      outgoing[dependency].push_back(operation.key.event_id);
    }
  }
  std::set<std::string> ready_ids;
  for (const auto& [id, degree] : indegree) {
    if (degree == 0) {
      ready_ids.insert(id);
    }
  }
  std::size_t visited = 0;
  while (!ready_ids.empty()) {
    const std::string id = *ready_ids.begin();
    ready_ids.erase(ready_ids.begin());
    ++visited;
    for (const auto& dependent : outgoing[id]) {
      auto& degree = indegree.at(dependent);
      --degree;
      if (degree == 0) {
        ready_ids.insert(dependent);
      }
    }
  }
  if (visited != operations_.size()) {
    throw EngineError("Phase 4 dependency cycle");
  }
  if (profile(ServiceClass::kCompute).uses_shared_fabric) {
    throw EngineError("compute profile cannot claim shared fabric in Phase 4");
  }
}

const ServiceProfile& SingleGpuModel::profile(ServiceClass value) const {
  return *std::find_if(
      platform_.profiles.begin(), platform_.profiles.end(),
      [&](const auto& item) { return item.service_class == value; });
}

U128 SingleGpuModel::service_duration(
    const ServiceProfile& item, const U128& work) const {
  const cpp_int numerator =
      cpp_int{work} * cpp_int{kFsPerSecond} *
      cpp_int{item.throughput_denominator};
  const cpp_int denominator{item.throughput_numerator_per_second};
  const cpp_int transfer = (numerator + denominator - 1) / denominator;
  const U128 duration = checked_u128(
      cpp_int{item.setup_latency_fs} + transfer,
      "Phase 4 service duration");
  if (duration == 0) {
    throw EngineError("Phase 4 service lacks strict progress");
  }
  return duration;
}

U128 SingleGpuModel::dependency_ready(const Operation& operation) const {
  U128 ready = operation.key.time_fs;
  for (const auto& dependency : operation.dependencies) {
    const auto found = completion_times_.find(dependency);
    if (found == completion_times_.end()) {
      throw EngineError("dependency is not complete");
    }
    ready = std::max(ready, found->second);
  }
  return ready;
}

EventKey SingleGpuModel::generated_key(
    const Operation& operation, TraceKind kind, const U128& time) const {
  EventKey key = operation.key;
  key.time_fs = time;
  if (kind == TraceKind::kComplete) {
    key.event_priority =
        operation.service_class == ServiceClass::kH2D ||
                operation.service_class == ServiceClass::kD2H
            ? 20
            : 30;
  } else {
    key.event_priority =
        operation.service_class == ServiceClass::kH2D ||
                operation.service_class == ServiceClass::kD2H
            ? 90
            : 100;
  }
  key.event_id = generated_id(operation, kind);
  return key;
}

std::string SingleGpuModel::generated_id(
    const Operation& operation, TraceKind kind) const {
  std::ostringstream stream;
  put(stream, "phase4-generated-event-v1");
  put(stream, platform_.platform_id);
  put(stream, platform_.phase3_ledger_sha256);
  put(stream, platform_.phase4_build_sha256);
  put(stream, platform_.model_contract_sha256);
  put(stream, platform_.checkpoint_schema_sha256);
  put(stream, profile(operation.service_class).profile_id);
  const auto& service_profile = profile(operation.service_class);
  put(stream, std::to_string(service_profile.lanes));
  put(stream, to_decimal(service_profile.throughput_numerator_per_second));
  put(stream, to_decimal(service_profile.throughput_denominator));
  put(stream, to_decimal(service_profile.setup_latency_fs));
  put(stream, service_profile.uses_shared_fabric ? "1" : "0");
  put(stream, fidelity_name(service_profile.fidelity));
  put(stream, operation.key.event_id);
  put(stream, to_decimal(operation.key.time_fs));
  put(stream, std::to_string(operation.key.event_priority));
  put(stream, operation.key.request_id.value_or(""));
  put(stream, operation.key.token_index.has_value()
                  ? std::to_string(*operation.key.token_index) : "-");
  put(stream, operation.key.layer_index.has_value()
                  ? std::to_string(*operation.key.layer_index) : "-");
  put(stream, operation.key.component_id);
  put(stream, class_name(operation.service_class));
  put(stream, to_decimal(operation.work));
  for (const auto& dependency : operation.dependencies) {
    put(stream, dependency);
  }
  put(stream, trace_name(kind));
  return sha256_bytes(stream.str());
}

void SingleGpuModel::activate_arrivals() {
  while (!future_arrivals_.empty() &&
         future_arrivals_.begin()->first.time_fs <= global_time_fs_) {
    const std::size_t index = future_arrivals_.begin()->second;
    future_arrivals_.erase(future_arrivals_.begin());
    const Operation& operation = operations_[index];
    if (remaining_dependencies_.at(operation.key.event_id) == 0) {
      ready_[operation.service_class].emplace(
          generated_key(operation, TraceKind::kStart, U128{0}), index);
      ++result_.scheduler_metrics.ready_queue_pushes;
    }
  }
}

void SingleGpuModel::update_clock_cursor() {
  clock_cursor_cycle_ = platform_.completion_clock.ceil_edge(global_time_fs_);
  clock_cursor_remainder_ =
      platform_.completion_clock.remainder(clock_cursor_cycle_);
}

std::optional<std::size_t> SingleGpuModel::feasible_start() {
  std::optional<std::size_t> selected;
  for (const ServiceClass service_class :
       {ServiceClass::kCompute, ServiceClass::kMemory,
        ServiceClass::kH2D, ServiceClass::kD2H}) {
    const auto queue = ready_.find(service_class);
    if (queue == ready_.end() || queue->second.empty()) {
      continue;
    }
    const std::size_t index = queue->second.begin()->second;
    const Operation& operation = operations_[index];
    const ServiceProfile& item = profile(operation.service_class);
    const auto& primary = primary_busy_.at(operation.service_class);
    const bool primary_available =
        std::find(primary.begin(), primary.end(), false) != primary.end();
    const bool shared_available =
        !item.uses_shared_fabric ||
        std::find(shared_busy_.begin(), shared_busy_.end(), false) !=
            shared_busy_.end();
    if (!primary_available || !shared_available) {
      continue;
    }
    ++result_.scheduler_metrics.scheduler_key_comparisons;
    if (!selected.has_value() ||
        generated_key(operation, TraceKind::kStart, global_time_fs_) <
            generated_key(
                operations_[*selected], TraceKind::kStart, global_time_fs_)) {
      selected = index;
    }
  }
  return selected;
}

std::optional<std::string> SingleGpuModel::next_completion() const {
  if (completion_queue_.empty()) {
    return std::nullopt;
  }
  return completion_queue_.begin()->second;
}

TraceEntry SingleGpuModel::step() {
  if (terminal_status_ != TerminalStatus::kRunning) {
    throw EngineError("cannot step terminal Phase 4 engine");
  }
  while (true) {
    const auto completion = next_completion();
    const auto start = feasible_start();
    bool take_completion = false;
    if (completion.has_value() &&
        active_.at(*completion).completion_fs <= global_time_fs_) {
      take_completion = true;
    } else if (start.has_value()) {
      take_completion = false;
    } else {
      std::optional<U128> next_time;
      if (completion.has_value()) {
        next_time = active_.at(*completion).completion_fs;
      }
      if (!future_arrivals_.empty() &&
          (!next_time.has_value() ||
           future_arrivals_.begin()->first.time_fs < *next_time)) {
        next_time = future_arrivals_.begin()->first.time_fs;
      }
      if (next_time.has_value()) {
        global_time_fs_ = *next_time;
        update_clock_cursor();
        activate_arrivals();
        continue;
      }
      const bool all_complete = std::all_of(
          states_.begin(), states_.end(),
          [](const auto& item) {
            return item.second == OperationState::kComplete;
          });
      terminal_status_ = all_complete
          ? TerminalStatus::kQuiescent
          : TerminalStatus::kDeadlock;
      result_.terminal_status = terminal_status_;
      result_.makespan_fs = global_time_fs_;
      result_.semantic_digest = state_digest();
      return {
          "", "", TraceKind::kComplete, global_time_fs_, 0, std::nullopt};
    }

    if (take_completion) {
      const ActiveReservation reservation = active_.at(*completion);
      const Operation& operation =
          operations_.at(operation_index_.at(reservation.operation_id));
      global_time_fs_ = reservation.completion_fs;
      update_clock_cursor();
      primary_busy_.at(reservation.service_class)
          .at(reservation.primary_lane) = false;
      if (reservation.shared_fabric_lane.has_value()) {
        shared_busy_.at(*reservation.shared_fabric_lane) = false;
      }
      states_.at(reservation.operation_id) = OperationState::kComplete;
      completion_times_[reservation.operation_id] = global_time_fs_;
      for (const std::size_t dependent : dependents_[reservation.operation_id]) {
        ++result_.scheduler_metrics.dependency_edge_visits;
        auto& remaining =
            remaining_dependencies_.at(operations_[dependent].key.event_id);
        if (remaining == 0) {
          throw EngineError("Phase 4 dependency counter underflow");
        }
        --remaining;
        if (remaining == 0 &&
            !future_arrivals_.contains(operations_[dependent].key)) {
          ready_[operations_[dependent].service_class].emplace(
              generated_key(
                  operations_[dependent], TraceKind::kStart, U128{0}),
              dependent);
          ++result_.scheduler_metrics.ready_queue_pushes;
        }
      }
      completion_queue_.erase(completion_queue_.begin());
      ++result_.scheduler_metrics.completion_queue_pops;
      active_.erase(reservation.operation_id);
      const EventKey key =
          generated_key(operation, TraceKind::kComplete, global_time_fs_);
      TraceEntry trace{
          reservation.operation_id,
          generated_id(operation, TraceKind::kComplete),
          TraceKind::kComplete,
          global_time_fs_, key.event_priority, key};
      result_.trace.push_back(trace);
      ++result_.scheduler_metrics.generated_event_count;
      return trace;
    }

    const Operation& operation = operations_[*start];
    const ServiceProfile& item = profile(operation.service_class);
    auto& primary = primary_busy_.at(operation.service_class);
    const auto primary_it = std::find(primary.begin(), primary.end(), false);
    const std::uint64_t primary_lane =
        static_cast<std::uint64_t>(std::distance(primary.begin(), primary_it));
    std::optional<std::uint64_t> shared_lane;
    if (item.uses_shared_fabric) {
      const auto shared_it =
          std::find(shared_busy_.begin(), shared_busy_.end(), false);
      shared_lane =
          static_cast<std::uint64_t>(
              std::distance(shared_busy_.begin(), shared_it));
    }
    const U128 ready = dependency_ready(operation);
    const U128 raw_end = checked_add(
        global_time_fs_, service_duration(item, operation.work),
        "Phase 4 completion time");
    const std::uint64_t completion_cycle =
        platform_.completion_clock.ceil_edge(raw_end);
    const U128 end =
        platform_.completion_clock.edge_time(completion_cycle);
    if (end <= global_time_fs_) {
      throw EngineError("Phase 4 completion lacks strict progress");
    }
    const U128 queue_delay = global_time_fs_ - ready;
    const ClassMetrics current_metrics =
        result_.class_metrics[operation.service_class];
    const U128 next_busy = checked_add(
        current_metrics.busy_lane_fs, end - global_time_fs_,
        "Phase 4 busy metric");
    const U128 next_queue = checked_add(
        current_metrics.queue_delay_fs, queue_delay,
        "Phase 4 queue metric");
    primary.at(primary_lane) = true;
    if (shared_lane.has_value()) {
      shared_busy_.at(*shared_lane) = true;
    }
    states_.at(operation.key.event_id) = OperationState::kInFlight;
    auto& ready_queue = ready_.at(operation.service_class);
    if (ready_queue.erase(
            {generated_key(operation, TraceKind::kStart, U128{0}), *start}) !=
        1) {
      throw EngineError("Phase 4 ready queue identity mismatch");
    }
    ++result_.scheduler_metrics.ready_queue_pops;
    const ActiveReservation reservation{
        operation.key.event_id, operation.service_class, primary_lane,
        shared_lane, end};
    if (!active_.emplace(operation.key.event_id, reservation).second) {
      throw EngineError("duplicate Phase 4 active reservation");
    }
    completion_queue_.emplace(
        generated_key(operation, TraceKind::kComplete, end),
        operation.key.event_id);
    ++result_.scheduler_metrics.completion_queue_pushes;
    result_.entries.push_back(
        {operation.key.event_id, item.profile_id, operation.service_class,
         ready, global_time_fs_, end, queue_delay, primary_lane, shared_lane});
    auto& metrics = result_.class_metrics[operation.service_class];
    metrics.busy_lane_fs = next_busy;
    metrics.queue_delay_fs = next_queue;
    ++metrics.operation_count;
    const EventKey key =
        generated_key(operation, TraceKind::kStart, global_time_fs_);
    TraceEntry trace{
        operation.key.event_id,
        generated_id(operation, TraceKind::kStart),
        TraceKind::kStart,
        global_time_fs_, key.event_priority, key};
    result_.trace.push_back(trace);
    ++result_.scheduler_metrics.generated_event_count;
    return trace;
  }
}

ScheduleResult SingleGpuModel::run_until_quiescent() {
  while (terminal_status_ == TerminalStatus::kRunning) {
    static_cast<void>(step());
  }
  if (terminal_status_ != TerminalStatus::kQuiescent) {
    throw EngineError("Phase 4 engine did not quiesce");
  }
  result_.semantic_digest = state_digest();
  return result_;
}

std::string SingleGpuModel::canonical_state() const {
  std::ostringstream stream;
  put(stream, "moe-phase4-state-v1");
  put(stream, platform_.platform_id);
  put(stream, platform_.phase3_ledger_sha256);
  put(stream, platform_.phase4_build_sha256);
  put(stream, platform_.model_contract_sha256);
  put(stream, platform_.checkpoint_schema_sha256);
  put(stream, platform_.completion_clock.clock_id);
  put(stream, std::to_string(platform_.completion_clock.frequency_numerator_hz));
  put(stream, std::to_string(platform_.completion_clock.frequency_denominator_hz));
  put(stream, to_decimal(platform_.completion_clock.phase_offset_fs));
  put(stream, std::to_string(platform_.completion_clock.local_cycle));
  put(
      stream,
      std::to_string(platform_.completion_clock.fractional_remainder));
  put(stream, std::to_string(platform_.shared_fabric_lanes));
  put(stream, "PROFILES");
  put(stream, std::to_string(platform_.profiles.size()));
  for (const auto& item : platform_.profiles) {
    put(stream, item.profile_id);
    put(stream, class_name(item.service_class));
    put(stream, std::to_string(item.lanes));
    put(stream, to_decimal(item.throughput_numerator_per_second));
    put(stream, to_decimal(item.throughput_denominator));
    put(stream, to_decimal(item.setup_latency_fs));
    put(stream, item.uses_shared_fabric ? "1" : "0");
    put(stream, fidelity_name(item.fidelity));
    put(stream, "RANGE_UNKNOWN");
  }
  put(stream, "OPERATIONS");
  put(stream, std::to_string(operations_.size()));
  for (const auto& operation : operations_) {
    put_key(stream, operation.key);
    put(stream, class_name(operation.service_class));
    put(stream, to_decimal(operation.work));
    put(stream, std::to_string(operation.dependencies.size()));
    for (const auto& dependency : operation.dependencies) put(stream, dependency);
  }
  put(stream, to_decimal(global_time_fs_));
  put(stream, std::to_string(static_cast<int>(terminal_status_)));
  put(stream, "STATES");
  put(stream, std::to_string(states_.size()));
  for (const auto& [id, state] : states_) {
    put(stream, id);
    put(stream, state_name(state));
  }
  put(stream, "ACTIVE");
  put(stream, std::to_string(active_.size()));
  for (const auto& [active_id, reservation] : active_) {
    put(stream, active_id);
    put(stream, reservation.operation_id);
    put(stream, class_name(reservation.service_class));
    put(stream, std::to_string(reservation.primary_lane));
    put(stream, reservation.shared_fabric_lane.has_value()
                    ? std::to_string(*reservation.shared_fabric_lane) : "-");
    put(stream, to_decimal(reservation.completion_fs));
  }
  put(stream, "COMPLETION_TIMES");
  put(stream, std::to_string(completion_times_.size()));
  for (const auto& [id, time] : completion_times_) {
    put(stream, id);
    put(stream, to_decimal(time));
  }
  put(stream, "REMAINING_DEPENDENCIES");
  put(stream, std::to_string(remaining_dependencies_.size()));
  for (const auto& [id, remaining] : remaining_dependencies_) {
    put(stream, id);
    put(stream, std::to_string(remaining));
  }
  put(stream, "FUTURE_ARRIVALS");
  put(stream, std::to_string(future_arrivals_.size()));
  for (const auto& [key, index] : future_arrivals_) {
    put_key(stream, key);
    put(stream, std::to_string(index));
  }
  put(stream, "READY");
  put(stream, std::to_string(ready_.size()));
  for (const auto& [service_class, queue] : ready_) {
    put(stream, class_name(service_class));
    put(stream, std::to_string(queue.size()));
    for (const auto& [key, index] : queue) {
      put_key(stream, key);
      put(stream, std::to_string(index));
    }
  }
  put(stream, "PRIMARY_BUSY");
  put(stream, std::to_string(primary_busy_.size()));
  for (const auto& [service_class, busy] : primary_busy_) {
    put(stream, class_name(service_class));
    put(stream, std::to_string(busy.size()));
    for (const bool value : busy) {
      put(stream, value ? "1" : "0");
    }
  }
  put(stream, "SHARED_BUSY");
  put(stream, std::to_string(shared_busy_.size()));
  for (const bool value : shared_busy_) {
    put(stream, value ? "1" : "0");
  }
  put(stream, "COMPLETION_QUEUE");
  put(stream, std::to_string(completion_queue_.size()));
  for (const auto& [key, id] : completion_queue_) {
    put_key(stream, key);
    put(stream, id);
  }
  put(stream, std::to_string(clock_cursor_cycle_));
  put(stream, std::to_string(clock_cursor_remainder_));
  put(stream, "SCHEDULE_ENTRIES");
  put(stream, std::to_string(result_.entries.size()));
  for (const auto& entry : result_.entries) {
    put(stream, entry.operation_id);
    put(stream, entry.profile_id);
    put(stream, class_name(entry.service_class));
    put(stream, to_decimal(entry.dependency_ready_fs));
    put(stream, to_decimal(entry.start_fs));
    put(stream, to_decimal(entry.end_fs));
    put(stream, to_decimal(entry.queue_delay_fs));
    put(stream, std::to_string(entry.primary_lane));
    put(
        stream,
        entry.shared_fabric_lane.has_value()
            ? std::to_string(*entry.shared_fabric_lane)
            : "-");
  }
  put(stream, "TRACE");
  put(stream, std::to_string(result_.trace.size()));
  for (const auto& trace : result_.trace) {
    put(stream, trace.operation_id);
    put(stream, trace.generated_event_id);
    put(stream, trace_name(trace.kind));
    put(stream, to_decimal(trace.time_fs));
    put(stream, std::to_string(trace.priority));
    put(stream, trace.generated_key.has_value() ? "1" : "0");
    if (trace.generated_key.has_value()) {
      put_key(stream, *trace.generated_key);
    }
  }
  put(stream, to_decimal(result_.makespan_fs));
  put(stream, std::to_string(static_cast<int>(result_.terminal_status)));
  put(stream, "CLASS_METRICS");
  put(stream, std::to_string(result_.class_metrics.size()));
  for (const auto& [service_class, metrics] : result_.class_metrics) {
    put(stream, class_name(service_class));
    put(stream, to_decimal(metrics.busy_lane_fs));
    put(stream, to_decimal(metrics.queue_delay_fs));
    put(stream, std::to_string(metrics.operation_count));
  }
  put(
      stream,
      std::to_string(result_.scheduler_metrics.scheduler_key_comparisons));
  put(stream, std::to_string(result_.scheduler_metrics.ready_queue_pushes));
  put(stream, std::to_string(result_.scheduler_metrics.ready_queue_pops));
  put(
      stream,
      std::to_string(result_.scheduler_metrics.dependency_edge_visits));
  put(
      stream,
      std::to_string(result_.scheduler_metrics.generated_event_count));
  put(
      stream,
      std::to_string(result_.scheduler_metrics.completion_queue_pushes));
  put(
      stream,
      std::to_string(result_.scheduler_metrics.completion_queue_pops));
  return stream.str();
}

std::string SingleGpuModel::state_digest() const {
  return sha256_bytes(canonical_state());
}

Checkpoint SingleGpuModel::checkpoint() const {
  Checkpoint value{
      "phase4-checkpoint-v1", global_time_fs_, terminal_status_,
      states_, active_, completion_times_, remaining_dependencies_,
      future_arrivals_, ready_, completion_queue_, primary_busy_, shared_busy_,
      clock_cursor_cycle_, clock_cursor_remainder_, result_, ""};
  value.state_digest = sha256_bytes(canonical_state());
  return value;
}

std::string SingleGpuModel::serialize_checkpoint() const {
  const std::string body = canonical_state();
  std::ostringstream stream;
  stream << "moe-phase4-checkpoint-v1\n"
         << result_.trace.size() << '\n'
         << static_cast<int>(terminal_status_) << '\n'
         << body.size() << '\n'
         << body
         << sha256_bytes(body) << '\n';
  return stream.str();
}

SingleGpuModel SingleGpuModel::restore(
    SingleGpuPlatform platform,
    std::vector<Operation> operations,
    const Checkpoint& checkpoint) {
  const bool semantic_digest_valid =
      (checkpoint.terminal_status == TerminalStatus::kRunning &&
       checkpoint.result.semantic_digest.empty()) ||
      (checkpoint.terminal_status != TerminalStatus::kRunning &&
       checkpoint.result.semantic_digest == checkpoint.state_digest);
  if (checkpoint.schema_version != "phase4-checkpoint-v1" ||
      checkpoint.state_digest.size() != 64 || !semantic_digest_valid) {
    throw EngineError("Phase 4 checkpoint schema or digest mismatch");
  }
  SingleGpuModel supplied(platform, operations);
  supplied.global_time_fs_ = checkpoint.global_time_fs;
  supplied.terminal_status_ = checkpoint.terminal_status;
  supplied.states_ = checkpoint.states;
  supplied.active_ = checkpoint.active;
  supplied.completion_times_ = checkpoint.completion_times;
  supplied.remaining_dependencies_ = checkpoint.remaining_dependencies;
  supplied.future_arrivals_ = checkpoint.future_arrivals;
  supplied.ready_ = checkpoint.ready;
  supplied.completion_queue_ = checkpoint.completion_queue;
  supplied.primary_busy_ = checkpoint.primary_busy;
  supplied.shared_busy_ = checkpoint.shared_busy;
  supplied.clock_cursor_cycle_ = checkpoint.clock_cursor_cycle;
  supplied.clock_cursor_remainder_ = checkpoint.clock_cursor_remainder;
  supplied.result_ = checkpoint.result;
  if (supplied.state_digest() != checkpoint.state_digest) {
    throw EngineError("Phase 4 checkpoint digest mismatch");
  }
  SingleGpuModel replay(std::move(platform), std::move(operations));
  while (replay.result_.trace.size() < checkpoint.result.trace.size()) {
    static_cast<void>(replay.step());
  }
  if (checkpoint.terminal_status != TerminalStatus::kRunning &&
      replay.terminal_status_ == TerminalStatus::kRunning) {
    const std::size_t trace_size = replay.result_.trace.size();
    static_cast<void>(replay.step());
    if (replay.result_.trace.size() != trace_size) {
      throw EngineError("Phase 4 terminal checkpoint is not a trace prefix");
    }
  }
  if (replay.state_digest() != checkpoint.state_digest ||
      replay.global_time_fs_ != checkpoint.global_time_fs ||
      replay.terminal_status_ != checkpoint.terminal_status ||
      replay.states_ != checkpoint.states ||
      replay.active_.size() != checkpoint.active.size() ||
      replay.clock_cursor_cycle_ != checkpoint.clock_cursor_cycle ||
      replay.clock_cursor_remainder_ != checkpoint.clock_cursor_remainder) {
    throw EngineError("Phase 4 checkpoint is not a reachable prefix");
  }
  return replay;
}

SingleGpuModel SingleGpuModel::restore_serialized(
    SingleGpuPlatform platform,
    std::vector<Operation> operations,
    const std::string& bytes) {
  std::size_t cursor = 0;
  const auto read_line = [&](std::size_t& position) {
    const std::size_t end = bytes.find('\n', position);
    if (end == std::string::npos) {
      throw EngineError("invalid Phase 4 checkpoint wire header");
    }
    const std::string value = bytes.substr(position, end - position);
    position = end + 1;
    return value;
  };
  const std::string magic = read_line(cursor);
  const std::string trace_count_text = read_line(cursor);
  const std::string terminal_text = read_line(cursor);
  const std::string body_size_text = read_line(cursor);
  if (magic != "moe-phase4-checkpoint-v1") {
    throw EngineError("invalid Phase 4 checkpoint wire header");
  }
  std::size_t trace_count = 0;
  std::size_t body_size = 0;
  int terminal = 0;
  const auto trace_parse = std::from_chars(
      trace_count_text.data(),
      trace_count_text.data() + trace_count_text.size(),
      trace_count);
  const auto terminal_parse = std::from_chars(
      terminal_text.data(), terminal_text.data() + terminal_text.size(),
      terminal);
  const auto body_parse = std::from_chars(
      body_size_text.data(), body_size_text.data() + body_size_text.size(),
      body_size);
  if (trace_parse.ec != std::errc{} ||
      trace_parse.ptr != trace_count_text.data() + trace_count_text.size() ||
      terminal_parse.ec != std::errc{} ||
      terminal_parse.ptr != terminal_text.data() + terminal_text.size() ||
      body_parse.ec != std::errc{} ||
      body_parse.ptr != body_size_text.data() + body_size_text.size() ||
      terminal < static_cast<int>(TerminalStatus::kRunning) ||
      terminal > static_cast<int>(TerminalStatus::kFailed) ||
      body_size > bytes.size() - cursor ||
      bytes.size() - cursor < 65 ||
      body_size != bytes.size() - cursor - 65) {
    throw EngineError("invalid Phase 4 checkpoint wire field");
  }
  const std::string body = bytes.substr(cursor, body_size);
  cursor += body_size;
  const std::string digest = bytes.substr(cursor, 64);
  cursor += 64;
  if (bytes[cursor] != '\n' ||
      digest.size() != 64 ||
      !std::all_of(digest.begin(), digest.end(), [](char value) {
        return (value >= '0' && value <= '9') ||
               (value >= 'a' && value <= 'f');
      }) ||
      sha256_bytes(body) != digest) {
    throw EngineError("invalid Phase 4 checkpoint wire field");
  }
  SingleGpuModel replay(std::move(platform), std::move(operations));
  while (replay.result_.trace.size() < trace_count) {
    static_cast<void>(replay.step());
  }
  const auto expected_terminal = static_cast<TerminalStatus>(terminal);
  if (expected_terminal != TerminalStatus::kRunning &&
      replay.terminal_status_ == TerminalStatus::kRunning) {
    const std::size_t before = replay.result_.trace.size();
    static_cast<void>(replay.step());
    if (replay.result_.trace.size() != before) {
      throw EngineError("Phase 4 wire terminal is not a trace prefix");
    }
  }
  if (replay.terminal_status_ != expected_terminal ||
      replay.state_digest() != digest ||
      replay.canonical_state() != body) {
    throw EngineError("Phase 4 checkpoint wire digest mismatch");
  }
  return replay;
}

}  // namespace moe_sim::phase4

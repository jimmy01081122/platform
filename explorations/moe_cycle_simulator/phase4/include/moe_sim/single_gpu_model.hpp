#pragma once

#include "moe_sim/engine.hpp"

#include <cstdint>
#include <map>
#include <optional>
#include <set>
#include <string>
#include <string_view>
#include <vector>

namespace moe_sim::phase4 {

inline constexpr std::string_view kPhase4BuildAuthoritySha256 =
    "6c96124ad794729ff502dd7970cfccfaa9eb8105df2de359743eedc83276f308";
inline constexpr std::string_view kPhase4ModelContractSha256 =
    "73072b3978674294703667d4970b7373e842b86a91fef0cd3cd9d6a4b10ed1e5";
inline constexpr std::string_view kPhase4CheckpointSchemaSha256 =
    "2c34e7cc327e331d06beed4ea9906ccda597228bcc7394dcb0a11552852774d9";

enum class ServiceClass { kCompute, kMemory, kH2D, kD2H };
enum class Fidelity { kAnalyticFirstOrder, kFunctionalOnly };
enum class RangeStatus { kRangeUnknown };
enum class OperationState { kPending, kInFlight, kComplete };
enum class TraceKind { kStart, kComplete };

struct ServiceProfile {
  std::string profile_id;
  ServiceClass service_class;
  std::uint64_t lanes;
  U128 throughput_numerator_per_second;
  U128 throughput_denominator;
  U128 setup_latency_fs;
  bool uses_shared_fabric;
  Fidelity fidelity;
  RangeStatus range_status;
};

struct SingleGpuPlatform {
  std::string platform_id;
  std::uint64_t shared_fabric_lanes;
  Clock completion_clock;
  std::vector<ServiceProfile> profiles;
  std::string phase3_ledger_sha256;
  std::string phase4_build_sha256;
  std::string model_contract_sha256;
  std::string checkpoint_schema_sha256;
};

struct Operation {
  EventKey key;
  std::vector<std::string> dependencies;
  ServiceClass service_class;
  U128 work;
};

struct ScheduleEntry {
  std::string operation_id;
  std::string profile_id;
  ServiceClass service_class;
  U128 dependency_ready_fs;
  U128 start_fs;
  U128 end_fs;
  U128 queue_delay_fs;
  std::uint64_t primary_lane;
  std::optional<std::uint64_t> shared_fabric_lane;
};

struct TraceEntry {
  std::string operation_id;
  std::string generated_event_id;
  TraceKind kind;
  U128 time_fs;
  std::uint32_t priority;
  std::optional<EventKey> generated_key;
};

struct ClassMetrics {
  U128 busy_lane_fs{0};
  U128 queue_delay_fs{0};
  std::uint64_t operation_count{0};
};

struct SchedulerMetrics {
  std::uint64_t scheduler_key_comparisons{0};
  std::uint64_t ready_queue_pushes{0};
  std::uint64_t ready_queue_pops{0};
  std::uint64_t dependency_edge_visits{0};
  std::uint64_t generated_event_count{0};
  std::uint64_t completion_queue_pushes{0};
  std::uint64_t completion_queue_pops{0};
};

struct ScheduleResult {
  std::vector<ScheduleEntry> entries;
  std::vector<TraceEntry> trace;
  std::map<ServiceClass, ClassMetrics> class_metrics;
  SchedulerMetrics scheduler_metrics;
  U128 makespan_fs{0};
  TerminalStatus terminal_status{TerminalStatus::kRunning};
  std::string semantic_digest;
};

struct ActiveReservation {
  std::string operation_id;
  ServiceClass service_class;
  std::uint64_t primary_lane;
  std::optional<std::uint64_t> shared_fabric_lane;
  U128 completion_fs;
};

struct Checkpoint {
  std::string schema_version{"phase4-checkpoint-v1"};
  U128 global_time_fs;
  TerminalStatus terminal_status;
  std::map<std::string, OperationState> states;
  std::map<std::string, ActiveReservation> active;
  std::map<std::string, U128> completion_times;
  std::map<std::string, std::uint64_t> remaining_dependencies;
  std::map<EventKey, std::size_t> future_arrivals;
  std::map<
      ServiceClass,
      std::set<std::pair<EventKey, std::size_t>>> ready;
  std::set<std::pair<EventKey, std::string>> completion_queue;
  std::map<ServiceClass, std::vector<bool>> primary_busy;
  std::vector<bool> shared_busy;
  std::uint64_t clock_cursor_cycle;
  std::uint64_t clock_cursor_remainder;
  ScheduleResult result;
  std::string state_digest;
};

class SingleGpuModel {
 public:
  SingleGpuModel(SingleGpuPlatform platform, std::vector<Operation> operations);

  [[nodiscard]] TraceEntry step();
  [[nodiscard]] ScheduleResult run_until_quiescent();
  [[nodiscard]] const ScheduleResult& result() const { return result_; }
  [[nodiscard]] Checkpoint checkpoint() const;
  [[nodiscard]] std::string serialize_checkpoint() const;
  [[nodiscard]] std::string state_digest() const;
  [[nodiscard]] static SingleGpuModel restore(
      SingleGpuPlatform platform,
      std::vector<Operation> operations,
      const Checkpoint& checkpoint);
  [[nodiscard]] static SingleGpuModel restore_serialized(
      SingleGpuPlatform platform,
      std::vector<Operation> operations,
      const std::string& bytes);

 private:
  SingleGpuPlatform platform_;
  std::vector<Operation> operations_;
  std::map<std::string, OperationState> states_;
  std::map<std::string, U128> completion_times_;
  std::map<std::string, std::size_t> operation_index_;
  std::map<std::string, std::uint64_t> remaining_dependencies_;
  std::map<std::string, std::vector<std::size_t>> dependents_;
  std::map<EventKey, std::size_t> future_arrivals_;
  std::map<
      ServiceClass,
      std::set<std::pair<EventKey, std::size_t>>> ready_;
  std::set<std::pair<EventKey, std::string>> completion_queue_;
  std::map<ServiceClass, std::vector<bool>> primary_busy_;
  std::vector<bool> shared_busy_;
  std::map<std::string, ActiveReservation> active_;
  std::uint64_t clock_cursor_cycle_{0};
  std::uint64_t clock_cursor_remainder_{0};
  U128 global_time_fs_{0};
  TerminalStatus terminal_status_{TerminalStatus::kRunning};
  ScheduleResult result_;

  void validate() const;
  [[nodiscard]] const ServiceProfile& profile(ServiceClass value) const;
  [[nodiscard]] U128 service_duration(
      const ServiceProfile& profile, const U128& work) const;
  [[nodiscard]] std::optional<std::size_t> feasible_start();
  [[nodiscard]] std::optional<std::string> next_completion() const;
  [[nodiscard]] U128 dependency_ready(const Operation& operation) const;
  [[nodiscard]] EventKey generated_key(
      const Operation& operation, TraceKind kind, const U128& time) const;
  [[nodiscard]] std::string generated_id(
      const Operation& operation, TraceKind kind) const;
  [[nodiscard]] std::string canonical_state() const;
  void activate_arrivals();
  void update_clock_cursor();
};

}  // namespace moe_sim::phase4

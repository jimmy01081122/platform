#pragma once

#include <boost/multiprecision/cpp_int.hpp>

#include <cstdint>
#include <map>
#include <optional>
#include <set>
#include <stdexcept>
#include <string>
#include <tuple>
#include <unordered_map>
#include <utility>
#include <vector>

namespace moe_sim {

using U128 = boost::multiprecision::uint128_t;

class EngineError : public std::runtime_error {
 public:
  using std::runtime_error::runtime_error;
};

std::string to_decimal(const U128& value);
U128 parse_u128(const std::string& value);
std::string sha256_bytes(const std::string& value);

struct Clock {
  std::string clock_id;
  std::uint64_t frequency_numerator_hz;
  std::uint64_t frequency_denominator_hz;
  U128 phase_offset_fs;
  std::uint64_t local_cycle{0};
  std::uint64_t fractional_remainder{0};

  void validate() const;
  [[nodiscard]] U128 edge_time(std::uint64_t cycle) const;
  [[nodiscard]] std::uint64_t remainder(std::uint64_t cycle) const;
  [[nodiscard]] std::uint64_t ceil_edge(const U128& time_fs) const;
};

enum class BridgeProtocol { kOneWay, kRequestAck, kCredit };
enum class BackpressurePolicy { kStallSource, kReject, kCreditBlock };

struct Bridge {
  std::string bridge_id;
  std::string source_clock_id;
  std::string target_clock_id;
  BridgeProtocol protocol;
  U128 forward_latency_fs;
  U128 reverse_latency_fs;
  std::uint64_t receiver_sync_cycles;
  std::uint64_t ack_sync_cycles;
  std::uint64_t queue_capacity;
  BackpressurePolicy backpressure_policy;

  void validate(const std::map<std::string, Clock>& clocks) const;
  [[nodiscard]] U128 arrival(
      const U128& source_completion_fs,
      const std::map<std::string, Clock>& clocks) const;
};

struct EventKey {
  U128 time_fs;
  std::uint32_t event_priority;
  std::optional<std::string> request_id;
  std::optional<std::uint64_t> token_index;
  std::optional<std::uint32_t> layer_index;
  std::string component_id;
  std::string event_id;

  [[nodiscard]] auto order_tuple() const {
    return std::tuple{
        time_fs,
        event_priority,
        request_id.value_or(""),
        token_index.value_or(UINT64_MAX),
        layer_index.value_or(UINT32_MAX),
        component_id,
        event_id};
  }
  bool operator<(const EventKey& other) const {
    return order_tuple() < other.order_tuple();
  }
  bool operator==(const EventKey& other) const {
    return order_tuple() == other.order_tuple();
  }
};

enum class ResourceKind {
  kComputeSlots,
  kMemoryBytes,
  kQueueEntries,
  kBridgeQueueEntries,
  kBridgeCredits,
  kUnsupportedPhase4,
};
enum class Arbitration { kFifo, kPriority, kRoundRobinUnsupported };
enum class Action { kAcquire, kRelease, kService, kTransferUnsupported };
enum class EventState {
  kPending,
  kBlocked,
  kComplete,
  kFailed,
};
enum class TerminalStatus {
  kRunning,
  kQuiescent,
  kDeadlock,
  kZeno,
  kFailed,
};

struct Event {
  EventKey key;
  std::vector<std::string> dependencies;
  Action action;
  std::string resource_id;
  std::string owner_id;
  U128 quantity;
  U128 service_demand;
};

struct Resource {
  std::string resource_id;
  ResourceKind kind;
  Arbitration arbitration;
  U128 capacity;
  U128 occupancy{0};
  std::map<std::string, U128> holders;
  std::vector<std::string> waiters;
};

struct EngineConfig {
  std::uint64_t max_events{1'000'000};
  std::uint64_t max_same_time_events{100'000};
  std::uint64_t max_wait_for_nodes{100'000};
  std::string phase2_ledger_sha256;
  std::string canonical_bundle_semantic_root;
  std::string engine_build_sha256;
  std::string engine_profile_sha256;
  std::string checkpoint_schema_sha256;
};

struct TraceEntry {
  std::string event_id;
  U128 scheduled_time_fs;
  U128 executed_time_fs;
  EventState state;
};

struct StepResult {
  std::string event_id;
  EventState state;
  U128 global_time_fs;
};

struct Checkpoint {
  std::string schema_version{"checkpoint-v1"};
  EngineConfig config;
  U128 global_time_fs;
  std::optional<U128> last_event_time_fs;
  std::uint64_t same_time_count;
  std::uint64_t processed_count;
  TerminalStatus terminal_status;
  std::vector<Event> events;
  std::map<std::string, EventState> states;
  std::map<std::string, Clock> clocks;
  std::map<std::string, Resource> resources;
  std::vector<TraceEntry> trace;
  std::string state_digest;
};

class Engine {
 public:
  Engine(
      std::vector<Event> events,
      std::vector<Resource> resources,
      EngineConfig config,
      std::vector<Clock> clocks = {});

  [[nodiscard]] StepResult step();
  [[nodiscard]] TerminalStatus run_until_quiescent();
  [[nodiscard]] TerminalStatus run_until_time(const U128& inclusive_limit);
  [[nodiscard]] std::string diagnose_deadlock() const;
  [[nodiscard]] std::string state_digest() const;
  [[nodiscard]] const std::vector<TraceEntry>& result_trace() const {
    return trace_;
  }
  [[nodiscard]] TerminalStatus terminal_status() const {
    return terminal_status_;
  }
  [[nodiscard]] Checkpoint checkpoint() const;
  [[nodiscard]] std::string serialize_checkpoint() const;
  [[nodiscard]] static std::string checkpoint_digest(
      const Checkpoint& checkpoint);
  static Engine restore(
      const Checkpoint& checkpoint,
      const EngineConfig& expected_config);
 static Engine restore_serialized(
      const std::string& bytes,
      const EngineConfig& expected_config);

 private:
  struct RestoreConstructionTag {};

  Engine(
      std::vector<Event> events,
      std::vector<Resource> resources,
      EngineConfig config,
      std::vector<Clock> clocks,
      RestoreConstructionTag);

  std::vector<Event> events_;
  std::map<std::string, EventState> states_;
  std::map<std::string, Clock> clocks_;
  std::map<std::string, Resource> resources_;
  EngineConfig config_;
  U128 global_time_fs_{0};
  std::optional<U128> last_event_time_fs_;
  std::uint64_t same_time_count_{0};
  std::uint64_t processed_count_{0};
  TerminalStatus terminal_status_{TerminalStatus::kRunning};
  std::vector<TraceEntry> trace_;

  void validate_inputs() const;
  void validate_runtime_state() const;
  [[nodiscard]] std::optional<std::size_t> next_ready_pending_index() const;
  [[nodiscard]] bool has_pending_events() const;
  [[nodiscard]] bool dependencies_complete(const Event& event) const;
  void acquire(const Event& event);
  void release(const Event& event);
  void drain_waiters(Resource& resource);
  void account_transition();
  void mark_complete(const Event& event, const U128& executed_time);
  [[nodiscard]] bool has_blocked_events() const;
};

}  // namespace moe_sim

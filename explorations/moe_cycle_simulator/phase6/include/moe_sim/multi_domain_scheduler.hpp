#pragma once

#include "moe_sim/routing_residency_policy.hpp"

#include <compare>
#include <cstddef>
#include <cstdint>
#include <map>
#include <optional>
#include <set>
#include <string>
#include <string_view>
#include <tuple>
#include <vector>

namespace moe_sim::phase6 {

inline constexpr std::string_view kPhase6BuildAuthoritySha256 =
    "e5c730b98d3bc0e3588780531269e6e129b861dd294011651d20911b8af4d41b";
inline constexpr std::string_view kPhase6TopologyContractSha256 =
    "6499e96c3a4b74d3097f27c0506eee83ed7a3cedf3713bcfb36f8a1bc043589b";
inline constexpr std::string_view kPhase6CdcContractSha256 =
    "9cdb23d0b91c0f61a3df7bb9bd2839ec4dbb07a11388563a2d999cdeed45f28a";
inline constexpr std::string_view kPhase6P2pContractSha256 =
    "8d003152ede06fdda1218339c18acc6d00babe4dc185813a14455004ab26fd88";
inline constexpr std::string_view kPhase6UmaContractSha256 =
    "d85159e78f4aac44e0185efc866c7d2b8147e8380dbfcdbe86f57aa82ade9c16";
inline constexpr std::string_view kPhase6CheckpointContractSha256 =
    "422c82a80e65763783bc47593b44800f152be13e97f91ce0ccceefdc4c8dc840";
inline constexpr std::string_view kPhase6ClaimBoundarySha256 =
    "a1a2221baeb8610330e84fa9aeff1551aa80b575bad54ba2f8ea7127a3fe354e";
inline constexpr std::string_view kPhase5LedgerSha256 =
    "b6b593a1fe79f1ddb60706c5c4af17470608c08589cd654eb7ea006d71ded834";
inline constexpr std::string_view kPhase5ReviewLedgerSha256 =
    "fb08d88c0f7ea381492a067ea10ee8286e24e63ca205e706bf0805967f10d93e";
inline constexpr std::string_view kPhase5ReviewAggregateSha256 =
    "c4e3140a301721f13d1b33c111ca9dd09388247b06db84e1cdf11433ca703370";

enum class PlatformMode {
  kDiscreteP2p2Gpu,
  kCoherentUma2Compute,
};

enum class OperationKind {
  kActivationDispatch,
  kExpertCompute,
  kActivationReturn,
  kTokenCombine,
  kExpertReplicate,
  kExpertMove,
  kUmaImmutableRead,
  kUmaMutableAcquireWrite,
  kUmaMutableRelease,
  kUmaMutableRead,
};

enum class OperationState { kPending, kInFlight, kComplete };
enum class TraceKind { kStart, kVisible, kAckOrCredit, kComplete };
enum class CoherenceState { kUncached, kShared, kModified };
enum class Fidelity { kFunctionalOnly, kAnalyticFirstOrder };
enum class RangeStatus { kRangeUnknown };

struct ContractAuthority {
  std::string build_authority_sha256;
  std::string topology_contract_sha256;
  std::string cdc_contract_sha256;
  std::string p2p_contract_sha256;
  std::string uma_contract_sha256;
  std::string checkpoint_contract_sha256;
  std::string claim_boundary_sha256;
  std::string phase5_ledger_sha256;
  std::string phase5_review_ledger_sha256;
  std::string phase5_review_aggregate_sha256;
};

struct ComputeDomain {
  std::string domain_id;
  std::string memory_domain_id;
  Clock clock;
  std::uint64_t compute_lanes;
  U128 throughput_numerator_per_second;
  U128 throughput_denominator;
  U128 setup_latency_fs;
};

struct MemoryDomain {
  std::string domain_id;
  U128 capacity_bytes;
};

struct DirectedLink {
  std::string link_id;
  std::string source_compute_id;
  std::string target_compute_id;
  Bridge bridge;
  U128 throughput_numerator_bytes_per_second;
  U128 throughput_denominator;
  U128 setup_latency_fs;
  std::uint64_t directional_lanes;
  std::string duplex_group;
  bool full_duplex;
  std::uint64_t initial_credits;
};

struct UmaFabric {
  std::string fabric_id;
  std::string memory_domain_id;
  Clock fabric_clock;
  std::uint64_t lanes;
  U128 throughput_numerator_bytes_per_second;
  U128 throughput_denominator;
  U128 setup_latency_fs;
  std::uint64_t queue_capacity;
  BridgeProtocol protocol;
  std::uint64_t initial_credits;
  U128 forward_latency_fs;
  U128 reverse_latency_fs;
  std::uint64_t receiver_sync_cycles;
  std::uint64_t ack_sync_cycles;
};

struct SyntheticClockAlignment {
  std::string clock_id;
  std::string calibration_method;
  U128 residual_error_fs;
  U128 confidence_interval_95_lower_fs;
  U128 confidence_interval_95_upper_fs;
  std::string quality;
  std::string evidence_label;
  U128 valid_start_fs;
  U128 valid_end_fs;
};

struct Topology {
  std::string topology_id;
  PlatformMode mode;
  std::vector<ComputeDomain> compute_domains;
  std::vector<MemoryDomain> memory_domains;
  std::vector<DirectedLink> links;
  std::optional<UmaFabric> uma_fabric;
  std::vector<SyntheticClockAlignment> synthetic_alignments;
  ContractAuthority authority;
};

struct TrafficPayloadProfile {
  std::string profile_id;
  U128 activation_bytes;
  U128 expert_bytes;
  U128 immutable_read_bytes;
  U128 mutable_object_bytes;
  U128 compute_work;
  Fidelity fidelity;
  RangeStatus range_status;
  std::string profile_sha256;
};

struct InitialObject {
  std::string object_id;
  U128 bytes;
  bool immutable;
  std::optional<phase5::ExpertKey> expert;
  std::string content_sha256;
  std::set<std::string> locations;
  std::uint32_t pins;
  std::uint64_t version;
  CoherenceState coherence_state;
  std::optional<std::string> owner;
  std::set<std::string> sharers;
};

struct RoutingBinding {
  std::string demand_id;
  EventKey route_key;
  std::uint32_t top_k;
  std::string token_owner_compute_domain_id;
  std::vector<phase5::ExpertKey> selected_experts;
  std::map<phase5::ExpertKey, std::string> assigned_compute_domains;
  std::string routing_provenance;
};

struct Operation {
  EventKey key;
  std::vector<std::string> dependencies;
  std::vector<std::string> source_phase5_action_ids;
  OperationKind kind;
  std::string demand_id;
  std::optional<phase5::ExpertKey> expert;
  std::string source_domain_id;
  std::string target_domain_id;
  std::string object_id;
  U128 bytes;
  U128 work;
  std::uint64_t expected_version;
  std::uint64_t output_version;
};

struct Program {
  std::string program_id;
  std::string phase5_plan_digest;
  std::string phase5_action_digest;
  std::vector<phase5::CompiledAction> phase5_actions;
  std::vector<std::string> allowed_phase5_action_ids;
  std::string phase5_ledger_sha256;
  TrafficPayloadProfile payload_profile;
  std::vector<InitialObject> initial_objects;
  std::vector<RoutingBinding> routing;
  std::vector<Operation> operations;
};

struct ResourceState {
  std::uint64_t capacity;
  std::uint64_t occupancy;
};

struct LinkRuntime {
  std::uint64_t queue_occupancy;
  std::uint64_t credits_available;
  std::uint64_t requests_started;
  std::uint64_t forward_completions;
  std::uint64_t acknowledgements;
  std::uint64_t credits_returned;
};

struct ObjectRuntime {
  U128 bytes;
  bool immutable;
  std::optional<phase5::ExpertKey> expert;
  std::string content_sha256;
  std::set<std::string> locations;
  std::uint32_t pins;
  std::uint64_t version;
  CoherenceState coherence_state;
  std::optional<std::string> owner;
  std::set<std::string> sharers;
};

struct ActiveReservation {
  std::string operation_id;
  std::vector<std::string> resource_ids;
  U128 visible_fs;
  U128 ack_fs;
  U128 completion_fs;
  bool visible_applied;
  bool ack_applied;
};

struct ScheduledEvent {
  EventKey key;
  std::string operation_id;
  TraceKind kind;

  bool operator<(const ScheduledEvent& other) const {
    if (key < other.key) return true;
    if (other.key < key) return false;
    if (kind != other.kind) return kind < other.kind;
    return operation_id < other.operation_id;
  }
  bool operator==(const ScheduledEvent& other) const {
    return key == other.key && kind == other.kind &&
           operation_id == other.operation_id;
  }
};

struct TraceEntry {
  std::string operation_id;
  TraceKind kind;
  U128 time_fs;
  EventKey key;
};

struct ScheduleEntry {
  std::string operation_id;
  U128 dependency_ready_fs;
  U128 start_fs;
  U128 visible_fs;
  U128 ack_fs;
  U128 completion_fs;
  std::vector<std::string> resource_ids;
};

struct SchedulerMetrics {
  std::uint64_t ready_queue_pushes{0};
  std::uint64_t ready_queue_pops{0};
  std::uint64_t completion_queue_pushes{0};
  std::uint64_t completion_queue_pops{0};
  std::uint64_t dependency_edge_visits{0};
  std::uint64_t scheduler_key_comparisons{0};
  std::uint64_t atomic_admission_attempts{0};
  std::uint64_t trace_events{0};
  std::uint64_t dispatches{0};
  std::uint64_t computes{0};
  std::uint64_t returns{0};
  std::uint64_t combines{0};
  std::uint64_t transfers_visible{0};
  std::uint64_t ack_or_credit_events{0};
  std::uint64_t queue_peak{0};
  std::uint64_t uma_reads{0};
  std::uint64_t uma_writes{0};
};

struct Result {
  std::vector<ScheduleEntry> entries;
  std::vector<TraceEntry> trace;
  SchedulerMetrics metrics;
  std::map<std::string, ObjectRuntime> terminal_objects;
  std::map<std::string, U128> terminal_memory_occupancy;
  U128 makespan_fs{0};
  TerminalStatus terminal_status{TerminalStatus::kRunning};
  Fidelity fidelity{Fidelity::kFunctionalOnly};
  RangeStatus range_status{RangeStatus::kRangeUnknown};
  std::string profile_origin{"CPU_SYNTHETIC"};
  std::string execution_claim;
  bool calibration_pass{false};
  bool gpu_used{false};
  std::string semantic_digest;
};

struct Checkpoint {
  std::string schema_version{"phase6-checkpoint-v1"};
  std::string topology_digest;
  std::string program_digest;
  U128 global_time_fs;
  TerminalStatus terminal_status;
  std::map<std::string, OperationState> states;
  std::map<std::string, ActiveReservation> active;
  std::map<std::string, U128> completion_times;
  std::map<std::string, std::uint64_t> remaining_dependencies;
  std::map<EventKey, std::size_t> future_arrivals;
  std::set<std::pair<EventKey, std::size_t>> ready;
  std::set<ScheduledEvent> completion_queue;
  std::map<std::string, ResourceState> resources;
  std::map<std::string, LinkRuntime> links;
  std::map<std::string, ObjectRuntime> objects;
  std::map<std::string, U128> memory_occupancy;
  std::set<std::pair<std::string, std::string>>
      inflight_destination_reservations;
  std::map<std::string, std::string> uma_object_reservations;
  std::map<std::string, std::uint64_t> clock_cycles;
  std::map<std::string, std::uint64_t> clock_remainders;
  Result result;
  std::string state_digest;
};

class MultiDomainSchedulerV1 {
 public:
  MultiDomainSchedulerV1(Topology topology, Program program);

  [[nodiscard]] TraceEntry step();
  [[nodiscard]] Result run_until_quiescent();
  [[nodiscard]] const Result& result() const { return result_; }
  [[nodiscard]] Checkpoint checkpoint() const;
  [[nodiscard]] std::string serialize_checkpoint() const;
  [[nodiscard]] std::string state_digest() const;
  [[nodiscard]] std::string topology_digest() const;
  [[nodiscard]] std::string program_digest() const;
  [[nodiscard]] static std::string traffic_profile_digest(
      const TrafficPayloadProfile& profile);
  [[nodiscard]] static std::string phase5_action_set_digest(
      const std::vector<std::string>& action_ids);
  [[nodiscard]] static std::string phase5_compiled_action_id(
      const std::string& plan_digest,
      std::size_t sequence,
      const phase5::CompiledAction& action);
  [[nodiscard]] static MultiDomainSchedulerV1 restore(
      Topology topology, Program program, const Checkpoint& checkpoint);
  [[nodiscard]] static MultiDomainSchedulerV1 restore_serialized(
      Topology topology, Program program, const std::string& bytes);

 private:
  Topology topology_;
  Program program_;
  std::map<std::string, OperationState> states_;
  std::map<std::string, std::size_t> operation_index_;
  std::map<std::string, std::uint64_t> remaining_dependencies_;
  std::map<std::string, std::vector<std::size_t>> dependents_;
  std::map<EventKey, std::size_t> future_arrivals_;
  std::set<std::pair<EventKey, std::size_t>> ready_;
  std::set<ScheduledEvent> completion_queue_;
  std::map<std::string, ActiveReservation> active_;
  std::map<std::string, U128> completion_times_;
  std::map<std::string, ResourceState> resources_;
  std::map<std::string, LinkRuntime> links_;
  std::map<std::string, ObjectRuntime> objects_;
  std::map<std::string, U128> memory_occupancy_;
  std::set<std::pair<std::string, std::string>>
      inflight_destination_reservations_;
  std::map<std::string, std::string> uma_object_reservations_;
  std::map<std::string, Clock> clocks_;
  std::string topology_digest_;
  std::string program_digest_;
  U128 global_time_fs_{0};
  TerminalStatus terminal_status_{TerminalStatus::kRunning};
  Result result_;

  void validate_and_compile();
  void initialize_runtime();
  void activate_arrivals();
  [[nodiscard]] U128 dependency_ready(const Operation& operation) const;
  [[nodiscard]] std::vector<std::string> required_resources(
      const Operation& operation) const;
  [[nodiscard]] bool can_admit(
      const Operation& operation,
      const std::vector<std::string>& resource_ids) const;
  [[nodiscard]] U128 service_duration(
      const U128& work,
      const U128& throughput_numerator,
      const U128& throughput_denominator,
      const U128& setup) const;
  [[nodiscard]] std::tuple<U128, U128, U128> timing(
      const Operation& operation, const U128& start) const;
  void apply_visibility(const Operation& operation);
  void validate_runtime_operation(const Operation& operation) const;
  void release_resources(const ActiveReservation& reservation);
  void update_clocks();
  void validate_alignment_time(const U128& time) const;
  void validate_object_invariants(
      const ObjectRuntime& object, bool uma_mode) const;
  void reconcile_terminal_capacity() const;
  [[nodiscard]] EventKey trace_key(
      const Operation& operation, TraceKind kind, const U128& time) const;
  [[nodiscard]] std::string canonical_state() const;
};

}  // namespace moe_sim::phase6

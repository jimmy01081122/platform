#pragma once

#include "moe_sim/single_gpu_model.hpp"

#include <compare>
#include <cstddef>
#include <cstdint>
#include <map>
#include <optional>
#include <set>
#include <string>
#include <string_view>
#include <vector>

namespace moe_sim::phase5 {

inline constexpr std::string_view kPhase5BuildAuthoritySha256 =
    "30a5987d2682db7da485e0aff7fdc676df6b9c06d65bb96114df7fba407fb68c";
inline constexpr std::string_view kPhase5PolicyContractSha256 =
    "03ffa9592bcd71c4a4cb8193010e9ae15d5adf67e65322f30b5a738e76ed468d";
inline constexpr std::string_view kPhase5CheckpointSchemaSha256 =
    "8c12cf9716b8d968366ef2300c4bef60c804ce6f8d66d6f77d730344cdb753ec";

enum class ExecutionMode { kTraceCompiledNonAdaptive };
enum class PrefetchMode { kOff, kHint };
enum class EvictionPolicy { kLru, kFifo };
enum class ActionKind {
  kHintBarrier,
  kRouteBarrier,
  kCleanEvict,
  kH2DLoad,
  kComputeAssignment
};

struct ExpertKey {
  std::uint32_t layer;
  std::uint32_t expert;

  auto operator<=>(const ExpertKey&) const = default;
};

struct ExpertRecord {
  ExpertKey key;
  U128 bytes;
};

struct TokenDemand {
  std::string demand_id;
  EventKey route_key;
  std::uint32_t top_k;
  std::vector<ExpertKey> selected_experts;
  std::string routing_provenance;
  U128 compute_work_per_expert;
};

struct PrefetchHint {
  std::string hint_id;
  EventKey arrival_key;
  std::string target_demand_id;
  ExpertKey expert;
  std::string provenance;
};

struct PolicyAuthority {
  std::string phase5_build_sha256;
  std::string policy_contract_sha256;
  std::string checkpoint_schema_sha256;
};

struct PolicyPlan {
  std::string plan_id;
  ExecutionMode execution_mode;
  PrefetchMode prefetch_mode;
  EvictionPolicy eviction_policy;
  U128 capacity_bytes;
  U128 reserved_nonexpert_bytes;
  std::vector<ExpertRecord> catalog;
  std::vector<ExpertKey> base_resident;
  std::vector<TokenDemand> demands;
  std::vector<PrefetchHint> hints;
  PolicyAuthority authority;
};

struct CompiledAction {
  std::string action_id;
  ActionKind kind;
  std::optional<ExpertKey> expert;
  std::string source_id;
  std::vector<std::string> dependencies;
  phase4::ServiceClass service_class;
  U128 work;
  U128 release_fs;
  bool prefetch_load;
};

struct CompiledPlan {
  std::string plan_digest;
  std::vector<CompiledAction> actions;
  std::vector<phase4::Operation> operations;
  std::set<ExpertKey> expected_terminal_resident;
  U128 expected_terminal_resident_bytes{0};
  std::uint64_t expected_assignments{0};
};

struct PolicyMetrics {
  std::uint64_t routing_demands{0};
  std::uint64_t assignments{0};
  std::uint64_t loads{0};
  std::uint64_t clean_evictions{0};
  std::uint64_t prefetch_loads{0};
  std::uint64_t useful_prefetches{0};
  std::uint64_t wasted_prefetches{0};
  std::uint64_t ignored_hints{0};
  std::uint64_t replay_action_lookups{0};
  U128 peak_resident_bytes{0};
};

struct PolicyResult {
  std::string plan_digest;
  std::string phase4_semantic_digest;
  std::string terminal_residency_digest;
  std::string semantic_digest;
  std::set<ExpertKey> terminal_resident;
  U128 terminal_resident_bytes;
  PolicyMetrics metrics;
  phase4::ScheduleResult phase4_result;
};

struct PolicyCheckpoint {
  std::string schema_version{"phase5-checkpoint-v1"};
  std::string plan_digest;
  std::size_t trace_prefix_size;
  std::string residency_digest;
  phase4::Checkpoint phase4_checkpoint;
  std::string state_digest;
};

class RoutingResidencyModel {
 public:
  RoutingResidencyModel(
      phase4::SingleGpuPlatform platform, PolicyPlan plan);

  [[nodiscard]] static CompiledPlan compile(const PolicyPlan& plan);
  [[nodiscard]] phase4::TraceEntry step();
  [[nodiscard]] PolicyResult run_until_quiescent();
  [[nodiscard]] PolicyCheckpoint checkpoint() const;
  [[nodiscard]] std::string serialize_checkpoint() const;
  [[nodiscard]] const CompiledPlan& compiled_plan() const { return compiled_; }
  [[nodiscard]] const phase4::ScheduleResult& phase4_result() const;
  [[nodiscard]] static RoutingResidencyModel restore(
      phase4::SingleGpuPlatform platform,
      PolicyPlan plan,
      const PolicyCheckpoint& checkpoint);
  [[nodiscard]] static RoutingResidencyModel restore_serialized(
      phase4::SingleGpuPlatform platform,
      PolicyPlan plan,
      const std::string& bytes);

 private:
  struct ReplayState {
    std::set<ExpertKey> resident;
    std::map<ExpertKey, std::uint32_t> pins;
    std::map<ExpertKey, std::string> pending_prefetches;
    std::set<std::string> started;
    std::set<std::string> completed;
    U128 resident_bytes{0};
    PolicyMetrics metrics;
  };

  phase4::SingleGpuPlatform platform_;
  PolicyPlan plan_;
  CompiledPlan compiled_;
  std::optional<phase4::SingleGpuModel> core_;
  std::map<std::string, std::size_t> action_index_;
  std::set<std::string> prefetch_sources_;
  std::map<ExpertKey, U128> catalog_bytes_;
  ReplayState replay_;
  std::size_t processed_trace_{0};
  bool finalized_{false};

  [[nodiscard]] ReplayState initial_replay() const;
  void build_replay_indexes();
  void replay_one(const phase4::TraceEntry& trace);
  void replay_existing_prefix();
  [[nodiscard]] PolicyResult finish_result();
  [[nodiscard]] std::string replay_digest(const ReplayState& state) const;
  [[nodiscard]] std::string checkpoint_state_digest(
      std::size_t prefix,
      const std::string& residency_digest,
      const std::string& phase4_state_digest) const;
};

}  // namespace moe_sim::phase5

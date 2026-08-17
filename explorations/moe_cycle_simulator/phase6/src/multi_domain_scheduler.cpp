#include "moe_sim/multi_domain_scheduler.hpp"

#include <algorithm>
#include <charconv>
#include <limits>
#include <numeric>
#include <sstream>
#include <tuple>

namespace moe_sim::phase6 {
namespace {

using boost::multiprecision::cpp_int;
constexpr std::uint64_t kFsPerSecond = 1'000'000'000'000'000ULL;
constexpr std::size_t kMaxOperations = 100'000;

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

bool is_hex_hash(const std::string& value) {
  return value.size() == 64 &&
         std::all_of(value.begin(), value.end(), [](const char item) {
           return (item >= '0' && item <= '9') ||
                  (item >= 'a' && item <= 'f');
         });
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

std::string mode_name(const PlatformMode value) {
  switch (value) {
    case PlatformMode::kDiscreteP2p2Gpu:
      return "DISCRETE_P2P_2GPU";
    case PlatformMode::kCoherentUma2Compute:
      return "COHERENT_UMA_2COMPUTE";
  }
  throw EngineError("invalid Phase 6 platform mode");
}

std::string kind_name(const OperationKind value) {
  switch (value) {
    case OperationKind::kActivationDispatch:
      return "ACTIVATION_DISPATCH";
    case OperationKind::kExpertCompute:
      return "EXPERT_COMPUTE";
    case OperationKind::kActivationReturn:
      return "ACTIVATION_RETURN";
    case OperationKind::kTokenCombine:
      return "TOKEN_COMBINE";
    case OperationKind::kExpertReplicate:
      return "EXPERT_REPLICATE";
    case OperationKind::kExpertMove:
      return "EXPERT_MOVE";
    case OperationKind::kUmaImmutableRead:
      return "UMA_IMMUTABLE_READ";
    case OperationKind::kUmaMutableAcquireWrite:
      return "UMA_MUTABLE_ACQUIRE_WRITE";
    case OperationKind::kUmaMutableRelease:
      return "UMA_MUTABLE_RELEASE";
    case OperationKind::kUmaMutableRead:
      return "UMA_MUTABLE_READ";
  }
  throw EngineError("invalid Phase 6 operation kind");
}

std::string trace_name(const TraceKind value) {
  switch (value) {
    case TraceKind::kStart: return "START";
    case TraceKind::kVisible: return "VISIBLE";
    case TraceKind::kAckOrCredit: return "ACK_OR_CREDIT";
    case TraceKind::kComplete: return "COMPLETE";
  }
  throw EngineError("invalid Phase 6 trace kind");
}

std::string state_name(const OperationState value) {
  switch (value) {
    case OperationState::kPending: return "PENDING";
    case OperationState::kInFlight: return "IN_FLIGHT";
    case OperationState::kComplete: return "COMPLETE";
  }
  throw EngineError("invalid Phase 6 operation state");
}

std::string coherence_name(const CoherenceState value) {
  switch (value) {
    case CoherenceState::kUncached: return "UNCACHED";
    case CoherenceState::kShared: return "SHARED";
    case CoherenceState::kModified: return "MODIFIED";
  }
  throw EngineError("invalid Phase 6 coherence state");
}

std::string fidelity_name(const Fidelity value) {
  switch (value) {
    case Fidelity::kFunctionalOnly: return "FUNCTIONAL_ONLY";
    case Fidelity::kAnalyticFirstOrder: return "ANALYTIC_FIRST_ORDER";
  }
  throw EngineError("invalid Phase 6 fidelity");
}

std::string expert_name(const phase5::ExpertKey& expert) {
  return std::to_string(expert.layer) + ":" + std::to_string(expert.expert);
}

std::string assignment_name(
    const std::string& demand, const phase5::ExpertKey& expert) {
  return demand + "\x1f" + expert_name(expert);
}

bool is_transfer(const OperationKind kind) {
  return kind == OperationKind::kActivationDispatch ||
         kind == OperationKind::kActivationReturn ||
         kind == OperationKind::kExpertReplicate ||
         kind == OperationKind::kExpertMove;
}

bool is_uma(const OperationKind kind) {
  return kind == OperationKind::kUmaImmutableRead ||
         kind == OperationKind::kUmaMutableAcquireWrite ||
         kind == OperationKind::kUmaMutableRelease ||
         kind == OperationKind::kUmaMutableRead;
}

std::uint32_t trace_priority(const OperationKind kind, const TraceKind trace) {
  if (trace == TraceKind::kVisible) return 20;
  if (trace == TraceKind::kAckOrCredit) return 40;
  if (trace == TraceKind::kComplete) return 30;
  return (is_transfer(kind) || is_uma(kind)) ? 90 : 100;
}

const ComputeDomain& compute(
    const Topology& topology, const std::string& id) {
  const auto found = std::find_if(
      topology.compute_domains.begin(), topology.compute_domains.end(),
      [&](const auto& item) { return item.domain_id == id; });
  if (found == topology.compute_domains.end()) {
    throw EngineError("unknown Phase 6 compute domain: " + id);
  }
  return *found;
}

const MemoryDomain& memory(
    const Topology& topology, const std::string& id) {
  const auto found = std::find_if(
      topology.memory_domains.begin(), topology.memory_domains.end(),
      [&](const auto& item) { return item.domain_id == id; });
  if (found == topology.memory_domains.end()) {
    throw EngineError("unknown Phase 6 memory domain: " + id);
  }
  return *found;
}

const DirectedLink& link_for(
    const Topology& topology,
    const std::string& source,
    const std::string& target) {
  const auto found = std::find_if(
      topology.links.begin(), topology.links.end(),
      [&](const auto& item) {
        return item.source_compute_id == source &&
               item.target_compute_id == target;
      });
  if (found == topology.links.end()) {
    throw EngineError(
        "missing directed Phase 6 path: " + source + " -> " + target);
  }
  return *found;
}

U128 reverse_arrival(
    const U128& destination_completion,
    const DirectedLink& link,
    const std::map<std::string, Clock>& clocks) {
  const Clock& source = clocks.at(link.bridge.source_clock_id);
  const Clock& target = clocks.at(link.bridge.target_clock_id);
  const U128 after_latency = checked_add(
      destination_completion, link.bridge.reverse_latency_fs,
      "Phase 6 reverse bridge latency");
  std::uint64_t cycle = source.ceil_edge(after_latency);
  if (link.bridge.ack_sync_cycles >
      std::numeric_limits<std::uint64_t>::max() - cycle) {
    throw EngineError("Phase 6 reverse synchronization cycle overflow");
  }
  cycle += link.bridge.ack_sync_cycles;
  const U128 result = source.edge_time(cycle);
  if (result <= destination_completion ||
      target.clock_id != link.bridge.target_clock_id) {
    throw EngineError("Phase 6 reverse crossing lacks progress");
  }
  return result;
}

bool contains_dependency(
    const Operation& operation, const std::string& id) {
  return std::binary_search(
      operation.dependencies.begin(), operation.dependencies.end(), id);
}

}  // namespace

std::string MultiDomainSchedulerV1::traffic_profile_digest(
    const TrafficPayloadProfile& profile) {
  std::ostringstream stream;
  put(stream, "phase6-traffic-payload-profile-v1");
  put(stream, profile.profile_id);
  put(stream, to_decimal(profile.activation_bytes));
  put(stream, to_decimal(profile.expert_bytes));
  put(stream, to_decimal(profile.immutable_read_bytes));
  put(stream, to_decimal(profile.mutable_object_bytes));
  put(stream, to_decimal(profile.compute_work));
  put(stream, fidelity_name(profile.fidelity));
  put(stream, "RANGE_UNKNOWN");
  return sha256_bytes(stream.str());
}

std::string MultiDomainSchedulerV1::phase5_action_set_digest(
    const std::vector<std::string>& action_ids) {
  std::vector<std::string> canonical = action_ids;
  std::sort(canonical.begin(), canonical.end());
  if (canonical.empty() ||
      std::any_of(canonical.begin(), canonical.end(), [](const auto& id) {
        return id.empty();
      }) ||
      std::adjacent_find(canonical.begin(), canonical.end()) !=
          canonical.end()) {
    throw EngineError("invalid canonical Phase 5 action membership set");
  }
  std::ostringstream stream;
  put(stream, "phase6-phase5-action-membership-v1");
  put(stream, std::to_string(canonical.size()));
  for (const auto& id : canonical) put(stream, id);
  return sha256_bytes(stream.str());
}

std::string MultiDomainSchedulerV1::phase5_compiled_action_id(
    const std::string& plan_digest,
    const std::size_t sequence,
    const phase5::CompiledAction& action) {
  const auto action_name = [](const phase5::ActionKind value) {
    switch (value) {
      case phase5::ActionKind::kHintBarrier: return "HINT_BARRIER";
      case phase5::ActionKind::kRouteBarrier: return "ROUTE_BARRIER";
      case phase5::ActionKind::kCleanEvict: return "CLEAN_EVICT";
      case phase5::ActionKind::kH2DLoad: return "H2D_LOAD";
      case phase5::ActionKind::kComputeAssignment:
        return "COMPUTE_ASSIGNMENT";
    }
    throw EngineError("invalid imported Phase 5 action kind");
  };
  const auto service_name = [](const phase4::ServiceClass value) {
    switch (value) {
      case phase4::ServiceClass::kCompute: return "COMPUTE";
      case phase4::ServiceClass::kMemory: return "MEMORY";
      case phase4::ServiceClass::kH2D: return "H2D";
      case phase4::ServiceClass::kD2H: return "D2H";
    }
    throw EngineError("invalid imported Phase 5 service class");
  };
  std::ostringstream stream;
  put(stream, "moe-phase5-compiled-action-v1");
  put(stream, plan_digest);
  put(stream, std::to_string(sequence));
  put(stream, action_name(action.kind));
  put(stream, action.expert.has_value() ? "1" : "0");
  if (action.expert.has_value()) {
    put(stream, std::to_string(action.expert->layer));
    put(stream, std::to_string(action.expert->expert));
  }
  put(stream, action.source_id);
  put(stream, std::to_string(action.dependencies.size()));
  for (const auto& dependency : action.dependencies) {
    put(stream, dependency);
  }
  put(stream, service_name(action.service_class));
  put(stream, to_decimal(action.work));
  put(stream, to_decimal(action.release_fs));
  put(stream, action.prefetch_load ? "1" : "0");
  return sha256_bytes(stream.str());
}

MultiDomainSchedulerV1::MultiDomainSchedulerV1(
    Topology topology, Program program)
    : topology_(std::move(topology)), program_(std::move(program)) {
  std::sort(
      topology_.compute_domains.begin(), topology_.compute_domains.end(),
      [](const auto& a, const auto& b) {
        return a.domain_id < b.domain_id;
      });
  std::sort(
      topology_.memory_domains.begin(), topology_.memory_domains.end(),
      [](const auto& a, const auto& b) {
        return a.domain_id < b.domain_id;
      });
  std::sort(
      topology_.links.begin(), topology_.links.end(),
      [](const auto& a, const auto& b) { return a.link_id < b.link_id; });
  std::sort(
      topology_.synthetic_alignments.begin(),
      topology_.synthetic_alignments.end(),
      [](const auto& a, const auto& b) {
        return a.clock_id < b.clock_id;
      });
  std::sort(
      program_.initial_objects.begin(), program_.initial_objects.end(),
      [](const auto& a, const auto& b) {
        return a.object_id < b.object_id;
      });
  std::sort(
      program_.routing.begin(), program_.routing.end(),
      [](const auto& a, const auto& b) {
        if (a.route_key < b.route_key) return true;
        if (b.route_key < a.route_key) return false;
        return a.demand_id < b.demand_id;
      });
  std::sort(
      program_.allowed_phase5_action_ids.begin(),
      program_.allowed_phase5_action_ids.end());
  for (auto& operation : program_.operations) {
    std::sort(operation.dependencies.begin(), operation.dependencies.end());
    std::sort(
        operation.source_phase5_action_ids.begin(),
        operation.source_phase5_action_ids.end());
  }
  std::sort(
      program_.operations.begin(), program_.operations.end(),
      [](const auto& a, const auto& b) { return a.key < b.key; });
  validate_and_compile();
  topology_digest_ = topology_digest();
  program_digest_ = program_digest();
  initialize_runtime();
}

void MultiDomainSchedulerV1::validate_and_compile() {
  const auto& authority = topology_.authority;
  if (topology_.topology_id.empty() ||
      authority.build_authority_sha256 != kPhase6BuildAuthoritySha256 ||
      authority.topology_contract_sha256 != kPhase6TopologyContractSha256 ||
      authority.cdc_contract_sha256 != kPhase6CdcContractSha256 ||
      authority.p2p_contract_sha256 != kPhase6P2pContractSha256 ||
      authority.uma_contract_sha256 != kPhase6UmaContractSha256 ||
      authority.checkpoint_contract_sha256 !=
          kPhase6CheckpointContractSha256 ||
      authority.claim_boundary_sha256 != kPhase6ClaimBoundarySha256 ||
      authority.phase5_ledger_sha256 != kPhase5LedgerSha256 ||
      authority.phase5_review_ledger_sha256 !=
          kPhase5ReviewLedgerSha256 ||
      authority.phase5_review_aggregate_sha256 !=
          kPhase5ReviewAggregateSha256) {
    throw EngineError("invalid Phase 6 exact build authority");
  }
  if (topology_.compute_domains.size() != 2 ||
      topology_.memory_domains.empty()) {
    throw EngineError("Phase 6 requires exactly two compute domains");
  }
  std::set<std::string> compute_ids;
  std::set<std::string> memory_ids;
  std::set<std::string> clock_ids;
  for (const auto& item : topology_.memory_domains) {
    if (item.domain_id.empty() || item.capacity_bytes == 0 ||
        !memory_ids.insert(item.domain_id).second) {
      throw EngineError("invalid Phase 6 memory domain");
    }
  }
  for (const auto& item : topology_.compute_domains) {
    item.clock.validate();
    if (item.domain_id.empty() || item.compute_lanes == 0 ||
        item.throughput_numerator_per_second == 0 ||
        item.throughput_denominator == 0 ||
        !compute_ids.insert(item.domain_id).second ||
        !clock_ids.insert(item.clock.clock_id).second ||
        !memory_ids.contains(item.memory_domain_id)) {
      throw EngineError("invalid Phase 6 compute domain");
    }
  }
  std::map<std::string, Clock> validation_clocks;
  for (const auto& item : topology_.compute_domains) {
    validation_clocks.emplace(item.clock.clock_id, item.clock);
  }
  if (topology_.mode == PlatformMode::kDiscreteP2p2Gpu) {
    if (topology_.memory_domains.size() != 2 ||
        topology_.links.size() != 2 || topology_.uma_fabric.has_value() ||
        topology_.compute_domains[0].memory_domain_id ==
            topology_.compute_domains[1].memory_domain_id) {
      throw EngineError("invalid discrete Phase 6 topology shape");
    }
    std::set<std::pair<std::string, std::string>> directions;
    std::map<std::string, bool> duplex_modes;
    std::set<std::string> link_ids;
    std::set<std::string> bridge_ids;
    std::set<std::string> topology_object_ids;
    for (const auto& item : topology_.links) {
      if (item.link_id.empty() || item.source_compute_id ==
              item.target_compute_id ||
          !compute_ids.contains(item.source_compute_id) ||
          !compute_ids.contains(item.target_compute_id) ||
          item.directional_lanes == 0 ||
          item.throughput_numerator_bytes_per_second == 0 ||
          item.throughput_denominator == 0 ||
          item.duplex_group.empty() ||
          item.bridge.bridge_id.empty() ||
          !link_ids.insert(item.link_id).second ||
          !bridge_ids.insert(item.bridge.bridge_id).second ||
          !topology_object_ids.insert(item.link_id).second ||
          !topology_object_ids.insert(item.bridge.bridge_id).second ||
          !directions.emplace(
              item.source_compute_id, item.target_compute_id).second ||
          (duplex_modes.contains(item.duplex_group) &&
           duplex_modes.at(item.duplex_group) != item.full_duplex)) {
        throw EngineError("invalid Phase 6 directed link");
      }
      duplex_modes[item.duplex_group] = item.full_duplex;
      item.bridge.validate(validation_clocks);
      if (item.bridge.source_clock_id !=
              compute(topology_, item.source_compute_id).clock.clock_id ||
          item.bridge.target_clock_id !=
              compute(topology_, item.target_compute_id).clock.clock_id ||
          (item.bridge.protocol != BridgeProtocol::kRequestAck &&
           item.bridge.protocol != BridgeProtocol::kCredit) ||
          (item.bridge.protocol == BridgeProtocol::kCredit &&
           (item.initial_credits == 0 ||
            item.initial_credits != item.bridge.queue_capacity)) ||
          (item.bridge.protocol == BridgeProtocol::kRequestAck &&
           item.initial_credits != 0)) {
        throw EngineError("invalid Phase 6 link CDC/credit contract");
      }
    }
    const auto& a = topology_.compute_domains[0].domain_id;
    const auto& b = topology_.compute_domains[1].domain_id;
    if (!directions.contains({a, b}) || !directions.contains({b, a})) {
      throw EngineError("Phase 6 reverse directed path is missing");
    }
    if (topology_.links[0].duplex_group !=
            topology_.links[1].duplex_group ||
        topology_.links[0].full_duplex !=
            topology_.links[1].full_duplex) {
      throw EngineError(
          "reverse links must share exact duplex group and mode");
    }
  } else if (topology_.mode == PlatformMode::kCoherentUma2Compute) {
    if (topology_.memory_domains.size() != 1 || !topology_.links.empty() ||
        !topology_.uma_fabric.has_value()) {
      throw EngineError("invalid coherent UMA Phase 6 topology shape");
    }
    const auto& fabric = *topology_.uma_fabric;
    fabric.fabric_clock.validate();
    if (fabric.fabric_id.empty() ||
        fabric.memory_domain_id != topology_.memory_domains[0].domain_id ||
        fabric.lanes == 0 ||
        fabric.throughput_numerator_bytes_per_second == 0 ||
        fabric.throughput_denominator == 0 ||
        fabric.queue_capacity == 0 ||
        (fabric.protocol != BridgeProtocol::kRequestAck &&
         fabric.protocol != BridgeProtocol::kCredit) ||
        (fabric.protocol == BridgeProtocol::kCredit &&
         (fabric.initial_credits == 0 ||
          fabric.initial_credits != fabric.queue_capacity)) ||
        (fabric.protocol == BridgeProtocol::kRequestAck &&
         fabric.initial_credits != 0) ||
        clock_ids.contains(fabric.fabric_clock.clock_id)) {
      throw EngineError("invalid coherent UMA fabric");
    }
    for (const auto& item : topology_.compute_domains) {
      if (item.memory_domain_id != fabric.memory_domain_id) {
        throw EngineError("UMA compute domain must alias shared memory");
      }
    }
  } else {
    throw EngineError("invalid Phase 6 platform mode");
  }

  std::set<std::string> required_alignment_clocks = clock_ids;
  if (topology_.uma_fabric.has_value()) {
    required_alignment_clocks.insert(
        topology_.uma_fabric->fabric_clock.clock_id);
  }
  std::set<std::string> actual_alignment_clocks;
  for (const auto& alignment : topology_.synthetic_alignments) {
    if (!actual_alignment_clocks.insert(alignment.clock_id).second ||
        alignment.calibration_method != "SIMULATOR_EXACT_SYNTHETIC" ||
        alignment.residual_error_fs != 0 ||
        alignment.confidence_interval_95_lower_fs != 0 ||
        alignment.confidence_interval_95_upper_fs != 0 ||
        alignment.quality != "CYCLE_GRADE" ||
        alignment.evidence_label !=
            "SIMULATOR_INTERNAL_NOT_HARDWARE_ALIGNMENT" ||
        alignment.valid_end_fs <= alignment.valid_start_fs) {
      throw EngineError("invalid Phase 6 synthetic clock alignment");
    }
  }
  if (actual_alignment_clocks != required_alignment_clocks) {
    throw EngineError("Phase 6 synthetic clock alignment set mismatch");
  }
  validate_alignment_time(U128{0});

  if (program_.program_id.empty() ||
      program_.phase5_ledger_sha256 != kPhase5LedgerSha256 ||
      !is_hex_hash(program_.phase5_plan_digest) ||
      program_.phase5_action_digest !=
          phase5_action_set_digest(program_.allowed_phase5_action_ids) ||
      program_.payload_profile.profile_id.empty() ||
      program_.payload_profile.activation_bytes == 0 ||
      program_.payload_profile.expert_bytes == 0 ||
      program_.payload_profile.immutable_read_bytes == 0 ||
      program_.payload_profile.mutable_object_bytes == 0 ||
      program_.payload_profile.compute_work == 0 ||
      (program_.payload_profile.fidelity != Fidelity::kFunctionalOnly &&
       program_.payload_profile.fidelity !=
           Fidelity::kAnalyticFirstOrder) ||
      program_.payload_profile.range_status != RangeStatus::kRangeUnknown ||
      program_.payload_profile.profile_sha256 !=
          traffic_profile_digest(program_.payload_profile) ||
      program_.operations.size() > kMaxOperations) {
    throw EngineError("invalid Phase 6 program authority or payload");
  }
  std::set<std::string> verified_phase5_action_ids;
  for (std::size_t index = 0; index < program_.phase5_actions.size();
       ++index) {
    const auto& action = program_.phase5_actions[index];
    if (action.action_id != phase5_compiled_action_id(
                                program_.phase5_plan_digest, index, action) ||
        action.source_id.empty() || action.work == 0 ||
        !std::is_sorted(
            action.dependencies.begin(), action.dependencies.end()) ||
        std::adjacent_find(
            action.dependencies.begin(), action.dependencies.end()) !=
            action.dependencies.end() ||
        !verified_phase5_action_ids.insert(action.action_id).second) {
      throw EngineError("invalid imported Phase 5 compiled action proof");
    }
  }
  const std::set<std::string> allowed_phase5_action_ids(
      program_.allowed_phase5_action_ids.begin(),
      program_.allowed_phase5_action_ids.end());
  if (verified_phase5_action_ids != allowed_phase5_action_ids) {
    throw EngineError(
        "Phase 5 action membership differs from verified compiled actions");
  }

  std::map<std::string, const InitialObject*> initial_by_id;
  std::map<phase5::ExpertKey, const InitialObject*> expert_objects;
  std::map<std::string, U128> initial_occupancy;
  for (const auto& item : program_.initial_objects) {
    static_cast<void>(coherence_name(item.coherence_state));
    if (item.object_id.empty() || item.bytes == 0 ||
        !initial_by_id.emplace(item.object_id, &item).second) {
      throw EngineError("invalid Phase 6 initial object");
    }
    if (item.expert.has_value()) {
      if (!item.immutable || !is_hex_hash(item.content_sha256) ||
          !expert_objects.emplace(*item.expert, &item).second) {
        throw EngineError("invalid or duplicate expert object identity");
      }
    } else if (!item.content_sha256.empty() &&
               !is_hex_hash(item.content_sha256)) {
      throw EngineError("invalid optional object content hash");
    }
    if (topology_.mode == PlatformMode::kDiscreteP2p2Gpu) {
      if (item.locations.empty()) {
        throw EngineError("discrete object lacks a location");
      }
      for (const auto& location : item.locations) {
        const auto& domain = memory(topology_, location);
        initial_occupancy[location] = checked_add(
            initial_occupancy[location], item.bytes,
            "Phase 6 initial private memory occupancy");
        if (initial_occupancy[location] > domain.capacity_bytes) {
          throw EngineError("Phase 6 per-domain initial capacity overflow");
        }
      }
    } else {
      if (item.expert.has_value()) {
        throw EngineError(
            "generic coherent UMA fixture cannot claim MoE expert objects");
      }
      for (const auto& alias : item.locations) {
        if (!compute_ids.contains(alias)) {
          throw EngineError("invalid Phase 6 UMA alias");
        }
      }
      initial_occupancy[topology_.memory_domains[0].domain_id] = checked_add(
          initial_occupancy[topology_.memory_domains[0].domain_id],
          item.bytes, "Phase 6 UMA initial capacity");
      if (initial_occupancy[topology_.memory_domains[0].domain_id] >
          topology_.memory_domains[0].capacity_bytes) {
        throw EngineError("Phase 6 UMA capacity overflow");
      }
      if (item.immutable && item.coherence_state == CoherenceState::kModified) {
        throw EngineError("immutable UMA object cannot be modified");
      }
      if (item.coherence_state == CoherenceState::kModified &&
          (!item.owner.has_value() ||
           !compute_ids.contains(*item.owner))) {
        throw EngineError("modified UMA object requires an owner");
      }
      const ObjectRuntime runtime{
          item.bytes, item.immutable, item.expert, item.content_sha256,
          item.locations, item.pins, item.version, item.coherence_state,
          item.owner, item.sharers};
      validate_object_invariants(runtime, true);
    }
  }

  std::map<std::string, const Operation*> by_id;
  std::set<EventKey> keys;
  for (const auto& operation : program_.operations) {
    static_cast<void>(kind_name(operation.kind));
    const bool valid_mode_kind =
        topology_.mode == PlatformMode::kDiscreteP2p2Gpu
            ? !is_uma(operation.kind)
            : is_uma(operation.kind);
    std::set<std::string> action_ids(
        operation.source_phase5_action_ids.begin(),
        operation.source_phase5_action_ids.end());
    if (operation.key.event_id.empty() ||
        operation.key.component_id.empty() ||
        operation.key.event_priority !=
            trace_priority(operation.kind, TraceKind::kStart) ||
        operation.source_phase5_action_ids.empty() ||
        action_ids.size() != operation.source_phase5_action_ids.size() ||
        !std::all_of(
            action_ids.begin(), action_ids.end(), [&](const auto& action) {
              return std::binary_search(
                  program_.allowed_phase5_action_ids.begin(),
                  program_.allowed_phase5_action_ids.end(), action);
            }) ||
        !valid_mode_kind ||
        !keys.insert(operation.key).second ||
        !by_id.emplace(operation.key.event_id, &operation).second) {
      throw EngineError("invalid or duplicate Phase 6 operation");
    }
    compute(topology_, operation.source_domain_id);
    compute(topology_, operation.target_domain_id);
    if (is_transfer(operation.kind)) {
      if (operation.source_domain_id == operation.target_domain_id ||
          operation.bytes == 0) {
        throw EngineError("invalid Phase 6 transfer operation");
      }
      static_cast<void>(link_for(
          topology_, operation.source_domain_id,
          operation.target_domain_id));
    } else if (operation.source_domain_id !=
               operation.target_domain_id) {
      throw EngineError("non-transfer Phase 6 operation crosses domains");
    }
    if (operation.kind == OperationKind::kExpertCompute &&
        (operation.work != program_.payload_profile.compute_work ||
         !operation.expert.has_value() || operation.object_id.empty() ||
         !initial_by_id.contains(operation.object_id) ||
         initial_by_id.at(operation.object_id)->bytes !=
             program_.payload_profile.expert_bytes ||
         initial_by_id.at(operation.object_id)->expert !=
             operation.expert)) {
      throw EngineError("invalid Phase 6 expert compute");
    }
    if ((operation.kind == OperationKind::kActivationDispatch ||
         operation.kind == OperationKind::kActivationReturn) &&
        operation.bytes != program_.payload_profile.activation_bytes) {
      throw EngineError("activation bytes do not match payload profile");
    }
    if ((operation.kind == OperationKind::kExpertReplicate ||
         operation.kind == OperationKind::kExpertMove) &&
        (operation.object_id.empty() ||
         !initial_by_id.contains(operation.object_id) ||
         operation.bytes != initial_by_id.at(operation.object_id)->bytes ||
         operation.bytes != program_.payload_profile.expert_bytes ||
         !operation.expert.has_value() ||
         initial_by_id.at(operation.object_id)->expert !=
             operation.expert)) {
      throw EngineError("invalid whole-expert transfer");
    }
    if (operation.kind == OperationKind::kExpertMove &&
        initial_by_id.at(operation.object_id)->pins != 0) {
      throw EngineError("pinned expert move is hard-rejected");
    }
    if (is_uma(operation.kind) &&
        (operation.object_id.empty() ||
         !initial_by_id.contains(operation.object_id) ||
         operation.bytes == 0)) {
      throw EngineError("invalid Phase 6 UMA operation object");
    }
    if (operation.kind == OperationKind::kUmaImmutableRead &&
        operation.bytes !=
            program_.payload_profile.immutable_read_bytes) {
      throw EngineError("immutable read bytes do not match payload profile");
    }
    if ((operation.kind == OperationKind::kUmaMutableAcquireWrite ||
         operation.kind == OperationKind::kUmaMutableRelease ||
         operation.kind == OperationKind::kUmaMutableRead) &&
        operation.bytes != program_.payload_profile.mutable_object_bytes) {
      throw EngineError("mutable bytes do not match payload profile");
    }
  }
  for (const auto& operation : program_.operations) {
    std::set<std::string> dependencies;
    for (const auto& dependency : operation.dependencies) {
      if (dependency == operation.key.event_id ||
          !by_id.contains(dependency) ||
          !dependencies.insert(dependency).second) {
        throw EngineError("invalid Phase 6 dependency");
      }
    }
  }
  std::map<std::string, std::uint64_t> indegree;
  std::map<std::string, std::vector<std::string>> outgoing;
  for (const auto& operation : program_.operations) {
    indegree[operation.key.event_id] = operation.dependencies.size();
    for (const auto& dependency : operation.dependencies) {
      outgoing[dependency].push_back(operation.key.event_id);
    }
  }
  std::set<std::string> dag_ready;
  for (const auto& [id, degree] : indegree) {
    if (degree == 0) dag_ready.insert(id);
  }
  std::size_t visited = 0;
  while (!dag_ready.empty()) {
    const std::string id = *dag_ready.begin();
    dag_ready.erase(dag_ready.begin());
    ++visited;
    for (const auto& dependent : outgoing[id]) {
      auto& degree = indegree.at(dependent);
      --degree;
      if (degree == 0) dag_ready.insert(dependent);
    }
  }
  if (visited != program_.operations.size()) {
    throw EngineError("Phase 6 dependency cycle");
  }

  if (topology_.mode == PlatformMode::kDiscreteP2p2Gpu) {
    std::map<std::string, const Operation*> compute_ops;
    std::map<std::string, const Operation*> dispatch_ops;
    std::map<std::string, const Operation*> return_ops;
    std::map<std::string, const Operation*> combine_ops;
    for (const auto& operation : program_.operations) {
      const std::string assignment = operation.expert.has_value()
          ? assignment_name(operation.demand_id, *operation.expert)
          : "";
      auto insert_unique = [&](auto& values, const std::string& id) {
        if (!values.emplace(id, &operation).second) {
          throw EngineError("duplicate Phase 6 routing-chain operation");
        }
      };
      if (operation.kind == OperationKind::kExpertCompute) {
        insert_unique(compute_ops, assignment);
      } else if (operation.kind == OperationKind::kActivationDispatch) {
        if (!operation.expert.has_value()) {
          throw EngineError("dispatch lacks expert identity");
        }
        insert_unique(dispatch_ops, assignment);
      } else if (operation.kind == OperationKind::kActivationReturn) {
        if (!operation.expert.has_value()) {
          throw EngineError("return lacks expert identity");
        }
        insert_unique(return_ops, assignment);
      } else if (operation.kind == OperationKind::kTokenCombine) {
        if (operation.expert.has_value()) {
          throw EngineError("combine must not carry one expert");
        }
        insert_unique(combine_ops, operation.demand_id);
      }
    }
    std::set<std::string> demand_ids;
    std::size_t expected_remote_assignments = 0;
    for (const auto& route : program_.routing) {
      const auto same_token_identity =
          [&](const Operation& operation) {
            return operation.key.request_id == route.route_key.request_id &&
                   operation.key.token_index == route.route_key.token_index &&
                   operation.key.layer_index == route.route_key.layer_index;
          };
      if (route.demand_id.empty() ||
          !demand_ids.insert(route.demand_id).second ||
          route.route_key.event_priority != 100 ||
          !route.route_key.request_id.has_value() ||
          route.route_key.request_id->empty() ||
          !route.route_key.token_index.has_value() ||
          *route.route_key.token_index ==
              std::numeric_limits<std::uint64_t>::max() ||
          !route.route_key.layer_index.has_value() ||
          *route.route_key.layer_index ==
              std::numeric_limits<std::uint32_t>::max() ||
          route.route_key.component_id.empty() ||
          route.route_key.event_id.empty() ||
          route.top_k == 0 ||
          route.selected_experts.size() != route.top_k ||
          route.assigned_compute_domains.size() != route.top_k ||
          route.routing_provenance.empty() ||
          !compute_ids.contains(route.token_owner_compute_domain_id)) {
        throw EngineError("invalid exact token routing binding");
      }
      std::set<phase5::ExpertKey> selected(
          route.selected_experts.begin(), route.selected_experts.end());
      if (selected.size() != route.selected_experts.size()) {
        throw EngineError("duplicate selected expert");
      }
      const auto combine_it = combine_ops.find(route.demand_id);
      if (combine_it == combine_ops.end() ||
          !same_token_identity(*combine_it->second) ||
          combine_it->second->source_domain_id !=
              route.token_owner_compute_domain_id) {
        throw EngineError("routing demand lacks exact combine");
      }
      std::set<std::string> expected_combine_dependencies;
      for (const auto& expert : route.selected_experts) {
        if (expert.layer != *route.route_key.layer_index ||
            !expert_objects.contains(expert)) {
          throw EngineError(
              "selected ExpertKey lacks exact route-layer object binding");
        }
        const auto domain_it = route.assigned_compute_domains.find(expert);
        if (domain_it == route.assigned_compute_domains.end() ||
            !compute_ids.contains(domain_it->second)) {
          throw EngineError("selected expert lacks exact domain assignment");
        }
        const std::string assignment =
            assignment_name(route.demand_id, expert);
        const auto compute_it = compute_ops.find(assignment);
        if (compute_it == compute_ops.end() ||
            !same_token_identity(*compute_it->second) ||
            compute_it->second->target_domain_id != domain_it->second) {
          throw EngineError("selected expert lacks exact compute assignment");
        }
        if (domain_it->second == route.token_owner_compute_domain_id) {
          if (dispatch_ops.contains(assignment) ||
              return_ops.contains(assignment)) {
            throw EngineError("local expert has phantom transfer");
          }
          expected_combine_dependencies.insert(
              compute_it->second->key.event_id);
        } else {
          ++expected_remote_assignments;
          const auto dispatch_it = dispatch_ops.find(assignment);
          const auto return_it = return_ops.find(assignment);
          if (dispatch_it == dispatch_ops.end() ||
              return_it == return_ops.end() ||
              !same_token_identity(*dispatch_it->second) ||
              !same_token_identity(*return_it->second) ||
              dispatch_it->second->source_domain_id !=
                  route.token_owner_compute_domain_id ||
              dispatch_it->second->target_domain_id != domain_it->second ||
              compute_it->second->source_domain_id != domain_it->second ||
              !contains_dependency(
                  *compute_it->second,
                  dispatch_it->second->key.event_id) ||
              return_it->second->source_domain_id != domain_it->second ||
              return_it->second->target_domain_id !=
                  route.token_owner_compute_domain_id ||
              !contains_dependency(
                  *return_it->second, compute_it->second->key.event_id)) {
            throw EngineError("invalid remote dispatch-compute-return chain");
          }
          expected_combine_dependencies.insert(
              return_it->second->key.event_id);
        }
      }
      const std::set<std::string> actual_combine_dependencies(
          combine_it->second->dependencies.begin(),
          combine_it->second->dependencies.end());
      if (actual_combine_dependencies != expected_combine_dependencies) {
        throw EngineError("combine dependency conservation mismatch");
      }
    }
    if (compute_ops.size() !=
        std::accumulate(
            program_.routing.begin(), program_.routing.end(),
            std::size_t{0},
            [](const std::size_t total, const auto& route) {
              return total + route.selected_experts.size();
            }) ||
        combine_ops.size() != program_.routing.size() ||
        dispatch_ops.size() != expected_remote_assignments ||
        return_ops.size() != expected_remote_assignments) {
      throw EngineError("routing assignment conservation mismatch");
    }
  } else if (!program_.routing.empty()) {
    throw EngineError("UMA functional program cannot carry routing bindings");
  }
}

void MultiDomainSchedulerV1::initialize_runtime() {
  result_.fidelity = program_.payload_profile.fidelity;
  result_.execution_claim =
      topology_.mode == PlatformMode::kCoherentUma2Compute
          ? "GENERIC_COHERENT_UMA_FUNCTIONAL_ONLY_NOT_MOE_EXECUTION"
          : "CPU_SYNTHETIC_DISCRETE_P2P_FUNCTIONAL_MODEL";
  for (std::size_t index = 0; index < program_.operations.size(); ++index) {
    const auto& operation = program_.operations[index];
    states_.emplace(operation.key.event_id, OperationState::kPending);
    operation_index_.emplace(operation.key.event_id, index);
    remaining_dependencies_.emplace(
        operation.key.event_id, operation.dependencies.size());
    future_arrivals_.emplace(operation.key, index);
    for (const auto& dependency : operation.dependencies) {
      dependents_[dependency].push_back(index);
    }
  }
  for (const auto& item : topology_.compute_domains) {
    clocks_.emplace(item.clock.clock_id, item.clock);
    resources_.emplace(
        "compute:" + item.domain_id,
        ResourceState{item.compute_lanes, 0});
    memory_occupancy_[item.memory_domain_id] = 0;
  }
  if (topology_.mode == PlatformMode::kDiscreteP2p2Gpu) {
    for (const auto& item : topology_.links) {
      resources_.emplace(
          "link:" + item.link_id,
          ResourceState{item.directional_lanes, 0});
      const std::string duplex_id =
          "duplex:" + item.duplex_group +
          (item.full_duplex
               ? ":" + item.source_compute_id + "->" +
                     item.target_compute_id
               : "");
      resources_.try_emplace(duplex_id, ResourceState{1, 0});
      links_.emplace(
          item.link_id,
          LinkRuntime{
              0, item.initial_credits, 0, 0, 0, 0});
    }
  } else {
    const auto& fabric = *topology_.uma_fabric;
    clocks_.emplace(fabric.fabric_clock.clock_id, fabric.fabric_clock);
    resources_.emplace(
        "uma:" + fabric.fabric_id, ResourceState{fabric.lanes, 0});
    links_.emplace(
        "uma:" + fabric.fabric_id,
        LinkRuntime{0, fabric.initial_credits, 0, 0, 0, 0});
  }
  for (const auto& item : program_.initial_objects) {
    ObjectRuntime object{
        item.bytes, item.immutable, item.expert, item.content_sha256,
        item.locations, item.pins, item.version, item.coherence_state,
        item.owner, item.sharers};
    objects_.emplace(item.object_id, std::move(object));
    if (topology_.mode == PlatformMode::kDiscreteP2p2Gpu) {
      for (const auto& location : item.locations) {
        memory_occupancy_[location] = checked_add(
            memory_occupancy_[location], item.bytes,
            "Phase 6 private initial occupancy");
      }
    } else {
      const std::string& shared = topology_.memory_domains[0].domain_id;
      memory_occupancy_[shared] = checked_add(
          memory_occupancy_[shared], item.bytes,
          "Phase 6 shared initial occupancy");
    }
  }
  update_clocks();
  activate_arrivals();
}

void MultiDomainSchedulerV1::activate_arrivals() {
  while (!future_arrivals_.empty() &&
         future_arrivals_.begin()->first.time_fs <= global_time_fs_) {
    const std::size_t index = future_arrivals_.begin()->second;
    future_arrivals_.erase(future_arrivals_.begin());
    const Operation& operation = program_.operations[index];
    if (remaining_dependencies_.at(operation.key.event_id) == 0) {
      ready_.emplace(operation.key, index);
      ++result_.metrics.ready_queue_pushes;
    }
  }
}

U128 MultiDomainSchedulerV1::dependency_ready(
    const Operation& operation) const {
  U128 ready = operation.key.time_fs;
  for (const auto& dependency : operation.dependencies) {
    const auto found = completion_times_.find(dependency);
    if (found == completion_times_.end()) {
      throw EngineError("Phase 6 dependency is not complete");
    }
    ready = std::max(ready, found->second);
  }
  return ready;
}

std::vector<std::string> MultiDomainSchedulerV1::required_resources(
    const Operation& operation) const {
  std::vector<std::string> result;
  if (is_transfer(operation.kind)) {
    const auto& link = link_for(
        topology_, operation.source_domain_id,
        operation.target_domain_id);
    result.push_back("link:" + link.link_id);
    result.push_back(
        "duplex:" + link.duplex_group +
        (link.full_duplex
             ? ":" + link.source_compute_id + "->" +
                   link.target_compute_id
             : ""));
  } else if (is_uma(operation.kind)) {
    result.push_back("compute:" + operation.source_domain_id);
    result.push_back("uma:" + topology_.uma_fabric->fabric_id);
  } else {
    result.push_back("compute:" + operation.source_domain_id);
  }
  std::sort(result.begin(), result.end());
  if (std::adjacent_find(result.begin(), result.end()) != result.end()) {
    throw EngineError("duplicate Phase 6 atomic resource");
  }
  return result;
}

void MultiDomainSchedulerV1::validate_runtime_operation(
    const Operation& operation) const {
  if (operation.kind == OperationKind::kExpertCompute) {
    if (operation.object_id.empty() ||
        !objects_.contains(operation.object_id)) {
      throw EngineError("expert compute lacks bound expert object");
    }
    const auto& domain =
        compute(topology_, operation.source_domain_id).memory_domain_id;
    if (!objects_.at(operation.object_id).locations.contains(domain)) {
      throw EngineError("expert is not resident at assigned compute domain");
    }
    if (objects_.at(operation.object_id).expert != operation.expert ||
        objects_.at(operation.object_id).pins ==
            std::numeric_limits<std::uint32_t>::max()) {
      throw EngineError("expert identity or pin admission failure");
    }
  }
  if (operation.kind == OperationKind::kExpertReplicate ||
      operation.kind == OperationKind::kExpertMove) {
    const auto& object = objects_.at(operation.object_id);
    const std::string& source_memory =
        compute(topology_, operation.source_domain_id).memory_domain_id;
    const std::string& target_memory =
        compute(topology_, operation.target_domain_id).memory_domain_id;
    if (!object.locations.contains(source_memory) ||
        object.locations.contains(target_memory)) {
      throw EngineError("invalid expert placement source/destination");
    }
    if (operation.kind == OperationKind::kExpertMove && object.pins != 0) {
      throw EngineError("pinned expert move is hard-rejected");
    }
    if (object.expert != operation.expert ||
        inflight_destination_reservations_.contains(
            {operation.object_id, target_memory})) {
      throw EngineError(
          "expert identity mismatch or duplicate destination reservation");
    }
    const U128 target_after = checked_add(
        memory_occupancy_.at(target_memory), object.bytes,
        "Phase 6 destination capacity");
    if (target_after > memory(topology_, target_memory).capacity_bytes) {
      throw EngineError("Phase 6 per-domain capacity overflow");
    }
  }
  if (is_uma(operation.kind)) {
    const auto& object = objects_.at(operation.object_id);
    validate_object_invariants(object, true);
    if (uma_object_reservations_.contains(operation.object_id)) {
      throw EngineError("concurrent same-object UMA operation rejected");
    }
    if (operation.bytes != object.bytes) {
      throw EngineError("UMA access must bind whole synthetic object bytes");
    }
    if (operation.kind == OperationKind::kUmaImmutableRead) {
      if (!object.immutable) {
        throw EngineError("mutable object used by immutable read");
      }
    } else {
      if (object.immutable) {
        throw EngineError("immutable UMA write/read contract violation");
      }
      if (operation.expected_version != object.version) {
        throw EngineError("stale UMA version");
      }
      if (operation.kind == OperationKind::kUmaMutableAcquireWrite) {
        if (object.coherence_state == CoherenceState::kModified &&
            object.owner != operation.source_domain_id) {
          throw EngineError("UMA write conflicts with modified owner");
        }
        if (object.version == std::numeric_limits<std::uint64_t>::max() ||
            operation.output_version != object.version + 1) {
          throw EngineError("UMA write version is not monotonic");
        }
      } else if (operation.kind == OperationKind::kUmaMutableRelease) {
        if (object.coherence_state != CoherenceState::kModified ||
            object.owner != operation.source_domain_id ||
            operation.output_version != object.version) {
          throw EngineError("invalid UMA release");
        }
      } else if (operation.kind == OperationKind::kUmaMutableRead &&
                 object.coherence_state == CoherenceState::kModified &&
                 object.owner != operation.source_domain_id) {
        throw EngineError("UMA read cannot observe unreleased modified data");
      }
    }
  }
}

bool MultiDomainSchedulerV1::can_admit(
    const Operation& operation,
    const std::vector<std::string>& resource_ids) const {
  for (const auto& id : resource_ids) {
    const auto& resource = resources_.at(id);
    if (resource.occupancy >= resource.capacity) return false;
  }
  if (operation.kind == OperationKind::kExpertReplicate ||
      operation.kind == OperationKind::kExpertMove) {
    const std::string target_memory =
        compute(topology_, operation.target_domain_id).memory_domain_id;
    if (inflight_destination_reservations_.contains(
            {operation.object_id, target_memory})) {
      return false;
    }
  }
  if (is_uma(operation.kind) &&
      uma_object_reservations_.contains(operation.object_id)) {
    return false;
  }
  if (is_transfer(operation.kind)) {
    const auto& link = link_for(
        topology_, operation.source_domain_id,
        operation.target_domain_id);
    const auto& runtime = links_.at(link.link_id);
    if (runtime.queue_occupancy >= link.bridge.queue_capacity ||
        (link.bridge.protocol == BridgeProtocol::kCredit &&
         runtime.credits_available == 0)) {
      return false;
    }
  } else if (is_uma(operation.kind)) {
    const auto& fabric = *topology_.uma_fabric;
    const auto& runtime = links_.at("uma:" + fabric.fabric_id);
    if (runtime.queue_occupancy >= fabric.queue_capacity ||
        (fabric.protocol == BridgeProtocol::kCredit &&
         runtime.credits_available == 0)) {
      return false;
    }
  }
  return true;
}

U128 MultiDomainSchedulerV1::service_duration(
    const U128& work,
    const U128& throughput_numerator,
    const U128& throughput_denominator,
    const U128& setup) const {
  if (work == 0 || throughput_numerator == 0 ||
      throughput_denominator == 0) {
    throw EngineError("invalid Phase 6 service profile");
  }
  const cpp_int numerator =
      cpp_int{work} * cpp_int{kFsPerSecond} *
      cpp_int{throughput_denominator};
  const cpp_int denominator{throughput_numerator};
  const cpp_int transfer = (numerator + denominator - 1) / denominator;
  const U128 result = checked_u128(
      cpp_int{setup} + transfer, "Phase 6 service duration");
  if (result == 0) {
    throw EngineError("Phase 6 service lacks positive progress");
  }
  return result;
}

std::tuple<U128, U128, U128> MultiDomainSchedulerV1::timing(
    const Operation& operation, const U128& start) const {
  if (is_transfer(operation.kind)) {
    const auto& link = link_for(
        topology_, operation.source_domain_id,
        operation.target_domain_id);
    const U128 raw = checked_add(
        start,
        service_duration(
            operation.bytes,
            link.throughput_numerator_bytes_per_second,
            link.throughput_denominator, link.setup_latency_fs),
        "Phase 6 P2P forward service");
    const U128 visible = link.bridge.arrival(raw, clocks_);
    const U128 acknowledge =
        reverse_arrival(visible, link, clocks_);
    return {visible, acknowledge, acknowledge};
  }
  if (is_uma(operation.kind)) {
    const auto& fabric = *topology_.uma_fabric;
    const auto& source_clock =
        clocks_.at(compute(topology_, operation.source_domain_id).clock.clock_id);
    const auto& fabric_clock = clocks_.at(fabric.fabric_clock.clock_id);
    const U128 raw = checked_add(
        start,
        service_duration(
            operation.bytes,
            fabric.throughput_numerator_bytes_per_second,
            fabric.throughput_denominator, fabric.setup_latency_fs),
        "Phase 6 UMA service");
    const U128 after_forward = checked_add(
        raw, fabric.forward_latency_fs, "Phase 6 UMA forward latency");
    std::uint64_t forward_cycle = fabric_clock.ceil_edge(after_forward);
    if (fabric.receiver_sync_cycles >
        std::numeric_limits<std::uint64_t>::max() - forward_cycle) {
      throw EngineError("Phase 6 UMA forward cycle overflow");
    }
    forward_cycle += fabric.receiver_sync_cycles;
    const U128 visible = fabric_clock.edge_time(forward_cycle);
    const U128 after_reverse = checked_add(
        visible, fabric.reverse_latency_fs,
        "Phase 6 UMA reverse latency");
    std::uint64_t reverse_cycle = source_clock.ceil_edge(after_reverse);
    if (fabric.ack_sync_cycles >
        std::numeric_limits<std::uint64_t>::max() - reverse_cycle) {
      throw EngineError("Phase 6 UMA reverse cycle overflow");
    }
    reverse_cycle += fabric.ack_sync_cycles;
    const U128 acknowledge = source_clock.edge_time(reverse_cycle);
    if (visible <= start || acknowledge <= visible) {
      throw EngineError("Phase 6 UMA crossing lacks positive progress");
    }
    return {visible, acknowledge, acknowledge};
  }
  const auto& domain = compute(topology_, operation.source_domain_id);
  const U128 work = operation.kind == OperationKind::kExpertCompute
      ? operation.work
      : U128{1};
  const U128 raw = checked_add(
      start,
      service_duration(
          work, domain.throughput_numerator_per_second,
          domain.throughput_denominator, domain.setup_latency_fs),
      "Phase 6 compute completion");
  const Clock& clock = clocks_.at(domain.clock.clock_id);
  const U128 completion = clock.edge_time(clock.ceil_edge(raw));
  if (completion <= start) {
    throw EngineError("Phase 6 compute lacks positive progress");
  }
  return {completion, completion, completion};
}

EventKey MultiDomainSchedulerV1::trace_key(
    const Operation& operation,
    const TraceKind kind,
    const U128& time) const {
  EventKey key = operation.key;
  key.time_fs = time;
  key.event_priority = trace_priority(operation.kind, kind);
  key.event_id =
      sha256_bytes(
          "phase6-event-v1:" + topology_digest() + ":" +
          program_digest() + ":" + operation.key.event_id + ":" +
          trace_name(kind));
  return key;
}

void MultiDomainSchedulerV1::apply_visibility(
    const Operation& operation) {
  ObjectRuntime* object_pointer = nullptr;
  std::optional<ObjectRuntime> next_object;
  std::optional<std::string> source_memory;
  std::optional<std::string> target_memory;
  if (operation.kind == OperationKind::kExpertReplicate ||
      operation.kind == OperationKind::kExpertMove) {
    auto& object = objects_.at(operation.object_id);
    object_pointer = &object;
    source_memory =
        compute(topology_, operation.source_domain_id).memory_domain_id;
    target_memory =
        compute(topology_, operation.target_domain_id).memory_domain_id;
    if (!inflight_destination_reservations_.contains(
            {operation.object_id, *target_memory}) ||
        object.locations.contains(*target_memory) ||
        memory_occupancy_.at(*target_memory) >
            memory(topology_, *target_memory).capacity_bytes) {
      throw EngineError("Phase 6 transfer commit capacity overflow");
    }
    next_object = object;
    if (!next_object->locations.insert(*target_memory).second) {
      throw EngineError("destination visibility insertion failed");
    }
    if (operation.kind == OperationKind::kExpertMove) {
      if (next_object->pins != 0 ||
          !next_object->locations.contains(*source_memory) ||
          memory_occupancy_.at(*source_memory) < next_object->bytes) {
        throw EngineError("invalid atomic Phase 6 move commit");
      }
      next_object->locations.erase(*source_memory);
    }
  } else if (operation.kind == OperationKind::kUmaMutableAcquireWrite) {
    auto& object = objects_.at(operation.object_id);
    object_pointer = &object;
    next_object = object;
    next_object->version = operation.output_version;
    next_object->coherence_state = CoherenceState::kModified;
    next_object->owner = operation.source_domain_id;
    next_object->sharers.clear();
    validate_object_invariants(*next_object, true);
  } else if (operation.kind == OperationKind::kUmaMutableRelease) {
    auto& object = objects_.at(operation.object_id);
    object_pointer = &object;
    next_object = object;
    next_object->coherence_state = CoherenceState::kShared;
    next_object->owner.reset();
    next_object->sharers.insert(operation.source_domain_id);
    validate_object_invariants(*next_object, true);
  } else if (operation.kind == OperationKind::kUmaMutableRead) {
    auto& object = objects_.at(operation.object_id);
    object_pointer = &object;
    next_object = object;
    if (next_object->coherence_state == CoherenceState::kUncached) {
      next_object->coherence_state = CoherenceState::kShared;
    }
    if (next_object->coherence_state == CoherenceState::kShared) {
      next_object->sharers.insert(operation.source_domain_id);
    }
    validate_object_invariants(*next_object, true);
  } else if (operation.kind == OperationKind::kUmaImmutableRead) {
    auto& object = objects_.at(operation.object_id);
    object_pointer = &object;
    next_object = object;
    next_object->locations.insert(operation.source_domain_id);
    validate_object_invariants(*next_object, true);
  }

  LinkRuntime* runtime_pointer = nullptr;
  if (is_transfer(operation.kind)) {
    const auto& link = link_for(
        topology_, operation.source_domain_id,
        operation.target_domain_id);
    auto& runtime = links_.at(link.link_id);
    runtime_pointer = &runtime;
    if (runtime.queue_occupancy == 0 ||
        runtime.forward_completions ==
            std::numeric_limits<std::uint64_t>::max() ||
        result_.metrics.transfers_visible ==
            std::numeric_limits<std::uint64_t>::max()) {
      throw EngineError("Phase 6 link queue underflow");
    }
  } else if (is_uma(operation.kind)) {
    auto& runtime =
        links_.at("uma:" + topology_.uma_fabric->fabric_id);
    runtime_pointer = &runtime;
    if (runtime.queue_occupancy == 0 ||
        runtime.forward_completions ==
            std::numeric_limits<std::uint64_t>::max() ||
        (operation.kind == OperationKind::kUmaMutableAcquireWrite &&
         result_.metrics.uma_writes ==
             std::numeric_limits<std::uint64_t>::max()) ||
        ((operation.kind == OperationKind::kUmaMutableRead ||
          operation.kind == OperationKind::kUmaImmutableRead) &&
         result_.metrics.uma_reads ==
             std::numeric_limits<std::uint64_t>::max()) ||
        !uma_object_reservations_.contains(operation.object_id) ||
        uma_object_reservations_.at(operation.object_id) !=
            operation.key.event_id) {
      throw EngineError("Phase 6 UMA queue underflow");
    }
  }

  if (object_pointer != nullptr && next_object.has_value()) {
    *object_pointer = std::move(*next_object);
  }
  if (operation.kind == OperationKind::kExpertMove) {
    memory_occupancy_.at(*source_memory) -=
        objects_.at(operation.object_id).bytes;
  }
  if (runtime_pointer != nullptr) {
    --runtime_pointer->queue_occupancy;
    ++runtime_pointer->forward_completions;
  }
  if (is_transfer(operation.kind)) {
    ++result_.metrics.transfers_visible;
  } else if (operation.kind == OperationKind::kUmaMutableAcquireWrite) {
    ++result_.metrics.uma_writes;
  } else if (operation.kind == OperationKind::kUmaMutableRead ||
             operation.kind == OperationKind::kUmaImmutableRead) {
    ++result_.metrics.uma_reads;
  }
}

void MultiDomainSchedulerV1::release_resources(
    const ActiveReservation& reservation) {
  for (const auto& id : reservation.resource_ids) {
    if (resources_.at(id).occupancy == 0) {
      throw EngineError("Phase 6 resource occupancy underflow");
    }
  }
  for (const auto& id : reservation.resource_ids) {
    auto& resource = resources_.at(id);
    --resource.occupancy;
  }
}

void MultiDomainSchedulerV1::update_clocks() {
  for (auto& [id, clock] : clocks_) {
    static_cast<void>(id);
    clock.local_cycle = clock.ceil_edge(global_time_fs_);
    clock.fractional_remainder = clock.remainder(clock.local_cycle);
  }
}

void MultiDomainSchedulerV1::validate_alignment_time(
    const U128& time) const {
  for (const auto& alignment : topology_.synthetic_alignments) {
    if (time < alignment.valid_start_fs ||
        time > alignment.valid_end_fs) {
      throw EngineError(
          "Phase 6 time is outside inclusive synthetic alignment range");
    }
  }
}

void MultiDomainSchedulerV1::validate_object_invariants(
    const ObjectRuntime& object, const bool uma_mode) const {
  if (!uma_mode) return;
  std::set<std::string> compute_ids;
  for (const auto& item : topology_.compute_domains) {
    compute_ids.insert(item.domain_id);
  }
  if (!std::all_of(
          object.locations.begin(), object.locations.end(),
          [&](const auto& id) { return compute_ids.contains(id); }) ||
      !std::all_of(
          object.sharers.begin(), object.sharers.end(),
          [&](const auto& id) { return compute_ids.contains(id); })) {
    throw EngineError("UMA object references an unknown compute alias");
  }
  if (object.immutable) {
    if (object.owner.has_value() ||
        object.coherence_state == CoherenceState::kModified) {
      throw EngineError("immutable UMA object violates coherence invariant");
    }
    return;
  }
  switch (object.coherence_state) {
    case CoherenceState::kUncached:
      if (object.owner.has_value() || !object.sharers.empty()) {
        throw EngineError("UNCACHED UMA invariant violation");
      }
      return;
    case CoherenceState::kShared:
      if (object.owner.has_value()) {
        throw EngineError("SHARED UMA invariant violation");
      }
      return;
    case CoherenceState::kModified:
      if (!object.owner.has_value() ||
          !compute_ids.contains(*object.owner) ||
          !object.sharers.empty()) {
        throw EngineError("MODIFIED UMA invariant violation");
      }
      return;
  }
  throw EngineError("invalid UMA coherence state");
}

void MultiDomainSchedulerV1::reconcile_terminal_capacity() const {
  std::map<std::string, U128> reconstructed;
  for (const auto& item : topology_.memory_domains) {
    reconstructed[item.domain_id] = 0;
  }
  if (topology_.mode == PlatformMode::kDiscreteP2p2Gpu) {
    for (const auto& [id, object] : objects_) {
      static_cast<void>(id);
      for (const auto& location : object.locations) {
        reconstructed.at(location) = checked_add(
            reconstructed.at(location), object.bytes,
            "Phase 6 reconstructed private occupancy");
      }
    }
    for (const auto& reservation : inflight_destination_reservations_) {
      const auto operation_it = std::find_if(
          program_.operations.begin(), program_.operations.end(),
          [&](const auto& item) {
            return item.object_id == reservation.first &&
                   compute(topology_, item.target_domain_id).memory_domain_id ==
                       reservation.second &&
                   active_.contains(item.key.event_id);
          });
      if (operation_it == program_.operations.end()) {
        throw EngineError("orphan in-flight destination reservation");
      }
      const auto& object = objects_.at(reservation.first);
      if (!object.locations.contains(reservation.second)) {
        reconstructed.at(reservation.second) = checked_add(
            reconstructed.at(reservation.second), object.bytes,
            "Phase 6 reconstructed incoming reservation");
      }
    }
  } else {
    const std::string& shared = topology_.memory_domains[0].domain_id;
    for (const auto& [id, object] : objects_) {
      static_cast<void>(id);
      reconstructed[shared] = checked_add(
          reconstructed[shared], object.bytes,
          "Phase 6 reconstructed shared occupancy");
      validate_object_invariants(object, true);
    }
  }
  if (reconstructed != memory_occupancy_) {
    throw EngineError("Phase 6 terminal capacity reconciliation mismatch");
  }
  if (terminal_status_ == TerminalStatus::kQuiescent &&
      (!active_.empty() || !inflight_destination_reservations_.empty() ||
       !uma_object_reservations_.empty())) {
    throw EngineError("Phase 6 terminal state retains in-flight reservation");
  }
}

TraceEntry MultiDomainSchedulerV1::step() {
  if (terminal_status_ != TerminalStatus::kRunning) {
    throw EngineError("cannot step terminal Phase 6 scheduler");
  }
  const auto incrementable = [](const std::uint64_t value,
                                const std::uint64_t amount = 1) {
    return amount <= std::numeric_limits<std::uint64_t>::max() - value;
  };
  while (true) {
    const bool scheduled_due =
        !completion_queue_.empty() &&
        completion_queue_.begin()->key.time_fs <= global_time_fs_;
    std::optional<std::set<std::pair<EventKey, std::size_t>>::iterator>
        feasible;
    std::vector<std::string> feasible_resources;
    std::uint64_t scan_count = 0;
    if (!scheduled_due) {
      for (auto iterator = ready_.begin(); iterator != ready_.end();
           ++iterator) {
        ++scan_count;
        const Operation& candidate = program_.operations[iterator->second];
        const auto resources = required_resources(candidate);
        if (can_admit(candidate, resources)) {
          feasible = iterator;
          feasible_resources = resources;
          break;
        }
      }
    }

    if (scheduled_due) {
      const ScheduledEvent scheduled = *completion_queue_.begin();
      const Operation& operation =
          program_.operations.at(operation_index_.at(
              scheduled.operation_id));
      auto& reservation = active_.at(scheduled.operation_id);
      validate_alignment_time(scheduled.key.time_fs);
      if (!incrementable(result_.metrics.completion_queue_pops) ||
          !incrementable(result_.metrics.trace_events)) {
        throw EngineError("Phase 6 scheduled-event metric overflow");
      }
      bool terminal_event = false;
      if (scheduled.kind == TraceKind::kVisible) {
        if (reservation.visible_applied) {
          throw EngineError("duplicate Phase 6 visibility event");
        }
        apply_visibility(operation);
        reservation.visible_applied = true;
      } else if (scheduled.kind == TraceKind::kAckOrCredit) {
        if (!reservation.visible_applied || reservation.ack_applied ||
            !incrementable(result_.metrics.ack_or_credit_events)) {
          throw EngineError("invalid Phase 6 acknowledge transition");
        }
        if (is_transfer(operation.kind)) {
          const auto& link = link_for(
              topology_, operation.source_domain_id,
              operation.target_domain_id);
          auto& runtime = links_.at(link.link_id);
          if (!incrementable(runtime.acknowledgements) ||
              (link.bridge.protocol == BridgeProtocol::kCredit &&
               (runtime.credits_available >= link.bridge.queue_capacity ||
                !incrementable(runtime.credits_returned)))) {
            throw EngineError("Phase 6 link acknowledge overflow");
          }
          ++runtime.acknowledgements;
          if (link.bridge.protocol == BridgeProtocol::kCredit) {
            ++runtime.credits_available;
            ++runtime.credits_returned;
          }
        } else if (is_uma(operation.kind)) {
          const auto& fabric = *topology_.uma_fabric;
          auto& runtime = links_.at("uma:" + fabric.fabric_id);
          if (!incrementable(runtime.acknowledgements) ||
              (fabric.protocol == BridgeProtocol::kCredit &&
               (runtime.credits_available >= fabric.queue_capacity ||
                !incrementable(runtime.credits_returned)))) {
            throw EngineError("Phase 6 UMA acknowledge overflow");
          }
          ++runtime.acknowledgements;
          if (fabric.protocol == BridgeProtocol::kCredit) {
            ++runtime.credits_available;
            ++runtime.credits_returned;
          }
        } else {
          throw EngineError("non-crossing operation has acknowledge event");
        }
        reservation.ack_applied = true;
        ++result_.metrics.ack_or_credit_events;
        terminal_event = true;
      } else if (scheduled.kind == TraceKind::kComplete) {
        if (is_transfer(operation.kind) || is_uma(operation.kind)) {
          throw EngineError(
              "crossing operation used generic completion priority");
        }
        terminal_event = true;
      } else {
        throw EngineError("invalid scheduled Phase 6 trace kind");
      }
      if (terminal_event) {
        if ((is_transfer(operation.kind) || is_uma(operation.kind)) &&
            (!reservation.visible_applied || !reservation.ack_applied)) {
          throw EngineError("Phase 6 terminal transition preceded acknowledge");
        }
        if (operation.kind == OperationKind::kExpertCompute) {
          if (objects_.at(operation.object_id).pins == 0) {
            throw EngineError("Phase 6 expert pin underflow");
          }
        }
        for (const auto& id : reservation.resource_ids) {
          if (resources_.at(id).occupancy == 0) {
            throw EngineError("Phase 6 resource completion underflow");
          }
        }
        if (completion_times_.contains(operation.key.event_id) ||
            states_.at(operation.key.event_id) !=
                OperationState::kInFlight) {
          throw EngineError("duplicate Phase 6 completion");
        }
        std::uint64_t ready_additions = 0;
        for (const std::size_t dependent :
             dependents_[operation.key.event_id]) {
          const auto& dependent_operation = program_.operations[dependent];
          if (remaining_dependencies_.at(
                  dependent_operation.key.event_id) == 0) {
            throw EngineError("Phase 6 dependency counter underflow");
          }
          if (remaining_dependencies_.at(
                  dependent_operation.key.event_id) == 1 &&
              !future_arrivals_.contains(dependent_operation.key)) {
            if (ready_.contains(
                    {dependent_operation.key, dependent})) {
              throw EngineError("duplicate Phase 6 ready transition");
            }
            ++ready_additions;
          }
        }
        if (!incrementable(
                result_.metrics.dependency_edge_visits,
                dependents_[operation.key.event_id].size()) ||
            !incrementable(
                result_.metrics.ready_queue_pushes, ready_additions)) {
          throw EngineError("Phase 6 dependency metric overflow");
        }
        if (operation.kind == OperationKind::kExpertReplicate ||
            operation.kind == OperationKind::kExpertMove) {
          const std::string target =
              compute(topology_, operation.target_domain_id).memory_domain_id;
          if (!inflight_destination_reservations_.contains(
                  {operation.object_id, target})) {
            throw EngineError("missing destination reservation at completion");
          }
        } else if (is_uma(operation.kind)) {
          if (!uma_object_reservations_.contains(operation.object_id) ||
              uma_object_reservations_.at(operation.object_id) !=
                  operation.key.event_id) {
            throw EngineError("missing UMA reservation at completion");
          }
        }
        release_resources(reservation);
        if (operation.kind == OperationKind::kExpertCompute) {
          --objects_.at(operation.object_id).pins;
        }
        if (operation.kind == OperationKind::kExpertReplicate ||
            operation.kind == OperationKind::kExpertMove) {
          const std::string target =
              compute(topology_, operation.target_domain_id).memory_domain_id;
          inflight_destination_reservations_.erase(
              {operation.object_id, target});
        } else if (is_uma(operation.kind)) {
          uma_object_reservations_.erase(operation.object_id);
        }
        states_.at(operation.key.event_id) = OperationState::kComplete;
        completion_times_[operation.key.event_id] = scheduled.key.time_fs;
        for (const std::size_t dependent :
             dependents_[operation.key.event_id]) {
          ++result_.metrics.dependency_edge_visits;
          auto& remaining = remaining_dependencies_.at(
              program_.operations[dependent].key.event_id);
          if (remaining == 0) {
            throw EngineError("Phase 6 dependency counter underflow");
          }
          --remaining;
          if (remaining == 0 &&
              !future_arrivals_.contains(
                  program_.operations[dependent].key)) {
            ready_.emplace(
                program_.operations[dependent].key, dependent);
            ++result_.metrics.ready_queue_pushes;
          }
        }
        active_.erase(operation.key.event_id);
      }
      global_time_fs_ = scheduled.key.time_fs;
      update_clocks();
      completion_queue_.erase(completion_queue_.begin());
      ++result_.metrics.completion_queue_pops;
      TraceEntry trace{
          operation.key.event_id, scheduled.kind, global_time_fs_,
          scheduled.key};
      result_.trace.push_back(trace);
      ++result_.metrics.trace_events;
      return trace;
    }

    if (feasible.has_value()) {
      const auto iterator = *feasible;
      const std::size_t index = iterator->second;
      const Operation& operation = program_.operations[index];
      validate_runtime_operation(operation);
      const U128 ready = dependency_ready(operation);
      const auto [visible, acknowledge, completion] =
          timing(operation, global_time_fs_);
      validate_alignment_time(global_time_fs_);
      validate_alignment_time(visible);
      validate_alignment_time(acknowledge);
      validate_alignment_time(completion);
      if (completion <= global_time_fs_ || visible <= global_time_fs_ ||
          ((is_transfer(operation.kind) || is_uma(operation.kind)) &&
           acknowledge <= visible)) {
        throw EngineError("Phase 6 operation lacks strict progress");
      }
      for (const auto& id : feasible_resources) {
        const auto& resource = resources_.at(id);
        if (resource.occupancy >= resource.capacity) {
          throw EngineError("non-atomic Phase 6 resource admission");
        }
      }
      const bool crossing =
          is_transfer(operation.kind) || is_uma(operation.kind);
      const std::uint64_t scheduled_count = crossing ? 2 : 1;
      auto class_metric = [&]() -> std::uint64_t& {
        switch (operation.kind) {
          case OperationKind::kActivationDispatch:
            return result_.metrics.dispatches;
          case OperationKind::kExpertCompute:
            return result_.metrics.computes;
          case OperationKind::kActivationReturn:
            return result_.metrics.returns;
          case OperationKind::kTokenCombine:
            return result_.metrics.combines;
          default:
            return result_.metrics.atomic_admission_attempts;
        }
      };
      if (!incrementable(
              result_.metrics.scheduler_key_comparisons, scan_count) ||
          !incrementable(
              result_.metrics.atomic_admission_attempts, scan_count) ||
          !incrementable(result_.metrics.ready_queue_pops) ||
          !incrementable(
              result_.metrics.completion_queue_pushes, scheduled_count) ||
          !incrementable(result_.metrics.trace_events) ||
          !incrementable(class_metric()) ||
          active_.contains(operation.key.event_id) ||
          states_.at(operation.key.event_id) != OperationState::kPending) {
        throw EngineError("Phase 6 fail-atomic admission precheck failed");
      }
      std::optional<std::pair<std::string, std::string>>
          destination_reservation;
      std::optional<U128> target_occupancy;
      if (operation.kind == OperationKind::kExpertReplicate ||
          operation.kind == OperationKind::kExpertMove) {
        const auto& object = objects_.at(operation.object_id);
        const std::string target =
            compute(topology_, operation.target_domain_id).memory_domain_id;
        destination_reservation =
            std::pair{operation.object_id, target};
        if (inflight_destination_reservations_.contains(
                *destination_reservation)) {
          throw EngineError("duplicate in-flight destination reservation");
        }
        target_occupancy = checked_add(
            memory_occupancy_.at(target), object.bytes,
            "Phase 6 incoming destination reservation");
        if (*target_occupancy >
            memory(topology_, target).capacity_bytes) {
          throw EngineError("non-atomic Phase 6 destination reservation");
        }
      } else if (is_uma(operation.kind) &&
                 uma_object_reservations_.contains(operation.object_id)) {
        throw EngineError("duplicate in-flight UMA object reservation");
      }
      LinkRuntime* runtime_pointer = nullptr;
      std::uint64_t queue_capacity = 0;
      BridgeProtocol protocol = BridgeProtocol::kRequestAck;
      if (is_transfer(operation.kind)) {
        const auto& link = link_for(
            topology_, operation.source_domain_id,
            operation.target_domain_id);
        runtime_pointer = &links_.at(link.link_id);
        queue_capacity = link.bridge.queue_capacity;
        protocol = link.bridge.protocol;
      } else if (is_uma(operation.kind)) {
        const auto& fabric = *topology_.uma_fabric;
        runtime_pointer = &links_.at("uma:" + fabric.fabric_id);
        queue_capacity = fabric.queue_capacity;
        protocol = fabric.protocol;
      }
      if (runtime_pointer != nullptr &&
          (runtime_pointer->queue_occupancy >= queue_capacity ||
           !incrementable(runtime_pointer->queue_occupancy) ||
           !incrementable(runtime_pointer->requests_started) ||
           (protocol == BridgeProtocol::kCredit &&
            runtime_pointer->credits_available == 0))) {
        throw EngineError("Phase 6 queue/credit admission precheck failed");
      }
      const EventKey start_key =
          trace_key(operation, TraceKind::kStart, global_time_fs_);
      const ScheduledEvent visible_event{
          trace_key(operation, TraceKind::kVisible, visible),
          operation.key.event_id, TraceKind::kVisible};
      const ScheduledEvent acknowledge_event{
          trace_key(operation, TraceKind::kAckOrCredit, acknowledge),
          operation.key.event_id, TraceKind::kAckOrCredit};
      const ScheduledEvent completion_event{
          trace_key(operation, TraceKind::kComplete, completion),
          operation.key.event_id, TraceKind::kComplete};
      if ((crossing &&
           (completion_queue_.contains(visible_event) ||
            completion_queue_.contains(acknowledge_event))) ||
          (!crossing && completion_queue_.contains(completion_event))) {
        throw EngineError("Phase 6 scheduled event collision");
      }
      ActiveReservation reservation{
          operation.key.event_id, feasible_resources, visible, acknowledge,
          completion, !crossing, !crossing};
      active_.emplace(operation.key.event_id, reservation);
      if (destination_reservation.has_value()) {
        inflight_destination_reservations_.insert(
            *destination_reservation);
      } else if (is_uma(operation.kind)) {
        uma_object_reservations_.emplace(
            operation.object_id, operation.key.event_id);
      }
      if (crossing) {
        completion_queue_.insert(visible_event);
        completion_queue_.insert(acknowledge_event);
      } else {
        completion_queue_.insert(completion_event);
      }
      result_.entries.push_back(
          ScheduleEntry{
              operation.key.event_id, ready, global_time_fs_, visible,
              acknowledge, completion, feasible_resources});
      for (const auto& id : feasible_resources) {
        ++resources_.at(id).occupancy;
      }
      if (operation.kind == OperationKind::kExpertCompute) {
        ++objects_.at(operation.object_id).pins;
      }
      if (destination_reservation.has_value()) {
        memory_occupancy_.at(destination_reservation->second) =
            *target_occupancy;
      }
      if (runtime_pointer != nullptr) {
        ++runtime_pointer->queue_occupancy;
        ++runtime_pointer->requests_started;
        if (protocol == BridgeProtocol::kCredit) {
          --runtime_pointer->credits_available;
        }
        result_.metrics.queue_peak = std::max(
            result_.metrics.queue_peak,
            runtime_pointer->queue_occupancy);
      }
      ready_.erase(iterator);
      ++result_.metrics.ready_queue_pops;
      result_.metrics.scheduler_key_comparisons += scan_count;
      result_.metrics.atomic_admission_attempts += scan_count;
      result_.metrics.completion_queue_pushes += scheduled_count;
      states_.at(operation.key.event_id) = OperationState::kInFlight;
      switch (operation.kind) {
        case OperationKind::kActivationDispatch:
          ++result_.metrics.dispatches;
          break;
        case OperationKind::kExpertCompute:
          ++result_.metrics.computes;
          break;
        case OperationKind::kActivationReturn:
          ++result_.metrics.returns;
          break;
        case OperationKind::kTokenCombine:
          ++result_.metrics.combines;
          break;
        default:
          break;
      }
      TraceEntry trace{
          operation.key.event_id, TraceKind::kStart, global_time_fs_,
          start_key};
      result_.trace.push_back(trace);
      ++result_.metrics.trace_events;
      return trace;
    }

    std::optional<U128> next_time;
    if (!completion_queue_.empty()) {
      next_time = completion_queue_.begin()->key.time_fs;
    }
    if (!future_arrivals_.empty() &&
        (!next_time.has_value() ||
         future_arrivals_.begin()->first.time_fs < *next_time)) {
      next_time = future_arrivals_.begin()->first.time_fs;
    }
    if (next_time.has_value()) {
      if (*next_time < global_time_fs_) {
        throw EngineError("Phase 6 global time regressed");
      }
      validate_alignment_time(*next_time);
      global_time_fs_ = *next_time;
      update_clocks();
      activate_arrivals();
      continue;
    }
    const bool all_complete = std::all_of(
        states_.begin(), states_.end(), [](const auto& item) {
          return item.second == OperationState::kComplete;
        });
    if (all_complete &&
        (!active_.empty() ||
         !inflight_destination_reservations_.empty() ||
         !uma_object_reservations_.empty())) {
      throw EngineError("completed program retains in-flight state");
    }
    terminal_status_ =
        all_complete ? TerminalStatus::kQuiescent : TerminalStatus::kDeadlock;
    reconcile_terminal_capacity();
    result_.terminal_status = terminal_status_;
    result_.makespan_fs = global_time_fs_;
    result_.terminal_objects = objects_;
    result_.terminal_memory_occupancy = memory_occupancy_;
    result_.semantic_digest = state_digest();
    return TraceEntry{"", TraceKind::kComplete, global_time_fs_, EventKey{}};
  }
}

Result MultiDomainSchedulerV1::run_until_quiescent() {
  while (terminal_status_ == TerminalStatus::kRunning) {
    static_cast<void>(step());
  }
  if (terminal_status_ != TerminalStatus::kQuiescent) {
    throw EngineError("Phase 6 scheduler did not quiesce");
  }
  result_.semantic_digest = state_digest();
  return result_;
}

std::string MultiDomainSchedulerV1::topology_digest() const {
  if (!topology_digest_.empty()) return topology_digest_;
  std::ostringstream stream;
  put(stream, "phase6-topology-v1");
  put(stream, topology_.topology_id);
  put(stream, mode_name(topology_.mode));
  const auto& authority = topology_.authority;
  put(stream, authority.build_authority_sha256);
  put(stream, authority.topology_contract_sha256);
  put(stream, authority.cdc_contract_sha256);
  put(stream, authority.p2p_contract_sha256);
  put(stream, authority.uma_contract_sha256);
  put(stream, authority.checkpoint_contract_sha256);
  put(stream, authority.claim_boundary_sha256);
  put(stream, authority.phase5_ledger_sha256);
  put(stream, authority.phase5_review_ledger_sha256);
  put(stream, authority.phase5_review_aggregate_sha256);
  put(stream, std::to_string(topology_.compute_domains.size()));
  for (const auto& item : topology_.compute_domains) {
    put(stream, item.domain_id);
    put(stream, item.memory_domain_id);
    put(stream, item.clock.clock_id);
    put(stream, std::to_string(item.clock.frequency_numerator_hz));
    put(stream, std::to_string(item.clock.frequency_denominator_hz));
    put(stream, to_decimal(item.clock.phase_offset_fs));
    put(stream, std::to_string(item.clock.local_cycle));
    put(stream, std::to_string(item.clock.fractional_remainder));
    put(stream, std::to_string(item.compute_lanes));
    put(stream, to_decimal(item.throughput_numerator_per_second));
    put(stream, to_decimal(item.throughput_denominator));
    put(stream, to_decimal(item.setup_latency_fs));
  }
  put(stream, std::to_string(topology_.memory_domains.size()));
  for (const auto& item : topology_.memory_domains) {
    put(stream, item.domain_id);
    put(stream, to_decimal(item.capacity_bytes));
  }
  put(stream, std::to_string(topology_.links.size()));
  for (const auto& item : topology_.links) {
    put(stream, item.link_id);
    put(stream, item.source_compute_id);
    put(stream, item.target_compute_id);
    put(stream, item.bridge.bridge_id);
    put(stream, item.bridge.source_clock_id);
    put(stream, item.bridge.target_clock_id);
    put(stream, std::to_string(static_cast<int>(item.bridge.protocol)));
    put(stream, to_decimal(item.bridge.forward_latency_fs));
    put(stream, to_decimal(item.bridge.reverse_latency_fs));
    put(stream, std::to_string(item.bridge.receiver_sync_cycles));
    put(stream, std::to_string(item.bridge.ack_sync_cycles));
    put(stream, std::to_string(item.bridge.queue_capacity));
    put(
        stream,
        std::to_string(static_cast<int>(item.bridge.backpressure_policy)));
    put(stream, to_decimal(item.throughput_numerator_bytes_per_second));
    put(stream, to_decimal(item.throughput_denominator));
    put(stream, to_decimal(item.setup_latency_fs));
    put(stream, std::to_string(item.directional_lanes));
    put(stream, item.duplex_group);
    put(stream, item.full_duplex ? "1" : "0");
    put(stream, std::to_string(item.initial_credits));
  }
  put(stream, topology_.uma_fabric.has_value() ? "1" : "0");
  if (topology_.uma_fabric.has_value()) {
    const auto& item = *topology_.uma_fabric;
    put(stream, item.fabric_id);
    put(stream, item.memory_domain_id);
    put(stream, item.fabric_clock.clock_id);
    put(stream, std::to_string(item.fabric_clock.frequency_numerator_hz));
    put(stream, std::to_string(item.fabric_clock.frequency_denominator_hz));
    put(stream, to_decimal(item.fabric_clock.phase_offset_fs));
    put(stream, std::to_string(item.fabric_clock.local_cycle));
    put(stream, std::to_string(item.fabric_clock.fractional_remainder));
    put(stream, std::to_string(item.lanes));
    put(stream, to_decimal(item.throughput_numerator_bytes_per_second));
    put(stream, to_decimal(item.throughput_denominator));
    put(stream, to_decimal(item.setup_latency_fs));
    put(stream, std::to_string(item.queue_capacity));
    put(stream, std::to_string(static_cast<int>(item.protocol)));
    put(stream, std::to_string(item.initial_credits));
    put(stream, to_decimal(item.forward_latency_fs));
    put(stream, to_decimal(item.reverse_latency_fs));
    put(stream, std::to_string(item.receiver_sync_cycles));
    put(stream, std::to_string(item.ack_sync_cycles));
  }
  put(stream, std::to_string(topology_.synthetic_alignments.size()));
  for (const auto& item : topology_.synthetic_alignments) {
    put(stream, item.clock_id);
    put(stream, item.calibration_method);
    put(stream, to_decimal(item.residual_error_fs));
    put(stream, to_decimal(item.confidence_interval_95_lower_fs));
    put(stream, to_decimal(item.confidence_interval_95_upper_fs));
    put(stream, item.quality);
    put(stream, item.evidence_label);
    put(stream, to_decimal(item.valid_start_fs));
    put(stream, to_decimal(item.valid_end_fs));
  }
  return sha256_bytes(stream.str());
}

std::string MultiDomainSchedulerV1::program_digest() const {
  if (!program_digest_.empty()) return program_digest_;
  std::ostringstream stream;
  put(stream, "phase6-program-v1");
  put(stream, program_.program_id);
  put(stream, program_.phase5_plan_digest);
  put(stream, program_.phase5_action_digest);
  put(stream, std::to_string(program_.phase5_actions.size()));
  for (const auto& action : program_.phase5_actions) {
    put(stream, action.action_id);
    put(stream, std::to_string(static_cast<int>(action.kind)));
    put(stream, action.expert.has_value() ? "1" : "0");
    put(
        stream,
        action.expert.has_value() ? expert_name(*action.expert) : "");
    put(stream, action.source_id);
    put(stream, std::to_string(action.dependencies.size()));
    for (const auto& dependency : action.dependencies) {
      put(stream, dependency);
    }
    put(stream, std::to_string(static_cast<int>(action.service_class)));
    put(stream, to_decimal(action.work));
    put(stream, to_decimal(action.release_fs));
    put(stream, action.prefetch_load ? "1" : "0");
  }
  put(stream, std::to_string(program_.allowed_phase5_action_ids.size()));
  for (const auto& action : program_.allowed_phase5_action_ids) {
    put(stream, action);
  }
  put(stream, program_.phase5_ledger_sha256);
  put(stream, traffic_profile_digest(program_.payload_profile));
  put(stream, std::to_string(program_.initial_objects.size()));
  for (const auto& item : program_.initial_objects) {
    put(stream, item.object_id);
    put(stream, to_decimal(item.bytes));
    put(stream, item.immutable ? "1" : "0");
    put(stream, item.expert.has_value() ? "1" : "0");
    put(
        stream,
        item.expert.has_value() ? expert_name(*item.expert) : "");
    put(stream, item.content_sha256);
    put(stream, std::to_string(item.locations.size()));
    for (const auto& location : item.locations) put(stream, location);
    put(stream, std::to_string(item.pins));
    put(stream, std::to_string(item.version));
    put(stream, coherence_name(item.coherence_state));
    put(stream, item.owner.has_value() ? "1" : "0");
    put(stream, item.owner.value_or(""));
    put(stream, std::to_string(item.sharers.size()));
    for (const auto& sharer : item.sharers) put(stream, sharer);
  }
  put(stream, std::to_string(program_.routing.size()));
  for (const auto& route : program_.routing) {
    put(stream, route.demand_id);
    put_key(stream, route.route_key);
    put(stream, std::to_string(route.top_k));
    put(stream, route.token_owner_compute_domain_id);
    put(stream, std::to_string(route.selected_experts.size()));
    for (const auto& expert : route.selected_experts) {
      put(stream, expert_name(expert));
    }
    put(stream, std::to_string(route.assigned_compute_domains.size()));
    for (const auto& [expert, domain] :
         route.assigned_compute_domains) {
      put(stream, expert_name(expert));
      put(stream, domain);
    }
    put(stream, route.routing_provenance);
  }
  put(stream, std::to_string(program_.operations.size()));
  for (const auto& operation : program_.operations) {
    put_key(stream, operation.key);
    put(stream, std::to_string(operation.dependencies.size()));
    for (const auto& dependency : operation.dependencies) {
      put(stream, dependency);
    }
    put(stream, std::to_string(operation.source_phase5_action_ids.size()));
    for (const auto& action : operation.source_phase5_action_ids) {
      put(stream, action);
    }
    put(stream, kind_name(operation.kind));
    put(stream, operation.demand_id);
    put(stream, operation.expert.has_value() ? "1" : "0");
    put(stream, operation.expert.has_value()
                    ? expert_name(*operation.expert)
                    : "");
    put(stream, operation.source_domain_id);
    put(stream, operation.target_domain_id);
    put(stream, operation.object_id);
    put(stream, to_decimal(operation.bytes));
    put(stream, to_decimal(operation.work));
    put(stream, std::to_string(operation.expected_version));
    put(stream, std::to_string(operation.output_version));
  }
  return sha256_bytes(stream.str());
}

std::string MultiDomainSchedulerV1::canonical_state() const {
  std::ostringstream stream;
  put(stream, "phase6-multi-domain-state-v1");
  put(stream, topology_digest());
  put(stream, program_digest());
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
  for (const auto& [id, active] : active_) {
    put(stream, id);
    put(stream, active.operation_id);
    put(stream, std::to_string(active.resource_ids.size()));
    for (const auto& resource : active.resource_ids) put(stream, resource);
    put(stream, to_decimal(active.visible_fs));
    put(stream, to_decimal(active.ack_fs));
    put(stream, to_decimal(active.completion_fs));
    put(stream, active.visible_applied ? "1" : "0");
    put(stream, active.ack_applied ? "1" : "0");
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
  put(stream, "FUTURE");
  put(stream, std::to_string(future_arrivals_.size()));
  for (const auto& [key, index] : future_arrivals_) {
    put_key(stream, key);
    put(stream, std::to_string(index));
  }
  put(stream, "READY");
  put(stream, std::to_string(ready_.size()));
  for (const auto& [key, index] : ready_) {
    put_key(stream, key);
    put(stream, std::to_string(index));
  }
  put(stream, "COMPLETION_QUEUE");
  put(stream, std::to_string(completion_queue_.size()));
  for (const auto& event : completion_queue_) {
    put_key(stream, event.key);
    put(stream, event.operation_id);
    put(stream, trace_name(event.kind));
  }
  put(stream, "RESOURCES");
  put(stream, std::to_string(resources_.size()));
  for (const auto& [id, state] : resources_) {
    put(stream, id);
    put(stream, std::to_string(state.capacity));
    put(stream, std::to_string(state.occupancy));
  }
  put(stream, "LINKS");
  put(stream, std::to_string(links_.size()));
  for (const auto& [id, state] : links_) {
    put(stream, id);
    put(stream, std::to_string(state.queue_occupancy));
    put(stream, std::to_string(state.credits_available));
    put(stream, std::to_string(state.requests_started));
    put(stream, std::to_string(state.forward_completions));
    put(stream, std::to_string(state.acknowledgements));
    put(stream, std::to_string(state.credits_returned));
  }
  put(stream, "OBJECTS");
  put(stream, std::to_string(objects_.size()));
  for (const auto& [id, object] : objects_) {
    put(stream, id);
    put(stream, to_decimal(object.bytes));
    put(stream, object.immutable ? "1" : "0");
    put(stream, object.expert.has_value() ? "1" : "0");
    put(
        stream,
        object.expert.has_value() ? expert_name(*object.expert) : "");
    put(stream, object.content_sha256);
    put(stream, std::to_string(object.locations.size()));
    for (const auto& location : object.locations) put(stream, location);
    put(stream, std::to_string(object.pins));
    put(stream, std::to_string(object.version));
    put(stream, coherence_name(object.coherence_state));
    put(stream, object.owner.has_value() ? "1" : "0");
    put(stream, object.owner.value_or(""));
    put(stream, std::to_string(object.sharers.size()));
    for (const auto& sharer : object.sharers) put(stream, sharer);
  }
  put(stream, "MEMORY_OCCUPANCY");
  put(stream, std::to_string(memory_occupancy_.size()));
  for (const auto& [id, occupancy] : memory_occupancy_) {
    put(stream, id);
    put(stream, to_decimal(occupancy));
  }
  put(stream, "INFLIGHT_DESTINATIONS");
  put(
      stream,
      std::to_string(inflight_destination_reservations_.size()));
  for (const auto& [object, target] :
       inflight_destination_reservations_) {
    put(stream, object);
    put(stream, target);
  }
  put(stream, "UMA_OBJECT_RESERVATIONS");
  put(stream, std::to_string(uma_object_reservations_.size()));
  for (const auto& [object, operation] : uma_object_reservations_) {
    put(stream, object);
    put(stream, operation);
  }
  put(stream, "CLOCKS");
  put(stream, std::to_string(clocks_.size()));
  for (const auto& [id, clock] : clocks_) {
    put(stream, id);
    put(stream, std::to_string(clock.local_cycle));
    put(stream, std::to_string(clock.fractional_remainder));
  }
  put(stream, "ENTRIES");
  put(stream, std::to_string(result_.entries.size()));
  for (const auto& entry : result_.entries) {
    put(stream, entry.operation_id);
    put(stream, to_decimal(entry.dependency_ready_fs));
    put(stream, to_decimal(entry.start_fs));
    put(stream, to_decimal(entry.visible_fs));
    put(stream, to_decimal(entry.ack_fs));
    put(stream, to_decimal(entry.completion_fs));
    put(stream, std::to_string(entry.resource_ids.size()));
    for (const auto& resource : entry.resource_ids) put(stream, resource);
  }
  put(stream, "TRACE");
  put(stream, std::to_string(result_.trace.size()));
  for (const auto& trace : result_.trace) {
    put(stream, trace.operation_id);
    put(stream, trace_name(trace.kind));
    put(stream, to_decimal(trace.time_fs));
    put_key(stream, trace.key);
  }
  const auto& metrics = result_.metrics;
  put(stream, std::to_string(metrics.ready_queue_pushes));
  put(stream, std::to_string(metrics.ready_queue_pops));
  put(stream, std::to_string(metrics.completion_queue_pushes));
  put(stream, std::to_string(metrics.completion_queue_pops));
  put(stream, std::to_string(metrics.dependency_edge_visits));
  put(stream, std::to_string(metrics.scheduler_key_comparisons));
  put(stream, std::to_string(metrics.atomic_admission_attempts));
  put(stream, std::to_string(metrics.trace_events));
  put(stream, std::to_string(metrics.dispatches));
  put(stream, std::to_string(metrics.computes));
  put(stream, std::to_string(metrics.returns));
  put(stream, std::to_string(metrics.combines));
  put(stream, std::to_string(metrics.transfers_visible));
  put(stream, std::to_string(metrics.ack_or_credit_events));
  put(stream, std::to_string(metrics.queue_peak));
  put(stream, std::to_string(metrics.uma_reads));
  put(stream, std::to_string(metrics.uma_writes));
  put(stream, to_decimal(result_.makespan_fs));
  put(stream, std::to_string(static_cast<int>(result_.terminal_status)));
  put(stream, fidelity_name(result_.fidelity));
  put(
      stream,
      result_.range_status == RangeStatus::kRangeUnknown
          ? "RANGE_UNKNOWN"
          : "INVALID");
  put(stream, result_.profile_origin);
  put(stream, result_.execution_claim);
  put(stream, result_.calibration_pass ? "1" : "0");
  put(stream, result_.gpu_used ? "1" : "0");
  put(stream, "TERMINAL_OBJECTS");
  put(stream, std::to_string(result_.terminal_objects.size()));
  for (const auto& [id, object] : result_.terminal_objects) {
    put(stream, id);
    put(stream, to_decimal(object.bytes));
    put(stream, object.immutable ? "1" : "0");
    put(stream, object.expert.has_value() ? "1" : "0");
    put(
        stream,
        object.expert.has_value() ? expert_name(*object.expert) : "");
    put(stream, object.content_sha256);
    put(stream, std::to_string(object.locations.size()));
    for (const auto& location : object.locations) put(stream, location);
    put(stream, std::to_string(object.pins));
    put(stream, std::to_string(object.version));
    put(stream, coherence_name(object.coherence_state));
    put(stream, object.owner.has_value() ? "1" : "0");
    put(stream, object.owner.value_or(""));
    put(stream, std::to_string(object.sharers.size()));
    for (const auto& sharer : object.sharers) put(stream, sharer);
  }
  put(stream, "TERMINAL_MEMORY");
  put(stream, std::to_string(result_.terminal_memory_occupancy.size()));
  for (const auto& [id, value] : result_.terminal_memory_occupancy) {
    put(stream, id);
    put(stream, to_decimal(value));
  }
  return stream.str();
}

std::string MultiDomainSchedulerV1::state_digest() const {
  return sha256_bytes(canonical_state());
}

Checkpoint MultiDomainSchedulerV1::checkpoint() const {
  validate_alignment_time(global_time_fs_);
  reconcile_terminal_capacity();
  std::map<std::string, std::uint64_t> cycles;
  std::map<std::string, std::uint64_t> remainders;
  for (const auto& [id, clock] : clocks_) {
    cycles[id] = clock.local_cycle;
    remainders[id] = clock.fractional_remainder;
  }
  return Checkpoint{
      "phase6-checkpoint-v1",
      topology_digest(),
      program_digest(),
      global_time_fs_,
      terminal_status_,
      states_,
      active_,
      completion_times_,
      remaining_dependencies_,
      future_arrivals_,
      ready_,
      completion_queue_,
      resources_,
      links_,
      objects_,
      memory_occupancy_,
      inflight_destination_reservations_,
      uma_object_reservations_,
      cycles,
      remainders,
      result_,
      state_digest()};
}

std::string MultiDomainSchedulerV1::serialize_checkpoint() const {
  const std::string body = canonical_state();
  std::ostringstream stream;
  stream << "moe-phase6-checkpoint-v1\n"
         << result_.trace.size() << '\n'
         << static_cast<int>(terminal_status_) << '\n'
         << body.size() << '\n'
         << body
         << sha256_bytes(body) << '\n';
  return stream.str();
}

MultiDomainSchedulerV1 MultiDomainSchedulerV1::restore(
    Topology topology,
    Program program,
    const Checkpoint& checkpoint_value) {
  if (checkpoint_value.schema_version != "phase6-checkpoint-v1" ||
      !is_hex_hash(checkpoint_value.state_digest) ||
      (checkpoint_value.terminal_status == TerminalStatus::kRunning &&
       !checkpoint_value.result.semantic_digest.empty()) ||
      (checkpoint_value.terminal_status != TerminalStatus::kRunning &&
       checkpoint_value.result.semantic_digest !=
           checkpoint_value.state_digest)) {
    throw EngineError("invalid Phase 6 checkpoint schema");
  }
  MultiDomainSchedulerV1 supplied(topology, program);
  if (supplied.topology_digest() != checkpoint_value.topology_digest ||
      supplied.program_digest() != checkpoint_value.program_digest) {
    throw EngineError("Phase 6 checkpoint authority mismatch");
  }
  supplied.global_time_fs_ = checkpoint_value.global_time_fs;
  supplied.terminal_status_ = checkpoint_value.terminal_status;
  supplied.states_ = checkpoint_value.states;
  supplied.active_ = checkpoint_value.active;
  supplied.completion_times_ = checkpoint_value.completion_times;
  supplied.remaining_dependencies_ = checkpoint_value.remaining_dependencies;
  supplied.future_arrivals_ = checkpoint_value.future_arrivals;
  supplied.ready_ = checkpoint_value.ready;
  supplied.completion_queue_ = checkpoint_value.completion_queue;
  supplied.resources_ = checkpoint_value.resources;
  supplied.links_ = checkpoint_value.links;
  supplied.objects_ = checkpoint_value.objects;
  supplied.memory_occupancy_ = checkpoint_value.memory_occupancy;
  supplied.inflight_destination_reservations_ =
      checkpoint_value.inflight_destination_reservations;
  supplied.uma_object_reservations_ =
      checkpoint_value.uma_object_reservations;
  supplied.result_ = checkpoint_value.result;
  supplied.validate_alignment_time(checkpoint_value.global_time_fs);
  for (auto& [id, clock] : supplied.clocks_) {
    if (!checkpoint_value.clock_cycles.contains(id) ||
        !checkpoint_value.clock_remainders.contains(id)) {
      throw EngineError("Phase 6 checkpoint clock set mismatch");
    }
    clock.local_cycle = checkpoint_value.clock_cycles.at(id);
    clock.fractional_remainder =
        checkpoint_value.clock_remainders.at(id);
  }
  if (checkpoint_value.clock_cycles.size() != supplied.clocks_.size() ||
      checkpoint_value.clock_remainders.size() != supplied.clocks_.size() ||
      supplied.state_digest() != checkpoint_value.state_digest) {
    throw EngineError("Phase 6 checkpoint digest mismatch");
  }

  MultiDomainSchedulerV1 replay(std::move(topology), std::move(program));
  while (replay.result_.trace.size() <
         checkpoint_value.result.trace.size()) {
    static_cast<void>(replay.step());
  }
  if (checkpoint_value.terminal_status != TerminalStatus::kRunning &&
      replay.terminal_status_ == TerminalStatus::kRunning) {
    const std::size_t before = replay.result_.trace.size();
    static_cast<void>(replay.step());
    if (replay.result_.trace.size() != before) {
      throw EngineError("Phase 6 terminal checkpoint is not a prefix");
    }
  }
  if (replay.terminal_status_ != checkpoint_value.terminal_status ||
      replay.state_digest() != checkpoint_value.state_digest) {
    throw EngineError("Phase 6 checkpoint is not a reachable exact prefix");
  }
  return replay;
}

MultiDomainSchedulerV1 MultiDomainSchedulerV1::restore_serialized(
    Topology topology,
    Program program,
    const std::string& bytes) {
  std::size_t cursor = 0;
  const auto read_line = [&](std::size_t& position) {
    const std::size_t end = bytes.find('\n', position);
    if (end == std::string::npos) {
      throw EngineError("invalid Phase 6 checkpoint wire header");
    }
    const std::string value = bytes.substr(position, end - position);
    position = end + 1;
    return value;
  };
  const std::string magic = read_line(cursor);
  const std::string trace_count_text = read_line(cursor);
  const std::string terminal_text = read_line(cursor);
  const std::string body_size_text = read_line(cursor);
  if (magic != "moe-phase6-checkpoint-v1") {
    throw EngineError("invalid Phase 6 checkpoint wire magic");
  }
  std::size_t trace_count = 0;
  std::size_t body_size = 0;
  int terminal = 0;
  const auto trace_parse = std::from_chars(
      trace_count_text.data(),
      trace_count_text.data() + trace_count_text.size(), trace_count);
  const auto terminal_parse = std::from_chars(
      terminal_text.data(), terminal_text.data() + terminal_text.size(),
      terminal);
  const auto body_parse = std::from_chars(
      body_size_text.data(), body_size_text.data() + body_size_text.size(),
      body_size);
  if (trace_parse.ec != std::errc{} ||
      trace_parse.ptr !=
          trace_count_text.data() + trace_count_text.size() ||
      terminal_parse.ec != std::errc{} ||
      terminal_parse.ptr != terminal_text.data() + terminal_text.size() ||
      body_parse.ec != std::errc{} ||
      body_parse.ptr != body_size_text.data() + body_size_text.size() ||
      terminal < static_cast<int>(TerminalStatus::kRunning) ||
      terminal > static_cast<int>(TerminalStatus::kFailed) ||
      cursor > bytes.size() || body_size > bytes.size() - cursor ||
      bytes.size() - cursor < 65 ||
      body_size != bytes.size() - cursor - 65) {
    throw EngineError("invalid Phase 6 checkpoint wire field");
  }
  const std::string body = bytes.substr(cursor, body_size);
  cursor += body_size;
  const std::string digest = bytes.substr(cursor, 64);
  cursor += 64;
  if (cursor >= bytes.size() || bytes[cursor] != '\n' ||
      cursor + 1 != bytes.size() || !is_hex_hash(digest) ||
      sha256_bytes(body) != digest) {
    throw EngineError("invalid Phase 6 checkpoint wire integrity");
  }
  MultiDomainSchedulerV1 replay(std::move(topology), std::move(program));
  while (replay.result_.trace.size() < trace_count) {
    static_cast<void>(replay.step());
  }
  const auto expected_terminal = static_cast<TerminalStatus>(terminal);
  if (expected_terminal != TerminalStatus::kRunning &&
      replay.terminal_status_ == TerminalStatus::kRunning) {
    const std::size_t before = replay.result_.trace.size();
    static_cast<void>(replay.step());
    if (replay.result_.trace.size() != before) {
      throw EngineError("Phase 6 wire terminal is not a prefix");
    }
  }
  if (replay.terminal_status_ != expected_terminal ||
      replay.state_digest() != digest ||
      replay.canonical_state() != body) {
    throw EngineError("Phase 6 checkpoint wire digest mismatch");
  }
  return replay;
}

}  // namespace moe_sim::phase6

#include "moe_sim/multi_domain_scheduler.hpp"

#include <functional>
#include <iostream>
#include <algorithm>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>

namespace {

using namespace moe_sim;
using namespace moe_sim::phase6;

void require(const bool condition, const std::string& message) {
  if (!condition) throw std::runtime_error(message);
}

template <class Callable>
void require_throws(Callable&& callable, const std::string& message) {
  try {
    callable();
  } catch (const EngineError&) {
    return;
  }
  throw std::runtime_error(message);
}

ContractAuthority authority() {
  return {
      std::string{kPhase6BuildAuthoritySha256},
      std::string{kPhase6TopologyContractSha256},
      std::string{kPhase6CdcContractSha256},
      std::string{kPhase6P2pContractSha256},
      std::string{kPhase6UmaContractSha256},
      std::string{kPhase6CheckpointContractSha256},
      std::string{kPhase6ClaimBoundarySha256},
      std::string{kPhase5LedgerSha256},
      std::string{kPhase5ReviewLedgerSha256},
      std::string{kPhase5ReviewAggregateSha256}};
}

Clock clock(const std::string& id, const std::uint64_t hz) {
  return Clock{id, hz, 1, 0, 0, 0};
}

ComputeDomain compute_domain(
    const std::string& id, const std::string& memory_id,
    const std::string& clock_id, const std::uint64_t hz = 1'000'000'000) {
  return {
      id, memory_id, clock(clock_id, hz), 1,
      U128{1'000'000}, U128{1}, U128{1}};
}

DirectedLink directed_link(
    const std::string& id, const std::string& source,
    const std::string& target, const std::string& source_clock,
    const std::string& target_clock, const bool full_duplex = true,
    const std::uint64_t queue_capacity = 2) {
  return {
      id,
      source,
      target,
      Bridge{
          "bridge-" + id, source_clock, target_clock,
          BridgeProtocol::kCredit, U128{13}, U128{17}, 1, 1,
          queue_capacity, BackpressurePolicy::kCreditBlock},
      U128{1'000'000'000}, U128{1}, U128{1}, 1, "nvlink0",
      full_duplex, queue_capacity};
}

SyntheticClockAlignment alignment(const std::string& clock_id) {
  return {
      clock_id, "SIMULATOR_EXACT_SYNTHETIC", U128{0}, U128{0}, U128{0},
      "CYCLE_GRADE", "SIMULATOR_INTERNAL_NOT_HARDWARE_ALIGNMENT",
      U128{0}, U128{1'000'000'000'000'000ULL}};
}

Topology discrete_topology(
    const bool full_duplex = true,
    const U128& vram0 = U128{4096},
    const U128& vram1 = U128{4096}) {
  return {
      "synthetic-discrete",
      PlatformMode::kDiscreteP2p2Gpu,
      {
          compute_domain("gpu0", "vram0", "clk0", 1'000'000'003),
          compute_domain("gpu1", "vram1", "clk1", 900'000'011),
      },
      {{"vram0", vram0}, {"vram1", vram1}},
      {
          directed_link(
              "link01", "gpu0", "gpu1", "clk0", "clk1",
              full_duplex),
          directed_link(
              "link10", "gpu1", "gpu0", "clk1", "clk0",
              full_duplex),
      },
      std::nullopt,
      {alignment("clk0"), alignment("clk1")},
      authority()};
}

Topology uma_topology(const U128& capacity = U128{4096}) {
  return {
      "synthetic-uma",
      PlatformMode::kCoherentUma2Compute,
      {
          compute_domain("cpu0", "uma0", "cpuclk0", 1'000'000'003),
          compute_domain("cpu1", "uma0", "cpuclk1", 800'000'011),
      },
      {{"uma0", capacity}},
      {},
      UmaFabric{
          "fabric0", "uma0", clock("fabricclk", 700'000'009),
          1, U128{1'000'000'000}, U128{1}, U128{1}, 2,
          BridgeProtocol::kCredit, 2, U128{11}, U128{19}, 1, 1},
      {
          alignment("cpuclk0"), alignment("cpuclk1"),
          alignment("fabricclk"),
      },
      authority()};
}

EventKey key(
    const std::string& id, const std::uint64_t token,
    const std::uint32_t priority = 100,
    const U128& time = U128{0}) {
  return EventKey{
      time, priority, std::string{"req"}, token, std::uint32_t{0},
      "phase6-test", id};
}

TrafficPayloadProfile payload() {
  TrafficPayloadProfile result{
      "synthetic-v1", U128{64}, U128{1024}, U128{128},
      U128{256}, U128{32}, Fidelity::kFunctionalOnly,
      RangeStatus::kRangeUnknown, ""};
  result.profile_sha256 =
      MultiDomainSchedulerV1::traffic_profile_digest(result);
  return result;
}

InitialObject initial_object(
    const std::string& id,
    const U128& bytes,
    const bool immutable,
    std::set<std::string> locations,
    const std::uint32_t pins,
    const std::uint64_t version,
    const CoherenceState coherence_state,
    std::optional<std::string> owner,
    std::set<std::string> sharers,
    std::optional<phase5::ExpertKey> expert = std::nullopt,
    const char content_hash_digit = 'a') {
  return InitialObject{
      id,
      bytes,
      immutable,
      expert,
      expert.has_value() ? std::string(64, content_hash_digit) : "",
      std::move(locations),
      pins,
      version,
      coherence_state,
      std::move(owner),
      std::move(sharers)};
}

void finalize_program_authority(Program& program) {
  std::set<std::string> source_labels;
  for (const auto& operation : program.operations) {
    source_labels.insert(
        operation.source_phase5_action_ids.begin(),
        operation.source_phase5_action_ids.end());
  }
  std::map<std::string, std::string> label_to_action;
  program.phase5_actions.clear();
  for (const auto& label : source_labels) {
    phase5::CompiledAction action{
        "",
        phase5::ActionKind::kRouteBarrier,
        std::nullopt,
        label,
        {},
        phase4::ServiceClass::kCompute,
        U128{1},
        U128{0},
        false};
    action.action_id =
        MultiDomainSchedulerV1::phase5_compiled_action_id(
            program.phase5_plan_digest, program.phase5_actions.size(),
            action);
    label_to_action.emplace(label, action.action_id);
    program.phase5_actions.push_back(std::move(action));
  }
  for (auto& operation : program.operations) {
    for (auto& action_id : operation.source_phase5_action_ids) {
      action_id = label_to_action.at(action_id);
    }
  }
  program.allowed_phase5_action_ids.clear();
  for (const auto& action : program.phase5_actions) {
    program.allowed_phase5_action_ids.push_back(action.action_id);
  }
  program.phase5_action_digest =
      MultiDomainSchedulerV1::phase5_action_set_digest(
          program.allowed_phase5_action_ids);
}

Operation operation(
    const std::string& id,
    const OperationKind kind,
    const std::string& demand,
    const std::optional<phase5::ExpertKey>& expert,
    const std::string& source,
    const std::string& target,
    std::vector<std::string> dependencies,
    const std::string& object = "",
    const U128& bytes = U128{64},
    const U128& work = U128{32},
    const std::uint64_t token = 0,
    const std::uint64_t expected_version = 0,
    const std::uint64_t output_version = 0) {
  return Operation{
      key(
          id, token,
          (kind == OperationKind::kActivationDispatch ||
           kind == OperationKind::kActivationReturn ||
           kind == OperationKind::kExpertReplicate ||
           kind == OperationKind::kExpertMove ||
           kind == OperationKind::kUmaImmutableRead ||
           kind == OperationKind::kUmaMutableAcquireWrite ||
           kind == OperationKind::kUmaMutableRelease ||
           kind == OperationKind::kUmaMutableRead)
              ? 90
              : 100),
      std::move(dependencies),
      {"phase5-action-" + demand},
      kind,
      demand,
      expert,
      source,
      target,
      object,
      bytes,
      work,
      expected_version,
      output_version};
}

Program discrete_program() {
  const phase5::ExpertKey local{0, 0};
  const phase5::ExpertKey remote{0, 1};
  Program result{};
  result.program_id = "discrete-program";
  result.phase5_plan_digest = std::string(64, '1');
  result.phase5_ledger_sha256 = std::string{kPhase5LedgerSha256};
  result.payload_profile = payload();
  result.initial_objects = {
      initial_object(
          "expert0", U128{1024}, true, {"vram0"}, 0, 0,
          CoherenceState::kShared, std::nullopt, {}, local, 'a'),
      initial_object(
          "expert1", U128{1024}, true, {"vram1"}, 0, 0,
          CoherenceState::kShared, std::nullopt, {}, remote, 'b'),
  };
  result.routing = {
          {"d0", key("route0", 0), 2, "gpu0", {local, remote},
           {{local, "gpu0"}, {remote, "gpu1"}}, "phase5-routing"},
  };
  result.operations = {
          operation(
              "local-compute", OperationKind::kExpertCompute, "d0",
              local, "gpu0", "gpu0", {}, "expert0"),
          operation(
              "dispatch", OperationKind::kActivationDispatch, "d0",
              remote, "gpu0", "gpu1", {}, "", U128{64}),
          operation(
              "remote-compute", OperationKind::kExpertCompute, "d0",
              remote, "gpu1", "gpu1", {"dispatch"}, "expert1"),
          operation(
              "return", OperationKind::kActivationReturn, "d0", remote,
              "gpu1", "gpu0", {"remote-compute"}, "", U128{64}),
          operation(
              "combine", OperationKind::kTokenCombine, "d0",
              std::nullopt, "gpu0", "gpu0",
              {"local-compute", "return"}, "", U128{1}, U128{1}),
  };
  finalize_program_authority(result);
  return result;
}

Program uma_program() {
  Program result{};
  result.program_id = "uma-program";
  result.phase5_plan_digest = std::string(64, '3');
  result.phase5_ledger_sha256 = std::string{kPhase5LedgerSha256};
  result.payload_profile = payload();
  result.initial_objects = {
      initial_object(
          "weights", U128{128}, true, {"cpu0"}, 0, 0,
          CoherenceState::kShared, std::nullopt, {"cpu0"}),
      initial_object(
          "mutable", U128{256}, false, {}, 0, 0,
          CoherenceState::kUncached, std::nullopt, {}),
  };
  result.operations = {
          operation(
              "immutable-read", OperationKind::kUmaImmutableRead, "",
              std::nullopt, "cpu0", "cpu0", {}, "weights", U128{128}),
          operation(
              "write", OperationKind::kUmaMutableAcquireWrite, "",
              std::nullopt, "cpu0", "cpu0", {}, "mutable", U128{256},
              U128{1}, 0, 0, 1),
          operation(
              "release", OperationKind::kUmaMutableRelease, "",
              std::nullopt, "cpu0", "cpu0", {"write"}, "mutable",
              U128{256}, U128{1}, 0, 1, 1),
          operation(
              "read", OperationKind::kUmaMutableRead, "",
              std::nullopt, "cpu1", "cpu1", {"release"}, "mutable",
              U128{256}, U128{1}, 0, 1, 1),
  };
  finalize_program_authority(result);
  return result;
}

void smoke() {
  MultiDomainSchedulerV1 model(discrete_topology(), discrete_program());
  const Result result = model.run_until_quiescent();
  require(result.terminal_status == TerminalStatus::kQuiescent, "not done");
  require(result.metrics.computes == 2, "compute conservation");
  require(result.metrics.dispatches == 1, "dispatch conservation");
  require(result.metrics.returns == 1, "return conservation");
  require(result.metrics.combines == 1, "combine conservation");
  require(!result.calibration_pass && !result.gpu_used, "claim boundary");
}

void uma_smoke() {
  MultiDomainSchedulerV1 model(uma_topology(), uma_program());
  const Result result = model.run_until_quiescent();
  require(result.metrics.uma_reads == 2, "UMA reads");
  require(result.metrics.uma_writes == 1, "UMA writes");
  require(
      result.terminal_objects.at("mutable").version == 1,
      "UMA version");
}

Program placement_program(
    std::vector<InitialObject> objects,
    std::vector<Operation> operations,
    const std::string& id = "placement-program") {
  Program result{};
  result.program_id = id;
  result.phase5_plan_digest = std::string(64, '5');
  result.phase5_ledger_sha256 = std::string{kPhase5LedgerSha256};
  result.payload_profile = payload();
  result.initial_objects = std::move(objects);
  result.operations = std::move(operations);
  finalize_program_authority(result);
  return result;
}

const ScheduleEntry& entry(const Result& result, const std::string& id) {
  const auto found = std::find_if(
      result.entries.begin(), result.entries.end(),
      [&](const auto& item) { return item.operation_id == id; });
  if (found == result.entries.end()) {
    throw std::runtime_error("missing schedule entry: " + id);
  }
  return *found;
}

void authority_topology_and_profile_rejection() {
  Topology bad_authority = discrete_topology();
  bad_authority.authority.phase5_review_ledger_sha256 =
      std::string(64, '0');
  require_throws(
      [&] {
        MultiDomainSchedulerV1 model(
            std::move(bad_authority), discrete_program());
      },
      "invalid exact authority accepted");

  Topology missing_reverse = discrete_topology();
  missing_reverse.links.pop_back();
  require_throws(
      [&] {
        MultiDomainSchedulerV1 model(
            std::move(missing_reverse), discrete_program());
      },
      "missing reverse path accepted");

  Topology wrong_direction = discrete_topology();
  wrong_direction.links[1].source_compute_id = "gpu0";
  wrong_direction.links[1].target_compute_id = "gpu1";
  require_throws(
      [&] {
        MultiDomainSchedulerV1 model(
            std::move(wrong_direction), discrete_program());
      },
      "duplicate direction accepted");

  Topology missing_alignment = discrete_topology();
  missing_alignment.synthetic_alignments.pop_back();
  require_throws(
      [&] {
        MultiDomainSchedulerV1 model(
            std::move(missing_alignment), discrete_program());
      },
      "missing synthetic alignment accepted");

  Program bad_profile = discrete_program();
  bad_profile.payload_profile.profile_sha256 = std::string(64, 'a');
  require_throws(
      [&] {
        MultiDomainSchedulerV1 model(
            discrete_topology(), std::move(bad_profile));
      },
      "caller-labeled payload hash accepted");

  Topology duplicate_link = discrete_topology();
  duplicate_link.links[1].link_id = duplicate_link.links[0].link_id;
  require_throws(
      [&] {
        MultiDomainSchedulerV1 model(
            std::move(duplicate_link), discrete_program());
      },
      "duplicate link ID accepted");

  Topology duplicate_bridge = discrete_topology();
  duplicate_bridge.links[1].bridge.bridge_id =
      duplicate_bridge.links[0].bridge.bridge_id;
  require_throws(
      [&] {
        MultiDomainSchedulerV1 model(
            std::move(duplicate_bridge), discrete_program());
      },
      "duplicate bridge ID accepted");

  Topology split_duplex_group = discrete_topology(false);
  split_duplex_group.links[1].duplex_group = "nvlink-other";
  require_throws(
      [&] {
        MultiDomainSchedulerV1 model(
            std::move(split_duplex_group), discrete_program());
      },
      "half-duplex reverse-pair group split accepted");

  Program tampered_action = discrete_program();
  tampered_action.phase5_actions[0].source_id += "-tampered";
  require_throws(
      [&] {
        MultiDomainSchedulerV1 model(
            discrete_topology(), std::move(tampered_action));
      },
      "Phase 5 action preimage tamper accepted");

  Program unbound_expert = discrete_program();
  unbound_expert.initial_objects[0].expert = phase5::ExpertKey{9, 9};
  require_throws(
      [&] {
        MultiDomainSchedulerV1 model(
            discrete_topology(), std::move(unbound_expert));
      },
      "selected ExpertKey accepted without exact object binding");

  Program bad_content = discrete_program();
  bad_content.initial_objects[0].content_sha256 = "caller-label";
  require_throws(
      [&] {
        MultiDomainSchedulerV1 model(
            discrete_topology(), std::move(bad_content));
      },
      "non-cryptographic expert content identity accepted");
}

void trace_priority_and_alignment_atomicity() {
  MultiDomainSchedulerV1 complete(
      discrete_topology(), discrete_program());
  const Result result = complete.run_until_quiescent();
  std::size_t acknowledge_count = 0;
  for (const auto& trace : result.trace) {
    if (trace.kind == TraceKind::kAckOrCredit) {
      require(
          trace.key.event_priority == 40,
          "ACK_OR_CREDIT did not use frozen priority 40");
      ++acknowledge_count;
    }
    if (trace.kind == TraceKind::kComplete) {
      require(
          trace.key.event_priority == 30,
          "generic completion did not use COMPUTE_COMPLETE priority 30");
      require(
          trace.operation_id == "local-compute" ||
              trace.operation_id == "remote-compute" ||
              trace.operation_id == "combine",
          "crossing operation emitted a generic completion trace");
    }
  }
  require(
      acknowledge_count == 2 &&
          result.metrics.ack_or_credit_events == acknowledge_count,
      "crossing acknowledge conservation failed");

  Topology short_alignment = discrete_topology();
  for (auto& item : short_alignment.synthetic_alignments) {
    item.valid_end_fs = U128{1};
  }
  MultiDomainSchedulerV1 fail_atomic(
      std::move(short_alignment), discrete_program());
  const std::string before = fail_atomic.serialize_checkpoint();
  require_throws(
      [&] { static_cast<void>(fail_atomic.step()); },
      "out-of-range crossing timing was accepted");
  require(
      fail_atomic.serialize_checkpoint() == before,
      "failed admission mutated scheduler state");
}

void uma_serialization_and_invariants() {
  Program invalid_shared = uma_program();
  invalid_shared.initial_objects[1].coherence_state =
      CoherenceState::kShared;
  invalid_shared.initial_objects[1].owner = "cpu0";
  require_throws(
      [&] {
        MultiDomainSchedulerV1 model(
            uma_topology(), std::move(invalid_shared));
      },
      "SHARED object with owner accepted");

  Program overflow = uma_program();
  overflow.initial_objects.erase(overflow.initial_objects.begin());
  overflow.operations.erase(
      std::remove_if(
          overflow.operations.begin(), overflow.operations.end(),
          [](const auto& item) { return item.key.event_id != "write"; }),
      overflow.operations.end());
  overflow.operations[0].dependencies.clear();
  overflow.initial_objects[0].version =
      std::numeric_limits<std::uint64_t>::max();
  for (auto& item : overflow.operations) {
    if (item.object_id == "mutable") {
      item.expected_version = std::numeric_limits<std::uint64_t>::max();
      item.output_version = std::numeric_limits<std::uint64_t>::max();
    }
  }
  MultiDomainSchedulerV1 version_guard(
      uma_topology(), std::move(overflow));
  const std::string before = version_guard.serialize_checkpoint();
  require_throws(
      [&] { static_cast<void>(version_guard.step()); },
      "uint64 UMA version increment overflow accepted");
  require(
      version_guard.serialize_checkpoint() == before,
      "version-overflow rejection was not fail-atomic");

  Program serialized{};
  serialized.program_id = "uma-same-object-serialization";
  serialized.phase5_plan_digest = std::string(64, '9');
  serialized.phase5_ledger_sha256 = std::string{kPhase5LedgerSha256};
  serialized.payload_profile = payload();
  serialized.initial_objects = {
      initial_object(
          "weights", U128{128}, true, {"cpu0", "cpu1"}, 0, 0,
          CoherenceState::kShared, std::nullopt, {"cpu0", "cpu1"}),
  };
  serialized.operations = {
      operation(
          "read-a", OperationKind::kUmaImmutableRead, "", std::nullopt,
          "cpu0", "cpu0", {}, "weights", U128{128}, U128{1}, 0),
      operation(
          "read-b", OperationKind::kUmaImmutableRead, "", std::nullopt,
          "cpu1", "cpu1", {}, "weights", U128{128}, U128{1}, 1),
  };
  finalize_program_authority(serialized);
  Topology topology = uma_topology();
  topology.uma_fabric->lanes = 2;
  MultiDomainSchedulerV1 same_object(
      std::move(topology), std::move(serialized));
  const Result serialization_result =
      same_object.run_until_quiescent();
  const auto& first = entry(serialization_result, "read-a");
  const auto& second = entry(serialization_result, "read-b");
  require(
      std::max(first.start_fs, second.start_fs) >=
          std::min(first.completion_fs, second.completion_fs),
      "same-object UMA operations overlapped");
}

Program opposite_transfers_program() {
  const phase5::ExpertKey expert_a{0, 0};
  const phase5::ExpertKey expert_b{0, 1};
  return placement_program(
      {
          initial_object(
              "expert-a", U128{1024}, true, {"vram0"}, 0, 0,
              CoherenceState::kShared, std::nullopt, {}, expert_a, 'a'),
          initial_object(
              "expert-b", U128{1024}, true, {"vram1"}, 0, 0,
              CoherenceState::kShared, std::nullopt, {}, expert_b, 'b'),
      },
      {
          operation(
              "replicate-a", OperationKind::kExpertReplicate, "p0",
              expert_a, "gpu0", "gpu1", {}, "expert-a", U128{1024}),
          operation(
              "replicate-b", OperationKind::kExpertReplicate, "p1",
              expert_b, "gpu1", "gpu0", {}, "expert-b", U128{1024},
              U128{32}, 1),
      });
}

void duplex_credit_capacity_and_move() {
  MultiDomainSchedulerV1 full(
      discrete_topology(true), opposite_transfers_program());
  const Result full_result = full.run_until_quiescent();
  MultiDomainSchedulerV1 half(
      discrete_topology(false), opposite_transfers_program());
  const Result half_result = half.run_until_quiescent();
  require(
      entry(full_result, "replicate-a").start_fs ==
          entry(full_result, "replicate-b").start_fs,
      "full duplex directions did not overlap");
  require(
      entry(half_result, "replicate-a").start_fs !=
          entry(half_result, "replicate-b").start_fs,
      "half duplex directions overlapped");
  require(
      half_result.makespan_fs > full_result.makespan_fs,
      "half duplex did not serialize");
  const phase6::Checkpoint full_checkpoint = full.checkpoint();
  for (const auto& [id, link] : full_checkpoint.links) {
    static_cast<void>(id);
    require(
        link.queue_occupancy == 0 &&
            link.requests_started == link.forward_completions &&
            link.forward_completions == link.acknowledgements &&
            link.requests_started == link.credits_returned &&
            link.credits_available == 2,
        "finite queue/credit conservation failed");
  }
  Topology request_ack_topology = discrete_topology();
  for (auto& item : request_ack_topology.links) {
    item.bridge.protocol = BridgeProtocol::kRequestAck;
    item.bridge.backpressure_policy = BackpressurePolicy::kStallSource;
    item.initial_credits = 0;
  }
  MultiDomainSchedulerV1 request_ack(
      std::move(request_ack_topology), opposite_transfers_program());
  static_cast<void>(request_ack.run_until_quiescent());
  for (const auto& [id, link] : request_ack.checkpoint().links) {
    static_cast<void>(id);
    require(
        link.requests_started == link.acknowledgements &&
            link.credits_available == 0 && link.credits_returned == 0,
        "REQUEST_ACK conservation failed");
  }

  Program overflow = placement_program(
      {
          initial_object(
              "a", U128{1024}, true, {"vram0"}, 0, 0,
              CoherenceState::kShared, std::nullopt, {},
              phase5::ExpertKey{0, 0}, 'a'),
          initial_object(
              "b", U128{1024}, true, {"vram0"}, 0, 0,
              CoherenceState::kShared, std::nullopt, {},
              phase5::ExpertKey{0, 1}, 'b'),
      },
      {
          operation(
              "copy-a", OperationKind::kExpertReplicate, "a",
              phase5::ExpertKey{0, 0}, "gpu0", "gpu1", {}, "a",
              U128{1024}),
          operation(
              "copy-b", OperationKind::kExpertReplicate, "b",
              phase5::ExpertKey{0, 1}, "gpu0", "gpu1", {}, "b",
              U128{1024},
              U128{32}, 1),
      });
  require_throws(
      [&] {
        MultiDomainSchedulerV1 model(
            discrete_topology(true, U128{4096}, U128{1024}),
            std::move(overflow));
        static_cast<void>(model.run_until_quiescent());
      },
      "per-domain overflow accepted because aggregate capacity fit");

  Program move_program = placement_program(
      {
          initial_object(
              "movable", U128{1024}, true, {"vram0"}, 0, 0,
              CoherenceState::kShared, std::nullopt, {},
              phase5::ExpertKey{0, 0}, 'a'),
      },
      {
          operation(
              "move", OperationKind::kExpertMove, "m",
              phase5::ExpertKey{0, 0}, "gpu0", "gpu1", {}, "movable",
              U128{1024}),
      });
  MultiDomainSchedulerV1 move(
      discrete_topology(), std::move(move_program));
  const Result move_result = move.run_until_quiescent();
  require(
      move_result.terminal_objects.at("movable").locations ==
          std::set<std::string>{"vram1"},
      "whole-expert MOVE commit was not atomic");

  Program pinned = placement_program(
      {
          initial_object(
              "pinned", U128{1024}, true, {"vram0"}, 1, 0,
              CoherenceState::kShared, std::nullopt, {},
              phase5::ExpertKey{0, 0}, 'a'),
      },
      {
          operation(
              "move", OperationKind::kExpertMove, "m",
              phase5::ExpertKey{0, 0}, "gpu0", "gpu1", {}, "pinned",
              U128{1024}),
      });
  require_throws(
      [&] {
        MultiDomainSchedulerV1 model(
            discrete_topology(), std::move(pinned));
      },
      "pinned MOVE was not hard-rejected");

  Program live_pin = discrete_program();
  const phase5::ExpertKey local{0, 0};
  live_pin.routing[0].top_k = 1;
  live_pin.routing[0].selected_experts = {local};
  live_pin.routing[0].assigned_compute_domains = {{local, "gpu0"}};
  live_pin.operations.erase(
      std::remove_if(
          live_pin.operations.begin(), live_pin.operations.end(),
          [](const auto& item) {
            return item.key.event_id == "dispatch" ||
                   item.key.event_id == "remote-compute" ||
                   item.key.event_id == "return";
          }),
      live_pin.operations.end());
  for (auto& item : live_pin.operations) {
    if (item.key.event_id == "combine") {
      item.dependencies = {"local-compute"};
    }
  }
  Operation concurrent_move = operation(
      "concurrent-move", OperationKind::kExpertMove, "move",
      local, "gpu0", "gpu1", {}, "expert0", U128{1024});
  concurrent_move.key.time_fs = U128{1};
  concurrent_move.source_phase5_action_ids = {
      live_pin.allowed_phase5_action_ids.front()};
  live_pin.operations.push_back(std::move(concurrent_move));
  require_throws(
      [&] {
        MultiDomainSchedulerV1 model(
            discrete_topology(), std::move(live_pin));
        static_cast<void>(model.run_until_quiescent());
      },
      "MOVE admission ignored a live compute pin");
}

void routing_chain_and_permutation() {
  Program empty_request_identity = discrete_program();
  empty_request_identity.routing[0].route_key.request_id = "";
  require_throws(
      [&] {
        MultiDomainSchedulerV1 model(
            discrete_topology(), std::move(empty_request_identity));
      },
      "empty request sentinel accepted as token identity");

  Program token_sentinel = discrete_program();
  token_sentinel.routing[0].route_key.token_index =
      std::numeric_limits<std::uint64_t>::max();
  require_throws(
      [&] {
        MultiDomainSchedulerV1 model(
            discrete_topology(), std::move(token_sentinel));
      },
      "UINT64_MAX token sentinel accepted as token identity");

  Program layer_sentinel = discrete_program();
  layer_sentinel.routing[0].route_key.layer_index =
      std::numeric_limits<std::uint32_t>::max();
  require_throws(
      [&] {
        MultiDomainSchedulerV1 model(
            discrete_topology(), std::move(layer_sentinel));
      },
      "UINT32_MAX layer sentinel accepted as token identity");

  Program missing_token_identity = discrete_program();
  missing_token_identity.routing[0].route_key.token_index.reset();
  require_throws(
      [&] {
        MultiDomainSchedulerV1 model(
            discrete_topology(), std::move(missing_token_identity));
      },
      "routing binding without token_index accepted");

  Program route_layer_mismatch = discrete_program();
  route_layer_mismatch.routing[0].route_key.layer_index = 1;
  require_throws(
      [&] {
        MultiDomainSchedulerV1 model(
            discrete_topology(), std::move(route_layer_mismatch));
      },
      "selected experts accepted across route layers");

  Program operation_token_mismatch = discrete_program();
  for (auto& item : operation_token_mismatch.operations) {
    if (item.key.event_id == "combine") {
      item.key.token_index = 99;
    }
  }
  require_throws(
      [&] {
        MultiDomainSchedulerV1 model(
            discrete_topology(), std::move(operation_token_mismatch));
      },
      "combine accepted with a different route token identity");

  Program broken = discrete_program();
  for (auto& item : broken.operations) {
    if (item.key.event_id == "return") item.dependencies.clear();
  }
  require_throws(
      [&] {
        MultiDomainSchedulerV1 model(
            discrete_topology(), std::move(broken));
      },
      "broken remote return chain accepted");

  Program phantom = discrete_program();
  const phase5::ExpertKey local{0, 0};
  phantom.operations.push_back(
      operation(
          "local-phantom", OperationKind::kActivationDispatch, "d0",
          local, "gpu0", "gpu1", {}));
  require_throws(
      [&] {
        MultiDomainSchedulerV1 model(
            discrete_topology(), std::move(phantom));
      },
      "local phantom transfer accepted");

  Topology topology_a = discrete_topology();
  Program program_a = discrete_program();
  MultiDomainSchedulerV1 first(topology_a, program_a);
  const Result first_result = first.run_until_quiescent();
  std::reverse(topology_a.compute_domains.begin(), topology_a.compute_domains.end());
  std::reverse(topology_a.memory_domains.begin(), topology_a.memory_domains.end());
  std::reverse(topology_a.links.begin(), topology_a.links.end());
  std::reverse(
      topology_a.synthetic_alignments.begin(),
      topology_a.synthetic_alignments.end());
  std::reverse(program_a.initial_objects.begin(), program_a.initial_objects.end());
  std::reverse(program_a.operations.begin(), program_a.operations.end());
  MultiDomainSchedulerV1 second(std::move(topology_a), std::move(program_a));
  const Result second_result = second.run_until_quiescent();
  require(
      first_result.semantic_digest == second_result.semantic_digest,
      "input permutation changed global schedule");

  Program tied_routes_a = discrete_program();
  RoutingBinding tied_route = tied_routes_a.routing[0];
  tied_route.demand_id = "d1";
  tied_routes_a.routing.push_back(std::move(tied_route));
  const std::vector<Operation> original_operations =
      tied_routes_a.operations;
  for (const auto& original : original_operations) {
    Operation clone = original;
    clone.demand_id = "d1";
    clone.key.event_id += "-d1";
    for (auto& dependency : clone.dependencies) {
      dependency += "-d1";
    }
    tied_routes_a.operations.push_back(std::move(clone));
  }
  Program tied_routes_b = tied_routes_a;
  std::reverse(
      tied_routes_b.routing.begin(), tied_routes_b.routing.end());
  MultiDomainSchedulerV1 tied_a(
      discrete_topology(), std::move(tied_routes_a));
  MultiDomainSchedulerV1 tied_b(
      discrete_topology(), std::move(tied_routes_b));
  require(
      tied_a.program_digest() == tied_b.program_digest(),
      "equal route-key ordering depended on input order instead of demand_id");

  Program grouping_a = opposite_transfers_program();
  Program grouping_b = grouping_a;
  const auto& action_a = grouping_a.allowed_phase5_action_ids[0];
  const auto& action_b = grouping_a.allowed_phase5_action_ids[1];
  grouping_a.operations[0].source_phase5_action_ids = {action_a, action_b};
  grouping_b.operations[0].source_phase5_action_ids = {action_a};
  grouping_b.operations[1].source_phase5_action_ids = {action_b};
  MultiDomainSchedulerV1 digest_a(discrete_topology(), grouping_a);
  MultiDomainSchedulerV1 digest_b(discrete_topology(), grouping_b);
  require(
      digest_a.program_digest() != digest_b.program_digest(),
      "variable-group structural digest collision");
}

void cdc_and_checkpoint() {
  const Clock noninteger = clock("fractional", 1'000'000'003);
  const std::uint64_t cycle = 1'000'000;
  const U128 edge = noninteger.edge_time(cycle);
  require(
      noninteger.ceil_edge(edge) == cycle &&
          noninteger.remainder(cycle) ==
              static_cast<std::uint64_t>(
                  (boost::multiprecision::cpp_int{cycle} *
                   boost::multiprecision::cpp_int{
                       1'000'000'000'000'000ULL}) %
                  boost::multiprecision::cpp_int{1'000'000'003}),
      "non-integer clock drifted from rational reference");

  Topology topology = discrete_topology();
  Program program = opposite_transfers_program();
  MultiDomainSchedulerV1 continuous(topology, program);
  static_cast<void>(continuous.step());
  const phase6::Checkpoint checkpoint = continuous.checkpoint();
  require(
      !checkpoint.active.empty() &&
          checkpoint.links.at("link01").queue_occupancy +
                  checkpoint.links.at("link10").queue_occupancy ==
              1,
      "checkpoint was not captured mid-transfer/credit");
  const std::string wire = continuous.serialize_checkpoint();
  MultiDomainSchedulerV1 object_restored =
      MultiDomainSchedulerV1::restore(topology, program, checkpoint);
  MultiDomainSchedulerV1 wire_restored =
      MultiDomainSchedulerV1::restore_serialized(topology, program, wire);
  const Result continuous_result = continuous.run_until_quiescent();
  require(
      object_restored.run_until_quiescent().semantic_digest ==
              continuous_result.semantic_digest &&
          wire_restored.run_until_quiescent().semantic_digest ==
              continuous_result.semantic_digest,
      "mid-transfer checkpoint continuation diverged");

  phase6::Checkpoint tampered = checkpoint;
  tampered.memory_occupancy["vram0"] += 1;
  require_throws(
      [&] {
        static_cast<void>(MultiDomainSchedulerV1::restore(
            topology, program, tampered));
      },
      "object checkpoint tamper accepted");
  require_throws(
      [&] {
        static_cast<void>(MultiDomainSchedulerV1::restore_serialized(
            topology, program, wire + "x"));
      },
      "wire trailing byte accepted");
  Topology mismatch = topology;
  mismatch.memory_domains[0].capacity_bytes += 1;
  require_throws(
      [&] {
        static_cast<void>(MultiDomainSchedulerV1::restore(
            mismatch, program, checkpoint));
      },
      "checkpoint topology mismatch accepted");
}

void uma_boundaries_and_checkpoint() {
  Program alias = uma_program();
  alias.initial_objects[0].locations = {"cpu0", "cpu1"};
  MultiDomainSchedulerV1 alias_model(uma_topology(U128{384}), alias);
  const Result alias_result = alias_model.run_until_quiescent();
  require(
      alias_result.terminal_memory_occupancy.at("uma0") == U128{384},
      "UMA aliases were counted more than once");
  require(
      std::none_of(
          alias_result.entries.begin(), alias_result.entries.end(),
          [](const auto& item) {
            return std::any_of(
                item.resource_ids.begin(), item.resource_ids.end(),
                [](const auto& id) { return id.starts_with("link:"); });
          }),
      "phantom P2P resource appeared in UMA mode");

  Program immutable_write = uma_program();
  for (auto& item : immutable_write.operations) {
    if (item.key.event_id == "write") {
      item.object_id = "weights";
      item.bytes = U128{128};
    }
  }
  require_throws(
      [&] {
        MultiDomainSchedulerV1 model(
            uma_topology(), std::move(immutable_write));
        static_cast<void>(model.run_until_quiescent());
      },
      "immutable UMA write accepted");

  Program stale = uma_program();
  for (auto& item : stale.operations) {
    if (item.key.event_id == "read") item.expected_version = 0;
  }
  require_throws(
      [&] {
        MultiDomainSchedulerV1 model(
            uma_topology(), std::move(stale));
        static_cast<void>(model.run_until_quiescent());
      },
      "stale UMA read accepted");

  Topology topology = uma_topology();
  Program program = uma_program();
  MultiDomainSchedulerV1 continuous(topology, program);
  static_cast<void>(continuous.step());
  const phase6::Checkpoint checkpoint = continuous.checkpoint();
  require(
      !checkpoint.active.empty() &&
          checkpoint.links.at("uma:fabric0").queue_occupancy == 1,
      "checkpoint was not captured mid-UMA access");
  const std::string wire = continuous.serialize_checkpoint();
  MultiDomainSchedulerV1 object_restored =
      MultiDomainSchedulerV1::restore(topology, program, checkpoint);
  MultiDomainSchedulerV1 wire_restored =
      MultiDomainSchedulerV1::restore_serialized(topology, program, wire);
  const std::string expected =
      continuous.run_until_quiescent().semantic_digest;
  require(
      object_restored.run_until_quiescent().semantic_digest == expected &&
          wire_restored.run_until_quiescent().semantic_digest == expected,
      "mid-UMA checkpoint continuation diverged");
}

void thousand_operation_envelope() {
  Program program{};
  program.program_id = "scale-1000";
  program.phase5_plan_digest = std::string(64, '7');
  program.phase5_ledger_sha256 = std::string{kPhase5LedgerSha256};
  program.payload_profile = payload();
  program.initial_objects = {
      initial_object(
          "weights", U128{128}, true, {"cpu0", "cpu1"}, 0, 0,
          CoherenceState::kShared, std::nullopt, {"cpu0", "cpu1"}),
  };
  for (std::uint64_t index = 0; index < 1000; ++index) {
    program.operations.push_back(
        operation(
            "read-" + std::to_string(index),
            OperationKind::kUmaImmutableRead, "", std::nullopt,
            index % 2 == 0 ? "cpu0" : "cpu1",
            index % 2 == 0 ? "cpu0" : "cpu1", {}, "weights",
            U128{128}, U128{1}, index));
  }
  finalize_program_authority(program);
  MultiDomainSchedulerV1 model(uma_topology(), std::move(program));
  const Result result = model.run_until_quiescent();
  require(
      result.metrics.uma_reads == 1000 &&
          result.metrics.trace_events == 3000 &&
          result.metrics.scheduler_key_comparisons <= 1'100'000,
      "1000-operation complexity envelope exceeded: reads=" +
          std::to_string(result.metrics.uma_reads) + " trace=" +
          std::to_string(result.metrics.trace_events) + " comparisons=" +
          std::to_string(result.metrics.scheduler_key_comparisons));
}

}  // namespace

int main() {
  try {
    const auto run = [](const std::string& name, const auto& test) {
      try {
        test();
      } catch (const std::exception& error) {
        throw std::runtime_error(name + ": " + error.what());
      }
    };
    run("smoke", smoke);
    run("uma_smoke", uma_smoke);
    run(
        "authority_topology_and_profile_rejection",
        authority_topology_and_profile_rejection);
    run(
        "trace_priority_and_alignment_atomicity",
        trace_priority_and_alignment_atomicity);
    run(
        "uma_serialization_and_invariants",
        uma_serialization_and_invariants);
    run("duplex_credit_capacity_and_move", duplex_credit_capacity_and_move);
    run("routing_chain_and_permutation", routing_chain_and_permutation);
    run("cdc_and_checkpoint", cdc_and_checkpoint);
    run("uma_boundaries_and_checkpoint", uma_boundaries_and_checkpoint);
    run("thousand_operation_envelope", thousand_operation_envelope);
    std::cout << "phase6 tests: PASS\n";
    return 0;
  } catch (const std::exception& error) {
    std::cerr << "phase6 tests: FAIL: " << error.what() << '\n';
    return 1;
  }
}

#include "moe_sim/engine.hpp"

#include <algorithm>
#include <functional>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

namespace {

using moe_sim::Action;
using moe_sim::Arbitration;
using moe_sim::BackpressurePolicy;
using moe_sim::Bridge;
using moe_sim::BridgeProtocol;
using moe_sim::Checkpoint;
using moe_sim::Clock;
using moe_sim::Engine;
using moe_sim::EngineConfig;
using moe_sim::EngineError;
using moe_sim::Event;
using moe_sim::EventKey;
using moe_sim::parse_u128;
using moe_sim::Resource;
using moe_sim::ResourceKind;
using moe_sim::sha256_bytes;
using moe_sim::TerminalStatus;
using moe_sim::U128;

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
        "expected " + needle + ", got EngineError: " +
            std::string{error.what()});
  }
}

EngineConfig config() {
  return {
      1000,
      100,
      100,
      std::string(64, '1'),
      std::string(64, '2'),
      std::string(64, '3'),
      std::string(64, '4'),
      std::string(64, '5')};
}

Resource compute_resource() {
  return {
      "gpu0",
      ResourceKind::kComputeSlots,
      Arbitration::kFifo,
      U128{1},
      U128{0},
      {},
      {}};
}

Event event(
    std::string id,
    std::uint64_t time,
    std::uint32_t priority,
    Action action,
    std::string owner,
    std::vector<std::string> dependencies = {}) {
  return {
      EventKey{
          U128{time},
          priority,
          std::string{"request-1"},
          std::uint64_t{0},
          std::uint32_t{0},
          "gpu0",
          std::move(id)},
      std::move(dependencies),
      action,
      "gpu0",
      std::move(owner),
      U128{1},
      U128{0}};
}

std::vector<Event> normal_events() {
  return {
      event("acquire", 10, 80, Action::kAcquire, "request-1"),
      event(
          "service", 20, 100, Action::kService, "request-1",
          {"acquire"}),
      event(
          "release", 30, 10, Action::kRelease, "request-1",
          {"service"})};
}

void test_clock() {
  Clock clock{"c", 3, 1, U128{7}, 0, 0};
  clock.validate();
  require(clock.edge_time(0) == 7, "clock phase offset");
  require(
      clock.edge_time(3) == U128{1'000'000'000'000'007ULL},
      "rational edge reference");
  const U128 edge = clock.edge_time(2);
  require(clock.ceil_edge(edge) == 2, "exact edge ceil");
  require(clock.ceil_edge(edge - 1) == 2, "just-before edge ceil");
  require(clock.ceil_edge(edge + 1) == 3, "just-after edge ceil");
  require(
      clock.edge_time(1'000'000) ==
          U128{7} +
              (U128{1'000'000} * U128{1'000'000'000'000'000ULL}) / 3,
      "long clock run has zero drift");

  Clock bad = clock;
  bad.local_cycle = 2;
  bad.fractional_remainder = 0;
  require_throws([&] { bad.validate(); }, "remainder");
  Clock unnormalized{"bad", 4, 2, 0, 0, 0};
  require_throws([&] { unnormalized.validate(); }, "gcd");
}

void test_sha256() {
  require(
      sha256_bytes("") ==
          "e3b0c44298fc1c149afbf4c8996fb924"
          "27ae41e4649b934ca495991b7852b855",
      "SHA-256 empty input known vector");
  require(
      sha256_bytes("abc") ==
          "ba7816bf8f01cfea414140de5dae2223"
          "b00361a396177a9cb410ff61f20015ad",
      "SHA-256 abc known vector");
}

void test_bridge() {
  std::map<std::string, Clock> clocks{
      {"src", Clock{"src", 1'000'000'000'000'000ULL, 1, 0, 0, 0}},
      {"dst", Clock{"dst", 500'000'000'000'000ULL, 1, 1, 0, 0}}};
  for (const auto& [id, clock] : clocks) {
    static_cast<void>(id);
    clock.validate();
  }
  Bridge bridge{
      "b",
      "src",
      "dst",
      BridgeProtocol::kRequestAck,
      U128{1},
      U128{1},
      2,
      1,
      4,
      BackpressurePolicy::kStallSource};
  require(
      bridge.arrival(U128{2}, clocks) == clocks.at("dst").edge_time(3),
      "CDC ceil plus synchronization cycles");
  Bridge invalid = bridge;
  invalid.protocol = BridgeProtocol::kCredit;
  require_throws([&] { invalid.validate(clocks); }, "CREDIT protocol");
  invalid.backpressure_policy = BackpressurePolicy::kCreditBlock;
  invalid.validate(clocks);
  invalid.forward_latency_fs = 0;
  invalid.receiver_sync_cycles = 0;
  require_throws([&] { invalid.validate(clocks); }, "strict progress");
}

void test_determinism_and_capacity() {
  const auto events = normal_events();
  Engine first(events, {compute_resource()}, config());
  require(
      first.run_until_quiescent() == TerminalStatus::kQuiescent,
      "normal execution quiesces");
  auto reversed = events;
  std::reverse(reversed.begin(), reversed.end());
  Engine second(reversed, {compute_resource()}, config());
  require(
      second.run_until_quiescent() == TerminalStatus::kQuiescent,
      "permuted execution quiesces");
  require(
      first.state_digest() == second.state_digest(),
      "input permutation leaves identical state digest");

  Resource occupied = compute_resource();
  occupied.occupancy = 1;
  occupied.holders["owner-0"] = 1;
  Engine blocked(
      {event("blocked", 10, 80, Action::kAcquire, "owner-1")},
      {occupied}, config());
  require(
      blocked.run_until_quiescent() == TerminalStatus::kDeadlock,
      "unreleased capacity is deadlock");
  require(
      blocked.diagnose_deadlock().find("blocked") != std::string::npos,
      "deadlock report names waiter");

  Resource r1 = compute_resource();
  r1.resource_id = "r1";
  r1.occupancy = 1;
  r1.holders["owner-a"] = 1;
  Resource r2 = compute_resource();
  r2.resource_id = "r2";
  r2.occupancy = 1;
  r2.holders["owner-b"] = 1;
  Event wait_a = event("wait-a", 10, 80, Action::kAcquire, "owner-a");
  wait_a.resource_id = "r2";
  Event wait_b = event("wait-b", 11, 80, Action::kAcquire, "owner-b");
  wait_b.resource_id = "r1";
  Engine cycle({wait_a, wait_b}, {r1, r2}, config());
  require(
      cycle.run_until_quiescent() == TerminalStatus::kDeadlock,
      "two-resource wait cycle is deadlock");
  require(
      cycle.diagnose_deadlock().find("WAIT_FOR_CYCLE") == 0,
      "wait-for cycle is diagnosed");

  Resource releasable = occupied;
  std::vector<Event> waiting{
      event("blocked", 10, 80, Action::kAcquire, "owner-1"),
      event("release", 20, 10, Action::kRelease, "owner-0")};
  Engine resumed(waiting, {releasable}, config());
  require(
      resumed.run_until_quiescent() == TerminalStatus::kQuiescent,
      "release drains deterministic waiter");

  Engine wrong_release(
      {event("release", 10, 10, Action::kRelease, "wrong-owner")},
      {occupied}, config());
  require_throws(
      [&] { static_cast<void>(wrong_release.step()); }, "wrong holder");

  Resource releasable_dependency = occupied;
  Event blocked_first =
      event("blocked-first", 10, 80, Action::kAcquire, "owner-1");
  Event dependent =
      event("dependent", 11, 100, Action::kService, "owner-1",
            {"blocked-first"});
  Event independent_release =
      event("independent-release", 20, 10, Action::kRelease, "owner-0");
  Engine dependency_ready(
      {blocked_first, dependent, independent_release},
      {releasable_dependency}, config());
  require(
      dependency_ready.run_until_quiescent() ==
          TerminalStatus::kQuiescent,
      "dependency-unready consumer does not block independent release");
}

void test_fail_closed_boundaries() {
  const U128 maximum =
      parse_u128("340282366920938463463374607431768211455");
  Resource wrapped_holders = compute_resource();
  wrapped_holders.capacity = maximum;
  wrapped_holders.holders["owner-a"] = maximum;
  wrapped_holders.holders["owner-b"] = 1;
  require_throws(
      [&] {
        Engine engine({}, {wrapped_holders}, config());
        static_cast<void>(engine);
      },
      "conservation");
  Resource maximum_valid = compute_resource();
  maximum_valid.capacity = maximum;
  maximum_valid.occupancy = maximum;
  maximum_valid.holders["owner-a"] = maximum;
  Engine maximum_engine({}, {maximum_valid}, config());
  require(
      maximum_engine.checkpoint().resources.at("gpu0").occupancy == maximum,
      "maximum valid resource quantity is preserved");

  Resource preloaded_waiter = compute_resource();
  preloaded_waiter.waiters.push_back("acquire");
  require_throws(
      [&] {
        Engine engine(
            {event("acquire", 1, 80, Action::kAcquire, "o")},
            {preloaded_waiter}, config());
        static_cast<void>(engine);
      },
      "fresh resources cannot contain waiters");

  Resource unsupported = compute_resource();
  unsupported.kind = ResourceKind::kUnsupportedPhase4;
  require_throws(
      [&] {
        Engine engine(
            {event("transfer", 1, 90, Action::kTransferUnsupported, "o")},
            {unsupported}, config());
        static_cast<void>(engine);
      },
      "UNSUPPORTED_PHASE4_RESOURCE");
  Resource round_robin = compute_resource();
  round_robin.arbitration = Arbitration::kRoundRobinUnsupported;
  require_throws(
      [&] {
        Engine engine(
            {event("acquire", 1, 80, Action::kAcquire, "o")},
            {round_robin}, config());
        static_cast<void>(engine);
      },
      "ROUND_ROBIN_UNSUPPORTED");
  require_throws(
      [&] {
        Engine engine({}, {unsupported}, config());
        static_cast<void>(engine);
      },
      "UNSUPPORTED_PHASE4_RESOURCE");
  require_throws(
      [&] {
        Engine engine({}, {round_robin}, config());
        static_cast<void>(engine);
      },
      "ROUND_ROBIN_UNSUPPORTED");

  auto bad_dependency = normal_events();
  bad_dependency.front().dependencies = {"release"};
  require_throws(
      [&] {
        Engine engine(bad_dependency, {compute_resource()}, config());
        static_cast<void>(engine);
      },
      "dependency");

  EngineConfig too_small = config();
  too_small.max_events = 2;
  require_throws(
      [&] {
        Engine engine(normal_events(), {compute_resource()}, too_small);
        static_cast<void>(engine);
      },
      "input event count");
  EngineConfig bad_hash = config();
  bad_hash.engine_build_sha256 = std::string(64, 'A');
  require_throws(
      [&] {
        Engine engine(normal_events(), {compute_resource()}, bad_hash);
        static_cast<void>(engine);
      },
      "lowercase 64-character");
  EngineConfig above_profile = config();
  above_profile.max_wait_for_nodes = 100001;
  require_throws(
      [&] {
        Engine engine(normal_events(), {compute_resource()}, above_profile);
        static_cast<void>(engine);
      },
      "frozen profile maxima");
  above_profile = config();
  above_profile.max_events = 1000001;
  require_throws(
      [&] {
        Engine engine(normal_events(), {compute_resource()}, above_profile);
        static_cast<void>(engine);
      },
      "frozen profile maxima");
  above_profile = config();
  above_profile.max_same_time_events = 100001;
  require_throws(
      [&] {
        Engine engine(normal_events(), {compute_resource()}, above_profile);
        static_cast<void>(engine);
      },
      "frozen profile maxima");
}

void test_checkpoint() {
  const auto events = normal_events();
  Clock checkpoint_clock{
      "clock", 3, 1, U128{7}, 2,
      static_cast<std::uint64_t>(
          (U128{2} * U128{1'000'000'000'000'000ULL}) % U128{3})};
  Engine continuous(
      events, {compute_resource()}, config(), {checkpoint_clock});
  static_cast<void>(continuous.run_until_quiescent());

  Checkpoint saved;
  Checkpoint after_one;
  std::string serialized;
  for (std::size_t boundary = 0; boundary <= events.size(); ++boundary) {
    Engine split(events, {compute_resource()}, config(), {checkpoint_clock});
    for (std::size_t index = 0; index < boundary; ++index) {
      static_cast<void>(split.step());
    }
    saved = split.checkpoint();
    if (boundary == 1) {
      after_one = saved;
    }
    Engine restored = Engine::restore(saved, config());
    static_cast<void>(restored.run_until_quiescent());
    require(
        continuous.state_digest() == restored.state_digest(),
        "checkpoint continuation equals continuous execution at boundary " +
            std::to_string(boundary));
    serialized = split.serialize_checkpoint();
    Engine restored_bytes =
        Engine::restore_serialized(serialized, config());
    static_cast<void>(restored_bytes.run_until_quiescent());
    require(
        continuous.state_digest() == restored_bytes.state_digest(),
        "serialized checkpoint continuation equals continuous execution at "
        "boundary " +
            std::to_string(boundary));
  }
  std::string corrupted = serialized;
  corrupted.back() = corrupted.back() == '0' ? '1' : '0';
  require_throws(
      [&] {
        static_cast<void>(
            Engine::restore_serialized(corrupted, config()));
      },
      "digest");

  Checkpoint tampered = saved;
  tampered.resources.at("gpu0").occupancy = 1;
  require_throws(
      [&] { static_cast<void>(Engine::restore(tampered, config())); },
      "conservation");
  tampered = saved;
  tampered.clocks.at("clock").fractional_remainder = 0;
  require_throws(
      [&] { static_cast<void>(Engine::restore(tampered, config())); },
      "remainder");

  const Checkpoint pristine =
      Engine(events, {compute_resource()}, config(), {checkpoint_clock})
          .checkpoint();
  const auto require_mutation_rejected =
      [&](const std::function<void(Checkpoint&)>& mutate,
          const std::string& name) {
        Checkpoint changed = pristine;
        mutate(changed);
        try {
          static_cast<void>(Engine::restore(changed, config()));
          require(false, "checkpoint mutation was accepted: " + name);
        } catch (const EngineError& error) {
          require(
              std::string{error.what()}.find("digest") !=
                  std::string::npos,
              "checkpoint mutation " + name +
                  " produced unexpected error: " + error.what());
        }
      };
  require_mutation_rejected(
      [](Checkpoint& value) { value.events[1].service_demand = 1; },
      "service demand");
  require_mutation_rejected(
      [](Checkpoint& value) { value.events[1].quantity = 2; },
      "quantity");
  require_mutation_rejected(
      [](Checkpoint& value) { value.events[1].action = Action::kAcquire; },
      "action");
  require_mutation_rejected(
      [](Checkpoint& value) { value.events[1].key.time_fs += 1; },
      "tie key");
  require_mutation_rejected(
      [](Checkpoint& value) { value.events[1].dependencies.clear(); },
      "dependency");
  require_mutation_rejected(
      [](Checkpoint& value) {
        value.resources.at("gpu0").kind = ResourceKind::kMemoryBytes;
      },
      "resource kind");
  require_mutation_rejected(
      [](Checkpoint& value) {
        value.resources.at("gpu0").arbitration = Arbitration::kPriority;
      },
      "resource arbitration");
  tampered = after_one;
  tampered.last_event_time_fs = *tampered.last_event_time_fs + 1;
  require_throws(
      [&] { static_cast<void>(Engine::restore(tampered, config())); },
      "digest");

  const auto rehash = [](Checkpoint& value) {
    value.state_digest = Engine::checkpoint_digest(value);
  };
  tampered = pristine;
  tampered.states.erase("service");
  rehash(tampered);
  require_throws(
      [&] { static_cast<void>(Engine::restore(tampered, config())); },
      "key-set");
  tampered = after_one;
  ++tampered.processed_count;
  rehash(tampered);
  require_throws(
      [&] { static_cast<void>(Engine::restore(tampered, config())); },
      "processed-count");
  tampered = after_one;
  tampered.trace.front().executed_time_fs = 0;
  rehash(tampered);
  require_throws(
      [&] { static_cast<void>(Engine::restore(tampered, config())); },
      "time monotonicity");
  tampered = pristine;
  tampered.terminal_status = TerminalStatus::kQuiescent;
  rehash(tampered);
  require_throws(
      [&] { static_cast<void>(Engine::restore(tampered, config())); },
      "quiescent terminal");
  tampered = after_one;
  tampered.last_event_time_fs = *tampered.last_event_time_fs + 1;
  rehash(tampered);
  require_throws(
      [&] { static_cast<void>(Engine::restore(tampered, config())); },
      "trace-tail");
  tampered = after_one;
  tampered.same_time_count = 2;
  rehash(tampered);
  require_throws(
      [&] { static_cast<void>(Engine::restore(tampered, config())); },
      "trace-tail");
  tampered = after_one;
  tampered.global_time_fs = 99;
  tampered.last_event_time_fs = 99;
  rehash(tampered);
  require_throws(
      [&] { static_cast<void>(Engine::restore(tampered, config())); },
      "trace-tail");
  tampered = pristine;
  tampered.terminal_status = TerminalStatus::kFailed;
  rehash(tampered);
  require_throws(
      [&] { static_cast<void>(Engine::restore(tampered, config())); },
      "NON_CHECKPOINTABLE");

  Resource preoccupied = compute_resource();
  preoccupied.occupancy = 1;
  preoccupied.holders["owner-0"] = 1;
  Engine blocked_engine(
      {event("blocked-checkpoint", 10, 80, Action::kAcquire, "owner-1")},
      {preoccupied}, config());
  static_cast<void>(blocked_engine.step());
  tampered = blocked_engine.checkpoint();
  tampered.resources.at("gpu0").waiters.clear();
  rehash(tampered);
  require_throws(
      [&] { static_cast<void>(Engine::restore(tampered, config())); },
      "blocked waiter/state");

  Checkpoint unreachable = continuous.checkpoint();
  unreachable.states.at("acquire") = moe_sim::EventState::kPending;
  unreachable.trace.erase(
      std::remove_if(
          unreachable.trace.begin(), unreachable.trace.end(),
          [](const moe_sim::TraceEntry& entry) {
            return entry.event_id == "acquire";
          }),
      unreachable.trace.end());
  --unreachable.processed_count;
  unreachable.terminal_status = TerminalStatus::kRunning;
  rehash(unreachable);
  require_throws(
      [&] {
        static_cast<void>(Engine::restore(unreachable, config()));
      },
      "trace-order");

  std::vector<Event> same_time_events{
      event("same-a", 10, 100, Action::kService, "owner-a"),
      event("same-b", 10, 100, Action::kService, "owner-b")};
  Engine same_time(
      same_time_events, {compute_resource()}, config());
  static_cast<void>(same_time.run_until_quiescent());
  Checkpoint reordered = same_time.checkpoint();
  std::swap(reordered.trace[0], reordered.trace[1]);
  rehash(reordered);
  require_throws(
      [&] { static_cast<void>(Engine::restore(reordered, config())); },
      "trace-order");
  EngineConfig wrong = config();
  wrong.phase2_ledger_sha256 = "wrong";
  require_throws(
      [&] { static_cast<void>(Engine::restore(saved, wrong)); },
      "contract hash");
}

void test_zeno_budget() {
  EngineConfig limited = config();
  limited.max_same_time_events = 2;
  std::vector<Event> events{
      event("a", 1, 100, Action::kService, "o"),
      event("b", 1, 100, Action::kService, "o"),
      event("c", 1, 100, Action::kService, "o")};
  Engine engine(events, {compute_resource()}, limited);
  static_cast<void>(engine.step());
  static_cast<void>(engine.step());
  require_throws(
      [&] { static_cast<void>(engine.step()); }, "ZENO");
  require(
      engine.terminal_status() == TerminalStatus::kZeno,
      "Zeno status is terminal and explicit");
  Engine zeno_restored = Engine::restore(engine.checkpoint(), limited);
  require(
      zeno_restored.state_digest() == engine.state_digest(),
      "Zeno checkpoint is self-restorable");

  Resource draining = compute_resource();
  draining.capacity = 2;
  draining.occupancy = 2;
  draining.holders["owner-0"] = 2;
  Event first_waiter =
      event("first-waiter", 1, 80, Action::kAcquire, "owner-1");
  Event second_waiter =
      event("second-waiter", 2, 80, Action::kAcquire, "owner-2");
  Event bulk_release =
      event("bulk-release", 3, 10, Action::kRelease, "owner-0");
  bulk_release.quantity = 2;
  EngineConfig drain_limit = config();
  drain_limit.max_same_time_events = 2;
  Engine drain_engine(
      {first_waiter, second_waiter, bulk_release},
      {draining}, drain_limit);
  static_cast<void>(drain_engine.step());
  static_cast<void>(drain_engine.step());
  require_throws(
      [&] { static_cast<void>(drain_engine.step()); },
      "ZENO_SAME_TIME_EVENT_BUDGET");
  require(
      drain_engine.terminal_status() == TerminalStatus::kZeno,
      "waiter-drain transitions enforce same-time budget");
  Engine drain_restored =
      Engine::restore(drain_engine.checkpoint(), drain_limit);
  require(
      drain_restored.state_digest() == drain_engine.state_digest(),
      "waiter-drain Zeno checkpoint is atomic and self-restorable");
}

void test_deadlock_graph_limit() {
  Resource r1 = compute_resource();
  r1.resource_id = "r1";
  r1.occupancy = 1;
  r1.holders["owner-a"] = 1;
  Resource r2 = compute_resource();
  r2.resource_id = "r2";
  r2.occupancy = 1;
  r2.holders["owner-b"] = 1;
  Event wait_a = event("wait-a", 10, 80, Action::kAcquire, "owner-a");
  wait_a.resource_id = "r2";
  Event wait_b = event("wait-b", 11, 80, Action::kAcquire, "owner-b");
  wait_b.resource_id = "r1";
  EngineConfig bounded = config();
  bounded.max_wait_for_nodes = 1;
  Engine engine({wait_a, wait_b}, {r1, r2}, bounded);
  require(
      engine.run_until_quiescent() == TerminalStatus::kDeadlock,
      "bounded wait-for fixture reaches deadlock");
  require_throws(
      [&] { static_cast<void>(engine.diagnose_deadlock()); },
      "WAIT_FOR_GRAPH_LIMIT_EXCEEDED");
}

}  // namespace

int main() {
  test_clock();
  test_sha256();
  test_bridge();
  test_determinism_and_capacity();
  test_fail_closed_boundaries();
  test_checkpoint();
  test_zeno_budget();
  test_deadlock_graph_limit();
  if (failures != 0) {
    std::cerr << failures << " checks failed\n";
    return 1;
  }
  std::cout << "PHASE3_CPP_TESTS: PASS\n";
  return 0;
}

#include "moe_sim/single_gpu_model.hpp"

#include <algorithm>
#include <chrono>
#include <iostream>
#include <string>
#include <vector>

namespace {

using moe_sim::Arbitration;
using moe_sim::EngineError;
using moe_sim::EventKey;
using moe_sim::TerminalStatus;
using moe_sim::U128;
using moe_sim::phase4::Fidelity;
using moe_sim::phase4::Checkpoint;
using moe_sim::phase4::Operation;
using moe_sim::phase4::RangeStatus;
using moe_sim::phase4::ScheduleResult;
using moe_sim::phase4::ServiceClass;
using moe_sim::phase4::ServiceProfile;
using moe_sim::phase4::SingleGpuModel;
using moe_sim::phase4::SingleGpuPlatform;

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

constexpr char kPhase3Ledger[] =
    "c4b9209d95bbf91c607d65a70062e3bbb03a5892807ce08d8a4a370000535e42";

ServiceProfile profile(
    const std::string& id,
    ServiceClass service_class,
    std::uint64_t lanes,
    bool shared) {
  return {
      id,
      service_class,
      lanes,
      U128{1'000'000'000'000'000ULL},
      U128{1},
      U128{0},
      shared,
      Fidelity::kAnalyticFirstOrder,
      RangeStatus::kRangeUnknown};
}

SingleGpuPlatform platform(
    std::uint64_t compute_lanes = 1,
    std::uint64_t shared_lanes = 1) {
  return {
      "synthetic-single-gpu",
      shared_lanes,
      moe_sim::Clock{
          "gpu-clock", 1'000'000'000'000'000ULL, 1, U128{0}, 0, 0},
      {
          profile("compute-v1", ServiceClass::kCompute, compute_lanes, false),
          profile("memory-v1", ServiceClass::kMemory, 1, true),
          profile("h2d-v1", ServiceClass::kH2D, 1, true),
          profile("d2h-v1", ServiceClass::kD2H, 1, true),
      },
      kPhase3Ledger,
      std::string(moe_sim::phase4::kPhase4BuildAuthoritySha256),
      std::string(moe_sim::phase4::kPhase4ModelContractSha256),
      std::string(moe_sim::phase4::kPhase4CheckpointSchemaSha256)};
}

Operation operation(
    const std::string& id,
    ServiceClass service_class,
    std::uint64_t release,
    std::uint64_t work,
    std::vector<std::string> dependencies = {}) {
  return {
      EventKey{
          U128{release},
          service_class == ServiceClass::kH2D ||
                  service_class == ServiceClass::kD2H
              ? 90U
              : 100U,
          std::string{"request-1"},
          std::uint64_t{0},
          std::uint32_t{0},
          "single-gpu",
          id},
      std::move(dependencies),
      service_class,
      U128{work}};
}

const auto& entry(const ScheduleResult& result, const std::string& id) {
  return *std::find_if(
      result.entries.begin(), result.entries.end(),
      [&](const auto& item) { return item.operation_id == id; });
}

void test_compute_contention_and_determinism() {
  std::vector<Operation> operations{
      operation("a", ServiceClass::kCompute, 0, 10),
      operation("b", ServiceClass::kCompute, 0, 10)};
  SingleGpuModel model(platform(), operations);
  const ScheduleResult result = model.run_until_quiescent();
  require(entry(result, "a").start_fs == 0, "first compute starts at zero");
  require(entry(result, "a").end_fs == 10, "first compute duration");
  require(entry(result, "b").start_fs == 10, "second compute queues");
  require(
      entry(result, "b").queue_delay_fs == 10,
      "compute queue delay is attributed");
  require(result.makespan_fs == 20, "compute contention makespan");
  require(
      result.class_metrics.at(ServiceClass::kCompute).busy_lane_fs == 20,
      "compute busy-lane metric");

  std::reverse(operations.begin(), operations.end());
  SingleGpuModel reversed(platform(), operations);
  const ScheduleResult reversed_result = reversed.run_until_quiescent();
  require(
      result.semantic_digest == reversed_result.semantic_digest,
      "input permutation preserves schedule digest");

  SingleGpuModel parallel(platform(2), operations);
  const ScheduleResult overlap = parallel.run_until_quiescent();
  require(
      entry(overlap, "a").start_fs == 0 &&
          entry(overlap, "b").start_fs == 0 &&
          overlap.makespan_fs == 10,
      "two compute lanes overlap");
}

void test_memory_copy_fabric_and_compute_overlap() {
  SingleGpuModel model(
      platform(),
      {
          operation("memory", ServiceClass::kMemory, 0, 10),
          operation("h2d", ServiceClass::kH2D, 0, 10),
          operation("compute", ServiceClass::kCompute, 0, 10),
      });
  const ScheduleResult result = model.run_until_quiescent();
  require(
      entry(result, "h2d").start_fs == 0 &&
          entry(result, "memory").start_fs == 10,
      "H2D and memory serialize by canonical key on shared fabric");
  require(
      entry(result, "compute").start_fs == 0,
      "compute overlaps shared-fabric traffic");
  require(result.makespan_fs == 20, "shared-fabric makespan");
}

void test_atomic_admission_and_priority() {
  const std::vector<Operation> operations{
      operation("memory", ServiceClass::kMemory, 0, 10),
      operation("h2d", ServiceClass::kH2D, 0, 5)};
  SingleGpuModel model(platform(), operations);
  const auto first = model.step();
  require(
      first.operation_id == "h2d" && first.kind == moe_sim::phase4::TraceKind::kStart,
      "canonical start key selects H2D");
  require(
      first.generated_key.has_value() &&
          first.generated_key->event_id == first.generated_event_id &&
          first.generated_key->event_priority == first.priority &&
          first.generated_event_id.size() == 64,
      "trace preserves the complete generated EventIR key");
  const Checkpoint blocked = model.checkpoint();
  require(
      blocked.active.size() == 1 &&
          blocked.states.at("memory") ==
              moe_sim::phase4::OperationState::kPending,
      "blocked multi-resource request holds no partial reservation");
  const ScheduleResult result = model.run_until_quiescent();
  const auto completion = std::find_if(
      result.trace.begin(), result.trace.end(), [](const auto& item) {
        return item.operation_id == "h2d" &&
               item.kind == moe_sim::phase4::TraceKind::kComplete;
      });
  const auto memory_start = std::find_if(
      result.trace.begin(), result.trace.end(), [](const auto& item) {
        return item.operation_id == "memory" &&
               item.kind == moe_sim::phase4::TraceKind::kStart;
      });
  require(
      completion != result.trace.end() && memory_start != result.trace.end() &&
          completion->time_fs == memory_start->time_fs &&
          completion < memory_start &&
          completion->priority < memory_start->priority,
      "same-time completion releases before next start");

  SingleGpuModel registry(
      platform(2, 2),
      {
          operation("compute-reg", ServiceClass::kCompute, 0, 1),
          operation("memory-reg", ServiceClass::kMemory, 0, 1),
          operation("h2d-reg", ServiceClass::kH2D, 2, 1),
          operation("d2h-reg", ServiceClass::kD2H, 2, 1),
      });
  const auto registry_result = registry.run_until_quiescent();
  for (const auto& trace : registry_result.trace) {
    const bool copy =
        trace.operation_id == "h2d-reg" || trace.operation_id == "d2h-reg";
    const std::uint32_t expected =
        trace.kind == moe_sim::phase4::TraceKind::kStart
            ? (copy ? 90U : 100U)
            : (copy ? 20U : 30U);
    require(trace.priority == expected, "generated priority registry");
  }
}

void test_dependency_and_checkpoint_replay() {
  const std::vector<Operation> operations{
      operation("h2d", ServiceClass::kH2D, 0, 8),
      operation(
          "compute", ServiceClass::kCompute, 1, 7, {"h2d"}),
      operation("d2h", ServiceClass::kD2H, 2, 5, {"compute"})};
  SingleGpuModel model(platform(), operations);
  const Checkpoint pristine = model.checkpoint();
  const std::string pristine_wire = model.serialize_checkpoint();
  SingleGpuModel restored_zero =
      SingleGpuModel::restore(platform(), operations, pristine);
  require(
      restored_zero.state_digest() == model.state_digest(),
      "boundary-zero checkpoint restores");
  SingleGpuModel wire_zero =
      SingleGpuModel::restore_serialized(
          platform(), operations, pristine_wire);
  require(
      wire_zero.state_digest() == model.state_digest(),
      "boundary-zero wire checkpoint restores");
  static_cast<void>(model.step());
  const Checkpoint in_flight = model.checkpoint();
  SingleGpuModel restored =
      SingleGpuModel::restore(platform(), operations, in_flight);
  require(
      restored.state_digest() == model.state_digest(),
      "in-flight reservation checkpoint restores");
  require(
      in_flight.result.semantic_digest.empty() &&
          restored.result().semantic_digest ==
              in_flight.result.semantic_digest,
      "running checkpoint preserves the exact derived semantic field");
  SingleGpuModel wire_in_flight =
      SingleGpuModel::restore_serialized(
          platform(), operations, model.serialize_checkpoint());
  require(
      wire_in_flight.state_digest() == model.state_digest(),
      "in-flight wire checkpoint restores");
  const ScheduleResult result = model.run_until_quiescent();
  const ScheduleResult restored_result = restored.run_until_quiescent();
  require(
      result.semantic_digest == restored_result.semantic_digest,
      "restored and continuous timed execution agree");
  const Checkpoint terminal = model.checkpoint();
  SingleGpuModel terminal_restored =
      SingleGpuModel::restore(platform(), operations, terminal);
  require(
      terminal_restored.state_digest() == model.state_digest(),
      "terminal checkpoint restores");
  SingleGpuModel terminal_wire =
      SingleGpuModel::restore_serialized(
          platform(), operations, model.serialize_checkpoint());
  require(
      terminal_wire.state_digest() == model.state_digest(),
      "terminal wire checkpoint restores");

  Checkpoint invalid_trace = terminal;
  invalid_trace.result.trace.back().kind =
      static_cast<moe_sim::phase4::TraceKind>(99);
  require_throws(
      [&] {
        static_cast<void>(
            SingleGpuModel::restore(platform(), operations, invalid_trace));
      },
      "invalid trace kind");

  std::string invalid_trace_wire = model.serialize_checkpoint();
  const std::size_t trace_section = invalid_trace_wire.find("5:TRACE");
  const std::size_t trace_kind =
      invalid_trace_wire.find("8:COMPLETE", trace_section);
  require(
      trace_section != std::string::npos &&
          trace_kind != std::string::npos,
      "wire fixture locates a completion TraceKind");
  invalid_trace_wire.replace(trace_kind, 10, "8:INVALID!");
  const std::size_t invalid_digest_start = invalid_trace_wire.size() - 65;
  std::size_t invalid_body_start = 0;
  for (int line = 0; line < 4; ++line) {
    invalid_body_start =
        invalid_trace_wire.find('\n', invalid_body_start) + 1;
  }
  const std::string invalid_body = invalid_trace_wire.substr(
      invalid_body_start, invalid_digest_start - invalid_body_start);
  invalid_trace_wire.replace(
      invalid_digest_start, 64, moe_sim::sha256_bytes(invalid_body));
  require_throws(
      [&] {
        static_cast<void>(SingleGpuModel::restore_serialized(
            platform(), operations, invalid_trace_wire));
      },
      "checkpoint wire digest");

  require(
      entry(result, "compute").start_fs == 8 &&
          entry(result, "d2h").start_fs == 15 &&
          result.makespan_fs == 20,
      "dependency completion controls readiness");

  Checkpoint tampered = in_flight;
  tampered.active.begin()->second.completion_fs += 1;
  require_throws(
      [&] {
        static_cast<void>(
            SingleGpuModel::restore(platform(), operations, tampered));
      },
      "checkpoint");

  tampered = in_flight;
  tampered.result.semantic_digest = std::string(64, '0');
  require_throws(
      [&] {
        static_cast<void>(
            SingleGpuModel::restore(platform(), operations, tampered));
      },
      "checkpoint");

  tampered = pristine;
  tampered.result.class_metrics.emplace(
      static_cast<ServiceClass>(99), moe_sim::phase4::ClassMetrics{});
  require_throws(
      [&] {
        static_cast<void>(
            SingleGpuModel::restore(platform(), operations, tampered));
      },
      "invalid service class");

  tampered = pristine;
  tampered.result.class_metrics.emplace(
      ServiceClass::kCompute, moe_sim::phase4::ClassMetrics{});
  require_throws(
      [&] {
        static_cast<void>(
            SingleGpuModel::restore(platform(), operations, tampered));
      },
      "checkpoint");

  std::string trailing = pristine_wire + "x";
  require_throws(
      [&] {
        static_cast<void>(SingleGpuModel::restore_serialized(
            platform(), operations, trailing));
      },
      "wire");

  std::string rehashed = pristine_wire;
  std::size_t cursor = 0;
  for (int line = 0; line < 4; ++line) {
    cursor = rehashed.find('\n', cursor) + 1;
  }
  const std::size_t digest_start = rehashed.size() - 65;
  rehashed[cursor] = rehashed[cursor] == '1' ? '2' : '1';
  const std::string body = rehashed.substr(cursor, digest_start - cursor);
  rehashed.replace(digest_start, 64, moe_sim::sha256_bytes(body));
  require_throws(
      [&] {
        static_cast<void>(SingleGpuModel::restore_serialized(
            platform(), operations, rehashed));
      },
      "digest");
}

void test_dependency_arrival_and_dag() {
  Operation producer =
      operation("z", ServiceClass::kCompute, 0, 5);
  Operation consumer =
      operation("a", ServiceClass::kCompute, 10, 1, {"z"});
  SingleGpuModel dependency_before_arrival(
      platform(), {consumer, producer});
  const auto first = dependency_before_arrival.run_until_quiescent();
  require(
      entry(first, "a").start_fs == 10,
      "completed dependency waits for later arrival");

  producer = operation("z", ServiceClass::kCompute, 10, 5);
  consumer = operation("a", ServiceClass::kCompute, 0, 1, {"z"});
  SingleGpuModel arrival_before_dependency(platform(), {consumer, producer});
  const auto second = arrival_before_dependency.run_until_quiescent();
  require(
      entry(second, "a").start_fs == 15,
      "early arrival waits for dependency completion");

  Operation cycle_a =
      operation("cycle-a", ServiceClass::kCompute, 0, 1, {"cycle-b"});
  Operation cycle_b =
      operation("cycle-b", ServiceClass::kCompute, 0, 1, {"cycle-a"});
  require_throws(
      [&] {
        SingleGpuModel cycle(platform(), {cycle_a, cycle_b});
        static_cast<void>(cycle);
      },
      "cycle");
}

void test_fail_closed_profiles_and_bounds() {
  SingleGpuPlatform incomplete = platform();
  incomplete.profiles.pop_back();
  require_throws(
      [&] {
        SingleGpuModel model(incomplete, {});
        static_cast<void>(model);
      },
      "incomplete");

  SingleGpuPlatform unbound = platform();
  unbound.phase3_ledger_sha256 = std::string(64, '0');
  require_throws(
      [&] {
        SingleGpuModel model(unbound, {});
        static_cast<void>(model);
      },
      "platform boundary");

  for (const int authority : {0, 1, 2}) {
    SingleGpuPlatform wrong = platform();
    if (authority == 0) {
      wrong.phase4_build_sha256 = std::string(64, '0');
    } else if (authority == 1) {
      wrong.model_contract_sha256 = std::string(64, '0');
    } else {
      wrong.checkpoint_schema_sha256 = std::string(64, '0');
    }
    require_throws(
        [&] {
          SingleGpuModel model(wrong, {});
          static_cast<void>(model);
        },
        "platform boundary");
  }

  SingleGpuPlatform no_fabric = platform();
  no_fabric.profiles[1].uses_shared_fabric = false;
  require_throws(
      [&] {
        SingleGpuModel model(no_fabric, {});
        static_cast<void>(model);
      },
      "require shared fabric");

  SingleGpuPlatform invalid_fidelity = platform();
  invalid_fidelity.profiles[0].fidelity = static_cast<Fidelity>(99);
  require_throws(
      [&] {
        SingleGpuModel model(invalid_fidelity, {});
        static_cast<void>(model);
      },
      "service profile");

  SingleGpuPlatform compute_fabric = platform();
  compute_fabric.profiles[0].uses_shared_fabric = true;
  require_throws(
      [&] {
        SingleGpuModel model(compute_fabric, {});
        static_cast<void>(model);
      },
      "compute profile");

  Operation wrong_priority =
      operation("wrong-priority", ServiceClass::kH2D, 0, 1);
  wrong_priority.key.event_priority = 100;
  require_throws(
      [&] {
        SingleGpuModel model(platform(), {wrong_priority});
        static_cast<void>(model);
      },
      "operation");

  Operation unknown =
      operation("unknown", ServiceClass::kCompute, 0, 1);
  unknown.service_class = static_cast<ServiceClass>(99);
  require_throws(
      [&] {
        SingleGpuModel model(platform(), {unknown});
        static_cast<void>(model);
      },
      "operation");

  auto bad_dependency = operation(
      "a", ServiceClass::kCompute, 0, 1, {"missing"});
  require_throws(
      [&] {
        SingleGpuModel model(platform(), {bad_dependency});
        static_cast<void>(model);
      },
      "dependency");

  SingleGpuPlatform overflow = platform();
  overflow.profiles[0].setup_latency_fs =
      moe_sim::parse_u128("340282366920938463463374607431768211455");
  SingleGpuModel overflow_model(
      overflow, {operation("a", ServiceClass::kCompute, 0, 1)});
  require_throws(
      [&] { static_cast<void>(overflow_model.run_until_quiescent()); },
      "unsigned 128-bit");

  SingleGpuPlatform rational_clock = platform();
  rational_clock.completion_clock =
      moe_sim::Clock{"rational", 3, 1, U128{7}, 0, 0};
  SingleGpuModel snapped(
      rational_clock,
      {operation("snap", ServiceClass::kCompute, 8, 1)});
  const auto snapped_result = snapped.run_until_quiescent();
  require(
      entry(snapped_result, "snap").end_fs ==
          rational_clock.completion_clock.edge_time(
              rational_clock.completion_clock.ceil_edge(U128{9})),
      "non-integer clock completion uses exact ceil edge");
}

void test_scale_envelope() {
  std::vector<Operation> operations;
  operations.reserve(1000);
  for (std::uint64_t index = 0; index < 1000; ++index) {
    operations.push_back(operation(
        "scale-" + std::to_string(index),
        ServiceClass::kCompute,
        index,
        1));
  }
  const auto begin = std::chrono::steady_clock::now();
  SingleGpuModel scale_model(platform(4), std::move(operations));
  const ScheduleResult result = scale_model.run_until_quiescent();
  const auto elapsed = std::chrono::duration_cast<std::chrono::seconds>(
      std::chrono::steady_clock::now() - begin);
  require(result.entries.size() == 1000, "scale fixture completes");
  const std::uint64_t generated =
      result.scheduler_metrics.generated_event_count;
  std::uint64_t logarithm = 0;
  for (std::uint64_t value = generated + 1; value > 1; value = (value + 1) / 2) {
    ++logarithm;
  }
  require(
      result.scheduler_metrics.ready_queue_pushes == 1000 &&
          result.scheduler_metrics.ready_queue_pops == 1000 &&
          generated == 2000,
      "scale fixture complexity counters conserve operations");
  require(
      result.scheduler_metrics.scheduler_key_comparisons <=
          16 * generated * logarithm,
      "ready/event selection stays within deterministic comparison envelope");
  require(
      elapsed.count() < 10,
      "1000-operation CPU synthetic operational envelope");
}

}  // namespace

int main() {
  test_compute_contention_and_determinism();
  test_memory_copy_fabric_and_compute_overlap();
  test_atomic_admission_and_priority();
  test_dependency_and_checkpoint_replay();
  test_dependency_arrival_and_dag();
  test_fail_closed_profiles_and_bounds();
  test_scale_envelope();
  if (failures != 0) {
    std::cerr << failures << " checks failed\n";
    return 1;
  }
  std::cout << "PHASE4_SINGLE_GPU_TESTS: PASS\n";
  return 0;
}

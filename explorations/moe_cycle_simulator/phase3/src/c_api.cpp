#include "moe_sim/c_api.h"

#include "moe_sim/engine.hpp"

#include <algorithm>
#include <cstring>
#include <memory>
#include <optional>
#include <string>
#include <vector>

struct moe_phase3_engine {
  std::vector<moe_sim::Event> events;
  std::vector<moe_sim::Resource> resources;
  moe_sim::EngineConfig config;
  std::unique_ptr<moe_sim::Engine> core;
};

namespace {

void write_buffer(char* target, size_t size, const std::string& value) {
  if (target == nullptr || size == 0) {
    return;
  }
  const size_t length = std::min(size - 1, value.size());
  std::memcpy(target, value.data(), length);
  target[length] = '\0';
}

template <typename Callable>
int guarded(Callable&& callable, char* error, size_t error_size) {
  try {
    callable();
    write_buffer(error, error_size, "");
    return 0;
  } catch (const std::exception& exception) {
    write_buffer(error, error_size, exception.what());
    return -1;
  }
}

std::string required(const char* value, const char* name) {
  if (value == nullptr || *value == '\0') {
    throw moe_sim::EngineError(std::string{name} + " is required");
  }
  return value;
}

moe_sim::ResourceKind resource_kind(int value) {
  switch (value) {
    case 0:
      return moe_sim::ResourceKind::kComputeSlots;
    case 1:
      return moe_sim::ResourceKind::kMemoryBytes;
    case 2:
      return moe_sim::ResourceKind::kQueueEntries;
    case 3:
      return moe_sim::ResourceKind::kBridgeQueueEntries;
    case 4:
      return moe_sim::ResourceKind::kBridgeCredits;
    case 5:
      return moe_sim::ResourceKind::kUnsupportedPhase4;
    default:
      throw moe_sim::EngineError("invalid resource kind");
  }
}

moe_sim::Arbitration arbitration(int value) {
  switch (value) {
    case 0:
      return moe_sim::Arbitration::kFifo;
    case 1:
      return moe_sim::Arbitration::kPriority;
    case 2:
      return moe_sim::Arbitration::kRoundRobinUnsupported;
    default:
      throw moe_sim::EngineError("invalid arbitration");
  }
}

moe_sim::Action action(int value) {
  switch (value) {
    case 0:
      return moe_sim::Action::kAcquire;
    case 1:
      return moe_sim::Action::kRelease;
    case 2:
      return moe_sim::Action::kService;
    case 3:
      return moe_sim::Action::kTransferUnsupported;
    default:
      throw moe_sim::EngineError("invalid action");
  }
}

}  // namespace

extern "C" moe_phase3_engine* moe_phase3_engine_create(
    uint64_t max_events,
    uint64_t max_same_time_events,
    uint64_t max_wait_for_nodes,
    const char* phase2_ledger_sha256,
    const char* canonical_bundle_semantic_root,
    const char* engine_build_sha256,
    const char* engine_profile_sha256,
    const char* checkpoint_schema_sha256,
    char* error,
    size_t error_size) {
  try {
    auto handle = std::make_unique<moe_phase3_engine>();
    handle->config = {
        max_events,
        max_same_time_events,
        max_wait_for_nodes,
        required(phase2_ledger_sha256, "phase2 ledger hash"),
        required(canonical_bundle_semantic_root, "bundle root"),
        required(engine_build_sha256, "engine build hash"),
        required(engine_profile_sha256, "engine profile hash"),
        required(checkpoint_schema_sha256, "checkpoint schema hash")};
    write_buffer(error, error_size, "");
    return handle.release();
  } catch (const std::exception& exception) {
    write_buffer(error, error_size, exception.what());
    return nullptr;
  }
}

extern "C" void moe_phase3_engine_destroy(moe_phase3_engine* engine) {
  delete engine;
}

extern "C" int moe_phase3_engine_add_resource(
    moe_phase3_engine* engine,
    const char* resource_id,
    int kind,
    int arbitration_value,
    const char* capacity_decimal,
    char* error,
    size_t error_size) {
  return guarded(
      [&] {
        if (engine == nullptr || engine->core != nullptr) {
          throw moe_sim::EngineError("engine builder is unavailable");
        }
        engine->resources.push_back(
            {required(resource_id, "resource ID"),
             resource_kind(kind),
             arbitration(arbitration_value),
             moe_sim::parse_u128(required(capacity_decimal, "capacity")),
             0,
             {},
             {}});
      },
      error, error_size);
}

extern "C" int moe_phase3_engine_add_event(
    moe_phase3_engine* engine,
    const char* event_id,
    const char* time_fs_decimal,
    uint32_t event_priority,
    const char* request_id_or_null,
    int has_token_index,
    uint64_t token_index,
    int has_layer_index,
    uint32_t layer_index,
    const char* component_id,
    const char* const* dependencies,
    size_t dependency_count,
    int action_value,
    const char* resource_id,
    const char* owner_id,
    const char* quantity_decimal,
    const char* service_demand_decimal,
    char* error,
    size_t error_size) {
  return guarded(
      [&] {
        if (engine == nullptr || engine->core != nullptr) {
          throw moe_sim::EngineError("engine builder is unavailable");
        }
        if (dependency_count != 0 && dependencies == nullptr) {
          throw moe_sim::EngineError("dependency array is null");
        }
        std::vector<std::string> dependency_values;
        dependency_values.reserve(dependency_count);
        for (size_t index = 0; index < dependency_count; ++index) {
          dependency_values.push_back(
              required(dependencies[index], "dependency ID"));
        }
        engine->events.push_back(
            {{moe_sim::parse_u128(required(time_fs_decimal, "event time")),
              event_priority,
              request_id_or_null == nullptr
                  ? std::optional<std::string>{}
                  : std::optional<std::string>{request_id_or_null},
              has_token_index ? std::optional<std::uint64_t>{token_index}
                              : std::optional<std::uint64_t>{},
              has_layer_index ? std::optional<std::uint32_t>{layer_index}
                              : std::optional<std::uint32_t>{},
              required(component_id, "component ID"),
              required(event_id, "event ID")},
             std::move(dependency_values),
             action(action_value),
             required(resource_id, "resource ID"),
             required(owner_id, "owner ID"),
             moe_sim::parse_u128(required(quantity_decimal, "quantity")),
             moe_sim::parse_u128(
                 required(service_demand_decimal, "service demand"))});
      },
      error, error_size);
}

extern "C" int moe_phase3_engine_finalize(
    moe_phase3_engine* engine, char* error, size_t error_size) {
  return guarded(
      [&] {
        if (engine == nullptr || engine->core != nullptr) {
          throw moe_sim::EngineError("engine builder is unavailable");
        }
        engine->core = std::make_unique<moe_sim::Engine>(
            engine->events, engine->resources, engine->config);
      },
      error, error_size);
}

extern "C" int moe_phase3_engine_run(
    moe_phase3_engine* engine,
    int* terminal_status,
    char* error,
    size_t error_size) {
  return guarded(
      [&] {
        if (engine == nullptr || engine->core == nullptr ||
            terminal_status == nullptr) {
          throw moe_sim::EngineError("finalized engine and output are required");
        }
        *terminal_status =
            static_cast<int>(engine->core->run_until_quiescent());
      },
      error, error_size);
}

extern "C" int moe_phase3_engine_state_digest(
    const moe_phase3_engine* engine,
    char* output,
    size_t output_size,
    char* error,
    size_t error_size) {
  return guarded(
      [&] {
        if (engine == nullptr || engine->core == nullptr ||
            output == nullptr || output_size == 0) {
          throw moe_sim::EngineError("finalized engine and output are required");
        }
        const std::string digest = engine->core->state_digest();
        if (digest.size() + 1 > output_size) {
          throw moe_sim::EngineError("digest output buffer is too small");
        }
        write_buffer(output, output_size, digest);
      },
      error, error_size);
}

#pragma once

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct moe_phase3_engine moe_phase3_engine;

moe_phase3_engine* moe_phase3_engine_create(
    uint64_t max_events,
    uint64_t max_same_time_events,
    uint64_t max_wait_for_nodes,
    const char* phase2_ledger_sha256,
    const char* canonical_bundle_semantic_root,
    const char* engine_build_sha256,
    const char* engine_profile_sha256,
    const char* checkpoint_schema_sha256,
    char* error,
    size_t error_size);

void moe_phase3_engine_destroy(moe_phase3_engine* engine);

int moe_phase3_engine_add_resource(
    moe_phase3_engine* engine,
    const char* resource_id,
    int resource_kind,
    int arbitration,
    const char* capacity_decimal,
    char* error,
    size_t error_size);

int moe_phase3_engine_add_event(
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
    int action,
    const char* resource_id,
    const char* owner_id,
    const char* quantity_decimal,
    const char* service_demand_decimal,
    char* error,
    size_t error_size);

int moe_phase3_engine_finalize(
    moe_phase3_engine* engine, char* error, size_t error_size);

int moe_phase3_engine_run(
    moe_phase3_engine* engine,
    int* terminal_status,
    char* error,
    size_t error_size);

int moe_phase3_engine_state_digest(
    const moe_phase3_engine* engine,
    char* output,
    size_t output_size,
    char* error,
    size_t error_size);

#ifdef __cplusplus
}
#endif

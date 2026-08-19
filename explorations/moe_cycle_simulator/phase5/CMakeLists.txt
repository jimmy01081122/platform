cmake_minimum_required(VERSION 3.20)
project(moe_sim_phase5 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

add_subdirectory(../phase4 phase4_model)

add_library(moe_sim_phase5 STATIC src/routing_residency_policy.cpp)
target_include_directories(moe_sim_phase5 PUBLIC include)
target_link_libraries(moe_sim_phase5 PUBLIC moe_sim_phase4)
target_compile_options(moe_sim_phase5 PRIVATE -Wall -Wextra -Wpedantic -Werror)

add_executable(moe_sim_phase5_tests tests/routing_residency_policy_tests.cpp)
target_link_libraries(moe_sim_phase5_tests PRIVATE moe_sim_phase5)
target_compile_options(
  moe_sim_phase5_tests PRIVATE -Wall -Wextra -Wpedantic -Werror)

# Stage A3 IR -> engine loader (executable only; not a CTest). Drives the
# existing Phase 5 residency policy from a plan spec emitted by the Python IR
# loader. Adds no test to the phase5 suite (kept at 4 tests).
add_executable(moe_sim_phase5_ir_loader tools/ir_replay_loader.cpp)
target_link_libraries(moe_sim_phase5_ir_loader PRIVATE moe_sim_phase5)
target_compile_options(
  moe_sim_phase5_ir_loader PRIVATE -Wall -Wextra -Wpedantic -Werror)

enable_testing()
add_test(NAME moe_sim_phase5_tests COMMAND moe_sim_phase5_tests)

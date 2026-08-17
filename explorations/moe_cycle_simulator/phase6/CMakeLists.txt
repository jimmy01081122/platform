cmake_minimum_required(VERSION 3.20)
project(moe_sim_phase6 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

add_subdirectory(../phase5 phase5_policy)

add_library(moe_sim_phase6 STATIC src/multi_domain_scheduler.cpp)
target_include_directories(moe_sim_phase6 PUBLIC include)
target_link_libraries(moe_sim_phase6 PUBLIC moe_sim_phase5)
target_compile_options(moe_sim_phase6 PRIVATE -Wall -Wextra -Wpedantic -Werror)

add_executable(moe_sim_phase6_tests tests/multi_domain_scheduler_tests.cpp)
target_link_libraries(moe_sim_phase6_tests PRIVATE moe_sim_phase6)
target_compile_options(
  moe_sim_phase6_tests PRIVATE -Wall -Wextra -Wpedantic -Werror)

enable_testing()
add_test(NAME moe_sim_phase6_tests COMMAND moe_sim_phase6_tests)

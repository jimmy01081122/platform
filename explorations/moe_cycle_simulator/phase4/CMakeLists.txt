cmake_minimum_required(VERSION 3.20)
project(moe_sim_phase4 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

add_subdirectory(../phase3 phase3_core)

add_library(moe_sim_phase4 STATIC src/single_gpu_model.cpp)
target_include_directories(moe_sim_phase4 PUBLIC include)
target_link_libraries(moe_sim_phase4 PUBLIC moe_sim_phase3)
target_compile_options(moe_sim_phase4 PRIVATE -Wall -Wextra -Wpedantic -Werror)

add_executable(moe_sim_phase4_tests tests/single_gpu_model_tests.cpp)
target_link_libraries(moe_sim_phase4_tests PRIVATE moe_sim_phase4)
target_compile_options(
  moe_sim_phase4_tests PRIVATE -Wall -Wextra -Wpedantic -Werror)

enable_testing()
add_test(NAME moe_sim_phase4_tests COMMAND moe_sim_phase4_tests)

cmake_minimum_required(VERSION 3.20)
project(moe_sim_phase3 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

add_library(moe_sim_phase3 STATIC src/engine.cpp)
target_include_directories(moe_sim_phase3 PUBLIC include)
target_compile_options(moe_sim_phase3 PRIVATE -Wall -Wextra -Wpedantic -Werror)

add_library(moe_sim_phase3_c SHARED src/engine.cpp src/c_api.cpp)
target_include_directories(moe_sim_phase3_c PUBLIC include)
target_compile_options(
  moe_sim_phase3_c PRIVATE -Wall -Wextra -Wpedantic -Werror)
set_target_properties(moe_sim_phase3_c PROPERTIES OUTPUT_NAME moe_sim_phase3)

add_executable(moe_sim_phase3_tests tests/engine_tests.cpp)
target_link_libraries(moe_sim_phase3_tests PRIVATE moe_sim_phase3)
target_compile_options(
  moe_sim_phase3_tests PRIVATE -Wall -Wextra -Wpedantic -Werror)

enable_testing()
add_test(NAME moe_sim_phase3_tests COMMAND moe_sim_phase3_tests)

find_package(Python3 REQUIRED COMPONENTS Interpreter)
add_test(
  NAME moe_sim_phase3_python_binding
  COMMAND
    ${Python3_EXECUTABLE}
    ${CMAKE_CURRENT_SOURCE_DIR}/tests/python_binding_test.py
    --library $<TARGET_FILE:moe_sim_phase3_c>)
set_tests_properties(
  moe_sim_phase3_python_binding
  PROPERTIES ENVIRONMENT "PYTHONDONTWRITEBYTECODE=1")

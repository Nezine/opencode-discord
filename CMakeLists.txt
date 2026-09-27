cmake_minimum_required(VERSION 3.20)
project(opencode_discord_engine LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
  set(CMAKE_BUILD_TYPE RelWithDebInfo CACHE STRING "" FORCE)
endif()

option(ENGINE_WERROR "Treat warnings as errors" OFF)
option(ENGINE_SANITIZE "Build engine_tests with AddressSanitizer and UndefinedBehaviorSanitizer" OFF)

# The venv interpreter owns both the module ABI and the pybind11 CMake package,
# so resolve pybind11 through it rather than relying on a system install.
find_package(Python3 COMPONENTS Interpreter Development.Module REQUIRED)

execute_process(
  COMMAND "${Python3_EXECUTABLE}" -m pybind11 --cmakedir
  OUTPUT_VARIABLE PYBIND11_CMAKEDIR
  OUTPUT_STRIP_TRAILING_WHITESPACE
  COMMAND_ERROR_IS_FATAL ANY
)
set(pybind11_DIR "${PYBIND11_CMAKEDIR}")
find_package(pybind11 CONFIG REQUIRED)

find_package(CURL REQUIRED)
find_package(SQLite3 REQUIRED)
find_package(Threads REQUIRED)

set(ENGINE_SOURCES
  cpp/bindings.cpp
  cpp/config.cpp
  cpp/sse.cpp
  cpp/store.cpp
  cpp/text.cpp
  cpp/turn.cpp
)

pybind11_add_module(_engine ${ENGINE_SOURCES})

target_include_directories(_engine PRIVATE
  "${CMAKE_CURRENT_SOURCE_DIR}/cpp"
  "${CMAKE_CURRENT_SOURCE_DIR}/third_party"
)

target_link_libraries(_engine PRIVATE
  CURL::libcurl
  SQLite3::SQLite3
  Threads::Threads
)

target_compile_options(_engine PRIVATE -Wall -Wextra -Wpedantic)
if(ENGINE_WERROR)
  target_compile_options(_engine PRIVATE -Werror)
endif()

# The editable install maps `bot` at the source tree and production runs
# `python -m bot.main` from the repo root, so the extension must land beside the
# Python sources rather than in a build tree.
set_target_properties(_engine PROPERTIES
  LIBRARY_OUTPUT_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}/bot"
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}/bot"
)

# The pure logic also builds as a plain executable, so sanitizers can run against
# it without instrumenting the extension module the bot loads.
add_executable(engine_tests
  cpp/engine_tests.cpp
  cpp/config.cpp
  cpp/sse.cpp
  cpp/store.cpp
  cpp/text.cpp
  cpp/turn.cpp
)

target_include_directories(engine_tests PRIVATE
  "${CMAKE_CURRENT_SOURCE_DIR}/cpp"
  "${CMAKE_CURRENT_SOURCE_DIR}/third_party"
)

target_link_libraries(engine_tests PRIVATE SQLite3::SQLite3)

target_compile_options(engine_tests PRIVATE -Wall -Wextra -Wpedantic)
if(ENGINE_WERROR)
  target_compile_options(engine_tests PRIVATE -Werror)
endif()

if(ENGINE_SANITIZE)
  target_compile_options(engine_tests PRIVATE
    -fsanitize=address,undefined -fno-omit-frame-pointer -g)
  target_link_options(engine_tests PRIVATE -fsanitize=address,undefined)
endif()

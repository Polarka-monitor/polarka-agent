cmake_minimum_required(VERSION 3.22.1)
project(rxnorth C CXX ASM)

# RandomX as it stands in the TrueNorth sources (src/randomx), unchanged. The folder is given by the build.
if(NOT RANDOMX_DIR OR NOT EXISTS "${RANDOMX_DIR}/src/randomx.h")
  message(FATAL_ERROR "RANDOMX_DIR must point at src/randomx of the TrueNorth sources")
endif()

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

add_subdirectory("${RANDOMX_DIR}" randomx EXCLUDE_FROM_ALL)

add_library(rxnorth SHARED rxnorth.cpp)
target_include_directories(rxnorth PRIVATE "${RANDOMX_DIR}/src")
target_compile_options(rxnorth PRIVATE -O3 -Wall)
target_link_libraries(rxnorth randomx)
find_package(Threads REQUIRED)
target_link_libraries(rxnorth Threads::Threads)
if(NOT APPLE)
  # see polarka-miner: RandomX's hand-written arm64 code is copied and run; its branches to global labels
  # must not go through the linker's jump table
  target_link_options(rxnorth PRIVATE -Wl,--exclude-libs,ALL -Wl,-Bsymbolic)
  # RandomX's assembly file carries no note about the stack; without this the library asks for an executable
  # stack, which newer systems (glibc 2.41, Debian 13) refuse to give a library loaded at run time
  target_link_options(rxnorth PRIVATE -Wl,-z,noexecstack)
endif()

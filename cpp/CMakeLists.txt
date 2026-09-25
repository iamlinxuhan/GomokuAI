# GomokuAI C++ 计算核心。
#
#   cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
#   cmake --build build -j
#   ./build/gomoku_engine --selftest
#
# 产物 gomoku_engine 是一个**本地 TCP 服务端**，Python 侧的 engine.py 会自动
# 拉起它（见 engine.py 的 config 段）。也可以手工跑：默认监听 127.0.0.1:8888。
#
# 零第三方依赖：只用标准库 + 平台 socket（后者条件编译收敛在 tcp_server.cpp）。

cmake_minimum_required(VERSION 3.16)
project(gomoku_engine CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)          # 严格 -std=c++17，不用 gnu++17

if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
  set(CMAKE_BUILD_TYPE Release CACHE STRING "" FORCE)
endif()

add_executable(gomoku_engine
  src/main.cpp
  src/tables.cpp
  src/board.cpp
  src/evaluate.cpp
  src/search.cpp
  src/opening.cpp
  src/json_util.cpp
  src/tcp_server.cpp
)

target_include_directories(gomoku_engine PRIVATE src)

# --- 警告 ---
# -Wall -Wextra 是硬要求：这个移植里最贵的一类 bug（棋型分类器的高位截断、
# enum 比较、有符号/无符号混用）都由它挡下。
if(MSVC)
  target_compile_options(gomoku_engine PRIVATE /W4 /utf-8)
  # /utf-8：源码里有中文字面量（reason 字段），不指定代码页会被 MSVC 按
  # 本地代码页解释，写出乱码的 JSON。
else()
  target_compile_options(gomoku_engine PRIVATE -Wall -Wextra)
  if(CMAKE_BUILD_TYPE STREQUAL "Release")
    target_compile_options(gomoku_engine PRIVATE -O2 -fno-math-errno)
  endif()
endif()

# --- 平台库 ---
if(WIN32)
  target_link_libraries(gomoku_engine PRIVATE ws2_32)
  # Windows 上多线程版标准库由 MSVC 默认提供；MinGW 需要显式声明线程模型。
  find_package(Threads REQUIRED)
  target_link_libraries(gomoku_engine PRIVATE Threads::Threads)
else()
  find_package(Threads REQUIRED)
  target_link_libraries(gomoku_engine PRIVATE Threads::Threads)
endif()

# --- 构建后自检 ---
# 表枚举写错是本移植唯一会**静默出错**的地方（症状是"分数略偏"，不报错）。
# 每次构建后自动跑一次 `--verify-tables`，让它在构建阶段就暴露。
add_custom_command(TARGET gomoku_engine POST_BUILD
  COMMAND $<TARGET_FILE:gomoku_engine> --verify-tables
  COMMENT "gomoku_engine: 预计算表自检"
  VERBATIM
)

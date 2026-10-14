# GomokuAI C++ 计算核心。
#
#   cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
#   cmake --build build -j
#   ./build/gomoku_engine --selftest
#   ./build/gomoku_engine_ban --selftest        # RIF 禁手版（同一个源，加宏）
#
# 产物 gomoku_engine 是一个**本地 TCP 服务端**，Python 侧的 engine.py 会自动
# 拉起它并显式传 --port（见 config.py 的 PORT_POOL）。手工跑时不给 --port 才
# 落到下面这个默认值；应用本身从不使用它。
#
# gomoku_engine_ban 是**禁手版**：与 gomoku_engine **同一份源码**，仅多了
# `-DGOMOKU_BAN=1`。两份源码不许分叉（`cpp/src/zobrist_table.inc` 也是共用的
# 生成文件，绝不能手改）。规则语义的唯一真源是仓库根的 `ban_rules.py`，C++ 侧
# 的等价实现是 `src/ban.h`。
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

# --- MSVC 运行库：静态链接 ---
# 必须写在这里 —— 该变量在 add_executable 时被读取，放到后面（比如 MSVC 的
# 编译选项那一节）对已创建的目标无效。
#
# 动态（/MD，MSVC 默认）会让 gomoku_engine.exe 的导入表里出现 MSVCP140.dll。
# 而 PyInstaller 打出来的包里**根目录没有** MSVCP140.dll（只有 VCRUNTIME140.dll
# / VCRUNTIME140_1.dll / ucrtbase.dll，Qt 与 numpy 各带一份但都在子目录，子进程
# 的加载器够不着），于是没装 VC++ 运行库的机器上引擎一启动就因
# STATUS_DLL_NOT_FOUND（0xC0000135）退出，Python 侧静默降级到本地引擎 ——
# 用户屏幕上没有任何提示，只表现为"AI 变慢变弱"。静态链接后引擎自带运行库，
# 不再依赖目标机器装没装 Redist。
#
# CMP0091 在 cmake_minimum_required(3.16) 下已是 NEW，该变量才会被采纳。
if(MSVC)
  set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
endif()

# 两个 target 共用同一份源文件列表。抽成变量，加文件时只需改一处。
set(GOMOKU_CORE_SOURCES
  src/main.cpp
  src/tables.cpp
  src/board.cpp
  src/evaluate.cpp
  src/search.cpp
  src/opening.cpp
  src/json_util.cpp
  src/tcp_server.cpp
)

# 两个 target 的警告 / 平台库 / 构建后自检设置完全一致，抽成一个函数 ——
# 复制粘贴几十行会让 free 与 ban 两边在后续改动里悄悄漂移。
function(gomoku_configure_target tgt)
  target_include_directories(${tgt} PRIVATE src)

  # --- 警告 ---
  # -Wall -Wextra 是硬要求：这个移植里最贵的一类 bug（棋型分类器的高位截断、
  # enum 比较、有符号/无符号混用）都由它挡下。
  if(MSVC)
    target_compile_options(${tgt} PRIVATE /W4 /utf-8)
    # /utf-8：源码里有中文字面量（reason 字段），不指定代码页会被 MSVC 按
    # 本地代码页解释，写出乱码的 JSON。
  else()
    target_compile_options(${tgt} PRIVATE -Wall -Wextra)
    if(CMAKE_BUILD_TYPE STREQUAL "Release")
      target_compile_options(${tgt} PRIVATE -O2 -fno-math-errno)
    endif()
  endif()

  # --- 平台库 ---
  find_package(Threads REQUIRED)
  if(WIN32)
    target_link_libraries(${tgt} PRIVATE ws2_32)
    # Windows 上多线程版标准库由 MSVC 提供（见上面的 CMAKE_MSVC_RUNTIME_LIBRARY）；
    # MinGW 需要显式声明线程模型。
    target_link_libraries(${tgt} PRIVATE Threads::Threads)
  else()
    target_link_libraries(${tgt} PRIVATE Threads::Threads)
  endif()

  # --- 构建后自检 ---
  # 表枚举写错是本移植唯一会**静默出错**的地方（症状是"分数略偏"，不报错）。
  # 每次构建后自动跑一次 `--verify-tables`，让它在构建阶段就暴露。
  add_custom_command(TARGET ${tgt} POST_BUILD
    COMMAND $<TARGET_FILE:${tgt}> --verify-tables
    COMMENT "${tgt}: 预计算表自检"
    VERBATIM
  )
endfunction()

# --- free 版（无禁手） ---
add_executable(gomoku_engine ${GOMOKU_CORE_SOURCES})
gomoku_configure_target(gomoku_engine)

# --- ban 版（RIF 禁手）：同一份源，只多一个宏 ---
add_executable(gomoku_engine_ban ${GOMOKU_CORE_SOURCES})
target_compile_definitions(gomoku_engine_ban PRIVATE GOMOKU_BAN=1)
gomoku_configure_target(gomoku_engine_ban)

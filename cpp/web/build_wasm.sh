#!/bin/sh
# 把引擎编译成浏览器能跑的 WebAssembly。
#
#   source ~/emsdk/emsdk_env.sh
#   sh cpp/web/build_wasm.sh
#
# 产物落在 `webapp/dist/`：`gomoku-free.js` 与 `gomoku-ban.js`，每个都是一份
# **自带 wasm 的单文件**（`-sSINGLE_FILE=1`）。这么做不是为了省一个文件，而是
# 为了绕开"MIME 类型"这个平台上无法验证的变量：.wasm 若被服务端按
# application/octet-stream 发出来，`WebAssembly.instantiateStreaming` 会直接
# 拒绝；内联成 base64 之后，浏览器根本不发这个请求。
#
# 两份而不是一份带开关：禁手是 `#if GOMOKU_BAN` 的编译期分支（见 `ban.h`），
# 与桌面版的两个可执行文件同构。运行时按规则选模块，与桌面版"两个引擎两个
# 端口"是同一个结构。
#
# 与 `CMakeLists.txt` 的关系：那边编的是本地 TCP 服务端，多了 `tcp_server.cpp`
# 与 `main.cpp`；这边只编内核 + 一层 C 接口（`wasm_api.cpp`）。
set -eu

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
cd "$ROOT"

OUT=webapp/dist
mkdir -p "$OUT"

CORE="cpp/src/board.cpp cpp/src/evaluate.cpp cpp/src/search.cpp cpp/src/tables.cpp cpp/src/opening.cpp"
API="cpp/web/wasm_api.cpp"

# 异常必须真的能 catch：搜索用 `throw SearchAborted` 从深递归里跳出时间上限
# （`search.h` 的文档串），关掉 catch 会让"到点停手"变成整个模块 abort。
COMMON="-O3 -std=c++17 -Icpp/src -fexceptions -sDISABLE_EXCEPTION_CATCHING=0"

LINK="-sMODULARIZE=1 -sEXPORT_NAME=createGomoku \
-sENVIRONMENT=web,worker,node \
-sSINGLE_FILE=1 -sFILESYSTEM=0 \
-sALLOW_MEMORY_GROWTH=1 -sINITIAL_MEMORY=96MB -sSTACK_SIZE=1MB \
-sASSERTIONS=0 -sEXPORT_ES6=0 \
-sEXPORTED_FUNCTIONS=[\"_gm_init\",\"_gm_new_game\",\"_gm_think\",\"_gm_bancheck\",\"_malloc\",\"_free\"] \
-sEXPORTED_RUNTIME_METHODS=[\"UTF8ToString\",\"HEAPU8\"]"

echo "== 无禁手"
em++ $COMMON $CORE $API $LINK -o "$OUT/gomoku-free.js"

echo "== 禁手"
em++ $COMMON -DGOMOKU_BAN=1 $CORE $API $LINK -o "$OUT/gomoku-ban.js"

ls -l "$OUT"/gomoku-*.js

#!/bin/sh
# 把引擎编译成浏览器能跑的 WebAssembly。
#
#   source ~/emsdk/emsdk_env.sh
#   sh cpp/web/build_wasm.sh
#
# 产物落在 `webapp/dist/`：`gomoku-<free|ban>-<n>.js`，每个都是一份
# **自带 wasm 的单文件**（`-sSINGLE_FILE=1`）。这么做不是为了省一个文件，而是
# 为了绕开"MIME 类型"这个平台上无法验证的变量：.wasm 若被服务端按
# application/octet-stream 发出来，`WebAssembly.instantiateStreaming` 会直接
# 拒绝；内联成 base64 之后，浏览器根本不发这个请求。
#
# 棋盘尺寸与桌面版同一套做法：`BOARD_SIZE` 是编译期常量（`constants.h`），
# 所以四个尺寸 × 两种规则 = 八个模块，运行时按玩家选的那一组加载一份。
# 想在运行时改尺寸就得动热路径上的每一个数组，那是另一类工程。
#
# 禁手是 `#if GOMOKU_BAN` 的编译期分支（见 `ban.h`），与桌面版的八个
# 可执行文件同构。
#
# 与 `CMakeLists.txt` 的关系：那边编的是本地 TCP 服务端，多了 `tcp_server.cpp`
# 与 `main.cpp`；这边只编内核 + 一层 C 接口（`wasm_api.cpp`）。
set -eu

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
cd "$ROOT"

OUT=webapp/dist
mkdir -p "$OUT"
#: 旧命名的产物（`gomoku-free.js` 这一代）不删掉会被 make_dist 一起打进包，
#: 浏览器又永远不会请求它们 —— 白白多背 300 KB。
rm -f "$OUT"/gomoku-*.js

# search.cpp 现在引用了 `openingBookLookup` 与 `dfpnSearch`（v4.4.0 起），
# 少了它们链接会直接失败。网页这边不下发 useBook / proofMode（默认都是 0），
# 所以这两块代码只是躺在包里，不会被走到。
CORE="cpp/src/board.cpp cpp/src/evaluate.cpp cpp/src/search.cpp cpp/src/tables.cpp cpp/src/opening.cpp cpp/src/opening_book.cpp cpp/src/dfpn.cpp"
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

#: 与 config.BOARD_SIZES 同一张表；增删要两边一起改。
SIZES="9 13 15 19"

for N in $SIZES; do
    echo "== ${N} 路 · 无禁手"
    em++ $COMMON -DGOMOKU_BOARD_SIZE=$N $CORE $API $LINK \
        -o "$OUT/gomoku-free-$N.js"
    echo "== ${N} 路 · 禁手"
    em++ $COMMON -DGOMOKU_BAN=1 -DGOMOKU_BOARD_SIZE=$N $CORE $API $LINK \
        -o "$OUT/gomoku-ban-$N.js"
done

ls -l "$OUT"/gomoku-*.js

#!/bin/sh
# 打一个能直接传 Toy 平台的包。
#
#   source ~/emsdk/emsdk_env.sh && sh cpp/web/build_wasm.sh
#   sh webapp/make_dist.sh
#
# 产物：
#   webapp/dist/        —— **扁平**的一层，index.html 就在根上
#   webapp/gomoku-web.zip
#
# 为什么是扁平的一层：`engine-worker.js` 里用 `importScripts('./gomoku-ban.js')`，
# 相对路径按 **Worker 脚本自己的 URL** 解析。源码目录里引擎在 dist/ 下、页面
# 文件在上一层，两边一合并就会指错地方。与其在打包时改路径（那样本地测的和
# 线上跑的不是同一份文件），不如让开发目录 = 发布目录：先拷进来，再打包，
# 本地起的服务也指向 dist/。
set -eu

APP=$(cd "$(dirname "$0")" && pwd)
OUT=$APP/dist
ZIP=$APP/gomoku-web.zip

# 开局库是生成物（tools/build_book_web.py 从 opening_book.py 转出来的），
# 缺了就会静默失去入门档的开局 —— 宁可在这里停下。
[ -f "$APP/book.js" ] || {
    echo "缺少 webapp/book.js，先生成：uv run --frozen python tools/build_book_web.py" >&2
    exit 1
}

mkdir -p "$OUT"
cp "$APP/index.html" "$APP/style.css" "$APP/app.js" "$APP/engine-worker.js" \
   "$APP/book.js" "$OUT/"

for f in gomoku-free.js gomoku-ban.js; do
    [ -f "$OUT/$f" ] || {
        echo "缺少 $OUT/$f，先跑：source ~/emsdk/emsdk_env.sh && sh cpp/web/build_wasm.sh" >&2
        exit 1
    }
done

rm -f "$ZIP"
(cd "$OUT" && zip -q -r "$ZIP" .)

# --- 平台的硬门槛 -----------------------------------------------------------
# 307000: index.html 未压缩必须 ≤ 1 MB；整包 ≤ 50 MB。
SIZE=$(wc -c < "$OUT/index.html")
[ "$SIZE" -le 1048576 ] || { echo "index.html 超了 1 MB：$SIZE" >&2; exit 1; }
TOTAL=$(wc -c < "$ZIP")
[ "$TOTAL" -le 52428800 ] || { echo "整个包超了 50 MB：$TOTAL" >&2; exit 1; }

echo "index.html  $SIZE B"
echo "包大小      $TOTAL B  ($(awk "BEGIN{printf \"%.2f\", $TOTAL/1048576}") MB)"
ls -l "$OUT"

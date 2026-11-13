# -*- coding: utf-8 -*-
"""把 ``data/gomocup_2026.json`` 生成两边的数据文件。

    uv run --frozen python tools/gen_gomocup_data.py

产物（**都是生成物，不要手改**）：

* ``cpp/src/gomocup_book_data.h`` —— C++ 引擎读的开局表原始数据；
* ``gomocup_book_data.py``          —— Python 本地引擎镜像的同一份数据。

两边共用同一份中间数据（``data/gomocup_2026.json``）是刻意的：D4 展开、尺寸
平移、冲突决胜这些**算法**各写一遍，但**数据**只有一处来源。任何一边漏了一条
开局线，跨语言对拍会立刻报出来。

坐标口径：JSON 里是 ``(x=列, y=行)``（pbrain 协议口径，15×15 原生），生成物
原样保留；转成线性索引是各引擎自己的事。
"""

from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "data", "gomocup_2026.json")
H_OUT = os.path.join(ROOT, "cpp", "src", "gomocup_book_data.h")
PY_OUT = os.path.join(ROOT, "gomocup_book_data.py")

#: 最长一条开局线的手数上限。数组按它开，生成时断言没有超出的。
MAX_MOVES = 32


def load():
    with open(SRC, encoding="utf-8") as fh:
        return json.load(fh)


def check(doc):
    seen = set()
    for e in doc["entries"]:
        name = e["name"]
        if name in seen:
            raise SystemExit("开局线重名：%s" % name)
        seen.add(name)
        if not (1 <= len(e["moves"]) <= MAX_MOVES):
            raise SystemExit("%s 的手数是 %d，超出 1..%d"
                             % (name, len(e["moves"]), MAX_MOVES))
        for x, y in e["moves"] + [e["response"]]:
            if not (0 <= x < 15 and 0 <= y < 15):
                raise SystemExit("%s 的坐标 (%d,%d) 不在 15×15 上"
                                 % (name, x, y))
    return doc


def _provenance(doc):
    """来源行。JSON 换过口径（以前是 crate 的转录），这里对两种都说得通。"""
    src = doc.get("source", "?")
    ver = doc.get("version")
    f = doc.get("file")
    lic = doc.get("license")
    head = src + (" %s" % ver if ver else "")
    if f:
        head += " 的 %s" % f
    if lic:
        head += "（%s）" % lic
    return head


def write_header(doc):
    out = [
        "// 生成物：由 tools/gen_gomocup_data.py 从 data/gomocup_2026.json 生成。",
        "// **不要手工编辑。**",
        "//",
        "// 来源：%s，取得于 %s。" % (_provenance(doc), doc.get("retrieved", "?")),
        "// moves 是官方开局着法（黑先交替）；response 由本项目的引擎在终局上",
        "// 搜索得出，**不是官方数据**。",
        "// 坐标是 (x=列, y=行)，15×15 原生；由 opening_book.cpp 做尺寸平移与",
        "// D4 展开。",
        "",
        "#pragma once",
        "",
        "namespace gomoku {",
        "",
        "struct GomocupRawEntry {",
        "    const char* name;",
        "    int n;",
        "    int moves[%d][2];" % MAX_MOVES,
        "    int response[2];",
        "};",
        "",
        "inline constexpr GomocupRawEntry GOMOCUP_RAW[] = {",
    ]
    for e in doc["entries"]:
        mv = ", ".join("{%d, %d}" % (x, y) for x, y in e["moves"])
        out.append('    {"%s", %d, {%s}, {%d, %d}},'
                   % (e["name"], len(e["moves"]), mv,
                      e["response"][0], e["response"][1]))
    out += [
        "};",
        "",
        "inline constexpr int GOMOCUP_RAW_COUNT =",
        "    static_cast<int>(sizeof(GOMOCUP_RAW) / sizeof(GOMOCUP_RAW[0]));",
        "",
        "}  // namespace gomoku",
        "",
    ]
    with open(H_OUT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out))


def write_python(doc):
    out = [
        "# -*- coding: utf-8 -*-",
        '"""生成物：由 tools/gen_gomocup_data.py 从 data/gomocup_2026.json 生成。',
        "",
        "**不要手工编辑。** 来源：%s，取得于 %s。" % (
            _provenance(doc), doc.get("retrieved", "?")),
        "moves 是官方开局着法；response 由本项目的引擎算出，不是官方数据。",
        "坐标是 ``(x=列, y=行)``，15×15 原生；尺寸平移与 D4 展开见",
        "``gomocup_book.py``。",
        '"""',
        "",
        "GOMOCUP_RAW = [",
    ]
    for e in doc["entries"]:
        mv = ", ".join("(%d, %d)" % (x, y) for x, y in e["moves"])
        out.append('    ("%s", [%s], (%d, %d)),'
                   % (e["name"], mv, e["response"][0], e["response"][1]))
    out += ["]", ""]
    with open(PY_OUT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out))


def main():
    doc = check(load())
    write_header(doc)
    write_python(doc)
    print("写出 %s 与 %s（%d 条开局线）"
          % (os.path.relpath(H_OUT, ROOT), os.path.relpath(PY_OUT, ROOT),
             len(doc["entries"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())

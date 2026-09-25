# -*- coding: utf-8 -*-
"""把 ``engine_local._ZOBRIST`` 导出成 C++ 头文件。

**为什么是"导出"而不是"在 C++ 里重算"**：Python 的
``random.Random(seed).getrandbits(64)`` 走的是 CPython 对 MT19937 的整数播种
（``init_by_array`` 展开成 32 位字）加一个按 32 位字拼接的取数顺序。用
``std::mt19937_64`` 复刻这套播种/取数**在任何一处细节上出错都不会报错** ——
只会得到一张"看着随机、其实与 Python 不同"的表，而 Zobrist 表不同并不影响
C++ 自身的正确性（增量与全量用的是同一张表），所以没有任何测试会变红。
既然正确性不依赖它、复刻又有静默出错风险，就直接导出。

用法::

    python cpp/tools/gen_zobrist.py            # 写 src/zobrist_table.inc
    python cpp/tools/gen_zobrist.py --check     # 只校验已生成文件是否过期

``--check`` 的用途是"改了种子却忘了重生成"——那时 C++ 侧的表会悄悄落后于
Python，而增量自检仍然全绿。
"""

from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CPP_DIR = os.path.dirname(HERE)
ROOT = os.path.dirname(CPP_DIR)          # …/GomokuAI
sys.path.insert(0, ROOT)

import engine_local as E  # noqa: E402

OUT = os.path.join(CPP_DIR, "src", "zobrist_table.inc")

HEADER = """// 本文件由 cpp/tools/gen_zobrist.py 生成，请勿手改。
//
// 源：engine_local._ZOBRIST —— 两个 361 项的 64 位表，由
//     random.Random(0x9E3779B9 + p).getrandbits(64) 逐项抽取。
// 第一维是行棋方（0=黑 / 1=白）。C++ 的 Board.hash 与本地引擎逐位相同，
// 不是为了跨语言对拍，而是为了让"同一张表驱动增量与全量"这条不变式在
// 两侧都成立（见 engine_local._zobrist_row 的 docstring）。
"""


def render() -> str:
    lines = [HEADER, "static const uint64_t ZOBRIST_TABLE[2][361] = {"]
    for p in range(2):
        lines.append("    {")
        row = E._ZOBRIST[p]
        for i in range(0, 361, 4):
            chunk = ", ".join("0x%016xULL" % v for v in row[i:i + 4])
            lines.append("        " + chunk + ",")
        lines.append("    },")
    lines.append("};")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="导出 Zobrist 表到 C++")
    ap.add_argument("--check", action="store_true",
                    help="只校验已生成的文件是否与当前 Python 表一致")
    args = ap.parse_args()

    text = render()
    if args.check:
        if not os.path.exists(OUT):
            print(f"缺少 {OUT}", file=sys.stderr)
            return 1
        with open(OUT, encoding="utf-8") as fh:
            cur = fh.read()
        if cur != text:
            print(f"{OUT} 已过期，请重新运行 gen_zobrist.py", file=sys.stderr)
            return 1
        print(f"{OUT} 是最新的")
        return 0

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write(text)
    # 顺手做一次"表内元素互不相同"的检查。engine_local._zobrist_row 的
    # docstring 记着这个错误真的发生过：发生器建在循环里，361 项全相同，
    # 而所有一致性测试仍然全绿。
    for p in range(2):
        assert len(set(E._ZOBRIST[p])) == 361, f"第 {p} 张 Zobrist 表有重复值"
    assert not (set(E._ZOBRIST[0]) & set(E._ZOBRIST[1])), "两张 Zobrist 表有交集"
    print(f"已写入 {OUT}（722 项，无重复）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

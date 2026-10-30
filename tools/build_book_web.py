# -*- coding: utf-8 -*-
"""把 ``opening_book.py`` 翻译成网页版能查的表：``webapp/book.js``。

## 为什么需要这一步

``opening_book.py`` 的键是 ``Board.hash``（Zobrist 异或），而那张 Zobrist 表
是 ``random.Random(0x9E3779B9)`` 抽出来的 —— 在浏览器里重算它等于把 Python
的 MT19937 再实现一遍，为了 50 条表项不值当。

所以换个方向：这里在 Python 侧**把每个局面走一遍**，反查出它的键，然后写成
"局面 → 着法"的明文表。网页只做一次字符串拼接，不需要任何哈希。

## 局面怎么枚举

开局库只覆盖盘上 ≤ 3 子的局面，而且树是从天元长出来的（见
``tools/build_book.py`` 的模块文档），所以候选点只可能在中心附近。这里枚举
``CENTER ± 5`` 的方形区域内 1/2/3 子的全部摆法，逐个算哈希去撞表 —— 撞不上
的摆法本来就不在库里，撞上的就是库里的那 50 条。

**这个脚本只读 ``opening_book.py``，不写它。** 那条"生成物不许手改"的规矩
针对的是建库本身（``tools/build_book.py``）；这里是同一份数据的另一种编码。

用法::

    uv run --frozen python tools/build_book_web.py
"""

from __future__ import annotations

import itertools
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import engine_local as EL                                # noqa: E402
from opening_book import BOOK                            # noqa: E402

BOARD = EL.BOARD_SIZE
CENTER = BOARD // 2
SPAN = 5                       # 中心 ±5 的方形区域
OUT = os.path.join(ROOT, "webapp", "book.js")


def cell_hash(idx: int, color: int) -> int:
    return EL._ZOBRIST[color - 1][idx]


def stone_hash(stones) -> int:
    h = 0
    for idx, color in stones:
        h ^= cell_hash(idx, color)
    return h


def key_of(stones, player: int) -> str:
    parts = ",".join("%d:%d" % (idx, color) for idx, color in sorted(stones))
    return "%s|%d" % (parts, player)


def collect() -> dict:
    """枚举中心附近 1~3 子的摆法，撞上 `BOOK` 的那些 → ``{明文键: [idx, 分, 层]}``。

    单独拆出来是给 ``tests/test_book_web.py`` 用的：``webapp/book.js`` 是生成物，
    测试要能**不写文件**地重算一遍，再去和磁盘上那份比。
    """
    lo = CENTER - SPAN
    hi = CENTER + SPAN
    cells = [r * BOARD + c
             for r in range(lo, hi + 1)
             for c in range(lo, hi + 1)
             if 0 <= r < BOARD and 0 <= c < BOARD]

    found = {}
    for n in (1, 2, 3):
        # 子色必须交替：1 子只有黑，2 子是黑白，3 子是黑白黑。
        colors = (1, 2, 1)[:n]
        for combo in itertools.combinations(cells, n):
            # 黑白黑里只有中间那颗（n=3 时的第二颗）能换位置
            perms = (itertools.permutations(colors) if n <= 3 else [colors])
            for perm in set(perms):
                stones = tuple(zip(combo, perm))
                h = stone_hash(stones)
                for player in (1, 2):
                    got = BOOK.get(h ^ (0 if player == 1 else EL._TT_SALT_W))
                    if got is None:
                        continue
                    k = key_of(stones, player)
                    if k in found and found[k] != list(got):
                        raise SystemExit("键冲突：%s" % k)
                    found[k] = list(got)

    if not found:
        raise SystemExit("一条都没撞上 —— 枚举范围或哈希算法错了")
    return found


def render(found: dict) -> str:
    """把 ``collect()`` 的结果渲染成 ``book.js`` 的全文。"""
    lines = [
        "/* 开局库（网页版）。**生成物：由 tools/build_book_web.py 生成，不要手改。**",
        " *",
        " * 源数据是 opening_book.py 的那张 Zobrist 表，这里换成\"局面 → 着法\"的",
        " * 明文键，省掉在浏览器里重算 Zobrist。键 = 按索引升序的 `格子:子色` 列表",
        " * + `|` + 走子方，例如 \"171:1|2\" 表示黑子在天元、轮到白方。",
        " *",
        " * 值的三项与搜索输出同义：[线性格索引, 走子方视角的分值, 建库时的层数]。",
        " * 只覆盖盘上 1~3 子的局面，与 opening_book.BOOK_STONES 一致。",
        " */",
        "self.GomokuBook = {",
        "  stones: 3,",
        "  table: {",
    ]
    for k in sorted(found):
        idx, score, depth = found[k]
        lines.append('    "%s": [%d, %d, %d],' % (k, idx, score, depth))
    lines += ["  },", "};", ""]
    return "\n".join(lines)


def main() -> int:
    found = collect()
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write(render(found))
    print("%d 条 → %s（%d 字节）" % (len(found), OUT, os.path.getsize(OUT)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

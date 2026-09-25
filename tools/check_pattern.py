# -*- coding: utf-8 -*-
"""棋型分类器的对照验算（不是 pytest，是一次性/回归用的独立校验器）。

`engine._line_level` 是静态评估的唯一语义来源，它一旦错了，搜索会稳定地
做错选择 —— 而错法往往很隐蔽（比如把冲四当活四）。所以这里用一个**慢但
显然正确**的参照实现逐例对照：

    参照实现直接在整条线上穷举
      ① |F|：所有空点，逐个试落子，看是否连成五
      ② 补一子 / 补两子的所有组合，同样穷举
    把结论按同一套定义翻译成等级，与快路径比对。

参照实现不与快路径共享任何一行代码，也不使用 `_segment` / `_completion_set`
这些"可能有同样误解"的辅助函数 —— 它只用 `_has_five_bits`（全局唯一的
胜负判定，另有独立测试）和纯 Python 循环。

用法::

    .venv/bin/python tools/check_pattern.py                 # 随机 + 穷举
    .venv/bin/python tools/check_pattern.py --exhaustive 6  # 长度 6 的全枚举
"""

from __future__ import annotations

import argparse
import itertools
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import engine_local as E  # noqa: E402


def _to_global(cells, line_my, line_opp):
    """线上局部位图 -> 全局 361 位位图（用整条线的格列表映射）。"""
    my = opp = 0
    for t, idx in enumerate(cells):
        if line_my >> t & 1:
            my |= 1 << idx
        elif line_opp >> t & 1:
            opp |= 1 << idx
    return my, opp


def _f_mask(cells, my, opp):
    """参照实现的 |F|：逐空点试落子，用独立的胜负判定问"成了吗"。"""
    m = 0
    full = 0
    for t in range(len(cells)):
        full |= 1 << t
    for t in range(len(cells)):
        if (my | opp) >> t & 1:
            continue
        if E._has_five_bits(my | (1 << t)):
            m |= 1 << t
    return m


def _slow_level(cells, my, opp):
    """参照实现的等级判定 —— 照定义直写，与快路径零共享。"""
    if not my:
        return E.LV_NONE
    if E._has_five_bits(my):
        return E.LV_FIVE

    f = _f_mask(cells, my, opp)
    n = f.bit_count()
    if n >= 2:
        return E.LV_FOUR_LIVE
    if n == 1:
        return E.LV_FOUR

    empties = [t for t in range(len(cells)) if not (my | opp) >> t & 1]
    best1 = 0
    for e in empties:
        v = _f_mask(cells, my | (1 << e), opp).bit_count()
        best1 = max(best1, v)
    if best1 >= 2:
        return E.LV_THREE_LIVE
    if best1 == 1:
        return E.LV_THREE_SLEEP

    best2 = 0
    for e1, e2 in itertools.combinations(empties, 2):
        v = _f_mask(cells, my | (1 << e1) | (1 << e2), opp).bit_count()
        best2 = max(best2, v)
    if best2 >= 2:
        return E.LV_TWO_LIVE
    if best2 == 1:
        return E.LV_TWO_SLEEP
    return E.LV_ONE


def fast_level(cells, my, opp):
    seg_my, seg_opp, wins = E._segment(my, opp, len(cells))
    return E._line_level(seg_my, seg_opp, wins)


def compare(cells, my, opp):
    """返回 ``(快, 慢)``；两者等级应当一致。"""
    return fast_level(cells, my, opp), _slow_level(cells, my, opp)


def _line_cells(length):
    """造一条长度为 ``length`` 的假线（只用于分类器对照，与棋盘无关）。"""
    return tuple(range(length))


def run_exhaustive(length):
    """全枚举：线上每格 ∈ {空, 我, 对手}，共 3^length 种。"""
    bad = []
    total = 0
    for combo in itertools.product(range(3), repeat=length):
        my = opp = 0
        for t, v in enumerate(combo):
            if v == 1:
                my |= 1 << t
            elif v == 2:
                opp |= 1 << t
        total += 1
        cells = _line_cells(length)
        a, b = compare(cells, my, opp)
        if a != b:
            bad.append((combo, a, b))
            if len(bad) > 12:
                break
    return total, bad


def _name(lv):
    return {
        E.LV_NONE: "无", E.LV_ONE: "活一", E.LV_TWO_SLEEP: "眠二",
        E.LV_TWO_LIVE: "活二", E.LV_THREE_SLEEP: "眠三",
        E.LV_THREE_LIVE: "活三", E.LV_FOUR: "冲四",
        E.LV_FOUR_LIVE: "活四", E.LV_FIVE: "五连",
    }[lv]


def _render(combo):
    return "".join("." if v == 0 else ("X" if v == 1 else "O") for v in combo)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exhaustive", type=int, default=0,
                    help="对指定长度做全枚举（3^L 种，L<=9 左右）")
    ap.add_argument("--len", type=int, default=19, help="随机线的长度")
    ap.add_argument("--cases", type=int, default=4000, help="随机条数")
    a = ap.parse_args()

    import random
    rng = random.Random(20260919)

    total = 0
    bad = []
    for _ in range(a.cases):
        cells = _line_cells(a.len)
        my = opp = 0
        for t in range(a.len):
            v = rng.random()
            # 偏稀疏：真实盘面绝大多数线是空的或只有几颗子
            if v < 0.18:
                my |= 1 << t
            elif v < 0.36:
                opp |= 1 << t
        total += 1
        x, y = compare(cells, my, opp)
        if x != y:
            bad.append((my, opp, x, y))
    print(f"随机 {total} 条（长度 {a.len}）：不一致 {len(bad)} 条")
    for my, opp, x, y in bad[:10]:
        print(f"  my={my:0{a.len}b} opp={opp:0{a.len}b} 快={_name(x)} 慢={_name(y)}")

    if a.exhaustive:
        L = a.exhaustive
        n, bad2 = run_exhaustive(L)
        print(f"全枚举长度 {L}（{n} 种）：不一致 {len(bad2)} 条")
        for combo, x, y in bad2[:10]:
            print(f"  {_render(combo)} 快={_name(x)} 慢={_name(y)}")

    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

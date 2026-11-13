# -*- coding: utf-8 -*-
"""Gomocup 2026 官方开局集（C++ ``opening_book.cpp`` 的 Python 镜像）。

## 数据与展开

原始数据在 ``data/gomocup_2026.json``，两边由 ``tools/gen_gomocup_data.py``
各自生成一份只读表（C++ 是 ``cpp/src/gomocup_book_data.h``，这里是
``gomocup_book_data.py``）。算法分头实现、数据只有一处来源。

每条开局线存的是**每个前缀**，不只是最终局面：

* 前 k 手的局面 → 线路第 k+1 手；
* 最后局面 → 本项目引擎在终局上搜出来的应手（官方只公布开局着法）。

对手只要照着官方线走，我们每一步都能立即应；只在最终局面查表，普通对局
几乎永远命中不了。

对每个前缀再做 D4 展开（4 旋转 + 4 镜像，共 8 个变换），键是
``hash ^ (先手方 == 黑 ? 0 : TT_SALT_W)``。同一键落到不同应手时取**线性格
索引最小**的那一手 —— 规则只写在这里和 ``opening_book.cpp``，两边必须一致。

## 尺寸

15×15 的 ``(x, y)`` 平移到 n×n：``col = n/2 + (x-7)``、``row = n/2 + (y-7)``。
任何一子越界就整条线丢弃（9×9 基本为空，那边由 ``opening_book.py`` 的小型
库兜底）。

## 线程

表按尺寸缓存，纯只读；查询可以在任意线程调用。``configure_size`` 只负责
提前构建，查询时传对 ``size`` 即可。
"""

from __future__ import annotations

import random

from gomocup_book_data import GOMOCUP_RAW

#: 与 ``engine_local._ZOBRIST_SEED_BB`` / C++ ``zobrist_table.inc`` 同一个种子。
ZOBRIST_SEED = 0x9E3779B9
#: 白方行棋的键扰动，与 ``engine_local._TT_SALT_W`` / C++ ``TT_SALT_W`` 相同。
TT_SALT_W = 0x5D5D5D5D5D5D5D5D

#: 15×15 原生盘的中心（``(x, y)``）。
_NATIVE_CENTER = 7

_TABLES: dict = {}
_STATS: dict = {}
_ZOBRIST: dict = {}


def _zobrist(n):
    """n×n 的 Zobrist 表（黑/白两行）。

    独立实例 + 固定种子，与 ``engine_local._zobrist_row`` 逐项相同 ——
    ``tests/test_gomocup_book.py`` 会用真实 ``Board.hash`` 对拍。
    """
    z = _ZOBRIST.get(n)
    if z is None:
        cells = n * n
        # 发生器必须在循环**外**建一次。写在生成器表达式里就是每格新建一个
        # 同种子实例、取它的第一个输出 —— 整张表退化成常量（`engine_local`
        # 的 `_zobrist_row` 那段注释记着同一个坑）。
        rng_b = random.Random(ZOBRIST_SEED)
        rng_w = random.Random(ZOBRIST_SEED + 1)
        z = (tuple(rng_b.getrandbits(64) for _ in range(cells)),
             tuple(rng_w.getrandbits(64) for _ in range(cells)))
        _ZOBRIST[n] = z
    return z


def _translate(x, y, n):
    """15×15 的 ``(x, y)`` → n×n 的 ``(row, col)``；越界返回 ``None``。"""
    m = n // 2
    c = m + (x - _NATIVE_CENTER)
    r = m + (y - _NATIVE_CENTER)
    if 0 <= r < n and 0 <= c < n:
        return r, c
    return None


def _xform(t, n, r, c):
    """D4 的 8 个变换。与 ``opening_book.cpp`` 的 ``xform`` 逐行对应。"""
    if t == 0:
        return r, c
    if t == 1:                        # 顺 90°
        return c, n - 1 - r
    if t == 2:                        # 180°
        return n - 1 - r, n - 1 - c
    if t == 3:                        # 270°
        return n - 1 - c, r
    if t == 4:                        # 水平镜像
        return r, n - 1 - c
    if t == 5:                        # 垂直镜像
        return n - 1 - r, c
    if t == 6:                        # 主对角转置
        return c, r
    return n - 1 - c, n - 1 - r       # 副对角转置


def _hash(stones, n):
    """``stones`` 是 ``[(r, c, player)]``；返回与 ``Board.hash`` 相同的值。"""
    z = _zobrist(n)
    h = 0
    for r, c, p in stones:
        h ^= z[p - 1][r * n + c]
    return h


def build(n):
    """构建 n×n 的那张表（``{key: 线性格索引}``）。"""
    table = {}
    lines_kept = 0
    prefix_nodes = 0
    for _name, raw_moves, raw_response in GOMOCUP_RAW:
        coords = [_translate(x, y, n) for x, y in raw_moves]
        resp = _translate(raw_response[0], raw_response[1], n)
        if resp is None or any(v is None for v in coords):
            continue                           # 这条线在本尺寸上放不下
        lines_kept += 1
        for k in range(1, len(coords) + 1):
            prefix = coords[:k]
            mv = coords[k] if k < len(coords) else resp
            side = 1 if k % 2 == 0 else 2       # 走了 k 手之后轮到谁
            prefix_nodes += 1
            for t in range(8):
                stones = []
                for i, (r, c) in enumerate(prefix):
                    tr, tc = _xform(t, n, r, c)
                    stones.append((tr, tc, 1 if i % 2 == 0 else 2))
                tr, tc = _xform(t, n, mv[0], mv[1])
                idx = tr * n + tc
                key = _hash(stones, n) ^ (0 if side == 1 else TT_SALT_W)
                prev = table.get(key)
                # 冲突决胜：线性格索引最小的那一手。C++ 侧逐字相同。
                if prev is None or idx < prev:
                    table[key] = idx
    _TABLES[n] = table
    _STATS[n] = {"lines": lines_kept, "nodes": prefix_nodes,
                 "entries": len(table)}
    return table


def configure_size(n):
    """提前建好 n×n 的表（``engine_local.configure_size`` 顺手调用）。"""
    if n not in _TABLES:
        build(n)
    return _TABLES[n]


def table(n):
    return _TABLES.get(n) or build(n)


def entry_count(n):
    """这张尺寸的书里有多少个键（0 = 这本书在这个尺寸上为空）。"""
    return len(table(n))


def stats(n):
    table(n)
    return dict(_STATS[n])


def lookup(board, player, size=None):
    """命中返回线性格索引，未命中返回 ``None``。

    ``board`` 是二维数组（0/1/2），``size`` 默认取它的边长。
    """
    n = int(board.shape[0]) if getattr(board, "shape", None) else len(board)
    if size is not None and size != n:
        n = int(size)
    tbl = table(n)
    if not tbl:
        return None
    z = _zobrist(n)
    h = 0
    for r in range(n):
        row = board[r]
        base = r * n
        for c in range(n):
            p = int(row[c])
            if p:
                h ^= z[p - 1][base + c]
    return tbl.get(h ^ (0 if player == 1 else TT_SALT_W))

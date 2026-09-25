# -*- coding: utf-8 -*-
"""胜负判定：四方向成五、边界、长连、以及**线性索引换行**的误判回归。

``_has_five_bits`` 用 ``bits & bits>>s & bits>>2s & ...`` 做全盘检测，快是快，
但线性索引把行末与下一行行首**接在一起**。若不滤掉这类假起点，(0,14)-(0,18)
之后紧跟的 (1,0) 会被当成"第六颗连子"，甚至 `(0,15)..(0,18) + (1,0)` 这种
四子加一子的组合会被判成五连。本文件专门盯住这一点。
"""

from __future__ import annotations

import numpy as np
import pytest

from engine_local import BOARD_SIZE, Board, check_win, _has_five_bits, _bits_from_array

N = BOARD_SIZE
DIRS = ((0, 1), (1, 0), (1, 1), (1, -1))


# ------------------------------------------------------------------ 参考实现

def ref_has_five(grid, player):
    """朴素参考：逐窗口全扫，边界在窗口内判定。"""
    for dr, dc in DIRS:
        for r in range(N):
            for c in range(N):
                if all(0 <= r + dr * k < N and 0 <= c + dc * k < N
                       and grid[r + dr * k][c + dc * k] == player
                       for k in range(5)):
                    return True
    return False


def grid_of(black=(), white=()):
    g = [[0] * N for _ in range(N)]
    for r, c in black:
        g[r][c] = 1
    for r, c in white:
        g[r][c] = 2
    return g


def bits_of(grid, player):
    return _bits_from_array(np.array(grid, dtype=np.uint8), player)


# ------------------------------------------------------------------ 基本方向

@pytest.mark.parametrize("dr,dc,name", [
    (0, 1, "横"), (1, 0, "竖"), (1, 1, "撇\\"), (1, -1, "捺/"),
])
def test_five_in_each_direction(dr, dc, name):
    """四方向各取一条**中线**上的五连，逐格偏移都要能判出来。"""
    for r0 in (0, 5, 7):
        for c0 in (0, 4, 9):
            if not all(0 <= r0 + dr * k < N and 0 <= c0 + dc * k < N
                       for k in range(5)):
                continue
            cells = [(r0 + dr * k, c0 + dc * k) for k in range(5)]
            g = grid_of(black=cells)
            assert ref_has_five(g, 1), "参考实现都不认，用例本身写错了"
            assert check_win(np.array(g, dtype=np.uint8), 1), f"{name} 方向漏判 {cells}"


def test_long_connect_six_and_seven_counts():
    """长连（六连、七连）判胜 —— 含五窗口，天然成立。"""
    for length in (6, 7, 9):
        cells = [(7, 2 + k) for k in range(length)]
        g = grid_of(black=cells)
        assert check_win(np.array(g, dtype=np.uint8), 1), f"{length} 连漏判"


def test_only_the_asking_player_counts():
    """对手的子不能算进我的五连 —— 黑四子 + 白一子接上不是黑胜。"""
    g = grid_of(black=[(7, 2 + k) for k in range(4)], white=[(7, 6)])
    a = np.array(g, dtype=np.uint8)
    assert not check_win(a, 1)
    assert not check_win(a, 2)


# ------------------------------------------------------------------ 反例

def test_gap_breaks_five():
    """`XXX_XX` 与 `XX_XXX` 都不是五连。"""
    for cells in ([(7, 2), (7, 3), (7, 4), (7, 6), (7, 7)],
                  [(7, 2), (7, 3), (7, 5), (7, 6), (7, 7)]):
        g = grid_of(black=cells)
        assert not check_win(np.array(g, dtype=np.uint8), 1), f"{cells} 被误判成五连"


def test_four_is_not_five():
    g = grid_of(black=[(9, 4), (9, 5), (9, 6), (9, 7)])
    assert not check_win(np.array(g, dtype=np.uint8), 1)


# ------------------------------------------------------------------ 换行回归（本文件的重点）

def test_wrap_row_end_plus_next_row_start_is_not_five():
    """(0,15)(0,16)(0,17)(0,18)(1,0) —— 线性索引相邻，但几何上不相邻。

    这是 `bits >> 1` 会踩到的坑：idx 15,16,17,18,19 连续，五个位全置。
    必须靠 ``_FIVE_STARTS`` 滤掉，否则误判。
    """
    cells = [(0, 15), (0, 16), (0, 17), (0, 18), (1, 0)]
    g = grid_of(black=cells)
    assert not ref_has_five(g, 1), "参考实现认为这是五连？用例写错了"
    b = bits_of(g, 1)
    # 先确认这五格在步长 1 下确实连成一段（即这个用例真的在考验滤网）
    assert b >> 15 & 0b11111 == 0b11111
    assert not _has_five_bits(b), "行末四子 + 下一行行首被误判成五连"


def test_wrap_vertical_and_diagonal():
    """竖、撇、捺三个方向的同类换行组合。"""
    cases = [
        ("竖：末行四子 + 下一列列首", [(15, 5), (16, 5), (17, 5), (18, 5), (0, 6)]),
        ("撇：右下角四子 + 左上角", [(15, 15), (16, 16), (17, 17), (18, 18), (0, 0)]),
        ("捺：左下四子 + 右上角", [(15, 4), (16, 3), (17, 2), (18, 1), (0, 18)]),
    ]
    for name, cells in cases:
        g = grid_of(black=cells)
        assert not ref_has_five(g, 1), f"{name}: 参考实现认为成立？用例写错了"
        assert not check_win(np.array(g, dtype=np.uint8), 1), f"{name} 被误判成五连"


def test_real_five_at_board_edges_is_still_found():
    """换行滤网不能把**真实的**边缘五连一起滤掉。"""
    cases = [
        [(0, 14 + k) for k in range(5)],          # 首行最右的五连
        [(0, k) for k in range(5)],               # 首行最左
        [(14 + k, 0) for k in range(5)],          # 首列最下
        [(18, 14 + k) for k in range(5)],         # 末行
        [(14 + k, 14 + k) for k in range(5)],     # 撇对角线最右下
        [(14 + k, 4 - k) for k in range(5)],      # 捺对角线（左下角）
        [(4 - k, 14 + k) for k in range(5)],      # 捺对角线（右上角）
    ]
    for cells in cases:
        if not all(0 <= r < N and 0 <= c < N for r, c in cells):
            continue
        g = grid_of(black=cells)
        assert check_win(np.array(g, dtype=np.uint8), 1), f"边缘真五连漏判 {cells}"


# ------------------------------------------------------------------ 随机差分

@pytest.mark.parametrize("seed", range(8))
@pytest.mark.parametrize("density", [0.02, 0.15, 0.45])
def test_random_boards_agree_with_reference(seed, density):
    """随机盘面上 ``check_win`` 与朴素参考逐一一致（两边都双向核对）。"""
    rng = np.random.RandomState(seed * 131 + int(density * 100))
    g = [[0] * N for _ in range(N)]
    for r in range(N):
        for c in range(N):
            x = rng.rand()
            if x < density / 2:
                g[r][c] = 1
            elif x < density:
                g[r][c] = 2
    a = np.array(g, dtype=np.uint8)
    for p in (1, 2):
        assert check_win(a, p) == ref_has_five(g, p), f"seed={seed} 密度={density} p={p}"


# ------------------------------------------------------------------ Board 上的同一份实现

def test_board_has_five_matches_check_win():
    """``Board.has_five`` 与 ``check_win`` 必须是同一答案（同一份核心）。"""
    g = grid_of(black=[(7, 2 + k) for k in range(5)])
    a = np.array(g, dtype=np.uint8)
    b = Board.from_array(a)
    assert b.has_five(1) and check_win(a, 1)
    assert not b.has_five(2) and not check_win(a, 2)


def test_makes_five_agrees_with_reference():
    """增量版的"这一子是否成五"与朴素窗口扫描一致。"""
    rng = np.random.RandomState(7)
    for trial in range(60):
        g = [[0] * N for _ in range(N)]
        n = rng.randint(1, 24)
        cells = []
        for _ in range(n):
            r, c = rng.randint(0, N), rng.randint(0, N)
            p = 1 + rng.randint(0, 2)
            g[r][c] = p
            cells.append((r * N + c, p))
        b = Board.from_array(np.array(g, dtype=np.uint8))
        for idx, p in cells:
            want = False
            r, c = divmod(idx, N)
            for dr, dc in DIRS:
                for k in range(5):
                    sr, sc = r - dr * k, c - dc * k
                    win = [(sr + dr * t, sc + dc * t) for t in range(5)]
                    if all(0 <= rr < N and 0 <= cc < N and g[rr][cc] == p
                           for rr, cc in win):
                        want = True
            assert b.makes_five(p, idx) == want, f"trial={trial} idx={idx}"

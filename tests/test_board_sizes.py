# -*- coding: utf-8 -*-
"""四种棋盘尺寸（9/13/15/19）的行为契约。

尺寸是这一版最大的改动：C++ 按尺寸编译出四套二进制，Python 本地引擎用
``configure_size(n)`` 在**一局结束之后**重建全部模块级表。两条路都容易在
"只测了 19"的情况下静默跑偏，所以这里逐尺寸钉住：

* 开局天元、星位（9/13/15 是四角 + 天元，19 是传统九星）；
* 五连 / 长连 / 平局阈值都按**本局尺寸**判，而不是模块导入时的 19；
* ``Session.configure`` 换尺寸会重开一张盘，悔棋不越界；
* ``engine_local.configure_size`` 之后表的大小与几何一起换掉；
* 本地搜索落子不越界；
* ``enhanced == 0`` 时 C++ 与 Python 在四个尺寸上**逐字同着同分** ——
  这是尺寸移植正确性唯一廉价的机械护栏（C++ 缺席时跳过）。
"""

from __future__ import annotations

import numpy as np
import pytest

import board_render
import config
import engine
import engine_local
from board_geometry import BoardGeometry
from session import Session

SIZES = list(config.BOARD_SIZES)


def _board(n, stones=()):
    b = np.zeros((n, n), dtype=int)
    for r, c, p in stones:
        b[r][c] = p
    return b


def _midgame(n):
    """盘心附近的一盘中局：六子，两侧都有棋，落在四个尺寸里都不出界。"""
    m = n // 2
    return _board(n, [(m, m, 1), (m, m + 1, 2), (m + 1, m + 1, 1),
                      (m - 1, m, 2), (m + 1, m, 1), (m - 1, m - 1, 2)])


# ------------------------------------------------------------------ 开局

@pytest.mark.parametrize("n", SIZES)
def test_opening_move_is_the_center_of_this_size(n):
    engine_local.configure_size(n)
    assert engine_local.opening_move(_board(n), 1) == (n // 2, n // 2)


# ------------------------------------------------------------------ 星位

def test_star_points_use_the_corners_on_small_boards():
    """9 路：C3/C7/G3/G7 + 天元。10 路以下用第四条线会挤成一团（旧实现的病灶）。"""
    assert sorted(board_render._star_points(9)) == \
        [(2, 2), (2, 6), (4, 4), (6, 2), (6, 6)]


@pytest.mark.parametrize("n,edge", [(13, 3), (15, 3)])
def test_star_points_are_on_the_fourth_line_and_the_center(n, edge):
    far = n - 1 - edge
    assert sorted(board_render._star_points(n)) == sorted(
        [(edge, edge), (edge, far), (n // 2, n // 2), (far, edge),
         (far, far)])


def test_19_keeps_the_traditional_nine_stars():
    assert len(board_render._star_points(19)) == 9
    assert (3, 15) in board_render._star_points(19)
    assert (9, 9) in board_render._star_points(19)


# ------------------------------------------------------------------ 几何

@pytest.mark.parametrize("n", SIZES)
def test_geometry_span_is_n_minus_1_segments(n):
    g = BoardGeometry.design(n, 34.0, 57.0)
    assert g.n == n
    assert g.span == pytest.approx((n - 1) * 34.0)
    assert g.to_grid(*g.px(0, 0)) == (0, 0)
    assert g.to_grid(*g.px(n - 1, n - 1)) == (n - 1, n - 1)


# ------------------------------------------------------------------ 判胜

@pytest.mark.parametrize("n", SIZES)
def test_win_and_line_use_the_given_size(n):
    """判胜按传入的 ``size`` 走：模块当前是 19，也不会把 9 路盘读串。"""
    row = n // 2
    stones = [(row, c, 1) for c in range(5)]
    b = _board(n, stones)
    assert engine_local.check_win(b, 1, n)
    assert len(engine_local.win_line(b, 1, n)) == 5
    # 白方没有五连。
    assert not engine_local.check_win(b, 2, n)


@pytest.mark.parametrize("n", SIZES)
def test_overline_wins_in_free_rules(n):
    row = n // 2
    b = _board(n, [(row, c, 1) for c in range(6)])
    assert engine_local.check_win(b, 1, n)
    assert len(engine_local.win_line(b, 1, n)) == 6


# ------------------------------------------------------------------ Session

@pytest.mark.parametrize("n", SIZES)
def test_session_allocates_the_right_board_and_keeps_it(n):
    s = Session()
    s.configure("pvp", rules="free", board_size=n)
    assert s.board_size == n
    assert s.board.shape == (n, n)
    s.apply_stone(n // 2, n // 2, 1)
    s.apply_stone(n // 2, n // 2 + 1, 2)
    assert s.board[n // 2][n // 2] == 1
    assert s.move_count == 2
    # 悔棋只撤本盘上的子，盘面形状不变。
    s.undo()
    assert s.move_count < 2
    assert s.board.shape == (n, n)


def test_session_switching_size_starts_a_fresh_board():
    s = Session()
    s.configure("pvp", board_size=19)
    s.apply_stone(9, 9, 1)
    s.configure("pvp", board_size=9)
    assert s.board_size == 9
    assert s.board.shape == (9, 9)
    assert s.move_count == 0
    assert s.board[4][4] == 0


def test_session_rejects_an_unknown_size():
    with pytest.raises(ValueError):
        Session().configure("pvp", board_size=11)


def test_draw_threshold_is_n_squared():
    """9 路下满 81 手判和。图案按 ``(r + 2c) % 4`` 上色，任何方向都不成五。"""
    n = 9
    s = Session()
    s.configure("pvp", board_size=n)
    pattern = np.zeros((n, n), dtype=int)
    for r in range(n):
        for c in range(n):
            v = (r + 2 * c) % 4
            pattern[r][c] = 1 if v < 2 else 2
    pattern[0][0] = 0
    s.board = pattern
    s.move_count = n * n - 1
    res = s.apply_stone(0, 0, 1)
    assert res is not None and res.outcome == "draw"
    assert s.game_over and s.winner == 0


# ------------------------------------------------------------------ 引擎表

@pytest.mark.parametrize("n", SIZES)
def test_configure_size_rebuilds_the_tables(n):
    engine_local.configure_size(n)
    assert engine_local.BOARD_SIZE == n
    assert engine_local.CELLS == n * n
    assert engine_local._N_LINES == 6 * n - 2
    assert engine_local._FULL_MASK == (1 << (n * n)) - 1
    # 回到 19 由 conftest 的 autouse 夹具负责。


def test_configure_size_is_a_noop_when_unchanged():
    engine_local.configure_size(15)
    first = engine_local._LINE_VIEWS
    engine_local.configure_size(15)
    assert engine_local._LINE_VIEWS is first, "同尺寸重建了表"


@pytest.mark.parametrize("n", SIZES)
def test_local_search_stays_inside_this_board(n):
    engine_local.configure_size(n)
    engine_local.new_game()
    idx, info = engine_local._ENGINE.think(_midgame(n), 2, 1, time_limit=2.0)
    assert 0 <= idx < n * n, (n, idx)
    r, c = divmod(idx, n)
    assert _midgame(n)[r][c] == 0, "引擎落在了已有棋子的格子上"


# ------------------------------------------------------------------ 跨语言护栏

@pytest.mark.parametrize("n", SIZES)
def test_enhanced_off_cpp_and_python_agree(n):
    """``enhanced == 0`` 时两边逐字同着同分。

    C++ 第 2 档与本地第 1 档是**同一组参数**（3 s / 深度 4 / VCF 0.3 /
    qply 4）；深度先于时钟跑满，结果只由局面决定。这条在 19 路之外从来没被
    机械验证过，而尺寸移植最容易错的就是"某个表按 19 建"。
    """
    if config.find_binary("free", n) is None:
        pytest.skip("没有构建 cpp/build/gomoku_engine_%d" % n)

    board = _midgame(n)
    engine_local.configure_size(n)
    engine_local.new_game()
    pidx, pinfo = engine_local._ENGINE.think(board, 2, 1, time_limit=30.0)

    client = engine._ServerClient(variant="free", size=n)
    try:
        cidx, cinfo = client.compute(board, 2, engine._cfg_for(2), None)
    finally:
        client.shutdown()

    assert divmod(cidx, n) == divmod(pidx, n), \
        "C++ 与本地引擎在小盘上走了不同的着法"
    assert cinfo["best_val"] == pinfo["best_val"], \
        "同着但分值不同 —— 两侧的评估或搜索参数已经分叉"
    assert cinfo["actual_depth"] == pinfo["actual_depth"]

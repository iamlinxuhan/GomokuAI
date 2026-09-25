# -*- coding: utf-8 -*-
"""engine.win_line：五连定位（终局高亮的数据源）。"""

from __future__ import annotations

from engine_local import win_line


def test_horizontal_five(board_factory):
    b = board_factory(black=[(9, 4), (9, 5), (9, 6), (9, 7), (9, 8)])
    assert win_line(b, 1) == [(9, 4), (9, 5), (9, 6), (9, 7), (9, 8)]


def test_vertical_five(board_factory):
    b = board_factory(white=[(3, 3), (4, 3), (5, 3), (6, 3), (7, 3)])
    assert win_line(b, 2) == [(3, 3), (4, 3), (5, 3), (6, 3), (7, 3)]


def test_diagonal_five(board_factory):
    cells = [(i, i) for i in range(10, 15)]
    b = board_factory(black=cells)
    assert win_line(b, 1) == cells


def test_overline_returns_whole_run(board_factory):
    cells = [(9, c) for c in range(4, 10)]      # 六连
    b = board_factory(black=cells)
    assert win_line(b, 1) == cells


def test_run_broken_by_opponent_is_not_found(board_factory):
    b = board_factory(black=[(9, 4), (9, 5), (9, 6), (9, 7)],
                      white=[(9, 8)])
    assert win_line(b, 1) == []


def test_no_five_returns_empty(board_factory):
    b = board_factory(black=[(9, 4), (9, 5), (9, 6), (9, 7)])
    assert win_line(b, 1) == []
    assert win_line(b, 2) == []


def test_run_starting_after_opponent_stone(board_factory):
    # 段首的前驱是**对手子**时该段仍是极大段 —— 前驱在盘内不能一概跳过
    b = board_factory(black=[(9, 4), (9, 5), (9, 6), (9, 7), (9, 8)],
                      white=[(9, 3)])
    assert win_line(b, 1) == [(9, 4), (9, 5), (9, 6), (9, 7), (9, 8)]

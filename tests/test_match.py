# -*- coding: utf-8 -*-
"""Player 与无头 Match 的单测（M2，零 Qt、零真实搜索）。

对局用 ScriptedPlayer 完全脚本化；规则正确性仍以 Session 的单测与 UI
契约测试为准，这里只验证"驱动"这一层：轮次、前缀、非法/异常/取消的
处置、平局兜底。
"""

from __future__ import annotations

import threading

import pytest

from match import Match
from players import PlayerSpec, ScriptedPlayer, make_player


# ==================== Player 抽象 ====================

def test_player_spec_defaults_and_ai_spec():
    assert PlayerSpec().kind == "human"
    ai = PlayerSpec("ai", level=3)
    assert ai.kind == "ai" and ai.level == 3


def test_make_player_rejects_human():
    with pytest.raises(ValueError):
        make_player(PlayerSpec("human"))


def test_scripted_player_exhausts():
    p = ScriptedPlayer([(1, 2)])
    r, c, _info = p.choose_move(None, 1)
    assert (r, c) == (1, 2)
    with pytest.raises(RuntimeError):
        p.choose_move(None, 1)


# ==================== Match 驱动 ====================

def test_black_wins_in_five():
    black = ScriptedPlayer([(9, 0), (9, 1), (9, 2), (9, 3), (9, 4)])
    white = ScriptedPlayer([(8, 0), (8, 1), (8, 2), (8, 3)])
    res = Match(black, white).run()
    assert res.error == ""
    assert res.winner == 1
    assert res.moves == 9                 # 黑 5 手 + 白 4 手
    assert [m[2] for m in res.record[:4]] == [1, 2, 1, 2]


def test_white_wins_on_its_fifth():
    black = ScriptedPlayer([(0, 0), (2, 0), (4, 0), (6, 0), (8, 0)])
    white = ScriptedPlayer([(9, 0), (9, 1), (9, 2), (9, 3), (9, 4)])
    res = Match(black, white).run()
    assert res.error == ""
    assert res.winner == 2
    assert res.moves == 10


def test_illegal_move_voids_the_game():
    black = ScriptedPlayer([(9, 9), (9, 9)])   # 第二手占同一格
    white = ScriptedPlayer([(8, 8), (8, 8)])
    res = Match(black, white).run()
    assert res.error.startswith("非法走法")
    assert res.winner == 0
    assert res.moves == 2


def test_player_exception_voids_the_game():
    black = ScriptedPlayer([(9, 9)])
    white = ScriptedPlayer([])                 # 轮到白时立刻用完
    res = Match(black, white).run()
    assert "RuntimeError" in res.error
    assert res.moves == 1


def test_cancel_before_start():
    ev = threading.Event()
    ev.set()
    res = Match(ScriptedPlayer([(9, 9)]), ScriptedPlayer([(8, 8)]),
                cancel=ev).run()
    assert res.error == "已取消" and res.moves == 0


def test_start_moves_prefix_and_turn_order():
    black = ScriptedPlayer([(8, 4), (8, 5), (8, 6), (8, 7), (8, 8)])
    white = ScriptedPlayer([(0, 0), (0, 2), (0, 4), (0, 6)])
    m = Match(black, white, start_moves=[(9, 9, 1), (9, 8, 2)])
    res = m.run()
    assert res.error == "" and res.winner == 1
    # 前缀两子已落盘；前缀之后第一手是黑、第二手是白
    assert m.session.board[9][9] == 1 and m.session.board[9][8] == 2
    assert [(rec[0], rec[1], rec[2]) for rec in res.record[:2]] == [
        (8, 4, 1), (0, 0, 2)]
    assert res.moves == 11                # 前缀 2 + 脚本 9 手


def test_conflicting_start_moves_rejected():
    with pytest.raises(ValueError):
        Match(ScriptedPlayer([]), ScriptedPlayer([]),
              start_moves=[(9, 9, 1), (9, 9, 2)])


class _PatternPlayer:
    """把无五连的满盘模式按自己的颜色顺序下完（用于测平局兜底）。"""

    def __init__(self, stone):
        self.stone = stone
        self.name = f"pattern-{stone}"
        self.cells = [(r, c) for r in range(19) for c in range(19)
                      if (1 if (r + 2 * c) % 4 < 2 else 2) == stone]

    def choose_move(self, board, stone, cancel=None):
        r, c = self.cells.pop(0)
        return r, c, {"reason": "pattern", "best_val": 0.0, "depth": 0}


def test_full_board_is_a_draw():
    res = Match(_PatternPlayer(1), _PatternPlayer(2), max_moves=361).run()
    assert res.error == ""
    assert res.winner == 0
    assert res.moves == 361


def test_max_moves_cap_counts_as_draw():
    # 上限设得比满盘早：双方都还有棋可下，但驱动到点即停，不臆造胜方。
    res = Match(_PatternPlayer(1), _PatternPlayer(2), max_moves=10).run()
    assert res.error == ""
    assert res.winner == 0
    assert res.moves == 10

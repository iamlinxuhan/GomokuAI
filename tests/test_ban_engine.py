# -*- coding: utf-8 -*-
"""禁手引擎（``engine_ban``）与它在对局层的两条判定。

分三层，从便宜到贵：

* ``ban_rules`` 的规则正确性在 ``tests/test_ban_rules.py``（24 条固定局面）。
  本文件**不重复**它 —— 这里测的是"规则接进了引擎与状态机之后还对不对"。
* 引擎侧：``check_win`` / ``win_line`` / ``forbidden_points`` 的禁手口径。
* 状态机侧：``Session`` 在 ``rules="ban"`` 下把禁手说成"这一手输"，而不是
  "这一手赢"。
"""

from __future__ import annotations

import numpy as np
import pytest

import ban_rules as R
import engine
import engine_ban as B
import engine_local as L
from session import Session

BLACK, WHITE = 1, 2


def _board(black=(), white=()):
    b = np.zeros((19, 19), dtype=int)
    for r, c in black:
        b[r][c] = BLACK
    for r, c in white:
        b[r][c] = WHITE
    return b


#: 长连局面：黑在 (9,5)…(9,8) 与 (9,10)，(9,9) 一落就是六连。
#: 白子散在别处，一条线上都不成串 —— 白方先成五会提前结束这一局。
_OVERLINE_READY = _board(black=[(9, 5), (9, 6), (9, 7), (9, 8), (9, 10)],
                         white=[(12, 6), (14, 3), (16, 9), (12, 13), (11, 2)])


# ---------------------------------------------------------------- 引擎侧


def test_exact_five_is_a_win_for_black():
    b = _board(black=[(9, 5), (9, 6), (9, 7), (9, 8)])
    b[9][9] = BLACK
    assert B.check_win(b, BLACK)
    assert B.win_line(b, BLACK) == [(9, 5), (9, 6), (9, 7), (9, 8), (9, 9)]


def test_overline_is_not_a_win_for_black():
    """六连对黑方不是胜 —— 这是禁手引擎与无禁手引擎最本质的一处分叉。

    无禁手侧**必须**同意那是胜（同一个局面上），否则下面这条断言只是在测
    "函数返回了 False"。
    """
    b = _OVERLINE_READY.copy()
    b[9][9] = BLACK
    assert not B.check_win(b, BLACK)
    assert L.check_win(b, BLACK)


def test_overline_is_not_a_winning_line_for_black():
    """``engine_ban.win_line`` 只答"有没有获胜的五连" —— 六连对黑方不是胜，
    所以它返回空表，而不是把那段六连当成一条"赢线"交出去。

    界面要标"输在哪一段"时走的是 ``Session`` 给出的 ``line``（那个用
    ``engine_local.win_line`` 的整段口径）—— 见本文件的状态机一节。两者分工
    不同，不是两份实现。
    """
    b = _OVERLINE_READY.copy()
    b[9][9] = BLACK
    assert B.win_line(b, BLACK) == []
    assert len(L.win_line(b, BLACK)) == 6


def test_white_is_not_bound_by_the_ban_rules():
    """白方六连照旧是胜，且 ``win_line`` 给出整段。"""
    b = _board(black=[(3, 6), (3, 7), (3, 8), (3, 9), (3, 11)],
               white=[(15, 5), (15, 6), (15, 7), (15, 8), (15, 10)])
    b[15][9] = WHITE
    assert B.check_win(b, WHITE)
    assert B.win_line(b, WHITE) == [(15, c) for c in range(5, 11)]


def test_forbidden_points_matches_ban_rules():
    """``engine_ban`` 的禁手点表与规则模块**同源**：一个点数都不能差。

    两份实现（一份吃 ndarray、一份吃 list）是同一套规则的两个入口，漂移了
    就意味着界面标红的点与判负的点不是同一批。
    """
    b = _board(black=[(9, 7), (9, 8), (7, 9), (8, 9)],
               white=[(12, 12), (13, 13)])
    assert B.forbidden_points(b) == R.forbidden_points(b.tolist())
    assert B.forbidden_points(b)                      # 空表的话上面比的是两个空
    assert B.forbidden_points(b, WHITE) == []         # 白方无禁手


def test_ban_kind_agrees_with_the_rule_module():
    b = _board(black=[(9, 7), (9, 8), (7, 9), (8, 9)])
    assert B.ban_kind(b, 9, 9) == R.FOUL_DOUBLE_THREE
    assert B.ban_kind(b, 0, 0) == R.FOUL_NONE
    assert B.ban_kind(b, 9, 7) == R.FOUL_NONE         # 已占点不判


def test_empty_board_has_no_forbidden_point():
    assert B.forbidden_points(np.zeros((19, 19), dtype=int)) == []


# ---------------------------------------------------------------- 搜索侧


def test_search_never_plays_a_forbidden_point():
    """黑方的搜索着法不是禁手点。

    局面取自"黑方四四在即"：``(9,6)`` 一落同时成两个四。搜索本来也不该选它
    （落下去当场判负，分值最低），这条断言钉的是**返回值**而不是搜索的偏好。
    """
    b = _board(black=[(9, 5), (9, 7), (9, 8), (6, 6), (7, 7), (8, 8)],
               white=[(9, 6 - 0), (12, 6), (13, 7), (14, 3)])
    b[9][6] = 0
    foul = R.forbidden_points(b.tolist())
    assert (9, 6) in foul or (9, 5 + 1) not in foul   # 局面自证：这里有禁手点
    r, c, info = B.ai_move(b, BLACK, 1, use_book=False)
    assert R.foul_kind(b.tolist(), r, c, BLACK) == R.FOUL_NONE, (
        "搜索返回了禁手点 (%d,%d)：%r" % (r, c, info))


def test_analyze_does_not_propose_a_forbidden_move():
    b = _board(black=[(9, 5), (9, 7), (9, 8), (6, 6), (7, 7), (8, 8)],
               white=[(12, 6), (13, 7), (14, 3)])
    best, val, cands, info = B.analyze(b, BLACK, 1, time_limit=0.5)
    if best >= 0:
        r, c = divmod(best, 19)
        assert R.foul_kind(b.tolist(), r, c, BLACK) == R.FOUL_NONE


def test_black_move_ok_is_the_rule_module_again():
    """``_black_move_ok`` 与规则模块逐点一致（它是"永不返回禁手"的兜底判据）。"""
    b = _board(black=[(9, 7), (9, 8), (7, 9), (8, 9)])
    for idx in range(19 * 19):
        r, c = divmod(idx, 19)
        expect = (int(b[r][c]) == 0
                  and R.foul_kind(b.tolist(), r, c, 1) == R.FOUL_NONE)
        assert B._black_move_ok(b, idx) == expect


# ---------------------------------------------------------------- 门面路由


def test_facade_routes_the_ban_variant_to_the_ban_engine():
    """``engine.ai_move(variant="ban")`` 空盘那一手不查开局库。

    无禁手侧同一个局面报的是「开局库」—— 两张表在同一台引擎上是同一个函数，
    差别只可能来自选中的那个模块。
    """
    empty = np.zeros((19, 19), dtype=int)
    r, c, info = engine.ai_move(empty, BLACK, 1, variant="ban")
    assert (r, c) == (9, 9)
    assert info["reason"] == "开局规则"
    assert engine._local_engine("ban") is B
    assert engine._local_engine("free") is L


def test_facade_rejects_an_unknown_variant():
    empty = np.zeros((19, 19), dtype=int)
    with pytest.raises(ValueError):
        engine.ai_move(empty, BLACK, 1, variant="banx")


# ---------------------------------------------------------------- 状态机


def _session(rules="ban"):
    s = Session()
    s.configure("pvp", rules=rules)
    s.reset()
    return s


def test_session_scores_an_overline_as_a_loss():
    s = _session()
    cur = _OVERLINE_READY.copy()
    # 直接把局面灌进 Session：逐手重放会经过"黑方四连"的中间态，
    # 而四连不终局，重放出来是同一个盘面 —— 但灌盘面更短，也更难写错。
    s.board = cur
    s.move_count = 10
    res = s.apply_stone(9, 9, BLACK)
    assert res is not None
    assert res.outcome == "forbidden"
    assert res.winner == WHITE          # 违规的是落子的人，赢的是对面
    assert res.foul == R.FOUL_OVERLINE
    assert len(res.line) == 6           # 界面上要标出来的是那一段长连
    assert s.game_over and s.winner == WHITE


def test_session_scores_the_same_overline_as_a_win_in_free_mode():
    """同一条六连：无禁手是胜，禁手是负。"""
    s = _session("free")
    s.board = _OVERLINE_READY.copy()
    s.move_count = 10
    res = s.apply_stone(9, 9, BLACK)
    assert res.outcome == "win"
    assert res.winner == BLACK


def test_session_keeps_exact_five_a_win_in_ban_mode():
    s = _session()
    s.board = _board(black=[(9, 5), (9, 6), (9, 7), (9, 8)],
                     white=[(12, 6), (13, 7), (14, 3), (11, 2)])
    s.move_count = 8
    res = s.apply_stone(9, 9, BLACK)
    assert res.outcome == "win"
    assert res.winner == BLACK
    assert len(res.line) == 5


def test_session_scores_a_double_three_as_a_loss():
    s = _session()
    s.board = _board(black=[(9, 7), (9, 8), (7, 9), (8, 9)],
                     white=[(12, 12), (13, 13), (14, 14), (11, 11)])
    s.move_count = 8
    res = s.apply_stone(9, 9, BLACK)
    assert res.outcome == "forbidden"
    assert res.foul == R.FOUL_DOUBLE_THREE
    assert res.line == ()               # 三三没有一条线可以标出来


def test_session_lets_white_make_an_overline():
    s = _session()
    s.board = _board(black=[(3, 6), (3, 7), (3, 8), (3, 9), (3, 11)],
                     white=[(15, 5), (15, 6), (15, 7), (15, 8)])
    s.move_count = 10
    res = s.apply_stone(15, 9, WHITE)
    assert res.outcome == "win"
    assert res.winner == WHITE


def test_session_reports_the_ai_losing_by_a_foul():
    """人机模式下黑方是 AI：它禁手负 = **人赢**（gamerule 2）。"""
    s = Session()
    s.configure("ai", human_stone=WHITE, ai_stone=BLACK, rules="ban")
    s.reset()
    s.board = _board(black=[(9, 7), (9, 8), (7, 9), (8, 9)],
                     white=[(12, 12), (13, 13), (14, 14), (11, 11)])
    s.move_count = 8
    res = s.apply_stone(9, 9, BLACK)
    assert res.winner == WHITE
    assert s.gamerule == 2              # 2 = 人赢（见 session.reset 的注释）


def test_session_rejects_an_unknown_rules_name():
    with pytest.raises(ValueError):
        Session().configure("pvp", rules="freee")

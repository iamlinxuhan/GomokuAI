# -*- coding: utf-8 -*-
"""Session 的独立规则单测（零 Qt、毫秒级）。

行为契约测试（``tests/test_game_flow.py``）走真实 UI，是"用户可见行为"的
最终判据；这里直接查 M1 抽取出来的状态机，是更细更快的一层规则网。两边
必须一致 —— 任何一边红了都说明抽取改坏了语义。
"""

from __future__ import annotations

from session import Session, UNDO_LIMIT

B = 19


def _ai_session(human_stone=1):
    s = Session()
    s.configure("ai", human_stone=human_stone, ai_stone=3 - human_stone)
    s.reset()
    return s


def _pvp_session():
    s = Session()
    s.configure("pvp")
    s.reset()
    return s


# ==================== 初始状态 ====================

def test_reset_is_empty():
    s = _ai_session()
    assert s.board.shape == (B, B)
    assert not s.board.any()
    assert s.move_count == 0 and s.move_history == []
    assert s.game_over is False and s.gamerule == 3 and s.winner == 0
    assert s.output == UNDO_LIMIT
    assert s.human_moves == [] and s.last_move is None
    assert s.opening_done is False


# ==================== 落子 ====================

def test_human_move_records_before_snapshot():
    s = _ai_session(human_stone=1)
    res = s.apply_human_move(9, 9)
    assert res.stone == 1 and res.outcome == "continue"
    assert s.board[9][9] == 1
    assert s.move_count == 1 and s.last_move == (9, 9, 1)
    rec = s.human_moves[0]
    assert rec["seq"] == 1 and (rec["r"], rec["c"]) == (9, 9)
    assert not rec["before"].any()          # 落子前

    res2 = s.apply_ai_move(8, 8)
    assert res2.stone == 2 and s.move_count == 2
    assert len(s.human_moves) == 1          # AI 的着法不进复盘记录


def test_pvp_alternates_and_records_nothing():
    s = _pvp_session()
    assert s.apply_human_move(9, 9).stone == 1
    assert s.apply_human_move(8, 8).stone == 2
    assert s.apply_human_move(9, 8).stone == 1
    assert s.human_moves == []


def test_apply_stone_generic_entry():
    """通用入口：坐席/回合由调用方定；占用格与空石头被拒。"""
    s = _pvp_session()
    assert s.apply_stone(9, 9, 1).stone == 1
    assert s.apply_stone(9, 9, 2) is None       # 已占
    assert s.apply_stone(8, 8, 0) is None       # 无石头
    assert s.apply_stone(8, 8, 2).stone == 2


def test_occupied_cell_is_rejected():
    s = _ai_session(1)
    s.apply_human_move(9, 9)
    assert s.apply_human_move(9, 9) is None
    assert s.move_count == 1


# ==================== 判胜 / 判和 ====================

def test_ai_mode_gamerules():
    # 玩家执黑五连 → gamerule=2（玩家赢）
    s = _ai_session(human_stone=1)
    res = None
    for c in range(5):
        res = s.apply_human_move(9, c)
    assert res.outcome == "win" and res.winner == 1
    assert s.game_over and s.gamerule == 2
    assert len(res.line) >= 5

    # 玩家执白、AI 执黑：AI 五连 → gamerule=1
    s2 = _ai_session(human_stone=2)
    for c in range(4):
        s2.apply_human_move(12, c)
        res = s2.apply_ai_move(8, c)
    res = s2.apply_ai_move(8, 4)
    assert res.outcome == "win" and res.winner == 1
    assert s2.game_over and s2.gamerule == 1


def test_pvp_win_gamerule_is_two_and_winner_identifies_side():
    s = _pvp_session()
    for c in range(4):
        s.apply_human_move(9, c)
        s.apply_human_move(8, c)
    res = s.apply_human_move(9, 4)
    assert res.outcome == "win" and res.winner == 1
    assert s.gamerule == 2


def test_draw_at_full_board():
    s = _ai_session(human_stone=1)
    for r in range(B):
        for c in range(B):
            s.board[r][c] = 1 if (r + 2 * c) % 4 < 2 else 2
    s.board[18][18] = 0
    s.move_count = B * B - 1
    res = s.apply_human_move(18, 18)
    assert res.outcome == "draw"
    assert s.game_over and s.gamerule == 0 and s.winner == 0
    assert s.move_count == B * B


# ==================== 悔棋 ====================

def test_undo_ai_removes_two_and_trims():
    s = _ai_session(1)
    s.apply_human_move(9, 9)
    s.apply_ai_move(8, 8)
    s.apply_human_move(9, 8)
    s.apply_ai_move(7, 7)

    u = s.undo()
    assert u.removed == 2 and not u.replay_opening
    assert s.move_count == 2
    assert s.board[9][9] == 1 and s.board[8][8] == 2
    assert s.board[9][8] == 0 and s.board[7][7] == 0
    assert [m["seq"] for m in s.human_moves] == [1]
    assert s.output == UNDO_LIMIT - 1
    assert s.last_move is None


def test_undo_pvp_removes_one():
    s = _pvp_session()
    s.apply_human_move(9, 9)
    s.apply_human_move(8, 8)
    u = s.undo()
    assert u.removed == 1
    assert s.move_count == 1
    assert s.board[9][9] == 1 and s.board[8][8] == 0


def test_undo_budget_and_floor():
    s = _ai_session(1)
    for i in range(5):
        s.apply_human_move(12, 2 * i)
        s.apply_ai_move(8, 2 * i)
    for _ in range(3):
        assert s.undo() is not None
    assert s.output == 0 and s.move_count == 4
    assert s.undo() is None                     # 上限用尽
    assert s.move_count == 4


def test_undo_before_any_move_is_noop():
    s = _ai_session(1)
    assert s.undo() is None
    assert s.output == UNDO_LIMIT


def test_undo_after_game_over_is_noop():
    s = _ai_session(1)
    for c in range(5):
        s.apply_human_move(9, c)
    assert s.game_over
    assert s.undo() is None
    assert s.move_count == 5


def test_ai_first_single_move_undo_requests_replay():
    s = _ai_session(human_stone=2)              # AI 执黑
    s.apply_opening_move(9, 9)
    assert s.opening_done
    u = s.undo()
    assert u.replay_opening and u.removed == 1
    assert s.opening_done is False
    assert s.move_count == 0 and not s.board.any()


def test_ai_first_pair_undo_keeps_quirk():
    """C11（现状，疑似缺陷）：撤两手后不重下天元，下一手由玩家先走。"""
    s = _ai_session(human_stone=2)
    s.apply_opening_move(9, 9)
    s.apply_human_move(8, 8)
    u = s.undo()
    assert not u.replay_opening and u.removed == 2
    assert s.move_count == 0 and not s.board.any()
    assert s.opening_done is True               # 现状：不重下
    res = s.apply_human_move(8, 8)              # 现状：玩家（白）先走
    assert res.stone == 2

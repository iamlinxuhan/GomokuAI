# -*- coding: utf-8 -*-
"""对局流程的行为契约（characterization tests）。

在 Session 抽取（M1）之前，把 ``main.py`` 里现存的、对用户可见的对局行为
逐条钉住：落子合法性、human_moves 快照、轮次与终局语义、悔棋规则、代数
作废、判和。契约清单与逐条对照见 ``tools/PLAN_SESSION.md`` 第 4 节。

这些用例描述的是**现状**，不是理想设计 —— 其中 C11（AI 先手局撤两手后
不重下天元）在方案里明确标了"疑似缺陷"。M1 重构必须逐条保持；修不修由
后续里程碑单独决定，并同步更新这里。

构建方式沿用 ``tests/test_anim_smoke.py``：离屏真实控件 + **脚本化 AI 回合**
（直接调 ``_on_ai_finished``，不启动搜索线程），对局节奏完全由测试控制。
"""

from __future__ import annotations

import io
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PyQt5.QtWidgets import QApplication

import main as M

BOARD_SIZE = 19

_REAL_LOGGER = M.GameLogger


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    return app


class _DummyLogger:
    """替身：不往项目根目录落 game_log_*.txt；保留坐标格式化。

    ``_ai_first_move`` 会调用 ``GameLogger.coord_to_sgf``（名字在 main 的
    模块全局里解析），所以替身必须带这个方法。
    """

    coord_to_sgf = staticmethod(_REAL_LOGGER.coord_to_sgf)

    def __init__(self):
        self.filepath = "<memory>"
        self.f = io.StringIO()

    def close(self):
        self.f.close()

    def log_human(self, *a, **k):
        pass

    def log_ai(self, *a, **k):
        pass

    def log_board_state(self, *a, **k):
        pass

    def log_result(self, *a, **k):
        pass


class _FakeClick:
    """只实现 ``_on_board_click`` 用到的 x()/y()。"""

    def __init__(self, x, y):
        self._x, self._y = x, y

    def x(self):
        return self._x

    def y(self):
        return self._y


def _click(board_widget, r, c):
    pt = board_widget.cell_center(r, c)
    return _FakeClick(pt.x(), pt.y())


def _build(monkeypatch, qapp, *, mode=0, color=0, level=1, ai=None):
    """构造一局。

    mode=0 挑战 AI、mode=1 本地双人；color: 0=玩家执黑、1=AI 执黑。
    ai=None → AI 回合 no-op；ai=[(r,c), ...] → 轮到 AI 时按序同步落子
    （通过真实的 ``_on_ai_finished``，不启线程）。
    """
    monkeypatch.setattr(M, "GameLogger", _DummyLogger)
    win = M.GomokuGame()
    win.show()
    if mode == 1:
        win._on_mode_selected(1)
    else:
        win._on_mode_selected(0)
        win._on_color_selected(color)
        win._on_difficulty_selected(level)

    if ai is None:
        monkeypatch.setattr(win, "_ai_turn", lambda ai_stone: None)
    else:
        script = list(ai)

        def fake_ai_turn(ai_stone):
            win.ai_thinking = True
            win.game_panel.show_thinking(True, ai_stone)
            win.game_panel.undo_btn.setEnabled(False)
            assert script, "测试脚本里的 AI 着法用完了"
            r, c = script.pop(0)
            win._on_ai_finished(
                r, c, {"reason": "测试脚本", "best_val": 0.0, "depth": 1})

        monkeypatch.setattr(win, "_ai_turn", fake_ai_turn)

    qapp.processEvents()
    return win


# ==================== 落子与快照 ====================

def test_human_then_ai_move_updates_board_and_snapshot(monkeypatch, qapp):
    """C2/C3/C4：人机局一手换一手；human_moves 记的是落子**前**的局面。"""
    win = _build(monkeypatch, qapp, mode=0, color=0, ai=[(8, 8)])
    try:
        bw = win.board_widget
        win._on_board_click(_click(bw, 9, 9))
        qapp.processEvents()

        assert win.board[9][9] == 1
        assert win.board[8][8] == 2
        assert win.move_count == 2
        assert win.last_move == (8, 8, 2)          # 最后一手是脚本 AI
        assert len(win.human_moves) == 1
        rec = win.human_moves[0]
        assert (rec["seq"], rec["r"], rec["c"]) == (1, 9, 9)
        assert rec["before"][9][9] == 0            # 落子前，不是落子后
        assert not rec["before"].any()
    finally:
        win._on_quit()
        qapp.processEvents()


def test_human_moves_not_recorded_in_pvp(monkeypatch, qapp):
    """C3：本地双人没有"复盘对象"，human_moves 必须为空。"""
    win = _build(monkeypatch, qapp, mode=1)
    try:
        win._on_board_click(_click(win.board_widget, 9, 9))
        assert win.human_moves == []
    finally:
        win._on_quit()
        qapp.processEvents()


# ==================== 轮次（本地双人） ====================

def test_pvp_alternates_by_move_parity_and_never_calls_ai(monkeypatch, qapp):
    """C2：本地对战黑先、按手数奇偶换色；AI 回合那一支整个不执行。"""
    win = _build(monkeypatch, qapp, mode=1)
    calls = []
    monkeypatch.setattr(win, "_ai_turn", lambda stone: calls.append(stone))
    try:
        bw = win.board_widget
        win._on_board_click(_click(bw, 9, 9))
        win._on_board_click(_click(bw, 8, 8))
        win._on_board_click(_click(bw, 9, 8))

        assert win.board[9][9] == 1
        assert win.board[8][8] == 2
        assert win.board[9][8] == 1
        assert win.move_count == 3
        assert calls == []
    finally:
        win._on_quit()
        qapp.processEvents()


# ==================== 悔棋 ====================

def test_undo_in_human_vs_ai_removes_two_and_trims_human_moves(monkeypatch, qapp):
    """C9：人机悔棋一次撤"玩家 + AI"两手，并剪掉已收回的复盘记录。"""
    win = _build(monkeypatch, qapp, mode=0, color=0, ai=[(8, 8), (7, 7)])
    try:
        bw = win.board_widget
        win._on_board_click(_click(bw, 9, 9))
        win._on_board_click(_click(bw, 9, 8))
        assert win.move_count == 4
        assert len(win.human_moves) == 2

        win._on_undo()
        assert win.move_count == 2
        assert win.board[9][8] == 0 and win.board[7][7] == 0
        assert win.board[9][9] == 1 and win.board[8][8] == 2
        assert [m["seq"] for m in win.human_moves] == [1]
        assert win.output == 2
    finally:
        win._on_quit()
        qapp.processEvents()


def test_undo_limit_is_three(monkeypatch, qapp):
    """C9：悔棋上限 3 次，用尽后点击无动作。"""
    win = _build(monkeypatch, qapp, mode=0, color=0,
                 ai=[(8, 4), (8, 6), (8, 8), (8, 10), (8, 12)])
    try:
        bw = win.board_widget
        for c in (0, 2, 4, 6, 8):
            win._on_board_click(_click(bw, 12, c))
        assert win.move_count == 10

        for _ in range(3):
            win._on_undo()
        assert win.move_count == 4
        assert win.output == 0
        assert [m["seq"] for m in win.human_moves] == [1, 3]

        win._on_undo()                     # 第 4 次：上限已到，什么也不做
        assert win.move_count == 4
    finally:
        win._on_quit()
        qapp.processEvents()


def test_undo_in_pvp_removes_one(monkeypatch, qapp):
    """C9：本地对战一次只撤一手（"撤回玩家 + AI 回应"在这里没有对应物）。"""
    win = _build(monkeypatch, qapp, mode=1)
    try:
        bw = win.board_widget
        win._on_board_click(_click(bw, 9, 9))
        win._on_board_click(_click(bw, 8, 8))
        win._on_undo()

        assert win.move_count == 1
        assert win.board[9][9] == 1
        assert win.board[8][8] == 0
        assert win.output == 2
    finally:
        win._on_quit()
        qapp.processEvents()


# ==================== AI 先手 ====================

def test_ai_first_move_is_tengen_and_single_undo_replays_it(monkeypatch, qapp):
    """C10：AI 先手走天元；仅天元这一手时悔棋会立刻重下。"""
    win = _build(monkeypatch, qapp, mode=0, color=1)
    try:
        assert win.move_count == 1
        assert win.board[9][9] == 1
        assert win.ai_first_move_done

        win._on_undo()
        qapp.processEvents()
        assert win.move_count == 1           # 撤掉又立刻补回
        assert win.board[9][9] == 1
        assert win.ai_first_move_done
    finally:
        win._on_quit()
        qapp.processEvents()


def test_ai_first_move_pair_undo_keeps_current_quirk(monkeypatch, qapp):
    """C11（现状，疑似缺陷）：天元 + 玩家一手后悔棋，撤两手且**不**重下天元。

    方案文档把它标为"疑似缺陷但 M1 逐字保留"：这里钉的是现状，不是设计。
    若后续里程碑决定修它，这条测试应随之改写。
    """
    win = _build(monkeypatch, qapp, mode=0, color=1)
    try:
        bw = win.board_widget
        win._on_board_click(_click(bw, 8, 8))    # 玩家（白）
        assert win.move_count == 2

        win._on_undo()
        assert win.move_count == 0
        assert not win.board.any()
        assert win.ai_first_move_done, "天元不会重下（现状）"

        win._on_board_click(_click(bw, 8, 8))
        assert win.board[8][8] == 2, "下一手由玩家（白）先走（现状）"
        assert win.board[9][9] == 0
    finally:
        win._on_quit()
        qapp.processEvents()


# ==================== 代数作废 ====================

def test_stale_ai_generation_is_discarded(monkeypatch, qapp):
    """C8：重开/悔棋后迟到的 AI 结果必须被代数校验丢弃。"""
    win = _build(monkeypatch, qapp, mode=0, color=0)
    try:
        win._ai_generation = 7
        win._on_ai_finished(3, 3, {"reason": "旧结果"}, generation=6)
        assert win.board[3][3] == 0
        assert win.move_count == 0

        win._on_ai_finished(3, 3, {"reason": "当前结果"}, generation=7)
        assert win.board[3][3] == 2
        assert win.move_count == 1
    finally:
        win._on_quit()
        qapp.processEvents()


# ==================== 终局 ====================

def test_player_win_sets_gamerule_and_blocks_later_clicks(monkeypatch, qapp):
    """C1/C5：玩家五连 → gamerule=2、winner=玩家子色；终局后点击被丢弃。"""
    win = _build(monkeypatch, qapp, mode=0, color=0)
    try:
        bw = win.board_widget
        for c in range(4, 9):
            win._on_board_click(_click(bw, 9, c))

        assert win.game_over
        assert win.gamerule == 2
        assert win.winner == 1
        assert win.board_widget.win_cells      # 连五高亮已给出

        blocked = win.move_count
        win._on_board_click(_click(bw, 0, 0))
        assert win.move_count == blocked
        assert win.board[0][0] == 0
    finally:
        win._on_quit()
        qapp.processEvents()


def test_ai_win_sets_gamerule_and_winner_is_ai_stone(monkeypatch, qapp):
    """C5：AI 五连 → gamerule=1、winner=AI 子色。"""
    win = _build(monkeypatch, qapp, mode=0, color=0,
                 ai=[(8, 4), (8, 5), (8, 6), (8, 7), (8, 8)])
    try:
        bw = win.board_widget
        for c in (0, 2, 4, 6, 8):
            win._on_board_click(_click(bw, 12, c))

        assert win.game_over
        assert win.gamerule == 1
        assert win.winner == 2
        assert win.board[8][8] == 2
        assert win.board_widget.win_cells
    finally:
        win._on_quit()
        qapp.processEvents()


def test_draw_when_board_is_full(monkeypatch, qapp):
    """C6：棋盘放满且无五连 → gamerule=0、winner=0。

    棋盘直接用 **无五连** 的满盘模式预置 360 子：按 ``(r + 2c) % 4`` 取前两档
    同色，四个方向的同色连长度最多 2（普通棋盘格不行 —— 主对角线奇偶不变，
    会整条同色）。再补最后一手即可走到判和分支，不必真点 361 次。
    """
    win = _build(monkeypatch, qapp, mode=0, color=0)
    try:
        board = np.zeros((BOARD_SIZE, BOARD_SIZE), dtype=int)
        for r in range(BOARD_SIZE):
            for c in range(BOARD_SIZE):
                board[r][c] = 1 if (r + 2 * c) % 4 < 2 else 2
        board[18][18] = 0
        win.board = board
        win.move_count = BOARD_SIZE * BOARD_SIZE - 1

        win._on_board_click(_click(win.board_widget, 18, 18))

        assert win.move_count == BOARD_SIZE * BOARD_SIZE
        assert win.game_over
        assert win.gamerule == 0
        assert win.winner == 0
    finally:
        win._on_quit()
        qapp.processEvents()


# ==================== 重开 ====================

def test_restart_returns_to_mode_selection(monkeypatch, qapp):
    """C13：重开回到模式选择页（而不是跳过"跟谁下"直接进颜色页）。"""
    win = _build(monkeypatch, qapp, mode=1)
    try:
        win._on_board_click(_click(win.board_widget, 9, 9))
        win._on_restart()
        qapp.processEvents()
        assert win.central.currentWidget() is win.selection_mode
    finally:
        win._on_quit()
        qapp.processEvents()

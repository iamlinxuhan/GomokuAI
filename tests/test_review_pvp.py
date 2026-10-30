# -*- coding: utf-8 -*-
"""复盘的对象与标注：本地对战算双方，局域网只算我方。

用户 2026-10-30 的口径（GitHub issue：把和别人的棋局拿来复盘）：

* **本地对战**：黑白双方每一手都重算，并标明是谁下的（【黑方】/【白方】）；
* **局域网联机**：只算我方的最优落子点，对手的棋不进我的复盘。

这两条落到代码上是三件事：``_on_room_move`` 记谁的手（``stone`` 键）、
``ReviewWorker`` 按每条记录自己的子色去算、``_record_line`` 在行首标出来。
本文件把这三件事分别钉住，外加两道入口判断 —— 终局遮罩给不给复盘按钮、
强度页列哪几档（双人局没有"本局难度"，必须列全表且带「自定义」）。
"""

from __future__ import annotations

import io
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PyQt5.QtWidgets import QApplication

import i18n
import main as M
from client import LocalRoom, RoomClient
from room import SeatSpec

BOARD_SIZE = 19


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class _DummyLogger:
    """替身：不往项目根目录落 game_log_*.txt。"""

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
    def __init__(self, x, y):
        self._x, self._y = x, y

    def x(self):
        return self._x

    def y(self):
        return self._y


def _pump_until(qapp, pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while True:
        qapp.processEvents()
        if pred():
            return True
        if time.monotonic() > deadline:
            return False
        time.sleep(0.005)


def _click(qapp, win, r, c):
    assert _pump_until(qapp, lambda: not win.ai_thinking
                       and not win._move_in_flight), "等回合时空等超时"
    pt = win.board_widget.cell_center(r, c)
    win._on_board_click(_FakeClick(pt.x(), pt.y()))


def _teardown(win, qapp):
    win._on_quit()
    qapp.processEvents()
    win.deleteLater()
    qapp.processEvents()


def _rec(**kw):
    """一条"走对了"的复盘记录；用例只改需要的那几个键。"""
    base = {"seq": 3, "played": (9, 9), "best": (9, 9), "best_val": 0.0,
            "before": np.zeros((BOARD_SIZE, BOARD_SIZE), dtype=int),
            "mate": False, "played_val": 0.0, "delta": 0,
            "offboard": False, "empty": False}
    base.update(kw)
    return base


# ==================== 行首标注 ====================

def test_pvp_rows_name_the_side():
    """本地对战的行首写【黑方】/【白方】—— 列表里黑白交替，不标就分不清是谁。"""
    assert M._record_line(_rec(stone=1)).startswith("【黑方】第 3 手")
    assert M._record_line(_rec(stone=2)).startswith("【白方】第 3 手")


def test_rows_without_a_side_keep_the_old_format():
    """人机与局域网的行不带前缀：整张列表从头到尾是同一个人。"""
    assert M._record_line(_rec()).startswith("第 3 手")
    # 而且除前缀外逐字相同 —— 老格式不能因为这次改动漂移
    assert (M._record_line(_rec(stone=1))[len("【黑方】"):]
            == M._record_line(_rec()))


def test_side_label_is_translated(qapp):
    """前缀走 i18n：英文界面读作「【Black】Move 3」。"""
    keep = i18n.language()
    i18n.set_language("en")
    try:
        line = M._record_line(_rec(stone=2))
        assert line.startswith("【White】Move 3"), line
    finally:
        i18n.set_language(keep)


# ==================== Worker：按每条记录自己的子色算 ====================

def test_review_worker_uses_each_records_own_stone(monkeypatch):
    """本地对战的记录自带 ``stone``：黑白两手要分别以黑、白视角重算。"""
    seen = []

    def fake_analyze(board, me, level, cancel=None, cfg=None, variant="free"):
        seen.append(int(me))
        idx = 9 * BOARD_SIZE + 9
        return idx, 10.0, [(idx, 10.0)], {}

    monkeypatch.setattr(M.engine, "analyze", fake_analyze)
    board = np.zeros((BOARD_SIZE, BOARD_SIZE), dtype=int)
    moves = [
        {"seq": 1, "r": 9, "c": 9, "before": board.copy(), "stone": 1},
        {"seq": 2, "r": 9, "c": 10, "before": board.copy(), "stone": 2},
    ]
    w = M.ReviewWorker(moves, human=1, level=1)
    recs = [w._one(m) for m in moves]

    assert seen == [1, 2], "worker 没有按记录自己的子色去算"
    assert [r["stone"] for r in recs] == [1, 2]


def test_review_worker_defaults_to_the_players_colour(monkeypatch):
    """人机局的记录没有 ``stone`` 键：一律按 ``human`` 算（旧行为不变）。"""
    seen = []

    def fake_analyze(board, me, level, cancel=None, cfg=None, variant="free"):
        seen.append(int(me))
        idx = 9 * BOARD_SIZE + 9
        return idx, 10.0, [(idx, 10.0)], {}

    monkeypatch.setattr(M.engine, "analyze", fake_analyze)
    board = np.zeros((BOARD_SIZE, BOARD_SIZE), dtype=int)
    moves = [{"seq": 1, "r": 9, "c": 9, "before": board.copy()}]
    w = M.ReviewWorker(moves, human=2, level=1)
    rec = w._one(moves[0])

    assert seen == [2]
    assert "stone" not in rec, "人机的行不该凭空多出一个前缀"


def test_review_worker_uses_the_games_rules(monkeypatch):
    """禁手局的复盘要按禁手规则算。

    拿无禁手的刻度去评一盘禁手棋，被禁的着法会被算成好棋 —— ``engine.analyze``
    的文档串点明了这条。复盘发生在终局之后，那局用的规则只剩 ``game_rules``
    这一个来源，所以它必须一路传到 ``analyze`` 的 ``variant``。
    """
    seen = []

    def fake_analyze(board, me, level, cancel=None, cfg=None, variant="free"):
        seen.append(variant)
        idx = 9 * BOARD_SIZE + 9
        return idx, 10.0, [(idx, 10.0)], {}

    monkeypatch.setattr(M.engine, "analyze", fake_analyze)
    board = np.zeros((BOARD_SIZE, BOARD_SIZE), dtype=int)
    moves = [{"seq": 1, "r": 9, "c": 9, "before": board.copy()}]
    w = M.ReviewWorker(moves, human=1, level=1, variant="ban")
    w._one(moves[0])
    assert seen == ["ban"], "复盘没有按本局的规则去算"


# ==================== 本地对战：终局给入口，强度页列全表 ====================

def test_local_win_offers_review_with_the_full_level_table(monkeypatch, qapp):
    """本地对战打完（黑方五连）→ 遮罩有复盘入口 → 强度页是双人版的全表。

    顺带钉住"``gamekunnan`` 停在上一次人机的档位不影响这里"：故意把它设成
    宗师（5）之后，强度页仍要从入门开始列 —— 双人局从来没有"本局难度"。
    """
    monkeypatch.setattr(M, "GameLogger", _DummyLogger)
    win = M.GomokuGame()
    win.show()
    try:
        win._on_mode_selected(1)
        win._on_rules_selected(0)
        assert _pump_until(qapp, lambda: win.game_panel is not None
                           and win._room is not None), "本地对战没有起来"

        # 黑先五连：黑下第 10 行，白在第 1 行陪着走。
        for c in range(5):
            _click(qapp, win, 10, c)
            assert _pump_until(qapp, lambda c=c: win.board[10][c] == 1), \
                "黑子未落"
            if c < 4:
                _click(qapp, win, 0, c)
                assert _pump_until(qapp, lambda c=c: win.board[0][c] == 2), \
                    "白子未落"

        assert _pump_until(qapp, lambda: win.game_over_overlay is not None,
                           timeout=8.0), "终局遮罩没弹出来"
        assert win.game_over_overlay.can_review is True, \
            "本地对战打完应当有复盘入口"

        # 九手全在素材里，黑白交替、各自带子色。
        assert [(m["seq"], m["stone"]) for m in win.human_moves] == \
            [(i, 1 if i % 2 == 1 else 2) for i in range(1, 10)]

        win.gamekunnan = 5               # 上一局人机留下的"宗师"
        win._on_review_clicked()
        page = win.review_strength
        assert page.mode == "review_local"
        assert page.min_level == 1, "双人局的复盘强度不能按上一局人机的档位过滤"
        assert len(page._cards) == len(M.engine.DIFFICULTY) + 1, \
            "双人局的强度页要照搬难度页：预设档全额 + 自定义"
    finally:
        _teardown(win, qapp)


# ==================== 局域网：只记我方 ====================

def test_lan_records_only_my_own_moves(monkeypatch, qapp):
    """加入方执白：房主（黑）的每一手都不进我的复盘素材，只记我自己的白子。"""
    monkeypatch.setattr(M, "GameLogger", _DummyLogger)
    local = LocalRoom(SeatSpec("remote"), SeatSpec("remote"),
                      name=M.LAN_ROOM_NAME)
    host = RoomClient()
    host.connect(local.host, local.port, local.name, "black")
    win = M.GomokuGame()
    win.show()
    try:
        win._on_mode_selected(2)
        qapp.processEvents()
        win._on_rules_selected(0)
        qapp.processEvents()
        win._on_lan_join_requested("127.0.0.1", str(local.port))
        assert _pump_until(qapp, lambda: win.session.human_stone == 2), \
            "自动席位没有把加入方放成白方"

        host.move(9, 9)
        assert _pump_until(qapp, lambda: win.board[9][9] == 1), "对手那一手没回显"
        assert win.human_moves == [], "对手的着法不该进我的复盘"

        _click(qapp, win, 0, 0)                    # 我执白
        assert _pump_until(qapp, lambda: win.board[0][0] == 2), "我的白子没落"
        assert [(m["seq"], m["stone"]) for m in win.human_moves] == [(2, 2)]

        host.move(9, 10)
        assert _pump_until(qapp, lambda: win.board[9][10] == 1), "对手第二手没回显"
        assert len(win.human_moves) == 1, "对手的着法又混进来了"

        # 强度页也走局域网那一支：全表 + 自定义。
        win._on_review_clicked()
        assert win.review_strength.mode == "review_lan"
        assert win.review_strength.min_level == 1
    finally:
        host.close()
        local.stop()
        _teardown(win, qapp)

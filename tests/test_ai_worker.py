# -*- coding: utf-8 -*-
"""AIWorker 与 Player 抽象的接线（M2）。零搜索、毫秒级。

真正的搜索正确性由引擎自己的测试负责；这里只钉"线程把活交给了玩家、
石色与取消事件原样传递、异常被翻译成错误哨兵"这三件事。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PyQt5.QtWidgets import QApplication

import main as M


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    return app


class _StubPlayer:
    def __init__(self):
        self.calls = []
        self.cancels = []

    def choose_move(self, board, stone, cancel=None):
        assert board[9][9] == 1              # 收到的是棋盘快照
        self.calls.append(stone)
        self.cancels.append(cancel)
        return 8, 8, {"reason": "stub"}


def test_ai_worker_delegates_to_player(qapp):
    player = _StubPlayer()
    board = np.zeros((19, 19), dtype=int)
    board[9][9] = 1
    w = M.AIWorker(board, player, 2)
    got = []
    w.finished.connect(lambda r, c, info: got.append((r, c, info)))
    w.run()                                  # 同步执行，不启线程
    qapp.processEvents()

    assert got and (got[0][0], got[0][1]) == (8, 8)
    assert player.calls == [2]               # 石色原样传给玩家
    assert player.cancels[0] is w._cancel    # 同一个取消事件


def test_ai_worker_translates_player_exception(qapp):
    class _Boom:
        def choose_move(self, board, stone, cancel=None):
            raise RuntimeError("boom")

    w = M.AIWorker(np.zeros((19, 19), dtype=int), _Boom(), 1)
    got = []
    w.finished.connect(lambda r, c, info: got.append((r, c, info)))
    w.run()
    qapp.processEvents()

    assert got and got[0][0] == -1 and got[0][1] == -1
    assert "RuntimeError" in got[0][2]["detail"]

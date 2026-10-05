# -*- coding: utf-8 -*-
"""动画与 GUI 冒烟：构造窗口 → 切主题 → 落子 → 动画收尾，全程不崩。

真正的"卡死/崩溃"大多发生在 paintEvent 与动画回调的组合上，纯逻辑测试
看不见它们 —— 所以这里走离屏真实控件路径。为让对局完全可控，AI 回合被
monkeypatch 成 no-op（engine 的正确性由它自己的测试负责）。
"""

from __future__ import annotations

import io
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtCore import QElapsedTimer, QEventLoop
from PyQt5.QtWidgets import QApplication, QWidget

import anim
import theme


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    theme.set_theme("dark", persist=False)


class _FixedAI:
    """房间 AI 席位替身：按行序走最近空点（冒烟不关心棋力）。

    M4c 起 AI 由房间的 ``AIPlayer`` 驱动；给它一个同步的替身，对局节奏
    才完全由测试的点击决定（与旧版 monkeypatch ``_ai_turn`` 为 no-op 的
    目的相同）。着法刻意选在 (0,0) 起步，不会挡到玩家在 9 行的连五。
    """

    def __init__(self, spec):
        self.spec = spec

    def choose_move(self, board, stone, cancel=None):
        for r in range(19):
            for c in range(19):
                if board[r][c] == 0:
                    return r, c, {"reason": "冒烟替身", "best_val": 0.0,
                                  "depth": 1}
        raise RuntimeError("满盘，没有可下的位置")


# ==================== anim 工具 ====================

def test_fade_in_runs_and_detaches_effect(qapp):
    w = QWidget()
    w.show()
    a = anim.fade_in(w, ms=30)
    assert w.graphicsEffect() is not None, "动画期间应挂着透明度效果"
    loop = QEventLoop()
    a.finished.connect(loop.quit)
    loop.exec_()
    qapp.processEvents()
    # 走完 duration 才发 finished → 效果被摘掉，控件恢复正常绘制路径
    assert w.graphicsEffect() is None
    w.deleteLater()


def test_pulse_start_and_stop(qapp):
    w = QWidget()
    w.show()
    anim.pulse(w)
    assert w.graphicsEffect() is not None
    anim.stop_pulse(w)
    assert w.graphicsEffect() is None
    anim.stop_pulse(w)          # 幂等：无动画时安全
    w.deleteLater()


# ==================== 窗口级冒烟 ====================

class _DummyLogger:
    """替身：不往项目根目录落 game_log_*.txt 文件。"""

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


def _build_game(monkeypatch, qapp):
    """建一局玩家执黑 vs 脚本 AI，等开局这批房间事件处理完。"""
    import main as M
    import room as room_module
    monkeypatch.setattr(M, "GameLogger", _DummyLogger)
    # AI 由房间驱动：换成同步替身，对局节奏完全由测试点击决定。
    monkeypatch.setattr(room_module, "AIPlayer", _FixedAI)
    win = M.GomokuGame()
    win.show()
    win._show_color_selection()
    win._on_color_selected(0)        # 玩家执黑
    win._on_difficulty_selected(1)
    assert _wait(qapp, lambda: win.game_panel is not None
                 and not win._move_in_flight), "开局事件未处理完"
    return win


def _drain(qapp, ms=300):
    """推进事件循环一小段时间，让定时器/动画走几拍。"""
    t = QElapsedTimer()
    t.start()
    while not t.hasExpired(ms):
        qapp.processEvents()


def _wait(qapp, pred, timeout=3.0):
    """轮询等待房间事件回显（M4c 起落子不再在点击调用栈里同步完成）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if pred():
            return True
        time.sleep(0.003)
    qapp.processEvents()
    return False


def _click_human(qapp, win, r, c):
    """轮到人类时点击一格（AI 思考中 / 有在途着法时先等）。"""
    assert _wait(qapp, lambda: not win.ai_thinking
                 and not win._move_in_flight), "等回合超时"
    win._on_board_click(_click(win.board_widget, r, c))


def test_game_smoke_theme_toggle(monkeypatch, qapp):
    import main as M
    # 起点必须是已知的。断言写的是"第一次切换后是 light"，所以起点得是 dark ——
    # 不显式指定的话，起点取决于 QSettings 里存着什么（开发者自己的偏好），
    # 于是这条测试会因为"你把主题切成了浅色"而红，而不是因为代码有问题。
    theme.set_theme("dark", persist=False)
    # 切换不落盘（不污染用户偏好）
    monkeypatch.setattr(
        theme, "toggle_theme",
        lambda *a, **k: theme.set_theme(
            "light" if theme.current_theme() == "dark" else "dark",
            persist=False))
    win = _build_game(monkeypatch, qapp)
    try:
        assert win.game_widget is not None
        assert win.board_widget is not None

        win._on_toggle_theme()
        assert theme.current_theme() == "light"
        _drain(qapp)

        win._on_toggle_theme()
        assert theme.current_theme() == "dark"
        _drain(qapp)
    finally:
        win._on_quit()
        qapp.processEvents()


def test_game_smoke_stone_anim_and_undo(monkeypatch, qapp):
    win = _build_game(monkeypatch, qapp)
    try:
        bw = win.board_widget
        _click_human(qapp, win, 9, 9)
        assert _wait(qapp, lambda: bw.board[9][9] == 1), "玩家落子未回显"
        assert bw._anim is not None, "落子应启动覆盖式动画"

        # 悔棋：set_last_move(None) 应取消在途动画（stop 不发 finished，
        # 收尾由 _cancel_stone_anim 手动完成）
        win._on_undo()
        assert _wait(qapp, lambda: win.move_count == 0
                     and bw.board[9][9] == 0), "悔棋未回退"
        assert bw._anim is None
        assert bw._anim_cell is None
        _drain(qapp)
    finally:
        win._on_quit()
        qapp.processEvents()


def test_game_smoke_win_line_highlight(monkeypatch, qapp):
    import main as M
    from engine_local import win_line
    win = _build_game(monkeypatch, qapp)
    try:
        bw = win.board_widget
        for c in range(4, 9):          # 玩家在 (9,4..8) 连成五
            _click_human(qapp, win, 9, c)
            if c < 8:
                n = 2 * (c - 3)        # 玩家 + AI 应答
                assert _wait(qapp, lambda n=n: win.move_count >= n), \
                    "第 %d 回合未完成" % (c - 3)
            else:
                assert _wait(qapp, lambda: win.game_over and bw.win_cells), \
                    "五连未判胜/未高亮"
        assert win.game_over
        cells = win_line(win.board, 1)
        assert len(cells) >= 5
        assert bw.win_cells == cells
        assert bw.win_player == 1
        _drain(qapp)
    finally:
        win._on_quit()
        qapp.processEvents()

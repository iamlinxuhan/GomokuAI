# -*- coding: utf-8 -*-
"""对局流程的行为契约（characterization tests）。

M4c 起 ``main.py`` 的单机对局改为"应用内房间（LocalRoom）+ RoomClient"：
UI 只提交意图，着法、判胜、悔棋政策全部由房间权威处理，事件异步回显。
所以本文件也从"直接调 Session / 直接调 _on_ai_finished"改为**房间驱动**：
monkeypatch ``room.AIPlayer`` 为脚本替身（房间每步会新建 AIPlayer 实例，
队列挂在替身类的闭包上，由所有实例共享），点击/悔棋后用轮询等待
（``processEvents`` + 截止时间）再断言。

契约清单与逐条对照见 ``tools/PLAN_SESSION.md`` 第 4 节。M4c 的覆盖变化
（都是**显式**调整，不是静默删除）：

* C6（判和，361 手）不再有 UI 级用例：旧用例靠直接写 ``board``/
  ``move_count`` 伪造残局，而新架构下棋盘由房间权威维护，UI 不能也不该
  伪造。判和规则由 ``tests/test_session.py::test_draw_at_full_board``
  覆盖（Session 是规则真源）。
* C8（代数作废）不再是 UI 职责：AI 调度搬进房间后，"迟到的 AIWorker
  结果"这条路径不存在了 —— 事件按连接顺序投递，开新局前旧客户端已整体
  关闭。该 UI 级用例删除；引擎的协作取消仍由 ``tests/test_cancel.py``
  等覆盖。
* C11：旧现状（AI 先手局悔棋撤两手后不重下天元、下一手由玩家白先走）
  被房间政策修正 —— 悔棋撤到请求方上一次落子之前，被撤的 AI 着法由房间
  重新计算（``room._recheck``）；撤掉天元后 AI 会重下天元。用例改为期望
  这一新行为，详见 ``test_ai_first_move_pair_undo_replays_tengen``。
"""

from __future__ import annotations

import io
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PyQt5.QtWidgets import QApplication, QLabel, QPushButton

import main as M
import room as room_module
from client import LocalRoom, RoomClient
from room import SeatSpec

BOARD_SIZE = 19

_REAL_LOGGER = M.GameLogger


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    return app


class _DummyLogger:
    """替身：不往项目根目录落 game_log_*.txt；保留坐标格式化。"""

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


def _human_click(qapp, win, r, c):
    """等"轮到自己且无在途着法"再点击一格。

    人机局 AI 回合的 turn 事件可能还在队列里（``ai_thinking`` 尚未置位或
    尚未复位），不等它点击会撞上 ``_on_board_click`` 的前置检查被丢掉。
    """
    assert _pump_until(qapp, lambda: not win.ai_thinking
                       and not win._move_in_flight), "等回合时空等超时"
    win._on_board_click(_click(win.board_widget, r, c))


def _teardown(win, qapp):
    """收尾：拆房间、关窗，并让 ``deleteLater`` 走完。

    不等销毁完成的话，上一个用例的窗口要等下一轮 processEvents 才真正
    销毁 —— 而那正是下一个用例正在轮询的同一段事件循环，会把废弃窗口的
    绘制/动画事件混进来（本机 offscreen 下会表现为原生崩溃）。
    """
    win._on_quit()
    qapp.processEvents()
    win.deleteLater()
    qapp.processEvents()


def _pump_until(qapp, pred, timeout=5.0):
    """轮询等待：推进事件循环直到 ``pred()`` 为真（或超时）。

    房间事件走 TCP + 读取线程 + Qt queued signal，不再像旧版那样在点击
    调用栈里同步完成，所以断言前必须等回显。
    """
    deadline = time.monotonic() + timeout
    while True:
        qapp.processEvents()
        if pred():
            return True
        if time.monotonic() > deadline:
            return False
        time.sleep(0.005)


def _scripted_ai(monkeypatch, moves):
    """把 ``room.AIPlayer`` 换成按队列出招的替身，返回共享队列。

    **队列不可以挂在实例上**：房间每到 AI 回合都会新建一个 ``AIPlayer``，
    实例级队列会在第二步就被新建实例重置。挂在闭包上，所有实例共享。
    """
    queue = [(int(r), int(c)) for r, c in moves]

    class _AI:
        def __init__(self, spec):
            self.spec = spec

        def choose_move(self, board, stone, cancel=None):
            assert queue, "测试脚本里的 AI 着法用完了"
            r, c = queue.pop(0)
            return r, c, {"reason": "测试脚本", "best_val": 0.0, "depth": 1}

    monkeypatch.setattr(room_module, "AIPlayer", _AI)
    return queue


def _build(monkeypatch, qapp, *, mode=0, color=0, level=1, ai=None):
    """构造一局，返回 ``(win, queue)``。

    mode=0 挑战 AI、mode=1 本地双人；color: 0=玩家执黑、1=AI 执黑。
    ai=[(r,c), ...] → 脚本化房间 AI 的着法队列（须给足合法着法）。
    queue 是同一个列表：用例可以用 ``not queue`` 判断 AI 是否已把着法
    全部走完（例如"悔棋导致 AI 重下"这类局面相同、只能靠调用次数区分的
    断言）。
    """
    monkeypatch.setattr(M, "GameLogger", _DummyLogger)
    queue = _scripted_ai(monkeypatch, ai) if ai is not None else None
    win = M.GomokuGame()
    win.show()
    if mode == 1:
        win._on_mode_selected(1)
    else:
        win._on_mode_selected(0)
        win._on_color_selected(color)
        win._on_difficulty_selected(level)
    if ai is not None and color == 1 and mode == 0:
        # AI 先手：等它第一手落定，否则用例点击时回合还没到人类。
        assert _pump_until(qapp, lambda: win.move_count >= 1,
                           timeout=5.0), "AI 先手局没有在时限内落子"
    else:
        # 玩家先手：等开局这批事件（welcome/state/turn）处理完。
        assert _pump_until(
            qapp, lambda: win.game_panel is not None and win._room is not None
            and not win._move_in_flight, timeout=5.0)
    return win, queue


# ==================== 落子与快照 ====================

def test_human_then_ai_move_updates_board_and_snapshot(monkeypatch, qapp):
    """C2/C3/C4：人机局一手换一手；human_moves 记的是落子**前**的局面。"""
    win, _ = _build(monkeypatch, qapp, mode=0, color=0, ai=[(8, 8)])
    try:
        _human_click(qapp, win, 9, 9)
        assert _pump_until(qapp, lambda: win.move_count == 2), "人机一手换一手未完成"

        assert win.board[9][9] == 1
        assert win.board[8][8] == 2
        assert win.last_move == (8, 8, 2)          # 最后一手是脚本 AI
        assert len(win.human_moves) == 1
        rec = win.human_moves[0]
        assert (rec["seq"], rec["r"], rec["c"]) == (1, 9, 9)
        assert rec["before"][9][9] == 0            # 落子前，不是落子后
        assert not rec["before"].any()
    finally:
        _teardown(win, qapp)


def test_human_moves_not_recorded_in_pvp(monkeypatch, qapp):
    """C3：本地双人没有"复盘对象"，human_moves 必须为空。"""
    win, _ = _build(monkeypatch, qapp, mode=1)
    try:
        _human_click(qapp, win, 9, 9)
        assert _pump_until(qapp, lambda: win.board[9][9] == 1), "本地首手未落子"
        assert win.human_moves == []
    finally:
        _teardown(win, qapp)


# ==================== 轮次（本地双人） ====================

def test_pvp_alternates_by_move_parity_and_never_calls_ai(monkeypatch, qapp):
    """C2：本地对战黑先、按手数奇偶换色；两个席位都是 remote（无 AI）。"""
    win, _ = _build(monkeypatch, qapp, mode=1)
    try:
        # 结构断言：本地对战的房间席位必须都是 remote，AI 根本不在桌上。
        specs = win._room.room.specs
        assert specs[1].kind == "remote" and specs[2].kind == "remote"

        _human_click(qapp, win, 9, 9)
        assert _pump_until(qapp, lambda: win.board[9][9] == 1), "黑子未落"
        _human_click(qapp, win, 8, 8)
        assert _pump_until(qapp, lambda: win.board[8][8] == 2), "白子未落"
        _human_click(qapp, win, 9, 8)
        assert _pump_until(qapp, lambda: win.board[9][8] == 1), "第二颗黑子未落"

        assert win.board[9][9] == 1
        assert win.board[8][8] == 2
        assert win.board[9][8] == 1
        assert win.move_count == 3
    finally:
        _teardown(win, qapp)


# ==================== 悔棋 ====================

def test_undo_in_human_vs_ai_removes_two_and_trims_human_moves(monkeypatch, qapp):
    """C9：人机悔棋一次撤"玩家 + AI"两手，并剪掉已收回的复盘记录。"""
    win, _ = _build(monkeypatch, qapp, mode=0, color=0, ai=[(8, 8), (7, 7)])
    try:
        _human_click(qapp, win, 9, 9)
        assert _pump_until(qapp, lambda: win.move_count == 2), "第一回合未完成"
        _human_click(qapp, win, 9, 8)
        assert _pump_until(qapp, lambda: win.move_count == 4), "第二回合未完成"
        assert len(win.human_moves) == 2

        win._on_undo()
        assert _pump_until(qapp, lambda: win.move_count == 2), "悔棋未回到第 2 手"
        assert win.board[9][8] == 0 and win.board[7][7] == 0
        assert win.board[9][9] == 1 and win.board[8][8] == 2
        assert [m["seq"] for m in win.human_moves] == [1]
        assert win.output == 2
    finally:
        _teardown(win, qapp)


def test_undo_limit_is_three(monkeypatch, qapp):
    """C9：悔棋上限 3 次，用尽后点击无动作。"""
    win, _ = _build(monkeypatch, qapp, mode=0, color=0,
                    ai=[(8, 4), (8, 6), (8, 8), (8, 10), (8, 12)])
    try:
        for i, c in enumerate((0, 2, 4, 6, 8), start=1):
            _human_click(qapp, win, 12, c)
            assert _pump_until(qapp, lambda n=2 * i: win.move_count >= n), \
                "第 %d 回合未完成" % i
        assert win.move_count == 10

        for i in range(3):
            win._on_undo()
            want = 8 - 2 * i
            assert _pump_until(
                qapp, lambda n=want, o=2 - i: win.move_count == n
                and win.output == o), "第 %d 次悔棋未生效" % (i + 1)
        assert win.move_count == 4
        assert win.output == 0
        assert [m["seq"] for m in win.human_moves] == [1, 3]

        win._on_undo()                     # 第 4 次：上限已到，什么也不做
        _pump_until(qapp, lambda: False, timeout=0.1)
        assert win.move_count == 4
    finally:
        _teardown(win, qapp)


def test_undo_in_pvp_removes_one(monkeypatch, qapp):
    """C9：本地对战一次只撤一手（"撤回玩家 + AI 回应"在这里没有对应物）。"""
    win, _ = _build(monkeypatch, qapp, mode=1)
    try:
        _human_click(qapp, win, 9, 9)
        assert _pump_until(qapp, lambda: win.board[9][9] == 1), "黑子未落"
        _human_click(qapp, win, 8, 8)
        assert _pump_until(qapp, lambda: win.board[8][8] == 2), "白子未落"

        win._on_undo()
        assert _pump_until(qapp, lambda: win.move_count == 1
                           and win.output == 2), "本地悔棋未退回一步"
        assert win.board[9][9] == 1
        assert win.board[8][8] == 0
        assert win.output == 2
    finally:
        _teardown(win, qapp)


# ==================== AI 先手（C10 / C11） ====================

def test_ai_first_move_is_tengen_and_single_undo_replays_it(monkeypatch, qapp):
    """C10：AI 先手走天元；仅天元这一手时悔棋会撤掉并让 AI 重下。

    旧版由 ``_ai_first_move`` 直接落天元（不经搜索）；M4c 起 AI 席位在房间
    里走 ``engine.ai_move`` 的空盘路由（= ``opening_move`` 天元），被悔棋
    撤掉后由房间主循环重新计算 —— 行为仍是"重下天元"。
    """
    win, queue = _build(monkeypatch, qapp, mode=0, color=1,
                        ai=[(9, 9), (9, 9)])
    try:
        assert win.move_count == 1
        assert win.board[9][9] == 1

        win._on_undo()
        assert _pump_until(
            qapp,
            lambda: not queue and win.output == 2
            and win.session.last_move == (9, 9, 1)
            and not win._move_in_flight,
            timeout=5.0), "AI 没有重下天元"
        assert win.move_count == 1           # 撤掉又立刻补回
        assert win.board[9][9] == 1
    finally:
        _teardown(win, qapp)


def test_ai_first_move_pair_undo_replays_tengen(monkeypatch, qapp):
    """C11（M4c 修正）：AI 先手局连续悔棋，撤掉天元后由 AI 重下天元。

    旧现状（PLAN_SESSION C11 标为疑似缺陷）：天元 + 玩家一手后悔棋撤两手，
    天元不重下，下一手由玩家（白）先走 —— 盘面出现"白棋先手、天元空着"。
    M4c 改成房间政策后：

    1. 天元 + 人类一手 + AI 回应（共 3 手）后悔棋：房间撤 2（人类那手 +
       AI 的回应），天元保留，轮到人类重下；
    2. 人类一手未下时再悔一次：房间撤 1（天元）并重新判断轮次，
       AI 重下天元 —— 修正后的行为，而不是让白棋在空盘上先走。
    """
    win, queue = _build(monkeypatch, qapp, mode=0, color=1,
                        ai=[(9, 9), (7, 7), (9, 9)])
    try:
        _human_click(qapp, win, 8, 8)             # 玩家（白）
        assert _pump_until(qapp, lambda: win.move_count == 3), "玩家+AI 回应未完成"

        win._on_undo()                            # 撤 2：白 + AI 回应，天元保留
        assert _pump_until(
            qapp, lambda: win.move_count == 1 and win.output == 2), "第一次悔棋未生效"
        assert win.board[9][9] == 1, "天元不该被一起撤掉（旧现状的错乱）"
        assert win.board[7][7] == 0 and win.board[8][8] == 0
        assert win.human_moves == []

        win._on_undo()                            # 撤 1：天元 → AI 重下天元
        assert _pump_until(
            qapp,
            lambda: not queue and win.output == 1
            and win.session.last_move == (9, 9, 1)
            and not win._move_in_flight,
            timeout=5.0), "撤掉天元后 AI 没有重下天元"
        assert win.move_count == 1
        assert win.board[9][9] == 1
        assert int(np.count_nonzero(win.board)) == 1   # 盘上只剩重下的天元
    finally:
        _teardown(win, qapp)


# ==================== 终局 ====================

def test_player_win_sets_gamerule_and_blocks_later_clicks(monkeypatch, qapp):
    """C1/C5：玩家五连 → gamerule=2、winner=玩家子色；终局后点击被丢弃。"""
    win, _ = _build(monkeypatch, qapp, mode=0, color=0,
                    ai=[(0, 0), (0, 2), (0, 4), (0, 6)])
    try:
        bw = win.board_widget
        for i, c in enumerate(range(4, 9), start=1):
            _human_click(qapp, win, 9, c)
            if i < 5:
                assert _pump_until(qapp, lambda n=2 * i: win.move_count >= n), \
                    "第 %d 回合未完成" % i
            else:
                assert _pump_until(qapp, lambda: win.game_over), "五连后未判胜"

        assert win.game_over
        assert win.gamerule == 2
        assert win.winner == 1
        assert win.board_widget.win_cells      # 连五高亮已给出

        blocked = win.move_count
        win._on_board_click(_click(bw, 18, 18))
        _pump_until(qapp, lambda: False, timeout=0.1)
        assert win.move_count == blocked
        assert win.board[18][18] == 0
    finally:
        _teardown(win, qapp)


def test_ai_win_sets_gamerule_and_winner_is_ai_stone(monkeypatch, qapp):
    """C5：AI 五连 → gamerule=1、winner=AI 子色。"""
    win, _ = _build(monkeypatch, qapp, mode=0, color=0,
                    ai=[(8, 4), (8, 5), (8, 6), (8, 7), (8, 8)])
    try:
        for i, c in enumerate((0, 2, 4, 6, 8), start=1):
            _human_click(qapp, win, 12, c)
            assert _pump_until(
                qapp, lambda n=2 * i: win.move_count >= n or win.game_over), \
                "第 %d 回合未完成" % i
        assert _pump_until(qapp, lambda: win.game_over), "AI 五连后未判负"

        assert win.game_over
        assert win.gamerule == 1
        assert win.winner == 2
        assert win.board[8][8] == 2
        assert win.board_widget.win_cells
    finally:
        _teardown(win, qapp)


# ==================== 重开 ====================

def test_restart_returns_to_mode_selection(monkeypatch, qapp):
    """C13：重开回到模式选择页（而不是跳过"跟谁下"直接进颜色页）。"""
    win, _ = _build(monkeypatch, qapp, mode=1)
    try:
        _human_click(qapp, win, 9, 9)
        assert _pump_until(qapp, lambda: win.board[9][9] == 1), "首手未落"
        win._on_restart()
        qapp.processEvents()
        assert win.central.currentWidget() is win.selection_mode
        assert win._room is None, "重开后房间应已拆掉"
    finally:
        _teardown(win, qapp)


# ==================== 局域网与 RoomConfig（M4d）====================

def test_lan_mode_opens_menu_and_join_screen(monkeypatch, qapp):
    """第三张卡 → 开房/加入岔路口；加入页离屏可构造、可原地报错。"""
    monkeypatch.setattr(M, "GameLogger", _DummyLogger)
    win = M.GomokuGame()
    win.show()
    try:
        win._on_mode_selected(2)
        qapp.processEvents()
        assert win.playmode == 2
        assert win.lan_menu is not None
        assert win.central.currentWidget() is win.lan_menu
        texts = [lab.text() for lab in win.lan_menu.findChildren(QLabel)]
        assert any("创建房间" in t for t in texts), texts
        assert any("加入房间" in t for t in texts), texts

        win.lan_menu.join_clicked.emit()
        qapp.processEvents()
        assert win.lan_join is not None
        assert win.central.currentWidget() is win.lan_join
        # 加入页不再让用户选颜色：房主定过色，剩余席位由服务端分配
        labels = [b.text() for b in win.lan_join.findChildren(QPushButton)]
        assert any("加入房间" in t for t in labels), labels
        assert all("执黑" not in t and "执白" not in t for t in labels), labels

        # 端口不是数字：留在加入页显示原因（不发起连接）
        win._on_lan_join_requested("127.0.0.1", "not-a-port")
        qapp.processEvents()
        assert win.lan_join is not None
        assert "端口" in win.lan_join.error_label.text()
    finally:
        _teardown(win, qapp)


def test_lan_join_room_full_shows_error(monkeypatch, qapp):
    """房间两席都有人：加入失败留在页面上，错误可读、可原地重试。"""
    monkeypatch.setattr(M, "GameLogger", _DummyLogger)
    local = LocalRoom(SeatSpec("remote"), SeatSpec("remote"),
                      name=M.LAN_ROOM_NAME)
    h1, h2 = RoomClient(), RoomClient()
    h1.connect(local.host, local.port, local.name, "black")
    h2.connect(local.host, local.port, local.name, "white")
    win = M.GomokuGame()
    win.show()
    try:
        win._on_mode_selected(2)
        qapp.processEvents()
        win._on_lan_join_requested("127.0.0.1", str(local.port))
        qapp.processEvents()
        assert win.lan_join is not None
        assert "已满" in win.lan_join.error_label.text()
        assert win._room_clients == {}, "失败后不该留下半开的客户端"
    finally:
        h1.close()
        h2.close()
        local.stop()
        _teardown(win, qapp)


def test_lan_host_waits_then_guest_joins_and_plays(monkeypatch, qapp):
    """开房方等待 → 对手入座自动进棋盘 → 双方落子经房间回显。"""
    monkeypatch.setattr(M, "GameLogger", _DummyLogger)
    win = M.GomokuGame()
    win.show()
    guest = RoomClient()
    try:
        win._on_mode_selected(2)
        qapp.processEvents()
        win._on_lan_create()
        qapp.processEvents()
        assert win.selection_color is not None, "创建房间应先走颜色页"

        win._on_color_selected(0)              # 房主执黑
        qapp.processEvents()
        assert win.lan_wait is not None, "开房后应显示等待遮罩"
        assert win._room is not None and win._room.host == "0.0.0.0"
        assert win._room.room.auto_consent is False, "LAN 对手的悔棋要协商"

        # 客人（另一台设备的等价物）从 127.0.0.1 入座；**不用选色** ——
        # 服务端把房主没占的那一席（白）分配给他
        guest.connect("127.0.0.1", win._room.port, M.LAN_ROOM_NAME, "auto")
        assert guest.seat == "white"
        assert _pump_until(qapp, lambda: win.lan_wait is None), \
            "对手入座后应收回等待屏"
        assert win.central.currentWidget() is win.game_widget
        assert win.session.human_stone == 1

        # 房主先手落子 → 客人收到；客人应一手 → 房主棋盘回显
        _human_click(qapp, win, 9, 9)
        assert _pump_until(qapp, lambda: win.board[9][9] == 1)
        guest.move(8, 8)
        assert _pump_until(qapp, lambda: win.board[8][8] == 2), "对手着法未回显"
        assert win.output == 3, "LAN 默认额度应与单机一致（3）"
    finally:
        guest.close()
        _teardown(win, qapp)


def test_room_config_disables_ui_entries(monkeypatch, qapp):
    """welcome 的 RoomConfig 落到界面：禁悔棋/重开、限额度、关复盘与曲线。"""
    monkeypatch.setattr(M, "GameLogger", _DummyLogger)
    win = M.GomokuGame()
    win.show()
    try:
        win.playmode = 0
        win.gamemode = 0
        win._build_game_ui()
        win._room_config = {"allow_undo": False, "undo_limit": 1,
                            "allow_review": False, "allow_restart": False,
                            "show_ai_scores": False}
        win._apply_room_config()
        assert not win.game_panel.undo_btn.isEnabled()
        assert not win.game_panel.restart_btn.isEnabled()
        assert win.output == 1, "面板剩余次数应与房间下发额度一致"

        # 就算是"输给 AI"，allow_review=False 也不给复盘入口
        win.human_moves = [{"seq": 1, "r": 9, "c": 9}]
        win.gamerule = 1
        win.winner = 2
        win.game_over = True
        win._show_game_over()
        assert win.game_over_overlay.can_review is False
        assert win.game_over_overlay.can_restart is False
        labels = [b.text() for b in
                  win.game_over_overlay.findChildren(QPushButton)]
        assert all("算法复盘" not in t for t in labels), labels
        assert all("再来一局" not in t for t in labels), labels

        n = len(win.game_panel._series)
        win._record_score({"best_val": 123.0, "depth": 3})
        assert len(win.game_panel._series) == n, "show_ai_scores=False 不追点"
    finally:
        _teardown(win, qapp)


def test_undo_proposed_dialog_paths(monkeypatch, qapp):
    """对手提案 → 弹窗 → 经我方连接应答；自己发起的提案不弹窗。

    真实 QMessageBox 的模态交互在离屏下无法自动点，这里替换掉弹窗本身，
    覆盖它两侧的分支逻辑（弹窗实现本身由 Qt 保证）。
    """
    class _StubClient:
        def __init__(self):
            self.responses = []

        def undo_response(self, accept):
            self.responses.append(accept)

    monkeypatch.setattr(M, "GameLogger", _DummyLogger)
    monkeypatch.setattr(M.QMessageBox, "question",
                        staticmethod(lambda *a, **k: M.QMessageBox.Yes))
    win = M.GomokuGame()
    win.show()
    stub = _StubClient()
    try:
        win.playmode = 2
        win.session.human_stone = 2
        win._room_clients[2] = stub
        win._on_room_undo_proposed({"type": "undo_proposed", "by": 1})
        assert stub.responses == [True], "同意要经我方连接发出"

        monkeypatch.setattr(M.QMessageBox, "question",
                            staticmethod(lambda *a, **k: M.QMessageBox.No))
        win._on_room_undo_proposed({"type": "undo_proposed", "by": 1})
        assert stub.responses == [True, False]

        # 自己发起的申请房间也会广播回来：不弹窗、不应答
        win._on_room_undo_proposed({"type": "undo_proposed", "by": 2})
        assert stub.responses == [True, False]
    finally:
        win._room_clients = {}
        _teardown(win, qapp)


def test_lan_join_auto_assigns_remaining_color(monkeypatch, qapp):
    """房主执黑 → 加入方不选色，自动成为白方并进入对局。"""
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
        win._on_lan_join_requested("127.0.0.1", str(local.port))
        qapp.processEvents()
        assert _pump_until(qapp, lambda: win.session.human_stone == 2), \
            "自动席位没有把加入方放成白方"
        assert 2 in win._room_clients and 1 not in win._room_clients
        assert win.playmode == 2 and win.gamemode == 1

        # 房主先手落子 → 加入方镜像回显
        host.move(9, 9)
        assert _pump_until(qapp, lambda: win.board[9][9] == 1)
    finally:
        host.close()
        local.stop()
        _teardown(win, qapp)

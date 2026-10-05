# -*- coding: utf-8 -*-
"""无头 GUI 冒烟测试。

不需要显示器：设置 ``QT_QPA_PLATFORM=offscreen`` 后构造真实的 ``GomokuGame``，
用 ``QTest`` 派发真实的鼠标点击，检查界面流程不崩溃、棋盘状态自洽。

**它测什么、不测什么**：本脚本验证的是"UI 与引擎的接线没断" —— 点击能落到
正确的格子、AI 回合能返回并把棋子写回棋盘、悔棋/重开/退出不会把界面留在
半死不活的状态。它**不**判断 AI 走得好不好（那是 ``positions.py`` 与
``selfplay.py`` 的事）。

两处刻意的设计：

* **日志重定向到临时目录** —— ``GameLogger`` 会往仓库根目录写
  ``game_log_*.txt``。冒烟测试跑一次就多几个文件，所以这里只把输出目录换掉，
  记录逻辑本身照常执行。
* **取消延迟是断言，不是报告** —— 引擎已实现协作取消（``engine.py`` 里每 1024
  个节点轮询 deadline 与 ``cancel``），所以"思考中途重开"能直接要求 worker 在
  0.5s 内退出。这条曾经以 ``deferred_until`` 记成 WARN：那时引擎接受 ``cancel``
  参数但从不读取，只能等满 ``_cancel_ai`` 的 3 秒余量 —— 一个永远黄的检查很快
  就没人看了，这是它被转正的原因。

用法::

    .venv/bin/python tools/gui_smoke.py            # 全部检查
    .venv/bin/python tools/gui_smoke.py --keep     # 保留临时日志目录
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import time
import traceback
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# 自动适配会按离屏平台的 800×600 把窗口降到 722×552、字号降到 small，把下面
# 那些按设计尺寸写的断言全搅乱 —— 而那些断言测的不是"自动适配"这件事。
os.environ.setdefault("GOMOKU_AI_UI_AUTOFIT", "0")

import numpy as np  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from PyQt5.QtCore import QPoint, QSettings, Qt  # noqa: E402
from PyQt5.QtTest import QTest  # noqa: E402
from PyQt5.QtWidgets import (QApplication, QLabel, QPushButton,  # noqa: E402
                             QScrollArea)

import theme  # noqa: E402
import main as M  # noqa: E402

# 兜底上限：取消走协作轮询（实测 10ms 量级就返回），这个值只用来在
# "取消路径整个失灵、只能等满超时"时把测试从挂死里救出来。
CANCEL_SAFETY_S = 10.0

_RESULTS = []  # (等级, 名称, 说明)


def record(level, name, detail=""):
    _RESULTS.append((level, name, detail))
    mark = {"PASS": "  ok  ", "FAIL": " FAIL ", "WARN": " warn "}[level]
    print(f"[{mark}] {name}" + (f"  —— {detail}" if detail else ""))


def check(cond, name, detail=""):
    record("PASS" if cond else "FAIL", name, detail)
    return cond


def expect(cond, name, detail="", deferred_until=None):
    """已知且已排期的问题记 WARN，未知问题记 FAIL。

    `deferred_until` 用来标注"方案里明确留到某个阶段才修"的量。把它记成 FAIL
    会让每次运行都带着几条红色，久而久之没人再看这个输出 —— 真正的新 failure
    就混在里面被忽略了。
    """
    if cond:
        record("PASS", name, detail)
    elif deferred_until:
        record("WARN", name, f"{detail}（{deferred_until}）")
    else:
        record("FAIL", name, detail)
    return cond


# ------------------------------------------------------------------ 工具

def pump(ms=50, app=None):
    """让 Qt 事件循环转 ms 毫秒（QThread 的 finished 信号要靠它投递）。"""
    app = app or QApplication.instance()
    deadline = time.monotonic() + ms / 1000.0
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)


def wait_until(pred, timeout_s, app=None):
    """轮询 pred()，返回 (是否达成, 实际等待秒数)。"""
    app = app or QApplication.instance()
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        if pred():
            return True, time.monotonic() - t0
        app.processEvents()
        time.sleep(0.01)
    return False, time.monotonic() - t0


def board_is_legal(board, expect_stones=None):
    """棋盘合法性：只含 0/1/2，且（给定 move_count 时）子数与步数一致。"""
    if board.shape != (M.BOARD_SIZE, M.BOARD_SIZE):
        return False, f"形状异常 {board.shape}"
    vals = set(np.unique(board).tolist())
    if not vals.issubset({0, 1, 2}):
        return False, f"出现非法棋子值 {sorted(vals)}"
    n = int(np.count_nonzero(board))
    if expect_stones is not None and n != expect_stones:
        return False, f"子数 {n} != move_count {expect_stones}"
    return True, f"{n} 子"


def click_cell(w, r, c):
    """派发一次真实鼠标点击，落在格点 ``(r, c)`` 的中心。

    坐标取自 ``BoardWidget.cell_center``（唯一真源），**不在这里自己算
    ``MARGIN + c * CELL_SIZE``** —— 那个公式只在设计尺寸下成立。棋盘一旦
    可缩放（离屏平台是 800x600，一定会触发缩放），自己算就会把点击投到
    错误的格子上，而 ``QTest`` 即便坐标落在控件外也照样投递，于是失败会
    表现为"棋子落在别处"而不是崩溃。
    """
    QTest.mouseClick(w.board_widget, Qt.LeftButton,
                     pos=w.board_widget.cell_center(r, c))


def pick_empty_near_center(board):
    """挑一个离天元最近的空点（保证点击一定落在可下位置）。"""
    empty = np.argwhere(board == 0)
    if not len(empty):
        return None
    center = (M.BOARD_SIZE // 2, M.BOARD_SIZE // 2)
    d = ((empty[:, 0] - center[0]) ** 2 + (empty[:, 1] - center[1]) ** 2)
    r, c = empty[int(np.argmin(d))]
    return int(r), int(c)


# ------------------------------------------------------------------ 检查项

def do_startup(app):
    """构造窗口并走完选择流程，进入对局界面。"""
    w = M.GomokuGame()
    w.show()
    pump(120, app)
    check(w.board_widget is None, "构造后停在选择界面（未直接进对局）")

    w._on_color_selected(0)          # 0 = 玩家执黑先行
    pump(60, app)
    check(w.gamemode == 0, "选择执黑 -> gamemode=0")

    w._on_difficulty_selected(1)     # 1 级，跑得快
    pump(120, app)
    check(w.board_widget is not None, "难度选定后棋盘已构建")
    check(w.gamekunnan == 1, "难度写入 gamekunnan")
    ok, detail = board_is_legal(w.board, w.move_count)
    check(ok, "开局棋盘合法", detail)
    return w


def do_player_moves(app, w, n=3):
    """玩家点 n 手，每手等 AI 应答，逐步校验棋盘自洽。

    数子用**黑子数**而非总子数：玩家执黑，黑子只可能是玩家落的，因此这个
    计数不受 AI 应答快慢影响。首手时盘上仅 1 子，引擎走快速路径几乎瞬回，
    用总子数会误判成"玩家一次落了两子"。
    """
    for i in range(n):
        if w.game_over:
            record("WARN", f"第 {i+1} 手跳过", "对局已结束")
            break

        got_idle, _ = wait_until(lambda: not w.ai_thinking, 30, app)
        if not check(got_idle, f"第 {i+1} 手：轮到玩家时 AI 空闲"):
            return

        cell = pick_empty_near_center(w.board)
        if not check(cell is not None, f"第 {i+1} 手：还存在空点"):
            return
        r, c = cell
        before_black = int((w.board == 1).sum())
        before_white = int((w.board == 2).sum())
        click_cell(w, r, c)
        pump(30, app)

        if not check(int(w.board[r][c]) == 1, f"第 {i+1} 手：点击落到了 ({r},{c})",
                     f"棋盘点 {w.board[r][c]}"):
            return
        now_black = int((w.board == 1).sum())
        if not check(now_black == before_black + 1,
                     f"第 {i+1} 手：玩家恰好落一子",
                     f"黑子 {before_black} -> {now_black}"):
            return

        if w.game_over:
            record("PASS", f"第 {i+1} 手", "玩家直接连五，对局结束")
            break

        got_ai, dt = wait_until(lambda: not w.ai_thinking, 30, app)
        if not check(got_ai, f"第 {i+1} 手：AI 应答返回", f"{dt:.2f}s"):
            return
        now_white = int((w.board == 2).sum())
        if not check(w.game_over or now_white == before_white + 1,
                     f"第 {i+1} 手：AI 恰好落一子",
                     f"白子 {before_white} -> {now_white}"):
            return

        ok, detail = board_is_legal(w.board, w.move_count)
        check(ok, f"第 {i+1} 手：棋盘依然合法", detail)


def do_theme(app, w):
    """B21 回归：所有按钮都走 ``theme`` 的变体机制，而不是各自的样式表。

    这条**替换**了原先的 ``do_stylesheet``。那个检查断言"按钮样式表里不含
    'QColor object'"，在改用全局 QSS 之后 ``btn.styleSheet()`` 恒为空串，
    于是它恒过 —— 一条永远绿的检查比没有检查更糟，因为它会让人以为这里有
    覆盖。现在断言的是真正起作用的东西：每个按钮都挂了 ``variant`` 属性，
    否则它就落不到任何一条 ``QPushButton[variant=...]`` 规则上，禁用态、
    悬停态、按下态全部缺失。
    """
    btns = w.findChildren(QPushButton)
    missing = [b.text() or "(无题)" for b in btns if not b.property("variant")]
    check(not missing, "所有按钮都挂了 variant 属性",
          f"缺 variant: {missing}" if missing else f"检查了 {len(btns)} 个按钮")

    # 变体名必须真的在 theme 里定义过，否则同样落不到规则上
    known = set(theme.button_variants()) | {"card"}
    unknown = sorted({b.property("variant") for b in btns
                      if b.property("variant") not in known})
    check(not unknown, "按钮 variant 都在 theme 里有定义",
          f"未定义的变体: {unknown}" if unknown else f"已知变体: {sorted(known)}")

    # 禁用态曾经是"界面在撒谎"的根源：AI 思考期间悔棋按钮 setEnabled(False)，
    # 但全文件 36 处样式表里零个 :disabled 规则，按钮看起来完全正常。
    qss = QApplication.instance().styleSheet()
    check(":disabled" in qss, "全局 QSS 含 :disabled 规则",
          "有" if ":disabled" in qss else "缺失 —— 禁用态将不可见")


def do_disabled_state(app, w):
    """禁用态必须**看得见**。

    ``_ai_turn`` 会在 AI 思考期间 ``undo_btn.setEnabled(False)``。旧的全文件
    36 处样式表里零个 ``:disabled`` 规则，于是用户看到一个外观完全正常的按钮，
    点下去毫无反应 —— 这不是观感问题，是界面在撒谎。

    这里渲染两次（启用/禁用）比对**整窗合成后**的按钮区域像素。不能用
    ``btn.grab()``：单独 grab 一个子控件不合成父级，拿不到真实的 QSS 底色。
    """
    btn = w.game_panel.undo_btn
    origin = btn.mapTo(w, QPoint(0, 0))
    box = (origin.x(), origin.y(), btn.width(), btn.height())

    def shot():
        for _ in range(6):
            app.processEvents()
        img = w.grab().toImage()
        return [img.pixel(x, y) for x in range(box[0], box[0] + box[2], 3)
                for y in range(box[1], box[1] + box[3], 3)]

    btn.setEnabled(True)
    on = shot()
    btn.setEnabled(False)
    off = shot()
    btn.setEnabled(True)

    changed = sum(1 for a, b in zip(on, off) if a != b)
    check(changed > 0.5 * len(on), "禁用态外观确实改变（不再是'看不见的禁用'）",
          f"{changed}/{len(on)} 个采样点不同" if changed else "启用/禁用渲染完全相同")


def do_charts(app, w):
    """右侧面板两个图表的接线：序列与 ``move_history`` 严格同长。

    **不变量只有一条：``len(series) == len(move_history)``。** 图表画的不是
    棋盘状态，是"每一手留下的一个分值"，所以它必须与历史同生共死。最容易漏的
    是悔棋 —— 少了同步，曲线会留着已经被撤销那几手的点，而画面看上去完全正常
    （多点几个点而已），只有把手指按在棋盘上数才看得出来。

    主题切换不重建序列：序列挂在 ``GamePanel`` 上，切主题只重装 QSS。
    这条单独钉是因为"切主题后图变空了"曾经是个真会发生的回归类型。
    """
    panel = getattr(w, "game_panel", None)
    if panel is None:
        record("FAIL", "图表接线", "没有 game_panel")
        return

    n_hist = len(w.move_history)
    n_ser = len(panel._series)
    check(n_ser == n_hist, "序列与 move_history 同长",
          f"序列 {n_ser} / 历史 {n_hist}")

    # 两种点都要出现过：玩家落子后记静态估值，AI 落子后记搜索结果。
    kinds = {k for _, k in panel._series}
    check(kinds == {"search", "static"}, "两种来路的点都记到了",
          f"出现过的来路: {sorted(kinds)}" if kinds else "序列是空的")

    # 悔棋：序列必须跟着回退（这是 _rewind 存在的唯一理由）
    if n_ser and not w.ai_thinking and not w.game_over:
        before = len(panel._series)
        w._on_undo()
        pump(60, app)
        after = len(panel._series)
        check(after < before, "悔棋后序列跟着回退", f"{before} -> {after}")
        check(after == len(w.move_history), "悔棋后序列仍与 move_history 同长",
              f"序列 {after} / 历史 {len(w.move_history)}")

    # 主题切换不重建序列（切完再切回来，长度必须原样）
    if panel._series:
        before = len(panel._series)
        start_theme = theme.current_theme()
        with mock.patch.object(
                theme, "toggle_theme",
                lambda *a, **k: theme.set_theme(
                    "light" if theme.current_theme() == "dark" else "dark",
                    persist=False)):
            w._on_toggle_theme()
            pump(40, app)
            n_light = len(panel._series)
            w._on_toggle_theme()
            pump(40, app)
        check(n_light == before and len(panel._series) == before,
              "主题切换不清空图表序列",
              f"{before} -> {n_light} -> {len(panel._series)}")
        # 起点取实际值而不是写死 "dark"：本脚本不落盘主题偏好，起点取决于
        # 开发者 QSettings 里存的是什么，写死会让这条因为别人的偏好而红。
        check(theme.current_theme() == start_theme, "切两次回到原主题",
              f"{start_theme} -> {theme.current_theme()}")

    # 纵轴数量级只增不减：回缩会让同一条曲线在下一手看着突然变陡。
    dec = panel._decade
    panel.push_score(1.0, "static")
    check(panel._decade >= dec, "评分图数量级只增不减",
          f"{dec} -> {panel._decade}（喂了一个 1.0）")
    panel.truncate_series(len(w.move_history))   # 把上面这针试探撤掉


def do_undo(app, w):
    """悔棋后棋盘与步数必须同步回退。"""
    if w.game_over or w.ai_thinking:
        record("WARN", "悔棋", "对局已结束或 AI 正在思考，跳过")
        return
    before_moves = len(w.move_history)
    before_stones = int(np.count_nonzero(w.board))
    w._on_undo()
    pump(60, app)
    check(len(w.move_history) < before_moves, "悔棋回退了历史记录",
          f"{before_moves} -> {len(w.move_history)}")
    ok, detail = board_is_legal(w.board, w.move_count)
    check(ok, "悔棋后棋盘合法", detail)
    check(int(np.count_nonzero(w.board)) < before_stones,
          "悔棋后盘上子数减少",
          f"{before_stones} -> {np.count_nonzero(w.board)}")


def _enter_thinking_game(app, w, level):
    """进入一局"AI 正**在**思考"的状态，用于验证取消路径。

    难点在于"思考中"必须是**真的**，否则测的就不是取消而是"线程自己跑完了"。
    实测难度 3 的搜索耗时随子数突变：盘上 2 子时 14ms 就返回（评估认为没有
    可搜的东西），4 子起才跑满约 8 秒。所以这里先垫够子数，再用一个短 pump
    把"瞬回"和"真在搜"区分开：

      * 点击后 pump 150ms，`ai_thinking` 仍为 True → 搜索确实还在跑，可用；
      * 已复位 → 这次搜索太快，继续垫子重试。

    选 **AI 先手**是为了让垫子阶段的落子全由 AI 自己完成，玩家的点击不会
    因为"轮次不对"被 `_on_board_click` 丢掉。
    """
    w._on_color_selected(1)          # 1 = AI 先手执黑
    pump(40, app)
    w._on_difficulty_selected(level)
    pump(60, app)

    landed, _ = wait_until(lambda: int(np.count_nonzero(w.board)) >= 1, 15, app)
    if not landed:
        return False
    wait_until(lambda: not w.ai_thinking, 15, app)

    # 150ms 的窗口是在引擎还很慢时定的，现在是**过长**的：引擎快了一个
    # 数量级之后，一个 60ms 就能算完的局面照样能通过 150ms 的判定，
    # 于是"取消路径被覆盖"这句话变得没有依据。改成 40ms —— 只要求
    # "点击之后 40ms 搜索仍在跑"，这就是真正的中途取消。
    for _ in range(12):              # 5 手太少：盘上十几子时搜索仍可能在 40ms 内结束
        cell = pick_empty_near_center(w.board)
        if cell is None:
            return False
        click_cell(w, *cell)
        pump(40, app)
        if w.ai_thinking:
            return True
        if w.game_over:
            return False
        wait_until(lambda: not w.ai_thinking, 30, app)
        pump(20, app)
    return False


def _worker_still_running(worker):
    """worker 是否还活着（PyQt 的 QThread 被 GC 后 isRunning 会失效，先判 None）。"""
    return worker is not None and worker.isRunning()


def do_restart_during_think(app, w):
    """B19 回归：AI 思考中途重开，必须不崩溃、状态干净、线程回收。

    延迟此刻只记录：Phase 1 的引擎不轮询 cancel，只能等 _cancel_ai 的 3 秒余量。
    """
    if not _enter_thinking_game(app, w, 3):   # 3 级 = 15s 预算，思考期足够长
        record("WARN", "重开测试", "未能构造出真正在搜索的局面，取消路径未被覆盖")
        return
    record("PASS", "重开测试：已构造出正在搜索的局面")

    worker = w.ai_worker
    t0 = time.monotonic()
    w._on_restart()
    dt = time.monotonic() - t0
    still = _worker_still_running(worker)

    check(w.ai_worker is None, "重开后 worker 引用已清空")
    check(not w.ai_thinking, "重开后 ai_thinking 已复位")
    check(dt <= CANCEL_SAFETY_S, "重开未挂死（安全性）", f"{dt:.2f}s")
    check(not still, "重开后搜索线程在 0.5s 内退出",
          f"{dt*1000:.0f}ms 内退出" if not still else
          f"等了 {dt*1000:.0f}ms 仍在运行 —— 协作取消没生效")

    # 重开后回到**模式选择页**（`_on_restart` 的落点）。这里直接调下一环，
    # 是因为要验的是"取消之后能不能干净地再开一局"，与模式页的按钮无关。
    w._on_color_selected(1)
    pump(40, app)
    w._on_difficulty_selected(1)
    pump(120, app)
    check(int(np.count_nonzero(w.board)) in (0, 1), "重开后的新局棋盘是干净的",
          f"{np.count_nonzero(w.board)} 子")


def do_quit_during_think(app, w):
    """B19 回归：思考中途退出，worker 必须被回收，不留下野线程。"""
    if not _enter_thinking_game(app, w, 3):
        record("WARN", "退出测试", "未能构造出真正在搜索的局面，取消路径未被覆盖")
        return

    worker = w.ai_worker
    t0 = time.monotonic()
    w._on_quit()
    dt = time.monotonic() - t0
    pump(80, app)

    # ai_worker 会被置 None，所以要在调用前抓住引用，否则这里的断言是空的
    still = _worker_still_running(worker)
    check(w.ai_worker is None, "退出后 worker 引用已清空")
    check(dt <= CANCEL_SAFETY_S, "退出未挂死", f"{dt:.2f}s")
    check(not still, "退出后无残留 AI 线程",
          "线程已回收" if not still else
          f"线程仍在运行（{dt*1000:.0f}ms 未退出），进程退出时可能触发 Qt 断言")


def do_local_battle(app):
    """本地双人对战：两人轮流下到五连，遮罩报的是"哪一方"赢。

    没有 AI 参与是**结构**上的：`_start_local_game` 之后 `playmode == 1`，
    `_on_board_click` 里的落子色由手数奇偶决定，AI 回合那一支整个不执行
    （见 `_on_board_click` 末尾）。这里就用"一个 AI worker 都没起过"来验它。
    """
    w = M.GomokuGame()
    w.show()
    pump(120, app)

    w._on_loading_finished()             # 加载页 -> 模式页
    pump(60, app)
    if not check(w.selection_mode is not None, "加载后进入模式选择页"):
        return
    check(w.selection_color is None, "模式页上没有同时建出颜色页")

    w._on_mode_selected(1)               # 1 = 本地对战
    pump(120, app)
    check(w.playmode == 1, "本地对战的 playmode=1")
    check(w.gamemode == 0, "本地对战固定黑先（gamemode=0）")
    check(w.gamemode == 0 and int(np.count_nonzero(w.board)) == 0,
          "本地对战开局不留 AI 先手子")
    check(w.ai_worker is None, "本地对战不起 AI 线程")

    # 黑下 9 行 14..18，白下 0 行 0..3 —— 白永远挡不到黑那条线。
    seq = [((9, 14), 1), ((0, 0), 2), ((9, 15), 1), ((0, 1), 2),
           ((9, 16), 1), ((0, 2), 2), ((9, 17), 1), ((0, 3), 2),
           ((9, 18), 1)]
    ok = True
    for i, ((r, c), stone) in enumerate(seq[:8]):
        # M4c 起落子经房间回显，点击后不能再立即断言 —— 等目标格出现该色。
        wait_until(lambda: not w._move_in_flight, 3, app)
        click_cell(w, r, c)
        landed, _ = wait_until(
            lambda r=r, c=c, s=stone: int(w.board[r][c]) == s, 3, app)
        if not check(landed,
                     f"本地第 {i+1} 手落在 ({r},{c}) 且为{'黑' if stone == 1 else '白'}子"):
            ok = False
            break
    if not ok:
        return

    # 悔棋：本地局一次退**一步**（人机局是退两步：玩家 + AI 回应）。必须
    # 在连五之前验 —— 终局后 `_on_undo` 直接返回（对局已结束）。
    w._on_undo()
    wait_until(lambda: w.move_count == 7, 3, app)
    pump(20, app)
    check(w.move_count == 7 and int(np.count_nonzero(w.board)) == 7,
          "本地对战悔棋退一步",
          f"move_count={w.move_count}")
    check(w.game_over is False, "悔棋之后对局回到进行中")
    # 退掉的是白子那一手 → 下一手仍是白。
    check(w.game_panel.turn_indicator.label.text().startswith("白棋"),
          "悔棋后轮到白方",
          w.game_panel.turn_indicator.label.text())
    r, c = seq[7][0]
    wait_until(lambda: not w._move_in_flight, 3, app)
    click_cell(w, r, c)
    wait_until(lambda r=r, c=c: int(w.board[r][c]) == 2, 3, app)
    check(int(w.board[r][c]) == 2,
          "悔棋后重下仍落在同一个点上且为白子")

    r, c = seq[8][0]
    wait_until(lambda: not w._move_in_flight, 3, app)
    click_cell(w, r, c)
    wait_until(lambda r=r, c=c: int(w.board[r][c]) == 1, 3, app)
    if not check(int(w.board[r][c]) == 1, "本地第 9 手为黑子"):
        return

    check(w.gamerule == 2 and w.winner == 1 and w.game_over,
          "本地对战五连后判黑方获胜",
          f"gamerule={w.gamerule} winner={w.winner}")
    check(w.ai_worker is None, "整局下来没有起过 AI 线程")

    pump(M.GAME_OVER_DELAY_MS + 200, app)     # 等终局遮罩的延迟投递
    ov = w.game_over_overlay
    if not check(ov is not None, "本地对战的终局遮罩已弹出"):
        return
    check(ov.result_text == "黑方获胜", "遮罩文案是「黑方获胜」",
          ov.result_text)
    check(not ov.can_review, "本地对战没有「算法复盘」按钮")
    labels = [b.text() for b in ov.findChildren(QPushButton)]
    check(all("算法复盘" not in t for t in labels),
          "遮罩上确实没有复盘按钮", str(labels))
    check(w.game_panel.turn_indicator.label.text() == "黑方获胜",
          "面板终局文案是「黑方获胜」",
          w.game_panel.turn_indicator.label.text())
    check(w.game_panel.engine_row._value.text() == "—",
          "本地对战的面板不显示引擎",
          w.game_panel.engine_row._value.text())

    w._cancel_ai()
    w._cancel_review(discard=True)


def _fake_review_record(seq, delta):
    """造一条复盘记录，只为建页面用（不经过引擎）。"""
    return {'seq': seq, 'played': (2, 3), 'best': (2, 3) if delta == 0 else (2, 5),
            'best_val': 0, 'played_val': -delta, 'delta': delta, 'mate': False,
            'offboard': False, 'empty': False, 'before': np.zeros((19, 19), dtype=int)}


def _scroll_check(app):
    """长列表要出现滑动条，且视口高度封顶。

    拿一份 40 条的合成列表直接建 ``ReviewScreen``：真实的这一局只有几手，
    装得下，压根不触发滚动 —— 用那一局去断言"有滑动条"是测不到的。
    """
    recs = [_fake_review_record(2 * i + 1, 0 if i % 3 else 100 * i)
            for i in range(40)]
    scr = M.ReviewScreen(recs, 1)
    scr.resize(900, 800)
    pump(60, app)
    area = _first_scroll_area(scr)
    if not check(area is not None, "长列表：结果页有记录列表"):
        return
    check(area.height() <= M.REVIEW_LIST_H,
          "长列表：视口高度封顶在 REVIEW_LIST_H",
          f"{area.height()}px（封顶 {M.REVIEW_LIST_H}px）")
    bar = area.verticalScrollBar()
    check(bar.maximum() > 0, "长列表：出现滑动条，能翻到最后一手",
          f"max={bar.maximum()}")
    check(_button_texts(scr).count("棋局显示") == len(recs),
          "长列表：每一行都有「棋局显示」",
          f"{_button_texts(scr).count('棋局显示')} 个 / {len(recs)} 行")
    scr.deleteLater()


def _has_text(widget, needle):
    """这棵控件树里有没有哪个 QLabel 写着 ``needle``。

    ``Screen`` 把标题/副标题交给 ``title_label`` / ``subtitle_label`` 建完
    就不留句柄了，所以只能按文字去找 —— 也正因此，断言的是"用户看得到的
    那句提示"，而不是某个内部属性的值。
    """
    return any(needle in lab.text()
               for lab in widget.findChildren(QLabel))


def _button_texts(widget):
    """这棵控件树里所有 ``QPushButton`` 的文字，按出现顺序。"""
    return [b.text() for b in widget.findChildren(QPushButton)]


def _first_scroll_area(widget):
    areas = widget.findChildren(QScrollArea)
    return areas[0] if areas else None


def _settings_btn(win):
    """窗口右上角那枚「设置」齿轮。

    **全局只有一个**，是主窗口自己的子控件、不在任何页面里 —— 页面怎么换它都
    钉在同一个位置。曾经是每页各挂一个（页面底部脚注 + 面板标题行），三种长相
    三种位置；现在按 tooltip 从窗口上认它，与 ``main.settings_button`` 同一个
    约定。
    """
    b = getattr(win, "settings_btn", None)
    if b is not None and b.toolTip() == "字号 / 主题":
        return b
    return None


def _gear_rect(w):
    """齿轮相对窗口的位置。"""
    b = _settings_btn(w)
    if b is None:
        return None
    return (b.x(), b.y(), b.width(), b.height())


def _title_rect(w):
    """当前页主标题在窗口坐标下的 (控件, 文字左端 x, 文字右端 x, 竖直中心)。"""
    import main as M
    page = w.central.currentWidget()
    if page is None:
        return None
    lbl = M._page_title_label(page)
    if lbl is None:
        return None
    tl = lbl.mapTo(w, QPoint(0, 0))
    tw = lbl.fontMetrics().horizontalAdvance(lbl.text())
    left = tl.x() + (lbl.width() - tw) // 2
    return (lbl.text(), left, left + tw, tl.y() + lbl.height() // 2)


def _gear_is_right_of_title(win):
    """齿轮就贴在主标题文字的右边，且与文字同一行。

    这是用户 2026-10-05 定下的位置：不钉窗口角（那里会压住面板边框、"遮挡
    GUI"），而是每页都紧挨着那页的主标题。
    """
    b = _settings_btn(win)
    t = _title_rect(win)
    if b is None or t is None:
        return False
    _, _, text_right, text_cy = t
    gap = b.x() - text_right
    if not (0 <= gap <= 24):
        return False
    return abs((b.y() + b.height() // 2) - text_cy) <= 8


def _card_texts(screen):
    """读出 ``SelectionScreen`` 每张卡片的主文案。"""
    out = []
    for btn in screen._cards:
        for lab in btn.findChildren(QLabel):
            if lab.property("role") == "card-text":
                out.append(lab.text())
    return out


def do_review(app):
    """战后算法复盘：入口只在对 AI 输棋时出现，强度受下限约束，能回到棋盘看。

    输棋那一步不靠"真下输"（1 档要下很久且不保证）：直接把 `gamerule` 置成
    输棋并弹遮罩 —— 这条路径与真实输棋**完全同一条**（`_show_game_over`），
    区别只是谁先发现输了。
    """
    # ---- 强度下限：只列不低于本局难度的档位 ----
    for min_level, want in ((1, 5), (3, 3), (5, 1)):
        s = M.SelectionScreen(mode="review", min_level=min_level)
        got = _card_texts(s)
        check(len(got) == want and len(s._cards) == want,
              f"复盘强度页 min_level={min_level} 只列 {want} 档",
              str(got))
        s.deleteLater()

    w = M.GomokuGame()
    w.show()
    pump(120, app)
    w._on_loading_finished()
    pump(40, app)
    w._on_mode_selected(0)               # 挑战 AI
    pump(40, app)
    check(w.selection_color is not None, "挑战AI 仍走原来的颜色页")
    w._on_color_selected(0)
    pump(40, app)
    w._on_difficulty_selected(1)         # 1 档，跑得快
    pump(120, app)

    # 下 4 手，攒出复盘素材
    for i in range(4):
        got_idle, _ = wait_until(lambda: not w.ai_thinking
                                 and not w._move_in_flight, 30, app)
        if not got_idle or w.game_over:
            break
        cell = pick_empty_near_center(w.board)
        if cell is None:
            break
        click_cell(w, *cell)
        # 等这一手回显（human_moves 在 move 事件里记录）再下下一手。
        wait_until(lambda n=i + 1: len(w.human_moves) >= n or w.game_over,
                   5, app)
    check(len(w.human_moves) >= 3, "对局中记下了玩家的每一步快照",
          f"{len(w.human_moves)} 手")
    for m in w.human_moves:
        n = int(np.count_nonzero(m['before']))
        if not check(n == m['seq'] - 1,
                     f"第 {m['seq']} 手的快照是落子**之前**的局面",
                     f"盘上 {n} 子"):
            break

    # 造一个"输给 AI"的终局
    w.gamerule = 1
    w.winner = 2
    w.game_over = True
    w._show_game_over()
    pump(30, app)
    ov = w.game_over_overlay
    labels = [b.text() for b in ov.findChildren(QPushButton)]
    check(ov.can_review and any("算法复盘" in t for t in labels),
          "输给 AI 后遮罩上有「算法复盘」按钮", str(labels))

    w._on_review_clicked()
    pump(80, app)
    check(w.game_widget is None, "进入复盘时对局 UI 已整棵拆掉")
    if not check(w.review_strength is not None, "进入复盘强度页"):
        return
    check("初级" in _card_texts(w.review_strength),
          "1 档输棋后可以选初级复盘", str(_card_texts(w.review_strength)))

    w._on_review_level_selected(1)
    # **不要 pump 再查 isRunning**：1 档复盘只要几十毫秒就可能算完，等
    # 60ms 再查会把"已结束"误报成"没启动"。start() 返回后立刻读状态。
    running = w.review_worker is not None and w.review_worker.isRunning()
    check(w.review_progress is not None, "进入复盘进度页")
    check(running, "复盘线程已启动")

    got, _ = wait_until(lambda: w.review_worker is None, 180, app)
    if not check(got, "复盘在时限内算完"):
        return
    check(w.review_list is not None, "复盘结果页已构建")
    check(isinstance(w.review_records, list), "复盘记录是列表",
          f"{len(w.review_records)} 条")
    check(w.game_panel is None, "复盘期间没有残留对局面板")

    # 复盘结果页同样要有设置入口，且从这儿进去、原路回来 —— 不换档，纯验证
    # "来回一趟不会把结果页顶掉"（换档重建那条由 do_settings 覆盖）。
    rgear = _settings_btn(w)
    if check(rgear is not None and rgear.isVisible(),
             "复盘结果页上齿轮可见"):
        rgear.click()
        pump(60, app)
        check(w.central.currentWidget() is w.settings_screen,
              "从复盘结果页进得了设置页")
        check(w._settings_origin is w.review_list,
              "设置页记住了来路是复盘结果页")
        w.settings_screen.back_clicked.emit()
        pump(60, app)
        check(w.central.currentWidget() is w.review_list,
              "返回回到复盘结果页")
        # 再来一趟：设置页是复用的，**第二趟不该再往栈里塞一页**。
        n_pages = w.central.count()
        rgear.click()
        pump(60, app)
        w.settings_screen.back_clicked.emit()
        pump(60, app)
        check(w.central.count() == n_pages,
              "来回两趟没有在栈里堆页面", f"{w.central.count()} 页")

    for rec in w.review_records:
        b, p = rec['best'], rec['played']
        if not check(rec['before'][p[0]][p[1]] == 0,
                     f"第 {rec['seq']} 手玩家落点 ({p[0]},{p[1]}) 在原局面里是空点"):
            break
        if b is None:
            # 没得比的那些（空盘起始手 / 搜索未跑完）：只要它自己交代得清楚
            # 就行，不该有坐标，也不该被算成"可改进"。
            if not check(rec['delta'] is None and not M._is_optimal(rec),
                         f"第 {rec['seq']} 手未评分（无最优点、Δ 为空）"):
                break
            continue
        # 最优点必须是**该局面上的空点**，否则圈出来的位置毫无意义。
        if not check(rec['before'][b[0]][b[1]] == 0,
                     f"第 {rec['seq']} 手的最优点 ({b[0]},{b[1]}) 在原局面里是空点"):
            break
        if not check(rec['delta'] is None or rec['delta'] >= 0,
                     f"第 {rec['seq']} 手的 Δ 非负（或无可比）",
                     str(rec['delta'])):
            break

    # 玩家执黑时第 1 手落在空盘上 —— 它没有"最优点"可比，但也**不能消失**，
    # 否则列表从第 3 手开始，正好是"棋谱断了"那个毛病。
    check(len(w.human_moves) >= 1
          and w.review_records[0]['seq'] == w.human_moves[0]['seq'],
          "列表从玩家第一手开始，没有断档",
          f"首条 seq={w.review_records[0]['seq'] if w.review_records else None} / "
          f"玩家首手 seq={w.human_moves[0]['seq'] if w.human_moves else None}")

    # **玩家下过的每一手都要在列表里**，走对的也在内 —— 只留错手的话列表
    # 就断了，回头看"我第 9 手下的哪儿"会找不到。
    check(len(w.review_records) == len(w.human_moves),
          "每一手玩家着法都在列表里（含走对的）",
          f"{len(w.review_records)} 条 / 玩家落了 {len(w.human_moves)} 手")
    opt = [r for r in w.review_records if M._is_optimal(r)]
    bad = [r for r in w.review_records if not M._is_optimal(r)]
    check(bool(opt) and bool(bad),
          "这一局里走对的与走错的两类都有（否则下面几条断言测不到东西）",
          f"最优 {len(opt)} 手 / 可改进 {len(bad)} 手")

    # 列表必须能滚动 —— 复盘动辄几十手，没有滚动条就等于后半盘看不见。
    # **本局只有 4 手，装得下，当然不出现滚动条** —— 所以这条不能用真实
    # 这一局来测，得拿一份长列表直接建页（见 ``_scroll_check``）。
    _scroll_check(app)

    # 每一行都得有「棋局显示」，包括走对的那几行。
    check(_button_texts(w.review_list).count("棋局显示") == len(w.review_records),
          "每一行（含最优行）都有「棋局显示」按钮",
          f"{_button_texts(w.review_list).count('棋局显示')} 个 / {len(w.review_records)} 行")

    # 走对的那一行文案是「最优」，不该再写一遍同样的坐标。
    optimal_lines = [lab.text() for lab in w.review_list.findChildren(QLabel)
                     if lab.property("tone") == "win"]
    check(optimal_lines and all(l.endswith("最优") for l in optimal_lines)
          and all("→ 最优点" not in l for l in optimal_lines),
          "最优行的文案是「最优」，不重复坐标",
          str(optimal_lines[:3]))

    rated = [i for i, r in enumerate(w.review_records) if M._is_rated(r)]
    if not rated:
        record("WARN", "复盘棋盘", "本局没有可评分的着法，跳过棋盘检查")
    else:
        # 挑一条**有最优点**的（空盘起始手没有），棋盘上才画得出那个圈。
        w._on_review_record_selected(rated[0])
        pump(60, app)
        if check(w.review_board is not None, "点「棋局显示」后进入复盘棋盘"):
            rb = w.review_board
            bw = rb.board_widget
            rec = w.review_records[rated[0]]
            check(bw is not None and bw.review_marker is not None,
                  "复盘棋盘上设置了标记",
                  str(getattr(bw, "review_marker", None)))
            check(tuple(bw.review_marker[0]) == tuple(rec['best'])
                  and tuple(bw.review_marker[1]) == tuple(rec['played']),
                  "标记指向该手的最优点与玩家落点")
            check(int(np.count_nonzero(bw.board)) == rec['seq'] - 1,
                  "复盘棋盘摆的是该手**之前**的局面",
                  f"{np.count_nonzero(bw.board)} 子")
            check(bw.board[rec['played'][0]][rec['played'][1]] == 0,
                  "玩家那一手是幽灵子，没有真的落到盘上")
            # 回归：复盘棋盘曾经 setFixedSize(726,726)，窗口矮一点就被顶出
            # 屏幕，底部的「返回复盘」用户够不着、也滚不到。现在它按可见区域
            # 现算边长（见 `ReviewBoardScreen._fit_board_side`）。
            check(bw.height() <= w.height(),
                  "复盘棋盘放得进窗口（不再被顶出屏幕）",
                  f"棋盘 {bw.height()}px / 窗口 {w.height()}px")
            # 只读：基类的 mousePressEvent 什么都不做，点击不该改棋盘
            before = bw.board.copy()
            QTest.mouseClick(bw, Qt.LeftButton,
                             pos=bw.cell_center(*rec['best']))
            pump(20, app)
            check((bw.board == before).all(), "复盘棋盘是只读的（点击不落子）")

            w._back_to_review_list()
            pump(40, app)
            check(w.central.currentWidget() is w.review_list,
                  "「返回复盘」回到结果页")
            check(w.central.count() == 4,
                  "返回没有把结果页重复入栈（强度/进度/结果/棋盘，共 4 页）",
                  f"栈内 {w.central.count()} 页")

    # 走对的那一手同样要能看棋局 —— 用户点名的"不管是不是最优解"。
    opt_idx = [i for i, r in enumerate(w.review_records) if M._is_optimal(r)]
    if opt_idx:
        n_before = w.central.count()
        w._on_review_record_selected(opt_idx[0])
        pump(60, app)
        if check(w.review_board is not None, "最优行也能点开「棋局显示」"):
            r2 = w.review_records[opt_idx[0]]
            bw2 = w.review_board.board_widget
            check(tuple(bw2.review_marker[0]) == tuple(r2['best'])
                  == tuple(r2['played']),
                  "最优行的标记是同一个点（你下的就是该下的）",
                  str(bw2.review_marker))
            check(w.central.count() == n_before,
                  "换一手看棋盘不会把上一张留在栈里",
                  f"{n_before} → {w.central.count()} 页")
            w._back_to_review_list()
            pump(40, app)

    # ---- 结果页的出口 ----
    # 复盘是一条有终点的路径：看完最后一条必须能落下去。结果页只有"棋局
    # 显示"，没有出口的话用户就被困在这一页了。
    btns = _button_texts(w.review_list)
    check("🏠 结束复盘" in btns and "✕ 退出游戏" in btns,
          "复盘结果页有「结束复盘」与「退出游戏」", str(btns))

    area = _first_scroll_area(w.review_list)
    if check(area is not None, "复盘结果页有记录列表"):
        h = area.height()
        check(h < M.REVIEW_LIST_H, "记录少时列表按内容收缩，不留大片空框",
              f"{h}px（封顶 {M.REVIEW_LIST_H}px）")

    w._cancel_review(discard=True)
    check(w.review_worker is None, "收尾后复盘线程引用已清空")


def do_review_finish(app):
    """「结束复盘」= 回到模式选择页（与终局遮罩的「再来一局」同一个去处）。"""
    w = M.GomokuGame()
    w.show()
    pump(120, app)
    w._on_loading_finished()
    pump(40, app)
    w._on_mode_selected(0)
    pump(40, app)
    w._on_color_selected(0)
    pump(40, app)
    w._on_difficulty_selected(1)
    pump(120, app)

    got, _ = wait_until(lambda: not w.ai_thinking
                        and not w._move_in_flight, 30, app)
    if not got or w.game_over:
        check(False, "结束复盘用例：先把局面走到能复盘")
        return
    click_cell(w, *pick_empty_near_center(w.board))
    wait_until(lambda: len(w.human_moves) >= 1 or w.game_over, 5, app)

    w.gamerule = 1
    w.winner = 2
    w.game_over = True
    w._show_game_over()
    pump(30, app)
    w._on_review_clicked()
    pump(60, app)
    w._on_review_level_selected(1)
    got, _ = wait_until(lambda: w.review_list is not None, 40, app)
    if not check(got, "结束复盘用例：复盘结果页已出现"):
        return

    # 直接发信号，等价于点那颗按钮 —— 不必去猜按钮在屏幕上的坐标。
    w.review_list.finish_clicked.emit()
    pump(60, app)
    check(w.selection_mode is not None,
          "「结束复盘」回到了模式选择页")
    check(w.review_list is None, "回到模式页后复盘结果页已拆掉")
    check(w.central.currentWidget() is w.selection_mode,
          "当前页就是模式选择页")

    w._cancel_review(discard=True)


def do_review_cancel(app):
    """复盘中途取消：进度页那颗按钮必须**立刻**收工，并展示已经算完的部分。

    取 3 档（每手 15 秒）就是为了让取消有东西可取消 —— 1 档常常几毫秒就返回，
    "取消"根本来不及按下，那种情况下这里测的其实是空路径。
    """
    w = M.GomokuGame()
    w.show()
    pump(120, app)
    w._on_loading_finished()
    pump(40, app)
    w._on_mode_selected(0)
    pump(40, app)
    w._on_color_selected(0)
    pump(40, app)
    w._on_difficulty_selected(1)
    pump(120, app)

    for i in range(2):
        got_idle, _ = wait_until(lambda: not w.ai_thinking
                                 and not w._move_in_flight, 30, app)
        if not got_idle or w.game_over:
            break
        cell = pick_empty_near_center(w.board)
        if cell is None:
            break
        click_cell(w, *cell)
        wait_until(lambda n=i + 1: len(w.human_moves) >= n or w.game_over,
                   5, app)
    if not check(len(w.human_moves) >= 2, "取消用例：攒到了复盘素材",
                 f"{len(w.human_moves)} 手"):
        return

    w.gamerule = 1
    w.winner = 2
    w.game_over = True
    w._show_game_over()
    pump(30, app)
    w._on_review_clicked()
    pump(60, app)
    w._on_review_level_selected(3)       # 3 档 = 每手 7 秒，够把取消按下去
    pump(60, app)

    pump(1_500, app)                     # 让第一手真的进到搜索里
    check(w.review_progress is not None
          and w.review_progress.progress_bar.value() >= 0,
          "进度条可读", f"value={w.review_progress.progress_bar.value()}")

    t0 = time.monotonic()
    w._cancel_review()
    dt = time.monotonic() - t0
    check(dt <= CANCEL_SAFETY_S, "取消复盘未挂死", f"{dt:.2f}s")

    got, _ = wait_until(lambda: w.review_list is not None, 20, app)
    if not check(got, "取消后照常走到结果页（展示已算完的部分）"):
        return
    check(_has_text(w.review_list, "已取消"), "结果页标明了这次是取消")
    check(w.review_worker is None, "取消后复盘线程引用已清空")

    w._cancel_review(discard=True)


def do_settings(app):
    """设置：**每一页都有入口**、从哪儿进就回哪儿、换档不丢局面。

    只在关掉自动适配（``GOMOKU_AI_UI_AUTOFIT=0``，本文件开头已设）的前提下
    跑 —— 否则离屏的 800×600 会让档位初始值取决于平台，断言不稳定。
    """
    s = QSettings(theme._SETTINGS_ORG, theme._SETTINGS_APP)
    saved_scale = s.value(theme._SETTINGS_SCALE_KEY, None)
    small_name = theme.available_scales()[0]

    w = M.GomokuGame()
    w.show()
    pump(120, app)
    # ---- 0. 加载页：**没有**设置入口 ----
    # 用户 2026-10-05：「他妈的加载界面你放个设置干啥」。这条必须在
    # `_on_loading_finished()` 之前断言 —— 那之后加载页就下线了。
    check(_settings_btn(w) is not None and not _settings_btn(w).isVisible(),
          "加载页上没有设置入口")
    w._on_loading_finished()
    pump(40, app)
    if not check(w.selection_mode is not None, "设置用例：进入模式选择页"):
        return

    # ---- 1. 选择页：右上角齿轮 ----
    gear = _settings_btn(w)
    if not check(gear is not None and gear.isVisible(),
                 "模式选择页上齿轮可见"):
        return
    check(_gear_is_right_of_title(w),
          "齿轮紧贴在当前页主标题文字的右边",
          f"{_gear_rect(w)} / 标题 {_title_rect(w)}")
    gear.click()
    pump(60, app)
    scr = w.settings_screen
    if not check(scr is not None, "「⚙ 设置」进入设置页"):
        return
    check(w.central.currentWidget() is scr, "当前页就是设置页")

    texts = _button_texts(scr)
    labels = [theme.scale_label(n) for n in theme.available_scales()]
    check(all(t in texts for t in labels),
          "设置页列出全部字号档位", str([t for t in texts if t in labels]))

    checked = [b.text() for b in scr.findChildren(QPushButton) if b.isChecked()]
    check(checked == [theme.scale_label(theme.current_scale())],
          "当前档位是唯一被勾选的那个", str(checked))

    # 点「小」：字号变、QSettings 落盘、本页重建
    before_md = theme.SIZE_MD
    scr.scale_selected.emit(small_name)
    pump(60, app)
    check(theme.current_scale() == small_name,
          "点档位后 theme 的档位变了", theme.current_scale())
    check(theme.SIZE_MD != before_md,
          "点档位后字号真的变了", f"{before_md} → {theme.SIZE_MD}")
    check(s.value(theme._SETTINGS_SCALE_KEY) == small_name,
          "档位写穿到 QSettings（下次打开还是这一档）",
          str(s.value(theme._SETTINGS_SCALE_KEY)))
    check(w.settings_screen is not scr,
          "换档后设置页重建了（旧页的布局已经不适用）")
    check(w.central.currentWidget() is w.settings_screen,
          "重建后当前页仍是设置页")

    # 返回：**回进来时那一页**。设置页是借道的，走的时候要把它从栈里摘掉，
    # 否则来回进几次就会让栈一直带着这一页。
    n_before = w.central.count()          # 含这一趟的设置页
    w.settings_screen.back_clicked.emit()
    pump(60, app)
    check(w.central.currentWidget() is w.selection_mode,
          "「← 返回」回到进来时的那一页（模式选择页）")
    check(w.central.count() == n_before - 1,
          "离开设置页后它没有赖在栈里", f"{w.central.count()} 页")

    # ---- 2. 对局页：面板标题行的紧凑入口，换档后局面/用时都不丢 ----
    theme.set_scale("normal", persist=False)
    w.selection_mode.mode_selected.emit(0)          # 挑战 AI
    pump(40, app)
    w.selection_color.color_selected.emit(0)        # 执黑
    pump(40, app)
    if not check(w.selection_difficulty is not None, "设置用例：进入难度页"):
        return
    check(_settings_btn(w) is not None and _settings_btn(w).isVisible(),
          "难度页上齿轮可见")
    check(_gear_is_right_of_title(w),
          "难度页上齿轮同样贴在标题文字右边",
          f"{_gear_rect(w)} / 标题 {_title_rect(w)}")
    w.selection_difficulty.difficulty_selected.emit(1)
    pump(150, app)
    if not check(w.game_widget is not None, "设置用例：进入对局页"):
        return

    do_player_moves(app, w, 2)
    pump(80, app)
    stones_before = int((w.board != 0).sum())
    moves_before = w.move_count
    # 把用时**摆到一个非零值**再换档：真实对局里它几乎不可能是 0，而 0 会让
    # "用时没被清零"这条断言恒真 —— 那样就测不到 `_build_game_ui` 里
    # `reset_timer()` 把表打回 00:00 这件事。
    w.game_panel._elapsed = 137
    w.game_panel.time_row.set_value("02:17")
    elapsed_before = w.game_panel._elapsed
    check(w.settings_btn.isVisible(), "对局页上齿轮可见")
    check(_gear_is_right_of_title(w),
          "对局页上齿轮贴在「五子棋 AI」右边",
          f"{_gear_rect(w)} / 标题 {_title_rect(w)}")

    game_before = w.game_widget
    w.settings_btn.click()
    pump(60, app)
    check(w.central.currentWidget() is w.settings_screen,
          "对局中点「⚙」进入了设置页")
    w.settings_screen.scale_selected.emit(small_name)
    pump(60, app)
    w.settings_screen.back_clicked.emit()
    pump(120, app)
    check(w.central.currentWidget() is w.game_widget,
          "从对局页进设置，返回还在对局页（没有被踢回首页）")
    check(w.game_widget is not game_before,
          "换档后对局页按新档位重建了（面板宽度等常量不会自己变）")
    check(int((w.board != 0).sum()) == stones_before,
          "换档重建后盘上子数不变",
          f"{stones_before} → {int((w.board != 0).sum())}")
    check(w.move_count == moves_before, "换档重建后手数不变", str(w.move_count))
    check(w.game_panel._elapsed >= elapsed_before,
          "换档重建后用时没有被清零",
          f"{elapsed_before}s → {w.game_panel._elapsed}s")
    txt = w.game_panel.time_row._value.text()
    check(txt == "%02d:%02d" % divmod(w.game_panel._elapsed, 60),
          "换档重建后面板的计时读数跟着走",
          f"{txt} vs {elapsed_before}s 之前")

    # 复原：别的用例还要按正常档位量尺寸，用户的配置也不该被冒烟测试改掉
    # （上面那条 persist=True 是**故意**的 —— 要验证它真的写穿了）。
    theme.set_scale("normal", persist=False)
    if saved_scale is None:
        s.remove(theme._SETTINGS_SCALE_KEY)
    else:
        s.setValue(theme._SETTINGS_SCALE_KEY, saved_scale)
    w._cancel_ai()
    w.settings_screen = None
    w.close()
    w.deleteLater()
    pump(30, app)


def do_chinese_path(app):
    """B22 回归：路径含非 ASCII 字符时 Qt 插件目录仍能被推导出来。"""
    saved = os.environ.pop("QT_QPA_PLATFORM_PLUGIN_PATH", None)
    try:
        M._fix_qt_plugin_path()
        got = os.environ.get("QT_QPA_PLATFORM_PLUGIN_PATH")
        check(bool(got) and os.path.isdir(got),
              "中文路径下 Qt 插件目录可推导", got or "（空）")
    finally:
        if saved is not None:
            os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = saved


# ------------------------------------------------------------------ 主流程

def main():
    ap = argparse.ArgumentParser(description="无头 GUI 冒烟测试")
    ap.add_argument("--keep", action="store_true", help="保留临时日志目录")
    args = ap.parse_args()

    M._fix_qt_plugin_path()
    app = QApplication(sys.argv[:1])
    # 不继承开发者机器上存着的档位：下面的断言全按设计尺寸写。设置页那条用例
    # 自己会换档，换完也会复原。
    theme.set_scale("normal", persist=False)
    app.setStyle("Fusion")

    log_dir = tempfile.mkdtemp(prefix="gomoku_smoke_")

    # 只换日志落盘目录，记录逻辑本身走真实代码
    real_logger = M.GameLogger

    class _TempLogger(real_logger):
        def __init__(self, log_dir_=None):
            super().__init__(log_dir=log_dir)

    M.GameLogger = _TempLogger

    print(f"platform={os.environ.get('QT_QPA_PLATFORM')}  日志目录={log_dir}")
    print("-" * 72)

    w = None
    try:
        w = do_startup(app)
        do_player_moves(app, w, 3)
        do_charts(app, w)
        do_theme(app, w)
        do_disabled_state(app, w)
        do_undo(app, w)
        do_restart_during_think(app, w)
        do_quit_during_think(app, w)
        do_local_battle(app)
        do_settings(app)
        do_review(app)
        do_review_finish(app)
        do_review_cancel(app)
        do_chinese_path(app)
    except Exception:
        record("FAIL", "冒烟测试异常中止", traceback.format_exc().splitlines()[-1])
        traceback.print_exc()
    finally:
        M.GameLogger = real_logger
        try:
            if w is not None and w.ai_worker is not None:
                w._cancel_ai()
        except Exception:
            pass
        pump(80, app)

    print("-" * 72)
    n_fail = sum(1 for r in _RESULTS if r[0] == "FAIL")
    n_warn = sum(1 for r in _RESULTS if r[0] == "WARN")
    n_pass = sum(1 for r in _RESULTS if r[0] == "PASS")
    print(f"通过 {n_pass}  警告 {n_warn}  失败 {n_fail}")

    logs = sorted(os.listdir(log_dir))
    if logs:
        print(f"产生的日志（{len(logs)} 个）: {logs[:3]}{' ...' if len(logs) > 3 else ''}")
    if args.keep:
        print(f"保留日志目录: {log_dir}")
    else:
        shutil.rmtree(log_dir, ignore_errors=True)

    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())

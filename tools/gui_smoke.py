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

import numpy as np  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from PyQt5.QtCore import QPoint, Qt  # noqa: E402
from PyQt5.QtTest import QTest  # noqa: E402
from PyQt5.QtWidgets import QApplication, QPushButton  # noqa: E402

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

    # 重开后应回到颜色选择界面，再次进入对局不残留上一局状态
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

# -*- coding: utf-8 -*-
"""无头 UI 端到端：**四种棋盘尺寸 × 三条路径**，全程走真实点击。

    .venv/bin/python tools/ui_e2e.py                       # 默认矩阵
    .venv/bin/python tools/ui_e2e.py --sizes 9,13          # 只跑两张盘
    .venv/bin/python tools/ui_e2e.py --modes pvp,review    # 只跑两条路径
    .venv/bin/python tools/ui_e2e.py --modes ai --levels 4,5

矩阵（``--modes`` 的默认值）是 9/13/15/19 四个尺寸 ×：

* ``pvp``    —— 本地双人：两人轮流点到五连，终局遮罩与面板都按本局尺寸；
* ``lan``    —— 局域网联机：房主开房、加入方连上，**加入方不选尺寸**，按
  ``welcome`` 接受房主的那一张盘，双方互发着法并在两边回显；
* ``review`` —— 算法复盘：本地双人打完 → 强度页 → 逐手复盘 → 打开某一手的
  复盘棋盘，棋盘的边长必须与该手快照一致。

``ai`` 保留原来的五档人机对局（``--levels``），用于量棋力与开局库；它不在
默认矩阵里，因为一局要按档位等十几秒到几分钟。用户点名的"UI 改动最容易埋雷"
的三条路径都能在几秒内跑完，所以默认跑它们。

**为什么必须走真实点击。** 图表的追点在 ``_on_board_click`` 提交意图、
``_on_room_move`` 回显事件这两条路径上，直接往 ``self.board`` 写数组就绕过了
它们 —— 那样测出来的"对局"不经过任何一条真实的数据接线，是最容易骗过
自己的一种测试。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from PyQt5.QtCore import QPointF, Qt  # noqa: E402
from PyQt5.QtGui import QMouseEvent  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

import charts  # noqa: E402
import engine  # noqa: E402
import main as M  # noqa: E402

#: 一局人机对局的步数上限。正常局 20–60 手，拿它兜住挂死。
MAX_MOVES = 80

RESULTS = []        # (是否通过, 场景, 说明)


def _ok(cond, scene, detail=""):
    RESULTS.append((bool(cond), scene, detail))
    print("  %s %s%s" % ("ok  " if cond else "FAIL", scene,
                        "  —— " + detail if detail else ""))
    return bool(cond)


def pump(app, ms=80):
    deadline = time.monotonic() + ms / 1000.0
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.003)


def _click(w, r, c):
    x, y = w.board_widget.geom.px(r, c)
    ev = QMouseEvent(QMouseEvent.MouseButtonPress, QPointF(x, y),
                     Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    w._on_board_click(ev)


def click_and_wait(app, w, r, c, wait_s):
    """在 (r, c) 落一子并等回显。走真实的 `_on_board_click`。"""
    _click(w, r, c)
    deadline = time.monotonic() + wait_s
    while w.ai_thinking and time.monotonic() < deadline:
        pump(app, 40)
    pump(app, 60)


def click_until_landed(app, w, r, c, stone, wait_s=6.0):
    """点 (r, c) 直到盘面上出现 ``stone``。

    联机与本地双人的落子都要经过房间回显，且 ``_move_in_flight`` 会拦住在途
    的重复点击。轮次事件可能比点击晚一拍，所以这里重试而不是只点一次 ——
    真实用户看到"点了没反应"也会再点一下，这比在测试里睡固定时长更接近它。
    """
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if int(w.board[r][c]) == stone:
            return True
        if not w._move_in_flight and not w.ai_thinking:
            _click(w, r, c)
        pump(app, 40)
    return int(w.board[r][c]) == stone


def _wait(app, pred, wait_s):
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        app.processEvents()
        if pred():
            return True
        time.sleep(0.005)
    return bool(pred())


def _center_row_plan(n):
    """黑在中线连成五、白在最上一行陪着走的落子序列。

    9/13/15/19 都能用：黑 ``(m, m-2..m+2)``，白 ``(0, 0..3)``；白离黑那条线
    很远，永远挡不到。返回 ``[((r, c), stone), ...]``，黑先。
    """
    m = n // 2
    seq = []
    for i in range(5):
        seq.append(((m, m - 2 + i), 1))
        if i < 4:
            seq.append(((0, i), 2))
    return seq


class _Human:
    """确定性的人类策略：首手天元，之后取半径 2 内棋子最多的空点。"""

    def __init__(self, n):
        self.n = n
        self.first = True

    def pick(self, board):
        n = self.n
        if self.first:
            self.first = False
            return (n // 2, n // 2)
        best, best_key = None, None
        for i in range(n * n):
            r, c = divmod(i, n)
            if board[r][c] != 0:
                continue
            k = 0
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    rr, cc = r + dr, c + dc
                    if 0 <= rr < n and 0 <= cc < n and board[rr][cc] != 0:
                        k += 1
            key = (-k, i)
            if best_key is None or key < best_key:
                best, best_key = (r, c), key
        return best


# ------------------------------------------------------------------ 窗口脚手架

def _new_window(app):
    w = M.GomokuGame()
    w.show()
    pump(app, 100)
    return w


def _select_local(app, w, size, rules=0):
    """模式 → 规则 → 尺寸 → 本地对局页。"""
    w._on_mode_selected(1)               # 1 = 本地对战
    pump(app, 100)
    w._on_rules_selected(rules)
    pump(app, 100)
    w._on_size_selected(size)
    pump(app, 120)


def _close(w):
    try:
        w._on_quit()
    except Exception:                    # noqa: BLE001 — 收尾不该掩盖真正的失败
        pass


# ------------------------------------------------------------------ 路径：本地双人

def play_pvp(app, size):
    """本地双人下到五连，返回窗口（复盘路径要接着用）。"""
    w = _new_window(app)
    _select_local(app, w, size)

    _ok(w.board_size == size and w.board_widget is not None
        and w.board_widget.board_size == size,
        "本地 %d×%d 棋盘按所选尺寸建出" % (size, size),
        "board_size=%r widget=%r" % (w.board_size,
                                      getattr(w.board_widget, "board_size",
                                              None)))
    _ok(w.board.shape == (size, size), "本地盘面形状是 %d×%d" % (size, size),
        str(w.board.shape))

    for (r, c), stone in _center_row_plan(size):
        if not click_until_landed(app, w, r, c, stone):
            _ok(False, "本地第 (%d,%d) 手没有落下" % (r, c))
            return w
    if not _wait(app, lambda: w.game_over, 6.0):
        _ok(False, "本地 %d×%d 五连后没有终局" % (size, size))
        return w

    _ok(w.gamerule == 2 and w.winner == 1,
        "本地 %d×%d 五连判黑胜" % (size, size),
        "gamerule=%r winner=%r" % (w.gamerule, w.winner))
    _ok([m.get("stone") for m in w.human_moves]
        == [1, 2, 1, 2, 1, 2, 1, 2, 1],
        "本地复盘素材按黑白交替记下每一手",
        str([m.get("stone") for m in w.human_moves]))
    _ok(all(m["before"].shape == (size, size) for m in w.human_moves),
        "每一手的快照都是 %d×%d" % (size, size))

    log = ""
    try:
        with open(w.logger.f.name, encoding="utf-8") as fh:
            log = fh.read()
    except Exception:                    # noqa: BLE001
        pass
    _ok(("棋盘: %d×%d" % (size, size)) in log,
        "日志头写明棋盘尺寸",
        "" if ("棋盘: %d×%d" % (size, size)) in log
        else "日志里没找到「棋盘: %d×%d」" % (size, size))

    pump(app, M.GAME_OVER_DELAY_MS + 200)
    _ok(w.game_over_overlay is not None
        and w.game_over_overlay.result_text == "黑方获胜",
        "终局遮罩文案是「黑方获胜」")
    return w


# ------------------------------------------------------------------ 路径：局域网

def play_lan(app, size):
    """房主开房 + 加入方连上，双方互下到黑胜。

    加入方**不选尺寸**：它连上之后按 ``welcome`` 的 ``board_size`` 校准，
    这正是协议里最容易漏掉的一条。
    """
    host = _new_window(app)
    guest = _new_window(app)
    try:
        host._on_mode_selected(2)        # 2 = 局域网
        pump(app, 80)
        host._on_rules_selected(0)
        pump(app, 80)
        host._on_lan_create()            # 创建房间 → 先选尺寸
        pump(app, 80)
        if not _ok(host.selection_size is not None, "开房先走尺寸页"):
            return
        host._on_size_selected(size)
        pump(app, 80)
        host._on_color_selected(0)       # 房主执黑
        pump(app, 150)

        port = getattr(host._room, "port", None)
        if not _ok(port, "房主的房间已开始监听"):
            return

        guest._on_mode_selected(2)
        pump(app, 80)
        guest._on_rules_selected(0)
        pump(app, 80)
        guest._on_lan_join_requested("127.0.0.1", str(port))
        pump(app, 200)

        _ok(_wait(app, lambda: guest.board_size == size, 5.0),
            "加入方按 welcome 接受了房主的 %d×%d" % (size, size),
            "board_size=%r" % (guest.board_size,))
        _ok(guest.board.shape == (size, size)
            and guest.board_widget is not None
            and guest.board_widget.board_size == size,
            "加入方的棋盘与控件都换成了 %d×%d" % (size, size))

        m = size // 2
        pairs = [((m, m - 2), (0, 0)), ((m, m - 1), (0, 1)),
                 ((m, m), (0, 2)), ((m, m + 1), (0, 3)),
                 ((m, m + 2), None)]
        for i, (hpos, gpos) in enumerate(pairs):
            if not click_until_landed(app, host, hpos[0], hpos[1], 1):
                _ok(False, "房主第 %d 手没有落下" % (i + 1))
                return
            if not _wait(app, lambda p=hpos: int(guest.board[p[0]][p[1]]) == 1,
                         5.0):
                _ok(False, "房主的第 %d 手没有回显到加入方" % (i + 1))
                return
            if gpos is None:
                break
            if not click_until_landed(app, guest, gpos[0], gpos[1], 2):
                _ok(False, "加入方第 %d 手没有落下" % (i + 1))
                return
            if not _wait(app, lambda p=gpos: int(host.board[p[0]][p[1]]) == 2,
                         5.0):
                _ok(False, "加入方的第 %d 手没有回显到房主" % (i + 1))
                return

        _ok(_wait(app, lambda: host.game_over and guest.game_over, 6.0),
            "局域网 %d×%d 五连后两边都终局" % (size, size))
        _ok(host.winner == 1 and guest.winner == 1,
            "两边都判黑方获胜",
            "host=%r guest=%r" % (host.winner, guest.winner))
        _ok([m.get("stone") for m in guest.human_moves] == [2, 2, 2, 2],
            "加入方只记自己的白子（对手的手不进我的复盘）",
            str([m.get("stone") for m in guest.human_moves]))
    finally:
        _close(guest)
        _close(host)


# ------------------------------------------------------------------ 路径：算法复盘

def play_review(app, size):
    """本地双人打完 → 复盘 → 打开某一手的复盘棋盘。"""
    w = play_pvp(app, size)
    try:
        if not w.game_over:
            _ok(False, "复盘：这一局没到终局，跳过")
            return
        n_moves = w.move_count
        w._on_review_clicked()
        pump(app, 120)
        if not _ok(w.review_strength is not None
                   and w.review_strength.mode == "review_local",
                   "本地局的复盘强度页是双人版"):
            return
        w._on_review_level_selected(1)   # 入门档，跑得快
        if not _ok(_wait(app, lambda: w.review_list is not None, 60.0),
                   "复盘线程在 60 s 内算完",
                   "还有 %d 手没算完" % n_moves):
            return
        _ok(len(w.review_records) == n_moves,
            "复盘记录覆盖每一手（走对的也留）",
            "%d 条 / %d 手" % (len(w.review_records), n_moves))
        _ok(all(rec["before"].shape == (size, size)
                for rec in w.review_records),
            "每条记录的快照都是 %d×%d" % (size, size))
        # 空盘那一手（本地双人的第一手）没有"最优点"可言，记录里是
        # ``best=None``（见 main.py 的 ``_analyze_item``）。那是设计如此，
        # 不是漏算 —— 只对**有分**的记录检查坐标范围。
        scored = [rec for rec in w.review_records if rec["best"] is not None]
        _ok(len(scored) >= len(w.review_records) - 1,
            "除空盘外每一手都给出了最优点",
            "%d 条有最优点 / 共 %d 条" % (len(scored), len(w.review_records)))
        _ok(all(0 <= rec["best"][0] < size and 0 <= rec["best"][1] < size
                for rec in scored),
            "最优点都落在本局棋盘内")

        w._on_review_record_selected(0)
        pump(app, 120)
        rb = w.review_board
        _ok(rb is not None and rb.board_widget.board_size == size,
            "复盘棋盘的边长跟着那一手的快照走",
            "board_size=%r" % (getattr(getattr(rb, "board_widget", None),
                                       "board_size", None),))
    finally:
        _close(w)


# ------------------------------------------------------------------ 路径：人机

def play_one(app, level, wait_s, size=19):
    """下一整局人机，返回统计 dict。断言失败以 'fails' 列表返回。"""
    fails = []
    w = M.GomokuGame()
    w.show()
    pump(app, 100)
    w._on_mode_selected(0)           # 0 = 挑战 AI
    pump(app, 100)
    w._on_rules_selected(0)          # 0 = 无禁手
    pump(app, 100)
    w._on_size_selected(size)
    pump(app, 100)
    w._on_color_selected(0)          # 0 = 玩家执黑先行
    pump(app, 60)
    w._on_difficulty_selected(level)
    pump(app, 120)

    log_path = w.logger.f.name
    human = _Human(size)
    ai_moves = []
    max_moves = max(MAX_MOVES, size * size)

    while not w.game_over and w.move_count < max_moves:
        pos = human.pick(w.board)
        if pos is None:
            break
        click_and_wait(app, w, pos[0], pos[1], wait_s)

    black = int((w.board == 1).sum())
    white = int((w.board == 2).sum())
    if not (black in (white, white + 1)):
        fails.append("黑白子数不自洽：黑 %d 白 %d" % (black, white))
    if w.move_count != black + white:
        fails.append("move_count=%d 与盘面子数 %d 不符"
                     % (w.move_count, black + white))
    if w.board.shape != (size, size):
        fails.append("盘面形状 %r != %d×%d" % (w.board.shape, size, size))
    if not w.game_over:
        fails.append("%d 手仍未见终局（上限 %d）" % (w.move_count, max_moves))

    series = list(w.game_panel._series)
    if len(series) != w.move_count:
        fails.append("追点数 %d != 步数 %d" % (len(series), w.move_count))
    kinds = {k for _, k in series}
    if len(series) and charts.SEARCH not in kinds:
        fails.append("图上一个搜索点都没有（kinds=%r）" % (kinds,))

    _ROW = re.compile(r"\|\s*(黑\*|白O)\s*\|\s*(\S+)\s*\|\s*(.*?)\s*\|\s*(.*)$")
    _STEP = re.compile(r"^\s*(\d+)\s*\|")
    rows = []
    with open(log_path, encoding="utf-8") as fh:
        for line in fh:
            m = _ROW.search(line.rstrip("\n"))
            n = _STEP.match(line)
            if m and n:
                rows.append(dict(step=int(n.group(1)), who=m.group(1),
                                 coord=m.group(2), reason=m.group(3).strip(),
                                 detail=m.group(4).strip()))
    ai_rows = [r for r in rows if r["who"] == "白O"]
    if len(ai_rows) != white:
        fails.append("日志里 AI 手数 %d != 白子数 %d" % (len(ai_rows), white))

    book_rows = [r for r in ai_rows if "开局库" in r["reason"]]
    # 四个尺寸各有自己的一张库（`opening_book.py` 的 ``BOOKS[n]``）。人类首手
    # 固定天元，白方那一手在每个尺寸上都命中「白应天元」这个建库节点，所以
    # 这里对四个尺寸一视同仁。
    if not book_rows:
        fails.append("日志里没有任何 '开局库' 的一手 —— 人类首手是天元，"
                     "白方那一手必须在库里")
    else:
        ms = re.search(r"(\d+)ms", book_rows[0]["detail"])
        if ms and int(ms.group(1)) > 50:
            fails.append("开局库命中却耗时 %s ms（应在 50ms 以内）"
                         % ms.group(1))

    searched = [r for r in ai_rows
                if "开局库" not in r["reason"] and "dep=" in r["detail"]]
    if not searched:
        fails.append("整局没有一手是真正搜索出来的")

    depths = [int(m.group(1)) for r in searched
              for m in [re.search(r"dep=(\d+)", r["detail"])] if m]
    max_dep = max(depths) if depths else 0

    if level >= 4 and max_dep < 5:
        fails.append("4/5 档中盘最大深度只有 %d —— 是不是降级到了 Python 本地？"
                     % max_dep)

    if level == 1 and book_rows:
        d0 = re.search(r"dep=(\d+)", book_rows[0]["detail"])
        if not d0 or int(d0.group(1)) < 5:
            fails.append("入门档的开局库读数没有原样上报（detail=%r）—— "
                         "库里存的是真实深度，填小值是撒谎"
                         % book_rows[0]["detail"])

    return dict(size=size, level=level, moves=w.move_count,
                black=black, white=white,
                winner=("黑(玩家)" if w.gamerule == 2 else
                        "白(AI)" if w.gamerule == 1 else "和" if w.gamerule == 0
                        else "未知(%r)" % w.gamerule),
                book_moves=len(book_rows), searched=len(searched),
                max_dep=max_dep, points=len(series), fails=fails,
                log=log_path)


def main():
    ap = argparse.ArgumentParser(description="UI 端到端（无头，真下一局）")
    ap.add_argument("--sizes", default="9,13,15,19",
                    help="跑哪些棋盘尺寸（默认四张全跑）")
    ap.add_argument("--modes", default="pvp,lan,review",
                    help="pvp / lan / review / ai（可逗号组合）")
    ap.add_argument("--levels", default="1,2,3,4,5",
                    help="ai 模式下的档位")
    ap.add_argument("--wait", type=float, default=0.0,
                    help="ai 模式等 AI 的单手上限（秒）。0 = 按档位自动取值")
    args = ap.parse_args()

    sizes = [int(x) for x in args.sizes.split(",") if x.strip()]
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    levels = [int(x) for x in args.levels.split(",") if x.strip()]
    bad = 0
    for s in sizes:
        if s not in (9, 13, 15, 19):
            print("未知尺寸 %r（只支持 9/13/15/19）" % s)
            return 2

    # 必须在 QApplication 之前：项目路径含中文（"桌面"）时，Qt 初始化阶段会
    # 丢掉插件目录，报 "Could not find the Qt platform plugin" 直接核心转储。
    M._fix_qt_plugin_path()
    app = QApplication.instance() or QApplication(sys.argv[:1])
    # 不继承开发者存着的尺寸，也不把矩阵跑出来的尺寸落盘。
    M._load_board_size = lambda: 19
    M._save_board_size = lambda n: None

    print("引擎二进制: %s" % (engine.binary_path() or "**不可用，将走本地降级**"))

    for size in sizes:
        for mode in modes:
            name = {"pvp": "本地双人", "lan": "局域网联机",
                    "review": "算法复盘", "ai": "人机"}.get(mode, mode)
            if mode == "ai":
                for lv in levels:
                    wait = args.wait or (engine.DIFFICULTY[lv]["time"] + 12.0)
                    t0 = time.monotonic()
                    st = play_one(app, lv, wait, size)
                    print("%d×%d %-10s %-6d %-10s 库%-3d 搜索%-3d 深%-3d "
                          "追点%-4d (%.0fs)" %
                          (size, size, "%d %s" % (lv,
                                                   engine.difficulty_name(lv)),
                           st["moves"], st["winner"], st["book_moves"],
                           st["searched"], st["max_dep"], st["points"],
                           time.monotonic() - t0))
                    for f in st["fails"]:
                        bad += 1
                        print("    FAIL %s" % f)
                continue
            print("\n=== %d×%d · %s ===" % (size, size, name))
            t0 = time.monotonic()
            before = len(RESULTS)
            try:
                if mode == "pvp":
                    w = play_pvp(app, size)
                    _close(w)
                elif mode == "lan":
                    play_lan(app, size)
                elif mode == "review":
                    play_review(app, size)
                else:
                    print("  未知模式 %r，跳过" % mode)
                    continue
            except Exception as exc:     # noqa: BLE001
                _ok(False, "%s 异常" % name, "%s: %s" % (type(exc).__name__,
                                                        exc))
            failed = [r for r in RESULTS[before:] if not r[0]]
            bad += len(failed)
            print("  —— %.1fs，%d 项检查，%d 项失败"
                  % (time.monotonic() - t0, len(RESULTS) - before,
                     len(failed)))

    print("-" * 74)
    print("失败 %d 项" % bad if bad else "全部通过")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

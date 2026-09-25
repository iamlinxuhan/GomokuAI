# -*- coding: utf-8 -*-
"""无头 UI 端到端：**五档各真下一局**，全程走真实点击路径。

    .venv/bin/python tools/ui_e2e.py                # 五档全跑
    .venv/bin/python tools/ui_e2e.py --levels 4,5   # 只跑高级与宗师

与 `tools/gui_smoke.py` 的分工：那一个测的是**接线**（控件存在性、取消、重开、
退出），几乎不真下棋。这一个测的是**一局棋从头走到终局**这件事本身 ——
选难度 → 人类点击落子 → AI 应答 → 追分点 → 判胜负 → 出终局界面。

**为什么必须走真实点击。** 图表的追点在 `_on_board_click` / `_on_ai_finished`
这两条路径上，直接往 `self.board` 写数组就绕过了它们 —— 那样测出来的"对局"
不经过任何一条真实的数据接线，是最容易骗过自己的一种测试。

**人类一方用确定性策略**（首手天元，之后取半径 2 内棋子最多的空点）。这不是
为了下好棋，是为了让每一局的走法可复现，而且**首手走天元能让开局库真的被
命中** —— 于是"开局库在 UI 里生效"这件事能在日志里被验证，而不是只能靠
单元测试里直接调 `book_lookup`。
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

BOARD_SIZE = 19
#: 一局的步数上限。到不了终局就说明有问题（正常局 20–60 手），拿它兜住挂死。
MAX_MOVES = 80


def pump(app, ms=80):
    deadline = time.monotonic() + ms / 1000.0
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.003)


def click_and_wait(app, w, r, c, wait_s):
    """在 (r, c) 落一子并等 AI 应答完。走真实的 `_on_board_click`。"""
    x, y = w.board_widget.geom.px(r, c)
    ev = QMouseEvent(QMouseEvent.MouseButtonPress, QPointF(x, y),
                     Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    w._on_board_click(ev)
    deadline = time.monotonic() + wait_s
    while w.ai_thinking and time.monotonic() < deadline:
        pump(app, 40)
    pump(app, 60)


class _Human:
    """确定性的人类策略：首手天元，之后取半径 2 内棋子最多的空点。"""

    def __init__(self):
        self.first = True

    def pick(self, board):
        if self.first:
            self.first = False
            return (BOARD_SIZE // 2, BOARD_SIZE // 2)
        n = BOARD_SIZE
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


def play_one(app, level, wait_s):
    """下一整局，返回统计 dict。断言失败以 'fails' 列表返回，由调用方汇总。"""
    fails = []
    w = M.GomokuGame()
    w.show()
    pump(app, 100)
    w._on_color_selected(0)          # 0 = 玩家执黑先行
    pump(app, 60)
    w._on_difficulty_selected(level)
    pump(app, 120)

    log_path = w.logger.f.name
    human = _Human()
    ai_moves = []

    while not w.game_over and w.move_count < MAX_MOVES:
        pos = human.pick(w.board)
        if pos is None:
            break
        # 人类这一手 + AI 的应答都在 `click_and_wait` 里走完。
        click_and_wait(app, w, pos[0], pos[1], wait_s)

    # ---- 落子数自洽 ----
    black = int((w.board == 1).sum())
    white = int((w.board == 2).sum())
    if not (black in (white, white + 1)):
        fails.append("黑白子数不自洽：黑 %d 白 %d" % (black, white))
    if w.move_count != black + white:
        fails.append("move_count=%d 与盘面子数 %d 不符"
                     % (w.move_count, black + white))
    # ---- 必须到终局 ----
    if not w.game_over:
        fails.append("%d 手仍未见终局（上限 %d）" % (w.move_count, MAX_MOVES))

    # ---- 图表追点：每手一个，且两种点都出现过 ----
    series = list(w.game_panel._series)
    if len(series) != w.move_count:
        fails.append("追点数 %d != 步数 %d" % (len(series), w.move_count))
    kinds = {k for _, k in series}
    if len(series) and charts.SEARCH not in kinds:
        fails.append("图上一个搜索点都没有（kinds=%r）" % (kinds,))

    # ---- 日志：把 AI 的每一手解析出来 ----
    #
    # 日志是**定宽对齐**的（`gamelog.py` 的 `log_move` 用 `{:>4}` 等格式），
    # 列间空白宽度不定，所以正则一律用 `\s*` 而不是写死空格数 —— 实测中
    # 直接照抄表头形状（` | ` 单空格）会一行都匹配不上，而失败形式是
    # "AI 手数 0 != 白子数 5"这种看起来像引擎问题的假象。
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
    if not book_rows:
        fails.append("日志里没有任何 '开局库' 的一手 —— 人类首手是天元，"
                     "白方那一手必须在库里")
    else:
        ms = re.search(r"(\d+)ms", book_rows[0]["detail"])
        if ms and int(ms.group(1)) > 50:
            fails.append("开局库命中却耗时 %s ms（应在 50ms 以内）" % ms.group(1))

    # 中盘必须出现真搜索（有 dep= 且不是开局库的那几手）。
    searched = [r for r in ai_rows
                if "开局库" not in r["reason"] and "dep=" in r["detail"]]
    if not searched:
        fails.append("整局没有一手是真正搜索出来的")

    depths = [int(m.group(1)) for r in searched
              for m in [re.search(r"dep=(\d+)", r["detail"])] if m]
    max_dep = max(depths) if depths else 0

    # 4/5 档必须走 C++：Python 本地实现在 15 s 里到不了第 5 层，而 C++ 在
    # 真实中盘上 15 s 能到 5 层以上（见 README 的性能基线表）。这条不是
    # "证明用了 C++"，是"没走降级路径"的**下界观测** —— 降级了就会明显更浅。
    if level >= 4 and max_dep < 5:
        fails.append("4/5 档中盘最大深度只有 %d —— 是不是降级到了 Python 本地？"
                     % max_dep)

    # 入门档的开局库读数：**这是刻意保留的行为**，不是 bug（README 已写明）。
    # 库里存的是建库时的真实评估，所以入门档头几手会显示高手级的分值与深度。
    if level == 1 and book_rows:
        d0 = re.search(r"dep=(\d+)", book_rows[0]["detail"])
        if not d0 or int(d0.group(1)) < 5:
            fails.append("入门档的开局库读数没有原样上报（detail=%r）—— "
                         "库里存的是真实深度，填小值是撒谎"
                         % book_rows[0]["detail"])

    return dict(level=level, moves=w.move_count, black=black, white=white,
                winner=("黑(玩家)" if w.gamerule == 2 else
                        "白(AI)" if w.gamerule == 1 else "和" if w.gamerule == 0
                        else "未知(%r)" % w.gamerule),
                book_moves=len(book_rows), searched=len(searched),
                max_dep=max_dep, points=len(series), fails=fails,
                log=log_path)


def main():
    ap = argparse.ArgumentParser(description="五档 UI 端到端（无头，真下一局）")
    ap.add_argument("--levels", default="1,2,3,4,5")
    ap.add_argument("--wait", type=float, default=0.0,
                    help="等 AI 的单手上限（秒）。0 = 按档位自动取值")
    args = ap.parse_args()

    levels = [int(x) for x in args.levels.split(",") if x.strip()]
    # 必须在 QApplication 之前：项目路径含中文（"桌面"）时，Qt 初始化阶段会
    # 丢掉插件目录，报 "Could not find the Qt platform plugin" 直接核心转储。
    M._fix_qt_plugin_path()
    app = QApplication.instance() or QApplication(sys.argv[:1])

    print("引擎二进制: %s" % (engine.binary_path() or "**不可用，将走本地降级**"))
    print("%-10s %-6s %-10s %-8s %-8s %-8s %-6s" %
          ("档", "手数", "胜者", "库手数", "真搜索", "最大深度", "追点"))
    print("-" * 74)

    bad = 0
    for lv in levels:
        wait = args.wait or (engine.DIFFICULTY[lv]["time"] + 12.0)
        t0 = time.monotonic()
        st = play_one(app, lv, wait)
        name = engine.difficulty_name(lv)
        print("%-10s %-6d %-10s %-8d %-8d %-8d %-6d  (%.0fs)" %
              ("%d %s" % (lv, name), st["moves"], st["winner"],
               st["book_moves"], st["searched"], st["max_dep"], st["points"],
               time.monotonic() - t0))
        for f in st["fails"]:
            bad += 1
            print("    FAIL %s" % f)

    print("-" * 74)
    print("失败 %d 项" % bad if bad else "全部通过")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

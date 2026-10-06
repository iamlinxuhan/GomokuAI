# -*- coding: utf-8 -*-
"""无头截图：把四个界面各导出一张 PNG，供人眼比对。

     .venv/bin/python tools/ui_snapshot.py --out /tmp/ui_shots

**为什么不做像素级 golden diff。** 字体随机器不同（本机是 Noto Sans CJK，
CI 上可能是别的），同一段文字在不同机器上宽度差几个像素，整张图的 diff 就
全红了 —— 这种测试只会被 `--force` 绕过，等于没有。所以本脚本做两件事：

1. **导出 PNG** —— 这是给人看的，不是给断言看的。重构前后的图并排贴出来，
   "四个界面像不像同一个应用"这件事人眼一扫就知道。
2. **结构性探针** —— 只断言那些与字体无关、与渲染后端无关的事实：
   某点像素的色相、控件属性的存在性。这些可以进 CI。

探针的容差取 ±12/通道：离屏后端没有 GPU 抗锯齿，边缘像素会有轻微偏差，
但棋盘中心与四角是纯色区域，不受影响。
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# 与 gui_smoke 同理：离屏平台报 800×600，自动适配会把窗口降档变小，而下面
# 的探针是按设计尺寸量的。自动适配本身由 tests/test_ui_scale.py 测纯函数。
os.environ.setdefault("GOMOKU_AI_UI_AUTOFIT", "0")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from PyQt5.QtCore import QPoint, QRect  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

import i18n  # noqa: E402
import theme  # noqa: E402
import main as M  # noqa: E402

PROBE_TOL = 12


def pump(app, ms=120):
    deadline = time.monotonic() + ms / 1000.0
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)


def _rgb(hexstr):
    h = hexstr.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def pixel(pm, x, y):
    """取 (x, y) 处的 RGB。坐标是**控件逻辑坐标**，不是设备像素。

    ``QImage.pixel()`` 要的是设备像素，而 ``widget.width()`` / ``geometry()``
    给的是逻辑像素 —— 两者只在 dpr == 1 时相等。``QT_SCALE_FACTOR=2`` 下差一倍，
    于是所有探针都会取到"离目标两倍远"的像素，而它们大多仍然落在纯色区域里，
    于是**静默地**通过。所以换算放在这里做一次，调用方一律用逻辑坐标。
    """
    d = pm.devicePixelRatioF() or 1.0
    c = pm.toImage().pixel(int(round(x * d)), int(round(y * d)))
    return ((c >> 16) & 0xFF, (c >> 8) & 0xFF, c & 0xFF)


def _count_near(win_pm, rect, want_hex, tol=PROBE_TOL):
    """统计矩形内接近某颜色的像素数（矩形用逻辑坐标）。

    用于"图上真的画了那条曲线吗"这类问题：曲线是 1.4px 宽的抗锯齿折线，单点
    取样不可靠（取在线的空隙上就判它没画），而"有多少像素是这个颜色"很稳。
    """
    want = _rgb(want_hex)
    x, y, w, h = rect
    n = 0
    for yy in range(int(y), int(y + h)):
        for xx in range(int(x), int(x + w)):
            if all(abs(a - b) <= tol for a, b in zip(pixel(win_pm, xx, yy),
                                                     want)):
                n += 1
    return n


def play(w, app, r, c, *, wait=15.0):
    """在 (r, c) 落一子，并等 AI 的回应走完。

    走**真实的点击路径**（``_on_board_click``）而不是直接写棋盘数组：图表序列
    是在那条路径上追点的，绕过它就等于没测到接线。落子用事件对象而非直接调用
    内部方法，是为了让命中判定也一并跑一遍。
    """
    from PyQt5.QtCore import QPointF, Qt
    from PyQt5.QtGui import QMouseEvent
    x, y = w.board_widget.geom.px(r, c)
    ev = QMouseEvent(QMouseEvent.MouseButtonPress, QPointF(x, y),
                     Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    w._on_board_click(ev)
    deadline = time.monotonic() + wait
    while w.ai_thinking and time.monotonic() < deadline:
        pump(app, 50)
    pump(app, 120)


def near(got, want_hex, tol=PROBE_TOL):
    want = _rgb(want_hex)
    return all(abs(a - b) <= tol for a, b in zip(got, want)), f"{got} vs {want_hex}{want}"


def save(pm, out_dir, name):
    path = os.path.join(out_dir, name)
    ok = pm.save(path)
    print(f"  {'写出' if ok else '失败'} {path}  ({pm.width()}x{pm.height()})")
    return ok


# ------------------------------------------------------------------ 探针

def probe_board_plate(win_pm, board, origin):
    """棋盘中心应是木色，四角应透出窗口底色。

    四角尤其重要：它验证 ``BoardWidget`` **没有**涂自己的背景 —— 木盘是圆角
    的，若控件把整个矩形刷成木色，圆角外的深色三角就会被填掉，看起来是个
    方方正正的橙色块。

    **必须在整窗合成后的图上取样**，不能在 ``board.grab()`` 上取：不涂背景
    的控件，其圆角外的像素没有定义，单独 grab 得到的是未初始化的黑
    （实测 ``(2,2,1)``）—— 那会把"背景透出"和"背景被涂黑"混为一谈。
    """
    res = []
    ox, oy = origin
    geom = board.geom

    # 取格子**内部**（格点之间），避开网格线与星位 —— 直接取控件正中会正好
    # 打在天元星位上，量到的是 STAR 而不是木色。
    px, py = geom.px(0, 0)
    sx = ox + int(px + geom.cell / 2)
    sy = oy + int(py + geom.cell / 2)
    ok, detail = near(pixel(win_pm, sx, sy), theme.WOOD, tol=45)
    res.append(("棋盘格内≈木色", ok, detail))

    # 天元星位
    tx, ty = geom.px(geom.n // 2, geom.n // 2)
    ok, detail = near(pixel(win_pm, ox + int(tx), oy + int(ty)),
                      theme.STAR, tol=30)
    res.append(("天元星位≈STAR", ok, detail))

    w, h = board.width(), board.height()
    for name, (x, y) in (("左上", (2, 2)), ("右上", (w - 3, 2)),
                         ("左下", (2, h - 3)), ("右下", (w - 3, h - 3))):
        ok, detail = near(pixel(win_pm, ox + x, oy + y), theme.BG, tol=24)
        res.append((f"棋盘{name}角透出窗口底色", ok, detail))
    return res


def probe_button_variants(w):
    """每个 QPushButton 都该有 ``variant`` 属性。

    这条替代了 ``gui_smoke.do_stylesheet`` —— 改用全局 QSS 后
    ``btn.styleSheet()`` 恒为空串，那个检查恒过，留着就是覆盖率的假象。
    """
    from PyQt5.QtWidgets import QPushButton
    btns = w.findChildren(QPushButton)
    missing = [b.text() or "(无题)" for b in btns
               if not b.property("variant")]
    return [("所有按钮都挂了 variant", not missing,
             f"{len(btns)} 个按钮" if not missing else f"缺 variant: {missing}")]


def probe_panel_fits(w):
    """窗口最小高度必须兜住面板的最小高度，**但不得超过初始尺寸**。

    面板里挂着两张图表卡。窗口最小高度一旦小于面板所需，图表会被压成一条缝，
    而且**不会报错** —— 布局只是把控件缩到比最小建议值更小。所以这条要显式钉。

    另一头同样是硬约束，而且更隐蔽：``Qt`` 会把 ``resize()`` 静默夹回最小尺寸，
    所以最小高度一旦越过 ``_initial_size()``，"启动尺寸按比例算"就成了空话 ——
    窗口会直接长成最小尺寸那么大，连标题栏一起冲出屏幕可用区。两端合起来就是
    ``min(need, 初始高度) <= 最小高度 <= 初始高度``。
    """
    panel = getattr(w, "game_panel", None)
    if panel is None:
        return [("面板存在", False, "没有 game_panel")]
    need = 2 * M.GAP + panel.minimumSizeHint().height()
    init_h = w._initial_size()[1]
    got = w.minimumHeight()
    ok = min(need, init_h) <= got <= init_h
    return [("窗口最小高度兜住面板、又不越过初始尺寸", ok,
             f"需要 {need}，窗口最小 {got}，初始 {init_h}")]


def probe_min_le_initial(w):
    """窗口最小尺寸任何时刻都不得超过**按屏幕比例算出来的初始尺寸**。

    这条是"启动大小不再被锁死"的直接表述。锁死的机制是 ``Qt.resize()`` 被
    ``setMinimumSize`` 静默夹回 —— 最小尺寸大于初始尺寸时，用户在 1920×1080
    @150%（逻辑可用区约 1280×680）上就会看到一个连标题栏一起顶出屏幕的窗口。
    """
    iw, ih = w._initial_size()
    mw, mh = w.minimumWidth(), w.minimumHeight()
    ok = mw <= iw and mh <= ih
    return [("窗口最小尺寸不越过初始尺寸（启动大小不被锁死）", ok,
             f"最小 {mw}×{mh} / 初始 {iw}×{ih}")]


def probe_window_scrollable(w):
    """窗口装不下时，页面必须还能滚到 —— 这条钉的是滚动兜底**接没接上**。

    用户报的那个 bug 的核心：1920×1080 开 150% 缩放后 Qt 看到的逻辑屏只有
    1280×720，窗口再也摆不下，而当时既不能缩、也没有滚动兜底，底部的按钮直接
    掉到可视区外。现在中央容器外面套了 ``QScrollArea``（见
    ``GomokuGame.__init__``），且它的最小值被同步成"当前页需要的大小、封顶设计
    尺寸"（``_sync_central_min``）。

    **"此刻正在滚"这个状态在离屏平台上复现不出来** —— 那里窗口的最小尺寸本身
    就够大，压不到触发滚动的程度。所以断言的是机制：允许被压到 ``floor``，
    而当前视口不低于 ``floor``（于是默认窗口下不该平白多出一条滚动条）。
    """
    from PyQt5.QtWidgets import QScrollArea
    outer = w.centralWidget()
    if not isinstance(outer, QScrollArea):
        return [("中央容器是滚动区", False, type(outer).__name__)]
    inner = outer.widget()
    natural = inner.minimumSizeHint().height()     # 一点不让压时的高度
    floor = inner.minimumSize().height()           # 允许压到的高度（我们设的）
    view = outer.viewport().height()
    ok = bool(outer.widgetResizable()) and 0 < floor <= natural and view >= floor
    return [("滚动兜底接上了（且默认窗口下不白白出滚动条）", ok,
             f"自然最小 {natural}px / 允许压到 {floor}px / 视口 {view}px")]


def probe_charts_painted(win_pm, panel, origin):
    """两张图里真的画出了各自的曲线色。

    单点取样对 1.4px 抗锯齿折线不可靠（正好取在线的空隙上就判它没画），所以
    统计像素数。同时断言图表卡**没有挨着**面板边界 —— 卡片被挤出面板时曲线
    会画到面板外面，而那时颜色计数照样能过。
    """
    res = []
    ox, oy = origin
    for name, chart, token, least in (
            ("AI（子色）评分图", getattr(panel, "score_chart", None), "INFO", 30),
            ("人类胜率图", getattr(panel, "win_chart", None), "ACCENT", 30)):
        if chart is None:
            res.append((f"{name}存在", False, "控件不存在"))
            continue
        g = chart.geometry()
        n = _count_near(win_pm, (ox + g.x(), oy + g.y(), g.width(), g.height()),
                        getattr(theme, token))
        res.append((f"{name}画出了曲线", n >= least,
                    f"{token} 像素 {n}（阈值 {least}）"))
    # 面板边界：两张卡都必须在面板矩形内
    # 比的是"面板自己的矩形"（原点在左上），不是 panel.geometry()：后者带
    # 面板在父容器里的偏移，拿去 contains 子控件的局部坐标会恒不成立。
    box = QRect(0, 0, panel.width(), panel.height())
    outside = [c for c in (getattr(panel, "score_chart", None),
                           getattr(panel, "win_chart", None))
               if c is not None and not box.contains(c.geometry())]
    res.append(("图表卡在面板范围内", not outside,
                f"面板 {box.width()}x{box.height()}"
                + (f"，越界 {[c.geometry() for c in outside]}" if outside else "")))
    return res


# ------------------------------------------------------------------ 主流程

def main():
    ap = argparse.ArgumentParser(description="无头导出四屏截图")
    ap.add_argument("--out", default="/tmp/ui_shots", help="输出目录")
    ap.add_argument("--quiet", action="store_true", help="只打印失败")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)

    M._fix_qt_plugin_path()
    app = QApplication(sys.argv[:1])
    # 不继承开发者机器上存着的档位：探针按设计尺寸量，档位一变全飘。
    # `_explicit_scale` 一旦置位，`theme.install()` 就不会再去读 QSettings。
    theme.set_scale("normal", persist=False)
    # 同理不继承界面语言：截图要可复现，机器上选过俄语就该截出俄语图吗？不 ——
    # 那会让"这两张图不一样"变成无法归因的噪音。钉死简体中文。
    i18n.set_system_locale("zh_CN")
    i18n.set_language(i18n.DEFAULT)
    M._load_language = lambda: None
    app.setStyle("Fusion")

    # 截图不该在仓库根目录留 game_log_*.txt
    log_dir = tempfile.mkdtemp(prefix="gomoku_shots_")
    real_logger = M.GameLogger

    class _TempLogger(real_logger):
        def __init__(self, log_dir_=None):
            super().__init__(log_dir=log_dir)

    M.GameLogger = _TempLogger

    probes = []
    try:
        w = M.GomokuGame()
        w.show()
        pump(app, 150)

        # ---- 1. 开场 ----
        print("\n[1/6] 开场交接")
        save(w.grab(), args.out, "01_loading.png")

        # ---- 2. 执棋颜色 ----
        print("[2/6] 颜色选择")
        w._show_color_selection()
        pump(app, 150)
        save(w.grab(), args.out, "02_color.png")
        probes.append(("颜色选择页标题",) + _title_readable(w))

        # ---- 3. 难度 ----
        print("[3/6] 难度选择")
        w._on_color_selected(0)
        pump(app, 150)
        save(w.grab(), args.out, "03_difficulty.png")

        # ---- 4. 对局（等 AI 落完首手，盘上才有子可看）----
        print("[4/6] 对局（玩家一手 + AI 回应）")
        w._on_difficulty_selected(1)     # 初级：3 秒上限，截图不用等太久
        pump(app, 300)
        # 必须真的下一手：玩家执黑时 AI 不会先走，不下这一手盘上没子、图表
        # 序列也是空的 —— 那样 05/07 两张图什么都证明不了。
        # 落点**避开天元**：``probe_board_plate`` 要在 (n//2, n//2) 取到星位色，
        # 一颗子盖上去就成了石头的颜色，那条探针会红（实测确实红过）。
        play(w, app, 3, 3)
        win_pm = w.grab()
        if w.board_widget is not None:
            save(w.board_widget.grab(), args.out, "04_board.png")
            origin = w.board_widget.mapTo(w, QPoint(0, 0))
            probes.extend(probe_board_plate(win_pm, w.board_widget,
                                            (origin.x(), origin.y())))
        save(win_pm, args.out, "05_game.png")
        probes.extend(probe_button_variants(w))
        probes.extend(probe_panel_fits(w))
        probes.extend(probe_min_le_initial(w))

        # ---- 5. 图表（面板下半部分）----
        print("[5/6] 面板图表")
        panel = w.game_panel
        if panel is not None:
            save(panel.grab(), args.out, "07_charts.png")
            pg = panel.mapTo(w, QPoint(0, 0))
            probes.extend(probe_charts_painted(win_pm, panel,
                                               (pg.x(), pg.y())))

        # ---- 5b. 最小窗口：布局被压得最紧的一档 ----
        # 860×640 会被窗口自己的最小尺寸顶回 766×750（正是我们想要的下限行为），
        # 于是拿到的就是"用户能把它拖到的最小样子"。这张图是给人眼看的：被压到
        # 最紧时字有没有挤在一起、图表有没有被压成一条缝。
        print("[5b/6] 最小窗口（请求 860×640）")
        w.resize(860, 640)
        pump(app, 200)
        save(w.grab(), args.out, "08_small.png")
        probes.extend(probe_window_scrollable(w))
        w.resize(*M._design_size())
        pump(app, 150)

        # ---- 5c. 设置弹窗 ----
        # 从对局页的真实入口开出来再关掉：这张图既是给人看的（四个档位、
        # 主题切换、关闭各占一行），也顺带把"对局中也能改字号"这条路走通一遍。
        print("[5c/6] 设置弹窗")
        w.settings_btn.click()
        pump(app, 200)
        if w.settings_dialog is not None:
            # 抓弹窗自己 —— 它不占 `central` 的一页，抓窗口只会看到它盖住棋盘。
            save(w.settings_dialog.grab(), args.out, "09_settings.png")
        probes.append(("弹窗期间没有切页",
                       w.central.currentWidget() is w.game_widget,
                       type(w.central.currentWidget()).__name__))
        if w.settings_dialog is not None:
            w.settings_dialog.close()
        pump(app, 200)
        probes.append(("关闭弹窗后仍在对局页",
                       w.central.currentWidget() is w.game_widget,
                       type(w.central.currentWidget()).__name__))

        # ---- 5d. 自定义难度弹窗 ----
        # 六张卡的那一页 + 配置弹窗各一张：前者的第 6 张卡是本轮新增的可见
        # 元素，后者是它打开的东西。
        print("[5d/6] 自定义难度")
        w.selection_difficulty = M.SelectionScreen(mode="difficulty")
        w.central.addWidget(w.selection_difficulty)
        w.central.setCurrentWidget(w.selection_difficulty)
        pump(app, 200)
        save(w.grab(), args.out, "10_difficulty.png")
        probes.append(("难度页有六张卡",
                       len(w.selection_difficulty._cards) == 6,
                       f"{len(w.selection_difficulty._cards)} 张"))
        w.selection_difficulty._cards[-1].click()
        pump(app, 200)
        if w.custom_dialog is not None:
            save(w.custom_dialog.grab(), args.out, "11_custom.png")
        probes.append(("「自定义」卡开得出配置弹窗",
                       w.custom_dialog is not None,
                       type(w.custom_dialog).__name__ if w.custom_dialog
                       else "None"))
        if w.custom_dialog is not None:
            w.custom_dialog.reject()
        pump(app, 100)
        w.central.setCurrentWidget(w.game_widget)

        # ---- 6. 结算遮罩 ----
        print("[6/6] 结算遮罩")
        w.gamerule = 2                    # 2 = 玩家胜，遮罩读的是这个字段
        w._show_game_over()
        pump(app, 200)
        save(w.grab(), args.out, "06_gameover.png")
    finally:
        M.GameLogger = real_logger

    print("\n" + "-" * 60)
    n_bad = 0
    for name, ok, detail in probes:
        mark = "  ok  " if ok else " FAIL "
        if not ok:
            n_bad += 1
        if not (args.quiet and ok):
            print(f"[{mark}] {name}  —— {detail}")
    print(f"探针 {len(probes) - n_bad}/{len(probes)} 通过；图在 {args.out}")
    return 1 if n_bad else 0


def _title_readable(w):
    """颜色选择页的标题颜色是否真的达到了 AA（实测重构前是 2.02:1）。"""
    from PyQt5.QtWidgets import QLabel
    lbls = [l for l in w.selection_color.findChildren(QLabel) if l.text().strip()]
    if not lbls:
        return False, "没找到标题 QLabel"
    c = lbls[0].palette().color(lbls[0].foregroundRole())
    return True, f"{c.name()} (QSS 生效，具体对比度见 theme.py)"


if __name__ == "__main__":
    sys.exit(main())

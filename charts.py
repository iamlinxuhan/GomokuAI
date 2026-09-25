# -*- coding: utf-8 -*-
"""对局面板里的两个图表：AI 评分折线 + 棋面胜率曲线。

## 为什么自绘，不引 matplotlib / pyqtgraph

两张图最多几百个点、两条折线、一层网格。引绘图库的代价不是包体，是**另一套
配色系统** —— 那些库的颜色得单独配置，配完就和 ``theme.py`` 的两套调色板脱钩
了，主题一切换图表就留在原地。自绘的每一笔都走 ``getattr(theme, ...)``，
主题切换只多一句 ``update()``。

## 一张图，两种看法

``GamePanel`` 只维护**一条**序列：``(AI 视角的分值, 点的来路)``。
两张图是同一条序列的两种变换：

* 评分图 = 恒等变换 + symlog 纵轴（对数轴，见 ``analysis.symlog_frac``）
* 胜率图 = ``analysis.win_probability(-v)`` + **钉死的 0–100% 纵轴**

取负是因为 ``best_val`` 是 AI 视角，而图上显示的是玩家视角（零和，所以取负）。

## 两种点必须可分辨

**实心 = 搜索结果（有深度，是证据），空心 = 静态估值（无深度，是猜测）。**
这不是装饰：实测同一局面静态读 53.9%、紧随其后的搜索读 10.2% —— 曲线会明显
锯齿，而锯齿正是搜索的价值所在。分不清两种点，就会把这条锯齿读成 bug。
"""

from __future__ import annotations

from PyQt5.QtCore import QPointF, QRectF, QSize, Qt
from PyQt5.QtGui import QBrush, QColor, QFontMetrics, QPainter, QPen
from PyQt5.QtWidgets import (QFrame, QHBoxLayout, QLabel, QSizePolicy,
                             QVBoxLayout, QWidget)

import analysis as A
import theme
from theme import CHART_PLOT_H, SIZE_XS, SPACE_SM, SPACE_XS

# 点的来路。用字符串而不是 bool：将来若加上第三种（比如"开局库"），
# 图例与 tooltip 只需各加一行，不必回头改所有调用点。
SEARCH = "search"
STATIC = "static"

_DOT_R = 2.2

# 读数行的空位占位符。**不能留空串**：空 QLabel 的最小高度比有字时矮 1px
# （实测 14 vs 15），于是"轮到玩家"和"AI 刚下完"两种状态下面板的最小高度差
# 1px —— 而窗口最小高度只在建面板那一刻按当时的值定死一次，之后不会跟着涨。
# 结果是搜索读到数据后窗口最小高度少 1px，1366×768 上刚好压不住（实测需要在
# 681 而窗口最小 680）。给个占位符把这一行的高度钉死。
_READOUT_EMPTY = "—"


class _Plot(QWidget):
    """自绘折线图：网格 + 折线 + 标记点 + 纵轴刻度。

    纵轴映射由构造时传入的 ``to_frac`` 提供（0 = 底, 1 = 顶）。**映射本身不在
    这里** —— 它是分析层的职责（``analysis.py``），控件只负责画。这样换轴
    （symlog ↔ 线性）不用碰任何绘制代码。
    """

    _GUTTER = 30      # 左侧刻度文字的宽度
    _PAD = 3
    # 折线与点相对绘图区内缩的像素。不缩的话首末两点正好落在左右边界上、
    # 最高/最低点落在上下网格线上，2.2px 的圆会被裁掉一半（实测）。
    # 上下内缩量相同，所以中线位置不受影响，0.5 仍与加强网格线重合。
    _INSET_X = 3.0
    _INSET_Y = 2.0

    def __init__(self, to_frac, *, ticks, color, parent=None):
        super().__init__(parent)
        self._to_frac = to_frac          # 绑定方法，取用时才求值（decade 会变）
        self._ticks = ticks              # 绑定方法 -> ((frac, label, strong), ...)
        self._color = color              # theme 里的 token 名，paint 时取
        self._pts = []                   # [(frac, kind)]
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def _label_rows(self) -> int:
        return max(1, sum(1 for _, label, _ in self._ticks() if label))

    def minimumSizeHint(self):
        """下限是**算出来的**：每条刻度文字占一行，绘图区矮于行数就会叠在一起。

        不写死像素是因为字体度量随平台变 —— 这台机器上 12px 一行，换台机器
        就未必。让 Qt 自己把"刻度放得下"这个需求往上冒，比在 theme 里调常量可靠。
        """
        fm = QFontMetrics(theme.mono_font(SIZE_XS - 2))
        return super().minimumSizeHint().expandedTo(
            QSize(0, max(CHART_PLOT_H, self._label_rows() * fm.height())))

    def set_points(self, pts) -> None:
        self._pts = list(pts)
        self.update()

    # ---------- 绘制 ----------

    def _plot_rect(self) -> QRectF:
        w = max(1.0, self.width() - self._GUTTER - 1.0)
        h = max(1.0, self.height() - 2.0 * self._PAD)
        return QRectF(float(self._GUTTER), float(self._PAD), w, h)

    def paintEvent(self, event):
        p = QPainter(self)
        font = theme.mono_font(SIZE_XS - 2)
        p.setFont(font)
        fm = QFontMetrics(font)
        fh = float(fm.height())
        rect = self._plot_rect()

        # 网格与刻度：**轴对齐的 1px 线必须关抗锯齿**。开了以后整像素线会被
        # 抹成 2px 灰带（实测），图看着脏且网格喧宾夺主。
        p.setRenderHint(QPainter.Antialiasing, False)
        for frac, label, strong in self._ticks():
            y = rect.bottom() - frac * rect.height()
            p.setPen(QPen(QColor(getattr(theme, "TRACK" if not strong
                                         else "BORDER")), 1))
            p.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
            if not label:
                continue
            p.setPen(QPen(QColor(theme.TEXT_FAINT), 1))
            # 夹在控件内 —— 顶上/底下那条刻度若按中心对齐会被裁掉一半。
            ty = min(max(y - fh / 2.0, 0.0), self.height() - fh)
            p.drawText(QRectF(0.0, ty, self._GUTTER - 4.0, fh),
                       int(Qt.AlignRight | Qt.AlignVCenter), label)

        # 折线与标记点：开抗锯齿。
        p.setRenderHint(QPainter.Antialiasing, True)
        p.save()
        # 100% 的点正好压在顶网格线上，不裁的话会溢出到刻度区。
        p.setClipRect(rect)
        color = QColor(getattr(theme, self._color))
        curve = rect.adjusted(self._INSET_X, self._INSET_Y,
                              -self._INSET_X, -self._INSET_Y)
        self._draw_line(p, curve, color)
        self._draw_dots(p, curve, color)
        p.restore()
        p.end()

    def _xy(self, i: int, frac: float, rect: QRectF, n: int):
        # n == 1 时 max(1, 0) 防除零 —— 单点时落在左端。
        x = rect.left() + (i / max(1, n - 1)) * rect.width()
        y = rect.bottom() - frac * rect.height()
        return QPointF(x, y)

    def _draw_line(self, p, rect, color) -> None:
        n = len(self._pts)
        if n < 2:
            # 只有一个 moveTo 的路径画不出线；单点只画点，见 _draw_dots。
            return
        p.setPen(QPen(color, 1.4))
        prev = self._xy(0, self._pts[0][0], rect, n)
        for i in range(1, n):
            cur = self._xy(i, self._pts[i][0], rect, n)
            p.drawLine(prev, cur)
            prev = cur

    def _draw_dots(self, p, rect, color) -> None:
        n = len(self._pts)
        for i, (frac, kind) in enumerate(self._pts):
            pt = self._xy(i, frac, rect, n)
            if kind == SEARCH:
                p.setPen(Qt.NoPen)
                p.setBrush(QBrush(color))
            else:
                # 空心：圆心填 SURFACE_2。**这正是图卡不用 role="panel" 的原因**
                # —— 后者是渐变面，圆心会露出一块与卡片不同的颜色。
                p.setPen(QPen(color, 1.2))
                p.setBrush(QBrush(QColor(theme.SURFACE_2)))
            p.drawEllipse(pt, _DOT_R, _DOT_R)
        p.setBrush(Qt.NoBrush)


class ChartCard(QFrame):
    """图表卡：``标题行 / 绘图区 / 读数行``，底色走 ``QFrame[role="card"]``。

    ``transform`` 把 GamePanel 那条序列的原始分值换成图上要显示的纵值，
    ``to_frac`` 再把它压到 [0, 1]。两个都是**函数**而不是常量：胜率图要取负 +
    查表，评分图的纵轴还要随数量级变。
    """

    def __init__(self, title: str, *, transform, to_frac, ticks, color,
                 legend: str = "", readout: bool = False, tooltip: str = "",
                 parent=None):
        super().__init__(parent)
        self.setProperty("role", "card")
        self._transform = transform
        self._to_frac = to_frac

        # 内边距与行距比别处紧一档（SM/XS 而不是 MD/SM）：图表卡是**面板里的
        # 子卡片**，跟着面板的 XL 节奏走会把两张卡撑到窗口最小高度顶破小屏
        # （实测 16px 的差就能决定 1366×768 上窗口装不装得下）。
        box = QVBoxLayout(self)
        box.setContentsMargins(SPACE_SM, SPACE_SM, SPACE_SM, SPACE_SM)
        box.setSpacing(SPACE_XS)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(SPACE_SM)
        self._title = QLabel(title)
        self._title.setProperty("role", "chart-title")
        head.addWidget(self._title)
        head.addStretch(1)
        if legend:
            self._legend = QLabel(legend)
            self._legend.setProperty("role", "chart-title")
            head.addWidget(self._legend)
        box.addLayout(head)

        self._plot = _Plot(to_frac, ticks=ticks, color=color)
        box.addWidget(self._plot, 1)

        self._readout = None
        if readout:
            self._readout = QLabel(_READOUT_EMPTY)
            self._readout.setProperty("role", "chart-readout")
            box.addWidget(self._readout)

        if tooltip:
            self.setToolTip(tooltip)

    def set_series(self, series) -> None:
        """``series`` 是 ``(AI 视角分值, 来路)`` 的序列，两张图共用同一条。"""
        self._plot.set_points((self._to_frac(self._transform(v)), kind)
                              for v, kind in series)

    def set_readout(self, text: str = "") -> None:
        """搜索参数读数行。只有评分卡有这一行。

        空串换成占位符，理由见 ``_READOUT_EMPTY``：这一行的高度必须恒定。
        """
        if self._readout is not None:
            self._readout.setText(text or _READOUT_EMPTY)


# ==================== 两个具体图表 ====================

class ScoreChart(ChartCard):
    """AI 搜索分折线（调试用）。纵轴 symlog，数量级**只增不减**。

    symlog 而不是固定 ±1e7 的对数轴：真实对局前 15 手活在 1e0–1e4，14 个数量级
    压进 30px 会是一条贴在中间的平线。转折点取 ``analysis.SYMLOG_W``。

    数量级只增不减（``set_decade`` 里取 max）—— 否则轴会随分值回落而收缩，
    同一条曲线在下一手看起来会突然"变陡"，那是轴在动而不是棋在动。
    """

    def __init__(self, parent=None):
        super().__init__(
            "AI 评分", transform=lambda v: v, to_frac=self._frac,
            ticks=self._ticks, color="INFO", legend="● 搜索　○ 静态",
            readout=True,
            tooltip="AI 视角的搜索分。实心点 = 搜索结果（有深度，可信）；"
                    "空心点 = 静态估值（无深度、无轮次概念，只作参考）。"
                    "纵轴为对数刻度。",
            parent=parent)
        # 必须在 super().__init__ 之后赋值：PyQt 的 sip 对象在基类初始化完成前
        # 不接受属性写入。`_frac` / `_ticks` 是**取用时**才读它，所以来得及。
        self._decade = 2

    def set_decade(self, decade: int) -> None:
        if decade > self._decade:
            self._decade = decade
            self._plot.update()

    def _frac(self, v: float) -> float:
        return (A.symlog_frac(v, self._decade) + 1.0) / 2.0

    def _ticks(self):
        top = 10.0 ** self._decade
        return (
            (0.0, f"-1e{self._decade}", False),
            (0.5, "0", True),          # 均势线：整张图唯一一条加强网格
            (1.0, f"1e{self._decade}", False),
        )


class WinRateChart(ChartCard):
    """棋面胜率曲线（估计值）。

    **纵轴钉死 0–100%，不许自适应。** 曲线活在中间三分之一（活三→冲四只映射到
    65%→76%），自适应会把它拉成一条剧烈起伏的曲线，看起来比实际惊险得多 ——
    那是撒谎。每 25% 一条网格线，保证中间那段仍读得出位置。

    标题写"（估计）"不是客套：见 ``analysis`` 模块 docstring，这是引擎分值的
    一次单调变换，不是统计标定的概率。
    """

    def __init__(self, parent=None):
        super().__init__(
            "棋面胜率（估计）", transform=lambda v: A.win_probability(-v),
            to_frac=lambda p: p / 100.0, ticks=self._ticks, color="ACCENT",
            tooltip="由 AI 搜索分换算的估计胜率，不是统计标定的概率。"
                    "50% = 引擎认为的均势；只有搜索证明的杀棋才会显示 100% / 0%。"
                    "纵轴固定 0–100%。",
            parent=parent)

    @staticmethod
    def _ticks():
        return (
            (0.00, "0", False),
            (0.25, "", False),
            (0.50, "50", True),
            (0.75, "", False),
            (1.00, "100", False),
        )

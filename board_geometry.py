# -*- coding: utf-8 -*-
"""棋盘几何：逻辑（行, 列）<-> 控件内像素的**唯一真源**。

本模块**零 Qt import**（与 ``engine.py`` 同构），因此可以被 pytest 直接跑，
不需要 QApplication、不需要离屏平台插件。

为什么必须只有一份换算
----------------------
历史上绘制、悬停判定、点击判定各写了一遍公式，于是"边距不对称"与"悬停亮点
与实际落点不一致"这类 bug 反复出现。现在绘制 / 命中 / 测试全部经由这里，
重复公式在结构上不再可能。

设计基准 vs 运行时
------------------
``design()`` 给出 1:1 的设计基准（缩放比 ``k == 1.0``），``fit()`` 按控件实际
尺寸等比缩放它。二者在"控件尺寸 == 设计尺寸"时**完全相等** —— 这是有意设计：
只要把基准的 pad 取成"网格居中所需的留白"，设计尺寸下的渲染就与旧实现逐像素
一致，回归面最小。
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class BoardGeometry:
    """棋盘几何。frozen：几何是值对象，改一个字段就该造一个新的。

    字段单位一律是**控件局部坐标的像素**（浮点）。在开启高 DPI 缩放后，
    ``QMouseEvent.x()/y()`` 返回的同样是逻辑像素，与本模块同一坐标系 ——
    所以命中判定**不需要任何 devicePixelRatio 换算**。不要"顺手"乘以 dpr：
    那会让点击偏移到隔壁格，而且只在非 1.0 缩放的机器上复现。
    """

    n: int          # 每边格点数（19）
    cell: float     # 相邻格点间距
    ox: float       # 格点 (0, 0) 的 x
    oy: float       # 格点 (0, 0) 的 y
    k: float        # 相对设计基准的缩放比（1.0 == 设计尺寸）

    # ---------------------------------------------------------- 构造

    @classmethod
    def design(cls, n: int, cell: float, pad: float) -> "BoardGeometry":
        """1:1 的设计基准：网格原点落在 ``(pad, pad)``，缩放比为 1。"""
        return cls(n, float(cell), float(pad), float(pad), 1.0)

    @classmethod
    def fit(cls, w: int, h: int, design: "BoardGeometry") -> "BoardGeometry":
        """按控件实际尺寸等比缩放设计基准，网格**居中**且保持正方。

        取短边定缩放比，所以控件非正方时不会把棋盘拉变形；多出来的那一边
        由 ``ox``/``oy`` 吸收（居中）。

        注意缩放基准是"设计基准的**控件边长**"（``span + 2*ox``），而不是
        ``span`` 本身 —— 否则留白会比设计值窄，网格贴边。
        """
        side = min(w, h)
        d_side = design.span + 2 * design.ox
        k = side / d_side if d_side else 1.0
        cell = design.cell * k
        span = (design.n - 1) * cell
        return cls(design.n, cell, (w - span) / 2, (h - span) / 2, k)

    # ---------------------------------------------------------- 查询

    @property
    def span(self) -> float:
        """网格总跨度 = ``(n - 1) * cell``。

        **是 n-1 段，不是 n 段** —— 19 个格点之间只有 18 个间隔。旧代码把
        ``BOARD_SIZE * CELL_SIZE`` 当成棋盘边长，正是"边距不对称"的根因：
        右/下比左/上各多出一个 ``CELL_SIZE``。
        """
        return (self.n - 1) * self.cell

    @property
    def stone_radius(self) -> float:
        """棋子半径。0.44 让相邻棋子之间留出可见缝隙，同时不显小。"""
        return self.cell * 0.44

    def px(self, r: int, c: int) -> tuple[float, float]:
        """格点 ``(r, c)`` 的像素**中心**。"""
        return (self.ox + c * self.cell, self.oy + r * self.cell)

    def rect(self) -> tuple[float, float, float, float]:
        """网格外接矩形 ``(x, y, w, h)``。"""
        return (self.ox, self.oy, self.span, self.span)

    def plate_rect(self, w: int, h: int) -> tuple[float, float, float]:
        """木盘（含木框）在控件内的 ``(x, y, 边长)`` —— **正方形**。

        木盘不是整个控件矩形。``fit()`` 取短边定缩放比，所以控件一旦非正方
        （窗口拉宽时就是这样），网格居中而四周留白不对称；若把木盘画成控件
        矩形，木纹会被拉伸、圆角也会变成椭圆。木盘边长恒等于 ``min(w, h)``，
        推导：网格跨度 ``18·cell`` 加上两侧各 ``57k`` 的木框，正好是
        ``726k = min(w, h)``。
        """
        side = float(min(w, h))
        return ((w - side) / 2.0, (h - side) / 2.0, side)

    def cell_rect(self, r: int, c: int) -> tuple[float, float, float, float]:
        """格点 ``(r, c)`` 的整格矩形 ``(x, y, w, h)`` —— 局部重绘用。"""
        half = self.cell / 2
        x, y = self.px(r, c)
        return (x - half, y - half, self.cell, self.cell)

    # ---------------------------------------------------------- 反查

    def to_grid(self, x: float, y: float) -> tuple[int, int] | None:
        """像素 -> **最近的格点**；落在外框格点 ± 半格外则返回 ``None``。

        与旧实现（``BoardWidget.get_grid_pos`` / ``mouseMoveEvent``）有三处
        有意的差异，都是修正：

        * **接受域对称。** 旧代码要求 ``MARGIN <= x <= MARGIN + (n-1)*cell``，
          于是左/上侧从第一个格点起算、右/下侧少半格 —— 边线附近的点击
          "偏一边"。这里对外框格点两侧各给半格。
        * **不用 Python 的 ``round()``。** 它对 ``.5`` 走银行家舍入
          （``round(2.5) == 2``、``round(3.5) == 4``），恰好落在两格中间时
          吸附方向会来回跳。这里用 ``floor(v + 0.5)``，方向恒定。
        * **越界返回 ``None``，而不是钳到边缘。** 钳边会让用户点到棋盘外的
          木框上也能落子。
        """
        fx = (x - self.ox) / self.cell
        fy = (y - self.oy) / self.cell
        if not (-0.5 <= fx <= self.n - 0.5 and -0.5 <= fy <= self.n - 0.5):
            return None
        c = min(self.n - 1, max(0, math.floor(fx + 0.5)))
        r = min(self.n - 1, max(0, math.floor(fy + 0.5)))
        return r, c

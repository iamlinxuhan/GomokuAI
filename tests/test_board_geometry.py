# -*- coding: utf-8 -*-
"""``board_geometry.BoardGeometry`` 的正确性。

纯数学，不需要 QApplication —— 只有最后那条"设计基准 == 旧公式"的契约测试
才惰性 import ``main``（它 import PyQt5）。

四组断言各自盯住一类失效：

* **往返** —— ``to_grid(*px(r, c)) == (r, c)``。这一条同时覆盖"格距算错"与
  "原点偏移"，且要在多个控件尺寸下都成立（缩放后仍然自洽）。
* **接受域** —— 半个格宽的边界两侧，以及越界返回 ``None``。旧实现在左/上
  比右/下少半格，是"边线点击偏一边"的根因。
* **``fit`` 不变式** —— 网格始终居中且正方，与设计尺寸等值时是恒等变换。
* **设计基准契约** —— 与旧公式逐像素一致，把"重构不改观感"钉死。
"""

from __future__ import annotations

import math

import pytest

from board_geometry import BoardGeometry
from engine_local import BOARD_SIZE

N = BOARD_SIZE
CELL = 34.0
PAD = 40.0          # 步骤 0 的设计基准留白（= 当时的 MARGIN）

DESIGN = BoardGeometry.design(N, CELL, PAD)

# 覆盖：比设计尺寸小得多 / 非正方 / 正好设计尺寸 / 大屏
SIZES = [(430, 430), (600, 500), (726, 726), (1200, 900)]


# ------------------------------------------------------------------ 设计基准

def test_design_cell_is_exact():
    """设计基准的格距必须**精确**等于传入值，不能有浮点漂移。

    因为渲染、点击、测试三处都依赖这个数，漂移 1e-12 也会让某些断言变成
    "几乎相等"，于是真正的回归被掩盖。
    """
    assert DESIGN.cell == CELL
    assert DESIGN.ox == PAD and DESIGN.oy == PAD
    assert DESIGN.k == 1.0


def test_span_uses_n_minus_1_segments():
    """19 个格点之间是 **18** 段 —— 这是旧几何缺陷的根因，钉死它。"""
    assert DESIGN.span == (N - 1) * CELL == 612.0
    assert DESIGN.span != N * CELL


def test_design_matches_legacy_formula():
    """设计尺寸下 ``px`` 必须与旧公式 ``MARGIN + c*CELL_SIZE`` 逐像素一致。

    这条是"步骤 0 零视觉变化"的证据，也是跨步骤的契约：步骤 3 会把
    ``M.MARGIN`` 从 40 改成 57（网格居中的真正留白），届时本用例自动跟着
    新常量走 —— 它断言的是"``px`` 与 ``main`` 的常量保持同一套语义"，
    而不是某个写死的数字。

    惰性 import ``main``：本文件其余部分是 Qt-free 的，不该为一个契约测试
    让整个文件付出 import PyQt5 的代价。
    """
    import main as M

    geom = BoardGeometry.design(M.BOARD_SIZE, M.CELL_SIZE, M.MARGIN)
    for r in range(M.BOARD_SIZE):
        for c in range(M.BOARD_SIZE):
            assert geom.px(r, c) == (M.MARGIN + c * M.CELL_SIZE,
                                     M.MARGIN + r * M.CELL_SIZE), \
                f"设计基准与旧公式不一致：格点 ({r}, {c})"


# ------------------------------------------------------------------ 往返

@pytest.mark.parametrize("w,h", SIZES)
def test_roundtrip_all_cells(w, h):
    """全部 361 个格点：``to_grid(*px(r, c))`` 必须回到原格。

    这是最有力的一条 —— 它同时排除格距错误与原点偏移，且不依赖任何
    写死的像素值。缩放后仍然成立，说明"绘制"与"命中"共用同一几何。
    """
    geom = BoardGeometry.fit(w, h, DESIGN)
    for r in range(N):
        for c in range(N):
            assert geom.to_grid(*geom.px(r, c)) == (r, c), \
                f"{w}x{h} 尺寸下格点 ({r}, {c}) 往返失败"


@pytest.mark.parametrize("w,h", SIZES)
def test_cell_rect_center_is_the_grid_point(w, h):
    """``cell_rect`` 的中心必须就是 ``px`` —— 局部重绘依赖这个一致性。"""
    geom = BoardGeometry.fit(w, h, DESIGN)
    half = geom.cell / 2
    for r in (0, 5, N - 1):
        for c in (0, 9, N - 1):
            x, y, cw, ch = geom.cell_rect(r, c)
            assert math.isclose(x + cw / 2, geom.px(r, c)[0])
            assert math.isclose(y + ch / 2, geom.px(r, c)[1])
            assert math.isclose(cw, geom.cell) and math.isclose(ch, geom.cell)
            assert math.isclose(x, geom.px(r, c)[0] - half)


# ------------------------------------------------------------------ 接受域

def test_half_cell_beyond_outer_grid_point_still_hits():
    """外框格点向外半格**之内**仍算命中 —— 边线附近更好点。"""
    g = DESIGN
    half = CELL / 2
    for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        r = 0 if dr == 1 else (N - 1 if dr == -1 else 9)
        c = 0 if dc == 1 else (N - 1 if dc == -1 else 9)
        # 从边界格点向外挪 0.4 格（在半个格宽以内）
        x = g.px(r, c)[0] + dc * half * 0.8
        y = g.px(r, c)[1] + dr * half * 0.8
        assert g.to_grid(x, y) == (r, c), \
            f"向外 0.4 格应仍吸附到 ({r}, {c})"


def test_beyond_half_cell_returns_none():
    """超过半格就是棋盘外 —— 不能钳到边缘，否则点到木框上也会落子。"""
    g = DESIGN
    half = CELL / 2
    over = half + 1.0
    assert g.to_grid(g.px(0, 0)[0] - over, g.px(0, 0)[1]) is None
    assert g.to_grid(g.px(0, 0)[0], g.px(0, 0)[1] - over) is None
    assert g.to_grid(g.px(N - 1, N - 1)[0] + over, g.px(9, 9)[1]) is None
    assert g.to_grid(g.px(9, 9)[0], g.px(N - 1, N - 1)[1] + over) is None


def test_boundary_acceptance_is_symmetric():
    """左/上 与 右/下 的接受范围必须一样宽。

    旧实现只在 ``[MARGIN, MARGIN + (n-1)*CELL]`` 内接受，于是左/上从第一个
    格点起算、右/下少半格 —— 边线点击"偏一边"。这里逐边量出实际可接受的
    外侧余量，四边必须相同。
    """
    g = DESIGN
    half = CELL / 2
    step = 0.25

    def outward_limit(r, c, dx, dy):
        """从格点向外走，返回最后一个仍命中的距离。"""
        last = 0.0
        d = 0.0
        x0, y0 = g.px(r, c)
        while d < half * 2:
            d += step
            if g.to_grid(x0 + dx * d, y0 + dy * d) == (r, c):
                last = d
            else:
                break
        return last

    limits = [
        outward_limit(0, 9, 0, -1),        # 上
        outward_limit(N - 1, 9, 0, 1),     # 下
        outward_limit(9, 0, -1, 0),        # 左
        outward_limit(9, N - 1, 1, 0),     # 右
    ]
    assert len(set(limits)) == 1, f"四边外侧余量不一致：{limits}"
    assert limits[0] > half - step, f"外侧余量 {limits[0]} 不足半格"


def test_half_integer_midpoint_does_not_flip():
    """恰好落在两格中间时，吸附方向必须**恒定向下**。

    Python 的 ``round()`` 走银行家舍入（``round(2.5) == 2``、``round(3.5) == 4``），
    于是鼠标在两格之间微动时吸附目标会来回跳。这里用 ``floor(v + 0.5)`` 固定。
    对中点两侧各偏一点点，结果必须连续地指向较大或较小的一侧，而不是交替。
    """
    g = DESIGN
    # 在格点 2 与 3 的正中间
    y = g.px(0, 0)[1]
    mid_x = g.px(0, 2)[0] + CELL / 2
    just_left = g.to_grid(mid_x - 0.01, y)
    just_right = g.to_grid(mid_x + 0.01, y)
    assert just_left == (0, 2), f"中点略偏左应吸附到 2，实得 {just_left}"
    assert just_right == (0, 3), f"中点略偏右应吸附到 3，实得 {just_right}"


# ------------------------------------------------------------------ fit 不变式

@pytest.mark.parametrize("w,h", SIZES)
def test_fit_keeps_square_and_centers(w, h):
    """网格始终正方形、居中对齐；缩放比按短边算。"""
    g = BoardGeometry.fit(w, h, DESIGN)
    left, right = g.ox, w - g.ox - g.span
    top, bottom = g.oy, h - g.oy - g.span
    assert math.isclose(left, right, abs_tol=1e-9), f"水平未居中 {left} vs {right}"
    assert math.isclose(top, bottom, abs_tol=1e-9), f"垂直未居中 {top} vs {bottom}"
    assert g.span > 0


def test_fit_at_design_size_is_identity():
    """控件等于设计尺寸时 ``fit`` 必须是恒等变换。

    这是"设计尺寸下渲染与旧实现逐像素一致"的前提，也让步骤 3 的改动范围
    严格限于"网格居中"这一件事。
    """
    d_side = int(DESIGN.span + 2 * DESIGN.ox)          # 726
    g = BoardGeometry.fit(d_side, d_side, DESIGN)
    assert math.isclose(g.k, 1.0)
    assert math.isclose(g.cell, DESIGN.cell)
    assert math.isclose(g.ox, DESIGN.ox)
    assert math.isclose(g.oy, DESIGN.oy)


def test_fit_scale_is_proportional_to_short_side():
    """缩放比与短边成正比 —— 半尺寸的控件得到约一半的格距。"""
    a = BoardGeometry.fit(726, 726, DESIGN)
    b = BoardGeometry.fit(363, 363, DESIGN)
    assert math.isclose(b.cell / a.cell, 0.5, rel_tol=1e-9)
    assert math.isclose(b.k / a.k, 0.5, rel_tol=1e-9)


def test_stone_radius_leaves_a_gap():
    """相邻棋子之间必须留缝：``2 * stone_radius < cell``。"""
    g = BoardGeometry.fit(726, 726, DESIGN)
    assert 2 * g.stone_radius < g.cell
    assert g.stone_radius > g.cell * 0.4, "太小会显得棋子单薄"


# ------------------------------------------------------------------ 木盘


@pytest.mark.parametrize("w,h", SIZES + [(1400, 726), (726, 1400)])
def test_plate_rect_is_a_centered_square(w, h):
    """木盘恒为正方形且在控件内居中。

    窗口可缩放之后控件不再保证正方（拉宽时 1104x726），若把木盘画成控件
    矩形，木纹会被横向拉伸、圆角会变成椭圆 —— 这是"窗口一拉宽棋盘就变形"
    的根因。
    """
    g = BoardGeometry.fit(w, h, DESIGN)
    x, y, side = g.plate_rect(w, h)
    assert math.isclose(side, min(w, h), rel_tol=1e-12), "木盘边长必须是短边"
    assert math.isclose(x, (w - side) / 2, abs_tol=1e-9), "水平未居中"
    assert math.isclose(y, (h - side) / 2, abs_tol=1e-9), "垂直未居中"


@pytest.mark.parametrize("w,h", SIZES)
def test_plate_contains_the_grid(w, h):
    """网格必须整个落在木盘内，且四周留白相等 —— 坐标标注就在这圈留白上。

    留白不足会让最外圈的字母／数字压到木框甚至被裁掉。
    """
    g = BoardGeometry.fit(w, h, DESIGN)
    x, y, side = g.plate_rect(w, h)
    gx, gy, span, _ = g.rect()
    margin_l = gx - x
    margin_r = (x + side) - (gx + span)
    margin_t = gy - y
    margin_b = (y + side) - (gy + span)
    assert margin_l > 0 and margin_r > 0, "网格超出木盘左右边界"
    assert margin_t > 0 and margin_b > 0, "网格超出木盘上下边界"
    assert math.isclose(margin_l, margin_r, abs_tol=1e-9)
    assert math.isclose(margin_t, margin_b, abs_tol=1e-9)


def test_plate_margin_scales_with_k():
    """木框留白与设计基准成固定比例 —— 缩放时不会"网格贴边"。"""
    for w, h in SIZES:
        g = BoardGeometry.fit(w, h, DESIGN)
        x, _, _ = g.plate_rect(w, h)
        gx = g.rect()[0]
        assert math.isclose(gx - x, DESIGN.ox * g.k, rel_tol=1e-9), (
            f"{w}x{h}: 留白 {gx - x} 应为设计留白 {DESIGN.ox} × k={g.k:.4f}")


# ------------------------------------------------------- 窗口下限的自洽性


def test_min_board_keeps_cell_at_the_floor():
    """窗口下限推出的棋盘宽度，必须让格距**恰好**不小于 ``main.MIN_CELL``。

    这条是"两处下限不许脱钩"的守卫：``main.MIN_BOARD`` 是从"格距 22"
    反推出来的，``BoardWidget.setMinimumSize`` 也用它。任何一边被手改成
    拍脑袋的数字，这里立刻红 —— 否则小屏上格距会悄悄掉到坐标标注糊掉的
    程度，而那只有在 1366x768 的机器上才看得出来。
    """
    import main as M

    side = M.MIN_BOARD
    g = BoardGeometry.fit(side, side, BoardGeometry.design(
        M.BOARD_SIZE, M.CELL_SIZE, M.BOARD_PAD))
    assert g.cell >= M.MIN_CELL - 1e-9, (
        f"MIN_BOARD={side} 只给出格距 {g.cell:.2f}，低于 MIN_CELL={M.MIN_CELL}")
    assert g.cell < M.MIN_CELL + 0.5, (
        f"MIN_BOARD={side} 给出格距 {g.cell:.2f}，比下限宽松太多（下限失效）")


def test_window_minimum_fits_the_pieces():
    """窗口下限必须容得下「棋盘 + 面板 + 边距」，否则布局会压扁面板。"""
    import main as M
    assert M.MIN_W >= M.MIN_BOARD + M.PANEL_W
    assert M.MIN_H >= M.MIN_BOARD
    assert M.MIN_W > M.MIN_H, "19 路棋盘加侧栏，窗口应当是宽大于高"

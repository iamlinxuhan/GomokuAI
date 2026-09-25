# -*- coding: utf-8 -*-
"""``analysis.py`` 的不变式：单调、奇对称、锚点来自引擎、估计值不许装成事实。

## 这些断言防的是什么

面板上会出现一条"棋面胜率（估计）"曲线。它**不是**统计标定的概率，而是
把引擎分值经一次有文档的单调变换得到的估计值。一旦下列任一性质被破坏，
这个估计值就会开始撒谎，而且**看不出来**：

* **单调**——分值更高的局面必须给出更高的胜率。破坏了就会在曲线上出现
  "AI 越走越优、胜率却下跌"的假象。
* **奇对称**——``wp(-s) == 100 - wp(s)``。破坏了说明变换里混进了与分值无关的
  偏置（比如"黑棋加成"），而那个量引擎从未计算过。
* **锚点是引擎常量**——每个拐点都要能指着 ``engine.py`` 的一行解释。
  一旦有人在这里写死一个凑出来的数（"30000 太低了，改成 40000 吧"），
  变换就从"引擎分值的读数"退化成"作者口味"。
* **静态带不到 100%**——``evaluate`` 不含轮次，能在已经输掉的局面里返回
  ≈1e6（你持活四+冲四，对方持冲四且轮到他先成五）。所以静态分永远不许
  显示成必胜；100% 只留给搜索**证明**出来的杀棋分。
"""

from __future__ import annotations

import ast
import math
import os

import pytest

import analysis as A
import engine_local as E

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_A_PATH = os.path.join(_ROOT, "analysis.py")


# ==================== 结构：零 Qt ====================

_QT_BINDINGS = ("PyQt5", "PyQt6", "PySide2", "PySide6")


def test_analysis_stays_qt_free():
    """本模块不许 import 任何 Qt 绑定 —— 一旦引入，下面这些断言就得先起 QApplication。

    走 AST 而不是"扫源码字符串"：本模块的 docstring 里就写着 Qt 绑定的名字
    （就是为了解释这条禁令），字符串扫描会被自己的说明文字误伤 ——
    ``test_no_literal_colors.py`` 里"注释里也不许写颜色"那条教训的另一面：
    当被禁止的东西**必须**被提及时，规则就得看语法而不是看文字。
    ``ast.walk`` 会走到函数体内部，所以"只在某个函数里 import"也躲不过。
    """
    with open(_A_PATH, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=_A_PATH)

    mods: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.append(node.module)

    for m in mods:
        assert not m.split(".")[0] in _QT_BINDINGS, f"analysis.py 引入了 {m}"
    assert "charts" not in [m.split(".")[0] for m in mods], (
        "analysis.py 不该依赖绘制模块 —— 方向是 charts 用 analysis，不是反过来")


# ==================== 锚点表本身 ====================

# 引擎里所有"分值"名字。锚点必须落在其中，不许自创数字。
_ENGINE_SCORES = frozenset(E.LINE_SCORES.values()) | frozenset({
    E.BONUS_DOUBLE_THREE, E.BONUS_FOUR_THREE, E.BONUS_DOUBLE_FOUR,
    E.STATIC_MAX,
})


def test_anchors_are_engine_constants():
    """每个锚点分值都必须是 ``engine`` 的常量，不是这里凑的数。

    这是整套变换唯一的"诚实性来源"：将来有人问"凭什么是 65%"，答案不是
    "作者觉得"，而是"活三 = 30000，而 30000 是引擎给活三定的分"。
    """
    for score, prob in A.WIN_ANCHORS:
        assert score in _ENGINE_SCORES, (
            f"锚点分值 {score} 不是 engine 的常量 —— 锚点表不许自创数字")
        assert 0.0 < prob < 100.0, f"锚点 {score} 的胜率 {prob} 越界"


def test_anchors_are_monotone():
    """分值严格递增、胜率严格递增。两个都破坏了插值就无意义。"""
    scores = [s for s, _ in A.WIN_ANCHORS]
    probs = [p for _, p in A.WIN_ANCHORS]
    assert scores == sorted(scores), scores
    assert len(set(scores)) == len(scores), f"锚点分值有重复：{scores}"
    assert probs == sorted(probs), probs
    assert len(set(probs)) == len(probs), f"锚点胜率有重复：{probs}"


def test_anchor_values_are_reproduced_exactly():
    """插值在每个锚点处必须**精确**落在表里那个值上（不许有半格漂移）。

    分段线性插值很容易在边界上差一档（``bisect_left`` 取错区间就会），
    而错位恰恰发生在最需要准的地方：活三、冲四、活四。
    """
    for score, prob in A.WIN_ANCHORS:
        assert A.win_probability(score) == pytest.approx(prob, abs=1e-9), (
            f"锚点 {score} 应给出 {prob}%")


def test_symlog_knee_is_an_engine_constant():
    """symlog 的线性→对数转折点必须等于眠三分（第一个真正的威胁）。

    小了会让开局前几手全挤在轴线中间，大了会把中局压扁。这个数是**选的**，
    但必须选在一个引擎常量上，理由才讲得清。
    """
    assert A.SYMLOG_W == float(E.LINE_SCORES[E.LV_THREE_SLEEP])


# ==================== 变换本身 ====================

def _grid():
    """覆盖 0 → ±1e7 的稠密对数网格，外加一批线性随机点。"""
    vals = {0.0}
    for exp in range(0, 8):
        base = 10.0 ** exp
        for k in range(1, 40):
            v = base * k
            vals.add(v)
            vals.add(-v)
    return sorted(vals)


def test_monotone_over_dense_log_grid():
    """整条曲线单调不减。这是"分值越高胜率越高"的字面意思。"""
    prev = -1.0
    for s in _grid():
        p = A.win_probability(s)
        assert p >= prev - 1e-12, f"胜率在 {s} 处回落：{prev} -> {p}"
        prev = p


def test_symmetry_about_zero():
    """``wp(-s) == 100 - wp(s)`` **精确**成立（不是 approx）。

    底下 ``evaluate`` 严格零和（``tests/test_eval.py`` 钉着），这里只做了一次
    关于 50% 的镜像，所以这是恒等式而不是近似。用 ``==`` 断言是有意的：
    将来若有人往变换里加一项"黑棋先行 +x%"，两个分支就对称不了，这条会响。
    """
    for s in _grid():
        assert A.win_probability(-s) == 100.0 - A.win_probability(s), (
            f"胜率变换在 {s} 处不对称")


def test_zero_is_fifty():
    """分值 0 就是均势，且它是**定义**出来的中点（log10(0) 无定义）。"""
    assert A.win_probability(0) == 50.0


def test_mate_is_pinned():
    """杀棋分钉到 100 / 0 —— 那是搜索证明的，不是估计的。"""
    assert A.win_probability(E.WIN_SCORE) == 100.0
    assert A.win_probability(-E.WIN_SCORE) == 0.0
    assert A.win_probability(E.STATIC_MAX + 1) == 100.0
    assert A.win_probability(-(E.STATIC_MAX + 1)) == 0.0
    # STATIC_MAX 本身**不是**杀棋分（is_mate 是严格大于），走插值。
    assert A.win_probability(E.STATIC_MAX) < 100.0


def test_static_band_never_claims_certainty():
    """静态带内任何读数都不许到 99%。

    理由见模块 docstring：无 tempo 的静态评估在已经输掉的局面里也能给 ≈1e6。
    这条上限是"胜率图不许把估计说成事实"的最后一处防线。
    """
    for s in _grid():
        if E.is_mate(s):
            continue
        p = A.win_probability(s)
        assert p < 99.0, f"静态分 {s} 读出了 {p}% —— 静态带不许接近 100"
        assert p > 1.0, f"静态分 {s} 读出了 {p}% —— 静态带不许接近 0"


# ==================== 与真实棋型对表 ====================

def _board(black=(), white=()):
    import numpy as np
    m = np.zeros((19, 19), dtype=np.uint8)
    for r, c in black:
        m[r][c] = 1
    for r, c in white:
        m[r][c] = 2
    return E.Board.from_array(m)


_C = 9


def _rel(cells):
    return [(_C + dr, _C + dc) for dr, dc in cells]


def _h(r0, c0, n):
    return [(r0, c0 + i) for i in range(n)]


def _v(r0, c0, n):
    return [(r0 + i, c0) for i in range(n)]


def _eval(black, white=()):
    return E.evaluate(_board(black, white), 1)


# (名字, 黑棋, 白棋, 期望区间)。坐标以天元为原点。
#
# 区间是**实测值放宽**，不是猜的 —— 写下它们时逐个跑过 evaluate。
# 它们钉住的是"曲线上这个位置该读多少"，而匹配的引擎常量写在期望值旁边的
# 注释里。若引擎分值表大改，这里会一起响，那时应重测而不是改宽区间。
_PATTERNS = (
    ("活三",   _rel(_h(0, 0, 3)),                 (),          62.0, 68.0),   # 30000
    ("眠三",   _rel(_h(0, 0, 3)),                 _rel([(0, -1)]), 52.0, 58.0),  # 1000
    ("冲四",   _rel(_h(0, 0, 4)),                 _rel([(0, -1)]), 72.0, 80.0),  # 100000
    ("活四",   _rel(_h(0, 0, 4)),                 (),          95.0, 98.5),   # 1000000
    ("双活三", _rel(_h(0, 0, 3) + _h(2, 0, 3)),   (),          85.0, 93.0),   # BONUS_DOUBLE_THREE
    ("双四",   _rel(_h(0, 0, 4) + _h(2, 0, 4)),   _rel([(0, -1), (2, -1)]),
                                                              93.0, 98.5),   # BONUS_DOUBLE_FOUR
)


def test_real_patterns_land_in_sane_bands():
    """真实棋型必须落在人认可的带内 —— 这是对锚点表的端到端校验。

    锚点表可以自洽（单调、对称）却整体错位；只有拿真局面跑一遍才知道
    "活三读 65%" 是不是真的成立。实测：活三 30090 → 65.03、眠三 1050 →
    55.14、冲四 100080 → 76.01、活四 1000120 → 97.00。
    """
    for name, black, white, lo, hi in _PATTERNS:
        p = A.win_probability(_eval(black, white))
        assert lo <= p <= hi, f"{name} 读出 {p:.2f}%，应在 [{lo}, {hi}] 内"


def test_real_pattern_ladder_is_ordered():
    """真实棋型的胜率必须与棋型的强弱同序。

    "冲四 < 双活三 < 双四 < 活四" 是引擎自己的分档（见 ``engine.BONUS_*``
    的注释），胜率曲线必须复现它，否则图表会在最关键的几手上给出反向信号。
    """
    got = {name: A.win_probability(_eval(b, w))
           for name, b, w, _, _ in _PATTERNS}
    assert (got["活三"] < got["冲四"] < got["双活三"]
            < got["双四"] < got["活四"]), got


# ==================== symlog 纵轴 ====================

def test_symlog_is_odd():
    """奇函数：分值变号时纵轴位置镜像。否则曲线过中线会跳一下。"""
    for decade in (2, 3, 5, 7):
        for s in _grid():
            assert A.symlog_frac(-s, decade) == -A.symlog_frac(s, decade)


def test_symlog_is_monotone_and_saturating():
    """单调递增，且永远钳在 [-1, 1]。杀棋分会撞到 ±1 —— 轴表达不了"比必胜更赢"。"""
    for decade in (2, 3, 5):
        top = 10.0 ** decade
        prev = -2.0
        for k in range(0, 200):
            v = -top * 3 + k * (top * 6 / 199.0)
            f = A.symlog_frac(v, decade)
            assert -1.0 <= f <= 1.0, f"symlog({v}, {decade}) = {f} 越界"
            assert f >= prev - 1e-12, f"symlog 在 {v} 处回落"
            prev = f


def test_symlog_is_linear_near_zero_and_log_like_far():
    """小分值近似线性、大分值近似对数 —— 这正是选 asinh 而不是纯 log 的理由。"""
    decade = 3
    # 远小于转折点时，位置与分值近似成正比。
    a = A.symlog_frac(A.SYMLOG_W * 0.01, decade)
    b = A.symlog_frac(A.SYMLOG_W * 0.02, decade)
    assert b == pytest.approx(2 * a, rel=0.02), (a, b)
    # 跨越一个数量级时，位置增量近似常数（对数特征）。
    d1 = (A.symlog_frac(A.SYMLOG_W * 100, decade)
          - A.symlog_frac(A.SYMLOG_W * 10, decade))
    d2 = (A.symlog_frac(A.SYMLOG_W * 1000, decade)
          - A.symlog_frac(A.SYMLOG_W * 100, decade))
    assert d2 == pytest.approx(d1, rel=0.35), (d1, d2)


def test_needed_decade_only_grows_and_respects_floor():
    """轴的数量级只增不减，且有下限（否则前几手会把轴压成一条线）。"""
    assert A.needed_decade(0) == 2
    assert A.needed_decade(1) == 2
    assert A.needed_decade(12345) == 5
    assert A.needed_decade(-12345) == 5
    assert A.needed_decade(E.WIN_SCORE) == 7
    assert A.needed_decade(50, floor=4) == 4


# ==================== 读数行 ====================

def test_readout_line_stays_short():
    """读数行必须放得下面板一行（12px 等宽下 24 字符就到头了）。

    用极端输入钉住：搜索可能报出巨大的 nodes/nps（长考 + 快机器），
    没有这个约束那行数字会溢出卡片。
    """
    worst = {"depth": 64, "nodes": 999_999_999, "nps": 99_999_999,
             "time_ms": 99_999}
    line = A.readout_line(worst)
    assert len(line) <= 24, f"读数行 {line!r} 长 {len(line)}"
    assert line.isascii(), f"读数行含非 ASCII：{line!r}"


def test_readout_line_survives_missing_and_zero_fields():
    """``info`` 有三个产出点且字段不全，缺字段/全零都不许抛。"""
    assert A.readout_line({}) == "d0 0 0/s 0ms"
    # 空盘分支只有 depth=0, best_val=0；搜索也可能在 depth 1 前被取消。
    assert A.readout_line({"depth": 0, "best_val": 0}) == "d0 0 0/s 0ms"
    assert A.readout_line({"depth": None, "nodes": None, "nps": None,
                           "time_ms": None}) == "d0 0 0/s 0ms"


def test_short_count_and_ms_stay_compact():
    """两个紧凑化助手本身的边界（它们是读数行长度可控的前提）。"""
    assert A.short_count(0) == "0"
    assert A.short_count(999) == "999"
    assert A.short_count(12345) == "12.3k"
    assert A.short_count(1_234_567) == "1.23M"
    # 进位必须跨档：这两个值曾经输出 "1000.0k" / "1000.0M"（7 字符），
    # 正好把读数行撑出卡片。
    assert A.short_count(999_999) == "1.00M"
    assert A.short_count(999_999_999) == "1.00G"
    for v in (999, 1000, 12345, 999_999, 1e6, 999_999_999, 1e9, 1e12):
        assert len(A.short_count(v)) <= 6, (v, A.short_count(v))
        assert len(A.short_count(-v)) <= 6, (v, A.short_count(-v))

    assert A.short_ms(340) == "340ms"
    assert A.short_ms(1500) == "1.5s"
    assert len(A.short_ms(99_999)) <= 6


def test_win_probability_never_returns_nan():
    """任何输入都不许产出 NaN —— 一次 NaN 会让整条曲线消失而不是画歪。"""
    for s in list(_grid()) + [float("-inf"), float("inf")]:
        p = A.win_probability(s)
        assert not math.isnan(p), f"wp({s}) 是 NaN"
        assert 0.0 <= p <= 100.0, f"wp({s}) = {p} 越界"

# -*- coding: utf-8 -*-
"""把引擎分值翻译成人看的量：胜率、对数坐标、紧凑读数。

## 为什么单独一个模块，且**零 Qt**

这里的每一件事都是纯算术，而其中的胜率映射带着一个**诚实性承诺**（见下），
必须能被 pytest 直接钉住 —— 如果它和绘图代码住在一起，想测它就得先起一个
``QApplication``，那条承诺就会慢慢没人验证。``tests/test_analysis.py`` 里有一条
"本模块不许 import 任何 Qt 绑定"的 AST 检查，理由与
``test_no_literal_colors.py`` 相同：约束要在源码层面成立，而不是靠自觉。

## 胜率：这是**有文档的单调变换**，不是标定过的概率

界面上要显示"棋面胜率"，但没有任何数据支持一个统计校准的概率。
所以这里的做法是：**把引擎自己的分值常量当作锚点，在 log10 空间里分段线性插值**。

选这个做法的理由只有一个 —— **每一个拐点都能指着 ``engine.py`` 里的一行解释**。
``WIN_ANCHORS`` 里的每个分值都必须是 ``engine`` 的模块级常量（
``tests/test_analysis.py::test_anchors_are_engine_constants`` 会逐个校验），
这样将来有人质疑"凭什么是 65%"，答案不是"作者觉得"，而是"活三 = 30000，
而 30000 是引擎给活三定的分"。

### 为什么不能是线性 sigmoid

实测 320 个随机局面上 ``|evaluate|`` 的分布：p50 ≈ 1100、p75 ≈ 30000、
p90 ≈ 1e5–5e5、p99 ≈ 1.5e6。跨度从 0 到 1e6 以上，任何线性压缩都会把
整局棋挤在 50% 附近的一个像素里。

### 为什么 50% 是"引擎模型的说法"而不是"棋局的说法"

``evaluate`` 的 docstring 明确拒绝加入 tempo（先行方）常数，因为那会破坏零和，
而零和是 negamax 的前提。所以这里也**不许**加"黑棋先行 +x%"之类的修正 ——
那是在编造一个引擎从未计算过的量。分值 0 就显示 50%，仅此而已。
将来若有人觉得"黑棋应该略优"想加一项，先去看 ``evaluate`` 的 docstring。

### 静态分永远不许显示 100%

``evaluate`` 不含轮次概念，所以它能在一个**已经输了**的局面里返回 ≈1e6
（你持活四 + 冲四，对方持冲四，且轮到对方先成五）。因此静态带内最高只到
``_P_MAX``（98.5），100% / 0% 只留给**已被搜索证明**的杀棋分。
"""

from __future__ import annotations

import bisect
import math

from engine import (BONUS_DOUBLE_FOUR, BONUS_DOUBLE_THREE, BONUS_FOUR_THREE,
                    LINE_SCORES, LV_FOUR, LV_FOUR_LIVE, LV_ONE,
                    LV_THREE_LIVE, LV_THREE_SLEEP, LV_TWO_LIVE, LV_TWO_SLEEP,
                    STATIC_MAX, is_mate)

# ``LV_*`` 是**等级下标**（1、2、3…），分值要去 ``LINE_SCORES`` 里查。
# 写成 LINE_SCORES[LV_XXX] 而不是直接写 10 / 100 / 500，是为了让"锚点分值来自
# 引擎"这件事在源码层面看得见 —— 改引擎的分值表，这里跟着变。
_SC_ONE = LINE_SCORES[LV_ONE]                 # 10
_SC_TWO_SLEEP = LINE_SCORES[LV_TWO_SLEEP]     # 100
_SC_TWO_LIVE = LINE_SCORES[LV_TWO_LIVE]       # 500
_SC_THREE_SLEEP = LINE_SCORES[LV_THREE_SLEEP]  # 1000
_SC_THREE_LIVE = LINE_SCORES[LV_THREE_LIVE]   # 30000
_SC_FOUR = LINE_SCORES[LV_FOUR]               # 100000
_SC_FOUR_LIVE = LINE_SCORES[LV_FOUR_LIVE]     # 1000000

# 静态带的上限概率。**必须小于 100** —— 见模块 docstring 里"静态分不含轮次"那段。
_P_MAX = 98.5

# 锚点表：(引擎分值, 该分值对应的估计胜率%)。分值必须严格递增。
#
# 几个刻意的取值：
#
# * **冲四(LV_FOUR) 只给 76%，不给 85%。** 冲四是将军不是赢棋：对手挡一手就
#   消耗掉了，局面回到原样只多一个先手。85% 会把一个"被迫的、不充分的威胁"
#   说成接近必胜。
# * **BONUS_* 三个拐点不能省。** 引擎自己认定双四/四三/双活三与单独一个冲四
#   不是一回事（见 engine.py 的 BONUS_* 注释），不给拐点的话 100000 → 1000000
#   会是一根长直线，反而低估了引擎的分档。
#   拐点取在 BONUS 值本身而不是实际总分（双活三实测 560180、四三 730170、
#   双四 900160）—— 总分随参与成型的线而变，BONUS 不随。
# * **活四(LV_FOUR_LIVE) 97%，不到 98.5。** 活四两头都能成五，挡不住；但理论上
#   仍可能被对方先成五，所以留给它一点余量。
WIN_ANCHORS = (
    (_SC_ONE, 50.5),                # 10        活一
    (_SC_TWO_SLEEP, 51.0),          # 100       眠二
    (_SC_TWO_LIVE, 53.0),           # 500       活二
    (_SC_THREE_SLEEP, 55.0),        # 1000      眠三
    (_SC_THREE_LIVE, 65.0),         # 30000     活三
    (_SC_FOUR, 76.0),               # 100000    冲四
    (BONUS_DOUBLE_THREE, 88.0),     # 500000    双活三
    (BONUS_FOUR_THREE, 91.0),       # 600000    四三
    (BONUS_DOUBLE_FOUR, 93.0),      # 700000    双四
    (_SC_FOUR_LIVE, 97.0),          # 1000000   活四
    (STATIC_MAX, _P_MAX),           # 静态钳位：永远不到 100
)

_LOG_SCORES = tuple(math.log10(s) for s, _ in WIN_ANCHORS)
_PROBS = tuple(p for _, p in WIN_ANCHORS)


def win_probability(score: float) -> float:
    """引擎分值 → 玩家视角的估计胜率（0..100）。单调、奇对称、无隐藏状态。

    ``wp(-s) == 100 - wp(s)`` **精确成立**（见 ``test_symmetry_about_zero``）——
    这条成立是因为底下的分值取自严格零和的 ``evaluate``，而这里只做了一次
    关于 50% 的镜像。

    杀棋分（``is_mate``）直接钉到 100 / 0：那是搜索**证明**出来的，不是估计。

    它**不是**统计标定的概率。图例与 tooltip 必须照实说明。
    """
    if is_mate(score):
        return 100.0 if score > 0 else 0.0
    if score == 0:
        # log10(0) 无定义，且分值 0 就是均势 —— 50 是定义出来的中点。
        return 50.0

    sign = 1.0 if score > 0 else -1.0
    x = math.log10(abs(score))
    if x <= _LOG_SCORES[0]:
        p = _PROBS[0]
    elif x >= _LOG_SCORES[-1]:
        # 静态分被钳在 ±STATIC_MAX，正常走不到这里；留着是为了永不返回 NaN。
        p = _PROBS[-1]
    else:
        i = bisect.bisect_left(_LOG_SCORES, x)
        x0, x1 = _LOG_SCORES[i - 1], _LOG_SCORES[i]
        p = _PROBS[i - 1] + (x - x0) / (x1 - x0) * (_PROBS[i] - _PROBS[i - 1])
    return 50.0 + sign * (p - 50.0)


# ==================== 分值图的对数轴 ====================

# 线性→对数的转折点。取 LV_THREE_SLEEP（眠三）——**第一个真正的威胁**。
# 再小就退化成纯对数（开局前几手全挤在中间），再大又把中局压扁。
SYMLOG_W = float(_SC_THREE_SLEEP)


def symlog_frac(value: float, decade: int) -> float:
    """分值在纵轴上的位置，值域 [-1, 1]（0 在中线）。

    用 ``asinh`` 而不是纯 ``log10``：它是**奇函数且过零点光滑**，
    分值变号时曲线不会在中间跳一下；小分值近似线性、大分值近似对数。

    ``decade`` 是当前轴要覆盖的数量级（见 ``needed_decade``）。杀棋分会钳在
    ±1 —— 轴本来就表达不了"比必胜更赢"，钳住是诚实的。
    """
    top = math.asinh(10.0 ** decade / SYMLOG_W)
    return max(-1.0, min(1.0, math.asinh(value / SYMLOG_W) / top))


def needed_decade(value: float, *, floor: int = 2) -> int:
    """要显示 ``|value|`` 至少需要几个数量级。``floor`` 是轴的最小数量级。"""
    return max(floor, math.ceil(math.log10(max(1.0, abs(value)))))


# ==================== 紧凑读数 ====================

_UNITS = ((1e3, "k"), (1e6, "M"), (1e9, "G"), (1e12, "T"))


def short_count(n: float) -> str:
    """大整数压到 ≤6 字符：``12345 -> 12.3k``、``1234567 -> 1.23M``。

    进位要**跨档**，否则会多出一个字符。``999_999_999`` 除以 1e6 得
    999.999999，``:.1f`` 进位成 ``1000.0`` —— 7 个字符，正好把读数行撑出卡片。
    实测过的两个坏值：``999999 -> "1000.0k"``、``999999999 -> "1000.0M"``。
    """
    n = float(n)
    a = abs(n)
    if a < _UNITS[0][0]:
        return str(int(n))
    idx = 0
    while idx + 1 < len(_UNITS) and a >= _UNITS[idx][0] * 1000.0:
        idx += 1
    limit, suffix = _UNITS[idx]
    v = n / limit
    txt = f"{v:.2f}" if abs(v) < 10 else f"{v:.1f}"
    if abs(float(txt)) >= 1000.0 and idx + 1 < len(_UNITS):
        limit, suffix = _UNITS[idx + 1]
        v = n / limit
        txt = f"{v:.2f}" if abs(v) < 10 else f"{v:.1f}"
    return txt + suffix


def short_ms(ms: float) -> str:
    """毫秒压到 ≤6 字符：``340 -> 340ms``、``1500 -> 1.5s``。"""
    if ms >= 10_000:
        return f"{ms / 1000.0:.0f}s"
    if ms >= 1000:
        return f"{ms / 1000.0:.1f}s"
    return f"{int(ms)}ms"


def readout_line(info: dict) -> str:
    """一行搜索读数：``d3 12.4k 2.1M/s 340ms``。

    **只用 ASCII**：这行由画笔或等宽字体渲染，而 ``theme.mono_font()`` 只能设
    候选链的**第一个**族（Qt5 没有 ``setFamilies``），中文得靠逐字回退，
    在这里不自找麻烦。

    长度必须可控 —— 面板一行只有约 198px，12px 等宽下 24 个字符就接近上限。
    ``test_readout_line_stays_short`` 用极端输入钉住这一点。
    """
    depth = int(info.get("depth", 0) or 0)
    nodes = info.get("nodes", 0) or 0
    nps = info.get("nps", 0) or 0
    ms = info.get("time_ms", 0.0) or 0.0
    return (f"d{depth} {short_count(nodes)} "
            f"{short_count(nps)}/s {short_ms(ms)}")

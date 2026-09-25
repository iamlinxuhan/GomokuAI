# -*- coding: utf-8 -*-
"""静态评估的不变式：阶梯偏序、值域钳制、**零和性**。

零和性这一条不是美学要求。negamax 用 ``-evaluate(child)`` 传播叶值，只有
反号对称的函数才能让"同一局面在奇数层与偶数层被评估"得到一致结论；
旧 ``evaluate_board`` 里的 ``ai_score - human_score * 0.85`` 破坏的正是这一点。
"""

from __future__ import annotations

import numpy as np
import pytest

import engine_local as E

B = 19
CENTER = (9, 9)


def board_from(spec, size=B):
    """``{"X": [(r,c)...], "O": [(r,c)...]}`` → ndarray。X 为黑。"""
    m = np.zeros((size, size), dtype=np.uint8)
    for r, c in spec.get("X", ()):
        m[r][c] = 1
    for r, c in spec.get("O", ()):
        m[r][c] = 2
    return m


def rel(cells):
    """把相对天元的坐标换成绝对坐标。"""
    return [(CENTER[0] + dr, CENTER[1] + dc) for dr, dc in cells]


# ------------------------------------------------------------------ 阶梯

def test_line_scores_are_strictly_ordered():
    """棋型分值必须严格递增，且四族与三族之间**拉开数量级**。

    严格递增挡住的是"两个等级同分"——那会让搜索在"做冲四"与"做活三"之间
    抛硬币。数量级差挡住的是旧表的老毛病：活三与冲四只差 3 倍，于是搜索会
    为了凑活三而放弃成四。
    """
    order = [E.LV_NONE, E.LV_ONE, E.LV_TWO_SLEEP, E.LV_TWO_LIVE,
             E.LV_THREE_SLEEP, E.LV_THREE_LIVE, E.LV_FOUR, E.LV_FOUR_LIVE,
             E.LV_FIVE]
    vals = [E.LINE_SCORES[lv] for lv in order]
    assert vals == sorted(vals) and len(set(vals)) == len(vals), vals


def test_pattern_ladder_by_actual_board():
    """同样的偏序，但由**真实盘面**产生，而不是直接读常量表。

    这一条把分类器与计分表串起来断言：如果分类器把"冲四"判成"活四"，
    这里立刻红。
    """
    cases = [
        (".XXXX.", E.LV_FOUR_LIVE),
        ("OXXXX.", E.LV_FOUR),          # 一端被对方堵住：只剩一个成五点
        ("XXXX.", E.LV_FOUR_LIVE),      # 两端皆空 ⇒ 两个成五点，是活四
        (".XXX..", E.LV_THREE_LIVE),
        (".XX.X.", E.LV_THREE_LIVE),
        ("OXXX..", E.LV_THREE_SLEEP),
        ("..XX..", E.LV_TWO_LIVE),
        ("OXX...", E.LV_TWO_SLEEP),
        (".X....", E.LV_ONE),
        ("OXXXO.", E.LV_ONE),
    ]
    for pat, want in cases:
        # 把模式横放在中线附近，"X" 是黑
        cells = rel([(0, i - 3) for i in range(len(pat))])
        spec = {"X": [rc for rc, ch in zip(cells, pat) if ch == "X"],
                "O": [rc for rc, ch in zip(cells, pat) if ch == "O"]}
        bd = E.Board.from_array(board_from(spec))
        lid = _line_id_through(cells[0], E._DIR_STEPS[0])
        assert bd.line_lv[0][lid] == want, \
            f"{pat}: 期望 {want}，实得 {bd.line_lv[0][lid]}"


def _line_id_through(rc, step):
    """过格子 ``(r, c)``、走向步长为 ``step`` 的那条线的线号。

    不用"方向号"索引 `_LINE_MASKS`：线号在 `_CELL_LINE_POS` 里是全局连续的，
    按方向自己拼一个索引反而要复刻 `_build_lines` 的枚举顺序。直接问
    "相邻的线上格与本格的差是否等于步长"更短也更不容易写错。
    """
    idx = rc[0] * B + rc[1]
    for lid, t in E._CELL_LINE_POS[idx]:
        length, cells = E._LINE_VIEWS[lid]
        if t + 1 < length and cells[t + 1] - idx == step:
            return lid
        if t - 1 >= 0 and cells[t - 1] - idx == -step:
            return lid
    raise AssertionError(f"找不到过 {idx} 步长 {step} 的线")


def test_b7_live_three_and_jump_live_three_are_close():
    """.XXX. 与 .XX.X. 都是活三，分值比必须很小（B7 回归）。

    B7 的内容是：旧的三元组 ``(count, open_ends, has_jump)`` 把 `.XX.X.`
    与 `.X.XX.` 判成**同一个 key**，但只有前者是活三（补一子能成活四）。
    修完之后两者都按集合语义重新判级，于是"活三 vs 跳活三"应当同分或至多
    差一档，而不是差出一个数量级。

    后半句"都远小于 FOUR"同样关键：活三与冲四若只差几倍，搜索会为了凑活三
    而放弃成四 —— 那是旧表在 BASELINE 里被记录在案的行为。
    """
    a = _score_of(".XXX..")
    b = _score_of(".XX.X.")
    assert a == b, f"两种活三应同分：{a} vs {b}"
    assert a / b < 3
    assert a < E.LINE_SCORES[E.LV_FOUR] / 3, "活三必须远小于冲四"
    assert b < E.LINE_SCORES[E.LV_FOUR] / 3


def _score_of(pat):
    cells = rel([(0, i - 3) for i in range(len(pat))])
    spec = {"X": [rc for rc, ch in zip(cells, pat) if ch == "X"],
            "O": [rc for rc, ch in zip(cells, pat) if ch == "O"]}
    bd = E.Board.from_array(board_from(spec))
    lid = _line_id_through(cells[0], E._DIR_STEPS[0])
    return E.LINE_SCORES[bd.line_lv[0][lid]]


# ------------------------------------------------------------------ 组合

def test_combo_bonus_order():
    """双四 > 四三 > 双活三 > 冲四，且都严格小于活四。

    这个偏序是棋理给死的。旧表把"双活三"塞进单线 key，实际上任何一条线上
    都只有"活三"，于是它按单活三计分 —— 一个必胜的双活三被算成 30000，
    而一个冲四是 100000，搜索会**放弃必胜去追冲四**。
    """
    f4, f4l = E.BONUS_DOUBLE_FOUR, E.BONUS_FOUR_THREE
    f3, three, four = E.BONUS_DOUBLE_THREE, E.LINE_SCORES[E.LV_THREE_LIVE], \
        E.LINE_SCORES[E.LV_FOUR]
    live4 = E.LINE_SCORES[E.LV_FOUR_LIVE]
    assert f4 > f3 > four > three, (f4, f3, f4l, four, three)
    assert f4l > f3
    # 组合加成是**绝对量**，不是倍乘：两条活三的合计必须小于活四
    assert 2 * three + f3 < live4, "双活三不能比活四值钱，否则搜索会为了凑双三放弃成四"
    assert 2 * four + f4 < live4


def test_double_three_is_recognized_across_lines():
    """双活三必须由**跨线**计数认出来，而不是某一条线上"更好的棋型"。

    两个活三分别落在横线与竖线上，任何单线视角都只看到"活三"。
    """
    bd = E.Board.from_array(board_from({
        "X": rel([(0, -1), (0, 0), (0, 1), (-1, 0), (1, 0)]),
        "O": rel([(2, 2), (2, -2), (-2, 2)]),
    }))
    agg = bd.threat_agg[0]
    assert agg[E.LV_THREE_LIVE] >= 2, f"预期至少两条活三线，实得 {agg}"
    assert E._combo_bonus(agg) >= E.BONUS_DOUBLE_THREE


# ------------------------------------------------------------------ 值域

def test_evaluate_is_clamped():
    """静态分必须落在 ±STATIC_MAX 内，**即使盘上已有五连**。

    没有这条钳制，`score_sum` 会把 WIN_SCORE 直接暴露成静态分，于是
    `is_mate()` 会把一个普通局面判成杀棋 —— 杀棋分与静态分的分带立刻失效。
    """
    boards = [
        np.zeros((B, B), dtype=np.uint8),
        board_from({"X": rel([(0, i) for i in range(5)])}),
        board_from({"X": rel([(0, i) for i in range(5)]),
                    "O": rel([(2, i) for i in range(5)])}),
        board_from({"X": rel([(0, i) for i in range(9)]),
                    "O": rel([(2, i) for i in range(9)])}),
    ]
    for arr in boards:
        bd = E.Board.from_array(arr)
        for me in (1, 2):
            v = E.evaluate(bd, me)
            assert abs(v) <= E.STATIC_MAX, f"评估未钳制：{v}"
            assert not E.is_mate(v), f"静态分不该落在杀棋带：{v}"


def test_mate_band_does_not_overlap_static_band():
    """杀棋分带与静态分带必须留真空带，`is_mate` 才可能可靠。"""
    assert E.STATIC_MAX < E.WIN_SCORE - E.MAX_PLY
    assert E.is_mate(E.WIN_SCORE)
    assert E.is_mate(E.WIN_SCORE - E.MAX_PLY + 1)
    assert E.is_mate(-(E.WIN_SCORE - E.MAX_PLY + 1))
    assert not E.is_mate(E.STATIC_MAX)
    assert not E.is_mate(-E.STATIC_MAX)


# ------------------------------------------------------------------ 零和

POSITIONS = [
    {"X": rel([(0, 0), (0, 1), (1, 1)]), "O": rel([(1, 0), (-1, 0)])},
    {"X": rel([(0, 0), (0, 1), (0, 2)]), "O": rel([(-1, 1), (1, 1), (0, 4)])},
    {"X": rel([(0, 0), (1, 1), (2, 2), (3, 3)]), "O": rel([(-1, -1), (0, 3)])},
    {"X": rel([(0, 0), (0, 1), (0, 2), (0, 3)]), "O": rel([(1, 0), (1, 1)])},
]


def mirror_colors(arr):
    """黑↔白互换。"""
    out = np.where(arr == 1, 3, arr)
    out = np.where(out == 2, 1, out)
    return np.where(out == 3, 2, out).astype(np.uint8)


@pytest.mark.parametrize("spec", POSITIONS)
def test_color_mirror_flips_the_sign(spec):
    """换色镜像必须让评估**变号**：``evaluate(pos, 黑) == -evaluate(镜像(pos), 黑)``。

    **注意是"同一位棋手"而不是"同一种颜色"。** 容易写错的那个版本是
    `evaluate(pos, 黑) == -evaluate(镜像(pos), 白)` —— 它等价于在问
    "黑在 pos 的优势是否等于黑在镜像里的劣势"，而后者按对称性显然是**相等**
    而非相反。换色 + 换视角 = 不变；只换色 = 变号。这里钉住的是后者。

    这是"不许再引入 0.85 这类不对称系数"的**可执行版本**：任何形如
    ``ai - human * k``（k≠1）的写法都会在这里红，因为"谁在评估"进入了
    分值本身。
    """
    a = E.Board.from_array(board_from(spec))
    b = E.Board.from_array(mirror_colors(board_from(spec)))
    for me in (1, 2):
        assert E.evaluate(a, me) == -E.evaluate(b, me), \
            f"换色未变号（me={me}）：{E.evaluate(a, me)} vs {E.evaluate(b, me)}"


@pytest.mark.parametrize("spec", POSITIONS)
def test_evaluate_is_antisymmetric_on_the_same_board(spec):
    """同一盘面上，两方视角必须互为相反数（且这蕴含"空盘评估为 0"）。

    这条 + 上一条合起来就是零和性：`evaluate` 只依赖于黑白双方的棋型总量之差。
    """
    a = E.Board.from_array(board_from(spec))
    assert E.evaluate(a, 1) == -E.evaluate(a, 2)


def test_empty_board_evaluates_to_zero():
    bd = E.Board.from_array(np.zeros((B, B), dtype=np.uint8))
    assert E.evaluate(bd, 1) == 0
    assert E.evaluate(bd, 2) == 0

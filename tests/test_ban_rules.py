# -*- coding: utf-8 -*-
"""禁手规则（``ban_rules``）的固定局面表。

这里的每一条都是**手工推演过**的局面，不是"跑一遍记下输出"。禁手判定错一格
的后果是"合法的一手被判负"或"禁手被放过"，两者都只在特定棋形下才显形 ——
所以用例刻意做成小局面，每条注释都写明为什么这个结论对。

``cpp/src/ban.h`` 是同一套规则的 C++ 实现，两边靠这张表对齐（见
``tests/test_ban_parity.py``）。
"""

from __future__ import annotations

import ban_rules as R


def _board(*stones):
    """19×19 空盘 + ``(r, c, player)`` 列表。"""
    b = [[0] * 19 for _ in range(19)]
    for r, c, p in stones:
        b[r][c] = p
    return b


def _blk(*rcs):
    return [(r, c, R.BLACK) for r, c in rcs]


def _wht(*rcs):
    return [(r, c, R.WHITE) for r, c in rcs]


# ---------------------------------------------------------------- 五连优先


def test_exact_five_is_a_win_not_a_foul():
    """恰好五连 —— 黑胜，任何禁手都不成立。"""
    b = _board(*_blk((9, 5), (9, 6), (9, 7), (9, 8)))
    assert R.five_at(b, 9, 9, R.BLACK)
    assert R.foul_kind(b, 9, 9, R.BLACK) == R.FOUL_NONE
    assert R.judgement(b, 9, 9, R.BLACK) == "win"


def test_overline_six_is_a_foul():
    """六连 = 长连禁手（本项目无禁手引擎把它当五，禁手模式必须分开）。"""
    b = _board(*_blk((9, 5), (9, 6), (9, 7), (9, 8), (9, 10)))
    assert not R.five_at(b, 9, 9, R.BLACK)
    assert R.foul_kind(b, 9, 9, R.BLACK) == R.FOUL_OVERLINE
    assert R.judgement(b, 9, 9, R.BLACK) == "foul"


def test_five_beats_overline_on_the_same_move():
    """一手同时造出六连（横）与五连（竖）—— **五连优先，黑胜**。

    这正是 RIF Rule 9.2 那句 "without at the same time attaining five in a row"
    的用处：若长连优先，六连的任意五格子窗口都算五连，"长连禁手"永远触发不了。
    """
    b = _board(*_blk((9, 5), (9, 6), (9, 7), (9, 8), (9, 10),
                     (5, 9), (6, 9), (7, 9), (8, 9)))
    assert R.foul_kind(b, 9, 9, R.BLACK) == R.FOUL_NONE
    assert R.judgement(b, 9, 9, R.BLACK) == "win"


def test_overline_against_the_wall():
    """贴墙的长连照样是长连 —— 线的端点就是墙，不是可以无限延伸的空位。"""
    b = _board(*_blk((0, 0), (0, 1), (0, 2), (0, 3), (0, 4)))
    assert R.foul_kind(b, 0, 5, R.BLACK) == R.FOUL_OVERLINE


def test_edge_five_is_still_a_win():
    b = _board(*_blk((0, 0), (0, 1), (0, 2), (0, 3)))
    assert R.foul_kind(b, 0, 4, R.BLACK) == R.FOUL_NONE
    assert R.judgement(b, 0, 4, R.BLACK) == "win"


# ---------------------------------------------------------------- 四四


def test_double_four_across_two_lines():
    """横一条冲四 + 竖一条冲四 → 四四禁手。

    横：``(9,2)(9,3)(9,4)``，走 ``(9,5)``；``(9,6)`` 白子挡死活四，只剩
    ``(9,1)`` 一个成五点 ⇒ 冲四。
    竖：``(5,5)(6,5)(7,5)``，走 ``(9,5)``；``(8,5)`` 空 ⇒ 四点在下那个空位。
    """
    b = _board(*_blk((9, 2), (9, 3), (9, 4), (5, 5), (6, 5), (7, 5)),
               *_wht((9, 6)))
    assert not R.five_at(b, 9, 5, R.BLACK)
    assert R.foul_kind(b, 9, 5, R.BLACK) == R.FOUL_DOUBLE_FOUR


def test_double_four_on_one_line():
    """**同线双四**（「变形四四」）也是禁手。

    黑在 ``(9,0)(9,2)(9,4)(9,6)``，走 ``(9,3)``：
      ``[0..4]`` 的 4 子是 ``{0,2,3,4}``，补 ``(9,1)`` 成五；
      ``[2..6]`` 的 4 子是 ``{2,3,4,6}``，补 ``(9,5)`` 成五。
    两组子不同 ⇒ 两个四，且都含刚落这一子。
    """
    b = _board(*_blk((9, 0), (9, 2), (9, 4), (9, 6)))
    assert R.foul_kind(b, 9, 3, R.BLACK) == R.FOUL_DOUBLE_FOUR


def test_live_four_counts_as_one_four():
    """活四 ``_XXXX_`` 有两个成五点，但只算**一个**四 —— 不是四四。"""
    b = _board(*_blk((9, 3), (9, 4), (9, 5)))
    assert R.foul_kind(b, 9, 6, R.BLACK) == R.FOUL_NONE


def test_four_that_can_only_make_an_overline_is_not_a_four():
    """补成五会变长连的那个"四"不成立 —— 于是只剩一个四，不是四四。

    黑 ``(9,1)(9,2)(9,3)(9,4)``，走 ``(9,6)``：``[2..6]`` 那组
    ``{2,3,4,6}`` 要补 ``(9,5)``，而 ``(9,5)`` 补出来的是 ``1..6`` 的六连，
    根本成不了五；真正成五的只有 ``{1,2,3,4}`` 补 ``(9,0)`` 那一组。
    """
    b = _board(*_blk((9, 1), (9, 2), (9, 3), (9, 4)))
    assert R.foul_kind(b, 9, 6, R.BLACK) == R.FOUL_NONE


def test_four_three_is_legal():
    """四三 —— 黑方的正常胜形，不是禁手。"""
    b = _board(*_blk((9, 2), (9, 3), (9, 4), (6, 5), (7, 5)),
               *_wht((9, 6)))
    assert R.foul_kind(b, 9, 5, R.BLACK) == R.FOUL_NONE


# ---------------------------------------------------------------- 三三


def test_double_three_across_two_lines():
    """横一条活三 + 竖一条活三 → 三三禁手。"""
    b = _board(*_blk((9, 3), (9, 4), (6, 5), (7, 5)))
    assert R.foul_kind(b, 9, 5, R.BLACK) == R.FOUL_DOUBLE_THREE


def test_three_is_deduped_by_stones_not_by_ways():
    """``_XXX_`` 补左补右都能成活四，但那是**同一组 3 颗子** ⇒ 一个活三。"""
    b = _board(*_blk((9, 3), (9, 4)))
    # 只有一条线有活三，其余三向是 1 颗孤子 —— 不是三三。
    assert R.foul_kind(b, 9, 5, R.BLACK) == R.FOUL_NONE


def test_fake_open_three_does_not_count():
    """**假活三**：补出来的"活四"一端补五是长连，所以那根本不是活四。

    黑 ``(9,0)(9,2)(9,3)`` 走 ``(9,5)``：``{2,3,5}`` 补 ``(9,4)`` 看似成
    ``2,3,4,5`` 的活四，但左端补 ``(9,1)`` 会连上 ``(9,0)`` 变成 6 连 ——
    补不出五，因此这一组不是活三。
    竖线 ``(6,5)(7,5)`` 补 ``(9,5)`` 是真活三。
    于是只有 1 个活三 ⇒ 不是三三。
    """
    b = _board(*_blk((9, 0), (9, 2), (9, 3), (6, 5), (7, 5)))
    assert R.foul_kind(b, 9, 5, R.BLACK) == R.FOUL_NONE


# ---------------------------------------------------------------- 白方与边界


def test_white_has_no_forbidden_moves():
    """白方无禁手 —— 同一个四四局面，白方照下不误。"""
    b = _board(*_blk((9, 2), (9, 3), (9, 4), (5, 5), (6, 5), (7, 5)),
               *_wht((9, 6)))
    assert R.foul_kind(b, 9, 5, R.WHITE) == R.FOUL_NONE
    assert R.judgement(b, 9, 5, R.WHITE) == "continue"


def test_occupied_point_is_not_judged():
    b = _board(*_blk((9, 3), (9, 4), (9, 5), (9, 6)))
    assert R.foul_kind(b, 9, 5, R.BLACK) == R.FOUL_NONE


def test_empty_board_has_no_forbidden_points():
    assert R.forbidden_points(_board()) == []


def test_single_stone_is_never_forbidden():
    b = _board(*_blk((9, 9)))
    assert R.foul_kind(b, 9, 10, R.BLACK) == R.FOUL_NONE


def test_forbidden_points_lists_the_foul_point():
    b = _board(*_blk((9, 3), (9, 4), (6, 5), (7, 5)))
    assert (9, 5) in R.forbidden_points(b, R.BLACK)
    assert R.forbidden_points(b, R.WHITE) == []


def test_foul_kind_out_of_range_is_none():
    b = _board()
    assert R.foul_kind(b, -1, 0, R.BLACK) == R.FOUL_NONE
    assert R.foul_kind(b, 19, 0, R.BLACK) == R.FOUL_NONE


# ------------------------------------------------------- 一条线上的四与三计数


def test_same_line_double_three_does_not_exist():
    """**同线双三不存在** —— 这条不是定义，而是穷举出来的事实。

    搜索侧的禁手预筛依赖它：预筛只看"有没有方向达到四"与"活三以上方向的条数"，
    一旦同线双三存在（它只会在**一条**线上留下活三），预筛就会漏掉那一手。
    ``≤6 颗子``以内的全部组合都没跑出同线双三，而同线双四有 17 个实例 ——
    两个结论一起说明预筛的两个分支各管什么。
    """
    import itertools

    line_len, center = 13, 6
    double_three = 0
    double_four = 0
    for k in range(0, 6):
        for combo in itertools.combinations(
                [i for i in range(line_len) if i != center], k):
            cells = [0] * line_len
            for i in combo:
                cells[i] = R.BLACK
            cells[center] = R.BLACK
            n_four, n_three = R._line_judge(list(cells), center, R.BLACK)
            if n_three >= 2 and n_four == 0:
                double_three += 1
            if n_four >= 2:
                double_four += 1
    assert double_three == 0, f"同线双三有 {double_three} 个实例，预筛需要改"
    assert double_four > 0, "同线双四一个实例都没有？那 _four_sets 的记法有问题"


def test_four_counting_matches_an_independent_formulation():
    """``_four_sets`` 与**另一种写法**逐例对齐。

    这里的参考实现换了完全不同的出发点：**先枚举"补上就成五"的空点 e，
    再看 e 所在的那条极大五连段**，把该段去掉 e 剩下的 4 颗子记作一个四。
    被测实现则是"扫 5 窗，4 己方 + 1 空"。两种出发点独立，结论必须一致
    —— 包括"活四只算一个四"（段唯一）与"``X_XXX_X`` 算两个四"（两组子不同）。
    """
    import itertools
    import random

    rng = random.Random(20261014)

    def reference(cells, center, me):
        n = len(cells)
        cells = list(cells)
        out = set()
        for e in range(n):
            if cells[e] != 0:
                continue
            cells[e] = me
            try:
                for start in range(max(0, e - 4), min(n - 5, e) + 1):
                    if any(cells[start + i] != me for i in range(5)):
                        continue
                    if start - 1 >= 0 and cells[start - 1] == me:
                        continue                     # 是长连的一部分，不是五连
                    if start + 5 < n and cells[start + 5] == me:
                        continue
                    four = tuple(start + i for i in range(5) if start + i != e)
                    if center in four:
                        out.add(four)
            finally:
                cells[e] = 0
        return out

    checked = 0
    for _ in range(1000):
        cells = [rng.choice((0, 0, 0, R.BLACK, R.WHITE)) for _ in range(13)]
        if cells[6] != 0:
            continue
        cells[6] = R.BLACK
        got = R._four_sets(list(cells), 6, R.BLACK)
        want = reference(cells, 6, R.BLACK)
        assert got == want, (cells, got, want)
        checked += 1
    assert checked > 550, "随机局面取样太少，这条断言没验到东西"


def test_foul_and_win_are_mutually_exclusive_on_random_positions():
    """随机局面上的一批不变量。

    最要紧的一条：**一手不可能既成五又是禁手** —— 那正是"五连优先"这条规则
    的实质。另外顺带钉住白方无禁手与"occupied 点不判"。
    """
    import random

    rng = random.Random(20261015)
    for _ in range(300):
        b = [[0] * 19 for _ in range(19)]
        for _ in range(rng.randint(1, 12)):
            b[rng.randrange(19)][rng.randrange(19)] = rng.choice((R.BLACK, R.WHITE))
        for _ in range(6):
            r, c = rng.randrange(19), rng.randrange(19)
            if b[r][c] != 0:
                continue
            kind = R.foul_kind(b, r, c, R.BLACK)
            win = R.five_at(b, r, c, R.BLACK)
            assert not (kind != R.FOUL_NONE and win), (b, r, c)
            assert R.foul_kind(b, r, c, R.WHITE) == R.FOUL_NONE
            # 禁手点一定不是"落子即成五"的点
            if kind != R.FOUL_NONE:
                assert not win


def test_forbidden_points_agrees_with_foul_kind():
    """``forbidden_points`` 就是逐点 ``foul_kind`` 的汇总，不能有第二套判据。"""
    import random

    rng = random.Random(20261016)
    b = [[0] * 19 for _ in range(19)]
    for _ in range(10):
        b[rng.randrange(19)][rng.randrange(19)] = rng.choice((R.BLACK, R.WHITE))
    listed = set(R.forbidden_points(b, R.BLACK))
    manual = {(r, c)
              for r in range(19) for c in range(19)
              if b[r][c] == 0 and R.foul_kind(b, r, c, R.BLACK) != R.FOUL_NONE}
    assert listed == manual


def test_two_fours_on_one_line_need_distinct_stone_sets():
    """``X_XXX_X`` 型：两组 4 子位置不同 ⇒ 同线双四。

    ``_four_sets`` 按**子集合**去重而不是按成五点去重，这条与上一条
    （活四只算一个）是同一个判据的两面。
    """
    b = _board(*_blk((9, 0), (9, 2), (9, 4), (9, 6)))
    assert R.foul_kind(b, 9, 3, R.BLACK) == R.FOUL_DOUBLE_FOUR
    # 同一局面的另一种走法：走 (9,1) 只补出 `{0,1,2,4}` 一个四（补 (9,3) 成五），
    # 另一个窗口 `[1..5]` 只有 3 颗子 —— 一个四，合法。
    assert R.foul_kind(b, 9, 1, R.BLACK) == R.FOUL_NONE

# -*- coding: utf-8 -*-
"""B1 回归 —— **搜索必须找得到杀棋，也必须认得必败。**

B1 是旧引擎最贵的一个缺陷，而且它不表现为崩溃或非法着法，只表现为
"棋下得很稳、但该赢的赢不下来"：

    旧搜索在评估层用 ``ai_score - human_score * 0.85``，并且只在很浅的
    固定层数上做 alpha-beta。于是只要对手有一个**醒目的静态威胁**
    （活三、冲四），浅层评估就会给出"我得先防一下"的信号，把真正的
    杀棋线压掉 —— 搜索**绕开**了赢棋的着法，而不是搜索不到。

修好之后，"找得到杀棋"与"认得必败"是同一个机制的两面：分值分带
（``STATIC_MAX`` 与 ``WIN_SCORE`` 之间那 128 的空档）让 ``is_mate()``
可以可靠区分"静态占优"与"已成杀"。这两件事必须一起测 —— 只测前者会
放过一个"把必败也报成必胜"的实现，只测后者会放过一个"根本找不到杀"的。

**这里还多测了一层别的文件都不测的东西：把引擎自己报出的杀棋线走一遍。**
分值落在杀棋带里只说明"搜索相信这件事"，不说明它是对的。杀棋分的两个
可见量 —— 符号与距离 —— 必须与"照着这条线走下去真的会连五"一致，而
这件事完全可以脱离搜索来验：轮番让引擎执双方各走一步，用
``engine.check_win``（与搜索共用同一份判定）看第几手终局。
"""

from __future__ import annotations

import numpy as np
import pytest

import engine_local as E


def mk(black, white):
    m = np.zeros((19, 19), dtype=np.uint8)
    for r, c in black:
        m[r][c] = 1
    for r, c in white:
        m[r][c] = 2
    return m


def mate_distance(val):
    """杀棋分 → "还需要几手"。与 ``WIN_SCORE - ply`` 的约定互为逆。"""
    return E.WIN_SCORE - abs(val)


def replay(board, me, level, maxply=12):
    """让引擎执双方各走一步，返回 ``(终局手数, 胜方)``；未终局返回 ``(None, None)``。

    每一步都 ``new_game()``，避免上一步的置换表影响下一步 —— 要验的是
    "这个着法确实通向连五"，不是"缓存记住了什么"。
    """
    b = board.copy()
    side = me
    for ply in range(maxply):
        E.new_game()
        r, c, _ = E.ai_move(b.copy(), side, level)
        b[r][c] = side
        if E.check_win(b, side):
            return ply + 1, side
        side = 3 - side
    return None, None


# ---------------------------------------------------------------- 通用不变式

# 每个局面 = (名字, 棋盘, 该谁走, 期望符号 +1/-1)
POSITIONS = [
    # 白方（AI）在行 9 上是**跳活三** (9,6)(9,7)(9,9) —— 只有 (9,8) 一个点
    # 能把它变成活四，因此杀棋着法是**唯一**的。黑方同时有一个活三
    # （主对角线 (5,5)(6,6)(7,7)），静态上白方还落后 500 分。
    ("unique_kill_white",
     mk([(5, 5), (6, 6), (7, 7), (15, 3), (15, 4), (12, 2), (12, 3)],
        [(9, 6), (9, 7), (9, 9), (2, 2), (2, 3), (17, 15), (17, 16)]),
     2, +1, (9, 8)),

    # 黑方双活三：(9,7)(9,8)(9,9) 与 (4,12)(5,12)(6,12)。白方轮走，
    # 挡得住一条挡不住另一条。白方在别处摆了 14 颗**毫无威胁**的子，
    # 子数 14 : 6 —— 只看子数的评估会认为白方挺好。
    ("double_three_lost_white",
     mk([(9, 7), (9, 8), (9, 9), (4, 12), (5, 12), (6, 12)],
        [(2, 2), (2, 3), (5, 8), (5, 9), (8, 5), (8, 6), (12, 12), (12, 13),
         (15, 2), (15, 3), (17, 16), (17, 17), (6, 15), (7, 15)]),
     2, -1, None),

    # 历史局面 B3（game_log_20260606_232014 第 34 手）：白方静态落后 557480，
    # 却有一条**连续冲四**（VCF）的杀棋线 —— 白在列 11 上有 (7,11)(8,11)(10,11)，
    # 落 (6,11) 后 (9,11) 成为成五点，此后一路冲四逼黑应，第四手连五。
    # **静态与杀棋方向相反**，正是 B1 要的那种局面。
    ("vcf_white_behind_on_static",
     mk([(6, 13), (7, 10), (7, 15), (8, 8), (8, 10), (9, 3), (9, 9),
         (10, 8), (10, 10), (11, 6), (11, 7), (11, 8), (11, 9), (11, 11),
         (12, 10), (12, 11), (14, 8)],
        [(7, 7), (7, 11), (7, 12), (7, 13), (7, 14), (8, 11), (9, 8),
         (9, 10), (10, 4), (10, 9), (10, 11), (11, 5), (11, 10), (12, 6),
         (12, 12), (13, 7)]),
     2, +1, None),
]


@pytest.mark.parametrize("name,board,me,want,fixed_move", POSITIONS)
@pytest.mark.parametrize("level", [1, 2, 3])
def test_mate_band_and_sign(new_engine, name, board, me, want, fixed_move, level):
    """杀棋带 + 符号：赢要报正、输要报负，且都必须落在杀棋带里。

    符号比"选了哪一格"更重要 —— 必败时每一格都输，钉死某一格只会让测试
    变成对搜索顺序的断言。只有 ``fixed_move`` 非空的局面（着法唯一）才
    额外断言具体着法。
    """
    new_engine.new_game()
    r, c, info = new_engine.ai_move(board.copy(), me, level)
    val = info["best_val"]

    assert new_engine.is_mate(val), \
        f"{name} L{level} 没报出杀棋分，返回静态分 {val}"
    assert (val > 0) == (want > 0), \
        f"{name} L{level} 杀棋方向反了：val={val}，期望 {'胜' if want > 0 else '负'}"
    if want > 0:
        assert val > new_engine.STATIC_MAX
        assert val <= new_engine.WIN_SCORE
    else:
        assert val < -new_engine.STATIC_MAX
        assert val >= -new_engine.WIN_SCORE

    if fixed_move is not None:
        assert (r, c) == fixed_move, \
            f"{name} L{level} 走了 ({r},{c})，唯一杀点是 {fixed_move}"

    assert 0 <= r < 19 and 0 <= c < 19, "必败也必须给出合法着法"


@pytest.mark.parametrize("name,board,me,want,fixed_move", POSITIONS)
def test_mate_line_really_ends_in_five(new_engine, name, board, me, want, fixed_move):
    """**把引擎自己报出的杀棋线走一遍** —— 分值与"真的会连五"必须自洽。

    这是唯一一条能证伪"搜索只是相信自己在杀"的检查。两点断言：

    * 终局时连五的是**同一边**（``winner == me``，与 ``want > 0`` 对照）；
    * 终局手数 == ``WIN_SCORE - abs(val) + 1``。

    第二条尤其值钱：杀棋分编码了"还剩几手"，而这个编码是 ``ply`` 约定
    （谁在 ply 节点上做成五，谁就拿 ``WIN_SCORE - ply``）与 ``_to_tt``
    归一化共同定义的**内部约定**。约定写错时，搜索照样能选中正确的杀棋
    着法，只是分值整体平移 —— 只有把分值拿去和实际手数对账才看得出来。

    （这里的 "+1" 来自约定本身：根节点是 ply 0，第 n 手落在 ply n-1 的
    节点上，于是五连的分值是 ``WIN_SCORE - (n - 1)``。）
    """
    new_engine.new_game()
    _, _, info = new_engine.ai_move(board.copy(), me, 3)
    val = info["best_val"]
    assert new_engine.is_mate(val), f"{name} 未报出杀棋分，无法对账：{val}"

    moves, winner = replay(board, me, 3)
    assert moves is not None, f"{name} 报了杀棋，12 手内却未见连五"
    assert (winner == me) == (want > 0), \
        f"{name} 终局胜方不对：{'黑' if winner == 1 else '白'}，期望 {'AI' if want > 0 else '对手'}"

    expect_moves = mate_distance(val) + 1
    assert moves == expect_moves, \
        f"{name} 分值说 {expect_moves} 手终局，实走 {moves} 手（val={val}）"


def test_unique_kill_beats_negative_static_evaluation(new_engine):
    """**静态评估把搜索往反方向拉** —— 这正是 B1 的病灶，所以单独钉住。

    这个局面里白方的静态分是**负的**：任何以静态分为准的选点都不会走
    (9,8)。旧引擎在同类局面（见 tools/positions.py 的 A2）返回的是
    ``val=0`` 的静态量级 —— 它能碰到正确着法纯属威胁层的快速通道，
    与搜索无关，因此换一个局面就漏。
    """
    board = POSITIONS[0][1]
    bd = E.Board.from_array(board)
    assert E.evaluate(bd, 2) < 0, "这个局面的要点就是静态评估站在反方向"

    new_engine.new_game()
    r, c, info = new_engine.ai_move(board.copy(), 2, 1)
    assert (r, c) == (9, 8), "最浅的一档也必须走唯一杀点"
    assert new_engine.is_mate(info["best_val"])


def test_loss_is_reported_even_when_material_looks_good(new_engine):
    """必败局面里 AI 子数更多（14 : 6），仍必须报出负的杀棋分。

    这也是"只看子数"的评估被证伪的地方：旧引擎的 ``ai_score -
    human_score * 0.85`` 在这一类局面上给不出任何警告。
    """
    board = POSITIONS[1][1]
    assert int((board == 2).sum()) > int((board == 1).sum()), "子数优势是这条测试的前提"

    new_engine.new_game()
    _, _, info = new_engine.ai_move(board.copy(), 2, 2)
    assert new_engine.is_mate(info["best_val"]) and info["best_val"] < 0


def test_mating_shapes_outscore_everything_that_cannot_mate(new_engine):
    """**"AI 静态占优却必败"为什么造不出来** —— 把原因变成可执行断言。

    原本想构造的对称反例是"对手有 3 步必杀，而 AI 有更大的静态优势"。
    反复构造之后发现它在**这套评估下不存在**，原因是可验的数值事实：

        一步成活四要求进攻方至少持有一条活三；要让对手挡不住，
        进攻方还得再有一条活三（双活三），或另有一条冲四（四三）。
        而这三样 —— 活四、双活三、四三 —— 恰恰是分值最高的棋型。

    所以"能杀的一方"在静态分上**永远**领先于"只有普通棋型的一方"。
    静态评估一旦错，只会把必胜看成静态优势、把必败看成静态劣势，**不会
    反过来**。于是杀棋带与静态带的分工是干净的：静态分负责"谁好一点"，
    杀棋分负责"谁已经赢了"，两者不会互相冒充。

    （"子数多寡"是另一回事 —— 局面 2 里 AI 是 14 子对 6 子。只看子数
    的评估才会被骗，那正是旧引擎那个线性组合的层次。）
    """
    LS = new_engine.LINE_SCORES
    live4 = LS[new_engine.LV_FOUR_LIVE]
    double_three = 2 * LS[new_engine.LV_THREE_LIVE] + new_engine.BONUS_DOUBLE_THREE
    four_three = (LS[new_engine.LV_FOUR] + LS[new_engine.LV_THREE_LIVE]
                  + new_engine.BONUS_FOUR_THREE)

    # 任何一种"挡不住"的杀型，都比"挡得住的"最强普通棋型（冲四）更贵
    best_harmless = LS[new_engine.LV_FOUR]
    assert double_three > best_harmless, (double_three, best_harmless)
    assert four_three > best_harmless, (four_three, best_harmless)
    # 而活四（一步成五）贵过一切组合
    assert live4 > double_three and live4 > four_three

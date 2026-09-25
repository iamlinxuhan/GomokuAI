# -*- coding: utf-8 -*-
"""**被判死刑时不许认命** —— 败局要加深搜索，且加深幅度有硬上限。

旧行为（用户实测到的"乱下"）：迭代加深里那一行

    if is_mate(val): break

对"我赢了"和"我被杀"**一视同仁**，于是必败局面在第 1 层就跳出循环 ——
日志里那一手是 `dep=1`、耗时 0–86 ms，而预算 5951 ms。剩下的 5.8 秒里
所有候选着法又都等于同一个杀棋分，严格的 `v > best` 让排序噪声
（TT 着法 / 杀手着法 / 历史）替引擎做了决定，于是走出 A1、C2 这种
在自己必死的那条线上毫无意义的点。

新行为 = 用户给的规则：

    出现严重问题时反而提高一个 dep 的算力用更广的搜索尝试寻找解法，
    但是最高不能超过正常普通对弈的 dep+2 …… 中级的普通 dep=5，
    那么全力寻找出路的 dep 要满足 3 < dep < 7

也就是**目标 `普通 dep + 1`**、**严格落在 `(普通 dep − 2, 普通 dep + 2)`**
开区间内。本文件把这条规则钉成四件事：

* ``test_escape_does_not_give_up_at_depth_one`` —— 必败不再第 1 层跳出；
* ``test_escape_depth_stays_inside_the_band`` —— 加深幅度不越过 `普通+1`，
  也就必然满足用户那个开区间；
* ``test_escape_picks_a_point_that_answers_the_threat`` —— 分值全相等时，
  从对手的成五点/四点里选，不再听排序噪声；
* ``test_winning_position_still_stops_at_once`` —— **必胜仍然立刻收工**：
  这条规则只治"必败认命"，不治"赢了还乱搜"。

对应 C++ 侧 ``search.cpp`` 的同名逻辑（`escapeTarget_` 与 `normalDepth_`），
两者是逐字镜像；跨实现的一致性由 ``tools/positions.py`` 的对照负责，
这里用纯 Python 实现测规则本身。
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


# 普通局面（无杀棋）：用来让引擎先记下一个"普通对弈的 dep"。
# 双方各三子、没有三连，搜索不可能在此得出杀棋结论。
QUIET = mk([(9, 9), (7, 9), (11, 11)], [(9, 10), (8, 10), (10, 8)])

# 必败局面：黑方在行 9 上有**活四** (9,5)-(9,8)，两端 (9,4)/(9,9) 皆空。
# 白方挡得住一头挡不住另一头 —— 这是**已证明**的必败（静止搜索里的双五点
# 判定），不是"引擎觉得不妙"。黑方的两个成五点就是白方唯一能做的事。
LOST = mk([(9, 5), (9, 6), (9, 7), (9, 8)],
          [(2, 2), (2, 3), (15, 15)])
LOST_FIVE_POINTS = {(9, 4), (9, 9)}

# 必胜局面：同一形状换成白方持有，白方轮走即可连五。
WON = mk([(2, 2), (2, 3), (15, 15), (15, 16)],
         [(9, 5), (9, 6), (9, 7), (9, 8)])


def _think(board, me, level, budget):
    """直接走 ``Engine.think`` 并返回 ``(cell, info)``。

    用 ``think`` 而不是 ``ai_move`` 是为了能给一个短时限：``ai_move`` 的
    签名（跨进程契约）里没有时限参数，而这里要测的是**深度**，不是那一档
    默认的 7 秒/15 秒。
    """
    idx, info = E._ENGINE.think(board, me, level, time_limit=budget)
    return divmod(idx, 19), info


def test_escape_does_not_give_up_at_depth_one(new_engine):
    """必败必须在第 2 层以上继续搜 —— 这是本次修复的**最小可观测差别**。

    旧实现断言的正是 ``actual_depth == 1``：一个已被证明的败局在 86 ms
    内就跳出了迭代加深循环，把剩下的预算全部浪费掉。新实现至少要再往下
    搜一层（目标是 ``普通 dep + 1``，见下一条用例）。
    """
    E.new_game()
    cell, info = _think(LOST, 2, 2, 3.0)

    assert E.is_mate(info["best_val"]) and info["best_val"] < 0, \
        f"这个局面就是已证明的必败，应报负杀棋分，实报 {info['best_val']}"
    assert info["actual_depth"] >= 2, \
        (f"必败时仍在第 {info['actual_depth']} 层就收手（旧行为是 1 层）——"
         f"用掉的 {info['time_ms']:.0f}ms 中大部分预算被白白丢掉")


def test_escape_depth_stays_inside_the_band(new_engine):
    """加深的幅度：目标 ``普通+1``，**上限不超过普通+1**，绝不越过开区间。

    规则的原话是"最高不能超过正常普通对弈的 dep+2 …… 中级普通 dep=5，
    那么全力寻找出路的 dep 要满足 3 < dep < 7"。这条用例把两个边界都量
    出来：先在一个无杀棋的局面里问出本档**最近一次的普通 dep**，再在必败
    局面上量实际搜到的层数。

    ``reset()`` 会清空 ``_normal_depth``，所以两次 ``think`` 之间**不能**
    调用 ``new_game()`` —— 那正是这条规则在设计上的前提：它是"同一局内
    上一手的见识"，不是跨局的常量。
    """
    E.new_game()
    _cell, quiet = _think(QUIET, 2, 2, 3.0)
    normal = E._ENGINE._normal_depth
    assert not E.is_mate(quiet["best_val"]), "这个局面不该有杀棋结论"
    assert normal >= 1, "普通局面搜完之后应当记下一个普通深度"

    _cell, info = _think(LOST, 2, 2, 3.0)
    dep = info["actual_depth"]

    assert dep >= 2, f"必败时只在第 {dep} 层收手"
    assert dep <= normal + E._ESCAPE_EXTRA, \
        (f"必败时搜到了第 {dep} 层，普通深度是 {normal} —— "
         f"超出目标（普通+{E._ESCAPE_EXTRA}）")
    assert dep < normal + E._ESCAPE_CAP, \
        f"第 {dep} 层越过了用户给的开区间上界（普通 {normal} + {E._ESCAPE_CAP}）"
    # 下界一侧由常量自身保证，这里把这条不变式也变成可执行的断言
    assert -E._ESCAPE_CAP < E._ESCAPE_EXTRA < E._ESCAPE_CAP


def test_escape_picks_a_point_that_answers_the_threat(new_engine):
    """分值全相等时的平局裁决：从对手的**成五点**里挑。

    "加深搜索"本身解决不了观感问题 —— 必败局面里每一手都是同一个杀棋分
    （对手的连五与你落在哪里无关），严格 ``v > best`` 于是把选择权交给了
    排序噪声。旧行为在日志里给出的 A1/C2 就是这么来的。这里断言的是修好
    之后的结果：落点必须落在黑方那两个成五点之一。
    """
    E.new_game()
    cell, info = _think(LOST, 2, 2, 3.0)

    assert E.is_mate(info["best_val"]) and info["best_val"] < 0
    assert cell in LOST_FIVE_POINTS, \
        (f"必败时走到了 {cell}，而对手的成五点是 {sorted(LOST_FIVE_POINTS)} —— "
         f"同一分值下应优先堵在对手马上要连五的点上")


def test_winning_position_still_stops_at_once(new_engine):
    """**必胜不加深**：这条规则只治"必败认命"，不治"赢了还乱搜"。

    ``val > 0`` 时迭代加深照旧立刻跳出（旧行为里这一半是对的），并且走出
    的那一手必须真的成五。
    """
    E.new_game()
    cell, info = _think(WON, 2, 1, 2.0)

    assert E.is_mate(info["best_val"]) and info["best_val"] > 0, \
        f"白方有成五点，应报正杀棋分，实报 {info['best_val']}"
    assert info["actual_depth"] <= 1, \
        f"必胜却搜到了第 {info['actual_depth']} 层 —— 该立刻收工"
    assert cell in LOST_FIVE_POINTS, f"必胜着法应在成五点上，实走 {cell}"

    b = WON.copy()
    b[cell[0]][cell[1]] = 2
    assert E.check_win(b, 2), f"走了 {cell} 之后并没有连五"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

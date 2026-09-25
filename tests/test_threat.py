# -*- coding: utf-8 -*-
"""威胁响应层：**它已经不作为一层存在了 —— 这正是要断言的事。**

旧引擎有一个 `_check_immediate_threat`（约 130 行）挡在搜索前面，用七段启发式
规则抢答"现在该走哪"。它的失败方式是结构性的：**启发式判对时省下的时间，远
小于判错时输掉的棋**。B17 是其中最典型的一处 —— 判据写成
`opp_win >= 2 or opp_live4 >= 2`，而实战里最常见的双杀形态是
`opp_win == 1 且 opp_live4 >= 1`，**恰好落在判据之外**。补丁是加一条规则，
而规则永远补不完；真正的问题是"用有限条规则去近似一个可以精确判定的问题"。

新引擎把这件事拆给两个**能给出确定性结论**的机制：

* **静止搜索**负责"强制着法"：它数成五点的**个数**（`|F| >= 2` 即挡不住），
  不是数"有没有活四"这种需要先定义清楚的中间量。数个数是精确的。
* **VCF** 负责"更长的强制序列"，返回 WIN / NO_WIN / EXHAUSTED 三态。
  它的防守侧走 AND 语义：对手**所有**应手都失败才算赢。

所以本文件测的不是"某一层"的行为，而是"**不该再有那一层**"：
`_check_immediate_threat`、`_find_forced_win`、`_find_winning_moves` 这些
名字必须不存在，而它们当年处理的局面必须仍然被正确处理。
"""

from __future__ import annotations

import numpy as np
import pytest

import engine_local as E


def mk(black=(), white=()):
    b = np.zeros((19, 19), dtype=np.uint8)
    for r, c in black:
        b[r][c] = 1
    for r, c in white:
        b[r][c] = 2
    return b


# ---------------------------------------------------------------- 层已删除

@pytest.mark.parametrize("name", [
    "_check_immediate_threat",      # 七段启发式抢答层
    "_find_forced_win",             # 把"搜不完"当"没有必胜"的那个（B15）
    "_find_winning_moves",          # 逐点全盘扫，O(361) Python 循环
    "_find_live_four_moves",        # 同上，且当年被重复计算两遍（B18）
    "DESPERATION_THRESHOLD",        # 拼命模式的阈值
])
def test_the_heuristic_layer_is_gone(name):
    """旧威胁层的每个入口都必须不存在。

    这条比"新代码写得对"更根本：只要这些名字还在，就说明有人又想把判断
    塞回启发式里。它们当年的正当诉求已经有精确的实现（见模块 docstring），
    重新引入只会重演同一个失败模式。
    """
    assert not hasattr(E, name), f"{name} 又回来了 —— 启发式抢答层不该复活"


# ---------------------------------------------------------------- B17

def test_b17_double_kill_is_reported_as_a_loss():
    """B17：**两个成五点**（而非"一个冲四加一个活四"这种需要解释的说法）。

    黑方在行 12 有四子（(12,2) 被白堵住，成五点 (12,7)），行 9 也有四子
    （成五点 (9,9)）。白方**挡得住其中一个，挡不住两个** —— 它已经输了。

    旧引擎在这里的判据是 `opp_win >= 2 or opp_live4 >= 2`；这里 `opp_win`
    若是 2 就会被抓到，但判据的设计意图（"数一数活四的个数"）表明它想数的
    是另一种东西。新引擎不做这个区分：`_five_points` 直接给出成五点集合，
    `bit_count() > 1` 就是"挡不住"，与这些点来自一条线还是两条线无关。

    距离也要对：白方这一手之后，黑方下一手成五，所以是 `-(WIN_SCORE - 1)`。
    """
    board = mk(black=[(12, 3), (12, 4), (12, 5), (12, 6), (9, 5), (9, 6),
                      (9, 7), (9, 8)],
               white=[(9, 4), (12, 2), (11, 3), (11, 4), (11, 5),
                      (13, 3), (13, 4)])
    bd = E.Board.from_array(board)
    five = E._five_points(bd.bits_of(1), bd.bits_of(2))
    assert five.bit_count() == 2, "构造局面就该是恰好两个成五点"
    assert not E._five_points(bd.bits_of(2), bd.bits_of(1)), "白方不该有威胁"

    E.new_game()
    r, c, info = E.ai_move(board.copy(), 2, 3)
    assert E.is_mate(info["best_val"]), "白方已必败，必须报出来而不是给个静态分"
    assert info["best_val"] < 0, "报出的必须是负的（对白方不利）"
    assert info["best_val"] == -(E.WIN_SCORE - 1), (
        f"距离应是一手（黑方下一手成五），实得 {info['best_val']}")


def test_single_five_point_is_still_merely_a_block():
    """对照面：**只有一个**成五点时，那是"必须挡"，不是"已经输了"。

    没有这条对照，上面那条用"报了负分"来判定 B17 就说明不了任何事 ——
    一个把所有威胁都报成必败的引擎也能通过。
    """
    board = mk(black=[(12, 3), (12, 4), (12, 5), (12, 6)],
               white=[(12, 2), (9, 3), (9, 4), (9, 5), (13, 3), (13, 4), (13, 5)])
    bd = E.Board.from_array(board)
    assert E._five_points(bd.bits_of(1), bd.bits_of(2)).bit_count() == 1

    E.new_game()
    r, c, info = E.ai_move(board.copy(), 2, 3)
    assert (r, c) == (12, 7), f"唯一正解是堵成五点 (12,7)，实得 {(r, c)}"
    assert info["best_val"] > -E.WIN_SCORE + E.MAX_PLY, (
        f"只是被冲四，不该报成必败：{info['best_val']}")

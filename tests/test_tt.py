# -*- coding: utf-8 -*-
"""置换表的两个易错点：**条目类型判定**与**杀棋分归一化**。

两者都是"写错了也能跑、只会悄悄下错棋"的类型 —— 不会抛异常，只会让搜索
在个别分支上返回一个冒充精确值的上/下界。所以这里把判定逻辑抽成纯函数
（`_flag_of` / `_to_tt` / `_from_tt`）单独测，而不是靠对局结果间接观察。
"""

from __future__ import annotations

import numpy as np
import pytest

import engine_local as E


# ------------------------------------------------------------------ 条目类型

def test_flag_boundaries():
    """`value == alpha_orig` 必须是 UPPER，`value == beta` 必须是 LOWER（B2）。

    旧版写成 `value > alpha_orig` 判 EXACT，于是"恰好等于下界"的失败低被
    当成精确值存下来。后续搜索命中该条目时会**直接返回这个上界冒充精确值**，
    表现为"同一局面偶尔给出明显更差的分值"。

    边界是这条函数的全部难点 —— 中间那一半反而不会错。
    """
    alpha, beta = 100, 200
    assert E._flag_of(100, alpha, beta) == E.TT_UPPER     # == alpha，不是 EXACT
    assert E._flag_of(99, alpha, beta) == E.TT_UPPER
    assert E._flag_of(200, alpha, beta) == E.TT_LOWER     # == beta，不是 EXACT
    assert E._flag_of(201, alpha, beta) == E.TT_LOWER
    assert E._flag_of(101, alpha, beta) == E.TT_EXACT
    assert E._flag_of(199, alpha, beta) == E.TT_EXACT


def test_flag_of_degenerate_window():
    """零窗口（`alpha + 1 == beta`，PVS 的试探窗口）下必须仍能判出 UPPER/LOWER。

    零窗口里没有"精确值"可言：`value <= alpha` 或 `value >= beta` 必居其一
    （因为 beta == alpha+1，任何整数都 <= alpha 或 >= beta）。若实现里写成
    `alpha < value < beta` 这种判 EXACT，零窗口会产出 EXACT 条目 —— 那是最
    坏的一类污染，因为 PVS 全靠零窗口。
    """
    for value in range(-3, 5):
        f = E._flag_of(value, 0, 1)
        assert f in (E.TT_UPPER, E.TT_LOWER), (value, f)
        assert f == (E.TT_UPPER if value <= 0 else E.TT_LOWER)


# ------------------------------------------------------------------ 杀棋分

@pytest.mark.parametrize("ply", [0, 1, 3, 17, E.MAX_PLY - 1])
def test_mate_score_roundtrip(ply):
    """存进去再取出来，杀棋分必须逐位还原。"""
    for raw in (E.WIN_SCORE, E.WIN_SCORE - 7, -(E.WIN_SCORE - 7)):
        assert E._from_tt(E._to_tt(raw, ply), ply) == raw


@pytest.mark.parametrize("ply", [0, 2, 9])
def test_static_score_is_not_perturbed(ply):
    """静态分**不参与**归一化 —— 否则它会被 ply 平移，破坏分带。"""
    for raw in (0, 1234, -1234, E.STATIC_MAX, -E.STATIC_MAX):
        assert E._to_tt(raw, ply) == raw
        assert E._from_tt(raw, ply) == raw


def test_mate_normalization_is_position_only():
    """**同一个局面**在不同层被存下时，存储值必须相同。

    这是归一化真正要保证的东西，写清楚很要紧 —— 免得把"存的是距离"误读成
    "存的是分值"：

      * 引擎内部返回的杀棋分是相对**根**的（在 ply 层发现成五 ⇒ `WIN_SCORE - ply`），
        所以同一个局面 P 在第 3 层和第 7 层被搜到时，原始值分别是
        `WIN_SCORE - 5` 与 `WIN_SCORE - 9`（都假设 P 之后还需 2 步）。
      * 存的时候 `+ ply` 把"距根"折回"距 P"：两者都变成 `WIN_SCORE - 2`。
      * 取的时候 `- ply` 再折回去，于是每个调用点都读到"距我这儿多少步"。

    所以这里变化的是**传入的原始值**（它本来就随 ply 变），断言不变的是
    **存储值**。若测试写成"给固定的 WIN_SCORE-3 换 ply 存"，那它期望的是
    "同一个分值在不同层代表同一个局面"—— 那是错的，会诱导实现去掉归一化。
    """
    for d in (0, 1, 5, 40):
        stored = {E._to_tt(E.WIN_SCORE - (ply + d), ply) for ply in range(0, 12)}
        assert stored == {E.WIN_SCORE - d}, f"归一化失效，存下了 {stored}"


def test_mate_band_survives_ply_shift():
    """归一化之后的取值必须仍然落在杀棋带里，否则 `is_mate` 会误判。

    这条防的是"归一化把杀棋分推出了 STATIC_MAX 之外的带"——那样读回来时
    `_from_tt` 就不再认它是杀棋分，会原样返回一个被 ply 平移过的数。
    """
    for ply in range(0, E.MAX_PLY):
        v = E._from_tt(E._to_tt(E.WIN_SCORE, ply), ply)
        assert E.is_mate(v), (ply, v)


# ------------------------------------------------------------------ 冷热一致

def _search(board, mod, level=2):
    """走模块级入口 —— 它用的就是 `_ENGINE` 这个单例。"""
    return mod.ai_move(board.copy(), 2, level)


def test_hot_and_cold_tt_agree(new_engine):
    """冷/热置换表下同一局面必须给出同一个着法。

    这一条是**行为层面**的：它不检查内部状态，只检查"结果与缓存温度无关"。
    旧引擎做不到 —— 它把 TT 放在模块全局，上一局的残留会改变这一局的着法。
    """
    board = np.zeros((19, 19), dtype=np.uint8)
    for r, c, p in ((9, 9, 1), (10, 10, 2), (9, 10, 1), (8, 10, 2), (10, 9, 1)):
        board[r][c] = p

    # 冷：先清空。热：先跑一次**别的**局面把表捂热。
    new_engine.new_game()
    cold = _search(board, new_engine)

    warm_board = board.copy()
    warm_board[0][0] = 2
    _search(warm_board, new_engine)
    hot = _search(board, new_engine)

    assert (cold[0], cold[1]) == (hot[0], hot[1]), \
        f"置换表温度改变了着法：冷 {cold[:2]} vs 热 {hot[:2]}"


def test_tt_lookup_respects_depth(new_engine):
    """深度不足的条目只能提供建议着法，**不能**直接返回值。

    否则浅层搜索结果会冒充深层结果 —— 表现为"迭代加深不起作用，深度数字
    在涨但棋力不涨"。
    """
    eng = new_engine._ENGINE
    eng.reset()
    board = np.zeros((19, 19), dtype=np.uint8)
    board[9][9] = 1
    board[9][10] = 2
    bd = new_engine.Board.from_array(board)

    key = bd.hash ^ (0 if 2 == 1 else new_engine._TT_SALT_W)
    eng.tt[key] = (3, 42, new_engine.TT_EXACT, 100)

    v, mv, hit = eng._lookup(bd, 2, depth=4, alpha=-1000, beta=1000, ply=0)
    assert not hit and mv == 100, "深度不足时应返回建议着法但不算命中"

    v, mv, hit = eng._lookup(bd, 2, depth=2, alpha=-1000, beta=1000, ply=0)
    assert hit and v == 42, "深度足够时应命中"

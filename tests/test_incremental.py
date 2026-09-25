# -*- coding: utf-8 -*-
"""增量状态的正确性 —— Phase 2 最有价值的一组测试。

``Board`` 的候选集、邻居计数、哈希、子数都是**增量维护**的：落子只更新周围
24 格，不做全盘扫描。增量代码的失效模式很隐蔽 —— 状态只是"稍微不对"，
搜索仍然跑得动，只是偶尔选错着法。所以这里不测"能不能搜"，而是每个随机
局面都把增量状态与**完全独立的朴素实现**逐项比对，再验 make/unmake 的位级还原。

三组断言各自覆盖一类失效：

* 与朴素实现比对 → 增量更新公式本身错了；
* 与 ``from_array`` 比对 → 两条构建路径（逐子 make / 批量重建）不一致；
* 乱序 unmake → 撤销不对称，状态在搜索回退时悄悄漂移。
"""

from __future__ import annotations

import random

import numpy as np
import pytest

from engine_local import CELLS, Board, _mask_cells

N = 19
DIRS = ((0, 1), (1, 0), (1, 1), (1, -1))


# ------------------------------------------------------------------ 朴素参考

def naive_candidates(g):
    """按定义算候选：为空、且切比雪夫距离 2 内有棋子。"""
    out = []
    for r in range(N):
        for c in range(N):
            if g[r][c]:
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    rr, cc = r + dr, c + dc
                    if 0 <= rr < N and 0 <= cc < N and g[rr][cc]:
                        out.append(r * N + c)
                        break
                else:
                    continue
                break
    return out


def naive_neighbor_count(g):
    nc = [0] * CELLS
    for r in range(N):
        for c in range(N):
            if not g[r][c]:
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    if dr == 0 and dc == 0:
                        continue
                    rr, cc = r + dr, c + dc
                    if 0 <= rr < N and 0 <= cc < N:
                        nc[rr * N + cc] += 1
    return nc


def naive_hash(g):
    from engine_local import _ZOBRIST
    h = 0
    for r in range(N):
        for c in range(N):
            v = g[r][c]
            if v:
                h ^= _ZOBRIST[v - 1][r * N + c]
    return h


# ------------------------------------------------------------------ 走子

def pick_move(g, rng, mode):
    """产生一步合法的随机着法（不依赖 Board.candidates，保持测试独立）。"""
    if mode == "cluster":
        pool = naive_candidates(g)
        if not pool:
            pool = [r * N + c for r in range(N) for c in range(N) if not g[r][c]]
    else:                                   # scatter：全盘均匀
        pool = [r * N + c for r in range(N) for c in range(N) if not g[r][c]]
    return rng.choice(pool)


def playout(seed, mode, n_moves):
    """随机走 n_moves 手，返回 (最终棋盘, 每手 (idx, player) 列表)。"""
    rng = random.Random(seed)
    g = [[0] * N for _ in range(N)]
    moves = []
    for k in range(n_moves):
        if all(g[r][c] for r in range(N) for c in range(N)):
            break
        idx = pick_move(g, rng, mode)
        p = 1 if k % 2 == 0 else 2
        g[idx // N][idx % N] = p
        moves.append((idx, p))
    return g, moves


# ------------------------------------------------------------------ 1. 逐步差分

@pytest.mark.parametrize("mode", ["cluster", "scatter"])
@pytest.mark.parametrize("seed", range(3))
def test_playout_matches_naive_every_step(mode, seed):
    """每落一子，增量状态都必须与朴素重算逐项一致。

    这是最能抓住增量 bug 的形态：不是"最后对不对"，而是**任意中间态**都要对。
    """
    rng = random.Random(seed)
    g = [[0] * N for _ in range(N)]
    b = Board()
    for step in range(200):
        if all(g[r][c] for r in range(N) for c in range(N)):
            break
        idx = pick_move(g, rng, mode)
        p = 1 if step % 2 == 0 else 2
        g[idx // N][idx % N] = p
        b.make(idx, p)

        ctx = f"\n第 {step+1} 手 ({idx//N},{idx%N}) 玩家{p} mode={mode} seed={seed}"
        assert _mask_cells(b.cand_mask) == naive_candidates(g), "候选集不一致" + ctx
        assert bytes(b.neighbor_count) == bytes(naive_neighbor_count(g)), \
            "邻居计数不一致" + ctx
        assert b.hash == naive_hash(g), "哈希不一致" + ctx
        assert (b._n_black, b._n_white) == (
            sum(row.count(1) for row in g), sum(row.count(2) for row in g)), \
            "子数不一致" + ctx
        assert b.stone_count == step + 1


# ------------------------------------------------------------------ 2. 两条构建路径

@pytest.mark.parametrize("seed", range(6))
def test_from_array_equals_incremental_build(seed):
    """``from_array``（批量）与逐子 make 必须得到位级相同的状态。

    这两条路径将分别用在"从 UI 传入的棋盘续弈"与"搜索内部落子"上，
    不一致就会表现为"同样局面搜出不同结果"。
    """
    g, moves = playout(seed, "cluster", 80)
    inc = Board()
    for idx, p in moves:
        inc.make(idx, p)
    bulk = Board.from_array(np.array(g, dtype=np.uint8))
    assert inc.diff(bulk) == []


# ------------------------------------------------------------------ 3. make/unmake

def test_unmake_immediately_restores_state():
    """在**每一个深度**上落一子再立刻撤销，状态都必须逐位还原。

    这模拟搜索的真实用法：深入一层再退回上一层。60 手全下完只测了最深的那
    一层，而搜索是在任意层级回退的 —— 所以这里逐层推进，每层都退一次再真下。
    """
    g, moves = playout(11, "cluster", 60)
    b = Board()

    def snap():
        return (b.b_bits, b.w_bits, b.hash, b.cand_mask,
                bytes(b.neighbor_count), b._n_black, b._n_white)

    for depth, (idx, p) in enumerate(moves):
        before = snap()
        b.make(idx, p)
        b.unmake(idx, p)
        assert snap() == before, \
            f"第 {depth+1} 手 ({idx//N},{idx%N}) 玩家{p}：落子后撤销未还原"
        b.make(idx, p)                  # 真正落下，进入下一层


def test_unmake_out_of_order_returns_to_empty():
    """**乱序**撤销全部落子后必须回到空盘。

    搜索在剪枝时会在任意层级 return，没有固定顺序的清理；对称的 make/unmake
    正是为此设计。若这里失败，说明撤销引入了顺序依赖。
    """
    g, moves = playout(23, "cluster", 100)
    b = Board()
    for idx, p in moves:
        b.make(idx, p)

    shuffled = list(moves)
    random.Random(99).shuffle(shuffled)
    assert shuffled != moves, "洗牌没起作用，这个用例就没意义了"
    for idx, p in shuffled:
        b.unmake(idx, p)
    assert b.diff(Board()) == [], "乱序撤销后未回到空盘"


def test_unmake_out_of_order_partial_matches_rebuild():
    """随机撤掉一部分（乱序）后，状态必须等于"用剩下的子重建"。"""
    g, moves = playout(31, "cluster", 120)
    b = Board()
    for idx, p in moves:
        b.make(idx, p)

    removed = set()
    rng = random.Random(5)
    for idx, p in moves:
        if rng.random() < 0.5:
            b.unmake(idx, p)
            removed.add(idx)

    g2 = [[0] * N for _ in range(N)]
    for idx, p in moves:
        if idx not in removed:
            g2[idx // N][idx % N] = p
    assert b.diff(Board.from_array(np.array(g2, dtype=np.uint8))) == []


def test_make_returns_true_exactly_when_five_forms():
    """``make`` 的返回值就是"这一手成了五" —— 与独立参考一致。"""
    for seed in range(4):
        rng = random.Random(seed)
        g = [[0] * N for _ in range(N)]
        b = Board()
        for step in range(120):
            if all(g[r][c] for r in range(N) for c in range(N)):
                break
            idx = pick_move(g, rng, "cluster")
            p = 1 if step % 2 == 0 else 2
            g[idx // N][idx % N] = p
            got = b.make(idx, p)

            r, c = divmod(idx, N)
            want = False
            for dr, dc in DIRS:
                for k in range(5):
                    sr, sc = r - dr * k, c - dc * k
                    win = [(sr + dr * t, sc + dc * t) for t in range(5)]
                    if all(0 <= rr < N and 0 <= cc < N and g[rr][cc] == p
                           for rr, cc in win):
                        want = True
            assert got == want, f"seed={seed} 第 {step+1} 手 ({r},{c}) p={p}"
            if got:
                break                       # 成五之后继续走就不满足前提了


# ------------------------------------------------------------------ 4. 候选集语义

def test_candidates_equals_empty_board_center():
    """空盘的候选是天元 —— 旧 ``_generate_moves`` 在候选为空时退化为全盘，
    新实现改为显式返回天元一个点。这是**有意的行为变更**：搜索的根节点
    由 Phase 3 的迭代加深负责，空盘只需要一个合法着法。"""
    b = Board()
    assert b.candidates() == [(N // 2) * N + N // 2]


def test_candidates_excludes_occupied():
    """候选集里绝不能出现已占格。"""
    g, moves = playout(17, "cluster", 80)
    b = Board()
    for idx, p in moves:
        b.make(idx, p)
    occ = set(idx for idx, _ in moves)
    assert not (set(b.candidates()) & occ)


def test_candidates_sorted_ascending():
    g, moves = playout(19, "cluster", 40)
    b = Board()
    for idx, p in moves:
        b.make(idx, p)
    cand = b.candidates()
    assert cand == sorted(cand)


# ------------------------------------------------------------------ 5. 表结构不变式

def test_precomputed_tables_have_expected_shape():
    """预计算表的规模是几何事实，不该随改动漂移。"""
    from engine_local import _LINE_MASKS, _CELL_LINES, _WIN_MASKS, _NEIGHBORS
    assert [len(m) for m in _LINE_MASKS] == [19, 19, 37, 37]
    assert {len(cl) for cl in _CELL_LINES} == {4}, "每格应恰在 4 条线上"
    assert len(_WIN_MASKS) == CELLS and len(_NEIGHBORS) == CELLS
    assert len(_WIN_MASKS[(N // 2) * N + N // 2]) == 20, "中心格应有 20 个连五窗口"
    assert len(_NEIGHBORS[(N // 2) * N + N // 2]) == 24
    assert len(_NEIGHBORS[0]) == 8, "角格（半径2邻域）应有 8 个邻居"


def test_every_cell_neighbors_are_mutual():
    """邻居关系必须对称 —— make 与 unmake 依赖它，不对称会导致撤销不净。"""
    from engine_local import _NEIGHBORS
    for i in range(CELLS):
        for j in _NEIGHBORS[i]:
            assert i in _NEIGHBORS[j], f"{i} 认为 {j} 是邻居，反之不然"


def test_zobrist_table_has_no_duplicates():
    """Zobrist 表里每个值必须互不相同 —— 这是它作为"位置指纹"的全部意义。

    这条断言补的是一个**真实的、已经发生过的**缺陷：建表时把
    ``random.Random(seed)`` 写在了生成器表达式内部，于是 361 次迭代各建一个
    同种子发生器、各取自己的第一个输出，全表退化成**一个常量**。后果不是
    "哈希变慢"，而是哈希彻底失去区分度：`Board.hash` 只剩"棋子数的奇偶性"
    （偶数颗异或成 0），置换表实际只有两个键，搜索在 depth≥6 之后每轮迭代
    都读到同一个错误条目 —— 报出 depth=24，实际只走到 ply=5，每步棋的分值
    冻结不动。

    它之所以能长期潜伏，是因为**没有任何测试在看这张表的"内容"**：
    `from_array` 与 `make` 共用同一张表，逐字段比对二者仍然逐一相等，
    一致性测试全绿。缺的正是"值互不相同"这一条。
    """
    from engine_local import _ZOBRIST
    for p, row in enumerate(_ZOBRIST, start=1):
        assert len(row) == CELLS, f"第 {p} 方应有 {CELLS} 个键"
        dup = len(row) - len(set(row))
        assert dup == 0, f"第 {p} 方的 Zobrist 表有 {dup} 个重复值"
    assert not (set(_ZOBRIST[0]) & set(_ZOBRIST[1])), "黑白两方的键不得相交"


def test_hash_distinguishes_positions_with_equal_stone_count():
    """同子数、不同位置的局面必须哈希不同 —— 差一子以上的比较是弱断言。

    上一条测的是表本身，这条测的是**表被用对了**：即便表没问题，只要
    `make` 里异或错了下标（比如漏掉 player 维度），同子数的局面仍会撞键。
    取"落一子后立刻撤销"与"落另一子"这种最小差异对，奇偶性完全相同。
    """
    from engine_local import Board
    b = Board()
    cells = [9 * 19 + 9, 9 * 19 + 10, 0, 360]
    hashes = []
    for c in cells:
        b.make(c, 1)
        hashes.append(b.hash)
        b.unmake(c, 1)
    assert len(set(hashes)) == len(cells), f"不同位置的哈希发生碰撞: {hashes}"

    b.make(cells[0], 1)
    b.make(cells[1], 2)
    two_stones = b.hash
    b.unmake(cells[1], 2)
    b.unmake(cells[0], 1)
    b.make(cells[2], 1)
    b.make(cells[3], 2)
    assert b.hash != two_stones, "同子数不同位置的两个局面哈希相同"

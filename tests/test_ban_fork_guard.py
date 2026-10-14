# -*- coding: utf-8 -*-
"""``engine_ban`` 与 ``engine_local`` 的分叉护栏。

``engine_ban.py`` 是 ``engine_local.py`` 的**分叉**：两份文件、各自演进。分叉的
风险是"顺手改了搜索本身" —— 症状是禁手局用的不再是同一个搜索，棋力与无禁手局
对不上，而这件事**在代码上看不出来**，只能靠对局发现。

本用例的做法是：让禁手判定**一次都不触发**，然后要求两边给出**逐位相同**的结果
（着法、分值、层数、节点数、理由）。禁手判定没触发时，两份引擎应当执行完全
相同的代码 —— 任何一处"顺手改了评估函数/排序/剪枝"都会在这里现形。

为什么必须让搜索自己跑完：引擎的余量是按**墙钟**计的（``deadline_`` 早退、
``_normal_depth`` 的应急上限），时间不够时两边会各自截在某一轮迭代上，于是
"不同"反映的是计时抖动而不是代码差异。所以这里直接调 ``_ENGINE.think`` 并给
一个远大于需要的 ``time_limit``，让迭代按 ``max_depth`` 自然收尾。
``ai_move`` 没有 ``time_limit`` 形参，只能走这条。
"""

from __future__ import annotations

import random

import numpy as np
import pytest

import engine_ban as B
import engine_local as L

#: 逐位比较的字段。``nps`` 与 ``time_ms`` 是墙钟派生量，本来就会飘，不比；
#: ``tt_hit_rate`` / ``qnode_ratio`` 同样由前几项相除得来，不比。
_KEYS = ("best_val", "depth", "nodes", "reason", "score_type")


@pytest.fixture
def ban_hits(monkeypatch):
    """记录搜索期间触发的黑方禁手次数。

    这个探针是护栏的**前提**，不是附加结论：一次禁手都没触发，才谈得上"两边
    执行了同一段代码"。真触发了，那一局的差异是**规则要求**的分叉，不能拿来做
    护栏 —— 用例必须如实报"这组局面不适用"，而不是放宽断言。
    """
    hits = []
    orig = B._black_ban

    def spy(bd, idx, made):
        out = orig(bd, idx, made)
        if out == B.BAN_LOSE:
            hits.append(idx)
        return out

    monkeypatch.setattr(B, "_black_ban", spy)
    return hits


def _random_position(rng, n_black=2, n_white=1):
    """中央区域随机撒子。子少 → 搜索浅、快、确定。"""
    board = np.zeros((19, 19), dtype=int)
    cells = [(r, c) for r in range(5, 14) for c in range(5, 14)]
    rng.shuffle(cells)
    it = iter(cells)
    for _ in range(n_black):
        r, c = next(it)
        board[r][c] = 1
    for _ in range(n_white):
        r, c = next(it)
        board[r][c] = 2
    return board


def test_engine_ban_is_bit_exact_when_no_foul_fires(ban_hits):
    """没有禁手参与时，两份引擎必须逐位一致。"""
    rng = random.Random(20261014)
    checked = 0
    for _ in range(6):
        board = _random_position(rng, rng.randint(1, 2), rng.randint(1, 2))
        for stone in (1, 2):
            if not (board == stone).any():
                continue
            ban_hits.clear()
            B.new_game()
            b_idx, b_info = B._ENGINE.think(board, stone, 1, time_limit=120.0)
            fired = len(ban_hits)
            L.new_game()
            l_idx, l_info = L._ENGINE.think(board, stone, 1, time_limit=120.0)

            assert fired == 0, (
                "这组局面触发了黑方禁手，不能用来做分叉护栏"
                "（护栏只覆盖'禁手不参与'的那一半）")
            assert b_idx == l_idx, (
                "engine_ban 与 engine_local 在同一个局面上给出了不同的着法: %r vs %r"
                % (b_idx, l_idx))
            for k in _KEYS:
                assert b_info[k] == l_info[k], (
                    "字段 %s 分叉: %r vs %r" % (k, b_info[k], l_info[k]))
            checked += 1

    # 护栏的样本量：低于这个数说明生成器退化了（比如全都同色），
    # 用例会静静地什么都不测。
    assert checked >= 8


def test_ban_engine_does_not_use_the_opening_book():
    """禁手引擎不查开局库 —— 那张表是在无禁手规则下生成的（§5.5）。"""
    assert B.USE_OPENING_BOOK is False
    # 空盘那一手是天元这条**规则**，不是查表：理由串必须说实话。
    board = np.zeros((19, 19), dtype=int)
    r, c, info = B.ai_move(board, 1, 1, use_book=False)
    assert (r, c) == (9, 9)
    assert info["reason"] == "开局规则"

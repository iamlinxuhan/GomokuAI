# -*- coding: utf-8 -*-
"""局面题库：题目自洽性 + 引擎解题率。

分三部分：
  1. 题目自洽性 —— 不依赖任何引擎，永远运行。
  2. 冻结基线的失败项 —— 锁定"旧引擎在 B1 上必错"这一事实，
     任何人误改 tools/legacy_engine.py 都会在这里被拦下。
  3. 新引擎解题率 —— Phase 3-5 的验收目标，**已达成**（9/9 = 100%）。
"""

from __future__ import annotations

import numpy as np
import pytest

import tools.positions as P

ALL = P.build_positions()
IDS = [p["id"] for p in ALL]


def _has_five(board, player):
    """独立于被测引擎的成五判定（朴素扫描，故意不复用引擎实现）。"""
    dirs = [(0, 1), (1, 0), (1, 1), (1, -1)]
    for r in range(19):
        for c in range(19):
            if board[r][c] != player:
                continue
            for dr, dc in dirs:
                if all(0 <= r + k * dr < 19 and 0 <= c + k * dc < 19
                       and board[r + k * dr][c + k * dc] == player
                       for k in range(5)):
                    return True
    return False


# ---------------------------------------------------------------- 1. 自洽性

@pytest.mark.parametrize("pos", ALL, ids=IDS)
def test_position_is_playable(pos):
    """每道题：棋盘合法、无既成五连、轮走方有合法着法。"""
    b = pos["board"]
    assert b.shape == (19, 19)
    assert set(np.unique(b)).issubset({0, 1, 2}), "只允许 0/1/2"

    nb = int((b == 1).sum())
    nw = int((b == 2).sum())
    assert abs(nb - nw) <= 1, f"黑白手数失衡: 黑{nb} 白{nw}"
    if pos["to_move"] == 1:
        assert nb == nw, "轮到黑走时黑白手数必须相等"
    else:
        assert nb == nw + 1, "轮到白走时黑应比白多一子"

    assert not _has_five(b, 1), "黑方已连五，题目无效"
    assert not _has_five(b, 2), "白方已连五，题目无效"
    assert (b == 0).any(), "无空点"


@pytest.mark.parametrize("pos", ALL, ids=IDS)
def test_position_expectation_is_wellformed(pos):
    """每道题必须有可判定的期望，且期望坐标未被占用。"""
    exp = pos["expect"]
    assert exp, f"{pos['id']} 没有任何期望，无法判定"
    # 期望必须落在"可判定"的键上。三种判据的分工：
    #   must_play / avoid —— 对**着法**的断言（正解唯一或错着唯一时用）
    #   mate_sign          —— 对**分值**的断言（必败/必胜局面用）
    #   opp_vcf            —— 对**局面**的断言（对手有连续冲四杀，由 Phase 5
    #                         的 VCF 独立证明，与搜索深度和时间无关）
    # 前两种读的是引擎的输出，第三种读的是局面的性质 —— B4 是后者的典型：
    # 那里引擎的输出（静态分）是对的，而"白必败"这件事从未被证明过。
    assert any(k in exp for k in ("must_play", "avoid", "mate_sign", "opp_vcf")), \
        f"{pos['id']} 的期望键都无法判定: {sorted(exp)}"

    b = pos["board"]
    for key in ("must_play", "avoid"):
        for r, c in exp.get(key, []):
            assert 0 <= r < 19 and 0 <= c < 19, f"{key} 坐标越界: {r},{c}"
            assert b[r][c] == 0, f"{key} 坐标已有子: {r},{c}"

    # 要求"必须走某点"时，该点不能同时出现在 avoid 里
    overlap = set(exp.get("must_play", [])) & set(exp.get("avoid", []))
    assert not overlap, f"must_play 与 avoid 冲突: {overlap}"


def test_historic_positions_have_source():
    """历史局面必须留下出处，否则无法复查。"""
    hist = [p for p in ALL if p["group"] == "historic"]
    assert hist, "应至少包含一条历史局面"
    for p in hist:
        assert p["source"] and len(p["source"]) > 8, f"{p['id']} 缺少出处"


# ---------------------------------------------------------------- 2. 冻结基线

def test_legacy_reproduces_the_documented_blunder(legacy_engine):
    """旧引擎在 B1 上必错 —— 这是本仓库进行核心重写的原始证据。

    若此测试失败，说明 tools/legacy_engine.py 被改动过，基线不再可信。
    """
    pos = next(p for p in ALL if p["id"] == "B1_blunder_233336_p10")
    res = P.evaluate_position(legacy_engine, pos, 1)
    assert not res["ok"], "旧引擎居然走对了？冻结快照可能已被修改"
    assert tuple(res["move"]) == (8, 9), (
        f"旧引擎应走 K9=(8,9)，实际 {res['move']}；冻结快照可能已被修改")
    # 白方在这里已经必败，所以"走哪一格"本来就不该是判据（挡 (7,7) 与挡
    # (11,11) 同样输）—— 能区分对错的只有**有没有报出必败**。旧引擎没有：
    # 它给出的 -12.5e6 落在自己复合静态分的量程里（拼命模式的攻防加权可以
    # 到 ±3.45e7），既不是 1e7±64 的将杀带，也没有 score_type 字段可查，
    # 于是 `_claims_mate` 判否 —— 界面看到的是"一个很大的负数"，不是"死棋"。
    assert res["checks"].get("mate_detected") is False
    assert res["checks"].get("mate_sign") is None


def test_legacy_detects_loss_but_still_plays_wrong(legacy_engine):
    """旧引擎能算出"我要输了"，却仍从排序表里挑出错着 —— B1 的完整形态。

    注：分数绝对值随难度浮动（1 级 -12.5e6，2 级 -8.8e6），因为它不是真正的
    将杀分而是静态评估的混合；故阈值取 -1e6（"局面严重不利"的量级），
    真正的断言是"看见危险却走错"这对组合。
    """
    pos = next(p for p in ALL if p["id"] == "B1_blunder_233336_p10")
    res = P.evaluate_position(legacy_engine, pos, 1)
    assert res["score"] is not None and res["score"] < -1e6, (
        f"旧引擎在此局面应报出严重不利的负分，实际 {res['score']}")
    assert tuple(res["move"]) == (8, 9), (
        f"却仍走 K9=(8,9)，实际 {res['move']}")


# ---------------------------------------------------------------- 3. 新引擎

# 这两条用例曾经挂着 `@pytest.mark.xfail(strict=False)`：Phase 1 的 engine.py
# 是旧引擎的逐字搬迁，必然在旧引擎失败的那些题上同样失败，所以当时它们
# 是"Phase 3-5 的验收目标"而非回归项。
#
# **标记已移除（Phase 3-5 完成后）**，理由不只是"目标达成了"：
# `strict=False` 之下，**回归也会变成 XFAIL 而不是 FAIL** —— 题库里任何一题
# 被写坏，测试都会安静通过，只在摘要里多一个 xfail。这正是"覆盖率假象"
# 的形态。标记的意义是"允许它失败"，而这件事现在不成立了。


@pytest.mark.parametrize("pos", ALL, ids=IDS)
def test_new_engine_solves_position(new_engine, pos):
    """新引擎逐题判定。"""
    res = P.evaluate_position(new_engine, pos, 3)
    assert res["ok"], (
        f"{pos['id']} 未通过：走 {res['move_sgf']} "
        f"val={res['score']} 检查={res['checks']}\n"
        f"期望 {pos['expect']}\n{pos['note']}")


def test_new_engine_overall_rate(new_engine):
    """整体解题率门槛（方案要求 >= 80%，实测 9/9 = 100%）。"""
    results = [P.evaluate_position(new_engine, p, 3) for p in ALL]
    n_pass = sum(1 for r in results if r["ok"])
    rate = n_pass / len(results)
    assert rate >= 0.80, (
        f"解题率 {rate:.1%} < 80%：" +
        ", ".join(r["id"] for r in results if not r["ok"]))

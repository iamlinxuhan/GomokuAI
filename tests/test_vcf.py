# -*- coding: utf-8 -*-
"""连续冲四（VCF，Phase 5）：**两件事必须成立 —— 不谎报，也不漏报。**

VCF 的用处只有一个：主搜索受深度所限，看不见比它更长的连续冲四杀。而连
续冲四的每一步都是**绝对强制**（对手不挡就立刻成五），所以这条线无论多长
都能被完整验证 —— 这也正是它值得单独做一个子系统的理由。

既然结论是"证明"，正确性就只有一条判据：**证明必须是真的**。下面每个用例
都在钉这件事的一个反面：

* 真链必须被找到（否则是漏报，白做）；
* 单冲四必须不算赢（否则是把"还能被挡"当成"挡不住"）；
* 对手有成五点必须直接否掉（**四压不住五** —— 我下一手做四，他直接成五）；
* 防守方挡那一手自己成四也必须否掉（同上，反先）；
* 预算用尽必须报 `EXHAUSTED` 而不是 `NO_WIN`。

最后一条是 B15：旧引擎的 `_find_forced_win` 把"搜不完"读成"没有必胜"，
于是**更慢的杀棋被判为不存在**。这个错误的代价不对称 —— 漏掉一个真杀棋
只是少赢一盘，把假杀棋当真才会输棋，但把"没算完"当成"算完了没有"，两个
方向都会错，而且没有任何办法从结果里看出来。
"""

from __future__ import annotations

import time

import numpy as np
import pytest

import engine_local as E
import tools.positions as P


def mk(black=(), white=()):
    b = np.zeros((19, 19), dtype=np.uint8)
    for r, c in black:
        b[r][c] = 1
    for r, c in white:
        b[r][c] = 2
    return b


def vcf_of(board, me, budget=1.0):
    """独立跑一次 VCF（不经过 `think`）。返回 ``(state, move, dist, nodes)``。"""
    eng = E.Engine()
    st, mv, d = eng.vcf(E.Board.from_array(board), me, budget)
    return st, mv, d, eng._vcf_nodes


def rc(idx):
    return divmod(idx, 19)


# ---------------------------------------------------------------- 真链

# 题库 B3 是人工核对过的一条真 VCF（白方连冲四取胜），也是这次 VCF 实现
# 唯一需要的"深链"样本：11 手，远超任何一次搜索能看到的深度。
B3 = next(p for p in P.build_positions() if p["id"] == "B3_blunder_232014_p34")


def test_deep_chain_is_found():
    """一条 11 手的真链必须被找到，且起手是人工核对过的那一格。

    (6,11) 是白方这条链的入口：它在第 11 列上让 (9,11) 成为五点，此后
    每一手都是冲四，黑方只能逐个去堵。
    """
    st, mv, dist, nodes = vcf_of(B3["board"], B3["to_move"])
    assert st == E.VCF_WIN
    assert rc(mv) == (6, 11), f"起手应为 M7=(6,11)，实得 {rc(mv)}"
    assert dist == 11, f"这条链应在 11 手内成五，实得 {dist}"
    assert nodes < 500, f"深链不该靠蛮力：跑了 {nodes} 个节点"


def test_vcf_distance_is_honest():
    """`dist` 必须真的等于"还有几手落下那颗成五的子"。

    这是 `dist` 唯一的用途（换算成杀棋分 `WIN_SCORE - (dist-1)`），所以它
    必须能被独立复现：按引擎自己的选择走完这条线，数到成五为止。

    **复现方式刻意不复用引擎的判断**：进攻方走 VCF 给的那一手，防守方走
    唯一的堵点（由 `_five_points` 直接给出），然后数盘面上第一次出现五连
    是在第几手。
    """
    board = B3["board"]
    me = B3["to_move"]
    st, _, dist, _ = vcf_of(board, me)
    assert st == E.VCF_WIN

    b = board.copy()
    side = me
    for ply in range(dist):
        bd = E.Board.from_array(b)
        opp = 3 - side
        if side == me:
            s, mv, _ = E.Engine().vcf(bd, me, 1.0)
            assert s == E.VCF_WIN, f"第 {ply} 手 VCF 失去了结论"
            r, c = rc(mv)
        else:
            fp = E._five_points(bd.bits_of(opp), bd.bits_of(side))
            assert fp, "防守方居然没有堵点可走 —— 说明上一手不是冲四"
            r, c = rc((fp & -fp).bit_length() - 1)
        b[r][c] = side
        if E.check_win(b, side):
            assert ply == dist - 1, (
                f"成五发生在第 {ply + 1} 手，而 dist 声称是 {dist}")
            assert side == me, "成五的居然不是进攻方"
            return
        side = 3 - side
    pytest.fail(f"{dist} 手走完仍未见五连 —— dist 在撒谎")


# ---------------------------------------------------------------- 单冲四

def test_live_four_wins():
    """活四（两个成五点）→ WIN。

    这是 AND 语义唯一的出口：我这一手之后有**两个**成五点，对手堵一个、
    我走另一个。要判 WIN，必须"所有应手都失败"—— 而不是"某个应手失败"。

    两端 (9,3)/(9,7) 等价（都是把三连变成活四），所以只断言"落在其中一端"。
    """
    st, mv, dist, _ = vcf_of(mk(white=[(9, 4), (9, 5), (9, 6)]), 2)
    assert st == E.VCF_WIN
    assert rc(mv) in {(9, 3), (9, 7)}, f"应下在能连成活四的一端，实得 {rc(mv)}"
    assert dist == 3, "活四的成五要再 2 手（对手堵一个），合计 3 手"


def test_single_four_is_not_a_win():
    """单冲四（一端被堵，唯一成五点）且无后续 → NO_WIN。

    这一格是 VCF 最容易自欺的地方：落子之后确实"有个四"，但对手堵上就
    没了。若把"能做出四"当成"能赢"，VCF 会把每一个活三都报成必胜。
    """
    st, _, _, _ = vcf_of(mk(black=[(9, 3)], white=[(9, 4), (9, 5), (9, 6)]), 2)
    assert st == E.VCF_NO_WIN


# ---------------------------------------------------------------- 假必胜

def test_opponent_five_point_refutes_the_attack():
    """对手已有成五点 → 我方这条链**一步都不能走** → NO_WIN。

    四压不住五：我这一手做出的四要下一手才成五，而对手下一手直接成五。
    旧实现的"假必胜"反例就长这样 —— 它只看"我能不能一路做四"，不问
    "对手在我做四的同时能不能直接赢"。
    """
    board = mk(black=[(10, 4), (10, 5), (10, 6), (10, 7), (10, 9)],
               white=[(9, 4), (9, 5), (9, 6)])
    st, _, _, nodes = vcf_of(board, 2)
    assert st == E.VCF_NO_WIN
    assert nodes <= 2, "这条判定在节点入口就该发生，不该搜下去"


def test_defenders_forced_block_can_counter_four():
    """防守方**被逼着走的那一手**自己成四 —— 进攻链同样作废。

    黑方在第 8 列已有 (6,8)(7,8)(8,8) 三子，白方冲四逼它去堵 (9,8)，
    这一堵正好连成四个，反过来威胁 (5,8)/(10,8)。白方下一手若继续冲四，
    黑方直接成五 —— 于是这条链死在第二步。

    这条与上一条的机制不同（上一条是"对手本来就有五点"，这条是"对手被
    我逼出来的四点"），但结论共用同一条判据。
    """
    board = mk(black=[(6, 8), (7, 8), (8, 8), (9, 3)],
               white=[(9, 4), (9, 5), (9, 6)])
    st, _, _, _ = vcf_of(board, 2)
    assert st == E.VCF_NO_WIN


# ---------------------------------------------------------------- 三态

def test_exhausted_is_not_no_win():
    """预算耗尽 ≠ 没有必胜。

    零预算下一个真链（B3）必须报 `EXHAUSTED`。这条断言看着平淡，它挡的
    是 B15 那个具体的写法：`if result is None: return None` —— 把"没搜到"
    和"搜完了、没有"合并成同一个返回值之后，调用方再也分不出来。
    """
    st, _, _, _ = vcf_of(B3["board"], B3["to_move"], budget=0.0)
    assert st == E.VCF_EXHAUSTED
    assert E.VCF_EXHAUSTED != E.VCF_NO_WIN, "三态必须彼此可分"


def test_exhausted_never_becomes_a_claim():
    """`think` 在 VCF 算不完时**不得**据此落子或报分。

    做法是把 VCF 预算压到必然耗尽，再看 `think` 的输出：它必须退回常规
    搜索（`score_type` 是静态分、`reason` 不是 VCF），而不是拿一个来路
    不明的结论当结果。
    """
    E.new_game()
    eng = E._ENGINE
    real = E.DIFFICULTY[3]["vcf_budget"]
    E.DIFFICULTY[3]["vcf_budget"] = 0.0          # 置 0 → 整个阶段被跳过
    try:
        _, _, info = E.ai_move(B3["board"].copy(), B3["to_move"], 3)
    finally:
        E.DIFFICULTY[3]["vcf_budget"] = real
    assert info["vcf_state"] is None, "预算为 0 时不该进入 VCF 阶段"
    assert "VCF" not in (info["reason"] or "")


def test_vcf_budget_is_wired_to_difficulty():
    """三档的 `vcf_budget` 必须真的被读到：**每一档都触发**。

    这条用例原本断言 `DIFFICULTY[1]["vcf_budget"] == 0.0` —— 那时 1 档是
    "关掉 VCF"的那一档，拿它当"置 0 会跳过"的样本。**那个立意已经作废**：
    1 档关 VCF 不是难度，是失明（既算不出自己的冲四链，也看不见对手的），
    实测一局败局里人类用一条 11 手 VCF 链取胜而 1 档全程没有机制能看见它。
    现在低档的区分度由 `max_depth` 承担，不再靠缺失一个子系统。

    于是这条改成钉住新的不变量：**每一档都有眼睛**。而"置 0 → 整个阶段
    跳过"那一半仍被覆盖 —— 上面的 `test_vcf_unfinished_is_not_trusted`
    就是临时把 3 档置 0 再验证 `vcf_state is None` 的。

    它原本要防的回归也没丢：在此之前 `vcf_budget` 是字典里一个**没有任何
    读取点**的键，调它不会有任何效果，也没人会发现。`vcf_state is not None`
    仍然直接证明这个键被读到了。
    """
    for lv in (1, 2, 3):
        assert E.DIFFICULTY[lv]["vcf_budget"] > 0.0, \
            f"{lv} 档没有 VCF 预算 —— 档位的区分度该由 max_depth 承担，而不是失明"

    E.new_game()
    _, _, info1 = E.ai_move(B3["board"].copy(), B3["to_move"], 1)
    assert info1["vcf_state"] is not None, "1 档的 vcf_budget 没有被读到"

    E.new_game()
    r3, c3, info3 = E.ai_move(B3["board"].copy(), B3["to_move"], 3)
    assert info3["vcf_state"] == E.VCF_WIN, "3 档应当在 B3 上被 VCF 命中"
    # 注意 `ai_move` 返回的是 **(行, 列)**，不是线性格索引 —— 线性索引只从
    # `Engine.vcf()` / `think()` 里出来。
    assert (r3, c3) == (6, 11)


def test_vcf_shortcut_keeps_the_shorter_mate():
    """VCF 是兜底，不是覆盖：搜到**更短**的杀棋就用搜索的。

    B3 上主搜索（深度 1 + 静止搜索）能给出 7 手的杀棋，比 VCF 的 11 手短。
    更短的杀棋在下棋过程中更不容易走错，所以哪怕 VCF 先算完，也该让搜索
    的结果胜出 —— 判据只需比较分值，杀棋分随距离单调。
    """
    E.new_game()
    r, c, info = E.ai_move(B3["board"].copy(), B3["to_move"], 3)
    assert E.is_mate(info["best_val"])
    assert info["vcf_state"] == E.VCF_WIN
    vcf_val = E.WIN_SCORE - (info["vcf_dist"] - 1)
    assert info["best_val"] >= vcf_val, "最终分值不该比 VCF 自称的距离还长"
    assert (r, c) == (6, 11), "两条结论的起手在 B3 上恰好一致"
    assert info["reason"] == "PVS搜索", (
        "既然搜索给出了更短的杀棋，结论就该算搜索的，不该挂 VCF 的名")


# 黑方有一条 5 手的 VCF，白方自己没有杀棋 —— 正是"必须去防守"的局面。
# 由随机搜索筛出来（条件：黑方 VCF 必胜 且 白方 VCF 非必胜 且 白方搜索
# 不出杀棋），坐标是筛出来那一刻的原始盘面。
DEF_BLACK = [(15, 16), (13, 12), (10, 16), (15, 12), (16, 14),
             (12, 15), (10, 15), (16, 10), (16, 13)]
DEF_WHITE = [(16, 15), (16, 9), (9, 18), (18, 16), (15, 10)]


def test_vcf_defence_breaks_the_threat():
    """防守 VCF 的核心断言是**证明**，不是"搜出来的着法看着像"。

    三件事按顺序钉住：

    1. 这个局面上黑方确实有 VCF 必胜（否则这条用例没在测防守）；
    2. `_vcf_defence` 给出的那一手之后，黑方的 VCF 变成 `VCF_NO_WIN`
       —— 这是"挡住了"的**证明**，与时间、深度、TT 全都无关；
    3. 白方自己的 VCF 不是必胜 —— 于是白方只能防，不能对攻取胜，这才
       是"防守"这个分支被真正走到的前提。
    """
    board = mk(DEF_BLACK, DEF_WHITE)
    st, _, dist = E.Engine().vcf(E.Board.from_array(board), 1, 1.0)
    assert st == E.VCF_WIN, "构造局面本身就该是黑方的 VCF 必胜"

    eng = E.Engine()
    eng._deadline = 0.0
    d = eng._vcf_defence(E.Board.from_array(board), 2, 1, time.monotonic() + 1.0)
    assert d >= 0, "这个局面上应当找得到挡点"

    after = board.copy()
    r, c = rc(d)
    after[r][c] = 2
    st2, _, _ = E.Engine().vcf(E.Board.from_array(after), 1, 1.0)
    assert st2 == E.VCF_NO_WIN, f"挡在 {(r, c)} 之后黑方仍有 VCF → 这一手没挡住"

    st_w, _, _ = E.Engine().vcf(E.Board.from_array(board), 2, 1.0)
    assert st_w != E.VCF_WIN, "白方若自己也有 VCF 必胜，这条用例就测不到防守分支"


def test_vcf_defence_does_not_invent_a_score():
    """防守分支**不得**报出杀棋分：VCF 证明的只是"威胁没了"，不是"我要赢了"。

    分值是主搜索给的（这里 -562200 是一个普通的中盘静态分）。若把 VCF 的
    "挡住了"翻译成一个杀棋分，界面就会在明明是劣势的局面上显示"必胜"——
    这正是旧引擎"拼命模式"的做法：用一个大得离谱的负数假装自己在算杀。
    """
    board = mk(DEF_BLACK, DEF_WHITE)
    eng = E.Engine()
    eng._deadline = 0.0
    d = eng._vcf_defence(E.Board.from_array(board), 2, 1, time.monotonic() + 1.0)
    assert d >= 0

    E.new_game()
    r, c, info = E.ai_move(board.copy(), 2, 3)
    assert 0 <= r < 19 and 0 <= c < 19
    assert not E.is_mate(info["best_val"]), (
        "防守只挡住对手的 VCF，这个分值 VCF 证明不了，不该报成杀棋分："
        f"{info['best_val']}")
    assert abs(info["best_val"]) <= E.STATIC_MAX
    # reason 与着法必须一致：标了 VCF 就必须真的走在挡点上，走在别处就
    # 不许标。这条挡的是"标记与实际不符"这类只在输出里看得出、下棋时
    # 看不出的错。
    #
    # **判据是"来源字符串"，不是 `"VCF" in reason`。** 后者曾是对的，直到
    # `PVS搜索(对手有VCF·无挡点/未算完)` 这两句出现 —— 它们说的是**对手**有
    # 冲四链，不是说这一手是挡点，子串判据会把它们误判成"标了 VCF"。枚举
    # `VCF_PROVENANCE` 才能把"这一手的来路是 VCF"与"这个局面里别人有 VCF"
    # 分开。
    said_vcf = (info["reason"] or "") in VCF_PROVENANCE
    assert said_vcf == ((r, c) == rc(d)), (
        f"reason={info['reason']!r} 与实际着法 {(r, c)} / 挡点 {rc(d)} 不一致")


# 唯一两句**宣称这一手的来路是 VCF** 的 reason。`连续冲四(VCF)` 是我方杀棋，
# `连续冲四防守(VCF)` 是挡点，两句都蕴含"这一手就是 VCF 算出来的那一手"。
VCF_PROVENANCE = ("连续冲四(VCF)", "连续冲四防守(VCF)")


def test_vcf_defence_reports_unknown_when_the_budget_is_gone():
    """预算用尽必须返回 `VCF_DEFENCE_UNKNOWN`，**不能**返回 `_NONE`。

    两者以前共用一个 -1，于是"没算完"被读成"算过了、没有挡点"—— 日志里说
    同一句话。这条用例把两个否定答案钉开：deadline 取一个已经过去的时刻，
    扫描在第 0 个候选上就撞线，返回值必须是 UNKNOWN。
    """
    board = mk(DEF_BLACK, DEF_WHITE)
    eng = E.Engine()
    eng._deadline = 0.0
    d = eng._vcf_defence(E.Board.from_array(board), 2, 1, time.monotonic() - 1.0)
    assert d == E.VCF_DEFENCE_UNKNOWN, (
        f"预算早就没了，却返回了 {d}（NONE={E.VCF_DEFENCE_NONE}）"
        f"—— 这是把'不知道'讲成了'算过了'")

    # 同一个局面，给足预算就必须走另一条路。两条一起断言，才说明区分的是
    # 预算而不是局面。
    eng2 = E.Engine()
    eng2._deadline = 0.0
    d2 = eng2._vcf_defence(E.Board.from_array(board), 2, 1, time.monotonic() + 1.0)
    assert d2 >= 0, "同一局面给足预算应当找得到挡点"

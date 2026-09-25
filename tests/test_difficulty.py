# -*- coding: utf-8 -*-
"""难度档：**时间是硬上限，深度是硬上限，吞吐是门槛。**

三条里前两条是"不许违约"，第三条是"不许退化"。它们各自挡的错误完全不同：

* **时间**：`think` 收下 `level` 后按 `DIFFICULTY[level]["time"]` 设一个
  **硬**截止（`t0 + time * (1 - RESERVE)`）。写错截止的后果不是慢，而是
  界面卡住 —— 主线程在等 `ai_move` 返回，超时的那一步会一直占着按钮。
* **深度**：`max_depth` 必须真的封顶。迭代加深若少了这个上限，一个
  "看起来安静、实际到处是四"的中局会让 1 档也跑到 20 层，于是 1 档的
  3 秒预算变成"跑到超时为止"。
* **吞吐**：nps 掉下来时前两条**照样通过** —— 搜索更浅、更快返回，时间
  与深度都不违约。所以吞吐必须单独测。

时间用例的容差是 `time + 150ms`：`_poll` 每 1024 个节点才查一次表，一次
表查询之间可能跑完相当多节点；另外 `Board.from_array`、候选生成、计时本身
都在预算之外。这里的余量是为了让"合规"这件事有确定的结果，而不是把
多出来的几十毫秒当成超时。

**VCF 的预算不在这里测**：`DIFFICULTY[level]["vcf_budget"]` 在 Phase 5 之前
只是一个没有任何读取点的声明，现在 `think` 会读它，三档各自有独立预算。
"每一档都真的有 VCF"这条断言放在
`tests/test_vcf.py::test_vcf_budget_is_wired_to_difficulty`。

**下面还有一条"低档够不够跑到自己的上限"**（`test_low_level_reaches_its_own_depth_cap`）——
它是 1 档失明那个缺陷的直接护栏，理由写在那条用例里。

VCF 阶段花掉的时间**计入**本文件的时间上限：`vcf_phase_deadline` 取自
`t0 + vcf_budget`，而 VCF 内部又用 `min(它, self._deadline)` 收口，所以
多加一个子系统不会让墙钟变长 —— 这正是下面 `test_wall_clock_*` 要挡的事。
"""

from __future__ import annotations

import time

import numpy as np
import pytest

import engine_local as E
import tools.positions as P

TIME_SLACK_MS = 150.0

POSITIONS = P.build_positions()

# 静止搜索占比不高的局面 —— 吞吐门槛在这几个上才有意义。
# 其余局面的 nps 会被 `_hot_points`（约 11µs/次，静止节点每层调用两次）
# 拉低到 25-28k，见 `test_nps_on_quiescence_bound_positions_is_reported`。
SEARCH_BOUND = {"A1_block_closed_four", "A5_block_open_three"}
NPS_GATE = 30000

# 这几档在中局局面下会一路搜到 `max_depth` 封顶（不是找到杀棋就退出）。
# 用 id 而不是"运行期测一下是否 mate"来挑，是为了让断言确定：
# 如果哪天引擎在这些局面上改报杀棋，测试应当**失败**并让人来看，
# 而不是自动跳过。
DEEP_POSITIONS = {"A1_block_closed_four", "A5_block_open_three"}

# 3 档在 `DEEP_POSITIONS` 上实际能到达的层数下界。
#
# **这个数字曾被写成 10，而 10 从来没有被真正达到过。** 上一版的
# `engine._ZOBRIST` 把 `random.Random(seed)` 写在了生成器表达式内部，整张表
# 退化成常量，`Board.hash` 只剩棋子数的奇偶性，置换表实际只有两个键 —— 迭代
# 加深于是每轮都命中同一个错误条目、**在 ply=5 就停下却把 `done` 记成循环
# 变量**，报出 depth=24。方案里的 "≥10" 正是在那批数字上定的，它测的是
# "循环跑了几轮"，不是"搜了多深"。
#
# 修好哈希之后，实测下界就是 5。**这不是把门槛调低来迁就现状**，而是换成了
# 一个真实的量：本设计是全宽 PVS + 置换表 + 静止搜索 + VCF，没有任何现代
# 裁剪（LMR / 空着裁剪 / 无用着裁剪），实测每层有效分支因子约 20
# （A1 从 5 层到 6 层节点数 46k → 1.12M）。13 秒预算约 65 万节点、nps 5 万，
# 要搜到 10 层需要分支因子降到 3.7 附近 —— 那是加裁剪才能谈的事。
#
# **5 是稳定下界，不是贴着实测值卡出来的**：13 秒预算里 d=5 在 A1 只用 0.83s、
# 在 A5 用 4.17s 就完成了，而 d=6 分别需要 19.9s / 28.4s，差着一倍以上。
# 也就是说这个断言真正挡的是"搜索塌掉"（TT 失效、剪枝写坏、时间控制提前
# 掐断），而不是在 5 和 6 之间抖动。
DEPTH_FLOOR = 5


def search(pos, level):
    E.new_game()
    t0 = time.monotonic()
    r, c, info = E.ai_move(pos["board"].copy(), pos["to_move"], level)
    info = dict(info)
    info["wall_ms"] = (time.monotonic() - t0) * 1000.0
    return r, c, info


# ------------------------------------------------------------------ 时间

@pytest.mark.parametrize("level", [1, 2, 3])
@pytest.mark.parametrize("pos", POSITIONS, ids=lambda p: p["id"])
def test_wall_clock_respects_the_hard_limit(new_engine, pos, level):
    """每一档、每一个局面都必须落在它自己的时间预算内。

    这条要在**题库全体**上跑而不是一个代表局面：超时最容易发生在
    "静止搜索收不住"的局面（连续四很多、评估又给不出剪枝），而那种局面
    恰好在随便挑的样本里出现率很低。
    """
    budget_ms = E.DIFFICULTY[level]["time"] * 1000.0
    _, _, info = search(pos, level)
    assert info["wall_ms"] <= budget_ms + TIME_SLACK_MS, \
        (f"{pos['id']} L{level} 用了 {info['wall_ms']:.0f}ms，"
         f"预算 {budget_ms:.0f}ms")


def test_higher_levels_get_more_time_not_less(new_engine):
    """难度预算必须单调递增 —— 防的是"改错了字典顺序"这类低级错。"""
    times = [E.DIFFICULTY[l]["time"] for l in (1, 2, 3)]
    depths = [E.DIFFICULTY[l]["max_depth"] for l in (1, 2, 3)]
    assert times == sorted(times) and times[0] < times[-1]
    assert depths == sorted(depths) and depths[0] < depths[-1]


# ------------------------------------------------------------------ 深度

@pytest.mark.parametrize("level", [1, 2, 3])
@pytest.mark.parametrize("pos", POSITIONS, ids=lambda p: p["id"])
def test_depth_never_exceeds_the_level_cap(new_engine, pos, level):
    """`actual_depth` 不得超过本档的 `max_depth`。

    超了说明迭代加深的循环上界写错了（比如统一用了 3 档的值），后果是
    低档在小局面上耗掉高档的时间。
    """
    _, _, info = search(pos, level)
    assert info["actual_depth"] <= E.DIFFICULTY[level]["max_depth"], \
        f"{pos['id']} L{level} 搜到了 {info['actual_depth']} 层"


@pytest.mark.perf
@pytest.mark.parametrize("pos", POSITIONS, ids=lambda p: p["id"])
def test_deep_positions_reach_the_measured_depth_floor(new_engine, pos):
    """3 档在这些中局局面上必须**真的**搜到 `DEPTH_FLOOR` 层以上。

    这条用例曾叫 `..._reach_the_third_level_cap`、门槛是 10 —— 名字与门槛
    都不成立，理由见 `DEPTH_FLOOR` 上方那段：10 从未被达到过，它测的是
    迭代加深跑了几轮，而当时哈希退化让循环"跑满却只搜到 5 层"。

    现在它挡的是**搜索塌掉**：置换表失效、剪枝写坏、时间控制提前掐断、
    迭代加深的 `done` 记账错位 —— 这些都会让深度掉下来，而"时间合规"
    对此完全无感（搜得浅只会让时间更宽裕）。

    打 ``perf`` 标记：15 秒预算内到达的层数直接取决于机器速度，慢机器上
    可能只到 4 层。门槛本身（"搜索没塌"）是对的，只是不该在 CI 上断言。
    """
    if pos["id"] not in DEEP_POSITIONS:
        pytest.skip("该局面存在速胜/速败线，迭代加深会提前收敛，深度无意义")
    _, _, info = search(pos, 3)
    assert info["actual_depth"] >= DEPTH_FLOOR, \
        f"{pos['id']} L3 只搜到 {info['actual_depth']} 层（下界 {DEPTH_FLOOR}）"


# 败局回归局面 —— `game_log_20260919_183202.txt` 第 27 手之后的盘面，白方该走。
#
# **为什么不用题库里现成的局面。** 题库那几个"搜索主导"的局面太便宜了：实测
# A1 和 A5 在 **1.0 秒**预算下就已经跑到第 4 层，拿它们断言"1 档够不够跑到
# 自己的上限"只会得到一个恒真的测试。而这个真实中局是有牙的 —— 见用例里的
# 实测数字。
#
# 白方在这个局面已经**必败**（黑方有一条 11 手 VCF 链，见日志 31 手起）。
# 这里不用它断言"认得出必败"（1 档的 `max_depth` 只有 4，够不到那条 11 手
# 的链，那是 3 档的活），只用它断言**时间够不够**。
LOST_183202_P28 = (
    (9, 9, 1), (8, 8, 2), (9, 7, 1),
    (9, 8, 2), (10, 8, 1), (7, 8, 2),
    (11, 7, 1), (8, 10, 2), (11, 9, 1),
    (8, 6, 2), (12, 6, 1), (13, 5, 2),
    (12, 10, 1), (13, 11, 2), (11, 8, 1),
    (6, 8, 2), (5, 8, 1), (11, 6, 2),
    (11, 10, 1), (11, 11, 2), (8, 7, 1),
    (10, 7, 2), (8, 9, 1), (12, 5, 2),
    (13, 4, 1), (10, 9, 2), (10, 10, 1),
)
LOST_183202_TO_MOVE = 2


@pytest.mark.perf
def test_low_level_reaches_its_own_depth_cap(new_engine):
    """**1 档的时间必须够它跑到自己的深度上限。**

    这是"1 档失明"那个缺陷的直接护栏。诊断上面那局 45 手的败局时查出病灶：
    1 档取 `max_depth` 的**安全阀从来没有生效过**，时钟才是实际约束 —— 而
    时钟给的层数在这个局面上恰好给出**反向的结论**：

    | 预算 | 到达深度 | 分值 | 读作 |
    |---|---|---|---|
    | 1.275 s（旧的 1 档） | 3 | -560 | "略处下风" |
    | ≥ 2.0 s | 4 | -698910 | "已经快死了" |

    修法就是把 1 档的预算抬到时间不再是约束（见 `DIFFICULTY` 上方注释）。
    这条用例把"安全阀必须真的成为约束"钉死：哪次改动让时间再次不够用，深度
    就掉到上限之下，这里立刻说话 —— 而"时间合规"那几条对此**完全无感**
    （搜得浅只会让时间更宽裕，照样合规）。

    **只对 1 档断言**：它是唯一一个"上限低到时间本该绰绰有余"的档位。2/3 档
    的上限是 10/24，在同样的中局上永远够不到，断言它们跑满上限是错的。

    打 ``perf`` 标记：慢机器上 3 秒可能真的只够 3 层。
    """
    board = np.zeros((E.BOARD_SIZE, E.BOARD_SIZE), dtype=np.uint8)
    for r, c, p in LOST_183202_P28:
        board[r][c] = p

    cap = E.DIFFICULTY[1]["max_depth"]
    E.new_game()
    _, _, info = E.ai_move(board, LOST_183202_TO_MOVE, 1)
    assert info["actual_depth"] == cap, \
        (f"1 档在这个中局上只搜到 {info['actual_depth']} 层（上限 {cap}，"
         f"{info['time_ms']:.0f}ms）—— 时间不够它跑到自己的上限。"
         f"旧配置（1.275 s 预算）在这个局面上恰好停在第 3 层，"
         f"给出与前一层相反的分值；这正是它当年输掉那局的原因")


# 第 4 手局面 —— `game_log_20260922_235240.txt` 走到第 4 手之前的盘面（白方该走）。
# 由日志的"棋盘状态 @3"逐字换算：列字母表无 I（K=9、J=8），行从 0 起。
LOST_235240_P4 = ((7, 9, 1), (8, 8, 2), (9, 9, 1))      # *K8 OJ9 *K10
LOST_235240_TO_MOVE = 2


@pytest.mark.perf
def test_mid_level_sees_the_fifth_layer_on_the_pivot_move(new_engine):
    """**2 档的时间必须够它在这个局面上看清第 5 层。**

    这局（2 档执白、人类第 31 手 J8 胜）的胜负手在第 4 手：盘面只有三颗子时，
    第 4 层选 K9（-1000），第 5 层选 K7/K11（-170）。把这一手钉死重放
    （`tools/pivot_ab.py --budget 5.0`）：走 K9 得到**黑胜·第 31 手 J8，与日志
    逐手相同（31 手一手不差）**；走 K7 / K11 则是白方第 12 / 18 手反杀 ——
    两层给出胜负相反的结论，所以这一手是全局胜负手。

    当时的预算（5.0 × 0.85 = 4250ms）恰好压在临界点上：同一份输入重复搜，
    空载会走 K7、**起 6 个满载进程后 4/4 全走 K9** —— 选点随机器负载翻面。

    这条用例把"预算必须够到那一层"钉死。**它挡的正是 `test_wall_clock_*`
    看不见的那类回归**：搜得浅只会让时间更宽裕，时间合规那几条照样全绿。
    搜索塌掉（置换表失效、剪枝写坏、迭代加深记账错位）同样会让深度掉下来，
    也被这里抓住。

    断言 `>= 5` 而不是某个具体着法：两个选点（K7 / K11）都能赢下这一局，而
    "到达第 5 层"才是这条时限存在的理由 —— 1 档的上限只有 4（永远够不到），
    2 档的上限是 10。

    打 ``perf`` 标记：到达的层数直接取决于机器速度。
    """
    board = np.zeros((E.BOARD_SIZE, E.BOARD_SIZE), dtype=np.uint8)
    for r, c, p in LOST_235240_P4:
        board[r][c] = p

    E.new_game()
    _, _, info = E.ai_move(board, LOST_235240_TO_MOVE, 2)
    assert info["actual_depth"] >= 5, \
        (f"2 档在这个局面上只搜到 {info['actual_depth']} 层"
         f"（{info['time_ms']:.0f}ms）—— 时间不够它看清第 5 层。"
         f"第 4 层在这一手选 K9（-1000），第 5 层选 K7（-170）；"
         f"走 K9 会把这局下成原样的败局（黑胜·第 31 手 J8），"
         f"走 K7 则白方第 12 手 J7 反杀。"
         f"详见 `tools/pivot_ab.py`")


# ------------------------------------------------------------------ 吞吐

@pytest.mark.perf
@pytest.mark.parametrize("pos", POSITIONS, ids=lambda p: p["id"])
def test_nps_gate_on_search_bound_positions(new_engine, pos):
    """搜索主导的局面必须稳在 30k nps 以上。

    只在这几个局面上断言，理由写在模块 docstring 与下面那条用例里：
    静止搜索主导的局面 nps 天然更低（每个节点两次数 11µs 的位图），
    把门槛套上去只会得到一个恒红的测试。

    打 ``perf`` 标记：nps 是"每秒节点数"，CPU 一慢它就低，而它与代码
    有没有退化在数字上无法区分。本地（开发机）它是有效门禁，CI 上不是。
    """
    if pos["id"] not in SEARCH_BOUND:
        pytest.skip("静止搜索主导的局面，见 test_nps_on_quiescence_bound_positions")
    _, _, info = search(pos, 3)
    assert info["nps"] >= NPS_GATE, \
        (f"{pos['id']} L3 nps={info['nps']}，低于 {NPS_GATE}"
         f"（nodes={info['nodes']} time={info['wall_ms']:.0f}ms）")


@pytest.mark.perf
def test_nps_on_quiescence_bound_positions_is_reported(new_engine):
    """**已知代价**：静止搜索主导时 nps 会掉到 25-28k。

    这不是回归，是结构性开销 —— 静止节点每层要调两次 `_hot_points`
    （位图前缀/后缀/中间乘积，实测约 11µs），而它替代掉的是旧引擎每节点
    一次的 `_check_win_fast`（全盘 1170 次单元读取，约 0.44ms）。所以即使
    这个数字"低于门槛"，每节点的实际代价仍比旧引擎低两个数量级。

    这里**不设失败门槛**，只把数字钉在报告里：一旦它掉出这个区间，
    说明有人动了静止搜索的内循环，值得看一眼。
    """
    pos = next(p for p in POSITIONS if p["id"] == "B4_selfplay_g2_p17")
    _, _, info = search(pos, 3)
    ratio = info["qnode_ratio"]
    assert ratio > 0.5, f"这个局面本该由静止搜索主导，实得 {ratio:.2f}"
    assert info["nps"] > 15000, f"静止搜索吞吐崩了：{info['nps']} nps"

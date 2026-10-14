# -*- coding: utf-8 -*-
"""RIF 禁手规则（纯函数，零 Qt、零 numpy、零第三方依赖）。

这是**全项目唯一一份禁手判定实现**：``engine_ban`` 的搜索、``session`` 的判负、
GUI 的禁手标点都从这里取。``cpp/`` 侧有一份等价实现（``cpp/src/ban.h``），
两者靠 ``tests/test_ban_rules.py`` 的固定局面表对齐。

## 规则来源与三条容易搞反的地方

依据 RIF 官方规则（Rule 3 定义、Rule 9.2 判负）：

* **只有黑方受禁**，白方怎么下都不禁（Rule 9.2 只对 black 立法）。
* **五连优先于全部禁手，含长连。** Rule 9.2 的原话是"the game is won for white
  if black **without at the same time attaining five in a row**: a) makes an
  overline; b) makes a double-four; c) makes a double-three" —— "同时成五"是
  三种禁手的**共同前提**。反证：若长连优先于五连，则任何六连的任意子窗口必含
  一个五连窗口，"长连禁手"这条就永远触发不了，规则里不会留一条死条文。
* **长连是 ≥6，五连是恰好 5。** 本项目无禁手引擎里 ``has_five`` 把两者都当五
  （``engine_local._has_five_bits``），禁手模式下必须分开。

## 「四」怎么数（同线双四）

一个「四」= 一组 4 颗己方子 + 一个空位，补上即成为五连（**且该五连不是长连**）。

* 两个「四」不同，当且仅当**这 4 颗子的位置集合**不同。于是活四 ``_XXXX_``
  虽然有两个成五点，仍是**一个**四（左右两个窗口的 4 颗子是同一组）；
  而 ``X_XXX_X`` 的 ``{0,2,3,4}`` 与 ``{2,3,4,6}`` 是两组 → **同一线上的双四**，
  这就是「变形四四」（另有两头四四 / 长蛇四四 / 争龙四四几个别名），确实算禁手。
* **只数包含刚落这一子的那些四。** RIF 的判据要求两个四都由这一手形成。
  不然 "黑棋早有一组活四、这一手补了颗远处的子" 会被误判成四四。
* **补成五会变长连的四不算四。** 例：黑在 1,2,3,4,6，往 5 补出的是 1..6 的
  六连 —— 这个"四"不成立（补不出五），剩下的只有 {1,2,3,4} 一个四。

## 「三个」怎么数

活三 = 一组 3 颗己方子 + 一个空位，补上即成**活四**（四连且两端皆空），
且那个活四**两端的补五一端都不能是长连**。假活三（补出来的"活四"其实因为长连
补不出五）因此自动不算。

**某个方向上一旦成立一个四，该方向就不再计活三。** 否则 ``_XXXX_`` 里的
``{a,b,c}``、``{b,c,d}`` 这些 3 子子集会被当成活三，凭空造出三三禁手。
"""

from __future__ import annotations

EMPTY = 0
BLACK = 1
WHITE = 2

#: 禁手种类。``FOUL_NONE`` 也用于"这一手合法"与"这一手直接成五"两种情形 ——
#: 成五由调用方另判（``five_at``），本模块不把"赢"混进禁手种类里。
FOUL_NONE = 0
FOUL_OVERLINE = 1       # 长连（≥6）
FOUL_DOUBLE_FOUR = 2    # 四四
FOUL_DOUBLE_THREE = 3   # 三三

FOUL_NAMES = {
    FOUL_NONE: "",
    FOUL_OVERLINE: "长连",
    FOUL_DOUBLE_FOUR: "四四",
    FOUL_DOUBLE_THREE: "三三",
}

#: 与 ``engine_local._DIRS`` 同序同值（横 / 竖 / 主对角 / 副对角）。
_DIRS = ((0, 1), (1, 0), (1, 1), (1, -1))


# --------------------------------------------------------------------------
# 线的取用
# --------------------------------------------------------------------------


def _line_cells(board, r, c, dr, dc, n):
    """过 ``(r, c)`` 沿 ``(dr, dc)`` 的**整条线**，返回 ``(cells, center)``。

    取整条线而不取 ±5 的小窗：小窗的边界要额外处理"窗外的子还会不会连上"
    （长连判定正需要这个），整条线则天然把墙放在线端，没有边界特例。线上最多
    19 格，代价可以忽略 —— 判禁手本来就是每手一次的事，不在搜索热路径上。
    """
    rr, cc = r, c
    while 0 <= rr - dr < n and 0 <= cc - dc < n:
        rr -= dr
        cc -= dc
    cells = []
    center = -1
    i = 0
    while 0 <= rr < n and 0 <= cc < n:
        if rr == r and cc == c:
            center = i
        cells.append(int(board[rr][cc]))
        rr += dr
        cc += dc
        i += 1
    return cells, center


def _run_at(cells, pos, me):
    """假设 ``cells[pos]`` 是 ``me``（它现在可能是空点），过 ``pos`` 的最大连续段长。

    **它不看 ``cells[pos]`` 本身** —— 这是本模块所有"假设在此落子"的判定的共同
    基础：成五、四、活三都是"补一子会怎样"的问题，而那一子此刻还是空的。
    """
    n = len(cells)
    lo = pos
    while lo - 1 >= 0 and cells[lo - 1] == me:
        lo -= 1
    hi = pos
    while hi + 1 < n and cells[hi + 1] == me:
        hi += 1
    return hi - lo + 1


def _four_sets(cells, center, me):
    """本方向上、**包含 center** 的「四」的位置集合的集合。

    每个四记成它那 4 颗子的坐标元组（不是那个 5 窗）—— 这是"活四只算一个四"
    与"``X_XXX_X`` 算两个四"能同时成立的唯一记法，理由见模块文档。
    """
    n = len(cells)
    out = set()
    if n < 5:
        return out
    lo = max(0, center - 4)
    hi = min(n - 5, center)
    for start in range(lo, hi + 1):
        win = cells[start:start + 5]
        if win.count(me) != 4 or win.count(EMPTY) != 1:
            continue
        e = start + win.index(EMPTY)
        if _run_at(cells, e, me) > 5:
            # 补这一子出来的是长连，不是五连 —— 按定义它成不了五，不算四。
            continue
        out.add(tuple(start + i for i in range(5) if cells[start + i] == me))
    return out


def _three_sets(cells, center, me):
    """本方向上、**包含 center** 的「活三」的位置集合的集合。

    枚举每一个可能补上的空点 ``e``，若补上之后出现一个**活四**
    （四连 + 两端皆空 + 两端补五都不会变长连），且该活四同时盖住 ``e`` 与
    ``center``，那么"该活四去掉 e"的那 3 颗子就是一组的活三。
    """
    n = len(cells)
    out = set()
    if n < 4:
        return out
    for e in range(max(0, center - 4), min(n - 1, center + 4) + 1):
        if cells[e] != EMPTY:
            continue
        cells[e] = me
        try:
            for i in range(0, n - 3):
                if any(cells[i + k] != me for k in range(4)):
                    continue
                if i - 1 < 0 or i + 4 >= n:
                    continue
                if cells[i - 1] != EMPTY or cells[i + 4] != EMPTY:
                    continue
                # 活四的两端补五都必须合法：补成六连的那个方向不算活。
                if i - 2 >= 0 and cells[i - 2] == me:
                    continue
                if i + 5 < n and cells[i + 5] == me:
                    continue
                if not (i <= center <= i + 3 and i <= e <= i + 3):
                    continue
                out.add(tuple(i + j for j in range(4) if i + j != e))
        finally:
            cells[e] = EMPTY
    return out


def _line_judge(cells, center, me):
    """本方向的 ``(四的集合数, 活三的集合数)``。

    有四分的方向不计活三 —— 见模块文档最后一段（``_XXXX_`` 的 3 子子集）。
    """
    fours = _four_sets(cells, center, me)
    if fours:
        return len(fours), 0
    return 0, len(_three_sets(cells, center, me))


# --------------------------------------------------------------------------
# 公开判定
# --------------------------------------------------------------------------


def five_at(board, r, c, player):
    """在空点 ``(r, c)`` 落 ``player`` 后，是否形成**恰好五连**（长连不算）。

    禁手模式下"恰好五连"是黑方的胜利条件，且**优先于全部禁手**。
    """
    n = len(board)
    for dr, dc in _DIRS:
        cells, center = _line_cells(board, r, c, dr, dc, n)
        if _run_at(cells, center, player) == 5:
            return True
    return False


def foul_kind(board, r, c, player=BLACK):
    """在空点 ``(r, c)`` 落 ``player`` 后的禁手种类（``FOUL_*``）。

    ``player`` 不是黑（或该点已有子）时恒返回 ``FOUL_NONE`` —— 白方无禁手。

    **判定顺序就是规则本身**：先看恰好五连（成五则什么禁手都不算），再看长连，
    最后数四与活三。三步的次序不可换 —— 调换任意两步都会造出与 RIF 相反的
    结论，理由见模块文档。
    """
    if player != BLACK:
        return FOUL_NONE
    if not 0 <= r < len(board) or not 0 <= c < len(board):
        return FOUL_NONE
    if int(board[r][c]) != EMPTY:
        return FOUL_NONE

    n = len(board)
    lines = [_line_cells(board, r, c, dr, dc, n) for dr, dc in _DIRS]
    # 把"假设落下的那一子"放进线里。``_run_at`` 本来就假设该点是己方子，
    # 但数四与数三要**看得见**它（``_four_sets`` 是在窗里数己方子的），
    # 所以这里真的写进去。四条线是各自独立的副本，改动不会外溢到棋盘。
    for cells, center in lines:
        cells[center] = BLACK

    # 第一步：恰好五连 —— 黑胜，此后不再看任何禁手。
    for cells, center in lines:
        if _run_at(cells, center, BLACK) == 5:
            return FOUL_NONE

    # 第二步：长连。放在数四之前，因为长连一旦成立，这一手与"什么样的四/三"
    # 已经无关了（且此时它不可能同时成五，否则第一步就返回了）。
    for cells, center in lines:
        if _run_at(cells, center, BLACK) > 5:
            return FOUL_OVERLINE

    # 第三步：数四与活三。**先把四个方向都数完再下结论** —— 边数边判会在
    # "第一个方向只有 1 个四、第二个方向也只有 1 个四"的地方漏判四四。
    n_four = 0
    n_three = 0
    for cells, center in lines:
        f, t = _line_judge(cells, center, BLACK)
        n_four += f
        n_three += t

    if n_four >= 2:
        return FOUL_DOUBLE_FOUR
    if n_four == 0 and n_three >= 2:
        # n_four == 1 时是**四三**—— 黑方的正常胜形，不是禁手。
        return FOUL_DOUBLE_THREE
    return FOUL_NONE


def is_foul(board, r, c, player=BLACK):
    """``(r, c)`` 落子是否构成禁手。"""
    return foul_kind(board, r, c, player) != FOUL_NONE


def forbidden_points(board, player=BLACK):
    """当前局面下 ``player`` 落子即构成禁手的空点，按行优先序返回 ``[(r, c), ...]``。

    GUI 的禁手标点与 ``engine.ai_move`` 的兜底过滤都用它。它是 O(361) 次完整
    判定（每次判定的线是 O(19)），**只在每手之后调一次**，不进搜索。
    """
    n = len(board)
    out = []
    for r in range(n):
        row = board[r]
        for c in range(n):
            if int(row[c]) != EMPTY:
                continue
            if foul_kind(board, r, c, player) != FOUL_NONE:
                out.append((r, c))
    return out


def judgement(board, r, c, player):
    """落子之后这一手的**结果**：``"win"`` / ``"foul"`` / ``"continue"``。

    给对局状态机用（``session`` / ``room``）。无禁手模式下走
    ``five_at`` 就够了，本函数只是把"禁手模式下黑方的超长连不是赢而是输"
    这一条收口在一处。
    """
    if foul_kind(board, r, c, player) != FOUL_NONE:
        return "foul"
    if five_at(board, r, c, player):
        return "win"
    return "continue"

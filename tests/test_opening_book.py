# -*- coding: utf-8 -*-
"""开局库的接线：查表、命中就不搜索、表外回落。

**这一组用例守的是"接线"，不是"表的内容"。** 表由 `tools/build_book.py` 生成，
内容会随引擎变强而变，所以这里一律**从表本身取事实**（哪一手在库里、分值多少），
再断言 `ai_move` 认它 —— 而不是把某个具体着法写死在测试里。写死的话，每次
重新建库都要改测试，改到最后就没人看了。

反过来说，下面这些断言是**生成物的结构契约**，建库工具必须一直满足：

* 盘上 1 子（黑天元）时白方那一手必须在库里；
* 盘上 2 子（天元 + 8 个标准第二手之一）时黑方那一手必须在库里 —— **8 手全在**；
* 键按走子方分区（同一盘面换个走子方必须查不到）；
* 查表命中时不搜索（`nodes == 0`）、`reason == '开局库'`、分值原样上报。
"""

from __future__ import annotations

import numpy as np
import pytest

import engine_local as EL
from opening_book import BOOK, BOOK_STONES
from tools.selfplay import CENTER, OPENING_SECOND_MOVES

BOARD_SIZE = 19
BLACK, WHITE = 1, 2


def _board(*stones):
    m = np.zeros((BOARD_SIZE, BOARD_SIZE), dtype=np.uint8)
    for r, c, p in stones:
        m[r][c] = p
    return m


def _require_book():
    if not BOOK:
        pytest.skip("开局库尚未生成（tools/build_book.py 还没跑过）—— "
                    "空表是合法状态，但这一组用例此时无从下手")


def test_book_stones_matches_the_gate(new_engine):
    """`BOOK_STONES` 与 `book_lookup` 的闸门必须是同一个数。

    闸门写死成别的数（比如 4）时，那张表永远只在 3 子以内有效 —— 症状是
    "库里明明有更深的局面却查不到"，而 `book_lookup` 自己不会报错。
    """
    assert BOOK_STONES == 3, (
        "生成物的 BOOK_STONES 变了。`book_lookup` 的闸门读的就是它，"
        "而建库工具按 '盘上 3 子以内' 建表 —— 两个数必须一起改")
    _require_book()
    # 闸门两侧各取一个盘面：正好 3 子必查，4 子必不查。
    three = _board((9, 9, 1), (8, 8, 2), (9, 8, 1))
    four = _board((9, 9, 1), (8, 8, 2), (9, 8, 1), (9, 10, 2))
    assert EL.Board.from_array(three).stone_count == 3
    assert EL.Board.from_array(four).stone_count == 4
    assert EL.book_lookup(four, 1) is None, "4 子不该查表（也不该命中）"


def test_first_ply_and_all_standard_second_moves_are_in_the_book(new_engine):
    """结构契约：白应天元在库；8 个标准第二手之后黑方那一手也全在库。"""
    _require_book()
    center_only = _board((9, 9, 1))
    assert EL.book_lookup(center_only, WHITE) is not None, \
        "库里没有'白应天元'这一手 —— 开盘第一手就查不到，等于库没生效"
    missing = []
    for sec in OPENING_SECOND_MOVES:
        b = _board((9, 9, 1), (sec[0], sec[1], 2))
        if EL.book_lookup(b, BLACK) is None:
            missing.append(sec)
    assert not missing, ("这些标准第二手之后没有黑方的库项：%r —— 建库工具的"
                         "第 3 手那一层漏了？" % (missing,))


def test_book_is_partitioned_by_side_to_move(new_engine):
    """同一个盘面换个走子方必须查不到（键里带 `_TT_SALT_W`）。

    漏掉盐值会让两方共用一条表项：看起来"库命中了"，实际把白方的着法与分值
    当成黑方的报了 —— 黑白分值符号相反，症状是分值离谱而非崩溃。
    """
    _require_book()
    center_only = _board((9, 9, 1))
    assert EL.book_lookup(center_only, WHITE) is not None
    # 盘上只有一颗黑子、却轮到黑方 —— 这不是合法对局状态，库里不该有它。
    assert EL.book_lookup(center_only, BLACK) is None, \
        "同一盘面在两个走子方下都查到了 —— 键没带 '局面 + 走子方'"


def test_ai_move_takes_the_book_move_without_searching(new_engine):
    """命中即返回：不搜索、分值原样上报、reason 是 '开局库'。"""
    _require_book()
    board = _board((9, 9, 1))
    ent = EL.book_lookup(board, WHITE)
    new_engine.new_game()
    r, c, info = new_engine.ai_move(board, WHITE, 1)

    assert info["reason"] == "开局库", \
        "这一手在库里，却走了搜索（reason=%r）" % (info["reason"],)
    assert (r, c) == divmod(ent[0], BOARD_SIZE), \
        "命中库却走了别的着法：库说 %r，实际 %r" % (divmod(ent[0], BOARD_SIZE), (r, c))
    assert board[r][c] == 0, "库里的着法落在已有棋子上"
    assert info["nodes"] == 0, \
        "查表不该产生节点（nodes=%r）—— 有节点说明它其实搜了" % (info["nodes"],)
    assert info["best_val"] == ent[1], \
        ("库里的分值必须**原样**上报（库 %r，实际 %r）。填 0 或重算都不行："
         "那是这个局面的真实评估，只是不是本次算的" % (ent[1], info["best_val"]))
    assert info["depth"] == ent[2] and info["actual_depth"] == ent[2]


def test_nonstandard_first_move_still_searches(new_engine):
    """对手第一手不在标准开局集里 → 不命中、回落搜索。

    **这条盯着一个具体的降智陷阱**：`opening_move` 里有一条"紧贴对手第一子、
    按固定偏移取第一个空点"的规则，它从不搜索。若把开局库的闸门写成
    "盘上子少"（而不是"查表命中"），这条规则就会被激活 —— 对手第一手下在
    角上时，白方会从固定偏移里挑一手。它今天不是现有行为（旧版那颗棋子
    因为键格式不匹配从未走到那里），所以这里把它钉住。
    """
    _require_book()
    board = _board((0, 0, 1))          # 角上，非标准第一手
    assert EL.book_lookup(board, WHITE) is None, "角上第一手不该在库里"

    new_engine.new_game()
    r, c, info = new_engine.ai_move(board, WHITE, 1)
    assert info["reason"] != "开局库", \
        "非标准开局走了开局库 —— 闸门是不是写成了'盘上子少'？"
    assert info["nodes"] > 0, "回落之后必须真的搜索（nodes=%r）" % (info["nodes"],)
    assert board[r][c] == 0 and abs(r - 0) + abs(c - 0) > 1, \
        "回落搜索不该给出紧贴角落里那颗子的固定偏移点 (r=%r c=%r)" % (r, c)


def test_midgame_is_never_looked_up(new_engine):
    """中盘局面一律不查表（闸门 + 哈希都不可能命中）。"""
    _require_book()
    board = np.zeros((BOARD_SIZE, BOARD_SIZE), dtype=np.uint8)
    for r, c, p in ((9, 9, 1), (8, 8, 2), (9, 8, 1), (10, 8, 2), (9, 10, 1),
                    (11, 7, 2), (8, 10, 1)):
        board[r][c] = p
    assert EL.book_lookup(board, 2) is None
    assert EL.book_lookup(board, 1) is None

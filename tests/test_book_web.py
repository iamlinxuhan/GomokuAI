# -*- coding: utf-8 -*-
"""``webapp/book.js`` 与 ``opening_book.py`` 的同步。

网页版的入门档靠这张表与桌面版走同一手（见 ``webapp/app.js`` 的 ``bookLookup``）。
表是 ``tools/build_book_web.py`` 从 ``opening_book.BOOK`` 转出来的**生成物**，
改了库却忘了重建，网页版就会**静默**退化成"只会搜索"—— 入门档从 (9,10) 变成
(7,7)，没有任何报错。这一组用例就是那道防线。

**守的是"同一份数据"，不是某个具体着法**：断言由生成器重算一遍再与磁盘比对，
所以重新建库之后跑一次生成器即可，不用回来改测试。
"""

from __future__ import annotations

import os

import numpy as np
import pytest

import engine_local as EL
from opening_book import BOOK, BOOK_STONES
from tools import build_book_web

BOARD_SIZE = 19
BLACK, WHITE = 1, 2


def _skip_if_absent():
    if not os.path.exists(build_book_web.OUT):
        pytest.skip("webapp/book.js 不在（网页版目录可能没随仓库分发）")
    if not BOOK:
        pytest.skip("开局库尚未生成（tools/build_book.py 还没跑过）")


def test_book_js_is_exactly_what_the_generator_renders():
    """磁盘上那份必须逐字节等于生成器的输出。

    比"内容差不多"严格得多，是**故意的**：这条挂了只有一种修法 —— 重跑
    ``uv run --frozen python tools/build_book_web.py``。要是允许手工微调，
    这张表就会慢慢长成第二份真值，而它的键是哈希反查出来的，没人能肉眼核对。
    """
    _skip_if_absent()
    found = build_book_web.collect()
    with open(build_book_web.OUT, encoding="utf-8") as fh:
        on_disk = fh.read()
    assert on_disk == build_book_web.render(found), (
        "webapp/book.js 与 opening_book.py 不同步 —— 重跑 "
        "`uv run --frozen python tools/build_book_web.py`")


def test_every_book_entry_reaches_the_web_table():
    """``BOOK`` 的每一条都要翻译过去，一条不漏。

    枚举范围只有中心 ±5，库要是往更远处长（对手第一手下在边角），这一条会先
    变红，而不是等到用户发现网页版开局怪怪的。
    """
    _skip_if_absent()
    found = build_book_web.collect()
    assert len(found) == len(BOOK), (
        "库里有 %d 条，网页表只翻译出 %d 条 —— 枚举范围（SPAN=%d）或键格式对不上"
        % (len(BOOK), len(found), build_book_web.SPAN))


def test_tengen_entry_agrees_with_book_lookup():
    """天元之后白方那一手：明文表与 ``engine_local.book_lookup`` 必须同源。

    单拿一手做端到端对账，覆盖"哈希 → 明文键"这一步转换本身。上面那两条只能
    证明两份数据来自同一次计算，证明不了键拼得对。
    """
    _skip_if_absent()
    found = build_book_web.collect()
    board = np.zeros((BOARD_SIZE, BOARD_SIZE), dtype=np.uint8)
    board[9][9] = BLACK
    ent = EL.book_lookup(board, WHITE)          # 参数是 19×19 的数组，不是 Board
    assert ent is not None, "天元之后白方那一手不在库里"
    key = "%d:1|%d" % (9 * BOARD_SIZE + 9, WHITE)
    assert found.get(key) == list(ent), (
        "明文键 %r 查到 %r，book_lookup 给的是 %r" % (key, found.get(key), list(ent)))
    assert len(found) and len(next(iter(found.values()))) == 3
    assert all(v[2] >= 1 for v in found.values()), "库项里的层数不该为 0"
    assert BOOK_STONES == 3

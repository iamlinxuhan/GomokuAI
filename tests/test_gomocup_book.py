# -*- coding: utf-8 -*-
"""Gomocup 2026 官方开局集：数据、展开、跨语言对拍、根节点接入。

三条护栏：

* **数据只有一处来源**：``data/gomocup_2026.json``；两份生成物与它逐条一致。
* **C++ 与 Python 的 D4 展开逐项相同**：``--dump-book`` 全量对拍。翻译、
  变换、冲突决胜任何一处写岔都会在这里现形。
* **接入点只在根节点、只在 compute、只在中级以上**：低档报文与搜索路径
  逐字节不变；``analyze`` 永不查书。
"""

from __future__ import annotations

import json
import os
import subprocess

import numpy as np
import pytest

import config
import engine
import gomocup_book as G
import gomocup_book_data
from gomocup_book_data import GOMOCUP_RAW

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIZES = list(config.BOARD_SIZES)


def _board(size, coords, side):
    """按 ``coords`` 摆一个局面（黑先交替），轮到 ``side``。"""
    b = np.zeros((size, size), dtype=np.uint8)
    for i, (r, c) in enumerate(coords):
        b[r][c] = 1 if i % 2 == 0 else 2
    return b


def _dump_py(n):
    return "\n".join("%016X %d" % (k, v)
                     for k, v in sorted(G.table(n).items()))


@pytest.fixture(autouse=True)
def _no_leaked_module_clients():
    """本文件会走 ``engine.analyze``，它按**盘面尺寸**取模块级连接。

    9/13/15 的连接会留在 ``engine._CLIENTS`` 里、连着的端口也留着，后面的
    用例（比如 ``test_remote_client`` 的面板端口）就会读到一个上一条用例
    留下的连接。这里在用例结束后把本文件新造出来的连接收干净，并把
    ``_LAST_SIZE`` 还原 —— 隔离这件事由本文件负责，不指望别的用例去猜。
    """
    last = dict(engine._LAST_SIZE)
    try:
        yield
    finally:
        for key, cl in list(engine._CLIENTS.items()):
            cl.shutdown()
            engine._CLIENTS.pop(key, None)
        engine._LAST_SIZE.clear()
        engine._LAST_SIZE.update(last)


# ------------------------------------------------------------------ 数据

def test_generated_data_matches_the_json():
    """``gomocup_book_data.py`` 是 JSON 的逐条镜像（漏生成会被这条逮住）。"""
    with open(os.path.join(ROOT, "data", "gomocup_2026.json"),
              encoding="utf-8") as fh:
        doc = json.load(fh)
    want = [(e["name"], [tuple(p) for p in e["moves"]],
             tuple(e["response"])) for e in doc["entries"]]
    got = [(name, [tuple(p) for p in moves], tuple(resp))
           for name, moves, resp in GOMOCUP_RAW]
    assert got == want


#: 官方页 <https://live.gomocup.org/openings> 上四条开局的原样坐标（相对盘心）。
#: 2026-11-13 与官方 PNG 逐子对过：freestyle15_1、freestyle15_5、caro_4（standard#3）、
#: freestyle15_7（standard#1）。**顺序也钉住** —— 前缀展开依赖黑先交替的次序。
OFFICIAL_SAMPLES = {
    "freestyle15#1": [(4, 6), (1, 5), (0, 3)],
    "freestyle15#5": [(0, 0), (0, -1), (0, -3), (1, -3), (2, -1)],
    "standard#3": [(-5, 5), (-5, 4), (-5, 3), (-4, 2), (-2, 2)],
    "standard#1": [(6, 6), (4, 4), (6, 2), (2, 4), (3, 2), (5, 4), (3, 4)],
}


def test_official_opening_coordinates_are_transcribed_verbatim():
    """数据必须是官方页那几条线本身（盘心平移后），不是第三方转录。"""
    got = {name: moves for name, moves, _resp in GOMOCUP_RAW}
    for name, official in OFFICIAL_SAMPLES.items():
        want = [(x + 7, y + 7) for x, y in official]
        assert got.get(name) == want, (
            "%s 与 Gomocup 官方公布的坐标不一致：%r != %r"
            % (name, got.get(name), want))


def test_every_entry_replays_without_overlap():
    """每条线的着法互不重叠，应手格是空的，坐标都在 15×15 内。"""
    for name, moves, resp in GOMOCUP_RAW:
        assert len(set(moves)) == len(moves), name
        assert resp not in moves, name
        for x, y in list(moves) + [resp]:
            assert 0 <= x < 15 and 0 <= y < 15, name


# ------------------------------------------------------------------ 展开

@pytest.mark.parametrize("n", SIZES)
def test_every_prefix_of_every_kept_line_hits(n):
    """保留的每条线、每个前缀、每个 D4 变换都命中，且冲突按最小索引决胜。

    同一前缀在多条官方线上出现时（比如两条线首手相同、第二手不同），一个
    键对应两个应手是**数据本身的分歧**，不是 bug；规则是取最小线性格索引，
    这条用例按同一规则独立算一遍再来比。
    """
    # 独立算一遍 key → {应手}，不读 build() 的实现。
    want = {}
    for _name, moves, resp in GOMOCUP_RAW:
        coords = [G._translate(x, y, n) for x, y in moves]
        resp_rc = G._translate(resp[0], resp[1], n)
        if resp_rc is None or any(v is None for v in coords):
            continue
        for k in range(1, len(coords) + 1):
            mv = coords[k] if k < len(coords) else resp_rc
            side = 1 if k % 2 == 0 else 2
            for t in range(8):
                stones = [(G._xform(t, n, r, c)[0], G._xform(t, n, r, c)[1],
                           1 if i % 2 == 0 else 2)
                          for i, (r, c) in enumerate(coords[:k])]
                tr, tc = G._xform(t, n, mv[0], mv[1])
                key = G._hash(stones, n) ^ (0 if side == 1 else G.TT_SALT_W)
                want.setdefault(key, set()).add(tr * n + tc)

    assert want
    for key, moves in want.items():
        assert G.table(n).get(key) == min(moves)


def test_the_book_is_empty_nowhere_we_claim_support():
    """9/13/15/19 都保住了若干条线（9 只剩贴着盘心的那几条）。"""
    for n in SIZES:
        st = G.stats(n)
        assert st["lines"] > 0 and st["entries"] > 0, (n, st)


# ------------------------------------------------------------------ 跨语言

@pytest.mark.parametrize("n", SIZES)
def test_cpp_and_python_expand_identically(n):
    binary = config.find_binary("free", n)
    if binary is None:
        pytest.skip("没有构建 %d 路的 C++ 引擎" % n)
    out = subprocess.run([binary, "--dump-book"], capture_output=True,
                         text=True, check=True)
    assert out.stdout.splitlines() == _dump_py(n).splitlines(), \
        "C++ 与 Python 的开局书逐项不同"


# ------------------------------------------------------------------ 接入

def _a_kept_prefix(n):
    """取一条在本尺寸上放得下的线的第二步局面（不走旧的小型开局库）。"""
    for name, moves, resp in GOMOCUP_RAW:
        coords = [G._translate(x, y, n) for x, y in moves]
        if any(v is None for v in coords):
            continue
        return name, coords, resp
    raise AssertionError("这个尺寸一条线都没保住")


@pytest.mark.parametrize("n", SIZES)
def test_intermediate_uses_the_book_and_reports_zero_nodes(n):
    """中级（3）命中时：同着、score=0、nodes=0 —— C++ 与 Python 镜像一致。"""
    if config.find_binary("free", n) is None:
        pytest.skip("没有构建 %d 路的 C++ 引擎" % n)
    _, coords, _ = _a_kept_prefix(n)
    side = 1 if 2 % 2 == 0 else 2          # 走了两手之后轮到黑
    board = _board(n, coords[:2], side)
    want = G.lookup(board, side, n)
    assert want is not None

    client = engine._ServerClient(variant="free", size=n)
    try:
        idx, info = client.compute(board, side,
                                   dict(engine.DIFFICULTY[3], book=1), None)
    finally:
        client.shutdown()
    assert idx == want
    assert info["reason"] == "开局库"
    assert info["best_val"] == 0 and info["nodes"] == 0


def test_beginner_never_gets_the_book():
    """初级（2）不下发 book：同一局面走的是搜索，节点数不为 0。"""
    n = 15
    if config.find_binary("free", n) is None:
        pytest.skip("没有构建 15 路的 C++ 引擎")
    _, coords, _ = _a_kept_prefix(n)
    board = _board(n, coords[:2], 1)
    client = engine._ServerClient(variant="free", size=n)
    try:
        idx, info = client.compute(board, 1, engine._cfg_for(2), None)
    finally:
        client.shutdown()
    assert info["reason"] != "开局库"
    assert info["nodes"] > 0
    assert config.find_binary("free", n) is not None


def test_analyze_never_consults_the_book():
    """复盘要逐候选分值：书给不出，所以 analyze 必须照常搜索。"""
    n = 15
    if config.find_binary("free", n) is None:
        pytest.skip("没有构建 15 路的 C++ 引擎")
    _, coords, _ = _a_kept_prefix(n)
    board = _board(n, coords[:2], 1)
    best_idx, best_val, cands, info = engine.analyze(board, 1, 3)
    assert cands, "复盘在开局书局面上没有给出候选表"
    assert info.get("reason") != "开局库"
    assert info.get("nodes", 0) > 0

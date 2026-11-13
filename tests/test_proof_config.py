# -*- coding: utf-8 -*-
"""DFPN 证明搜索的开关面：出厂全关、只在显式配置时下发、复盘不受影响。

**这一层测的是"开关"，不是"证明器本身"。** 证明器的正确性由 C++ 自检钉住
（``main.cpp`` 的 ``testDfpn`` 与 ``testMateTruth`` 里的并行接管用例），这里
守的是那条容易在后来的改动里悄悄失守的边界：

1. **出厂全部为 0。** 老报文必须逐字节不变 —— 低档与 ``engine_local`` 的
   逐字一致护栏全靠这一点（``search.h`` 里 ``proofMode`` 的注释）。
2. **初/中两档永远不下发。** Python 侧没有 DFPN；一旦第 2/3 档开了，那条
   跨语言护栏当场失效，而且没有任何测试会告诉你有东西变了 —— 所以在这里
   直接钉死。
3. **0 时一个键都不发。** 不是发 ``proof_mode: 0``，而是根本不出现这个键。
4. **复盘（``analyze``）不下发。** 复盘要的是逐候选分值；C++ 侧就算收到
   也会因为 ``outRoot != nullptr`` 跳过证明线程 —— 不发是让"复盘不可能被
   证明搜索改写"成为报文层面的事实。
"""

from __future__ import annotations

import numpy as np
import pytest

import engine

BOARD_SIZE = 19
BLACK, WHITE = 1, 2

_MIDGAME = [(9, 9, BLACK), (9, 10, WHITE), (8, 9, BLACK), (10, 10, WHITE),
            (9, 8, BLACK), (8, 10, WHITE)]


def _board(stones):
    m = np.zeros((BOARD_SIZE, BOARD_SIZE), dtype=np.uint8)
    for r, c, p in stones:
        m[r][c] = p
    return m


@pytest.fixture
def client():
    """不建连的客户端 —— ``_search_request`` 只拼报文，不碰 socket。"""
    c = engine._ServerClient(variant="free")
    yield c
    c.shutdown()


def _cfg_with_proof(level, **over):
    cfg = dict(engine._cfg_for(level))
    cfg.update(over)
    return cfg


# ------------------------------------------------------------------ 出厂值

def test_every_preset_ships_with_proof_off():
    """六档预设与自定义档的出厂值都是 0。

    这是"默认行为与没有这套东西逐位相同"的机械保障。把某一档改成 1 之后，
    ``tools/ab_proof.py`` 的结论才允许它不是 0 —— 那是唯一该出现非零值的
    地方，而且要先有一份能拿出手的 A/B 报告。
    """
    for level, cfg in engine.DIFFICULTY.items():
        assert cfg.get("proof_mode", 0) == 0, f"第 {level} 档出厂应为关"
    assert engine._CUSTOM_DEFAULT.get("proof_mode", 0) == 0


def test_low_levels_must_never_enable_proof():
    """初/中两档的 ``proof_mode`` 必须是 0 —— 这两档是跨语言护栏的范围。

    ``engine_local`` 没有 DFPN。一旦这两档下发 ``proof_mode``，C++ 与 Python
    就不再是同一个搜索，``tools/positions.py --engine cpp/local`` 那条唯一
    廉价的移植正确性检验会陷入"某几道题突然不同"的噪声里。
    """
    for level in (2, 3):
        assert engine._cfg_for(level).get("proof_mode", 0) == 0


# ------------------------------------------------------------------ 报文

def test_off_sends_no_key_at_all(client):
    """``proof_mode`` 为 0 时，报文里根本没有这个键（不是发 0）。"""
    board = _board(_MIDGAME)
    req = client._search_request("compute", board, WHITE, engine._cfg_for(5))
    for key in ("proof_mode", "proof_time", "proof_nodes"):
        assert key not in req


def test_on_sends_the_three_keys(client):
    board = _board(_MIDGAME)
    cfg = _cfg_with_proof(5, proof_mode=2, proof_time=0.75, proof_nodes=50000)
    req = client._search_request("compute", board, WHITE, cfg)
    assert req["proof_mode"] == 2
    assert req["proof_time"] == pytest.approx(0.75)
    assert req["proof_nodes"] == 50000


def test_analyze_never_sends_proof(client):
    """复盘不下发 —— 复盘路径的结论不能被证明搜索改写。"""
    board = _board(_MIDGAME)
    cfg = _cfg_with_proof(5, proof_mode=2)
    req = client._search_request("analyze", board, WHITE, cfg)
    assert "proof_mode" not in req


def test_negative_values_are_clamped(client):
    """负的时限/节点数被钳到 0，不会被原样送到 C++。"""
    board = _board(_MIDGAME)
    cfg = _cfg_with_proof(5, proof_mode=1, proof_time=-1.0, proof_nodes=-5)
    req = client._search_request("compute", board, WHITE, cfg)
    assert req["proof_time"] == 0.0
    assert req["proof_nodes"] == 0


# ------------------------------------------------------------------ 自定义

def test_custom_config_round_trips_proof_mode():
    """自定义档能存住 ``proof_mode`` —— 它是"往上调"的入口，得能调到证明搜索。"""
    old = engine.custom_config()
    try:
        engine.set_custom_config(dict(old, proof_mode=2))
        assert engine.custom_config()["proof_mode"] == 2
    finally:
        engine.set_custom_config(old)

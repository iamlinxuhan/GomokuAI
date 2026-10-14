# -*- coding: utf-8 -*-
"""第 6 档「Gomocup」：档位形状、绝对秒数的余量、复盘回退的时间钳制。

这一档与前五档有两处**结构性**差别，各自会以不同方式悄悄失效，所以分开测：

1. **它是唯一带 ``reserve_sec`` 的档。** 余量从比例改成绝对秒数。写错的后果
   是"300 秒里有 45 秒没被用上"，界面看不出任何异常；而**下发多了一个键**
   又会碰到另一条更贵的护栏 —— 前五档的报文必须逐字节不变（低档与
   ``engine_local`` 的逐字一致靠它）。所以这里两头都钉：第 6 档有，其余没有。
2. **它的时长（300 秒）超出本地 Python 引擎能承担的范围。** C++ 不在时复盘
   会把 ``cfg["time"]`` 原样转发给本地引擎，一局几十手就是几小时。钳制写在
   ``engine.analyze`` 的降级分支里，这里用一个假的 `_client` 把那条路走通。

数值本身（深度 40、VCF 30 秒、缓冲 20 秒）不在这里断言 —— 它们是可调的，
改它们不该让测试变红；要守的是"这一档有这些机制"和"别的档没有"。
"""

from __future__ import annotations

import numpy as np
import pytest

import config
import engine
import engine_local

BOARD_SIZE = 19
BLACK, WHITE = 1, 2


def _board(stones):
    m = np.zeros((BOARD_SIZE, BOARD_SIZE), dtype=np.uint8)
    for r, c, p in stones:
        m[r][c] = p
    return m


#: 一个中局局面：黑有活三、白有一子挡着，两边都还有得算。
_MIDGAME = [(9, 9, BLACK), (9, 10, WHITE), (8, 9, BLACK), (10, 10, WHITE),
            (9, 8, BLACK), (8, 10, WHITE)]


@pytest.fixture
def client():
    """不建连的客户端 —— ``_search_request`` 只拼报文，不碰 socket。"""
    c = engine._ServerClient(variant="free")
    yield c
    c.shutdown()


# ------------------------------------------------------------------ 档位形状

def test_gomocup_is_the_sixth_preset():
    assert sorted(engine.DIFFICULTY) == [1, 2, 3, 4, 5, 6]
    assert engine.DIFFICULTY[engine.GOMOCUP_LEVEL]["name"] == "Gomocup"
    assert engine.GOMOCUP_LEVEL == 6
    assert engine.difficulty_name(6) == "Gomocup"


def test_custom_level_moved_out_of_the_way():
    """哨兵档跟着挪到 7，且仍不在 ``DIFFICULTY`` 里。

    挪号本身是安全的（没有任何东西把档位号落盘），但要挡住"顺手让自定义也用
    第 6 档"这种改法 —— 那会让难度页出现两张同号的卡。
    """
    assert engine.CUSTOM_LEVEL == 7
    assert engine.CUSTOM_LEVEL not in engine.DIFFICULTY
    assert engine.CUSTOM_LEVEL not in engine.DIFFICULTY_NAMES
    assert engine.difficulty_name(engine.CUSTOM_LEVEL) == "自定义"


def test_cfg_for_reads_the_live_table():
    """``_cfg_for`` 返回的是表里那一份本身，运行期改表即改行为。

    压测脚本正是这么用的（临时把这一档的 time / reserve_sec 改小）。要是哪天
    改成返回副本，压测就变成空跑。
    """
    assert engine._cfg_for(engine.GOMOCUP_LEVEL) is \
        engine.DIFFICULTY[engine.GOMOCUP_LEVEL]


# -------------------------------------------------------------- reserve_sec

def test_reserve_sec_is_sent_by_the_gomocup_level_only(client):
    """只有第 6 档的报文里有 ``reserve_sec``。

    **其余档一个键都不发**，报文因此逐字节与从前相同 —— 那是低档与
    ``engine_local`` 逐字同着那条护栏的一部分（见 ``search.h`` 里
    ``reserveSec`` 的注释）。
    """
    board = _board(_MIDGAME)
    req = client._search_request("compute", board, WHITE,
                                 engine._cfg_for(engine.GOMOCUP_LEVEL))
    assert req["reserve_sec"] == pytest.approx(
        float(engine.DIFFICULTY[engine.GOMOCUP_LEVEL]["reserve_sec"]))
    for level in sorted(engine.DIFFICULTY):
        if level == engine.GOMOCUP_LEVEL:
            continue
        assert "reserve_sec" not in client._search_request(
            "compute", board, WHITE, engine._cfg_for(level)), \
            f"第 {level} 档不该带 reserve_sec"


def test_reserve_sec_tracks_a_runtime_edit(client, monkeypatch):
    """改表里的 ``reserve_sec``，下一次请求就跟着变。"""
    board = _board(_MIDGAME)
    monkeypatch.setitem(engine.DIFFICULTY[engine.GOMOCUP_LEVEL],
                        "reserve_sec", 2.5)
    req = client._search_request("compute", board, WHITE,
                                 engine._cfg_for(engine.GOMOCUP_LEVEL))
    assert req["reserve_sec"] == pytest.approx(2.5)


def test_analyze_wire_format_matches_compute(client):
    """复盘那条路的报文与对局**逐字相同**（只有 ``type`` 一字之差）。

    复盘强度必须与对局用同一套配置，"用某一档复盘"这句话才有确定的含义。新加
    的键要两边都到 —— 只补一处会让复盘按另一套时间管理跑。
    """
    board = _board(_MIDGAME)
    cfg = engine._cfg_for(engine.GOMOCUP_LEVEL)
    a = client._search_request("compute", board, WHITE, cfg)
    b = client._search_request("analyze", board, WHITE, cfg)
    assert a.pop("type") == "compute"
    assert b.pop("type") == "analyze"
    assert a == b


# --------------------------------------------------- 复盘降级的时间钳制

class _FakeLocal:
    """顶替 ``engine_local`` / ``engine_ban`` 的桩，只记下收到的 ``time_limit``。"""

    #: 与真引擎同形的档位表 —— 钳制上限就是从这张表里读的。
    DIFFICULTY = dict(engine_local.DIFFICULTY)

    def __init__(self):
        self.calls = []

    def analyze(self, board, me, level, cancel=None, time_limit=None):
        self.calls.append(time_limit)
        return -1, 0, [], {}


def test_local_fallback_clamps_the_gomocup_time(monkeypatch):
    """C++ 不可用时，复盘传给本地引擎的 ``time_limit`` 不超过本地最高档。

    原样转发 300 秒的后果：一局几十手、每手 255 秒的纯 Python 搜索。钳制是
    ``min(cfg["time"], 本地最高档)`` —— 低档本来就低于上限，对它们恒等。
    """
    fake = _FakeLocal()
    monkeypatch.setattr(engine, "_local_engine", lambda variant="free": fake)
    monkeypatch.setattr(engine, "_client",
                        lambda variant="free": _raise(_Boom()))

    engine.analyze(_board(_MIDGAME), WHITE, engine.GOMOCUP_LEVEL)
    ceiling = max(engine_local.DIFFICULTY[k]["time"]
                  for k in engine_local.DIFFICULTY)
    assert fake.calls == [ceiling], fake.calls


def test_local_fallback_never_exceeds_the_local_ceiling(monkeypatch):
    """**六档一起过一遍**：每一档给到本地的都不超过本地引擎的最高档。

    ``engine.DIFFICULTY`` 与 ``engine_local.DIFFICULTY`` 是两张不同的表
    （0.5/3/7/15/20/300 对 3/7/15），钳制是 ``min`` 而不是赋值，所以低档必须
    **原样**传下去 —— 写成赋值会让 1–3 档在回退机上一律变成 15 秒，那是把
    "复盘一局入门棋"从 0.5 秒拖成 15 秒。
    """
    fake = _FakeLocal()
    monkeypatch.setattr(engine, "_local_engine", lambda variant="free": fake)
    monkeypatch.setattr(engine, "_client",
                        lambda variant="free": _raise(_Boom()))
    for level in sorted(engine.DIFFICULTY):
        engine.analyze(_board(_MIDGAME), WHITE, level)
    ceiling = max(engine_local.DIFFICULTY[k]["time"]
                  for k in engine_local.DIFFICULTY)
    assert fake.calls == [min(float(engine.DIFFICULTY[l]["time"]), ceiling)
                          for l in sorted(engine.DIFFICULTY)]


class _Boom(Exception):
    pass


def _raise(exc):
    raise exc


# ------------------------------------------------- C++ 侧的绝对秒数（真实计时）

@pytest.mark.perf
def test_reserve_sec_is_absolute_in_the_cpp_engine():
    """``reserve_sec`` 真的按**秒**扣，不是按比例。

    同一个局面、同一个 ``time_limit``，只改余量的给法：

    * 不给 ``reserve_sec`` → 预算 = ``time_limit × (1 - 0.15)``；
    * 给 ``reserve_sec = 3.0`` → 预算 = ``time_limit - 3.0``。

    两者差出 2.4 秒，远超容差，所以这条测试认的是"哪一套算法在跑"。它同时
    挡住删掉 ``searchBudget()`` 让两处消费点各自为政的改法 —— 那种情况下软停
    会按比例算出还剩 2.4 秒余量，于是开一轮注定跑不完的迭代。

    盘面必须**深到搜不完**：``max_depth`` 给 40，位置选宽局面的中局，否则
    引擎会提前撞上深度上限或找到杀棋，两次都提前返回，差值就没了。
    """
    if config.find_binary("free") is None:
        pytest.skip("没有构建 cpp/build/gomoku_engine")

    import time as _t

    import tools.bench as B

    pos = next(p for p in B.bench_positions() if p["id"] == "spread_wide")
    board, me = pos["board"], pos["to_move"]
    c = engine._ServerClient(variant="free")
    try:
        cfg = dict(time=4.0, max_depth=40, vcf_budget=0.0, qply=8,
                   enhance=1)
        t0 = _t.monotonic()
        c.compute(board, me, cfg, None)
        proportional = _t.monotonic() - t0

        t0 = _t.monotonic()
        c.compute(board, me, dict(cfg, reserve_sec=3.0), None)
        absolute = _t.monotonic() - t0
    finally:
        c.shutdown()

    assert proportional > 2.6, f"按比例的余量没生效：{proportional:.2f}s"
    assert absolute < 2.0, f"绝对余量没生效：{absolute:.2f}s"

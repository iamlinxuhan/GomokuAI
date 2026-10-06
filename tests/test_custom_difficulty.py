# -*- coding: utf-8 -*-
"""自定义难度：哨兵档位 + 可注入配置（引擎侧，零 Qt）。

「自定义」**不是** ``DIFFICULTY`` 里的第 6 档，而是一个哨兵档位号
（``engine.CUSTOM_LEVEL``）加一份可以整只替换的配置。这么定有四条要同时成立的
不变量，逐条钉在这里：

1. ``DIFFICULTY`` 仍然是**五档** —— 它是 `sorted()` / `max()` 的读者们（难度卡
   循环、`tools/bench.py`、`tools/ui_e2e.py`、非法档位回退）共同的假设，多一档
   会连带惊动它们。
2. ``_cfg_for(CUSTOM_LEVEL)`` 给的就是那份注入的配置，且**每次都现读**——
   `DIFFICULTY` 本来就可以在运行期改写（``tests/test_vcf.py`` 就是这么用的），
   自定义配置没有理由更死。
3. ``custom_config()`` 返回**副本**。返回内部那只 dict 的话，调用方随手改一下就
   等于绕过 ``set_custom_config`` 的归一（name 固定、bias 置空）改了引擎状态。
4. 对局（``ai_move``）与复盘（``analyze``）拿到的是**同一份**配置 —— 否则
   "用自定义难度复盘"这句话就没有确定的含义。

不需要 QApplication：`engine.py` 是零 Qt 的（`server.py`、`tools/bench.py` 这些
无头程序直接导入它）。
"""

from __future__ import annotations

import numpy as np
import pytest

import engine


@pytest.fixture(autouse=True)
def _restore():
    """每个用例前后都回出厂值 —— 配置是模块级的，污染会跨用例。"""
    engine.reset_custom_config()
    yield
    engine.reset_custom_config()


CUSTOM_KEYS = ("time", "max_depth", "vcf_budget", "qply", "enhance")


# --------------------------------------------------------------- 哨兵档位本身

def test_custom_level_is_not_in_difficulty_table():
    """``DIFFICULTY`` 保持五档，且不含哨兵号。"""
    assert engine.CUSTOM_LEVEL not in engine.DIFFICULTY
    assert sorted(engine.DIFFICULTY) == [1, 2, 3, 4, 5]
    assert max(engine.DIFFICULTY) == 5


def test_difficulty_name_for_custom_level():
    """档位名必须是「自定义」。

    少了这一支，`difficulty_name` 会 fallback 到最强档，于是日志、对局面板的
    "难度"行、复盘页副标题一起写错。
    """
    assert engine.difficulty_name(engine.CUSTOM_LEVEL) == "自定义"


def test_default_matches_the_strongest_preset():
    """出厂值取宗师（第 5 档）那一组。

    自定义是"往上调"的入口，默认比预设最强档还弱说不过去；关掉 enhance 就自然
    落到「高级」附近。
    """
    d = engine.custom_defaults()
    top = engine.DIFFICULTY[5]
    for k in CUSTOM_KEYS:
        assert d[k] == top[k], k
    assert d["bias"] is None


# --------------------------------------------------------------- 注入与读取

def test_cfg_for_returns_injected_config():
    cfg = dict(time=5.0, max_depth=9, vcf_budget=0.0, qply=4, enhance=False)
    engine.set_custom_config(cfg)
    got = engine._cfg_for(engine.CUSTOM_LEVEL)
    for k in CUSTOM_KEYS:
        assert got[k] == cfg[k], k


def test_cfg_for_reads_live_not_a_cached_copy():
    """运行期改配置，下一次 ``_cfg_for`` 必须看得见。

    对标 ``tests/test_vcf.py`` 对 ``DIFFICULTY`` 的用法：这张表是活的，缓存一份
    副本就会让"改完不生效"变成一个要靠翻源码才能解释的现象。
    """
    engine.set_custom_config(dict(time=5.0))
    assert engine._cfg_for(engine.CUSTOM_LEVEL)["time"] == 5.0
    engine.set_custom_config(dict(time=30.0))
    assert engine._cfg_for(engine.CUSTOM_LEVEL)["time"] == 30.0
    engine.reset_custom_config()
    assert engine._cfg_for(engine.CUSTOM_LEVEL)["time"] == 20.0


def test_custom_config_is_a_copy():
    """``custom_config()`` 返回副本 —— 改它不该动到引擎里那份。"""
    snapshot = engine.custom_config()
    snapshot["time"] = 1.0
    assert engine.custom_config()["time"] == 20.0
    assert engine._cfg_for(engine.CUSTOM_LEVEL)["time"] == 20.0


def test_set_custom_config_ignores_unknown_and_none():
    """缺项 / 显式 None 都保留出厂值，未知键被丢掉。"""
    engine.set_custom_config(dict(time=5.0, nonsense=1, max_depth=None))
    cfg = engine.custom_config()
    assert cfg["time"] == 5.0
    assert cfg["max_depth"] == engine.custom_defaults()["max_depth"]
    assert "nonsense" not in cfg


def test_set_custom_config_pins_name_and_clears_bias():
    """``name`` 与 ``bias`` 不接受注入。

    ``name`` 是界面与日志的显示名，让它被配置改掉就等于让用户能给档位改名；
    ``bias``（根节点进攻偏置）在 ``DIFFICULTY`` 里只为第 1 档存在，且实测在中低
    档会让 AI 变弱（中级 0 胜 8 负），做成旋钮等于邀请用户把 AI 调坏。
    """
    engine.set_custom_config(dict(name="宗师", bias=dict(attack=9.0, defence=0.0,
                                                        tolerance=3000.0)))
    cfg = engine.custom_config()
    assert cfg["name"] == "自定义"
    assert cfg["bias"] is None


# --------------------------------------------------------------- 配置真的下发了

def _board():
    """一个非空、且**不在开局库里的**局面。

    ``ai_move`` 会先查开局库，命中就直接走本地，压根到不了远程分支 —— 用天元
    附近的常规开局做样本的话，这组用例会静默地什么都没测到。
    """
    engine.new_game()
    a = np.zeros((engine.BOARD_SIZE, engine.BOARD_SIZE), dtype=np.uint8)
    for i, (r, c) in enumerate([(3, 4), (3, 5), (4, 6), (12, 13), (5, 9)]):
        a[r, c] = 1 if i % 2 == 0 else 2
    assert engine._local.book_lookup(a, 2) is None
    return a, 2


class _FakeClient:
    """顶掉 TCP 客户端，把拼好的请求截下来。"""

    def __init__(self):
        self.seen = []
        self._info = {"best_val": 1, "depth": 1, "nodes": 1, "ms": 1,
                      "engine": "cpp"}

    def mark_reset_needed(self):
        """``engine.new_game()`` 会调它 —— 真客户端只置一个标记，不发请求。"""

    def compute(self, board, me, cfg, cancel):
        self.seen.append(cfg)
        return 0, dict(self._info)

    def analyze(self, board, me, cfg, cancel):
        self.seen.append(cfg)
        return 0, dict(self._info), [(0, 1)]


@pytest.fixture
def fake(monkeypatch):
    """把两条路都换成一个只记录配置的假客户端。

    ``ai_move`` 的远程分支走 ``engine._REMOTE_PLAYER``（它持有自己的 client），
    ``analyze`` 直接走 ``engine._CLIENT`` —— 两处都得换，否则测的还是真 socket。
    """
    f = _FakeClient()
    monkeypatch.setattr(engine, "_CLIENT", f)
    monkeypatch.setattr(engine, "_REMOTE_PLAYER", engine.RemoteAIPlayer(f))
    return f


def test_ai_move_and_analyze_share_one_config(fake):
    """对局与复盘走的是同一份自定义配置。

    ``engine.py`` 里那条既有的不变量是"compute 与 analyze 的字段逐字相同"；
    自定义档必须在**配置这一层**也守住它 —— 两份引擎各读各的表，配置分叉了就
    会出现"对局按 30 秒搜、复盘按 20 秒算"。
    """
    engine.set_custom_config(dict(time=5.0, max_depth=9, vcf_budget=0.0, qply=4,
                                  enhance=False))
    board, me = _board()

    engine.ai_move(board, me, engine.CUSTOM_LEVEL)
    engine.analyze(board, me, engine.CUSTOM_LEVEL)

    assert len(fake.seen) == 2
    a, b = fake.seen
    assert a == b
    assert a["time"] == 5.0
    assert a["max_depth"] == 9


def test_live_table_edit_reaches_the_request(fake):
    """改配置之后，**下发的请求里**的时限跟着变。

    只断言 ``_cfg_for`` 变了还不够 —— 真正要钉的是这条改动确实走到了报文上。
    """
    board, me = _board()

    engine.set_custom_config(dict(time=5.0))
    engine.ai_move(board, me, engine.CUSTOM_LEVEL)
    engine.set_custom_config(dict(time=30.0))
    engine.ai_move(board, me, engine.CUSTOM_LEVEL)

    assert [c["time"] for c in fake.seen] == [5.0, 30.0]


def test_analyze_cfg_argument_overrides_the_level(fake):
    """``analyze(..., cfg=...)`` 显式传的那份优先。

    复盘读的是**开局那一刻快照的参数**：用户打完棋再打开自定义弹窗改几个数字，
    这一局的复盘结果不该跟着变。所以复盘这条路上"传进来的"必须赢过"现读的"。
    """
    engine.set_custom_config(dict(time=20.0))
    board, me = _board()
    engine.analyze(board, me, engine.CUSTOM_LEVEL,
                   cfg=dict(time=3.0, max_depth=4, vcf_budget=0.3, qply=4,
                            enhance=False))
    seen = fake.seen[0]
    assert seen["time"] == 3.0
    assert seen["max_depth"] == 4


def test_reset_restores_defaults():
    engine.set_custom_config(dict(time=1.0, max_depth=4))
    engine.reset_custom_config()
    assert engine.custom_config() == engine.custom_defaults()

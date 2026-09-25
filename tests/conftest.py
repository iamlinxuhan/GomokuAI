# -*- coding: utf-8 -*-
"""pytest 公共夹具。"""

from __future__ import annotations

import os
import sys

# ---- GUI 离屏测试的环境准备（必须在任何 QApplication 之前）----
# 项目路径含非 ASCII（"桌面"）时 Qt 会丢掉插件目录（main._fix_qt_plugin_path
# 修的正是同一个问题）；这里在 conftest 导入期就把离屏平台与插件路径备好。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if not os.environ.get("QT_QPA_PLATFORM_PLUGIN_PATH"):
    try:
        import PyQt5
        _platforms = os.path.join(os.path.dirname(PyQt5.__file__),
                                  'Qt5', 'plugins', 'platforms')
        if os.path.isdir(_platforms):
            os.environ['QT_QPA_PLATFORM_PLUGIN_PATH'] = os.path.dirname(_platforms)
    except Exception:
        pass

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

BOARD_SIZE = 19
BLACK, WHITE = 1, 2


@pytest.fixture
def empty_board():
    return np.zeros((BOARD_SIZE, BOARD_SIZE), dtype=np.uint8)


@pytest.fixture
def board_factory():
    """board_factory(black=[(r,c)...], white=[(r,c)...]) -> ndarray"""
    def make(black=(), white=()):
        m = np.zeros((BOARD_SIZE, BOARD_SIZE), dtype=np.uint8)
        for r, c in black:
            m[r][c] = BLACK
        for r, c in white:
            m[r][c] = WHITE
        return m
    return make


@pytest.fixture(scope="session")
def legacy_engine():
    """冻结的旧引擎（A/B 基线）。"""
    import tools.legacy_engine as mod
    return mod


@pytest.fixture(autouse=True)
def _isolate_legacy_globals(request):
    """把旧引擎的模块级可变状态在**每个用例前后**清空。

    旧引擎把 TT / history / killer / eval 缓存全放在模块全局（main.py 的
    `_transposition_table` 等），于是不同调用之间会互相污染：同一局面第二次
    搜索会命中第一次留下的 TT，返回"缓存结果"而非真实搜索结果。这既是
    重写要消除的缺陷之一，也会让测试之间的结果不可独立复现。
    """
    try:
        legacy = request.getfixturevalue("legacy_engine")
    except Exception:  # noqa: BLE001 — 用例未请求该夹具时无需隔离
        yield
        return

    from tools.positions import reset_engine

    reset_engine(legacy)
    yield
    reset_engine(legacy)


@pytest.fixture(scope="session")
def new_engine():
    """本地引擎实现（原 ``engine.py``，重构后改名）。

    重构后 ``engine.py`` 是 TCP 客户端，真正含算法的是 ``engine_local.py``。
    这一组用例测的是**算法本身**（增量一致性 / 置换表 / VCF / 难度），所以
    夹具指向本地实现，与重构前逐字相同。
    """
    try:
        import engine_local as mod
    except ImportError:
        pytest.skip("engine_local.py 尚未落地（Phase 1+）")
    return mod

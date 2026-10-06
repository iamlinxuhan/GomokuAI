# -*- coding: utf-8 -*-
"""pytest 公共夹具。"""

from __future__ import annotations

import os
import sys

# ---- GUI 离屏测试的环境准备（必须在任何 QApplication 之前）----
# 项目路径含非 ASCII（"桌面"）时 Qt 会丢掉插件目录（main._fix_qt_plugin_path
# 修的正是同一个问题）；这里在 conftest 导入期就把离屏平台与插件路径备好。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# **关掉界面自动适配。** 离屏平台报的可用区是 800×600，比设计尺寸 1022×750
# 还小；自动适配一旦生效，会去把窗口降到 722×552、字号降到 small，于是所有
# 既有的尺寸/截图断言全部失守 —— 而它们测的并不是"自动适配"这件事。
# 自动适配本身由 `tests/test_ui_scale.py` 直接测纯函数 `_autofit_scale`。
os.environ.setdefault("GOMOKU_AI_UI_AUTOFIT", "0")
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


@pytest.fixture(scope="session", autouse=True)
def _neutral_ui_scale():
    """测试进程不继承开发者机器上存着的字号档位。

    ``theme.install()`` 现在会从 QSettings 读 ``ui_scale``。开发者只要在设置页
    点过一次"特大"，全套 GUI 的尺寸断言就会在**他那台机器上**红，而失败点与
    他改的代码毫无关系 —— 与 ``install()`` 里主题那条注释是同一个道理。这里在
    任何 ``QApplication`` 之前（session 级、autouse）把它钉回 normal。
    """
    import theme
    theme.set_scale("normal", persist=False)


@pytest.fixture(scope="session", autouse=True)
def _neutral_ui_language():
    """测试进程不继承开发者机器上选着的界面语言。

    `_neutral_ui_scale` 的同构版本，防的是同一种事故：界面文案现在是查表翻的，
    开发者在设置里点过一次俄语，所有断言中文文案的用例（``"⚙"`` 的 tooltip、
    局域网那几句提示）就会在**他那台机器上**红，而失败点与他改的代码无关。

    钉死之后还要把 ``main._load_language`` 换成空操作：``GomokuGame.__init__``
    每次都调它，会把落盘的语言读回来盖掉上面这一行。测试要的是确定的语言，
    所以这一步在测试进程里整个跳过。
    """
    import i18n
    # 系统语言也一并钉住：``set_language(SYSTEM)`` 时要落到一个确定的答案上，
    # 不能随测试机是 en_US 还是 ja_JP 而变。
    i18n.set_system_locale("zh_CN")
    i18n.set_language(i18n.DEFAULT)

    import main
    main._load_language = lambda: None


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

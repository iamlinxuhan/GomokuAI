# -*- coding: utf-8 -*-
"""Qt 绑定的收口点 ``qt_compat``。

业务代码只认这一层：优先 PySide6（Qt 6），32 位平台回退 PyQt5。这里盯两件
会在 CI 上兑现的事 —— 绑定与主版本对得上、插件目录真的存在（中文路径下
Qt 会丢掉插件路径，``main._fix_qt_plugin_path`` 靠的就是它）。
"""

from __future__ import annotations

import os

import qt_compat


def test_binding_and_major_version_agree():
    assert qt_compat.API in ("PySide6", "PyQt5")
    assert qt_compat.QT_MAJOR == (6 if qt_compat.API == "PySide6" else 5)


def test_plugin_root_contains_platforms():
    platforms = os.path.join(str(qt_compat.PLUGIN_ROOT), "platforms")
    assert os.path.isdir(platforms), platforms


def test_signals_are_aliased():
    """PySide6 叫 Signal，业务代码继续写 pyqtSignal —— 别名必须是真的。"""
    assert hasattr(qt_compat.pyqtSignal, "__call__")

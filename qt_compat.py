# -*- coding: utf-8 -*-
"""Qt 绑定的唯一入口：优先 PySide6，32 位平台回退 PyQt5。

## 为什么要换

PyQt5 是 Qt 5；PySide6 是 Qt 6。Qt 6 的渲染管线（RHI）与图形栈在合成、缩放
和高 DPI 下走的是另一条路，界面空闲时的事件循环也更省。绑定本身由 Qt 官方
维护，许可证是 LGPLv3（PyQt5 是 GPLv3），对"链接它的二进制"约束更轻。

## 为什么保留 PyQt5

i386 与 armv7 两个包没有 PySide6 轮子（Qt 6 官方不支持 32 位）。这两个架构
继续装 PyQt5，其余平台装 PySide6。**业务代码只 import 本模块**，两条绑定在
这里收口，不在界面代码里散落 ``if``。

## 用法

    from qt_compat import QWidget, Qt, pyqtSignal

名字与原先 ``from PyQt5.QtWidgets import ...`` 完全一致 —— 信号仍叫
``pyqtSignal``（PySide 的 ``Signal`` 在这里起别名），枚举继续用短写法
（PySide6 默认开启 forgiveness mode，``Qt.AlignCenter`` 可用）。
"""

from __future__ import annotations

import os
from pathlib import Path

#: ``GOMOKU_QT_BINDING=PyQt5`` 可强制走 Qt 5 那一支，给"两条绑定跑同一套
#: 测试"用（CI 与本地对比都靠它）。留空就按平台自动挑。
_FORCE = os.environ.get("GOMOKU_QT_BINDING", "").strip()
_use_pyqt5 = _FORCE == "PyQt5"
if not _use_pyqt5:
    try:                                # PySide6（Qt 6）优先
        from PySide6 import QtCore, QtGui, QtWidgets
        from PySide6.QtCore import (Signal as pyqtSignal, Slot as pyqtSlot,
                                    Property as pyqtProperty)
        from PySide6.QtCore import *    # noqa: F401,F403
        from PySide6.QtGui import *     # noqa: F401,F403
        from PySide6.QtWidgets import *  # noqa: F401,F403
        API = "PySide6"
        QT_MAJOR = 6
    except ImportError:                 # 32 位平台没有 Qt 6 轮子
        _use_pyqt5 = True

if _use_pyqt5:
    from PyQt5 import QtCore, QtGui, QtWidgets
    from PyQt5.QtCore import (pyqtSignal, pyqtSlot, pyqtProperty)
    from PyQt5.QtCore import *          # noqa: F401,F403
    from PyQt5.QtGui import *           # noqa: F401,F403
    from PyQt5.QtWidgets import *       # noqa: F401,F403
    API = "PyQt5"
    QT_MAJOR = 5

#: ``QTest`` 只给冒烟工具用，**不进打包产物**（PyInstaller 里被排除）。
#: 单独 try：它缺席时退回 PyQt5 是灾难 —— 包按一个绑定打的，另一个根本没装。
try:
    if QT_MAJOR == 6:
        from PySide6.QtTest import QTest
    else:
        from PyQt5.QtTest import QTest
except ImportError:                     # 冻结包里没有 QtTest，正常
    QTest = None

Qt = QtCore.Qt

#: Qt 插件目录（platforms / imageformats 等）。中文路径下 Qt 会丢掉搜索路径，
#: 包一层显式指定时要用它；两种绑定的目录层级不同。
_BINDING_DIR = Path(QtCore.__file__).resolve().parent
PLUGIN_ROOT = (_BINDING_DIR / "Qt" / "plugins" if QT_MAJOR == 6
               else _BINDING_DIR / "Qt5" / "plugins")

__all__ = ["QtCore", "QtGui", "QtWidgets", "Qt", "pyqtSignal", "pyqtSlot",
           "pyqtProperty", "QTest", "API", "QT_MAJOR", "PLUGIN_ROOT"]

# -*- coding: utf-8 -*-
"""动画工具：页面淡入 / 呼吸脉动 / 落子动画常量。

## 设计原则

* **纯函数、零状态**：函数返回 ``QPropertyAnimation``，生命周期交给
  ``DeleteWhenStopped`` 或调用方持有。
* **零颜色字面量**：这里只做运动（时长 / 缓动 / 位移），视觉归属
  ``theme.py`` —— ``tests/test_no_literal_colors.py`` 同样守卫本模块。
* **动效语言是克制**：全部短促（落子 160ms、切页 180ms），唯一的长动画是
  AI 思考的 1.2s 呼吸循环。无弹跳 —— 棋类应用要的是沉稳。

## QGraphicsOpacityEffect 的生命周期坑

给控件 ``setGraphicsEffect(effect)`` 之后，effect 的父对象是控件本身；
动画结束**必须** ``setGraphicsEffect(None)`` 把它摘掉，否则该控件从此走
离屏合成路径（QSS 背景绘制行为也会变化），白白多一层开销。
"""

from __future__ import annotations

from PyQt5.QtCore import QAbstractAnimation, QEasingCurve, QPropertyAnimation
from PyQt5.QtWidgets import QGraphicsOpacityEffect, QWidget

# ---- 时长（毫秒）----
FADE_MS = 180       # 页面切换淡入
PULSE_MS = 1200     # AI 思考呼吸周期
STONE_MS = 160      # 落子下落

# ---- 落子动画参数 ----
# 下落起始偏移（像素，随格距缩放在 BoardWidget 里乘 k）与起始不透明度。
# 结束值恒为 (0, 1.0)：动画结束时覆盖绘制的 sprite 与缓存层中的同一颗子
# 逐像素重合，无跳变。
STONE_DROP_PX = 8.0
STONE_FADE_FROM = 0.5


#: 在途淡入动画的**强引用**。见 ``fade_in`` —— 少了它会在切页时随机段错误。
_flying = set()


def _sweep() -> None:
    """把已经停下、或随控件一起销毁的动画从强引用表里清掉。

    ``fade_in`` 每次都调一遍，所以这张表不会有意义地增长。控件先塌的那种
    情况（页面在淡入途中被 ``deleteLater``）拿不到 ``finished``，只能在这里
    靠"包装对象已失效"认出来 —— 访问它的任何方法都会 ``RuntimeError``。
    """
    for a in list(_flying):
        try:
            if a.state() == QAbstractAnimation.Stopped:
                _flying.discard(a)
        except RuntimeError:            # C++ 对象已随控件销毁
            _flying.discard(a)


def fade_in(widget: QWidget, ms: int = FADE_MS) -> QPropertyAnimation:
    """控件从 0 → 1 淡入（OutCubic）。结束后自动摘掉透明度效果。

    可作用于任何 widget（页面、卡片、覆盖层）。重复调用时旧的 effect
    会被替换，不会叠加。

    **必须自己拿住返回的动画**（``_flying``）。动画的父对象是控件，C++ 侧
    活得好好的；但它的 Python 包装对象出了这个函数就没主了 —— 一旦被 GC，
    PyQt 为 ``finished`` 建的那个槽代理跟着失效，而动画还在跑。跑完时
    ``QUnifiedTimer`` 会往一个已经释放的槽上投递，栈里只留下一个 ``<lambda>``
    帧，进程直接段错误（2026-10-05：连开几次切页淡入必崩，且崩在哪一次
    取决于 GC 的时机）。

    ``DeleteWhenStopped`` 删的是 C++ 对象，不是这个引用 —— 表里的死条目由
    ``_sweep`` 收。
    """
    _sweep()
    effect = QGraphicsOpacityEffect(widget)
    widget.setGraphicsEffect(effect)
    anim = QPropertyAnimation(effect, b"opacity", widget)
    anim.setDuration(ms)
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)
    anim.setEasingCurve(QEasingCurve.OutCubic)

    def _retire():
        _flying.discard(anim)
        widget.setGraphicsEffect(None)

    anim.finished.connect(_retire)
    _flying.add(anim)
    anim.start(QPropertyAnimation.DeleteWhenStopped)
    return anim


def pulse(widget: QWidget, ms: int = PULSE_MS,
          low: float = 0.55, high: float = 1.0) -> QPropertyAnimation:
    """呼吸循环：opacity 在 low↔high 之间往返，无限循环。

    与 ``fade_in`` 不同，这个动画**不会自己停** —— 调用方必须持有返回值，
    不想动时调 ``stop_pulse(widget)``（或对返回的 anim ``stop()`` 后
    ``widget.setGraphicsEffect(None)``）。
    """
    effect = QGraphicsOpacityEffect(widget)
    widget.setGraphicsEffect(effect)
    anim = QPropertyAnimation(effect, b"opacity", widget)
    anim.setDuration(ms)
    anim.setStartValue(high)
    anim.setKeyValueAt(0.5, low)
    anim.setEndValue(high)
    anim.setLoopCount(-1)
    anim.setEasingCurve(QEasingCurve.InOutSine)
    anim.start()
    return anim


def stop_pulse(widget: QWidget) -> None:
    """停掉挂在 widget 上的脉动并恢复正常绘制（无动画时安全调用）。"""
    effect = widget.graphicsEffect()
    if isinstance(effect, QGraphicsOpacityEffect):
        widget.setGraphicsEffect(None)   # 摘除即销毁，动画随之失效

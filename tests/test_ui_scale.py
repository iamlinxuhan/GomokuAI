# -*- coding: utf-8 -*-
"""界面字号档位与自动适配。

三件事必须钉住：

1. ``set_scale`` 真的把 ``theme.SIZE_*`` 等**模块全局**改了，且 ``app_stylesheet()``
   里出现的是新值 —— 界面上"看起来变了"必须能追溯到样式表，而不是靠某个控件
   恰好读了别的数。
2. ``mono_font()`` 不带参数时取的是**当前**档位的 ``SIZE_XS``。默认参数在 def
   那一刻就固化，这是 ``ui_kit`` / ``charts`` 那类"半缩放"割裂的来源；这里守住
   它不要再造一个。
3. ``_autofit_scale`` 只降不升，且阈值判据与 ``_initial_size`` 是同一个 ``k``。

必须在 QApplication 之后测 —— ``app_stylesheet`` 要用 QFontDatabase。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtCore import QRect, QSettings
from PyQt5.QtWidgets import QApplication

import main as M
import theme


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _restore_scale(qapp):
    """测试不污染用户的档位偏好，也不把档位泄漏给别的用例。

    档位是**模块全局**（``theme.SIZE_*`` 被重新赋值），一个用例把它改成"特大"
    之后，同一进程里后面的用例量到的就是特大号的尺寸 —— 失败点与它们改的代码
    毫无关系。
    """
    s = QSettings(theme._SETTINGS_ORG, theme._SETTINGS_APP)
    old_saved = s.value(theme._SETTINGS_SCALE_KEY, None)
    yield
    if old_saved is None:
        s.remove(theme._SETTINGS_SCALE_KEY)
    else:
        s.setValue(theme._SETTINGS_SCALE_KEY, old_saved)
    theme.set_scale("normal", persist=False)


# --------------------------------------------------------------- 档位本身

def test_available_scales_and_labels():
    assert theme.available_scales() == ("small", "normal", "large", "xlarge")
    for name in theme.available_scales():
        assert theme.scale_label(name)          # 每档都有中文名
    assert theme.scale_label("normal") == "标准"


def test_set_scale_reassigns_module_globals(qapp):
    """档位改变的是**模块全局**，且 ``normal`` 恰好等于基准值。

    这条同时守住"来回切档不累积误差"：从 small 切回 normal 必须精确回到基准。
    """
    base_md = theme.SIZE_MD
    base_panel = theme.PANEL_W
    theme.set_scale("normal", persist=False)
    assert theme.SIZE_MD == base_md and theme.PANEL_W == base_panel

    theme.set_scale("xlarge", persist=False)
    assert theme.SIZE_MD > base_md, "特大档的字号必须真的变大"
    assert theme.PANEL_W > base_panel, "面板宽度也要跟着走，否则特大字会挤爆"

    theme.set_scale("small", persist=False)
    assert theme.SIZE_MD < base_md

    theme.set_scale("normal", persist=False)
    assert theme.SIZE_MD == base_md, "切回 normal 必须精确等于基准，不能有取整漂移"
    assert theme.PANEL_W == base_panel


def test_stylesheet_contains_current_sizes(qapp):
    """样式表里的字号跟着档位走 —— 否则界面看起来根本没变。"""
    theme.set_scale("normal", persist=False)
    normal_css = theme.app_stylesheet()
    theme.set_scale("xlarge", persist=False)
    big_css = theme.app_stylesheet()
    assert normal_css != big_css
    # 按钮那档字号（SIZE_MD）应当以 px 的形式出现在样式表里。
    assert ("font-size: %dpx" % theme.SIZE_MD) in big_css
    theme.set_scale("normal", persist=False)
    assert ("font-size: %dpx" % theme.SIZE_MD) in theme.app_stylesheet()


def test_unknown_scale_raises(qapp):
    with pytest.raises(ValueError):
        theme.set_scale("巨大")


def test_persist_false_does_not_touch_settings(qapp):
    s = QSettings(theme._SETTINGS_ORG, theme._SETTINGS_APP)
    s.setValue(theme._SETTINGS_SCALE_KEY, "normal")
    theme.set_scale("large", persist=False)
    assert s.value(theme._SETTINGS_SCALE_KEY) == "normal"


def test_persist_true_writes_settings(qapp):
    s = QSettings(theme._SETTINGS_ORG, theme._SETTINGS_APP)
    theme.set_scale("large")
    assert s.value(theme._SETTINGS_SCALE_KEY) == "large"
    assert theme.saved_scale() == "large"


def test_saved_scale_none_when_absent(qapp):
    s = QSettings(theme._SETTINGS_ORG, theme._SETTINGS_APP)
    s.remove(theme._SETTINGS_SCALE_KEY)
    assert theme.saved_scale() is None


def test_install_restores_saved_scale(qapp):
    """``install()`` 恢复落盘档位 —— 与主题同一条路径。

    ``_explicit_scale`` 要清掉：``set_scale`` 会把它置位，置位之后 install 就
    不再恢复落盘值（那是"本进程显式选过"的意思），而这条测的正是恢复路径本身。
    """
    s = QSettings(theme._SETTINGS_ORG, theme._SETTINGS_APP)
    s.setValue(theme._SETTINGS_SCALE_KEY, "xlarge")
    theme.set_scale("normal", persist=False)
    theme._installed = False
    theme._explicit_scale = False
    theme.install()
    assert theme.current_scale() == "xlarge"

    s.setValue(theme._SETTINGS_SCALE_KEY, "不是合法档位名")
    theme._installed = False
    theme._explicit_scale = False
    theme.install()                      # 非法记录回退 normal
    assert theme.current_scale() == "normal"


# --------------------------------------------------------------- 默认参数固化

def test_mono_font_default_follows_current_scale(qapp):
    """``mono_font()`` 不带参数时用**当前**档位的 ``SIZE_XS``。

    这条是专门守住"默认参数在 def 时固化"那个坑的 —— 写成
    ``def mono_font(size_px: int = SIZE_XS)`` 的话，切档后它纹丝不动。
    """
    theme.set_scale("normal", persist=False)
    assert theme.mono_font().pixelSize() == theme.SIZE_XS

    theme.set_scale("xlarge", persist=False)
    assert theme.mono_font().pixelSize() == theme.SIZE_XS
    assert theme.mono_font().pixelSize() > 12, "特大档下等宽字号必须真的变大"

    # 显式传参仍然照传不误。
    assert theme.mono_font(9).pixelSize() == 9


# --------------------------------------------------------------- 自动适配

def test_autofit_scale_is_lower_only():
    """只降不升：再大的屏也不返回 large / xlarge。"""
    for w, h in ((1022, 750), (1280, 720), (1920, 1080),
                 (2560, 1440), (3840, 2160)):
        assert M._autofit_scale(w, h) in ("small", "normal"), (w, h)


def test_autofit_scale_thresholds():
    """判据与 ``_initial_size`` 同一个 ``k``，阈值 0.92。"""
    dw, dh = M._design_size()
    # 刚够放（k 略大于 0.92）→ normal
    assert M._autofit_scale(int(dw / 0.95), int(dh / 0.95)) == "normal"
    # 放不下 → small。1920×1080 @150% 的逻辑屏正是 1280×720。
    assert M._autofit_scale(1280, 720) == "small"


def test_autofit_scale_handles_degenerate_input():
    assert M._autofit_scale(0, 0) == "normal"
    assert M._autofit_scale(-100, -100) == "normal"


def test_autofit_env_kill_switch(monkeypatch):
    """``GOMOKU_AI_UI_AUTOFIT=0`` 是给离屏测试用的急停开关。"""
    monkeypatch.setenv(M.AUTOFIT_ENV, "0")
    assert M._autofit_enabled() is False
    monkeypatch.setenv(M.AUTOFIT_ENV, "1")
    assert M._autofit_enabled() is True
    monkeypatch.delenv(M.AUTOFIT_ENV)
    assert M._autofit_enabled() is True


# --------------------------------------------------------------- 像素助手

def test_px_scales_with_current_scale(qapp):
    theme.set_scale("normal", persist=False)
    assert M._px(100) == 100
    theme.set_scale("small", persist=False)
    assert M._px(100) == int(round(100 * theme.scale_factor()))
    assert M._px(100) < 100
    theme.set_scale("normal", persist=False)


def test_px_never_returns_zero(qapp):
    """再小的档位也不能把高度算成 0 —— 那会让控件彻底看不见。"""
    assert M._px(1) >= 1


def test_settings_button_is_icon_only(qapp):
    """设置入口只有**一种**长相：32px 见方的纯图标按钮。

    ``iconOnly`` 是给 ``theme`` 认的 —— 变体 QSS 里那条 ``padding: 0 20px``
    是给文字按钮写的，放到 32px 的小按钮上会把齿轮本身裁掉（用户 2026-10-05
    截图报的"设置显示残缺"）。
    """
    b = M.settings_button()
    try:
        assert b.text() == "⚙"
        assert b.toolTip() == "字号 / 主题"
        assert b.width() == b.height(), "必须是方的：图标按钮的长宽比不该是凑的"
        assert b.property("iconOnly") == "true"
        # 样式表里必须真有一条能把 padding 压回去的规则，否则属性设了也没用。
        css = theme.app_stylesheet()
        assert 'QPushButton[iconOnly="true"]' in css
        # 而它必须排在变体规则**之后**（同特异性下靠后的胜）。
        assert css.index('QPushButton[iconOnly="true"]') > css.index(
            'QPushButton[variant="ghost"]')
    finally:
        b.deleteLater()


# --------------------------------------------------- 启动尺寸不被最小尺寸锁死

class _FakeScreen:
    """只提供 ``availableGeometry()`` 的假屏幕。

    ``_initial_size`` / ``_min_size`` / ``_fit_cap_h`` 只问这一个方法，
    把整块真实屏幕换掉是**唯一**能把 1920×1080 @150% 那台机器搬进单测的办法。
    """

    def __init__(self, w, h):
        self._rect = QRect(0, 0, w, h)

    def availableGeometry(self):
        return self._rect

    def geometry(self):
        return self._rect


def _fake_screen(monkeypatch, w, h):
    """把主屏换成一个 ``w×h`` 的假屏，并打开自动适配。"""
    fake = _FakeScreen(w, h)
    monkeypatch.setenv(M.AUTOFIT_ENV, "1")
    monkeypatch.setattr(M.QApplication, "primaryScreen",
                        staticmethod(lambda: fake))


class _NoFrame:
    """``_fit_cap_h`` 只用 ``self._frame_h`` 一个实例属性，给个壳就够。"""

    _frame_h = 0


#: 各种屏幕可用区（逻辑像素）。后两组是"又矮又宽"的退化比例 —— 老代码在那里
#: 也会把窗口顶出屏幕，只是没人碰得到而已。
_SCREENS = ((1280, 680), (1280, 720), (1920, 500), (800, 600), (1022, 750),
            (3840, 2160))


def test_min_size_never_exceeds_initial_size(monkeypatch, qapp):
    """**启动尺寸不被锁死**的直接表述：最小尺寸永不越过初始尺寸。

    ``Qt`` 会把 ``resize()`` 静默夹回最小尺寸，所以只要最小尺寸大于按比例算
    出来的初始尺寸，窗口就会变成最小尺寸那么大 —— 用户在 1920×1080 @150%
    （逻辑可用区约 1280×680）上看到的"启动尺寸超出屏幕高度"正是这么来的。
    """
    for name in theme.available_scales():
        theme.set_scale(name, persist=False)
        for sw, sh in _SCREENS:
            _fake_screen(monkeypatch, sw, sh)
            iw, ih = M.GomokuGame._initial_size()
            mw, mh = M.GomokuGame._min_size()
            assert mw <= iw and mh <= ih, (
                f"档位 {name} / 屏幕 {sw}×{sh}：最小 {mw}×{mh} 越过了初始 {iw}×{ih}")
            assert 0 < iw <= sw * 0.92 + 1 and 0 < ih <= sh * 0.92 + 1, (
                f"档位 {name} / 屏幕 {sw}×{sh}：初始 {iw}×{ih} 超出了可用区")


def test_initial_size_keeps_room_for_the_window_frame(monkeypatch, qapp):
    """可用高度要扣掉标题栏 —— 客户区贴着可用区上沿时标题栏就悬到屏幕外了。"""
    theme.set_scale("normal", persist=False)
    sw, sh = 1280, 680
    _fake_screen(monkeypatch, sw, sh)
    _, ih = M.GomokuGame._initial_size()
    assert ih + M.FRAME_FALLBACK_H <= sh, (
        f"客户区 {ih} + 外框 {M.FRAME_FALLBACK_H} 超过了可用高度 {sh}")


def test_fit_cap_h_follows_the_current_scale(monkeypatch, qapp):
    """``_fit_cap_h`` 的上限是**当前档位的设计高度**，不是模块常量 ``WINDOW_H``。

    老写法 ``min(WINDOW_H, cap)`` 在小屏降档后仍按 normal 档的 750 算，等于把
    窗口最小高度顶回屏幕外 —— 另一个"启动尺寸溢出"的来路。
    """
    theme.set_scale("small", persist=False)
    _fake_screen(monkeypatch, 1280, 680)
    dh = M._design_size()[1]
    assert dh < M.WINDOW_H, "小档的设计高度必须比 normal 档矮，否则这条测不到东西"
    cap = M.GomokuGame._fit_cap_h(_NoFrame())
    assert cap <= dh, f"{cap} 越过了小档设计高度 {dh}"
    assert cap <= 680 * 0.96

    # 自动适配关掉时（离屏测试）保持原样：那些断言全按设计尺寸写。
    monkeypatch.setenv(M.AUTOFIT_ENV, "0")
    assert M.GomokuGame._fit_cap_h(_NoFrame()) == M.WINDOW_H

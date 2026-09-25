# -*- coding: utf-8 -*-
"""theme 双调色板：token 完备性 / __getattr__ 代理 / 切换与持久化。

必须在 QApplication 之后测 —— ``app_stylesheet`` 要用 QFontDatabase。
离屏平台足够，不需要真实显示。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtCore import QSettings
from PyQt5.QtWidgets import QApplication

import theme


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _preserve_saved_theme(qapp):
    """测试不污染用户的主题偏好：记下原值，用例结束还原。"""
    s = QSettings(theme._SETTINGS_ORG, theme._SETTINGS_APP)
    old = s.value(theme._SETTINGS_KEY, "dark")
    yield
    s.setValue(theme._SETTINGS_KEY, old)
    theme.set_theme("dark", persist=False)


# 调色板必须齐备的界面 token（缺一个，QSS 模板 substitute 当场 KeyError）。
EXPECTED_KEYS = {
    "BG", "BG_HI", "SURFACE", "SURFACE_HI", "SURFACE_2", "BORDER", "TRACK",
    "TEXT", "TEXT_DIM", "TEXT_FAINT",
    "ACCENT", "ACCENT_HI", "ACCENT_LO", "ON_ACCENT",
    "SUCCESS", "SUCCESS_HI", "SUCCESS_LO",
    "DANGER", "DANGER_HI", "DANGER_LO",
    "INFO", "GHOST_HOVER", "GHOST_PRESS", "OVERLAY",
}


def test_two_palettes_with_identical_keys():
    names = set(theme._PALETTES)
    assert names == {"dark", "light"}
    keys = {n: set(p) for n, p in theme._PALETTES.items()}
    for n, k in keys.items():
        assert EXPECTED_KEYS <= k, f"{n} 缺 token：{EXPECTED_KEYS - k}"
    assert keys["dark"] == keys["light"], "两套调色板键集必须一致"


def test_getattr_proxies_current_palette(qapp):
    theme.set_theme("dark", persist=False)
    assert theme.ACCENT == theme._PALETTES["dark"]["ACCENT"]
    theme.set_theme("light", persist=False)
    assert theme.ACCENT == theme._PALETTES["light"]["ACCENT"]
    assert theme.ACCENT != theme._PALETTES["dark"]["ACCENT"]


def test_board_colors_are_shared_module_attrs(qapp):
    """棋盘色不随主题切换（模块常量，不走 __getattr__）。"""
    dark = theme.WOOD
    theme.set_theme("light", persist=False)
    assert theme.WOOD is dark
    theme.set_theme("dark", persist=False)


def test_unknown_token_raises_attribute_error():
    with pytest.raises(AttributeError):
        theme.NO_SUCH_TOKEN


def test_set_theme_unknown_name_raises(qapp):
    with pytest.raises(ValueError):
        theme.set_theme("solarized", persist=False)


def test_toggle_theme(qapp):
    theme.set_theme("dark", persist=False)
    assert theme.toggle_theme(persist=False) == "light"
    assert theme.current_theme() == "light"
    assert theme.toggle_theme(persist=False) == "dark"


def test_stylesheet_tracks_palette(qapp):
    theme.set_theme("dark", persist=False)
    dark_acc = theme._PALETTES["dark"]["ACCENT"]
    assert dark_acc in theme.app_stylesheet()
    theme.set_theme("light", persist=False)
    light_acc = theme._PALETTES["light"]["ACCENT"]
    qss = theme.app_stylesheet()
    assert light_acc in qss
    assert dark_acc not in qss
    theme.set_theme("dark", persist=False)


def test_set_theme_reinstalls_app_stylesheet(qapp):
    theme.install()
    theme.set_theme("light", persist=False)
    assert theme._PALETTES["light"]["BG"] in qapp.styleSheet()
    theme.set_theme("dark", persist=False)
    assert theme._PALETTES["dark"]["BG"] in qapp.styleSheet()


def test_settings_roundtrip(qapp):
    s = QSettings(theme._SETTINGS_ORG, theme._SETTINGS_APP)
    theme.set_theme("light")
    assert s.value(theme._SETTINGS_KEY) == "light"
    theme.set_theme("dark")
    assert s.value(theme._SETTINGS_KEY) == "dark"


def test_persist_false_does_not_touch_settings(qapp):
    s = QSettings(theme._SETTINGS_ORG, theme._SETTINGS_APP)
    s.setValue(theme._SETTINGS_KEY, "dark")
    theme.set_theme("light", persist=False)
    assert s.value(theme._SETTINGS_KEY) == "dark"
    theme.set_theme("dark", persist=False)


def _force_reinstall():
    """让下一次 ``install()`` 真的重读设置。

    ``_explicit`` 也要清 —— ``set_theme`` 会把它置位，而置位之后 install 就
    不再恢复落盘的偏好（那是"本进程显式选过"的意思）。这里要测的正是恢复
    路径本身，所以两个标志都得复位。
    """
    theme._installed = False
    theme._explicit = False


def test_install_restores_saved_theme(qapp):
    s = QSettings(theme._SETTINGS_ORG, theme._SETTINGS_APP)
    s.setValue(theme._SETTINGS_KEY, "light")
    _force_reinstall()
    theme.install()
    assert theme.current_theme() == "light"

    s.setValue(theme._SETTINGS_KEY, "不是合法主题名")
    _force_reinstall()
    theme.install()                     # 非法记录回退深色
    assert theme.current_theme() == "dark"


def test_explicit_choice_beats_saved_theme(qapp):
    """本进程显式选过的主题优先于落盘的偏好。

    少了这条，``theme.set_theme("dark", persist=False)`` 之后一次 ``install()``
    会把主题又拽回用户存的那个值 —— 离屏冒烟测试于是依赖于开发者机器上恰好
    存着什么，症状是"你把主题切浅色之后，主题切换那条用例开始红"。
    """
    s = QSettings(theme._SETTINGS_ORG, theme._SETTINGS_APP)
    s.setValue(theme._SETTINGS_KEY, "light")
    theme.set_theme("dark", persist=False)
    theme._installed = False        # 只复位 install，**不**动 _explicit
    theme.install()
    assert theme.current_theme() == "dark"

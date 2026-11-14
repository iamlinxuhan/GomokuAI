# -*- coding: utf-8 -*-
"""「启动时自动检查更新」这个开关的读写。

用户 2026-11-14：得让人能关掉自动更新。开关本身在设置页里早就有了，缺的是
**关掉之后真的不再查**：偏好读回来是 ``on_start=False``，``GomokuGame`` 才
不会排那次 2.5 秒后的静默检查。这里守的是偏好这一层，按钮到偏好之间由
``test_settings_dialog`` 与 ``gui_smoke`` 覆盖。
"""

from __future__ import annotations

import pytest
from PyQt5.QtCore import QSettings

import main as M
import theme


_KEYS = (M._UPDATE_ON_START_KEY, M._UPDATE_INTERVAL_KEY, M._UPDATE_LAST_KEY)


@pytest.fixture(autouse=True)
def _preserve_pref():
    """别把开发者自己存的偏好改掉：记下原值，用完还原。"""
    store = QSettings(theme._SETTINGS_ORG, theme._SETTINGS_APP)
    old = {key: store.value(key, None) for key in _KEYS}
    yield store
    for key, value in old.items():
        if value is None:
            store.remove(key)
        else:
            store.setValue(key, value)


def test_default_is_on(_preserve_pref):
    """没存过时默认开启 —— 出厂状态要能收到更新。"""
    _preserve_pref.remove(M._UPDATE_ON_START_KEY)
    assert M._load_update_prefs()["on_start"] is True


def test_switching_off_round_trips(_preserve_pref):
    M._save_update_pref(M._UPDATE_ON_START_KEY, "0")
    assert M._load_update_prefs()["on_start"] is False


def test_switching_back_on_round_trips(_preserve_pref):
    M._save_update_pref(M._UPDATE_ON_START_KEY, "0")
    M._save_update_pref(M._UPDATE_ON_START_KEY, "1")
    assert M._load_update_prefs()["on_start"] is True


@pytest.mark.parametrize("raw", ["0", "false", "False", "no", "off", " 0 "])
def test_off_spellings_are_understood(_preserve_pref, raw):
    """老版本/手改配置文件写进来的各种写法都算关闭。"""
    _preserve_pref.setValue(M._UPDATE_ON_START_KEY, raw)
    assert M._load_update_prefs()["on_start"] is False


@pytest.mark.parametrize("raw", ["1", "true", "yes", "on", ""])
def test_on_spellings_are_understood(_preserve_pref, raw):
    """空串是"没填过"，按开启处理。"""
    _preserve_pref.setValue(M._UPDATE_ON_START_KEY, raw)
    assert M._load_update_prefs()["on_start"] is True


# ---------------------------------------------------------------- 检查频率

def test_interval_default_is_startup(_preserve_pref):
    _preserve_pref.remove(M._UPDATE_INTERVAL_KEY)
    assert M._load_update_prefs()["interval"] == "startup"


def test_interval_round_trips(_preserve_pref):
    M._save_update_pref(M._UPDATE_INTERVAL_KEY, "weekly")
    assert M._load_update_prefs()["interval"] == "weekly"


def test_bogus_interval_falls_back_to_startup(_preserve_pref):
    _preserve_pref.setValue(M._UPDATE_INTERVAL_KEY, "fortnightly")
    assert M._load_update_prefs()["interval"] == "startup"


def test_startup_interval_is_always_due(_preserve_pref):
    _preserve_pref.setValue(M._UPDATE_LAST_KEY, str(int(M.time.time())))
    assert M._update_due({"interval": "startup"}) is True


def test_daily_interval_skips_when_checked_recently(_preserve_pref):
    _preserve_pref.setValue(M._UPDATE_LAST_KEY, str(int(M.time.time()) - 60))
    assert M._update_due({"interval": "daily"}) is False


def test_daily_interval_is_due_after_a_day(_preserve_pref):
    _preserve_pref.setValue(M._UPDATE_LAST_KEY,
                            str(int(M.time.time()) - 25 * 3600))
    assert M._update_due({"interval": "daily"}) is True


def test_weekly_interval_is_due_after_a_week(_preserve_pref):
    _preserve_pref.setValue(M._UPDATE_LAST_KEY,
                            str(int(M.time.time()) - 8 * 24 * 3600))
    assert M._update_due({"interval": "weekly"}) is True


def test_missing_timestamp_counts_as_due(_preserve_pref):
    _preserve_pref.remove(M._UPDATE_LAST_KEY)
    assert M._update_due({"interval": "weekly"}) is True

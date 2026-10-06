# -*- coding: utf-8 -*-
"""``i18n`` 的核心不变量。

这个模块**零 Qt**，所以这里也不需要 ``qapp`` —— 全是纯函数与表。要 Qt 的那部分
（选项按钮、勾选回显、切换后界面重排）在 ``test_settings_dialog.py`` 里。
"""

from __future__ import annotations

import pytest

import i18n

#: 表里的键就是中文原文本身。挑一条一定会被翻的当样本 —— 别用简称，比如「悔棋」
#: 并不存在，真正的键是带图标的 ``"↩ 悔棋"``。
KEY = "↩ 悔棋"


@pytest.fixture(autouse=True)
def _restore_language():
    """每个用例自己折腾语言，但别把状态漏给下一个用例。

    ``i18n`` 的当前语言是模块级全局，而 ``conftest.py`` 那个钉语言的夹具是
    session 级的 —— 它只在会话开头跑一次。于是本文件里任何一个 ``set_language``
    都会顺流到**后面所有文件**的用例去，让它们在一门没预期的语言下断言中文文案。
    """
    keep = (i18n.language(), i18n._system_locale)
    yield
    i18n.set_system_locale(keep[1])
    i18n.set_language(keep[0] if keep[0] in i18n.CHOICES else i18n.SYSTEM)
    # ``enable_miss_log()`` 没有对应的关闭开关（实际用法是一次性命令行工具），
    # 开了就一直开着。用例之间必须自己掐掉，否则上一条记下的漏译会漏进下一条。
    i18n._misses = None


# ---------------------------------------------------------------------------
# 语言清单与自名
# ---------------------------------------------------------------------------

def test_six_languages_plus_system():
    assert i18n.LANGUAGES == ("zh-CN", "zh-TW", "en", "ru", "ja", "ko")
    assert i18n.CHOICES == (i18n.SYSTEM,) + i18n.LANGUAGES
    assert i18n.DEFAULT == "zh-CN"


def test_every_language_has_a_self_name():
    """一个只会日语的人得在选项里认出「日本語」，所以自名不能缺、不能是代码。"""
    for code in i18n.LANGUAGES:
        name = i18n.LANGUAGE_NAME[code]
        assert name and name != code


def test_the_four_per_language_maps_agree():
    """四张按语言索引的表必须覆盖同一批语言 —— 少一张就有一门语言回落到中文。"""
    for table in (i18n.LANGUAGE_NAME, i18n.LANGUAGE_LABEL,
                  i18n.FOLLOW_SYSTEM, i18n.LANGUAGE_HINT):
        assert set(table) == set(i18n.LANGUAGES)


def test_language_name_of_the_system_choice_follows_the_system():
    """「跟随系统」不是一个语言，它用**系统**语言书写，与应用内语言无关。

    用户 2026-10-07 报的 bug：选了俄语之后这一项变成「Как в системе」——
    它描述的明明是系统那边。这里把"应用内语言再改它也不动"钉死。
    """
    i18n.set_system_locale("ja_JP")
    i18n.set_language("ru")
    assert i18n.language_name(i18n.SYSTEM) == "システムに従う"
    i18n.set_language("en")
    assert i18n.language_name(i18n.SYSTEM) == "システムに従う"
    i18n.set_system_locale("en_US")
    assert i18n.language_name(i18n.SYSTEM) == "Use system language"
    # 系统语言不在六种里（法语）→ 与界面本身的回退同一档：英语。
    i18n.set_system_locale("fr_FR")
    assert i18n.language_name(i18n.SYSTEM) == "Use system language"


def test_language_name_of_a_real_language_does_not_follow():
    """真实语言按钮上写的是**自名**：在俄语界面里「日本語」还是「日本語」。"""
    i18n.set_language("ru")
    assert i18n.language_name("ja") == "日本語"
    assert i18n.language_name("zh-CN") == "中文（简体）"


# ---------------------------------------------------------------------------
# 系统语言映射
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("locale, expected", [
    ("zh_CN", "zh-CN"),
    ("zh_SG", "zh-CN"),
    ("zh", "zh-CN"),
    ("zh_TW", "zh-TW"),
    ("zh_HK", "zh-TW"),
    ("zh_MO", "zh-TW"),
    ("en_GB", "en"),
    ("ru_RU", "ru"),
    ("ja_JP", "ja"),
    ("ko_KR", "ko"),
])
def test_system_locale_mapping(locale, expected):
    assert i18n._normalize(locale) == expected


@pytest.mark.parametrize("locale", ["de_DE", "fr", "pt_BR", "", "xx"])
def test_unknown_system_locale_falls_back_to_english(locale):
    """认不出来一律回英语。

    界面做出来是给不认识中文的人用的 —— 落回中文等于这门功能没做。空串是另一种
    情形（界面层还没调 ``set_system_locale``），那个由 ``_resolve`` 兜到默认。
    """
    if locale == "":
        assert i18n._normalize(locale) == i18n.DEFAULT
    else:
        assert i18n._normalize(locale) == "en"


def test_locale_separator_and_case_are_ignored():
    """``QLocale.system().name()`` 给的是 ``zh_CN``，但别的地方可能写 ``zh-CN``。"""
    assert i18n._normalize("ZH-tw") == "zh-TW"
    assert i18n._normalize("zh-CN") == "zh-CN"
    assert i18n._normalize("RU_RU") == "ru"


# ---------------------------------------------------------------------------
# 选择的解析
# ---------------------------------------------------------------------------

def test_selecting_a_language_resolves_to_itself():
    i18n.set_system_locale("ja_JP")
    i18n.set_language("ru")
    assert i18n.language() == "ru"
    assert i18n.resolved() == "ru"


def test_follow_system_resolves_through_the_system_locale():
    i18n.set_language(i18n.SYSTEM)
    i18n.set_system_locale("zh_TW")
    assert i18n.resolved() == "zh-TW"
    i18n.set_system_locale("ko_KR")
    assert i18n.resolved() == "ko"


def test_follow_system_without_a_locale_yet_is_the_default():
    i18n.set_system_locale("")
    i18n.set_language(i18n.SYSTEM)
    assert i18n.resolved() == i18n.DEFAULT


def test_bogus_choice_is_treated_as_follow_system():
    """非法值不抛异常 —— 与其让界面开不了，不如回退。"""
    i18n.set_system_locale("ja_JP")
    i18n.set_language("klingon")
    assert i18n.language() == i18n.SYSTEM
    assert i18n.resolved() == "ja"


# ---------------------------------------------------------------------------
# 查表与回退
# ---------------------------------------------------------------------------

def test_simplified_chinese_is_the_identity():
    """简体中文是原文，不需要表 —— 查不到就原样返回，结果一样。"""
    i18n.set_language("zh-CN")
    assert i18n.t(KEY) == KEY
    assert i18n.t("从来没见过的一条") == "从来没见过的一条"


def test_missing_key_falls_back_to_the_chinese_source():
    i18n.set_language("en")
    assert i18n.t("这条一定不在表里") == "这条一定不在表里"


def test_a_real_key_translates():
    for code in i18n.LANGUAGES:
        if code == i18n.DEFAULT:
            continue
        i18n.set_language(code)
        table = i18n._STRINGS[code]
        for key, want in table.items():
            assert i18n.t(key) == want, f"{code}: {key!r}"


# ---------------------------------------------------------------------------
# _Done：一条文字只查一次
# ---------------------------------------------------------------------------

def test_done_is_a_str_subclass_so_it_still_works_as_a_label_text():
    i18n.set_language("en")
    got = i18n.t(KEY)
    assert isinstance(got, str)
    assert isinstance(got, i18n._Done)


def test_done_short_circuits_the_second_lookup():
    """``t(t(x))`` 与 ``t(x)`` 必须一模一样 —— 工厂再过一遍手不该改变结果。"""
    i18n.set_language("en")
    once = i18n.t(KEY)
    assert i18n.t(once) == once


def test_done_does_not_get_recorded_as_a_miss():
    i18n.set_language("en")
    i18n.enable_miss_log()
    i18n.t(i18n.t(KEY))
    assert i18n.miss_log() == []


def test_formatted_string_is_marked_done_even_though_percent_demotes_it():
    """``%`` 会把 ``str`` 子类降回 ``str``，``tf()`` 得自己补回标记。"""
    i18n.set_language("en")
    got = i18n.tf("第 %d 手   %s", 3, "黑棋")
    assert isinstance(got, i18n._Done)
    i18n.enable_miss_log()
    i18n.t(got)
    assert i18n.miss_log() == []


def test_manual_formatting_loses_the_marker_and_that_is_why_tf_exists():
    """手动 ``%`` 会断开标记链，格式化成品与模板不再相等 —— 于是被判成漏译。

    这不是缺陷，是 ``tf()`` 存在的**唯一理由**：``_translated_values`` 比的是整条
    译文，而 ``"Move %d   %s"`` 这条译文与成品 ``"Move 2   白棋"`` 永远不相等。
    谁要自己格式化，就得自己走 ``tf()``。
    """
    i18n.set_language("en")
    i18n.enable_miss_log()
    plain = str(i18n.t("第 %d 手   %s")) % (2, "白棋")
    assert i18n.t(plain) == plain
    assert i18n.miss_log() == [plain]


def test_a_whole_plain_translation_passed_back_is_recognised():
    """不带占位符的那一类：``t()`` 的成品再交回来一次，整条译文比对认得出。"""
    i18n.set_language("en")
    done = i18n.t(KEY)
    i18n.enable_miss_log()
    # 断掉标记链，只留下那串译文本身。
    plain = str(done)
    assert i18n.t(plain) == plain
    assert i18n.miss_log() == []


# ---------------------------------------------------------------------------
# 漏译盘点
# ---------------------------------------------------------------------------

def test_miss_log_is_off_by_default():
    i18n.set_language("en")
    i18n.t("一条绝对不在表里的中文")
    assert i18n.miss_log() == []


def test_only_cjk_bearing_strings_are_recorded():
    """盘点里只该出现"待翻的文案"，不该是数据。

    计时的 "00:00"、占位符 "—"、齿轮 "⚙"、还有本来就不该翻的 "Gomoku AI"，
    每次刷新都会路过 ``t()``；不滤掉的话真漏译会被它们埋掉。
    """
    i18n.set_language("en")
    i18n.enable_miss_log()
    for data in ("00:00", "3", "—", "⚙", "Gomoku AI", "●", "AI"):
        i18n.t(data)
    assert i18n.miss_log() == []

    i18n.t("这条是真漏译了")
    assert i18n.miss_log() == ["这条是真漏译了"]


def test_miss_log_is_deduplicated_and_sorted():
    i18n.set_language("en")
    i18n.enable_miss_log()
    for text in ("乙条目", "甲条目", "乙条目"):
        i18n.t(text)
    assert i18n.miss_log() == sorted(["甲条目", "乙条目"])


# ---------------------------------------------------------------------------
# 表的完整性
# ---------------------------------------------------------------------------

def test_simplified_chinese_has_no_table_at_all():
    """简体中文是原文，它的表**是空的** —— 不是"键的出处"。

    这条钉着一个容易写错的前提：``_STRINGS[DEFAULT]`` 空表意味着"六张表键集一致"
    这件事只能在另外五门语言之间比。把 zh-CN 拉进来比会立刻炸。
    """
    assert i18n._STRINGS[i18n.DEFAULT] == {}


def test_every_translated_language_has_the_same_key_set():
    """五张译文表的键集必须一致，否则某门语言会静默缺字。"""
    others = [c for c in i18n.LANGUAGES if c != i18n.DEFAULT]
    keys = set(i18n._STRINGS[others[0]])
    assert keys, "译文表是空的，测试的前提没了"
    for code in others[1:]:
        missing = keys - set(i18n._STRINGS[code])
        extra = set(i18n._STRINGS[code]) - keys
        assert not missing, f"{code} 少了 {sorted(missing)[:5]}"
        assert not extra, f"{code} 多了 {sorted(extra)[:5]}"


def test_keys_are_the_chinese_source_strings():
    """键就是中文原文本身 —— 这是这套方案能"查不到就原样返回"的前提。"""
    for code in i18n.LANGUAGES:
        for key in i18n._STRINGS[code]:
            assert i18n._CJK.search(key), f"{code}: 键 {key!r} 里没有汉字，不像是界面文案"


def test_no_translation_is_empty():
    for code in i18n.LANGUAGES:
        for key, value in i18n._STRINGS[code].items():
            assert value, f"{code} 的 {key!r} 译成了空串"


def test_placeholder_shapes_survive_translation():
    """``%s`` / ``%d`` / ``%g`` 的个数与种类不能在后半段被改掉，否则格式化会炸。"""
    import re
    pat = re.compile(r"%[sdg]")
    for code in i18n.LANGUAGES:
        for key, value in i18n._STRINGS[code].items():
            assert sorted(pat.findall(key)) == sorted(pat.findall(value)), \
                f"{code} 的 {key!r} → {value!r} 占位符对不上"

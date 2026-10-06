# -*- coding: utf-8 -*-
"""长语言（俄语）下的界面裁切回归（需要 QApplication）。

2026-10-06 的功能分支上，俄语等长语言会把若干文本挤出控件边界 ——
选择卡片、对局面板、复盘列表、局域网页与结算遮罩都中过招。根因是这些
文案是**构造期**写进控件的、而布局里有一批"按中文口径写死的宽度"。

修法是让宽度/折行跟着当前语言与字体的**内容需求**走，这里逐处钉住：

1. 卡片：最长的不可断单词放得进卡宽，多词副标题折行显示完整；
2. 面板：宽度按运行期值域（难度名、回合状态）预热，值变宽也不裁；
3. 按钮：``width=`` 是下限而不是定宽，长译文自动撑开；
4. 复盘列表与局域网表单：列宽/标签宽按内容算。

判定与 ``tools/i18n_inventory.py`` 的目检口径一致：非 wrap 控件按
``sizeHint`` 宽，wrap 控件按"最长不可断单元 + 折行高度" —— 换行显示完整
算通过，省略号/裁剪不算。

**不继承开发者机器上的偏好**：语言由本文件的夹具自己还原（``conftest``
的 `_neutral_ui_language` 只在会话开头钉一次），字号由 `_neutral_ui_scale`
钉在 normal。
"""

from __future__ import annotations

import os
import re

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PyQt5.QtGui import QFontMetrics
from PyQt5.QtWidgets import QAbstractButton, QApplication, QLabel, QWidget

import engine
import i18n
import main as M
import theme

_CJK = re.compile(r"[\u3000-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
                  r"\uac00-\ud7af\uff00-\uffef]")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _restore_language():
    """本文件的用例自己切语言，用完还原 —— 别漏给后面的用例。

    与 ``tests/test_i18n.py`` 的夹具同构：``conftest`` 那个钉语言的夹具是
    session 级的，而这里每次 ``set_language`` 都会顺流到后续测试文件。
    """
    keep = (i18n.language(), i18n.resolved())
    yield
    i18n.set_language(keep[0] if keep[0] in i18n.CHOICES else i18n.SYSTEM)


@pytest.fixture(autouse=True)
def _installed_theme():
    """确保全局 QSS 已装 —— 度量断言依赖 role 选择器里的字号。"""
    theme.install()


def _pump(qapp, n=20):
    for _ in range(n):
        qapp.processEvents()


def _longest_unit(text, fm):
    """最长不可断单元：CJK 单字可断；其余按空白拆成单词。"""
    best = 0
    for tok in re.split(r"\s+", text.strip()):
        if not tok:
            continue
        if _CJK.search(tok):
            run = ""
            for ch in tok:
                if _CJK.match(ch):
                    best = max(best, fm.horizontalAdvance(run))
                    best = max(best, fm.horizontalAdvance(ch))
                    run = ""
                else:
                    run += ch
            best = max(best, fm.horizontalAdvance(run))
        else:
            best = max(best, fm.horizontalAdvance(tok))
    return best


def _clipped(root):
    """列出实际显示不下的文本控件 ``(控件, 原因, 需要宽度, 实际)``。"""
    bad = []
    for w in root.findChildren(QWidget):
        if isinstance(w, QAbstractButton):
            txt = w.text()
            if not txt:
                continue
            if w.sizeHint().width() > w.width() + 1:
                bad.append((w, "button", w.sizeHint().width(), w.width()))
            continue
        if not isinstance(w, QLabel):
            continue
        txt = w.text()
        if not txt:
            continue
        if w.wordWrap():
            unit = _longest_unit(txt, QFontMetrics(w.font()))
            if unit > w.width() + 1:
                bad.append((w, "wrap-word", unit, w.width()))
            elif w.heightForWidth(w.width()) > w.height() + 1:
                bad.append((w, "wrap-height",
                            w.heightForWidth(w.width()), w.height()))
            continue
        if w.sizeHint().width() > w.width() + 1:
            bad.append((w, "single", w.sizeHint().width(), w.width()))
    return bad


def _assert_fits(root, tag):
    bad = _clipped(root)
    report = "\n".join(
        "%s: %r 需要 %d / 实际 %d" % (kind, (w.text()[:60]), need, got)
        for w, kind, need, got in bad)
    assert not bad, "[%s] 仍有 %d 处裁切：\n%s" % (tag, len(bad), report)


def _show(widget, qapp, size=None):
    if size is not None:
        widget.resize(*size)
    widget.show()
    _pump(qapp)


# ==================== 1. 选择页卡片 ====================

@pytest.mark.parametrize("mode", ["mode", "color", "difficulty", "review"])
def test_selection_cards_fit_in_russian(mode, qapp):
    """卡片标题/副标题在俄语下完整显示（单词不裁、折行高度够）。

    曾经：「Гроссмейстер」在 149px 的难度卡里被切掉一截；修法是按单词宽
    放大卡片并在放不下时折行（``_fit_card_row``）。
    """
    i18n.set_language("ru")
    page = M.SelectionScreen(mode=mode, min_level=3)
    try:
        _show(page, qapp, M._design_size())
        _assert_fits(page, "SelectionScreen/" + mode)
    finally:
        page.deleteLater()
        _pump(qapp, 5)


def test_russian_difficulty_cards_wrap_to_two_rows(qapp):
    """俄语难度页 6 张卡一行排不下时折行，而不是把卡片压到文字之下。

    这里钉住"折行发生了"：单行 6×192 放不进 974 的可用宽度，至少有两行。
    """
    from PyQt5.QtCore import QPoint
    i18n.set_language("ru")
    page = M.SelectionScreen(mode="difficulty")
    try:
        _show(page, qapp, M._design_size())
        # 不写死像素（Windows/Linux 字体度量不同）：先按本机字体把卡片内容
        # 的"最长不可断单元 + 边距"算一遍，只有一行真的排不下时才要求折行。
        _assert_fits(page, "difficulty/ru")
        cards = page._cards
        unit = max(
            _longest_unit(lbl.text(), QFontMetrics(lbl.font()))
            + 2 * theme.SPACE_MD
            for card in cards for lbl in card.findChildren(QLabel)
            if lbl.property("role") in ("card-text", "card-sub"))
        gap = theme.SPACE_LG
        if len(cards) * unit + (len(cards) - 1) * gap > M._page_content_w():
            ys = {c.mapTo(page, QPoint(0, 0)).y() for c in cards}
            assert len(ys) >= 2, "一行排不下却没有折行"
        # 同一组卡片必须等宽等高（观感约束，与字体无关）
        assert len({(c.width(), c.height()) for c in cards}) == 1
    finally:
        page.deleteLater()
        _pump(qapp, 5)


def test_chinese_cards_keep_the_classic_geometry(qapp):
    """中文卡片尺寸/排布与改动前一致：难度卡一行 6 张、边长 149。

    "长语言修好了，中文却变胖/变高"是这次最该防的回归。
    """
    i18n.set_language("zh-CN")
    page = M.SelectionScreen(mode="difficulty")
    try:
        _show(page, qapp, M._design_size())
        sizes = {(c.width(), c.height()) for c in page._cards}
        assert len({c.y() for c in page._cards}) == 1, "中文卡片不该折行"
        assert sizes == {(149, 149)}, "中文难度卡尺寸变了：%s" % (sizes,)
    finally:
        page.deleteLater()
        _pump(qapp, 5)


# ==================== 2. 对局面板 ====================

def test_game_panel_fits_in_russian_with_worst_case_values(qapp):
    """面板按**运行期值域**定宽：难度值「Гроссмейстер」、状态文字都不裁。

    曾经面板死守 ``theme.PANEL_W``=264，「✕ Выйти из игры」（272px）与宽
    难度值都会把控件挤出边界。
    """
    i18n.set_language("ru")
    panel = M.GamePanel()
    try:
        # 高度给足设计尺寸：面板在窄环境里被压扁时，wrap 文本会被压到
        # 最小高度以下，那是"窗口太矮"而不是"文案摆不下"。
        _show(panel, qapp, M._design_size())
        panel.difficulty_row.set_value(
            i18n.t(engine.difficulty_name(5)))       # 宗师：最长难度名
        panel.turn_indicator.set_thinking(2)         # 「AI думает…」
        panel.score_chart.set_readout(
            "d7 12.3k 2.10M/s 340ms")
        _pump(qapp)
        _assert_fits(panel, "GamePanel/ru")
        assert panel.width() > theme.PANEL_W, "俄语面板没有按内容需求加宽"
    finally:
        panel.deleteLater()
        _pump(qapp, 5)


def test_panel_width_is_derived_not_magic(qapp):
    """面板宽度由内容推导：中文=基准 264，俄语更宽且能装下最宽按钮。"""
    for lang, expect_grow in (("zh-CN", False), ("ru", True)):
        i18n.set_language(lang)
        panel = M.GamePanel()
        try:
            _show(panel, qapp)
            if not expect_grow:
                assert panel.width() == theme.PANEL_W
            else:
                assert panel.width() >= panel.quit_btn.sizeHint().width() \
                    + 2 * theme.SPACE_XL
        finally:
            panel.deleteLater()
            _pump(qapp, 5)


# ==================== 3. 结算遮罩 ====================

def test_game_over_overlay_buttons_fit_in_russian(qapp):
    """遮罩的按钮、结果文字不裁：按钮的 ``width=`` 现在是下限。"""
    i18n.set_language("ru")
    overlay = M.GameOverOverlay("你赢了！", True, can_review=True)
    try:
        _show(overlay, qapp, M._design_size())
        _assert_fits(overlay, "GameOverOverlay/ru")
    finally:
        overlay.deleteLater()
        _pump(qapp, 5)


# ==================== 4. 复盘与局域网页 ====================

@pytest.fixture
def review_records():
    before = np.zeros((19, 19), dtype=int)
    before[9][9] = 1
    before[8][8] = 2
    return [
        {"seq": 1, "played": (9, 9), "best": None, "delta": None,
         "offboard": False, "mate": False, "empty": True, "before": before},
        {"seq": 3, "played": (8, 7), "best": (10, 10), "delta": 123.45,
         "offboard": False, "mate": False, "empty": False, "before": before},
        {"seq": 18, "played": (0, 0), "best": (1, 1), "delta": 1.0,
         "offboard": True, "mate": False, "empty": False, "before": before},
    ]


def test_review_list_fits_in_russian(review_records, qapp):
    """复盘列表按内容/可用宽度算：行文本与「Отображение партии」按钮都不压。"""
    i18n.set_language("ru")
    page = M.ReviewScreen(review_records, 3)
    try:
        _show(page, qapp, M._design_size())
        _assert_fits(page, "ReviewScreen/ru")
    finally:
        page.deleteLater()
        _pump(qapp, 5)


def test_review_progress_and_board_fit_in_russian(review_records, qapp):
    """复盘进度页与棋盘页的按钮（「✕ Отменить разбор」等）不裁。"""
    i18n.set_language("ru")
    progress = M.ReviewProgressScreen(3, 3)
    board = M.ReviewBoardScreen(review_records[1])
    try:
        _show(progress, qapp, M._design_size())
        _show(board, qapp, M._design_size())
        _assert_fits(progress, "ReviewProgress/ru")
        _assert_fits(board, "ReviewBoard/ru")
    finally:
        progress.deleteLater()
        board.deleteLater()
        _pump(qapp, 5)


def test_lan_screens_fit_in_russian(qapp):
    """局域网三屏：表单标签（「IP хоста」）与按钮都按内容宽。"""
    i18n.set_language("ru")
    menu = M.LanMenuScreen()
    join = M.LanJoinScreen()
    wait = M.LanWaitScreen("192.168.1.7", 51234, 1)
    try:
        _show(menu, qapp, M._design_size())
        _show(join, qapp, M._design_size())
        _show(wait, qapp, M._design_size())
        _assert_fits(menu, "LanMenu/ru")
        _assert_fits(join, "LanJoin/ru")
        _assert_fits(wait, "LanWait/ru")
    finally:
        menu.deleteLater()
        join.deleteLater()
        wait.deleteLater()
        _pump(qapp, 5)


# ==================== 5. 加载页字体警告 ====================

def test_loading_screen_fits_even_with_font_warning(qapp):
    """加载页：没有中文字体时那行长警告折行显示完整（有字体时无此控件）。"""
    i18n.set_language("ru")
    splash = M.LoadingScreen(lambda: None)
    try:
        _show(splash, qapp, M._design_size())
        _assert_fits(splash, "Loading/ru")
    finally:
        splash.deleteLater()
        _pump(qapp, 5)

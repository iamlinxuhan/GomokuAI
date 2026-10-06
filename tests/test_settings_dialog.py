# -*- coding: utf-8 -*-
"""设置弹窗化 + 自定义难度卡片（界面侧，需要 QApplication）。

本轮把「设置」从 `central` 里的一整页改成模态弹窗，改动落点是几条**用户可见的
契约**，逐条钉在这里：

1. **弹窗期间底下那一页纹丝不动。** 这是弹窗相对整页的**唯一**实际收益 ——
   从前"进设置"要切页，"出设置"要按 `_settings_origin` 原路返回。现在 `central`
   的当前页在弹窗前后必须是同一个对象。
2. **关窗时按新档位重建底下那一页。** 档位是构造期算死的度量，不重建就是
   "字号变了、布局还是旧的"。而没换过档位时**不该**重建 —— 白白重建一次会
   把对局的动画/图表序列打断。
3. **齿轮只在加载页隐藏。** 以前还要为"设置页自己"藏一次，现在那一页不存在了。
4. **「自定义」卡只出现在难度页**，复盘强度页仍然只有五张。这一条是哨兵方案
   （`engine.CUSTOM_LEVEL` 不进 `DIFFICULTY`）存在的理由，也是方案 A 会踩的坑：
   复盘页按 `level >= min_level` 过滤，6 是最大值，于是每一局的复盘页都会多出
   一张「自定义」卡。
5. **自定义参数要点「确定」才生效**，「取消」什么都不动；改完要落盘。

必须走 QApplication。设置弹窗用 ``QDialog.open()`` 而非 ``exec_()``，正是为了
让这里能推进事件循环并直接 emit 信号。
"""

from __future__ import annotations

import io
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtCore import QSettings
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import QApplication

import engine
import i18n
import main as M
import theme

_REAL_LOGGER = M.GameLogger


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class _DummyLogger:
    """替身：不往项目根目录落 game_log_*.txt。"""

    coord_to_sgf = staticmethod(_REAL_LOGGER.coord_to_sgf)

    def __init__(self):
        self.filepath = "<memory>"
        self.f = io.StringIO()

    def close(self):
        self.f.close()

    def log_human(self, *a, **k):
        pass

    def log_ai(self, *a, **k):
        pass

    def log_board_state(self, *a, **k):
        pass

    def log_result(self, *a, **k):
        pass


@pytest.fixture(autouse=True)
def _clean_prefs(qapp):
    """测试不污染用户的档位与自定义难度，也不让它们泄漏给别的用例。"""
    s = QSettings(theme._SETTINGS_ORG, theme._SETTINGS_APP)
    old_scale = s.value(theme._SETTINGS_SCALE_KEY, None)
    old_custom = s.value(M._SETTINGS_CUSTOM_KEY, None)
    engine.reset_custom_config()
    yield
    for key, old in ((theme._SETTINGS_SCALE_KEY, old_scale),
                     (M._SETTINGS_CUSTOM_KEY, old_custom)):
        if old is None:
            s.remove(key)
        else:
            s.setValue(key, old)
    engine.reset_custom_config()
    theme.set_scale("normal", persist=False)


@pytest.fixture
def win(monkeypatch, qapp):
    monkeypatch.setattr(M, "GameLogger", _DummyLogger)
    w = M.GomokuGame()
    w.show()
    qapp.processEvents()
    yield w
    w._on_quit()
    qapp.processEvents()
    w.deleteLater()
    qapp.processEvents()


def _pump(qapp, n=5):
    for _ in range(n):
        qapp.processEvents()


def _card_text(card):
    """卡片上的主文案。

    ``card_button`` 是个空 ``QPushButton``，文字在子 ``QLabel``（``role=card-text``）
    里 —— 图形、标题、副标题三者靠布局上下排，按钮自身的 ``text()`` 是空的。
    """
    from PyQt5.QtWidgets import QLabel
    for lbl in card.findChildren(QLabel):
        if lbl.property("role") == "card-text":
            return lbl.text()
    return None


# ==================== 1/2/3：弹窗与齿轮 ====================

def test_open_settings_dialog_keeps_the_underlying_page(win, qapp):
    """开弹窗**不切页**，关掉之后底下还是原来那一页。"""
    win._on_mode_selected(1)          # 本地双人 → 直接进对局页（绕过难度页）
    _pump(qapp)
    page = win.central.currentWidget()
    assert page is win.game_widget

    win._open_settings_dialog()
    _pump(qapp)
    assert win.settings_dialog is not None, "齿轮没把设置弹窗打开"
    assert win.central.currentWidget() is page, "弹窗期间切了页"

    win.settings_dialog.close()
    _pump(qapp)
    assert win.settings_dialog is None, "关窗后没撤引用"


def test_settings_dialog_exposes_the_same_signals(win, qapp):
    """三个信号名沿用旧设置页 —— 工具的驱动方式只换对象名，不换语义。"""
    win._open_settings_dialog()
    _pump(qapp)
    d = win.settings_dialog
    for name in ("scale_selected", "theme_toggled", "back_clicked"):
        assert hasattr(d, name), name
    d.close()
    _pump(qapp)


def test_scale_change_rebuilds_the_page_on_close(win, qapp):
    """弹窗里换档 → 关窗时底下那一页按新度量重建，且当前页指向新对象。"""
    win._on_mode_selected(1)
    _pump(qapp)
    old_page = win.central.currentWidget()

    win._open_settings_dialog()
    _pump(qapp)
    other = "large" if theme.current_scale() != "large" else "small"
    win.settings_dialog.scale_selected.emit(other)
    _pump(qapp)
    assert theme.current_scale() == other

    win.settings_dialog.close()
    _pump(qapp)
    new_page = win.central.currentWidget()
    assert new_page is not old_page, "换了档却没重建底下那一页"
    assert win.central.currentWidget() is new_page, "重建后没把当前页指过去"


def test_close_without_scale_change_keeps_the_same_page(win, qapp):
    """没换档就不该重建 —— 重建会打断对局的动画与图表序列。"""
    win._on_mode_selected(1)
    _pump(qapp)
    old_page = win.central.currentWidget()

    win._open_settings_dialog()
    _pump(qapp)
    win.settings_dialog.close()
    _pump(qapp)
    assert win.central.currentWidget() is old_page


def test_gear_is_hidden_only_on_the_loading_screen(win, qapp):
    """齿轮只在加载页隐藏。"""
    win._init_loading()
    _pump(qapp)
    assert not win.settings_btn.isVisible()

    win._on_mode_selected(1)
    _pump(qapp)
    assert win.settings_btn.isVisible()


# ==================== 4：自定义卡只出现在难度页 ====================

def test_custom_card_only_on_the_difficulty_page(qapp):
    """难度页六张卡（五档 + 自定义），复盘页仍然五张。

    ``SelectionScreen`` 的卡片表来自 ``engine.DIFFICULTY``，而哨兵档**不在**表里
    —— 自定义卡是唯一一张单独追加的。复盘页按 ``level >= min_level`` 过滤，若把
    哨兵塞进 ``DIFFICULTY``，这里会变成 6。
    """
    difficulty = M.SelectionScreen(mode="difficulty")
    review = M.SelectionScreen(mode="review", min_level=1)
    try:
        assert len(difficulty._cards) == len(engine.DIFFICULTY) + 1
        assert len(review._cards) == len(engine.DIFFICULTY)
        assert _card_text(difficulty._cards[-1]) == "自定义"
    finally:
        difficulty.deleteLater()
        review.deleteLater()


def test_custom_card_emits_the_sentinel_level(qapp):
    """点第 6 张卡发的是 ``difficulty_selected(CUSTOM_LEVEL)``，复用既有信号。"""
    page = M.SelectionScreen(mode="difficulty")
    seen = []
    page.difficulty_selected.connect(seen.append)
    try:
        page._cards[-1].click()
        assert seen == [engine.CUSTOM_LEVEL]
    finally:
        page.deleteLater()


# ==================== 5：确定才生效、取消不动 ====================

def test_choosing_custom_opens_dialog_without_touching_the_engine(win, qapp):
    """点「自定义」只开弹窗：这一刻引擎里的配置一个字都没动。"""
    win._show_difficulty_selection()
    _pump(qapp)
    before = engine.custom_config()

    win._on_difficulty_selected(engine.CUSTOM_LEVEL)
    _pump(qapp)
    assert win.custom_dialog is not None
    assert engine.custom_config() == before
    assert win.gamekunnan != engine.CUSTOM_LEVEL, "还没确定就换了档"
    win.custom_dialog.reject()
    _pump(qapp)


def test_cancel_leaves_everything_alone(win, qapp):
    """取消：不写引擎、不落盘、不开局，退回难度页。"""
    win._show_difficulty_selection()
    _pump(qapp)
    before = engine.custom_config()
    started = []
    win._start_game = lambda: started.append(True)

    win._on_difficulty_selected(engine.CUSTOM_LEVEL)
    _pump(qapp)
    win.custom_dialog.reject()
    _pump(qapp)

    assert engine.custom_config() == before
    assert not started, "取消不该开局"
    assert win.central.currentWidget() is win.selection_difficulty


def test_accept_writes_the_config_and_snapshots_it_for_this_game(win, qapp):
    """确定：写引擎、落盘、把这份参数快照到本局，然后才开局。"""
    win._show_difficulty_selection()
    _pump(qapp)
    started = []
    win._start_game = lambda: started.append(True)

    win._on_difficulty_selected(engine.CUSTOM_LEVEL)
    _pump(qapp)
    cfg = dict(time=5.0, max_depth=9, vcf_budget=0.5, qply=4, enhance=False)
    win.custom_dialog.chosen = cfg
    win.custom_dialog.accept()
    _pump(qapp)

    assert started == [True]
    assert win.gamekunnan == engine.CUSTOM_LEVEL
    # 引擎那份是**归一后**的（name 固定、bias 清空），本局快照是用户选的原始值。
    assert engine.custom_config()["time"] == 5.0
    assert engine.custom_config()["name"] == "自定义"
    assert win.game_custom_cfg["max_depth"] == 9
    # 落盘：下次启动读回来还是这一份。
    raw = QSettings(theme._SETTINGS_ORG,
                    theme._SETTINGS_APP).value(M._SETTINGS_CUSTOM_KEY, None)
    assert raw and '"max_depth": 9' in raw


def test_preset_level_clears_the_snapshot(win, qapp):
    """选了预设档，上一局的自定义快照必须作废 —— 否则复盘会拿错参数。"""
    win.game_custom_cfg = dict(time=1.0)
    started = []
    win._start_game = lambda: started.append(True)
    win._on_difficulty_selected(3)
    assert win.game_custom_cfg is None
    assert started == [True]


# ==================== 自定义局的复盘直通 ====================

def test_custom_review_skips_the_strength_page(win, qapp, monkeypatch):
    """自定义局的复盘**跳过强度页**，直接把快照参数交给复盘线程。

    强度页的卡片表只装五档，``min_level=6`` 会过滤出一张空页 —— 方案 A（把哨兵
    写进 ``DIFFICULTY``）会在用户可见的地方踩这个坑。
    """
    win.gamekunnan = engine.CUSTOM_LEVEL
    win.game_custom_cfg = dict(time=5.0, max_depth=9, vcf_budget=0.5, qply=4,
                               enhance=False)
    win.gamemode = 0
    win.human_moves = []

    captured = {}
    # 只用 __init__ 的入参做断言，真线程与它的信号一并顶掉。
    monkeypatch.setattr(M, "ReviewWorker", _make_stub(captured))
    win._show_review_strength()
    _pump(qapp)

    assert captured["level"] == engine.CUSTOM_LEVEL
    assert captured["cfg"]["max_depth"] == 9
    assert win.review_strength is None, "自定义局不该渲染强度页"


def _make_stub(captured):
    """造一个 ``ReviewWorker`` 替身类：只记参数，不真的开线程。"""
    from PyQt5.QtCore import QObject, pyqtSignal

    class _Stub(QObject):
        progressed = pyqtSignal(int, int)
        completed = pyqtSignal(object)

        def __init__(self, moves, human, level, cfg=None):
            super().__init__()
            captured["level"] = level
            captured["cfg"] = cfg

        def start(self):
            pass

        def cancel(self):
            pass

        def isRunning(self):
            """收尾路径（``_cancel_review``）会问它 —— 替身没线程，永远在跑完态。"""
            return False

        def wait(self, _ms=0):
            return True

        @property
        def cancelled(self):
            return False

    return _Stub


# ==================== 4：滑杆行与语言行（本轮新增的两种控件）====================

@pytest.fixture
def _lang_restore():
    """让语言行相关的用例改完语言（与系统语言）能还原，别漏给别的用例。

    ``conftest.py`` 里那个钉语言的夹具是 session 级的，只在会话开头跑一次 ——
    这里改语言会顺流到后面所有文件的用例去；系统语言同理（「跟随系统」的
    文案现在由它决定）。
    """
    import i18n
    keep = (i18n.language(), i18n._system_locale)
    yield i18n
    i18n.set_system_locale(keep[1])
    i18n.set_language(keep[0])


def _slider_row(**kw):
    kw.setdefault("label", "深度上限")
    kw.setdefault("lo", 2)
    kw.setdefault("hi", 40)
    kw.setdefault("unit", "层")
    kw.setdefault("current", 24)
    return M._SliderRow(**kw)


def test_slider_row_uses_the_exact_integer_scale_the_user_asked_for(qapp):
    """步长 1、范围就是用户给的那一段 —— 这是"滑条"相对"一排按钮"的关键。

    旧版把时限砍成 6 个按钮（1/3/7/15/20/30 秒），用户立刻发现选不了 12 秒。
    """
    for label, lo, hi, unit in (("思考时间上限", 10, 60, "秒"),
                                ("深度上限", 2, 40, "层"),
                                ("静止搜索层数", 2, 24, "层")):
        row = _slider_row(label=label, lo=lo, hi=hi, unit=unit, current=lo)
        assert row._slider.minimum() == lo
        assert row._slider.maximum() == hi
        assert row._slider.singleStep() == 1


def test_slider_row_readout_tracks_the_handle(qapp):
    row = _slider_row(current=24)
    assert row._readout.text() == "24 层"
    row._slider.setValue(9)
    assert row._readout.text() == "9 层"
    assert row.value() == 9


def test_slider_row_emits_float_only_where_the_engine_wants_float(qapp):
    """``time`` 那一格引擎里本来就是浮点，其余是整数 —— 出口类型别弄反。"""
    secs = _slider_row(label="思考时间上限", lo=10, hi=60, unit="秒",
                       current=20, to_float=True)
    got = []
    secs.value_selected.connect(got.append)
    secs._slider.setValue(37)
    assert got == [37.0] and isinstance(got[0], float)

    depth = _slider_row(current=24)
    got2 = []
    depth.value_selected.connect(got2.append)
    depth._slider.setValue(11)
    assert got2 == [11] and isinstance(got2[0], int)


def test_slider_row_clamps_out_of_range_values_on_the_way_in(qapp):
    """落盘的配置可能来自旧版本或手改的 conf，进来先夹进刻度里。"""
    row = _slider_row(current=999)
    assert row.value() == 40
    assert row._readout.text() == "40 层"
    assert _slider_row(current=-5).value() == 2


def test_slider_row_set_value_does_not_echo_back(qapp):
    """「恢复默认」是绕过用户交互改配置 —— 改完再发一次 ``value_selected``
    就成了回环：弹窗收到后写回配置、配置又回来 set_value。"""
    row = _slider_row(current=24)
    got = []
    row.value_selected.connect(got.append)
    row.set_value(8)
    assert got == []
    assert row.value() == 8
    assert row._readout.text() == "8 层", "读数行还是要跟上"


def test_slider_row_is_wide_enough_to_be_a_slider(qapp):
    """弹窗内容列用 ``Qt.AlignLeft`` 加控件，那个对齐标志会让行只占 sizeHint
    宽 —— 而 QSlider 的 sizeHint 只有一百多像素，不给下限三条滑杆就都是小短条。"""
    row = _slider_row()
    assert row.minimumWidth() >= M._px(400)


def test_slider_style_actually_reaches_the_widget(qapp):
    """``theme._SLIDER`` 是这一轮**唯一**新增的 QSS，而它踩过两个坑。

    最要命的一个：伪状态写在子控件**前面**时，Qt 会静静地丢掉整条 QSlider
    规则，滑杆长回原生的立体灰槽 —— 不报错、不影响别的控件。这里至少钉住
    "角色属性在、样式表里有对应选择器"这条链路。
    """
    row = _slider_row()
    assert row._slider.property("role") == "value"
    assert 'QSlider[role="value"]' in theme.app_stylesheet()


def test_language_row_offers_all_seven_choices(qapp, _lang_restore):
    row = M._LanguageRow()
    items = [row.combo.itemData(i) for i in range(row.combo.count())]
    assert items == list(i18n.CHOICES)
    assert len(items) == 7


def test_language_row_shows_self_names_and_the_system_label(qapp,
                                                            _lang_restore):
    """自名不随界面语言变；『跟随系统』跟**系统**语言走（2026-10-07 的修复）。"""
    _lang_restore.set_system_locale("ru_RU")
    _lang_restore.set_language("ja")
    row = M._LanguageRow()
    texts = {row.combo.itemData(i): row.combo.itemText(i)
             for i in range(row.combo.count())}
    # 自名：界面是日语，它们也还是各自的语言写法。
    assert texts["ja"] == "日本語"
    assert texts["zh-TW"] == "中文（繁體）"
    # 「跟随系统」：系统是俄语 —— 界面换到哪儿它都写「Как в системе」。
    assert texts[i18n.SYSTEM] == "Как в системе"
    _lang_restore.set_language("ko")
    row2 = M._LanguageRow()
    follow = {row2.combo.itemData(i): row2.combo.itemText(i)
              for i in range(row2.combo.count())}
    assert follow[i18n.SYSTEM] == "Как в системе"


def test_language_row_checks_the_current_choice(qapp, _lang_restore):
    _lang_restore.set_language("ko")
    row = M._LanguageRow()
    assert row.combo.currentData() == "ko"


def test_language_combo_picking_an_item_emits_that_code(qapp, _lang_restore):
    row = M._LanguageRow()
    got = []
    row.language_selected.connect(got.append)
    row.combo.setCurrentIndex(row.combo.findData("zh-TW"))
    assert got == ["zh-TW"]


def test_language_combo_is_never_narrower_than_its_widest_item(
        qapp, _lang_restore):
    """任何语言下，下拉框都要装得下最宽的那条选项（含『跟随系统』）。

    选项文本长度差三倍（"한국어" 对 "Use system language"），宽度由下拉框的
    sizeHint 自己撑开；这条是"没被最小宽度把谁裁掉"的回归。
    """
    for lang in i18n.LANGUAGES + (i18n.SYSTEM,):
        _lang_restore.set_language(lang)
        row = M._LanguageRow()
        row.adjustSize()
        assert row.combo.width() >= row.combo.sizeHint().width(), \
            f"{lang} 界面下语言下拉被裁了"


def test_language_combo_style_and_self_drawn_arrow(qapp, _lang_restore):
    """``theme._COMBO`` 是本轮新增的 QSS；箭头则是自绘的（QSS 画不了三角）。

    离屏平台画不了文字，但图形能画：在 ``::drop-down`` 区正中取一个像素，
    它应当与下拉框底色明显不同 —— 箭头没画上去时那里就是纯底色。
    """
    row = M._LanguageRow()
    assert row.combo.property("role") == "language"
    assert 'QComboBox[role="language"]' in theme.app_stylesheet()
    row.resize(row.sizeHint())
    row.show()
    for _ in range(20):
        qapp.processEvents()
    img = row.combo.grab().toImage()
    # 图像是设备像素；cx/cy 按比例换算，高 DPI 下也对得上。
    cx = img.width() - img.width() * theme.SPACE_XL // (2 * row.combo.width())
    cy = img.height() // 2
    px = img.pixelColor(cx, cy)
    bg = QColor(theme.SURFACE_2)
    dist = (abs(px.red() - bg.red()) + abs(px.green() - bg.green())
            + abs(px.blue() - bg.blue()))
    assert dist > 30, f"下拉箭头没画出来：像素 {px.name()} ≈ 底色 {bg.name()}"
    row.deleteLater()


def test_even_width_takes_the_widest_and_respects_the_floor(qapp):
    from PyQt5.QtWidgets import QPushButton
    bs = [QPushButton(t) for t in ("关", "低", "中", "高",
                                   "Высокий", "Макс")]
    w = M._even_width(bs, M._px(78))
    assert {b.width() for b in bs} == {w}
    assert w >= max(b.sizeHint().width() for b in bs)
    assert w >= M._px(78)


def test_even_width_floor_wins_over_short_text(qapp):
    """一排短文案（「大/中/小」）不该缩成三个小方块 —— 下限先兜住。"""
    from PyQt5.QtWidgets import QPushButton
    bs = [QPushButton("大"), QPushButton("中"), QPushButton("小")]
    w = M._even_width(bs, M._px(110))
    assert w == M._px(110)
    assert {b.width() for b in bs} == {w}

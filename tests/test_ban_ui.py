# -*- coding: utf-8 -*-
"""禁手规则在界面上的三条用户可见契约（需要 QApplication）。

1. **规则页排在模式页之后、颜色/难度页之前。** 这是用户 2026-10-14 点名的位置
   （"该选择放在选择对战模式之后"），也是唯一自洽的位置：难度页要按已选规则读
   对应的档位表，而"这一局跟谁下"必须先定 —— 局域网加入方尤其不该看到一个
   自己无权决定的选项（规则由房主定，见 `_apply_room_config`）。
2. **禁手标点是几何图形**（半透明红圆 + 红环 + 红叉），不是字符。用字符画
   （U+274C 之类）在缺字形的系统上会变成豆腐块，而棋盘是哪怕 UI 字体全部回退
   也必须能读的那一层。这条用**像素**验：环上是不透明的红、环内是淡红、对角线上
   是红芯、格子角落是全透明的 —— 四条同时成立才说明那是一枚画出来的标记。
3. **标点只在禁手局、只在轮到黑方时出现，不在禁手局上画。** 白方回合画上去，
   白方会把这些点读成自己的禁区，而它们恰恰是白方能下、黑方碰不得的点。

``engine_ban`` 本身（判定对不对）由 ``tests/test_ban_rules.py`` 与
``tests/test_ban_engine.py`` 负责，这里只管"界面上呈现成什么样"。
"""

from __future__ import annotations

import io
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtWidgets import QApplication, QPushButton

import board_render
import engine_ban
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
    from PyQt5.QtWidgets import QLabel
    for lbl in card.findChildren(QLabel):
        if lbl.property("role") == "card-text":
            return lbl.text()
    return None


# ==================== 1：规则页的位置与分流 ====================

def test_rules_page_offers_exactly_two_choices(qapp):
    """两张卡：无禁手 / 禁手，点击各自发出 0 / 1。"""
    page = M.SelectionScreen(mode="rules")
    got = []
    page.rules_selected.connect(got.append)
    try:
        assert len(page._cards) == 2
        assert [_card_text(c) for c in page._cards] == [
            i18n.t("无禁手"), i18n.t("禁手")]
        page._cards[1].click()
        assert got == [1]
    finally:
        page.deleteLater()
        _pump(qapp, 3)


@pytest.mark.parametrize("mode,expected_attr", [
    (0, "selection_color"),      # 挑战 AI：规则 → 颜色 → 难度
    (1, "game_widget"),          # 本地对战：规则 → 直接开局
    (2, "lan_menu"),             # 局域网：规则 → 创建/加入岔路口
])
def test_mode_then_rules_then_the_right_next_page(win, qapp, mode, expected_attr):
    """三种模式都先落到规则页，选完各自去正确的那一页。

    ``_on_mode_selected`` 直接进颜色页/开房间的旧行为会让局域网加入方在**没有
    规则可言**的位置上看到规则选择 —— 规则由房主定，加入方的选择无处安放。
    """
    win._on_mode_selected(mode)
    _pump(qapp)
    assert win.selection_rules is not None, "模式页之后不是规则页"
    assert win.central.currentWidget() is win.selection_rules

    win._on_rules_selected(0)
    _pump(qapp)
    assert getattr(win, expected_attr) is not None, (
        "选完规则没到 %s" % expected_attr)


def test_lan_join_never_asks_the_guest_for_rules(win, qapp):
    """加入方的规则来自房主的 ``welcome``，本机上那一格必须先落成默认值。

    加入方的 ``_prepare_lan_game(rules=None)`` 写的是"无禁手"；真实值要等
    ``_apply_room_config`` 从 welcome 的 config 里读回来。这里钉住"进对局之前
    不残留上一局的禁手口径" —— 残留的效果是空盘上挂着一片上一局的禁手标点。
    """
    win.game_rules = "ban"                     # 假装上一局是禁手局
    win._prepare_lan_game("局域网联机（加入方）")
    assert win.game_rules == "free"
    assert win.session.rules == "free"


def test_ban_choice_reaches_the_session_and_the_room(win, qapp):
    """本地禁手局：规则同时落到 UI 镜像会话与本局房间的 config。

    两处不是重复：房间的 Session 才是**判**的那一方，UI 这份只供渲染与复盘
    读取。只写一处的话，棋盘上会画着禁手标点而房间照旧按五连判。
    """
    win._on_mode_selected(1)
    _pump(qapp)
    win._on_rules_selected(1)
    _pump(qapp)
    assert win.game_rules == "ban"
    assert win.session.rules == "ban"
    assert win._room.room.config.rules == "ban"
    assert "禁手" in win.logger.f.getvalue(), "日志没记这一局的规则"


# ==================== 2：面板的「规则」行 ====================

def test_panel_rules_row_reports_the_variant_in_words(qapp):
    """面板上写的是「无禁手」/「禁手」，不是裸的 free/ban。

    两个值都如实写，不留「—」：空缺会被读成"信息还没到"，而这一行从开局那
    一刻起就是确定的。
    """
    panel = M.GamePanel()
    try:
        panel.update_info(1, 1, "进行中", 3, 0, human=1, local=True,
                          rules="free")
        assert panel.rules_row._value.text() == i18n.t("无禁手")
        panel.update_info(1, 1, "进行中", 3, 0, human=1, local=True,
                          rules="ban")
        assert panel.rules_row._value.text() == i18n.t("禁手")
    finally:
        panel.deleteLater()
        _pump(qapp, 3)


def test_panel_width_budget_covers_the_longer_of_the_two_rows(qapp):
    """面板宽度按**两种规则里较宽的那一个**留 —— 否则切到禁手局会被裁。

    钉的是 ``_width_need`` 的值域表把「无禁手」与「禁手」都试穿过。中文下
    「禁手」更短、这条不会红；俄语「Запрещённые ходы」是这一列里最长的一个，
    按当前规则挑窄的算，切过去就被裁。
    """
    panel = M.GamePanel()
    try:
        fm = panel.rules_row._value.fontMetrics()
        need = max(fm.horizontalAdvance(i18n.t("无禁手")),
                   fm.horizontalAdvance(i18n.t("禁手")))
        assert panel._width_need() >= need
    finally:
        panel.deleteLater()
        _pump(qapp, 3)


# ==================== 3：标点的出现时机 ====================

@pytest.fixture
def fake_ban(monkeypatch):
    """把禁手点判定换成固定答案，只测"什么时候画、画哪些"。"""
    calls = []

    def fake_points(board, player=1):
        calls.append(player)
        return [(3, 3)]

    monkeypatch.setattr(engine_ban, "forbidden_points", fake_points)
    return calls


def test_free_game_never_asks_for_forbidden_points(win, qapp, fake_ban):
    """无禁手局连判定都不问 —— ``forbidden_cells`` 恒空，绘制路径不存在。"""
    win._on_mode_selected(1)
    _pump(qapp)
    win._on_rules_selected(0)
    _pump(qapp)
    win._sync_forbidden()
    assert fake_ban == [], "无禁手局不该调 forbidden_points"
    assert win.board_widget.forbidden_cells == []


def test_forbidden_points_are_shown_only_on_blacks_turn(win, qapp, fake_ban):
    """禁手局：黑方回合显示，白方回合清空。

    白方回合留着这片叉，白方会把它们读成自己的禁区。
    """
    win._on_mode_selected(1)
    _pump(qapp)
    win._on_rules_selected(1)
    _pump(qapp)
    _pump(qapp, 20)

    assert win.move_count == 0, "空盘应当是黑方回合"
    assert win.board_widget.forbidden_cells == [(3, 3)]

    # 黑先落一手 → 轮到白方 → 标点全清（不靠画成透明的，是真的空）。
    win.move_count = 1                       # 等价于"已经下过一手"
    win._sync_forbidden()
    assert win.board_widget.forbidden_cells == []
    assert fake_ban, "黑方回合那一次应当问过判定"


def test_forbidden_points_are_cleared_when_the_game_ends(win, qapp, fake_ban):
    """终局后不画：胜负已定，禁手点不再有任何可操作性。"""
    win._on_mode_selected(1)
    _pump(qapp)
    win._on_rules_selected(1)
    _pump(qapp)
    assert win.board_widget.forbidden_cells == [(3, 3)]

    win.session.game_over = True
    win._sync_forbidden()
    assert win.board_widget.forbidden_cells == []


def test_forbidden_cells_are_told_not_assumed(qapp):
    """重复设同一份集合不该触发重绘 —— 每手都会调一次，白画一次是一整帧。

    ``set_forbidden`` 用 `==` 比而不是永远 `update()`：``_sync_forbidden``
    在 ``_on_room_state`` 与 ``_on_room_move`` 里各调一次，同一手会走两遍。
    """
    w = M.BoardWidget()
    painted = []
    original = w.update
    w.update = lambda *a, **k: (painted.append(1), original(*a, **k))[1]
    try:
        w.set_forbidden([(3, 3)])
        assert len(painted) == 1
        w.set_forbidden([(3, 3)])
        assert len(painted) == 1, "同一份集合被重复设了一次就重绘了"
        w.set_forbidden([(3, 4)])
        assert len(painted) == 2
    finally:
        w.update = original
        w.set_forbidden([])


# ==================== 4：标点的几何形状 ====================

def _marker_image(d=40.0, dpr=1.0):
    return board_render.forbidden_marker(d, dpr).toImage()


def _pixel(img, x, y):
    return img.pixelColor(int(round(x)), int(round(y)))


def _rgb_hex(qcolor):
    return "#%02x%02x%02x" % (qcolor.red(), qcolor.green(), qcolor.blue())


def _same_hue_as_forbidden(qcolor, tol=8):
    """颜色是否是 ``theme.FORBIDDEN`` 那支红（允许 8 的差）。

    半透明像素在 ARGB32_Premultiplied 上要经过一次"预乘 → 反预乘"，
    46/255 的那次除法会把每个通道挪一两点 —— 那是存储格式的舍入，不是画错了
    颜色。不透明像素（alpha=255）没有这一步，所以环那一条可以逐字比。
    """
    want = theme.FORBIDDEN.lstrip("#")
    got = _rgb_hex(qcolor).lstrip("#")
    return all(abs(int(got[i:i + 2], 16) - int(want[i:i + 2], 16)) <= tol
               for i in (0, 2, 4))


def test_forbidden_marker_is_a_ring_a_cross_and_a_fill(qapp):
    """一枚标记的四个部位，逐点验像素。

    全是几何画法：**不依赖任何字体**，也不依赖任何平台字符渲染。
    """
    d = 40.0
    img = _marker_image(d)
    s = d * board_render._MARKER_SCALE
    cx = cy = s / 2.0
    r = d / 2.0                                # 与 `forbidden_marker` 同口径
    ring_r = r * 0.80
    arm = ring_r * 0.50                        # 叉的半臂长
    red = theme.FORBIDDEN

    # ① 环：右缘正中是不透明的红。
    on_ring = _pixel(img, cx + ring_r, cy)
    assert on_ring.alpha() == 255, "环应当是实心的"
    assert on_ring.name() == red, "环的颜色应当就是 theme.FORBIDDEN"

    # ② 环内填充：淡红，透明度就是那个常量，色相与环同支。
    #
    # **不能在正中心取样**：两条对角线都过原点，中心那一像素是叉的芯，
    # 不透明。取 (cx + arm, cy) —— 离环还有半个半径、离对角线的距离是
    # arm/√2 ≈ 5.7px，比最宽那支笔画（衬底）的半宽 3.4px 还宽，只剩填充。
    inside = _pixel(img, cx + arm, cy)
    assert 0 < inside.alpha() < 120, "填充应当是半透明的（%d）" % inside.alpha()
    assert _same_hue_as_forbidden(inside), (
        "填充与环应当同色，实际 %s" % _rgb_hex(inside))

    # ③ 叉：两条对角线在离中心半个臂长处都有红芯。
    core = _pixel(img, cx + arm / 2.0, cy + arm / 2.0)
    anti = _pixel(img, cx + arm / 2.0, cy - arm / 2.0)
    for name, px in (("主对角", core), ("副对角", anti)):
        assert px.alpha() > inside.alpha() + 60, (
            "%s上没有画出叉的芯（alpha %d，填充才 %d）"
            % (name, px.alpha(), inside.alpha()))

    # ④ 格子四角：全透明。标记不铺满整格 —— 铺满会盖住旁边的网格与星位。
    for x, y in ((0, 0), (s - 1, 0), (0, s - 1), (s - 1, s - 1)):
        assert _pixel(img, x, y).alpha() == 0, "标记铺到了格子角落 (%s,%s)" % (x, y)


def test_forbidden_marker_leaves_the_stone_visible(qapp):
    """环内填充必须**明显淡于**环本身。

    填充是给"这个点是红的"提供一个可读的色块，不是把整格涂掉 —— 空盘的木色
    与网格线要照样看得见，否则一片禁手区看起来像棋盘缺了一块。
    """
    img = _marker_image()
    s = 40.0 * board_render._MARKER_SCALE
    cx = cy = s / 2.0
    ring = _pixel(img, cx + 16.0, cy)          # 40 * 0.8 / 2 = 16
    fill = _pixel(img, cx + 8.0, cy)           # 环内、离叉够远（见上一条）
    assert fill.alpha() * 3 < ring.alpha(), (
        "填充（alpha %d）与环（alpha %d）差别太小"
        % (fill.alpha(), ring.alpha()))
    assert fill.alpha() == theme.FORBIDDEN_FILL_ALPHA


def test_forbidden_marker_scales_with_the_cell_and_is_cached(qapp):
    """尺寸跟着格子走（同一个尺寸只画一次）。

    缓存键含 dpr：换屏幕（或窗口跨到另一块屏）时按旧 dpr 画的 sprite 会被
    直接放大，边缘发糊 —— 缓存本身是每次 ``paintEvent`` 都要走的路径，
    没有缓存则一帧里 19×19 个禁手点各画一遍。
    """
    a = board_render.forbidden_marker(30.0, 1.0)
    assert a is board_render.forbidden_marker(30.0, 1.0), "同尺寸没有命中缓存"
    b = board_render.forbidden_marker(30.0, 2.0)
    assert b is not a, "dpr 没进缓存键"
    assert b.devicePixelRatio() == 2.0
    assert abs(b.width() - 2 * a.width()) <= 1, "高 dpi 下按 dpr 放大画布"
    # 尺寸不同则形状按比例：环的半径占画布的比例两者一致。
    big = _marker_image(60.0)
    assert big.width() == _marker_image(40.0).width() * 3 // 2


def test_forbidden_marker_paints_onto_the_board(win, qapp, fake_ban):
    """整条链路：``forbidden_cells`` → ``paintEvent`` → 棋盘上真的有红像素。

    只比 sprite 的话，"算出来了但没画上去"这种断线不会被发现 ——
    ``paintEvent`` 里那一段正是本次新增的绘制路径。
    """
    win._on_mode_selected(1)
    _pump(qapp)
    win._on_rules_selected(1)
    _pump(qapp)
    _pump(qapp, 20)
    assert win.board_widget.forbidden_cells == [(3, 3)]

    bw = win.board_widget
    dpr = bw.devicePixelRatioF()
    pt = bw.cell_center(3, 3)
    # 取该格中心：环与叉都不落在这一像素上，只有那层淡红填充 —— 它与空盘木色
    # 在任何主题下都不该相同。先画后擦，两次同格取样做差。
    marked = _at(bw.grab().toImage(), pt, dpr)
    bw.set_forbidden([])
    bare = _at(bw.grab().toImage(), pt, dpr)
    diff = (abs(marked.red() - bare.red())
            + abs(marked.green() - bare.green())
            + abs(marked.blue() - bare.blue()))
    assert diff > 6, (
        "禁手格中心与空盘同色（差 %d）—— paintEvent 那一段没画上去" % diff)


def _at(img, pt, dpr):
    return img.pixelColor(int(round(pt.x() * dpr)), int(round(pt.y() * dpr)))


def test_forbidden_tooltip_only_on_the_marked_cells(win, qapp, fake_ban):
    """悬停在禁手点上才出 tooltip：这是"这个点为什么是红的"的唯一解释。"""
    win._on_mode_selected(1)
    _pump(qapp)
    win._on_rules_selected(1)
    _pump(qapp)
    win._sync_forbidden()

    bw = win.board_widget
    bw.hover_pos = (3, 3)
    bw._sync_hover_tip()
    assert bw.toolTip() == i18n.t("禁手点：黑方落子即判负")

    bw.hover_pos = (3, 4)
    bw._sync_hover_tip()
    assert bw.toolTip() == "", "非禁手点上不该挂提示"


def test_the_confirm_dialog_is_the_only_way_to_play_a_forbidden_point(win, qapp):
    """点禁手点先弹确认框，默认按钮是「否」。取消则这一手根本不发出去。

    房间那边照判不误（落下去就是判负），所以这里拦的不是"规则"而是"误触"。
    """
    from PyQt5.QtWidgets import QMessageBox

    win._on_mode_selected(1)
    _pump(qapp)
    win._on_rules_selected(1)
    _pump(qapp)
    win.board_widget.set_forbidden([(3, 3)])

    asked = {}

    class _No:
        """替一个"用户点了否"的 QMessageBox。"""

        Yes, No = QMessageBox.Yes, QMessageBox.No

        @staticmethod
        def question(parent, title, text, buttons, default):
            asked["title"] = title
            asked["text"] = text
            asked["default"] = default
            return QMessageBox.No

    class _FakeClient:
        """只记下"这一手有没有发出去"；``close`` 是收尾路径要的。"""

        def __init__(self):
            self.sent = []

        def move(self, r, c):
            self.sent.append((r, c))

        def close(self):
            pass

    client = _FakeClient()
    win._room_clients = {1: client}
    original = M.QMessageBox
    M.QMessageBox = _No
    try:
        pt = win.board_widget.cell_center(3, 3)
        win._on_board_click(_Click(pt.x(), pt.y()))
    finally:
        M.QMessageBox = original

    assert asked["default"] == QMessageBox.No, "默认按钮是「是」——回车就等于认输"
    assert asked["title"] == i18n.t("禁手点")
    assert client.sent == [], "用户选了否，这一手不该发出去"


class _Click:
    """只实现 ``_on_board_click`` 用到的 x()/y()。"""

    def __init__(self, x, y):
        self._x, self._y = x, y

    def x(self):
        return self._x

    def y(self):
        return self._y


def test_rule_cards_use_the_danger_tone_for_ban(qapp):
    """「禁手」那张卡用 danger 音色 —— 与棋盘上那枚红叉同一个语义色。

    ``card_button`` 把 tone 存成按钮属性、由 QSS 定文字色，所以这里读属性。
    两张卡**都不给卡面图形**：这一页的区分全在文字里，"没有标记"这件事本来
    就没有图形可画（见 ``setup_ui`` 的 rules 分支）。
    """
    page = M.SelectionScreen(mode="rules")
    try:
        tones = [c.property("tone") for c in page._cards]
        assert tones == ["ghost", "danger"], tones
        for card in page._cards:
            assert card.property("variant") == "card"
    finally:
        page.deleteLater()
        _pump(qapp, 3)

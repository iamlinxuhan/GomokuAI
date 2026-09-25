# -*- coding: utf-8 -*-
"""可复用 UI 原语：标题 / 副标题 / 分隔线 / 信息行 / 按钮 / 页面骨架。

样式一律走 ``theme`` 里的全局 QSS + 动态属性（``role`` / ``variant`` /
``tone``），**不在这里写 styleSheet 字符串** —— 那是"同一个语义散落十几处
字面量"的老问题。本模块只负责把控件造出来并挂上正确的属性。

## 边界：什么该抽，什么不该

抽的是**重复出现且语义相同**的东西（三处标题的三种字号、两个手搓分隔线、
三个各自为政的按钮工厂）。不抽的是**单实例**的东西 —— ``BoardWidget`` /
``GamePanel`` 各只有一个，为它们造基类是假想需求；``QVBoxLayout.setSpacing()``
本身就是 API，再包一层只增加阅读成本。
"""

from __future__ import annotations

from PyQt5.QtCore import Qt, QPointF, QRectF
from PyQt5.QtGui import QBrush, QColor, QPainter, QPixmap, QRadialGradient
from PyQt5.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton,
                             QSizePolicy, QStyle, QStyleOption, QVBoxLayout,
                             QWidget)

import board_render
import theme
from theme import (CARD_PX, CONTROL_H, SPACE_LG, SPACE_MD, SPACE_SM,
                   SPACE_XL, SPACE_XS, SPACE_XXL)

# ==================== 屏幕页的科技感底纹 ====================
#
# 只给 `Screen` 页（加载 / 选择），**对局页绝对不加**：棋盘四周必须干净，
# `tools/ui_snapshot.py:probe_board_plate` 断言棋盘四角像素 ≈ theme.BG —— 那
# 条探针钉住的正是这件事，底纹一旦铺到对局页它就会随机失败（网格线是否穿过
# 取样点取决于窗口尺寸）。

_GRID_PX = 32          # 网格间距
_GRID_ALPHA = 16       # 网格透明度（约 6%）。再高就从"结构感"变成"格子布"
_GLOW_ALPHA = 34       # 辉光中心透明度
_BRACKET_ARM = 14      # HUD 括号的臂长
_BRACKET_GAP = 10      # 括号离标记的距离

_tiles: dict = {}


def _grid_tile(dpr: float, color: str) -> QPixmap:
    """一格网格的贴图，按 (dpr, 颜色) 缓存。

    缓存而不是每次画 40 条竖线 + 25 条横线：加载页与选择页每帧都在重绘，
    而那两屏正是"启动时看着最该顺"的地方。
    """
    key = (round(dpr, 2), color)
    tile = _tiles.get(key)
    if tile is not None:
        return tile
    side = max(1, int(round(_GRID_PX * dpr)))
    tile = QPixmap(side, side)
    tile.setDevicePixelRatio(dpr)
    tile.fill(Qt.transparent)
    p = QPainter(tile)
    c = QColor(color)
    c.setAlpha(_GRID_ALPHA)
    p.setPen(c)
    # 只画左、上两条 —— 平铺之后就是完整网格，且相邻格共用一条线。
    p.drawLine(0, 0, 0, side - 1)
    p.drawLine(0, 0, side - 1, 0)
    p.end()
    _tiles[key] = tile
    return tile


def _paint_backdrop(p: QPainter, rect, center: QPointF, dpr: float) -> None:
    """一层极弱的结构：网格 + 标题区一团辉光。

    目的不是装饰，是给"这一屏在等你"一个视觉落点 —— 纯竖向渐变的底太平，
    屏幕上除了文字没有任何东西说明这是个"正在发生什么"的界面。
    """
    p.save()
    p.setRenderHint(QPainter.Antialiasing, False)   # 1px 网格线开了会糊成 2px
    p.fillRect(rect, QBrush(_grid_tile(dpr, theme.ACCENT)))
    p.setRenderHint(QPainter.Antialiasing, True)
    # 辉光：以标题/标记为中心向外衰减。半径按屏幕短边取，随窗口缩放。
    radius = max(rect.width(), rect.height()) * 0.45
    grad = QRadialGradient(center, radius)
    c = QColor(theme.ACCENT)
    c.setAlpha(_GLOW_ALPHA)
    grad.setColorAt(0.0, c)
    c2 = QColor(theme.ACCENT)
    c2.setAlpha(0)
    grad.setColorAt(1.0, c2)
    p.fillRect(rect, QBrush(grad))
    p.restore()


def _paint_hud(p: QPainter, box, dpr: float) -> None:
    """标记四角的 HUD 括号（L 形细线）。"""
    p.save()
    p.setRenderHint(QPainter.Antialiasing, False)
    c = QColor(theme.ACCENT)
    c.setAlpha(150)
    p.setPen(c)
    x0, y0 = box.left() - _BRACKET_GAP, box.top() - _BRACKET_GAP
    x1, y1 = box.right() + _BRACKET_GAP, box.bottom() + _BRACKET_GAP
    a = _BRACKET_ARM
    for (x, y, sx, sy) in ((x0, y0, 1, 1), (x1, y0, -1, 1),
                           (x0, y1, 1, -1), (x1, y1, -1, -1)):
        p.drawLine(x, y, x + sx * a, y)
        p.drawLine(x, y, x, y + sy * a)
    p.restore()


def _set_role(w, role: str):
    """挂 ``role`` 动态属性 —— QSS 靠它选中不同语义的同类控件。"""
    w.setProperty("role", role)
    return w


# ==================== 文本 ====================

def title_label(text: str, role: str = "title") -> QLabel:
    """屏幕主标题。字号由 QSS 的 ``role`` 决定，不在这里写死。"""
    lbl = QLabel(text)
    lbl.setAlignment(Qt.AlignCenter)
    return _set_role(lbl, role)


def subtitle_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setAlignment(Qt.AlignCenter)
    return _set_role(lbl, "subtitle")


def faint_label(text: str, align=Qt.AlignCenter) -> QLabel:
    """页脚 / 统计行等次要文字。"""
    lbl = QLabel(text)
    lbl.setAlignment(align)
    return _set_role(lbl, "faint")


def value_label(text: str = "—") -> QLabel:
    """信息行里的值。"""
    lbl = QLabel(text)
    lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
    return _set_role(lbl, "value")


# ==================== 分隔线 ====================

def separator() -> QFrame:
    """1px 分隔线。两条手搓的"灰条 + ``max-height:1px``"合并于此。"""
    line = QFrame()
    line.setFrameShape(QFrame.NoFrame)
    line.setFixedHeight(1)
    line.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
    return _set_role(line, "separator")


# ==================== 按钮 ====================

def button(text: str, variant: str = "primary", *,
           height: int = CONTROL_H, width: int | None = None) -> QPushButton:
    """标准按钮。四态（hover / pressed / checked / disabled）由 QSS 按 ``variant`` 生成。

    历史上这里有三个各自为政的工厂（``_make_button`` 高 40、重开/退出按钮
    高 45、卡片另算一套），半径与字号互不匹配。现在变体是**数据**
    （``theme.button_variants()``），新增一种只是加一行。
    """
    btn = QPushButton(text)
    btn.setFixedHeight(height)
    if width is not None:
        btn.setFixedWidth(width)
    btn.setCursor(Qt.PointingHandCursor)
    return _set_role_buttons(btn, variant)


def _set_role_buttons(btn: QPushButton, variant: str) -> QPushButton:
    btn.setProperty("variant", variant)
    return btn


def card_button(text: str, tone: str, *, face: QWidget | None = None,
                sub: str = "", index: str = "",
                size: int = CARD_PX) -> QPushButton:
    """选择用的方形卡片：``[face] / text / sub``，底色一律 SURFACE。

    ``tone`` 只决定文字色（见 ``theme._card_tones``）—— 整面高饱和色板是旧版
    最显旧的一处，已废。

    ``face`` 是**子控件**而不是 QPushButton 的 icon：按钮的 icon 与文字只能
    横排（``QToolButton`` 才有竖排档），而卡片要的是图形在上、文字在下。
    子控件默认不吃鼠标事件，点击照常落到按钮上。

    ``index``（如 ``"01"``）画在卡片左上角。它**不进布局**，而是按绝对坐标
    ``move()`` 上去：进了布局就会和上下两根 stretch 抢空间，把居中的那组
    "图形 + 两行字"整体推偏。卡片是 ``setFixedSize`` 的，尺寸不会变，所以
    一次定位就够，不需要接 ``resizeEvent``。
    """
    btn = QPushButton()
    btn.setFixedSize(size, size)
    btn.setCursor(Qt.PointingHandCursor)
    btn.setProperty("variant", "card")
    btn.setProperty("tone", tone)

    if index:
        tag = QLabel(index, btn)          # 父控件是按钮 -> 叠在卡面上
        tag.setProperty("role", "card-index")
        # 不吃鼠标事件：它盖在按钮左上角，吃掉了那一小块就点不动卡片。
        tag.setAttribute(Qt.WA_TransparentForMouseEvents)
        # 先 polish 再量尺寸 —— QSS 里才定的字号，不 polish 量到的是默认字体。
        tag.ensurePolished()
        tag.adjustSize()
        tag.move(SPACE_MD, SPACE_SM)

    box = QVBoxLayout(btn)
    box.setContentsMargins(SPACE_MD, SPACE_MD, SPACE_MD, SPACE_MD)
    box.setSpacing(SPACE_SM)
    # 上下等量留白把"图形 + 两行字"这一组整体居中。难度卡没有 face，内容
    # 只有 36px 高，全靠这两根 stretch 撑出居中而不是顶到上边。
    box.addStretch(1)

    if face is not None:
        box.addWidget(face, 0, Qt.AlignHCenter)

    label = QLabel(text)
    label.setAlignment(Qt.AlignCenter)
    label.setProperty("role", "card-text")
    label.setProperty("tone", tone)
    box.addWidget(label)

    if sub:
        hint = QLabel(sub)
        hint.setAlignment(Qt.AlignCenter)
        hint.setProperty("role", "card-sub")
        box.addWidget(hint)

    box.addStretch(1)
    return btn


# ==================== 信息行 ====================

class InfoRow(QWidget):
    """``标签 ———— 值`` 的一行。

    替代原先 5 段各写一遍的 QLabel 样式表，以及 ``update_info`` 每次更新都把
    整条样式表按 3 个分支重写一遍的做法：现在只改文本（必要时加 ``tone``），
    颜色由 QSS 管。
    """

    def __init__(self, label: str, value: str = "—", parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(SPACE_SM)

        self._label = QLabel(label)
        self._label.setProperty("role", "subtitle")
        self._value = value_label(value)

        row.addWidget(self._label)
        row.addStretch(1)
        row.addWidget(self._value)

    def set_value(self, text: str, tone: str = "normal") -> None:
        """更新值。``tone`` 走动态属性 + repolish（Qt 不会自动重算样式）。"""
        self._value.setText(text)
        self._value.setProperty("tone", tone)
        self._value.style().unpolish(self._value)
        self._value.style().polish(self._value)


# ==================== 回合指示 ====================

class StoneDot(QWidget):
    """面板里的小棋子图标（自绘，复用 ``board_render`` 的 sprite 缓存）。

    直径固定像素值即可 —— 它是**图标**，不随棋盘缩放，否则面板里会忽大忽小。
    """

    def __init__(self, diameter: int = 26, parent=None):
        super().__init__(parent)
        self._player = 1
        self._d = float(diameter)
        # sprite 画布比棋子直径大（含投影余量），控件要给投影留出空间。
        pad = int(self._d * (board_render._SPRITE_SCALE - 1.0) / 2.0) + 1
        self.setFixedSize(diameter + 2 * pad, diameter + 2 * pad)

    def set_player(self, player: int) -> None:
        if player != self._player:
            self._player = player
            self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        sprite = board_render.stone_sprite(
            self._player, self._d, self.devicePixelRatioF())
        half = sprite.width() / (2.0 * self.devicePixelRatioF())
        cx, cy = self.width() / 2.0, self.height() / 2.0
        # 图标语境下投影会喧宾夺主，向上提半格让视觉中心居中。
        p.drawPixmap(QPointF(cx - half, cy - half - self._d * 0.06), sprite)
        p.end()


class StoneFace(QWidget):
    """卡片上的棋子图形 —— 选颜色时用户看到的就是他挑的那颗子。

    与面板里的 ``StoneDot`` 同一份 sprite，但**投影余量收紧到 1.25 倍**：
    ``StoneDot`` 留的是 1.6 倍（面板空间宽裕），用在卡片上会让图形四周空掉
    一圈，一行三颗时更是散成三块。1.25 倍刚好不裁到接触投影。
    """

    def __init__(self, player: int = 1, diameter: int = 60, parent=None):
        super().__init__(parent)
        self._player = player
        self._d = float(diameter)
        side = int(round(self._d * 1.25))
        self.setFixedSize(side, side)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        dpr = self.devicePixelRatioF()
        sprite = board_render.stone_sprite(self._player, self._d, dpr)
        # sprite 的画布是 d * _SPRITE_SCALE（含投影余量），按画布居中而非按
        # 棋子直径居中 —— 否则重心会偏下。
        side = self._d * board_render._SPRITE_SCALE
        p.drawPixmap(QPointF((self.width() - side) / 2.0,
                             (self.height() - side) / 2.0), sprite)
        p.end()


def stone_row(players, *, diameter: int = 28) -> QWidget:
    """一行棋子。难度卡用它当"强度条"：初级一颗、中级两颗、高级三颗。"""
    return hbox(*(StoneFace(p, diameter) for p in players), spacing=SPACE_XS)


class BrandMark(QWidget):
    """应用标记：一块缩微的木棋盘（见 ``board_render.brand_mark``）。

    自绘而非 ``QLabel.setPixmap``：DPR 变化时要按新的 DPR 重取 sprite，
    ``paintEvent`` 里现取是唯一不会取到旧分辨率的路子（与 ``StoneDot`` 同理）。
    """

    def __init__(self, size: int = 104, parent=None):
        super().__init__(parent)
        self._size = float(size)
        # 画布含投影余量，控件要把它算进去，否则外层阴影被裁掉。
        side = int(round(size * 1.20))
        self.setFixedSize(side, side)
        self.setAttribute(Qt.WA_TranslucentBackground, True)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        pm = board_render.brand_mark(self._size, self.devicePixelRatioF())
        # 木牌在画布里是居中的，控件也居中放置即可。
        p.drawPixmap(QPointF((self.width() - pm.width()
                              / self.devicePixelRatioF()) / 2.0,
                             (self.height() - pm.height()
                              / self.devicePixelRatioF()) / 2.0), pm)
        p.end()


class TurnIndicator(QFrame):
    """回合指示卡：大棋子图标 + 状态文字。

    面板里最醒目的一块 —— 用户瞥一眼就知道"现在轮到谁、我能不能落子"。
    样式由 QSS 的 ``QFrame[role="turn-card"]``（含 ``[state="think"]`` 变体）
    提供；呼吸动画由 ``GamePanel`` 用 ``anim.pulse`` 挂在本控件上。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty("role", "turn-card")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        row = QHBoxLayout(self)
        row.setContentsMargins(SPACE_LG, SPACE_MD, SPACE_LG, SPACE_MD)
        row.setSpacing(SPACE_MD)

        self.dot = StoneDot()
        row.addWidget(self.dot, 0, Qt.AlignVCenter)

        self.label = QLabel("准备开始")
        self.label.setProperty("role", "turn-text")
        row.addWidget(self.label, 1)

    def _set(self, player: int, text: str, tone: str, state: str) -> None:
        self.dot.set_player(player)
        self.label.setText(text)
        self.label.setProperty("tone", tone)
        self.setProperty("state", state)
        # 动态属性改完必须 repolish，否则 QSS 不重算（老坑）。
        for w in (self, self.label):
            w.style().unpolish(w)
            w.style().polish(w)

    def set_turn(self, player: int, text: str) -> None:
        """常态：轮到某方落子。"""
        self._set(player, text, "", "idle")

    def set_thinking(self, player: int, text: str = "AI 思考中…") -> None:
        """AI 思考态：INFO 蓝文字 + 卡片描边转蓝（配合呼吸动画）。"""
        self._set(player, text, "think", "think")

    def set_result(self, player: int, text: str, tone: str) -> None:
        """终局态：``tone`` 为 "win" / "lose" 或空（平局）。"""
        self._set(player, text, tone, "idle")


# ==================== 页面骨架 ====================

class Screen(QWidget):
    """全屏页面骨架：深色底 + 垂直居中节奏。

    Loading / 颜色选择 / 难度选择 / 结算遮罩共用同一套节奏，四个界面一字不差：

        [stretch 2] 标题 [SM] 副标题 [XXL] <body> [stretch 3]

    这样"四个界面像同一个应用"是结构上成立的，而不是靠逐屏调参凑出来。

    ``GamePanel`` **不继承本类** —— 它是"贴边、定宽、四段式"，与这里的
    "居中、全屏、上下留白"是两种排布。体系里是**两套排布、一套度量**。
    """

    def __init__(self, title: str = "", subtitle: str = "", *,
                 lead: QWidget | None = None, backdrop: bool = False,
                 root_name: str = "screenRoot", parent=None):
        super().__init__(parent)
        self.setObjectName(root_name)
        # 自定义 QWidget 子类**必须**显式开这个属性，QSS 的 background 才生效。
        # （Qt 已知类如 QFrame 不需要。）
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._backdrop = backdrop
        self._lead = lead

        root = QVBoxLayout(self)
        root.setContentsMargins(SPACE_XL, SPACE_XL, SPACE_XL, SPACE_XL)
        root.setSpacing(0)
        root.addStretch(2)

        # 标题之上的图形位（加载页的木牌标记）。只有加载页用得上，但节奏
        # 归 Screen 管 —— 让调用方自己拼，四个界面就会有四种标题间距。
        if lead is not None:
            root.addWidget(lead, 0, Qt.AlignCenter)
            root.addSpacing(SPACE_MD)

        if title:
            root.addWidget(title_label(title), 0, Qt.AlignCenter)
        if subtitle:
            root.addSpacing(SPACE_SM)
            root.addWidget(subtitle_label(subtitle), 0, Qt.AlignCenter)
        if title or subtitle:
            root.addSpacing(SPACE_XXL)

        self.body = QVBoxLayout()
        self.body.setSpacing(SPACE_MD)
        root.addLayout(self.body)
        root.addStretch(3)

    # ---- 科技感底纹 ----

    def _glow_center(self) -> QPointF:
        """辉光中心：有标记就落在标记上，否则落在标题带（上三分之一）。

        挂在标记上而不是屏幕正中，是因为标记是视线第一落点 —— 辉光在中心、
        内容在上方，看起来像背景没对齐。
        """
        if self._lead is not None and self._lead.isVisible():
            return QPointF(self._lead.geometry().center())
        return QPointF(self.width() / 2.0, self.height() * 0.32)

    def paintEvent(self, event):
        """先让 QSS 画底，再叠底纹。

        **覆盖 ``paintEvent`` 之后必须自己调 ``drawPrimitive(PE_Widget)``。**
        原来 QSS 背景由 ``WA_StyledBackground`` 自动画；覆盖之后不显式画，
        整页会变成黑的 —— 这是这套 QSS 最容易踩空的一处。
        """
        opt = QStyleOption()
        opt.initFrom(self)
        p = QPainter(self)
        self.style().drawPrimitive(QStyle.PE_Widget, opt, p, self)
        if self._backdrop:
            dpr = self.devicePixelRatioF()
            _paint_backdrop(p, self.rect(), self._glow_center(), dpr)
            if self._lead is not None and self._lead.isVisible():
                _paint_hud(p, self._lead.geometry(), dpr)
        p.end()

    def add_content(self, w: QWidget, align=Qt.AlignCenter) -> QWidget:
        self.body.addWidget(w, 0, align)
        return w

    def add_footer(self, w: QWidget, align=Qt.AlignCenter) -> QWidget:
        """底部固定内容（如无中文字体警告）。加在 stretch 之后，贴底。"""
        lay = self.layout()
        lay.addWidget(w, 0, align)
        return w


def hbox(*widgets, spacing: int = SPACE_MD, align=None) -> QWidget:
    """把若干控件横排进一个容器。仅用于"卡片行"这类确实要横排的场景。"""
    box = QWidget()
    lay = QHBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(spacing)
    if align is not None:
        lay.setAlignment(align)
    for w in widgets:
        lay.addWidget(w)
    return box

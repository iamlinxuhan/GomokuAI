"""五子棋游戏 — PyQt5 界面层。

界面、交互、动画与日志展示在这里；AI 引擎在 engine.py（纯 CPU、零 Qt/torch），
日志格式在 gamelog.py。本文件不再包含任何搜索或评估逻辑。
"""
import os
import sys
import math
import threading

import numpy as np

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget,
    QVBoxLayout, QHBoxLayout, QStackedWidget, QStackedLayout,
    QProgressBar, QFrame, QSizePolicy
)
from PyQt5.QtCore import (
    Qt, QTimer, QThread, pyqtSignal, QRect, QPoint, QPointF,
    QElapsedTimer, QEasingCurve, QVariantAnimation
)
from PyQt5.QtGui import (
    QPainter, QPen, QBrush, QColor, QMouseEvent
)

import analysis
import anim
import board_render
import charts
import engine
import theme
from board_geometry import BoardGeometry
from engine import (BOARD_SIZE, Board, ai_move, check_win, evaluate, is_mate,
                    new_game, opening_move, win_line)
from gamelog import GameLogger
from ui_kit import (BrandMark, InfoRow, Screen, StoneFace, TurnIndicator,
                    button, card_button, faint_label, hbox, separator,
                    stone_row, title_label)

# ==================== 界面常量 ====================
CELL_SIZE = 34                      # 设计基准：格距
# 19 个格点之间只有 **18 段**。旧代码写 `BOARD_SIZE * CELL_SIZE`，把格点当成了
# 段，于是网格右／下各比左／上多出一个格距，棋盘看着是偏的。
BOARD_SPAN = (BOARD_SIZE - 1) * CELL_SIZE      # 612
BOARD_PAD = 57                      # 网格到木盘边（要容下坐标标注）
MARGIN = BOARD_PAD                  # 设计基准下网格原点的偏移
BOARD_PX = BOARD_SPAN + 2 * BOARD_PAD          # 726 —— 棋盘控件设计边长
PANEL_W = theme.PANEL_W                        # 264 —— 面板宽度（唯一真源）
GAP = theme.SPACE_MD                           # 12  —— 棋盘/面板与窗口边缘
WINDOW_W = GAP + BOARD_PX + theme.SPACE_SM + PANEL_W + GAP   # 1022
WINDOW_H = GAP + BOARD_PX + GAP                              # 750

# ---- 缩放下限 ----
# 棋盘不再定死 726×726：小屏（1366×768）上旧窗口 1022×750 连标题栏一起摆不下，
# 底部按钮会被屏幕边缘吃掉。改为「窗口有下限、棋盘按控件尺寸等比缩放」。
MIN_CELL = 22                       # 最小格距；再小坐标标注会糊成一团
# 木盘边距（坐标标注带）随几何等比缩放，占设计边长的比例是固定的，所以
# 要反推「格距恰好 22 时棋盘控件该多宽」而不是拍一个数：
#   控件宽 = 18*cell / (1 - 2*边距占比)
# （plan 里写的 456 是按边距绝对值 30 算的，反推回来 cell 只有 21.4，
#   够不上 22 这条下限，所以这里从真实比例推。）
_PAD_FRAC = BOARD_PAD / BOARD_PX                     # 57/726 ≈ 0.0785
MIN_BOARD = math.ceil((BOARD_SIZE - 1) * MIN_CELL / (1.0 - 2.0 * _PAD_FRAC))  # 470
MIN_W = GAP + MIN_BOARD + theme.SPACE_SM + PANEL_W + GAP     # 766
# 高度下限由**面板**而不是棋盘决定：面板里现在挂着两张图表卡，它的
# minimumSizeHint 比棋盘那一列高。这个数只是兜底，真值在 `_build_game_ui`
# 里按实测重算（它依赖字体度量，需要 QApplication，写不成模块常量）。
#
# **上限是 MIN_W**：`tests/test_board_geometry.py` 断言 MIN_W > MIN_H，因为
# 棋盘取 min(w, h) 定格距 —— 高度超过宽度后，再高的窗口也只会给棋盘上下
# 加留白，格距不再增长。所以 MIN_H 不许越过 766。
MIN_H = 494
MAX_SCALE = 1.35                    # 初始尺寸上限：棋盘再大就一眼看不全 19 路了

# 棋盘几何的设计基准（1:1）。绘制、命中判定、无头测试全部经由它，
# 不要再在别处写第二份 `MARGIN + c * CELL_SIZE`。
_DESIGN_GEOM = BoardGeometry.design(BOARD_SIZE, CELL_SIZE, MARGIN)

# 调色板一律来自 theme —— 这里刻意**不再**声明任何 QColor 常量。
# `_qss_color()` 也随之删除：只要主题层不产出 QColor，"QColor 插进样式表变成
# 非法 CSS、Qt 静默丢弃整条规则"这个曾经真实发生过的 bug 在结构上就不可能出现。


# ==================== AI Worker 线程 ====================
class AIWorker(QThread):
    """AI计算线程，避免阻塞UI — 返回 (r, c, info_dict)

    取消是**协作式**的：cancel() 置位 Event，引擎在搜索循环里轮询到之后
    主动退出。原版用的是 QThread.terminate()，它会在任意字节码处强杀线程；
    搜索正在做棋盘 make/unmake 时被强杀，会留下不一致的状态。
    """
    finished = pyqtSignal(int, int, object)  # (row, col, info_dict)

    def __init__(self, board, ai_player, depth):
        super().__init__()
        self.board = board.copy()
        self.ai_player = ai_player
        self.depth = depth
        self._cancel = threading.Event()

    def cancel(self):
        """请求取消搜索。线程会在下一次节点轮询时退出。"""
        self._cancel.set()

    def run(self):
        try:
            r, c, info = ai_move(self.board, self.ai_player, self.depth,
                                 cancel=self._cancel)
        except Exception as exc:
            # 引擎异常不应让线程静默死掉、把 UI 永远卡在"思考中"
            self.finished.emit(-1, -1, {'reason': 'AI异常', 'best_val': 0.0,
                                        'detail': f'{type(exc).__name__}: {exc}'})
            return
        self.finished.emit(r, c, info)


# ==================== 加载界面 ====================
SPLASH_MS = 900     # 开场交接时长；真实启动成本约 115ms，见下
# 进度条步进间隔。15ms ≈ 66fps，肉眼连续；步数 60 也够让"在推进"看得清。
_SPLASH_TICK_MS = 15


class LoadingScreen(Screen):
    """开场交接动画。

    **进度条走的是"交接倒计时"而不是"加载进度"。** 真实启动成本约 115ms
    （``import numpy`` 85ms + ``import engine`` 16ms + ``new_game()`` 14ms），
    而其中最重的一步发生在 ``QApplication`` 构造**之前** —— 用户看到这一屏
    时，"最重的那件事"早已做完。所以这里不演百分比数字（旧版演 5000ms，与
    真实成本差 43 倍，是纯粹的谎言），只如实显示这屏还要停留多久：QTimer
    按 ``SPLASH_MS`` 推进 0→100。同理，那八条"正在加载开局库..."式的假步骤
    一并删除 —— 开局库在模块拆分时就没了，GPU 分支也恒为 CPU。

    **为什么保留而不是直接删掉。** 冷启动时用户已等了 OS 几百毫秒，硬切会
    显得突兀；而且这一屏是唯一能承载"未找到中文字体"警告的位置。

    视觉上是**棋盘的缩影**：一块木牌（材质与真棋盘同源，见
    ``board_render.brand_mark``）压住标题，进度条压在下面收尾。纯文字排版
    的问题是它可以是任何一个应用的开场页；而木牌一出现，"这是那个下棋的
    应用"在第一帧就成立了。
    """

    # 木牌边长。载荷只有网格与两子，超过这个尺寸线距会显空。
    _MARK_PX = 104

    def __init__(self, on_finished):
        super().__init__(title="五 子 棋", subtitle="Gomoku AI",
                         lead=BrandMark(self._MARK_PX), backdrop=True)
        self.on_finished = on_finished
        self.setup_ui()

    def setup_ui(self):
        self.progress_bar = QProgressBar()
        # 定量条（0→100）+ 主题 chunk。**不要**改成 setRange(0, 0)：不定量条
        # 在 Fusion 下会画成一整条默认蓝（既非主题色也无可见动画），
        # 而给它写 ::chunk 又会盖掉 busy 动画、条直接变空。详见 theme.py 注释。
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        # 5px：与 32px 标题、14px 副标题同处一屏时，6px 会显得像根横梁。
        self.progress_bar.setFixedHeight(5)
        self.progress_bar.setFixedWidth(300)
        self.add_content(self.progress_bar)

        self._elapsed = QElapsedTimer()
        self._elapsed.start()
        self._tick = QTimer(self)
        self._tick.setInterval(_SPLASH_TICK_MS)
        self._tick.timeout.connect(self._advance)
        self._tick.start()

        if not theme.has_cjk_font():
            self.add_footer(faint_label(
                "未找到简体中文字体，界面可能显示为方块；"
                "Linux 请安装 fonts-noto-cjk"))

    def _advance(self):
        """按 SPLASH_MS 线性推进进度条；走满即停表并交接。

        用已流逝的墙钟而非累加步数：QTimer 在系统繁忙时会丢拍，累加会让
        进度条慢于实际交接时刻，出现"跳到 80% 就切页"的观感。
        """
        elapsed = self._elapsed.elapsed()
        if elapsed >= SPLASH_MS:
            self._tick.stop()
            self.progress_bar.setValue(100)
            self.on_finished()
            return
        self.progress_bar.setValue(int(elapsed * 100 / SPLASH_MS))


# ==================== 游戏棋盘组件 ====================
class BoardWidget(QWidget):
    """棋盘绘制组件。

    **自身不涂任何背景。** 木盘是圆角的，控件矩形四角那四块深色三角必须由父
    容器透出来 —— 一旦给这个控件设了 QSS 背景或 ``WA_StyledBackground``，圆角
    就没有意义了，整个控件会变成一个方方正正的橙色块。

    绘制分三层（详见 ``board_render``）：静态层与棋子层各自缓存成 pixmap，
    ``paintEvent`` 里只剩两次 ``drawPixmap`` 加最后手环／悬停幽灵两个小图形。
    """

    def __init__(self):
        super().__init__()
        self.board = np.zeros((BOARD_SIZE, BOARD_SIZE), dtype=int)
        self.last_move = None  # (r, c, player)
        self.hover_pos = None
        self.hover_player = 1  # 幽灵子显示谁的颜色
        self.geom = _DESIGN_GEOM      # 像素<->格子的唯一真源
        self._static = None           # 缓存：静态层
        self._stones = None           # 缓存：棋子层
        self._cache_key = None        # (w, h, dpr) —— 变化即重建
        self._stones_dirty = True
        # 落子动画：正在动画的那颗子**不在**棋子缓存层里，由 paintEvent
        # 覆盖绘制（下落位移 + 淡入），结束时并回缓存层。
        self._anim = None             # QVariantAnimation
        self._anim_cell = None        # (r, c, player)
        self.win_cells = []           # 终局五连 [(r, c), ...]
        self.win_player = 0
        # 下限与窗口下限同源：MIN_BOARD 正是「格距恰好 MIN_CELL」。写死一个
        # 360 会让两处下限脱钩 —— 窗口允许缩到棋盘只剩 360 宽时，格距掉到
        # 16.9，坐标标注直接糊没。
        self.setMinimumSize(MIN_BOARD, MIN_BOARD)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMouseTracking(True)

    def cell_center(self, r, c) -> QPoint:
        """格点 ``(r, c)`` 的中心在控件内的像素坐标。

        供派发鼠标事件使用（``_on_board_click`` / 无头冒烟测试）。**不要在
        调用方自己算 ``MARGIN + c * CELL_SIZE``** —— 那个公式只在设计尺寸下
        成立，棋盘一旦可缩放就会把点击投到错误的格子上。
        """
        x, y = self.geom.px(r, c)
        return QPoint(round(x), round(y))

    def set_board(self, board):
        self.board = board.copy()
        self._stones_dirty = True
        self.update()

    def set_last_move(self, r, c, player):
        if r is None or c is None:
            self.last_move = None
            self._cancel_stone_anim()   # 悔棋/重置：取消在途的落子动画
        else:
            self.last_move = (r, c, player)
            self._start_stone_anim(r, c, player)
        self.update()          # 只画一个环，不必重建棋子层

    def set_win_cells(self, cells, player):
        """终局五连高亮（``cells`` 来自 ``engine.win_line``）。"""
        self.win_cells = list(cells) if cells else []
        self.win_player = player
        self.update()

    # ---- 落子动画 ----

    def _cancel_stone_anim(self):
        """立刻终止在途动画并把该子并回缓存层。

        Qt 的 ``stop()`` 在动画**未到终点**时不发 ``finished``（只有走完
        duration 才发），所以收尾必须手动做 —— 否则悔棋后那颗已不存在的子
        会继续被排除在缓存层外、被动画层绘制 160ms。
        """
        if self._anim is None:
            return
        self._anim.stop()
        self._anim = None
        cell = self._anim_cell[:2] if self._anim_cell else None
        self._anim_cell = None
        self._stones_dirty = True
        if cell is not None:
            self._repaint_cell(cell)

    def _start_stone_anim(self, r, c, player):
        """启动落子的覆盖式动画。

        动画期间该子被 ``render_stones(exclude=...)`` 排除出缓存层，
        ``paintEvent`` 在缓存层之上单独绘制它（位移 + 淡入）；结束时
        重建缓存层把它并回去 —— 结束帧与静态帧逐像素重合，无跳变。

        **先收掉旧动画再置新状态**：AI 极快响应时上一手动画可能还没放完，
        旧子必须先并回缓存层，否则它会在两段动画的间隙里凭空消失。
        """
        self._cancel_stone_anim()
        self._anim_cell = (r, c, player)
        self._stones_dirty = True
        anim_obj = QVariantAnimation(self)
        anim_obj.setDuration(anim.STONE_MS)
        anim_obj.setStartValue(0.0)
        anim_obj.setEndValue(1.0)
        anim_obj.setEasingCurve(QEasingCurve.OutCubic)
        cell = (r, c)
        anim_obj.valueChanged.connect(lambda _: self._repaint_cell(cell))
        anim_obj.finished.connect(lambda: self._finish_stone_anim(cell))
        self._anim = anim_obj
        anim_obj.start()

    def _finish_stone_anim(self, cell):
        """动画收尾：该子并回缓存层。"""
        self._anim = None
        self._anim_cell = None
        self._stones_dirty = True
        self._repaint_cell(cell)

    def set_hover_player(self, player):
        """设置幽灵子的颜色（``1``/``2``）；传 ``None`` 表示当前不该有悬停预览
        （AI 思考中、对局已结束）。"""
        if player != self.hover_player:
            self.hover_player = player
            self.update()

    def resizeEvent(self, event):
        # 几何随控件尺寸等比缩放，网格重新居中且保持正方。缓存全部作废。
        self.geom = BoardGeometry.fit(self.width(), self.height(), _DESIGN_GEOM)
        self._static = None
        self._stones = None
        self._cache_key = None
        self._stones_dirty = True
        super().resizeEvent(event)

    def _ensure_cache(self):
        """按 (尺寸, DPR) 惰性重建缓存。DPR 变化（拖到另一块屏）也走这里。"""
        dpr = self.devicePixelRatioF()
        key = (self.width(), self.height(), round(dpr, 2))
        if key == self._cache_key:
            return
        self._cache_key = key
        self._static = board_render.render_static(
            self.geom, self.width(), self.height(), dpr)
        self._stones = None
        self._stones_dirty = True

    def _repaint_cell(self, cell):
        """只重绘一个格子（含棋子向外溢出的投影余量）。"""
        if cell is None:
            return
        x, y, w, h = self.geom.cell_rect(cell[0], cell[1])
        pad = self.geom.cell * 0.5
        self.update(QRect(int(x - pad), int(y - pad),
                          int(w + 2 * pad), int(h + 2 * pad)))

    def mouseMoveEvent(self, event: QMouseEvent):
        # 同格内移动直接返回 —— 旧实现是无条件整盘重绘，鼠标每动一像素就
        # 重建上百个渐变对象。
        cell = self.geom.to_grid(event.x(), event.y())
        if cell == self.hover_pos:
            return
        old, self.hover_pos = self.hover_pos, cell
        self._repaint_cell(old)
        self._repaint_cell(cell)

    def leaveEvent(self, event):
        old, self.hover_pos = self.hover_pos, None
        self._repaint_cell(old)

    def paintEvent(self, event):
        self._ensure_cache()
        painter = QPainter(self)

        painter.drawPixmap(0, 0, self._static)

        if self._stones_dirty or self._stones is None:
            exclude = self._anim_cell[:2] if self._anim_cell else None
            self._stones = board_render.render_stones(
                self.geom, self.width(), self.height(),
                self.devicePixelRatioF(), self.board, exclude=exclude)
            self._stones_dirty = False
        painter.drawPixmap(0, 0, self._stones)

        painter.setRenderHint(QPainter.Antialiasing, True)
        r = self.geom.stone_radius

        # 落子动画：正在动画的子不在缓存层里，带下落位移与淡入覆盖绘制。
        if self._anim_cell is not None:
            ar, ac, ap = self._anim_cell
            x, y = self.geom.px(ar, ac)
            t = float(self._anim.currentValue()) if self._anim is not None else 1.0
            drop = anim.STONE_DROP_PX * self.geom.k * (1.0 - t)
            alpha = anim.STONE_FADE_FROM + (1.0 - anim.STONE_FADE_FROM) * t
            d = r * 2.0
            sprite = board_render.stone_sprite(ap, d, self.devicePixelRatioF())
            half = d * board_render._SPRITE_SCALE / 2.0
            painter.setOpacity(alpha)
            painter.drawPixmap(QPointF(x - half, y - half - drop), sprite)
            painter.setOpacity(1.0)

        # 终局五连：贯穿光带 + 每颗子一个环（色同最后一手环 —— 墨色只有一份）
        if self.win_cells:
            first, last = self.win_cells[0], self.win_cells[-1]
            x0, y0 = self.geom.px(*first)
            x1, y1 = self.geom.px(*last)
            win_col = QColor(theme.LAST_LIGHT if self.win_player == 1
                             else theme.LAST_DARK)
            band = QColor(win_col)
            band.setAlpha(80)
            band_pen = QPen(band, r * 1.1)
            band_pen.setCapStyle(Qt.RoundCap)
            painter.setBrush(Qt.NoBrush)
            painter.setPen(band_pen)
            painter.drawLine(QPointF(x0, y0), QPointF(x1, y1))
            ring = QPen(win_col, max(2.0, r * 0.26))
            painter.setPen(ring)
            for rr, cc in self.win_cells:
                x, y = self.geom.px(rr, cc)
                painter.drawEllipse(QPointF(x, y), r * 0.78, r * 0.78)

        # 最后一手：与棋子**反色**的环。旧的固定红圈叠在白子上只有 2.1:1，
        # 叠在黑子上也发闷。
        if self.last_move:
            lr, lc, player = self.last_move
            x, y = self.geom.px(lr, lc)
            color = theme.LAST_LIGHT if player == 1 else theme.LAST_DARK
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor(color), max(1.5, r * 0.22)))
            painter.drawEllipse(QPointF(x, y), r * 0.72, r * 0.72)

        # 悬停：幽灵子（半透明的"你将落下的那颗子"）。旧的灰盘压在木色上
        # 几乎看不见。描边用棋盘墨色 GRID（浅色主题的 ACCENT 对木色不够）。
        if (self.hover_player and self.hover_pos
                and self.board[self.hover_pos[0]][self.hover_pos[1]] == 0):
            hr, hc = self.hover_pos
            x, y = self.geom.px(hr, hc)
            base = theme.STONE_B if self.hover_player == 1 else theme.STONE_W
            ghost = QColor(base)
            ghost.setAlpha(120)
            painter.setPen(QPen(QColor(theme.GRID), max(1.0, r * 0.12)))
            painter.setBrush(QBrush(ghost))
            painter.drawEllipse(QPointF(x, y), r, r)

        painter.end()

    def get_grid_pos(self, screen_x, screen_y):
        """控件内坐标转棋盘格点；不在棋盘上则返回 None。

        唯一实现在 ``BoardGeometry.to_grid``。历史上这里有第二份公式副本，
        与 ``mouseMoveEvent`` 各自演化，是"悬停亮点与实际落点不一致"的来源。
        """
        return self.geom.to_grid(screen_x, screen_y)


# ==================== 游戏面板（右侧） ====================
class GamePanel(QFrame):
    """右侧信息面板。

    继承 ``QFrame`` 而不是 ``QWidget``：QFrame 是 Qt 已知类，QSS 的
    ``background`` 直接生效，不需要 ``WA_StyledBackground`` 那个补丁。
    """

    theme_clicked = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setProperty("role", "panel")
        self.setFixedWidth(PANEL_W)
        self._thinking = False
        self._pulse_anim = None      # AI 思考的呼吸动画（须持有，否则被 GC）
        self._elapsed = 0
        # 图表序列（AI 视角的分值 + 点的来路）。**挂在面板上而不是
        # GomokuGame 上**：面板每局新建，序列因此自动清零；挂到游戏对象上
        # 会让新局接着显示上一局已经作废的历史。
        self._series: list = []
        self._decade = 2             # 评分图纵轴的数量级，只增不减
        self.setup_ui()

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick_timer)

    def setup_ui(self):
        """节奏：内边距 XL(24)；四段之间 LG(16)；段内行距 SM(8)。

        ``GamePanel`` **不继承 ``Screen``**：Screen 是"居中、全屏、上下留白"，
        面板是"贴边、定宽、四段式"，硬套会让两端都别扭。体系里共享 token 与
        原语、不共享骨架 —— 两套排布，一套度量。
        """
        layout = QVBoxLayout(self)
        layout.setContentsMargins(theme.SPACE_XL, theme.SPACE_XL,
                                  theme.SPACE_XL, theme.SPACE_XL)
        layout.setSpacing(theme.SPACE_LG)

        # 段 1：标题行（标题 + 主题切换）
        head = QHBoxLayout()
        head.setSpacing(theme.SPACE_SM)
        head.addWidget(title_label("五子棋 AI", role="panel-title"),
                       1, Qt.AlignVCenter)
        self.theme_btn = button(self._theme_btn_text(), "ghost", height=32)
        self.theme_btn.setFixedWidth(40)
        self.theme_btn.setToolTip("切换深色 / 浅色主题")
        self.theme_btn.clicked.connect(self.theme_clicked.emit)
        head.addWidget(self.theme_btn, 0, Qt.AlignVCenter)
        layout.addLayout(head)

        # 段 2：回合指示卡（面板里最醒目的一块）
        self.turn_indicator = TurnIndicator()
        layout.addWidget(self.turn_indicator)

        # 段 3：信息
        info = QVBoxLayout()
        info.setSpacing(theme.SPACE_SM)
        self.difficulty_row = InfoRow("AI 难度", "—")
        # 这一行是**诊断**用途，不是玩法信息：C++ 服务端没编译/没启动/中途挂了
        # 时棋局照常继续（静默降级到本地引擎），用户唯一的感知就是 AI 变慢、
        # 变弱。不把当前生效的引擎摆出来，"为什么刚才那步只要 0.1 秒、这步要 7
        # 秒"就无从解释。取值来自 engine.engine_label()，只读不探测。
        self.engine_row = InfoRow("引擎", "—")
        # 端口是**诊断**的下一格：引擎那一行只说"用了谁"，不说"在哪"。
        # 池子里 15 格、还可能退到内核端口，出问题时（某个端口被别的程序占着、
        # 或者旧版本程序的残留服务端还活着）第一个要问的就是"它到底连在哪一格"。
        # 取值来自 engine.current_port()，与引擎那一行同源：只读已建立的连接。
        self.port_row = InfoRow("当前 TCP 端口", "—")
        self.port_row.setToolTip(
            "当前这条 C++ 引擎连接用的端口。\n"
            "「—」= 这一步没走 TCP：开局库查表（不搜索），"
            "或引擎已降级到本地 Python。")
        self.undo_row = InfoRow("悔棋次数", "3")
        self.moves_row = InfoRow("步数", "0")
        self.time_row = InfoRow("用时", "00:00")
        for row in (self.difficulty_row, self.engine_row, self.port_row,
                    self.undo_row, self.moves_row, self.time_row):
            info.addWidget(row)
        layout.addLayout(info)

        # 段 4：图表（原先这里是 addStretch(1) 的一块空白）
        #
        # 两张图共用 GamePanel._series 这一条序列，只是变换不同：上面是
        # 原始分值（symlog 纵轴），下面是换算出的胜率（0–100% 纵轴）。
        # 数据接线在 `GomokuGame._record_score`，**不在 _update_panel** ——
        # 后者是渲染函数，每手会跑 2–3 次，在那里追点会重复。
        self.score_chart = charts.ScoreChart()
        self.win_chart = charts.WinRateChart()
        for chart in (self.score_chart, self.win_chart):
            chart.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
            layout.addWidget(chart, 1)

        # 段 5：操作
        self.undo_btn = button("↩ 悔棋", "ghost")
        self.restart_btn = button("🔄 重新开始", "primary")
        self.quit_btn = button("✕ 退出游戏", "danger")
        for btn in (self.undo_btn, self.restart_btn, self.quit_btn):
            layout.addWidget(btn)

    @staticmethod
    def _theme_btn_text() -> str:
        """图标指向"点一下会变成的样子"：深色中显示太阳，浅色中显示月亮。"""
        return "🌙" if theme.current_theme() == "light" else "☀"

    def update_theme_button(self) -> None:
        self.theme_btn.setText(self._theme_btn_text())
        # 图表是自绘的，颜色在 paint 时取 theme.*，所以主题切换后必须重画
        # 一次 —— QSS 的 repolish 管不到 QPainter。
        for chart in (self.score_chart, self.win_chart):
            chart.update()

    # ---- 图表序列 ----
    #
    # 两条约束，都只在这里成立一次：
    #   1. 序列与 `GomokuGame.move_history` **严格同长** —— 悔棋靠 truncate 同步。
    #   2. 数量级只增不减 —— 否则轴会随分值回落而收缩，同一条曲线在下一手看
    #      起来会突然"变陡"，而那是轴在动、不是棋在动。

    def push_score(self, value: float, kind: str) -> None:
        """追一个点。``kind`` 见 ``charts.SEARCH`` / ``charts.STATIC``。"""
        value = float(value)
        self._series.append((value, kind))
        self._decade = max(self._decade, analysis.needed_decade(value))
        self._refresh_charts()

    def truncate_series(self, n: int) -> None:
        """把序列截回 ``n`` 个点（悔棋）。**不缩数量级** —— 见上面第 2 条。"""
        del self._series[n:]
        self._refresh_charts()

    def set_readout(self, text: str) -> None:
        """搜索参数读数（评分卡的第三行）。"""
        self.score_chart.set_readout(text)

    def _refresh_charts(self) -> None:
        self.score_chart.set_decade(self._decade)
        self.score_chart.set_series(self._series)
        self.win_chart.set_series(self._series)

    # ---- 状态更新 ----

    def update_info(self, turn, difficulty, status, undo_count, move_count,
                    human=1):
        players = {1: "黑棋 ●", 2: "白棋 ○"}
        # 显示档位**名称**而不是"N 级"。档位号重构后从 3 档变成 5 档，旧编号
        # 已经没有稳定含义；名称直接来自 engine.DIFFICULTY 那一张表，改表即改
        # 界面，不会出现"界面上写 3 级、代码里是中级"这种两处对不上的情形。
        self.difficulty_row.set_value(engine.difficulty_name(difficulty))
        self.engine_row.set_value(engine.engine_label())
        port = engine.current_port()
        self.port_row.set_value(str(port) if port else "—")
        self.undo_row.set_value(str(undo_count))
        self.moves_row.set_value(str(move_count))
        if status == "进行中":
            if not self._thinking:
                if turn == human:
                    self.turn_indicator.set_turn(turn, "轮到你落子")
                else:
                    self.turn_indicator.set_turn(turn,
                                                 f"{players[turn]} 行动中")
        else:
            tone = {"你赢了！": "win", "你输了！": "lose"}.get(status, "")
            stone = human if tone == "win" else (3 - human)
            self.turn_indicator.set_result(stone, status, tone)
            self.stop_timer()        # 终局停表

    def show_thinking(self, show=True, ai_stone=2):
        """AI 思考态：指示卡转蓝 + 呼吸动画。"""
        self._thinking = show
        if show:
            self.turn_indicator.set_thinking(ai_stone)
            if self._pulse_anim is None:
                self._pulse_anim = anim.pulse(self.turn_indicator)
        else:
            if self._pulse_anim is not None:
                anim.stop_pulse(self.turn_indicator)
                self._pulse_anim = None

    # ---- 计时器 ----

    def reset_timer(self):
        self._elapsed = 0
        self.time_row.set_value("00:00")
        self._timer.start()

    def stop_timer(self):
        self._timer.stop()

    def _tick_timer(self):
        self._elapsed += 1
        m, s = divmod(self._elapsed, 60)
        self.time_row.set_value(f"{m:02d}:{s:02d}")


# ==================== 选择界面 ====================
class SelectionScreen(Screen):
    """执棋颜色 / AI难度选择。

    两个模式共用 ``Screen`` 的骨架与节奏，差异只剩标题文案与卡片行 —— 历史上
    两条分支各自抄了一份 stretch/spacing（一个 30 一个 25，没有理由）。
    """

    color_selected = pyqtSignal(int)  # 0=黑先, 1=白后
    difficulty_selected = pyqtSignal(int)  # 档位 1-5，对应 engine.DIFFICULTY

    def __init__(self, mode="color"):
        self.mode = mode
        self._cards = []
        if mode == "color":
            super().__init__(title="选择执棋颜色",
                             subtitle="黑棋为先手，白棋为后手",
                             backdrop=True)
        else:
            super().__init__(title="选择 AI 难度",
                             subtitle="难度越高，AI 思考越深入",
                             backdrop=True)
        self.setup_ui()

    def setup_ui(self):
        if self.mode == "color":
            # 卡面直接放那颗子本身（黑 = player 1），不再用 ⚫/⚪ 字符 ——
            # 那两个字符由 CJK 字体回退渲染成一个小圆点，既不是棋子也不是
            # 那个颜色，是这张卡片最关键的区分信息却最看不清的地方。
            cards = [("黑棋", "black", 0, 1, "先手"),
                     ("白棋", "white", 1, 2, "后手")]
            for i, (text, tone, value, player, sub) in enumerate(cards):
                btn = card_button(text, tone, face=StoneFace(player), sub=sub,
                                  index=f"{i + 1:02d}")
                btn.clicked.connect(lambda _=False, v=value:
                                    self.color_selected.emit(v))
                self._cards.append(btn)
        else:
            # 副标题**曾经写的是"搜索深度 1/2/3"**，那是假的：三个档位的搜索
            # 深度上限是 4/10/24，实测到的是 4/4/5（见 tools/BASELINE.md）。
            # 改报思考时限 —— 它是 engine.DIFFICULTY 里真实存在、且用户能直接
            # 感知的量（"AI 要想多久"）。
            #
            # 卡片文案**从 engine.DIFFICULTY 读**，不再在这里抄一份数字。旧代码
            # 把 "3/7/15" 硬编码在卡片上，而引擎里的真实值恰好也是 3/7/15 ——
            # 两处对得上纯属巧合。扩到 5 档时这种巧合必然失守，而失守的表现是
            # "界面上写着 9 秒、AI 实际想了 20 秒"，没有任何测试会报警。
            #
            # 配色只有四种 tone（primary/success/danger/ghost），五档必然重复
            # 一个。重复选在最后两档：区分它们的仍是强度条（4 颗子 vs 5 颗子）
            # 与文字，而颜色只承担"越往下越亮"的辅助作用。
            tones = ("ghost", "success", "primary", "danger", "danger")
            levels = sorted(engine.DIFFICULTY)
            assert len(tones) == len(levels), "难度卡配色与档位数不同步"
            for level, tone in zip(levels, tones):
                # N 颗子当强度条 —— 用的是棋盘上那套材质，不是另画一个图标。
                btn = card_button(engine.difficulty_name(level), tone,
                                  face=stone_row([1] * level),
                                  sub="思考上限 %g 秒" % engine.DIFFICULTY[level]["time"],
                                  index=f"{level:02d}")
                btn.clicked.connect(lambda _=False, l=level:
                                    self.difficulty_selected.emit(l))
                self._cards.append(btn)

        # 5 张卡在 SPACE_XL(24) 下是 5×160+4×24 = 896px，仍塞得进 WINDOW_W=1022
        # —— 但只剩 126px 余量，而卡片是 setFixedSize 的（不随窗口缩放），
        # 颜色页那种"留白富余"的观感会被挤掉。降到 SPACE_LG(16) 得 864px。
        # 只调难度页：颜色页只有 2 张卡，宽间距是那张页面的节奏，没理由跟着改。
        gap = theme.SPACE_LG if self.mode != "color" else theme.SPACE_XL
        self.add_content(hbox(*self._cards, spacing=gap))


# ==================== 游戏结束覆盖层 ====================
class GameOverOverlay(Screen):
    """游戏结束遮罩。

    继承 ``Screen`` 并把 ``objectName`` 换成 ``overlayRoot`` —— 半透明底由
    ``theme`` 的 ``QWidget#overlayRoot`` 规则给。

    **不要再 setStyleSheet 上色。** 旧的 ``setStyleSheet("background: rgba(...)")``
    没有选择器，Qt 会把它传播给全部子控件，于是"结果文字"和两个按钮各自被刷成
    一块深色圆角方块，而遮罩本身反而不铺满。这就是窗口级样式表的同一个陷阱。
    """

    restart_clicked = pyqtSignal()
    quit_clicked = pyqtSignal()

    def __init__(self, result_text, is_win):
        super().__init__(root_name="overlayRoot")
        self.result_text = result_text
        self.is_win = is_win
        self.setup_ui()

    def setup_ui(self):
        result = title_label(self.result_text)
        result.setProperty("tone", "win" if self.is_win else "lose")
        self.add_content(result)

        restart_btn = button("🔄 再来一局", "success", width=150)
        restart_btn.clicked.connect(self.restart_clicked.emit)
        quit_btn = button("✕ 退出游戏", "danger", width=150)
        quit_btn.clicked.connect(self.quit_clicked.emit)
        self.add_content(hbox(restart_btn, quit_btn, spacing=theme.SPACE_LG))


# ==================== 主窗口 ====================
class GomokuGame(QMainWindow):
    """主游戏窗口"""

    @staticmethod
    def _initial_size() -> tuple:
        """按屏幕可用区算初始尺寸。

        **不要写回 setFixedSize。** 1366×768 这类屏幕上设计尺寸 WINDOW_W×WINDOW_H
        连标题栏一起是摆不下的，定死会让窗口底部（退出按钮）掉到屏幕外，而且
        用户无法挽救 —— 窗口既不能缩也不能拉。

        上限取 ``MAX_SCALE``：4K 屏上按可用区铺满的话棋盘大到需要转头看，而
        棋盘的可用性上限来自"一眼能看全 19 路"。**不设下限** —— 那由
        ``setMinimumSize`` 负责，两处各管一头。
        """
        screen = QApplication.primaryScreen()
        if screen is None:
            return (WINDOW_W, WINDOW_H)
        avail = screen.availableGeometry()
        # 留 8% 余量，避免贴着屏幕边缘（任务栏、窗口阴影、部分 WM 的吸附区）。
        k = min(MAX_SCALE,
                (avail.width() * 0.92) / WINDOW_W,
                (avail.height() * 0.92) / WINDOW_H)
        k = max(k, 1.0)
        return (int(WINDOW_W * k), int(WINDOW_H * k))

    def _center_on_screen(self):
        """把窗口摆到所在屏幕可用区中央。"""
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        frame = self.frameGeometry()
        frame.moveCenter(screen.availableGeometry().center())
        self.move(frame.topLeft())

    def showEvent(self, event):
        """首次显示时居中。

        放在 showEvent 而不是 ``__init__``：``frameGeometry()`` 在窗口还没被
        窗口管理器加上标题栏／边框之前是不可信的，构造期算出来的中心会偏。
        """
        super().showEvent(event)
        if not self._centered:
            self._centered = True
            self._center_on_screen()

    def __init__(self):
        super().__init__()
        # 全局 QSS 必须早于任何 widget 构造。放在这里而不是 main()：
        # gui_smoke 自建 QApplication 后直接构造本窗口、不走 main()，
        # 装在这儿离屏冒烟才会真的执行这套样式。
        theme.install()

        self.setWindowTitle("五子棋 AI")
        self.setMinimumSize(MIN_W, MIN_H)
        self.resize(*self._initial_size())
        self._centered = False      # 只在首次 show 时居中一次
        # 这里**不要**再写 setStyleSheet("background-color: ...")。Qt 会把
        # 控件级样式表传播给全部子控件，且优先级高于应用级 —— 一条无选择器的
        # 背景色会把进度条的槽、面板底色一起刷掉（实测槽色直接消失）。
        # 窗口底色由 theme 的 QMainWindow 规则统一给。

        # 游戏状态变量
        self.board = np.zeros((BOARD_SIZE, BOARD_SIZE), dtype=int)
        self.move_history = []  # 每步落子后保存棋盘快照
        self.gamemode = 0  # 0=先手(黑), 1=后手(白)
        self.gameplayer = 1
        self.gamekunnan = 1
        self.gamerule = 3  # 1=输, 2=赢, 3=进行中
        self.output = 3  # 悔棋次数
        self.move_count = 0
        self.game_over = False
        self.ai_thinking = False
        self.ai_first_move_done = False
        self.last_move = None

        # AI Worker
        self.ai_worker = None
        self._ai_generation = 0     # 每次发起搜索递增，用于丢弃陈旧结果

        # 游戏日志
        self.logger = None

        # 中央容器
        self.central = QStackedWidget()
        self.setCentralWidget(self.central)

        # 各页面
        self.loading_screen = None
        self.selection_color = None
        self.selection_difficulty = None
        self.game_widget = None
        self.board_widget = None
        self.game_panel = None
        self.game_over_overlay = None

        self._init_loading()

    def _init_loading(self):
        """初始化加载界面"""
        self.loading_screen = LoadingScreen(on_finished=self._on_loading_finished)
        self.central.addWidget(self.loading_screen)
        self.central.setCurrentWidget(self.loading_screen)

    def _on_loading_finished(self):
        """加载完成，进入主菜单。

        开场只有 900ms，用户（或冒烟测试）可能已经抢先切走了 —— 那样这个
        迟到的回调会把界面**拽回**颜色选择页。所以在推进前先确认加载页仍是
        当前页；`central.currentWidget()` 在页面被删后为 None，也一并挡住。
        """
        if self.central.currentWidget() is not self.loading_screen:
            return
        self._show_color_selection()

    def _drop_pages(self):
        """切页时回收旧页面。

        旧代码每个 ``_show_*`` 都是 ``central.addWidget(...)`` 却从不移除 ——
        每重开一局就多留一整棵 widget 树（棋盘连同它的两层 pixmap 缓存）在
        ``QStackedWidget`` 里，永远不会被回收。

        **置空引用和 deleteLater 同样重要。** ``deleteLater`` 只是排队删除，
        C++ 对象随即失效；若 ``self.board_widget`` 仍指向它，之后任何访问都会
        抛 ``RuntimeError: wrapped C/C++ object has been deleted``，而它通常
        发生在 Qt 槽里 —— 直接崩。

        调用方必须**先** ``_cancel_ai()``：AI 线程的结果回调会碰棋盘状态，
        在已删除的 widget 上写日志会抛 ValueError。
        """
        for attr in ("loading_screen", "selection_color",
                     "selection_difficulty", "game_widget"):
            page = getattr(self, attr, None)
            if page is not None:
                self.central.removeWidget(page)
                page.deleteLater()
        # board_widget / game_panel / overlay 是 game_widget 的子控件，
        # 随父一起销毁，不需要（也不能）单独 removeWidget。
        for attr in ("loading_screen", "selection_color", "selection_difficulty",
                     "game_widget", "board_widget", "game_panel",
                     "game_over_overlay", "_stack"):
            setattr(self, attr, None)

    def _switch_page(self, page):
        """统一收口的切页：入栈 + 置当前 + 180ms 淡入。

        加载页→颜色页→难度页→游戏页共 4 处切换点，全部走这里 —— "切页有
        过渡"是结构保证的，不是逐处手抄出来的。
        """
        self.central.addWidget(page)
        self.central.setCurrentWidget(page)
        anim.fade_in(page)

    def _show_color_selection(self):
        """显示执棋颜色选择"""
        self._drop_pages()
        self.selection_color = SelectionScreen(mode="color")
        self.selection_color.color_selected.connect(self._on_color_selected)
        self._switch_page(self.selection_color)

    def _on_color_selected(self, mode):
        """选择了执棋颜色"""
        self.gamemode = mode
        self._show_difficulty_selection()

    def _show_difficulty_selection(self):
        """显示难度选择"""
        self.selection_difficulty = SelectionScreen(mode="difficulty")
        self.selection_difficulty.difficulty_selected.connect(self._on_difficulty_selected)
        self._switch_page(self.selection_difficulty)

    def _on_difficulty_selected(self, level):
        """选择了难度，开始游戏"""
        self.gamekunnan = level
        self._start_game()

    def _start_game(self):
        """初始化游戏"""
        # 关闭上局日志
        if self.logger:
            self.logger.close()
        self.logger = GameLogger()
        self.logger.f.write(f"  模式: {'玩家先手(黑)' if self.gamemode == 0 else 'AI先手(黑), 玩家后手(白)'}\n")
        # 记档位名称与 C++ 可执行文件的**可用性**。后者是这份日志里唯一能回答
        # "刚才那局为什么 AI 又慢又弱"的静态信息 —— 一份日志事后翻出来，用时
        # 一列 0.5 秒和 7 秒的差别只能靠它解释。注意措辞：这一行说的是"能不能
        # 用 C++"，而不是"实际用了谁"（那要等第一次搜索之后才有答案）。
        self.logger.f.write(f"  难度: {engine.difficulty_name(self.gamekunnan)}\n")
        self.logger.f.write(f"  引擎: {engine.binary_path() or 'C++ 不可用，将用本地 Python 引擎'}\n\n")
        self.logger.f.flush()

        self.board = np.zeros((BOARD_SIZE, BOARD_SIZE), dtype=int)
        self.move_history = []  # 每步落子后保存棋盘快照
        self.gameplayer = 1
        self.gamerule = 3
        self.output = 3
        self.move_count = 0
        self.game_over = False
        self.ai_thinking = False
        self.ai_first_move_done = False
        self.last_move = None

        # 清空引擎的跨局面状态（置换表 / history / killer）。
        # 原版在这里重建 main.py 的模块级全局；引擎改为提供显式入口，
        # 状态不再散落在模块级别。
        new_game()
        # 代数只增不减：重置回 0 反而危险 —— 上一局某个"迟到"的结果可能正好
        # 持有重置后才会出现的编号，于是被当成当前局的合法结果放行。
        self._ai_generation += 1     # 新局作废上一局的一切在途结果

        # 构建游戏界面
        self._build_game_ui()

    def _build_game_ui(self):
        """构建游戏主界面。

        节奏：``GAP | 棋盘 | SPACE_SM | 面板 | GAP``，纵向 ``GAP | 棋盘 | GAP``。
        于是棋盘在窗口里正好是 ``BOARD_PX`` 见方（设计基准 1:1），不再需要给
        ``BoardWidget`` 写死尺寸 —— 缩放交给 ``BoardGeometry.fit()``。

        刻意**没有**棋盘外面的包装容器（旧代码有一层刷成木色的
        ``board_wrapper``，给棋盘做左侧圆角）：木盘自己就是圆角的，外面再套一
        层等大的木色只会把圆角外的深色三角填满，看起来是个方正的橙块。
        """
        self.game_widget = QWidget()
        self.game_widget.setObjectName("screenRoot")
        self.game_widget.setAttribute(Qt.WA_StyledBackground, True)

        self.board_widget = BoardWidget()
        self.board_widget.set_board(self.board)
        self.board_widget.mousePressEvent = self._on_board_click

        self.game_panel = GamePanel()
        self.game_panel.undo_btn.clicked.connect(self._on_undo)
        self.game_panel.restart_btn.clicked.connect(self._on_restart)
        self.game_panel.quit_btn.clicked.connect(self._on_quit)
        self.game_panel.theme_clicked.connect(self._on_toggle_theme)
        self.game_panel.reset_timer()

        # 纯容器，**不要**给它 setStyleSheet：Qt 会把控件级样式表传播给全部
        # 子控件，一条无选择器的 background 会把面板底色、按钮底色全刷掉。
        # 裸 QWidget 本来就不画背景，什么都不用设。
        #
        # 窗口边距放在这个布局上，**不要指望 QStackedLayout 的
        # setContentsMargins**：实测它被忽略，子控件拿到的是控件全尺寸
        # （棋盘因此变成 750x750、k=1.033，不再是设计基准 1:1）。
        game_row = QWidget()
        row = QHBoxLayout(game_row)
        row.setContentsMargins(GAP, GAP, GAP, GAP)
        row.setSpacing(theme.SPACE_SM)
        row.addWidget(self.board_widget, 1)
        row.addWidget(self.game_panel, 0)

        # 高度下限按**面板的实测最小值**兜底，不能用模块常量 MIN_H：
        # 那个数依赖字体度量（刻度文字、读数行的高度）与平台控件尺寸，
        # 只有 QApplication 起来之后才量得准。这里量出来比 MIN_H 高就抬上去 ——
        # 抬不上去的后果是面板被挤，图表压成一条缝，而它不会报错。
        need_h = 2 * GAP + self.game_panel.minimumSizeHint().height()
        # 上限取设计高度 WINDOW_H：窗口最小高度一旦超过设计尺寸，小屏上的
        # `_initial_size()` 就压不下去（最小尺寸优先于它），窗口会连标题栏
        # 一起顶出屏幕。宁可面板挤一点，也不能让窗口装不下。
        need_h = min(need_h, WINDOW_H)
        if need_h > self.minimumHeight():
            self.setMinimumHeight(need_h)

        # 结算遮罩叠在同一块区域上。
        #
        # 旧代码把遮罩 addWidget 进一个 QVBoxLayout 后又 setGeometry(rect()) ——
        # 那是在和布局打架。它没露馅只是因为棋盘当时 setFixedSize 撑着容器最小
        # 高度，遮罩只能拿到 0 高度。棋盘一旦可缩放，容器最小高度塌陷，遮罩就
        # 会把棋盘挤成一半。QStackedLayout(StackAll) 让两者共用同一块几何，
        # 尺寸完全交给布局托管。
        self.game_over_overlay = None
        self._stack = QStackedLayout(self.game_widget)
        self._stack.setContentsMargins(0, 0, 0, 0)
        self._stack.setStackingMode(QStackedLayout.StackAll)
        self._stack.addWidget(game_row)

        self._switch_page(self.game_widget)

        self._update_panel()

        # AI先手
        if self.gamemode == 1:  # 玩家后手，AI先手
            self._ai_first_move()

    def _on_toggle_theme(self):
        """面板上的主题切换：换调色板 → QSS 重装 → 按钮图标翻转。

        棋盘色两套主题共享，board_render 的纹理/sprite/静态层缓存**不需要**
        作废 —— 切换是纯 QSS 操作，不会闪烁或卡顿。
        """
        theme.toggle_theme()
        self.game_panel.update_theme_button()

    def _ai_first_move(self):
        """AI先手的第一着，由 engine.opening_move 决定（确定性，无随机）。"""
        if not self.ai_first_move_done:
            self.ai_first_move_done = True
            # 这一手不经过 engine.ai_move（所以指示器不会自动更新），但面板上
            # 该显示的仍然是「开局库」：它是查表得来的天元，既不是 C++ 也不是
            # 降级后的 Python。不记的话这一行的初始值会一直是「—」。
            engine.note_book()
            mv = opening_move(self.board, 1)
            if mv is None:                      # 理论上不会发生
                mv = (BOARD_SIZE // 2, BOARD_SIZE // 2)
            r, c = mv
            self.board[r][c] = 1
            self.last_move = (r, c, 1)
            self.board_widget.set_board(self.board)
            self.board_widget.set_last_move(r, c, 1)
            self.move_count += 1
            self.move_history.append(self.board.copy())
            self._record_score(None)
            if self.logger:
                self.logger.log_ai(self.move_count, 1, r, c,
                    {'reason': 'AI先手-开局着法',
                     'detail': GameLogger.coord_to_sgf(r, c)})
            self._update_panel()

    def _on_board_click(self, event: QMouseEvent):
        """处理棋盘点击"""
        if self.game_over or self.ai_thinking:
            return

        pos = self.board_widget.get_grid_pos(event.x(), event.y())
        if pos is None:
            return
        r, c = pos
        if self.board[r][c] != 0:
            return

        # 玩家落子
        if self.gamemode == 0:
            self.board[r][c] = 1  # 玩家执黑
            player_stone = 1
            ai_stone = 2
        else:
            self.board[r][c] = 2  # 玩家执白
            player_stone = 2
            ai_stone = 1

        self.last_move = (r, c, player_stone)
        self.board_widget.set_board(self.board)
        self.board_widget.set_last_move(r, c, player_stone)
        self.move_count += 1
        self.move_history.append(self.board.copy())
        self._record_score(None)
        if self.logger:
            self.logger.log_human(self.move_count, player_stone, r, c)
            # 每隔约5步记录一次完整棋盘状态
            if self.move_count % 5 == 1 or self.move_count <= 3:
                self.logger.log_board_state(self.move_count, self.board)
        self._update_panel()

        # 检查玩家是否获胜
        if check_win(self.board, player_stone):
            self.gamerule = 2
            self.game_over = True
            self.board_widget.set_win_cells(
                win_line(self.board, player_stone), player_stone)
            self._show_game_over()
            return

        # 检查平局
        if self.move_count >= BOARD_SIZE * BOARD_SIZE:
            self.gamerule = 0
            self.game_over = True
            self._show_game_over()
            return

        # AI回合
        self._ai_turn(ai_stone)

    def _ai_turn(self, ai_stone):
        """AI回合"""
        self.ai_thinking = True
        self.game_panel.show_thinking(True, ai_stone)
        self.game_panel.undo_btn.setEnabled(False)

        self._ai_generation += 1
        gen = self._ai_generation
        self.ai_worker = AIWorker(self.board, ai_stone, self.gamekunnan)
        self.ai_worker.finished.connect(
            lambda r, c, info, g=gen: self._on_ai_finished(r, c, info, g))
        self.ai_worker.start()

    def _on_ai_finished(self, r, c, info=None, generation=None):
        """AI落子完成。

        generation 校验：重开局或悔棋会让上一局的 worker 结果"迟到"到达，
        不丢弃的话就会把旧局的棋子落到新棋盘上。
        """
        if generation is not None and generation != self._ai_generation:
            return          # 陈旧结果，丢弃
        if r < 0 or c < 0:
            # 引擎返回了错误哨兵
            self.ai_thinking = False
            self.game_panel.show_thinking(False)
            self.game_panel.undo_btn.setEnabled(True)
            print(f"[AI异常] {(info or {}).get('detail', '')}")
            return
        self.ai_thinking = False
        self.game_panel.show_thinking(False)
        self.game_panel.undo_btn.setEnabled(True)

        if self.gamemode == 0:
            ai_stone = 2
        else:
            ai_stone = 1

        self.board[r][c] = ai_stone
        self.last_move = (r, c, ai_stone)
        self.board_widget.set_board(self.board)
        self.board_widget.set_last_move(r, c, ai_stone)
        self.move_count += 1
        self.move_history.append(self.board.copy())
        self._record_score(info)

        # 记录AI决策日志。
        # 额外判一次 f.closed 是纵深防御：代数校验已经保证陈旧结果到不了这里，
        # 但真到了的话，往已关闭文件写会抛 ValueError —— 异常在 Qt 槽里传播
        # 会直接让整个程序崩溃。宁可少写一行日志，也不能崩掉用户的对局。
        if self.logger and not self.logger.f.closed:
            if info is None:
                info = {'reason': '未知'}
            self.logger.log_ai(self.move_count, ai_stone, r, c, info)
            # 每隔约5步记录棋盘状态（与人类步数错开）
            if self.move_count % 5 == 0 or info.get('reason') in ('威胁检测', '搜索-发现必胜'):
                self.logger.log_board_state(self.move_count, self.board,
                    f"AI={info.get('reason','')}")

        self._update_panel()

        # 检查AI是否获胜
        if check_win(self.board, ai_stone):
            self.gamerule = 1
            self.game_over = True
            self.board_widget.set_win_cells(
                win_line(self.board, ai_stone), ai_stone)
            self._show_game_over()
            return

        # 检查平局
        if self.move_count >= BOARD_SIZE * BOARD_SIZE:
            self.gamerule = 0
            self.game_over = True
            self._show_game_over()

    def _record_score(self, info=None):
        """把一个分值挂进面板的两张图。**三个 `move_history.append` 各调一次。**

        为什么不放进 ``_update_panel``：那是渲染函数，每手会跑 2–3 次
        （``_build_game_ui`` 在第一手之前、``_show_game_over`` 在获胜之后还会
        再来一次），在那里追点会产生幽灵点和重复点。

        取值分两条路，**图上用两种点区分**（见 ``charts``）：

        * 有可用的搜索结果 → ``info['best_val']``（AI 视角；杀棋分带内是
          搜索**证明**的，不是估计的）
        * 否则 → ``-evaluate(board, human)`` 的静态估值（无深度、无轮次概念）

        判定必须防御性：``info`` 有三个产出点且字段不全（空盘分支只有
        ``depth=0, best_val=0``），搜索也可能在 depth 1 之前就被 VCF 吃光预算。
        """
        best = None if info is None else info.get("best_val")
        # `or is_mate(best)` 不是装饰：VCF 已证明必胜、主循环在 depth 1 之前被
        # 取消时 best_val **就是**已证明的杀棋分而 depth == 0，丢掉它等于扔掉
        # 全局最强的证据。
        if best is not None and (info.get("depth", 0) > 0 or is_mate(best)):
            self.game_panel.push_score(best, charts.SEARCH)
            self.game_panel.set_readout(analysis.readout_line(info))
        else:
            # 求 player 视角再取负 = AI 视角（evaluate 严格零和，见 test_eval）。
            player = 1 if self.gamemode == 0 else 2
            static = -evaluate(Board.from_array(self.board), player)
            self.game_panel.push_score(static, charts.STATIC)
            self.game_panel.set_readout("")

    def _rewind(self, n: int) -> None:
        """弹出 ``n`` 步并让图表序列跟着退。

        "序列与 ``move_history`` 严格同长"这条不变量**只在这里**定义一次 ——
        三个悔棋分支各写一遍 `truncate_series` 迟早会漏掉一个。
        """
        for _ in range(n):
            if self.move_history:
                self.move_history.pop()
        self.game_panel.truncate_series(len(self.move_history))

    def _on_undo(self):
        """悔棋：撤回玩家最后一步及其后的AI回应（共2步）"""
        if self.game_over or self.ai_thinking:
            return
        if self.output <= 0:
            return
        if self.move_count == 0:
            return  # 棋局尚未开始，无法悔棋

        self.output -= 1

        if self.move_count >= 2:
            # 弹出最后两步（玩家 + AI）
            self._rewind(2)
            self.board = self.move_history[-1].copy() if self.move_history else np.zeros((BOARD_SIZE, BOARD_SIZE), dtype=int)
            self.move_count -= 2
        elif self.move_count == 1 and self.gamemode == 1:
            # AI先手的情况，撤回AI第一步，重下天元
            self._rewind(1)
            self.board = np.zeros((BOARD_SIZE, BOARD_SIZE), dtype=int)
            self.move_count = 0
            self.ai_first_move_done = False
            # _ai_first_move 自己会 _record_score(None)，所以这里不用补
            self._ai_first_move()
            self.board_widget.set_board(self.board)
            self._update_panel()
            return
        elif self.move_count == 1:
            self._rewind(1)
            self.board = np.zeros((BOARD_SIZE, BOARD_SIZE), dtype=int)
            self.move_count = 0

        self.last_move = None
        self.board_widget.set_board(self.board)
        self.board_widget.set_last_move(None, None, None)
        self._update_panel()

    def _cancel_ai(self):
        """协作式取消正在运行的 AI 搜索并等待其退出。

        不用 QThread.terminate()：那会在任意字节码处强杀线程，搜索正在做
        make/unmake 时被杀死会留下不一致状态。配合引擎的取消轮询，
        正常应在一次节点轮询之内退出，这里给 3 秒余量。

        **先递增代数再等待**，这一步不能省：等待有超时上限，而引擎此刻未必
        实现了取消轮询（Phase 1 就是如此）。超时后线程仍在跑，它的结果稍后
        会作为信号投递回来 —— 那个时刻对局可能已经被重开、日志文件已经关闭，
        于是写日志直接抛 ValueError（在 Qt 槽里会一路冒泡成崩溃）。
        递增代数让这份迟到的结果在 `_on_ai_finished` 入口就被丢弃。
        """
        self._ai_generation += 1     # 立刻作废所有在途结果，早于下面可能超时的等待
        w = self.ai_worker
        if w is not None and w.isRunning():
            w.cancel()
            if not w.wait(3000):
                print("[警告] AI 线程未在 3 秒内响应取消")
        self.ai_worker = None
        self.ai_thinking = False

    def _on_restart(self):
        """重新开始"""
        self._cancel_ai()
        if self.logger:
            # 打包版不写日志，`filepath` 是 None —— 别报一个"已保存:None"。
            if self.logger.filepath:
                print(f"[日志] 对局日志已保存: {self.logger.filepath}")
            self.logger.close()
        self._show_color_selection()

    def _on_quit(self):
        """退出"""
        self._cancel_ai()
        if self.logger:
            try:
                self.logger.close()
            except Exception:
                pass
        self.close()

    def _update_panel(self):
        """更新右侧面板"""
        if self.gamemode == 0:
            turn = 1 if self.move_count % 2 == 0 else 2
        else:
            turn = 2 if self.move_count % 2 == 0 else 1

        if self.gamerule == 1:
            status = "你输了！"
        elif self.gamerule == 2:
            status = "你赢了！"
        elif self.gamerule == 0:
            status = "平局！"
        else:
            status = "进行中"

        human = 1 if self.gamemode == 0 else 2
        self.game_panel.update_info(turn, self.gamekunnan, status,
                                    self.output, self.move_count, human=human)

        # 悬停幽灵子只在**轮到玩家**时出现，且用玩家自己的颜色：AI 思考中还给
        # 预览、或玩家执白却预览黑子，都是在骗人。
        if self.board_widget is not None:
            self.board_widget.set_hover_player(
                human if (turn == human and not self.ai_thinking
                          and not self.game_over) else None)

    def _show_game_over(self):
        """显示游戏结束覆盖层"""
        self._update_panel()

        is_win = (self.gamerule == 2)
        if self.gamerule == 2:
            text = "你赢了！"
            winner_str = "human"
        elif self.gamerule == 1:
            text = "你输了！"
            winner_str = "ai"
        else:
            text = "平局！"
            winner_str = "draw"

        # 记录对局结果到日志
        if self.logger:
            self.logger.log_result(winner_str,
                total_steps=self.move_count, move_count=self.move_count)
            # 记录终局完整棋盘
            self.logger.log_board_state(self.move_count, self.board, "[终局]")

        overlay = GameOverOverlay(text, is_win)
        overlay.restart_clicked.connect(self._on_restart)
        overlay.quit_clicked.connect(self._on_quit)

        # 几何完全交给 QStackedLayout(StackAll)：遮罩与棋盘行共用同一块区域，
        # 尺寸随窗口走。**不要**再 setGeometry —— 那会和布局打架。
        self.game_over_overlay = overlay
        self._stack.addWidget(overlay)
        self._stack.setCurrentWidget(overlay)
        anim.fade_in(overlay)


# ==================== 入口 ====================
def _fix_qt_plugin_path():
    """构造 QApplication 之前修正 Qt 插件搜索路径。

    当项目路径含非 ASCII 字符（例如中文的"桌面"）时，Qt 在初始化阶段会丢掉
    插件目录，报 "Could not find the Qt platform plugin" 而无法启动。
    这里从 PyQt5 的安装位置反推插件目录并显式写入环境变量，
    pip / venv / 系统包各种安装方式下都成立。
    """
    if os.environ.get('QT_QPA_PLATFORM_PLUGIN_PATH'):
        return
    try:
        import PyQt5
        platforms = os.path.join(os.path.dirname(PyQt5.__file__),
                                 'Qt5', 'plugins', 'platforms')
        if os.path.isdir(platforms):
            os.environ['QT_QPA_PLATFORM_PLUGIN_PATH'] = os.path.dirname(platforms)
    except Exception:
        pass


def main():
    _fix_qt_plugin_path()

    # 高 DPI 属性**必须在 QApplication 构造之前**设置，构造之后设无效。
    # 刻意放在 main() 里而不是模块级 import：gui_smoke 自建 QApplication 且
    # 不经过 main()，于是 CI 上 DPR 恒为 1.0，所有像素几何断言都是确定的。
    # （真要放进模块级，就得改用 QT_ENABLE_HIGHDPI_SCALING 环境变量 —— 那个
    #   是进程级的，同样会污染测试。）
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    # 字体不在这里设：theme.install() 同时设 QSS 的 font-family 与 app.setFont，
    # 两者同源。在这里再写一个 QFont 只会被 QSS 覆盖，看着像生效了其实没有。

    window = GomokuGame()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()

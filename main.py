"""五子棋游戏 — PyQt5 界面层。

界面、交互、动画与日志展示在这里；AI 引擎在 engine.py（纯 CPU、零 Qt/torch），
日志格式在 gamelog.py。本文件不再包含任何搜索或评估逻辑。
"""
import os
import sys
import math
import json
import threading

import numpy as np

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QDialog,
    QVBoxLayout, QHBoxLayout, QStackedWidget, QStackedLayout,
    QProgressBar, QFrame, QSizePolicy, QLabel, QScrollArea, QButtonGroup,
    QGridLayout, QLineEdit, QSlider, QMessageBox
)
from PyQt5.QtCore import (
    Qt, QTimer, QThread, QObject, QAbstractAnimation, pyqtSignal, QRect,
    QPoint, QPointF, QSize, QElapsedTimer, QEasingCurve, QVariantAnimation,
    QLocale, QSettings
)
from PyQt5.QtGui import (
    QPainter, QPainterPath, QPen, QBrush, QColor, QMouseEvent, QIcon
)

import analysis
import anim
import board_render
import charts
import engine
import i18n
import theme
from board_geometry import BoardGeometry
from client import LocalRoom, RoomClient, local_ip
from engine import (BOARD_SIZE, Board, evaluate, is_mate,
                    new_game, win_line)
from gamelog import GameLogger
from room import SeatSpec
from session import Session
from ui_kit import (BrandMark, InfoRow, QuestionMark, Screen, StoneFace,
                    TurnIndicator,
                    button as _ui_button, card_button as _ui_card_button,
                    card_fit_height, faint_label, hbox as _ui_hbox, separator,
                    subtitle_label, title_label)

# ---- ui_kit 的"半缩放"补丁 ----------------------------------------------
#
# ``ui_kit.py`` 是冻结的，而且它的 ``button(height=CONTROL_H)`` /
# ``card_button(size=CARD_PX)`` / ``hbox(spacing=SPACE_MD)`` 把度量写成了**默认
# 参数** —— 默认参数在 def 那一刻就固化，换档之后纹丝不动。所以在这里套一层
# 同名的本地包装：**调用方一个字符都不用改**，但默认值改成按当前档位现取。
#
# 只补默认值，不覆盖显式传参 —— 调用方写死的 ``height=32`` 之类的字面量另有
# `_px()` 处理（见下）。
def button(text, variant="primary", *, height=None, width=None):
    # ``width`` 是**宽度下限**而不是定宽：外文文案更长时按钮按 sizeHint 撑开，
    # 中文下与从前逐字不差（sizeHint 不超过下限时实际宽度就是那个下限）。
    # 定宽裁掉长译文是这一轮之前的通病（「✕ Отменить ожидание」要 336px）。
    btn = _ui_button(text, variant,
                     height=theme.CONTROL_H if height is None else height,
                     width=None)
    if width is not None:
        btn.setMinimumWidth(width)
    return btn


def card_button(text, tone, *, face=None, sub="", index="", size=None):
    return _ui_card_button(text, tone, face=face, sub=sub, index=index,
                           size=theme.CARD_PX if size is None else size)


def hbox(*widgets, spacing=None, align=None):
    return _ui_hbox(*widgets,
                    spacing=theme.SPACE_MD if spacing is None else spacing,
                    align=align)

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

# ---- 字号档位相关的工具 --------------------------------------------------

#: 自动适配的急停开关。离屏测试平台报的是 800×600，若自动适配无条件生效，
#: 冒烟与截图里窗口会从 1022×750 变成 736×540，把既有的尺寸断言全搅乱。
#: 由 ``tests/conftest.py`` 与 ``tools/gui_smoke.py`` 置 0。写法仿
#: ``gamelog.LOG_DIR_ENV``（同一个仓库里的既有惯例）。
AUTOFIT_ENV = "GOMOKU_AI_UI_AUTOFIT"


#: 窗口外框（标题栏 + 边框）高度的兜底值，只在 ``frameGeometry()`` 还量不出来
#: 时用（首次 show 之前）。``_fit_cap_h`` 要从可用高度里扣掉它 —— 屏幕可用区
#: 是**不含**外框的，而窗口总高是"客户区 + 外框"，不扣就等于让标题栏悬到屏幕
#: 外，正是用户报的那个"启动尺寸超出屏幕高度"。
FRAME_FALLBACK_H = 40


def _autofit_enabled() -> bool:
    return os.environ.get(AUTOFIT_ENV, "1") != "0"


def _px(base: int) -> int:
    """设计像素 → 当前档位下的像素。"""
    return max(1, int(round(base * theme.scale_factor())))


def settings_button():
    """全局唯一的「设置」入口：**右上角那枚小齿轮**。

    所有页面共用**同一个个控件对象**（挂在主窗口上，见
    ``GomokuGame.settings_btn``）—— 位置、尺寸、样式各页一字不差。曾经的做法
    是每页各挂一个按钮（页面底部的脚注 + 对局面板的紧凑版），结果是三种长相、
    三种位置，而且面板里那版被变体 QSS 的 ``padding`` 裁成了残缺的一条
    （用户 2026-10-05 的截图）。

    ``iconOnly`` 是给 ``theme`` 认的：只有它能把变体那条为文字按钮写的左右
    padding 压回去。
    """
    b = button("⚙", "ghost", height=_px(32), width=_px(32))
    # 齿轮是正圆图标位，不受"宽度下限"那条规则摆布 —— 它不在布局里，靠
    # `_place_settings_button` 按 `width()` 定位，尺寸必须当场确定。
    b.setFixedWidth(_px(32))
    b.setProperty("iconOnly", "true")
    b.setToolTip(i18n.t("字号 / 主题"))
    return b


def _page_title_label(page):
    """页面的主标题控件；没有就返回 ``None``。

    ``Screen`` 骨架的标题是 ``role="title"``，对局面板的是 ``role="panel-title"``
    —— 齿轮要贴在它右边，所以得先把它找出来。按 ``role`` 认而不是按控件顺序，
    是因为面板里除了标题还有一堆别的 label。
    """
    for role in ("panel-title", "title"):
        for lbl in page.findChildren(QLabel):
            if lbl.property("role") == role:
                return lbl
    return None


#: 面板实际宽度缓存。键 = (当前语言, 字号档位) —— 面板宽度由当前语言的文案
#: 度量与档位共同决定，两者之一变了就该重算（换语言/换档都会重建游戏页）。
_PANEL_W_CACHE: dict = {}


def _panel_active_w() -> int:
    """当前语言与档位下右侧面板**实际需要**的宽度（``theme.PANEL_W`` 是下限）。

    ``theme.PANEL_W`` 只是中文基准：俄语「✕ Выйти из игры」单行要 272px，比
    264px 的面板内区还宽。若面板死守 264，长语言下要么裁按钮、要么默认窗口
    装不下面板。所以窗口设计宽度、页面可用宽度都按**内容需要**算。

    实现是构造一只探针面板量 ``minimumSizeHint()`` —— 按钮的 QSS padding、
    图表卡边距这些都不是纯文本度量能复刻的，问控件最稳。每个（语言, 档位）
    组合只算一次；探针用完即删，不进任何页面栈。
    """
    key = (i18n.resolved(), theme.current_scale())
    cached = _PANEL_W_CACHE.get(key)
    if cached is not None:
        return cached
    need = theme.PANEL_W
    if QApplication.instance() is not None:
        try:
            probe = GamePanel()
            probe.ensurePolished()
            need = max(need, probe.minimumSizeHint().width())
            probe.deleteLater()
        except Exception:
            # 量不出就退回基准 —— 面板顶多按旧宽度画，不该让窗口开不了。
            pass
    _PANEL_W_CACHE[key] = need
    return need


def _design_size() -> tuple:
    """按**当前档位与语言**现算的设计窗口尺寸。

    ``WINDOW_W`` / ``WINDOW_H`` 是 normal 档、中文下的基准（tools 与测试上按
    模块常量引用，不能变成会漂的值），这里是它在别的档位/语言下的对应物：
    宽度取面板的**实际需要**（见 ``_panel_active_w``），高度只跟档位走。
    """
    gap = theme.SPACE_MD
    return (gap + BOARD_PX + theme.SPACE_SM + _panel_active_w() + gap,
            gap + BOARD_PX + gap)


def _autofit_scale(avail_w: int, avail_h: int) -> str:
    """按屏幕可用区选**初始**档位。只降不升。

    用户报的那台 1920×1080 @150% 的机器上，Qt 的逻辑可用区只有 1280×720：
    设计尺寸 1022×750 摆不下，而按 1:1 画的字相对屏幕又偏大。这里据此降一档，
    让窗口在逻辑像素上重新"看起来是那么大"。

    **纯函数**（不碰 Qt、不读全局），便于单测。判据与 ``_initial_size`` 同一个
    ``k``：缩不进去就降档。
    """
    dw, dh = _design_size()
    if avail_w <= 0 or avail_h <= 0:
        return "normal"
    k = min(avail_w * 0.92 / dw, avail_h * 0.92 / dh)
    if k < 0.92:
        return "small"
    return "normal"

#: 终局到结算遮罩之间的停顿。
#:
#: 遮罩是**整屏**盖住棋盘的，一落子就弹等于把"你输在哪"当场抹掉 —— 2026-10-01
#: 用户的反馈正是"还没看见 AI 的连五在哪里，就被三个大字遮住了"。这一秒留给
#: 已经画好的红线与蓝环。两侧（赢、输）都给，平局不给：平局没有连五可看。
GAME_OVER_DELAY_MS = 1000

#: 局域网房间名。开房方与加入方必须是同一个（M4d 的 LAN 只有一桌，
#: 不做房间列表；"专用服务器多房间"那条路走 server.py 的 --room）。
LAN_ROOM_NAME = "lan"

# 终局那条红线的颜色。**刻意不随主题走。**
#
# 棋盘（木色、网格、棋子）是跨主题固定的，画在棋盘上的东西也必须固定 ——
# 同一个棋盘不该因为窗口换了主题就换色。更要命的是"另一档"那支根本是照深背景
# 调的：``DANGER`` 在深色档是一支亮粉，落在橙金木盘上的亮度对比只有 1.06:1，
# 整条线等同于没画。所以取浅色档那支深红：木盘上 2.30–3.10:1，对白子 5.70:1。
#
# 走 ``theme.WIN_LINE``（一个真实的模块属性）而**不是** ``theme.DANGER`` ——
# 后者是 PEP 562 的按主题取值，恰恰是这里要避开的东西。颜色本身定义在
# ``theme.py`` 里（``tests/test_no_literal_colors.py`` 要求颜色只在那一个文件里
# 出现），这里只取用。
_WIN_LINE_COLOR = theme.WIN_LINE                        # 深红

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

    出招本身走 ``players.Player`` 抽象（M2 起）：进程内 AI 与以后的网络
    Bot 对这条线程是同一件事。
    """
    finished = pyqtSignal(int, int, object)  # (row, col, info_dict)

    def __init__(self, board, player, stone):
        super().__init__()
        self.board = board.copy()
        self.player = player
        self.stone = stone
        self._cancel = threading.Event()

    def cancel(self):
        """请求取消搜索。线程会在下一次节点轮询时退出。"""
        self._cancel.set()

    def run(self):
        try:
            r, c, info = self.player.choose_move(self.board, self.stone,
                                                 cancel=self._cancel)
        except Exception as exc:
            # 引擎异常不应让线程静默死掉、把 UI 永远卡在"思考中"
            self.finished.emit(-1, -1, {'reason': 'AI异常', 'best_val': 0.0,
                                        'detail': f'{type(exc).__name__}: {exc}'})
            return
        self.finished.emit(r, c, info)


# ==================== 房间事件桥 ====================
class _RoomBridge(QObject):
    """房间事件 → Qt 主线程的跨线程投递桥（M4c）。

    ``RoomClient`` 在读取线程上调用 ``on_event``；``pyqtSignal.emit`` 的默认
    ``AutoConnection`` 会在发射线程与接收对象所属线程不同时自动排队
    （queued），所以 ``GomokuGame._on_room_event`` 总在 Qt 主线程执行 —— UI
    可以在槽里安全地碰控件，不必自己写 ``QMetaObject.invokeMethod``。

    每次开局新建一座桥（见 ``_close_room``）：连接断开时把它整座拆掉，
    迟到的 emit 只会静默落在无人监听的信号上，不会碰到已销毁的对局 UI。
    """

    event = pyqtSignal(object)


# ==================== 复盘 Worker 线程 ====================
class ReviewWorker(QThread):
    """战后复盘：逐手重算玩家每一着，找出"不是最优点"的那些。

    **每手都用所选档位的时限**（``engine.analyze`` 的默认口径 = 难度表里的
    ``time``），所以这一轮可能跑上十几秒 × 十几手。取消同样是协作式的：
    ``cancel()`` 置位 Event，引擎在搜索循环里轮询到就退出，**已经算完的部分
    保留**（``completed`` 照常发出，只是短一些）。

    只复盘**玩家**的着法：``moves`` 是 ``main`` 记下的那些落子前局面快照，
    ``human`` 是玩家执的子色。AI 的着法不在其中 —— 用户点名的口径。
    """

    progressed = pyqtSignal(int, int)   # (已完成, 总数)
    completed = pyqtSignal(object)      # [record, ...]

    def __init__(self, moves, human, level, cfg=None):
        super().__init__()
        self.moves = list(moves)
        self.human = human
        self.level = level
        # 自定义档的复盘**用开局时快照的那份参数**，而不是现读引擎里那一份 ——
        # 否则打完棋再打开自定义弹窗改参数，这一局的复盘结果会被改写。预设档
        # 传 None，照旧走难度表。
        self.cfg = dict(cfg) if cfg is not None else None
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        """是否被要求取消。结果页据此在副标题上标一句"仅列出已算完的部分"。"""
        return self._cancel.is_set()

    def run(self):
        records = []
        total = len(self.moves)
        for i, item in enumerate(self.moves):
            if self._cancel.is_set():
                break
            try:
                rec = self._one(item)
            except Exception as exc:
                # 单步失败不该让整轮复盘作废 —— 与 ``AIWorker`` 同一条哲学：
                # 异常若冒泡出 ``run``，线程就静默死了，UI 会永远停在进度页。
                print(f"[复盘] 第 {item.get('seq')} 手分析失败: "
                      f"{type(exc).__name__}: {exc}")
                rec = None
            if rec is not None:
                records.append(rec)
            self.progressed.emit(i + 1, total)
        self.completed.emit(records)

    def _one(self, item):
        """分析一手。返回记录，或 ``None``（**只**在被取消时）。

        只有取消才丢行。其余"算不出结论"的情形一律照出一行 —— 结果页列的是
        一整局棋谱，中间少一行就等于棋谱断了。
        """
        before = item['before']
        played = (item['r'], item['c'])
        best_idx, best_val, cands, _info = engine.analyze(
            before, self.human, self.level, cancel=self._cancel, cfg=self.cfg)
        if self._cancel.is_set():
            return None
        if not cands:
            # 两手空空的两个来源，都不该让这一行消失：
            #  * **空盘**（玩家执黑第一手）：盘上没有子，也就没有"最优点"这
            #    个概念 —— 不是走错，是没得比。
            #  * 搜索连一轮都没跑完：截断了，同样没得比。
            # 两者都记 ``best=None``，结果页按"未评分"处理（见 ``_record_line``）。
            return {
                'seq': item['seq'], 'played': played, 'best': None,
                'best_val': None, 'before': before, 'mate': False,
                'played_val': None, 'delta': None, 'offboard': False,
                'empty': not bool(np.any(before)),
            }
        table = dict(cands)
        played_idx = played[0] * BOARD_SIZE + played[1]
        rec = {
            'seq': item['seq'],
            'played': played,
            'best': divmod(best_idx, BOARD_SIZE),
            'best_val': best_val,
            'before': before,
            # 杀棋分（|v| > 静态上限）之间的差值是 2×10⁷ 这种没有量纲意义的数，
            # 结果页要另给一句文字结论（见 ``_record_line``）。
            'mate': is_mate(best_val),
        }
        if played_idx not in table:
            # 不在表里有两种**完全不同**的原因，不能混成一句话：
            #
            #  * 它压根不是候选点（候选按邻接生成，下到离战场很远的地方就不会
            #    出现）→「偏离战场」。这不是"漏报"，如实标出来。
            #  * 它是合法候选点却没被打分 → 根节点**提前返回**了：只要有一手
            #    立刻成五，`_root` 就当场返回，后面的候选一个都不再走
            #    （`engine_local.Board` 与 C++ 的 `root()` 同款）。那时的正确
            #    说法是"错失必胜"，判为"偏离战场"就是把最严重的一手讲成无害。
            legal = set(Board.from_array(before).candidates())
            rec.update({'played_val': None, 'delta': None,
                        'offboard': played_idx not in legal, 'empty': False})
        else:
            played_val = table[played_idx]
            delta = best_val - played_val
            rec.update({'played_val': played_val, 'delta': delta,
                        'offboard': False, 'empty': False})
            if delta == 0:
                # **并列最优。** ``delta == 0`` 只说明玩家这一手与引擎的最优
                # **同分**，而 ``best_idx`` 只是同分者里被挑中的一个 —— 完全
                # 可能是另一个点。圈在别处却写着"最优"，读起来就是程序在说
                # 反话。同分即意味着玩家下的这颗**也是**最优，把圈指回它自己：
                # 幽灵子与圈重合，"你下的这颗就是最好的那一颗"。
                rec['best'] = played
        return rec


# ==================== 复盘页面 ====================
REVIEW_ROW_W = 620          # 结果行宽度：够放下"第 NN 手 … Δ=…"那串文字
# 列表视口高度。**必须定死**：几十条记录自然高度会把整页撑得比窗口还高，
# 底下的按钮被推出屏幕且无法滚动到。
REVIEW_LIST_H = 420


def _fmt_point(rc):
    """把 ``(r, c)`` 写成棋盘坐标（列 A–S、行 1–19，与日志同一套）。"""
    r, c = rc
    return "%s%d" % (chr(ord('A') + c), r + 1)


def _is_rated(rec):
    """这一手到底有没有被评过。

    空盘起始手与"搜索一轮都没跑完"都记 ``best=None`` —— 它们是**没得比**，
    不是"比过了、没问题"。这两类既要进列表（棋谱得连续），又不能算进
    "可改进"的计数里，否则结果页会耸人听闻地报出一个玩家没犯过的错。
    """
    return rec['best'] is not None


def _is_optimal(rec):
    """这一手是不是就是引擎眼中的最优点。

    ``delta == 0`` 才算 —— ``delta is None``（偏离战场 / 搜索被截断）是
    **没比较过**，不是"比较了、没问题"，两者绝不能归成一句"最优"。
    """
    return rec['delta'] == 0 and not rec['offboard']


def _record_line(rec):
    """一条复盘记录的正文。

    **每一手都进列表，包括走对的那些。** 复盘动辄算上几分钟，只列错手的话
    用户回头就找不到"我当时第 9 手走哪儿了" —— 列表要有从头到尾的连续性，
    结论才落在一条完整的棋谱上而不是一串孤立的错误。

    分值差在杀棋局面里没有量纲意义（两个杀棋分之差是 2×10⁷ 这个量级），
    所以那里改报一句文字结论 —— 光甩一个七位数只会让人以为程序算错了。
    """
    head = i18n.tf("第 %d 手   %s", rec['seq'], _fmt_point(rec['played']))
    if not _is_rated(rec):
        # 空盘起始手没有"最优点"可言；搜索没跑完一轮则是没算出来。都不编。
        return head + ("   " + i18n.t("空盘起始手，无候选可比") if rec['empty']
                       else "   " + i18n.t("未能比较（搜索未跑完一轮）"))
    if _is_optimal(rec):
        # 走对了就不必再报一遍同样的坐标（``played == best``，写成
        # "J13 → 最优点 J13" 只会让人以为程序在说胡话）。
        return head + "   " + i18n.t("最优")
    head += i18n.tf(" → 最优点 %s", _fmt_point(rec['best']))
    if rec['offboard']:
        return head + "   " + i18n.t("偏离战场（不在候选点内）")
    if rec['mate']:
        return head + "   " + i18n.t(
            "错失必胜" if rec['best_val'] > 0 else "漏防必败")
    if rec['delta'] is None:
        # 合法候选点却没有分值：搜索被时限截断在半张表上（``_root`` 的提前
        # 返回之外，取消也会走到这里）。**不能**当 0 处理 —— "没算过"与
        # "算过了、没问题"是两件事。
        return head + "   " + i18n.t("未能比较（搜索被截断）")
    return head + i18n.tf("   分值差 %d", rec['delta'])


class ReviewProgressScreen(Screen):
    """复盘计算中的进度页：进度条 + 「第 N/M 手」+ 取消。"""

    cancel_clicked = pyqtSignal()

    def __init__(self, total, level):
        super().__init__(title="算法复盘",
                         subtitle=i18n.tf("正在按「%s」逐手重算",
                                          i18n.t(engine.difficulty_name(level))),
                         backdrop=True)
        self.total = max(1, int(total))
        self.setup_ui()

    def setup_ui(self):
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, self.total)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        # 与加载页同一套尺寸：进度条是同一件东西，只是量纲从"毫秒"变成"手"。
        self.progress_bar.setFixedHeight(_px(5))
        self.progress_bar.setFixedWidth(_px(300))
        self.add_content(self.progress_bar)

        self.step_label = faint_label(i18n.tf("第 %d/%d 手", 0, self.total))
        self.add_content(self.step_label)

        cancel_btn = button("✕ 取消复盘", "danger", width=_px(150))
        cancel_btn.clicked.connect(self.cancel_clicked.emit)
        self.add_content(cancel_btn)

    def set_progress(self, done, total):
        self.progress_bar.setRange(0, max(1, int(total)))
        self.progress_bar.setValue(int(done))
        self.step_label.setText(i18n.tf("第 %d/%d 手", done, total))


class ReviewScreen(Screen):
    """复盘结果列表。**每一手一行**（走对的也在内），各带一个「棋局显示」按钮。"""

    record_selected = pyqtSignal(int)   # 选中记录在 self.records 里的下标
    finish_clicked = pyqtSignal()       # 结束复盘：回模式选择页
    exit_clicked = pyqtSignal()         # 退出游戏

    def __init__(self, records, level, cancelled=False):
        # 只数**评过且不是最优**的那些。"没得比"的两类不算错 —— 这一行是要
        # 说"你哪几手可以更好"，把空盘起始手算进去就成了凭空指控。
        blunders = sum(1 for r in records
                       if _is_rated(r) and not _is_optimal(r))
        super().__init__(
            title="复盘结果",
            subtitle=i18n.tf(
                "强度「%s」· 共 %d 手，其中 %d 手可改进%s",
                i18n.t(engine.difficulty_name(level)), len(records), blunders,
                i18n.t("（已取消，仅列出已算完的部分）") if cancelled else ""),
            backdrop=True)
        self.records = list(records)
        self.setup_ui()

    def setup_ui(self):
        self.add_content(self._build_list())

        # 出口放在**列表底下**而不是用 ``add_footer`` 贴到窗口最底边：
        # 复盘是一条有终点的路径（结果 → 棋局显示 → 返回），看完最后一条
        # 就该有地方落下去，让视线从列表自然接到按钮上。
        finish = button("🏠 结束复盘", "primary", width=_px(150))
        finish.clicked.connect(self.finish_clicked.emit)
        quit_btn = button("✕ 退出游戏", "danger", width=_px(150))
        quit_btn.clicked.connect(self.exit_clicked.emit)
        self.add_content(hbox(finish, quit_btn, spacing=theme.SPACE_LG))

    def _build_list(self):
        if not self.records:
            return faint_label("本局没有可复盘的着法")
        inner = QWidget()
        col = QVBoxLayout(inner)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(theme.SPACE_SM)
        for i, rec in enumerate(self.records):
            text = QLabel(_record_line(rec))
            text.setProperty("role", "value")
            # 走对的那几手标绿：一眼扫出"哪几手没问题"比逐行读文字快得多，
            # 而这份列表的用处正是**回头定位**（复盘算了几分钟之后）。
            if _is_optimal(rec):
                text.setProperty("tone", "win")
            show = button("棋局显示", "ghost", width=_px(110))
            # 这一行没有 stretch，按钮会被布局拉伸/压缩 —— 锁成"内容宽度与
            # 旧定宽 110 的较大者"：俄语「Отображение партии」要 320px，按
            # sizeHint 排；中文 110px 不变。
            show.setFixedWidth(max(_px(110), show.sizeHint().width()))
            show.clicked.connect(lambda _=False, idx=i:
                                 self.record_selected.emit(idx))
            col.addWidget(hbox(text, show, spacing=theme.SPACE_MD))
        col.addStretch(1)

        area = QScrollArea()
        area.setWidgetResizable(True)
        # 宽度 = 基准与**内容需求**取大者，但不超过页面可用宽度：
        # 「棋局显示」/「Отображение партии」按钮与行文本都按自己的 sizeHint
        # 渲染，列表被压窄时它们会被挤掉；内容超出可用宽度则交给视口横向滚动，
        # 而不是把文字裁掉。中文行比基准窄，这里就是原来的 644px。
        inner_need = inner.sizeHint().width() + 2 * theme.SPACE_SM
        area.setFixedWidth(min(_page_content_w(),
                               max(_px(REVIEW_ROW_W) + _px(24), inner_need)))
        area.setWidget(inner)
        # **高度按内容收缩，封顶 ``REVIEW_LIST_H``。** 定高的用意是兜住
        # "几十条记录"那种极端（否则整页撑得比窗口还高）；但只有两条记录
        # 时也摆一个 420px 的空框，看上去像列表没加载出来。
        area.setFixedHeight(min(_px(REVIEW_LIST_H),
                                inner.sizeHint().height() + 2 * theme.SPACE_SM))
        return area


class ReviewBoardScreen(Screen):
    """某一手的复盘棋盘：**落子之前**的局面 + 最优点圈 + 你那一手的幽灵子。"""

    back_clicked = pyqtSignal()

    def __init__(self, rec):
        super().__init__(title="棋局显示",
                         subtitle=_record_line(rec), backdrop=True)
        self.rec = rec
        self.setup_ui()

    def setup_ui(self):
        board = BoardWidget()
        # 只读：不接 ``mousePressEvent``（基类什么都不做），也不给悬停预览。
        #
        # **不再无条件 setFixedSize(BOARD_PX, BOARD_PX)。** 定死 726 正是
        # "复盘界面无法缩放"的直接原因：窗口被压小之后那张盘还是 726，底部
        # 按钮被顶出可视区。
        #
        # 但也不能只是"给下限 + 让它 Expanding"就算完 —— `Screen` 的骨架把
        # 正文塞在两根 stretch 之间，横向还带 AlignCenter，控件只会拿到自己的
        # sizeHint，撑不开。所以这里按**页面可见区域**现算边长（`_fit_board_side`），
        # 并在 `resizeEvent` 里跟着窗口走。
        board.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        board.set_board(self.rec['before'])
        board.set_last_move(None, None, None)
        board.set_hover_player(None)
        board.set_review_marker(self.rec['best'], self.rec['played'],
                                self._played_player())
        # 留个句柄给"复核这张盘摆了哪一手的什么局面"的调用方（无头冒烟测试
        # 就用它读回 review_marker 与盘面）。**与主窗口的 `board_widget` 不是
        # 同一个属性** —— 那是主窗口的，这里是本页自己的。
        self.board_widget = board
        self.add_content(board)

        back = button("← 返回复盘", "primary", width=_px(150))
        back.clicked.connect(self.back_clicked.emit)
        self.add_content(back)
        # 首次布局前先定一个尺寸，避免构造期拿到 0×0 让 `geom` 退化成除零。
        self._fit_board_side()

    #: 棋盘以外那一摞的高度（标题 + 副标题 + 两段间距 + 返回按钮 + 页面边距），
    #: 按**设计像素**估。写死是为了让 `_fit_board_side` 不依赖布局测量 ——
    #: 它要在 resizeEvent 里跑，而那时布局往往还没算完。
    _REVIEW_CHROME = 210

    def _fit_board_side(self) -> int:
        """复盘棋盘的边长：min(设计边长, 页面装得下的边长)。

        量的是**窗口**而不是本页 —— 本页在滚动区里，窗口矮的时候它会保持
        自己的最小高度（否则就成了"页面高度取决于棋盘、棋盘又取决于页面高度"
        的循环），用它算永远缩不下去。
        """
        win = self.window()
        page_h = win.height() if win is not None and win.height() > 0 else self.height()
        avail_h = page_h - _px(self._REVIEW_CHROME)
        avail_w = self.width() - 2 * theme.SPACE_XL
        side = min(_px(BOARD_PX), avail_h, avail_w)
        return max(_px(MIN_BOARD), int(side))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        bw = getattr(self, "board_widget", None)   # 构造期可能先于 setup_ui 触发
        if bw is None:
            return
        side = self._fit_board_side()
        bw.setFixedSize(side, side)
        # 棋盘改了尺寸，本页的最小尺寸也跟着变，得让主窗口把滚动区的最小值
        # 重算一次。**延到下一轮事件循环**：这里还在布局过程中，当场改会与
        # QScrollArea 的调整互相触发；延后一步就收敛（棋盘边长只取决于窗口
        # 高度，不依赖这次同步的结果）。
        win = self.window()
        sync = getattr(win, "_sync_central_min", None)
        if sync is not None:
            QTimer.singleShot(0, sync)

    def _played_player(self):
        """玩家那一手用的是谁的子色 —— 幽灵子的颜色。

        棋盘上黑先白后，第 N 手（``seq`` 从 1 起）的奇偶就是子色。不用
        ``self.human``：那是"玩家执哪一色"，只有在玩家始终执同一色的对局里
        才等价，而这里要的是"这一手是谁下的"。
        """
        return 1 if self.rec['seq'] % 2 == 1 else 2


# ==================== 设置弹窗 ====================
#
# 2026-10-06 从整页改为模态弹窗。动机不是"弹窗更好看"：它原来是一页，塞在
# `central` 这个 QStackedWidget 里，于是"进设置"要借道切页、"出设置"要原路返回
# 并重建原页 —— 一整套 `_settings_origin` / `_scale_changed_in_settings` /
# `_rebuild_settings` / `_back_from_settings` 都是在为"它占了一页"这件事服务。
# 弹窗期间 `central` 的当前页岿然不动，"从哪来"这个问题就不存在了。
# （`_scale_changed_in_settings` 留着：它是"弹窗里换过档没有"的标记，关窗时据此
#   决定要不要按新度量重建底下那一页。）
#
# **用 ``open()`` 而不是 ``exec_()``。** 两者都是模态，但 ``exec_()`` 进的是嵌套
# 事件循环，`tools/gui_smoke.py` 那套靠 QTimer/pump() 驱动的手动测试会卡死在里面；
# ``open()`` 不阻塞调用方，信号照常直连、事件循环照常由测试推进。

def _even_width(buttons, minimum: int = 0) -> int:
    """把一排按钮统一成同一个宽度：取其中最宽的那个，但不低于 ``minimum``。

    跨语言时同一排按钮的文案长度差得很远 —— 「关 / 低 / 中 / 高 / 满」在俄语里
    是「Выкл / Низкий / Сред / Высокий / Макс」，写死 78px 会把「Высокий」和
    「Макс」各裁掉一截。统一成最宽的那个，既保住"一排等宽"的观感，又不会在
    任何一种语言下截断。

    ``sizeHint()`` 是问过 QSS 的（padding/border 都算进去），所以这里量的宽度
    就是按钮真的需要占的宽度。
    """
    w = max([minimum] + [b.sizeHint().width() for b in buttons])
    for b in buttons:
        b.setFixedWidth(w)
    return w


class _OptionRow(QWidget):
    """自定义难度里的一行**枚举**参数：标签 + 一排互斥的 ghost 按钮。

    **用按钮而不是 QComboBox。** `theme.py` 的 QSS 不覆盖 QComboBox —— 原生
    下拉框在这套设计系统里会是一个没被样式化的灰块，深浅两套主题下都不对。
    可勾选的 ghost 按钮则是设置弹窗里那四档字号用过的写法，QSS 早已覆盖。

    留给**取值离散且带语义**的参数（VCF 的关/低/中/高/满、增强的开/关）：这些
    选项各自是一个说法，不是一个刻度上的刻度值，摆成滑杆反而看不出"关"与
    "低"之间到底是什么关系。连续量走 ``_SliderRow``。
    """

    value_selected = pyqtSignal(object)

    def __init__(self, label, options, current, parent=None):
        """``options`` 是 ``[(值, 按钮文案), ...]``；``current`` 是当前值。"""
        super().__init__(parent)
        self._value = current
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(theme.SPACE_XS)

        col.addWidget(faint_label(label, align=Qt.AlignLeft))

        group = QButtonGroup(self)
        row = []
        self._options = list(options)
        for value, text in options:
            b = button(text, "ghost")
            b.setCheckable(True)
            b.setChecked(value == current)
            b.clicked.connect(lambda _=False, v=value: self._pick(v))
            group.addButton(b)
            row.append(b)
        _even_width(row, _px(78))
        # group 只挂在局部变量上会被 GC 掉，互斥随之失效。
        self._group = group
        self._buttons = row
        col.addWidget(hbox(*row, spacing=theme.SPACE_XS, align=Qt.AlignLeft))

    def _pick(self, value):
        self._value = value
        self.value_selected.emit(value)

    def value(self):
        return self._value

    def set_value(self, value):
        """外部（如「恢复默认」）改选中项，**不**发 ``value_selected``。"""
        self._value = value
        for b, (v, _text) in zip(self._buttons, self._options):
            b.setChecked(v == value)


class _SliderRow(QWidget):
    """自定义难度里的一行**连续**参数：标签 / 当前值 —— 滑杆。

    ## 为什么滑杆，以及为什么必须给 ``theme.py`` 补规则

    时限 10–60 秒、深度 2–40 层、静止搜索 2–24 层：这类量**每一档都有意义**，
    摆成一排按钮就得二选一 —— 要么列 51 个按钮，要么把刻度砍成残的（旧版列
    6 档，用户立刻发现"只能选 1/3/7/15/20/30 秒"，没有 12 秒）。滑杆才是它
    本来的形状。

    代价是 Qt 的**原生** QSlider 是带立体感的灰槽 + 凸起方块，摆进两套扁平主题
    里都是异物，而 ``theme.py`` 原先对 QSlider 一条规则都没有。所以这里配一条
    ``QSlider[role="value"]`` 的 QSS（见 ``theme._SLIDER``）—— 这是整个自定义
    弹窗唯一新增的样式规则，也是它唯一该新增的地方（``main.py`` 不许写颜色）。

    ## 值是整数刻度，落盘仍是原来的类型

    滑杆只能走整数，所以 10–60 是"秒数"这个整数刻度；发射时 ``time`` 转回
    ``float``（引擎那一格本来就是浮点），其余按整数发。刻度 1 步进，与用户
    "步长 1 秒 / 一层"的要求一致。
    """

    value_selected = pyqtSignal(object)

    def __init__(self, label, lo, hi, unit, current, *, to_float=False,
                 parent=None):
        super().__init__(parent)
        self._lo, self._hi = int(lo), int(hi)
        self._unit = unit
        self._to_float = to_float
        # **进来先夹一次。** 落盘的那份配置可能来自旧版本、也可能是手改 conf 的
        # 结果（比如 max_depth 写成 999）。滑杆自己会夹到 40，但 ``_value`` 若照
        # 抄原值，读数行显示"40 层"而 ``value()`` 报 999 —— 界面说的和引擎收到的
        # 就不是同一件事了。
        self._value = self._emit_value(self._clamp(current))

        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(theme.SPACE_XS)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.addWidget(faint_label(label, align=Qt.AlignLeft))
        head.addStretch(1)
        self._readout = faint_label("", align=Qt.AlignRight)
        head.addWidget(self._readout)
        col.addLayout(head)

        self._slider = QSlider(Qt.Horizontal)
        self._slider.setProperty("role", "value")
        self._slider.setRange(self._lo, self._hi)
        self._slider.setSingleStep(1)
        self._slider.setPageStep(max(1, (self._hi - self._lo) // 10))
        # 高度给足 16px 的滑块 + 上下各 4px，否则滑块会被行高裁成一条。
        self._slider.setFixedHeight(_px(24))
        # **必须有下限宽度。** 这一行被 `add_content` 以 `Qt.AlignLeft` 加进
        # 弹窗的内容列，那个对齐标志会让控件只占 sizeHint 宽度（不再横向拉伸）；
        # 而 QSlider 的 sizeHint 只有一百多像素，三条滑杆就会缩成三个小短条，
        # 框里的三项参数各占一小截。给一个下限，弹窗宽度也随之由它定下来。
        self.setMinimumWidth(_px(400))
        self._slider.setCursor(Qt.PointingHandCursor)
        self._slider.setValue(self._clamp(current))
        self._slider.valueChanged.connect(self._on_slide)
        col.addWidget(self._slider)

        self._sync_readout(self._slider.value())

    # ---- 内部 ----

    def _clamp(self, value):
        return max(self._lo, min(self._hi, int(round(float(value)))))

    def _emit_value(self, raw):
        return float(raw) if self._to_float else int(raw)

    def _sync_readout(self, raw):
        # 读数写成"整数 + 单位"，与滑杆刻度一一对应 —— 拖到哪读到哪。
        self._readout.setText("%d %s" % (raw, i18n.t(self._unit)))

    def _on_slide(self, raw):
        self._value = self._emit_value(raw)
        self._sync_readout(raw)
        self.value_selected.emit(self._value)

    # ---- 与 _OptionRow 同形的接口 ----

    def value(self):
        return self._value

    def set_value(self, value):
        """外部（如「恢复默认」）改刻度，**不**发 ``value_selected``。

        ``blockSignals`` 是必须的：``setValue`` 会触发 ``valueChanged``，于是
        读数行会刷一次（对），但 ``value_selected`` 也会跟着发一次 —— 而
        「恢复默认」正在做的事情恰恰是**绕过**用户交互直接改配置，再往上发一次
        就变成了回环。
        """
        self._value = value
        blocked = self._slider.blockSignals(True)
        try:
            self._slider.setValue(self._clamp(value))
        finally:
            self._slider.blockSignals(blocked)
        self._sync_readout(self._slider.value())


class _LanguageRow(QWidget):
    """设置弹窗里的「语言」一行：七种选项，两列网格。

    ## 为什么每个选项写**自名**而不是当前语言里的说法

    一个只会日语的人打开这个下拉，得能认出「日本語」三个字。写成当前界面语言
    的说法（"日语" / "Japanese"），恰恰是唯一读不懂这台机器上那份界面的人
    最不容易认出的写法 —— 语言选项是**唯一**该用自名的界面文案。

    唯一随界面语言变的是**「跟随系统」那一项**：它不是一个语言名，而是一个
    选择（"用系统那一档"），所以用户原话里给了它六种写法。

    ## 为什么两列而不是像字号那样排成一行

    七个选项排成一行要 7×190 + 6×间距 ≈ 1400px，比整个窗口还宽；而按钮文本
    长度差得远（"한국어" 与 "Use system language" 差三倍），按最长的那条定宽
    会让其余六条全是空荡的宽条。两列四行是这七个里最接近方的排法。

    ## 文案不走 ``i18n``

    选项文本是各语言的自名，**永远不翻**（翻了就失去意义）；「跟随系统」那一
    项由 ``i18n.follow_system_label()`` 现取。两者都直接进按钮，不像别处那样
    留中文原文给工厂去查表 —— 所以这里的 ``button()`` 刻意收的是成品串，
    查表落空后原样返回，结果正好。
    """

    language_selected = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(theme.SPACE_XS)

        col.addWidget(faint_label(i18n.language_label(), align=Qt.AlignLeft))

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(theme.SPACE_XS)
        group = QButtonGroup(self)
        current = i18n.language()
        self._buttons = {}
        for i, code in enumerate(i18n.CHOICES):
            b = button(i18n.language_name(code), "ghost")
            b.setCheckable(True)
            b.setChecked(code == current)
            # 给测试与工具一个稳定的抓手：按钮上的文字会随语言变，这个属性不会。
            b.setProperty("langChoice", code)
            b.clicked.connect(lambda _=False, c=code: self._pick(c))
            group.addButton(b)
            grid.addWidget(b, i // 2, i % 2, Qt.AlignLeft)
            self._buttons[code] = b
        # 两列网格也要等宽，否则「시스템 설정 따르기」会把那一列撑出去、另一列
        # 空一截。取全部七个里最宽的那个，两列就一样宽。
        _even_width(self._buttons.values(), _px(190))
        self._group = group
        col.addLayout(grid)

        col.addWidget(faint_label(i18n.language_hint(), align=Qt.AlignLeft))

    def _pick(self, code):
        self.language_selected.emit(code)


class SettingsDialog(QDialog):
    """设置弹窗：字号档位 + 深/浅主题 + 语言。

    **点选即刻生效并落盘**（``theme.set_scale(..., persist=True)``），随后**弹窗
    自己重建** —— 否则用户点了"特大"却看不见任何变化，会以为按钮坏了。这条理由
    与它当年还是一页时一字不差（少一档就重算一次度量，旧布局就过期了）。

    弹窗不掌握 ``QStackedWidget``，所以重建只重建自己；底下那一页由主窗口在
    ``finished`` 里按新档位重建（见 ``GomokuGame._on_settings_dialog_closed``）。

    四个档位按钮用 ``QButtonGroup`` 互斥（默认 ``autoExclusive``），外观走现成的
    ``QPushButton[variant="ghost"]:checked`` —— 不需要为它新增任何 QSS。
    """

    scale_selected = pyqtSignal(str)
    theme_toggled = pyqtSignal()
    language_selected = pyqtSignal(str)
    back_clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(i18n.t("设置"))
        self.setModal(True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self._body = None
        # 「关闭」按钮与 Esc 走同一条路：关掉就是关掉。主窗口挂在 finished 上，
        # 所以这里不需要（也不该）自己去通知谁。
        self.back_clicked.connect(self.close)
        self._build_body()

    def _build_body(self):
        """重建整只内容体（换档后度量全变了，旧布局已经不对）。"""
        old = self._body
        # 底板直接复用页面那套：Screen 自己会设 objectName("screenRoot") 与
        # WA_StyledBackground，于是 QSS 里那条 `QWidget#screenRoot` 渐变照旧生效
        # —— 不用给 QDialog 单独写背景，也就不用在 main.py 里碰任何颜色。
        body = Screen(title="设置",
                      subtitle="字号立即生效并记住；下次打开还是这一档",
                      backdrop=False, parent=self)

        group = QButtonGroup(self)
        row = []
        for name in theme.available_scales():
            b = button(theme.scale_label(name), "ghost")
            b.setCheckable(True)
            b.setChecked(name == theme.current_scale())
            # 当前档位再单独给个 tooltip，鼠标停在按钮上就能确认选中的是它。
            if name == theme.current_scale():
                b.setToolTip(i18n.t("当前档位"))
            b.clicked.connect(lambda _=False, n=name: self._on_scale_clicked(n))
            group.addButton(b)
            row.append(b)
        _even_width(row, _px(110))
        # 让 group 活到本次内容体销毁 —— 只挂在局部变量上会被 GC 掉。
        body._scale_group = group
        body.add_content(hbox(*row, spacing=theme.SPACE_SM))
        body.add_content(faint_label(i18n.tf("当前：%s", theme.scale_label(
            theme.current_scale()))))

        # 用**下限**宽度而不是固定宽度：这句文案在俄语里比中文长一倍多，钉死
        # 280px 会把「Сменить тёмную / светлую тему」裁掉后半截。给下限既保住
        # 中文版不会缩成一小条，又让长文案自己撑开。
        theme_btn = button("🌓 切换深色 / 浅色主题", "ghost")
        theme_btn.setMinimumWidth(_px(280))
        theme_btn.clicked.connect(self._on_theme_clicked)
        body.add_content(theme_btn)

        body.add_content(separator())

        # 语言放在最下面：换一次语言整只弹窗要重建成另一种文字，摆在下半区
        # 用户才不会觉得"我刚点的按钮自己跑了"。换档同理，但档位是这一页的
        # 主角，它得在最上面。
        self.language_row = _LanguageRow(self)
        self.language_row.language_selected.connect(self._on_language_clicked)
        body.add_content(self.language_row, Qt.AlignLeft)

        back = button("关闭", "primary", width=_px(150))
        back.clicked.connect(self.back_clicked.emit)
        body.add_content(back)

        self.layout().addWidget(body)
        self._body = body
        if old is not None:
            self.layout().removeWidget(old)
            old.deleteLater()
        self.adjustSize()

    def _on_scale_clicked(self, name):
        """先让主窗口落盘并作废棋盘缓存，再重建自己。

        顺序不能反：``scale_selected`` 是直连信号，``emit`` 返回时主窗口那一段
        已经做完了，此时重建才读得到新的 ``theme.current_scale()``。
        """
        self.scale_selected.emit(name)
        self._build_body()

    def _on_theme_clicked(self):
        self.theme_toggled.emit()
        # 主题不改任何像素度量，但弹窗自己的配色要跟上。
        self._build_body()

    def _on_language_clicked(self, code):
        """换语言：先让主窗口落盘并切过去，再重建自己。

        顺序与换档一字不差，理由也一样 —— ``language_selected`` 是直连信号，
        ``emit`` 返回时 ``i18n`` 已经切好了，此时重建读到的才是新语言；反过
        来先重建，弹窗会整体重画成**旧**语言，用户会以为点了没用。

        重建是必须的：这几十条文案是**构造期**写进控件的，不重建就留在原地。
        """
        self.language_selected.emit(code)
        self._build_body()


# ==================== 自定义难度 ====================
#
# 五项参数取自 ``engine._search_request`` 真正会下发的那些键 —— 一个键都不多。
# 每项都在下面标出**两份引擎是否都认它**，因为这两件事不一样：
#  ``time`` / ``max_depth`` / ``vcf_budget`` / ``qply``  两份都读
#  ``enhance``（LMR + 强制着法延伸）                     只有 C++ 有
# 而且降级到本地时 ``ai_move`` 的回退分支压根不转发配置，本地按自己的档位表跑
# 最强档 —— 也就是说**自定义参数在回退时几乎全部不生效**。界面必须把这件事说
# 出来，否则就是"静默降级"换了个壳。
_CUSTOM_OPTIONS = (
    # 连续量 —— 滑杆。刻度范围覆盖全部预设档位再留余量：
    #   时限  预设 0.5–20 s，滑杆到 60 s（原来的 30 s 上限于"想让它多想一会儿"
    #         这件事没有意义，用户点名要 60）。
    #   深度  预设 2–24 层，滑杆到 40。
    #   qply  预设 2–12 层，滑杆到 24。
    ("time", "思考时间上限", ("range", 10, 60, "秒", True)),
    ("max_depth", "搜索深度上限", ("range", 2, 40, "层", False)),
    ("qply", "静止搜索层数", ("range", 2, 24, "层", False)),
    # 离散量 —— 按钮。摆成滑杆看不出"关"与"低"之间是什么关系。
    ("vcf_budget", "VCF（连续冲四）预算",
     ("choice", ((0.0, "关"), (0.3, "低"), (0.5, "中"), (1.5, "高"), (3.0, "满")))),
    ("enhance", "增强搜索（LMR + 强制着法延伸）",
     ("choice", ((False, "关"), (True, "开")))),
)

#: 每项参数在**当前取值**下要不要挂一句说明。空串表示不挂。
#:
#: VCF 关掉那一句不是"提醒"，是项目自己实测出来的结论：关掉 VCF 的档位不是变
#: 简单，是**失明** —— 既算不出自己的冲四链，也看不见对手的，曾有低档因此整局
#: 没有任何机制能看见对手一条 11 手的连续冲四（见 ``tests/test_vcf.py``）。
#: 用户点名要这个开关，所以给；但代价必须写在开关旁边。
_CUSTOM_HINTS = {
    "vcf_budget": lambda v: (
        "关掉 VCF 之后 AI 会看不见连续冲四：既算不出自己的，也看不见对手的。"
        "预设的五档从不用这个办法拉开差距 —— 它们靠深度上限。"
        if float(v) <= 0.0 else ""),
    "enhance": lambda v: (
        "" if v else "关掉后与「高级」的搜索方式一致。"),
}


def _build_custom_row(label, spec, current, parent=None):
    """按 ``_CUSTOM_OPTIONS`` 里的规格造出这一行的控件。

    ``spec`` 两种形状：``("range", 下限, 上限, 单位, 是否浮点)`` 造滑杆，
    ``("choice", ((值, 文案), ...))`` 造按钮排。两种控件的对外接口同形
    （``value`` / ``set_value`` / ``value_selected``），所以 ``_build`` 与
    ``_on_reset`` 不必分情况。
    """
    if spec[0] == "range":
        lo, hi, unit, to_float = spec[1], spec[2], spec[3], spec[4]
        return _SliderRow(label, lo, hi, unit, current,
                          to_float=to_float, parent=parent)
    if spec[0] == "choice":
        return _OptionRow(label, list(spec[1]), current, parent=parent)
    raise ValueError("未知的自定义参数类型：%r" % (spec[0],))


def _custom_engine_note():
    """自定义弹窗底部那行「这份配置能生效到哪一步」。"""
    if engine.binary_path():
        return ("五项参数随每次搜索下发给 C++ 引擎；其中增强搜索只有 C++ 有。")
    return ("没找到 C++ 引擎，本机将退回本地 Python：自定义参数基本不生效，"
            "本地会按自己最强的一档跑（15 秒 / 深度 24）。")


#: 自定义难度在 QSettings 里的键。
#:
#: **落盘放在界面层，不放进 engine.py。** `engine.py` / `engine_local.py` 是零 Qt
#: 设计（`server.py`、`tools/bench.py` 这些无头程序都导入它们），一个 QSettings
#: 就会把 Qt 拖进去。无头程序照旧用 `engine.py` 里的出厂值 —— 一致且可预期。
_SETTINGS_CUSTOM_KEY = "custom_difficulty"


def _load_custom_settings():
    """开机时把上次的自定义难度读回引擎。缺项 / 损坏 / 没有记录都静默回出厂值。"""
    raw = QSettings(theme._SETTINGS_ORG, theme._SETTINGS_APP).value(
        _SETTINGS_CUSTOM_KEY, None)
    if not raw:
        return
    try:
        cfg = json.loads(raw)
    except (TypeError, ValueError):
        return
    if isinstance(cfg, dict):
        engine.set_custom_config(cfg)


def _save_custom_settings(cfg):
    """把自定义难度落盘。失败不抛 —— 存不下配置不该让一局棋开不了。"""
    try:
        QSettings(theme._SETTINGS_ORG, theme._SETTINGS_APP).setValue(
            _SETTINGS_CUSTOM_KEY, json.dumps(cfg))
    except (TypeError, ValueError):
        pass


#: 界面语言在 QSettings 里的键。落盘放在界面层，理由与自定义难度那份一样：
#: ``i18n`` 是零 Qt 模块，``server.py`` 那些无头程序导入它不该被拖进 Qt。
_SETTINGS_LANG_KEY = "ui_language"


def _load_language():
    """开机时把上次选的语言读回 ``i18n``。

    **没存过就等同于「跟随系统」**（``i18n`` 的出厂值）—— 一个从没进过设置页
    的用户，系统是什么语言就该看到什么语言。
    """
    # 系统语言先喂进去：即使这一步之后读不到任何落盘值，"跟随系统"也有答案。
    i18n.set_system_locale(QLocale.system().name())
    saved = QSettings(theme._SETTINGS_ORG, theme._SETTINGS_APP).value(
        _SETTINGS_LANG_KEY, None)
    i18n.set_language(saved if saved else i18n.SYSTEM)


def _save_language(code):
    """把语言选择落盘。失败不抛 —— 存不下偏好不该让界面开不了。"""
    try:
        QSettings(theme._SETTINGS_ORG, theme._SETTINGS_APP).setValue(
            _SETTINGS_LANG_KEY, code)
    except (TypeError, ValueError):
        pass


class CustomDifficultyDialog(QDialog):
    """自定义算法与难度：五项参数 + 恢复默认 / 取消 / 确定。

    打开时用**当前的**自定义配置回显，所以"改第二次"是在上次的基础上改。
    「确定」把结果放进 ``self.chosen``，由调用方写回引擎并开局；「取消」什么都不动。
    """

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.setWindowTitle(i18n.t("自定义算法和难度"))
        self.setModal(True)
        #: 用户点了「确定」之后的那份配置。取消时保持 ``None``。
        self.chosen = None
        self._cfg = dict(cfg)
        self._rows = {}
        self._hints = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self._build()

    def _build(self):
        body = Screen(title="自定义算法和难度",
                      subtitle="五项参数随每次搜索下发，只影响这一档",
                      backdrop=False, parent=self)

        for key, label, spec in _CUSTOM_OPTIONS:
            row = _build_custom_row(label, spec, self._cfg[key], self)
            row.value_selected.connect(lambda v, k=key: self._on_value(k, v))
            self._rows[key] = row
            body.add_content(row, Qt.AlignLeft)
            hint = faint_label("", align=Qt.AlignLeft)
            body.add_content(hint, Qt.AlignLeft)
            self._hints[key] = hint

        for key in self._rows:
            self._refresh_hint(key)

        body.add_content(separator())
        body.add_content(faint_label(_custom_engine_note()))

        # 同样用下限而不是固定宽度：「恢复默认」在英语里是 "Restore defaults"，
        # 钉死 130px 会把它裁成 "estore defaults"。
        reset_btn = button("恢复默认", "ghost")
        cancel_btn = button("取消", "ghost")
        ok_btn = button("确定", "primary")
        for b, w in ((reset_btn, 130), (cancel_btn, 110), (ok_btn, 130)):
            b.setMinimumWidth(_px(w))
        reset_btn.clicked.connect(self._on_reset)
        cancel_btn.clicked.connect(self.reject)
        ok_btn.clicked.connect(self._on_accept)
        body.add_content(hbox(reset_btn, cancel_btn, ok_btn,
                              spacing=theme.SPACE_SM))

        self.layout().addWidget(body)
        self.adjustSize()

    def _on_value(self, key, value):
        self._cfg[key] = value
        self._refresh_hint(key)

    def _refresh_hint(self, key):
        fn = _CUSTOM_HINTS.get(key)
        self._hints[key].setText(fn(self._cfg[key]) if fn else "")

    def _on_reset(self):
        """回到出厂值。**只动这个弹窗**，不碰引擎 —— 用户还可以再改回去。"""
        defaults = engine.custom_defaults()
        for key, _label, _spec in _CUSTOM_OPTIONS:
            self._cfg[key] = defaults[key]
            self._rows[key].set_value(defaults[key])
            self._refresh_hint(key)

    def _on_accept(self):
        self.chosen = dict(self._cfg)
        self.accept()


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
        self.progress_bar.setFixedHeight(_px(5))
        self.progress_bar.setFixedWidth(_px(300))
        self.add_content(self.progress_bar)

        self._elapsed = QElapsedTimer()
        self._elapsed.start()
        self._tick = QTimer(self)
        self._tick.setInterval(_SPLASH_TICK_MS)
        self._tick.timeout.connect(self._advance)
        self._tick.start()

        if not theme.has_cjk_font():
            warn = faint_label("未找到简体中文字体，界面可能显示为方块；"
                               "Linux 请安装 fonts-noto-cjk")
            # 这句是整屏最长的文案，外文（俄语）比窗口还宽 —— 允许换行，
            # 贴底显示完整。它只在"候选链一个中文字体都没命中"时出现。
            # **拉满整行**（对齐只给垂直方向）：不拉满时 QLabel 会缩到"启发式
            # 折行宽度"，俄语在启发宽度下要 3 行、容器却只按 sizeHint 给 2 行，
            # 最后一行仍会被裁。拉满后宽度就是页面可用宽，折几行都放得下。
            warn.setWordWrap(True)
            warn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
            self.add_footer(warn, Qt.AlignVCenter)

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
        # 复盘标记 ``(best_rc, played_rc, played_player)``。默认 None ——
        # 正常对局这条分支整个不存在，绘制路径零影响（见 ``paintEvent``）。
        self.review_marker = None
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

    def set_review_marker(self, best_rc, played_rc, played_player):
        """复盘标记：把"这一步的最优点"圈出来，把"你实际下的那手"画成幽灵子。

        ``best_rc`` / ``played_rc`` 是 ``(r, c)``（任一可为 ``None``）。
        ``played_player`` 决定幽灵子的颜色。**只存状态、不落子** —— 幽灵子不进
        ``self.board``，复盘棋盘展示的仍是"那一手落子之前"的真实局面。

        走对的那一手两者**重合**（``played == best``）：幽灵子先画、圈后画，
        于是看到的是一颗被圈住的子 —— "你下的这颗，正是该下的那颗"。这是
        故意的，不必特判成只画圈。
        """
        self.review_marker = (best_rc, played_rc, played_player)
        self.update()

    def set_win_cells(self, cells, player):
        """终局五连高亮（``cells`` 来自 ``engine.win_line``）。

        ``player`` 只被记下、绘制不读它：红线从五连本身定方向与长度，与是谁赢的
        无关（这个设计是 2026-10-01 定的，此前按棋子颜色分两支环，已删）。
        保留这个参数是因为"谁赢了"仍是终局状态的一部分，且 ``tests`` 钉着它。
        """
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

        # 终局五连：一条红线划过（在遮罩弹出前 1 秒画好，见 ``GAME_OVER_DELAY_MS``）
        if self.win_cells:
            first, last = self.win_cells[0], self.win_cells[-1]
            x0, y0 = self.geom.px(*first)
            x1, y1 = self.geom.px(*last)
            dx, dy = x1 - x0, y1 - y0
            dist = math.hypot(dx, dy) or 1.0
            ux, uy = dx / dist, dy / dist
            painter.setBrush(Qt.NoBrush)

            # 两端各探出半个格子：横竖 0.5 格，斜线 0.7 格（≈√2/2）。
            #
            # 斜线为什么要多那 √2：0.7 格沿对角线分解回 x/y 分量恰好是 0.5 格
            # （0.7/√2 ≈ 0.495）。取同一个 0.5 的话，斜线的端点会在两个轴向上
            # 都缩水三成，看着比横竖的短一截 —— 三种方向的"探出量"要按轴对齐
            # 才一致。
            #
            # 方向从**棋盘坐标**判断而不是像素差：``geom.px`` 是浮点，横线的
            # dy 未必恰好是 0。
            dr = last[0] - first[0]
            dc = last[1] - first[1]
            over = self.geom.cell * (0.7 if (dr and dc) else 0.5)

            # 必须用 ``FlatCap``：``RoundCap`` 会在两端再多画半个笔宽（旧实现是
            # RoundCap + ``r*1.1`` 的粗笔，两端冒出去将近一个子）。
            #
            # 画两遍：先深色衬、再红芯。**单画一条红是不够的** —— 红在橙金木盘
            # 上的亮度对比只有 2.30–3.10:1，整盘缩到屏幕尺寸就化在木色里了
            # （2026-10-01 实测：4.5px 的红线在 726px 的盘面上看不出来）。
            # 衬用跨主题的 ``LAST_DARK``（"白子上的深色环"那支，木盘上 5.38:1），
            # 它把线从木盘上抬起来；红芯负责"这是一条红线"。
            a = QPointF(x0 - ux * over, y0 - uy * over)
            b = QPointF(x1 + ux * over, y1 + uy * over)
            back_pen = QPen(QColor(theme.LAST_DARK), max(2.0, r * 0.36))
            back_pen.setCapStyle(Qt.FlatCap)
            painter.setPen(back_pen)
            painter.drawLine(a, b)
            line_pen = QPen(QColor(_WIN_LINE_COLOR), max(2.0, r * 0.20))
            line_pen.setCapStyle(Qt.FlatCap)
            painter.setPen(line_pen)
            painter.drawLine(a, b)

        # 最后一手：与棋子**反色**的环。旧的固定红圈叠在白子上只有 2.1:1，
        # 叠在黑子上也发闷。
        #
        # 终局不画（``win_cells`` 非空时）。最后一手必然在五连里，而那个环正好
        # 套在五连两端的那颗子上、压在红线之上 —— 线于是被切断一截，看着像
        # "没延长出去"，那颗子也像个还没落定的候选。2026-10-01 用户报的正是这个。
        if self.last_move and not self.win_cells:
            lr, lc, player = self.last_move
            x, y = self.geom.px(lr, lc)
            color = theme.LAST_LIGHT if player == 1 else theme.LAST_DARK
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor(color), max(1.5, r * 0.22)))
            painter.drawEllipse(QPointF(x, y), r * 0.72, r * 0.72)

        # 复盘标记（只有复盘棋盘会设，见 ``set_review_marker``）。
        #
        # 两层都画：**幽灵子**是"你实际下的那一手"，它不在 ``self.board`` 里，
        # 因为这张盘展示的是落子**之前**的局面；**圈**是"算法认为该下的那手"。
        # 只圈不画幽灵子的话，"该下哪"看得见、"你下在哪"看不见，两者没法对照；
        # 反过来只画幽灵子也一样。
        if self.review_marker is not None:
            m_best, m_played, m_player = self.review_marker
            if m_played is not None:
                gr, gc = m_played
                gx, gy = self.geom.px(gr, gc)
                base = theme.STONE_B if m_player == 1 else theme.STONE_W
                ghost2 = QColor(base)
                ghost2.setAlpha(120)
                painter.setPen(QPen(QColor(theme.GRID), max(1.0, r * 0.12)))
                painter.setBrush(QBrush(ghost2))
                painter.drawEllipse(QPointF(gx, gy), r, r)
            if m_best is not None:
                br, bc = m_best
                bx, by = self.geom.px(br, bc)
                # 与终局红线同一套"深色衬 + 亮色芯"：单画一圈 ``DANGER`` 在
                # 橙金木盘上会化掉（见上面五连那段实测）。
                painter.setBrush(Qt.NoBrush)
                back = QPen(QColor(theme.LAST_DARK), max(2.0, r * 0.36))
                painter.setPen(back)
                painter.drawEllipse(QPointF(bx, by), r * 0.9, r * 0.9)
                core = QPen(QColor(theme.DANGER), max(1.5, r * 0.20))
                painter.setPen(core)
                painter.drawEllipse(QPointF(bx, by), r * 0.9, r * 0.9)

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


# ==================== 主题图标（自绘） ====================
#
# 标题行那颗太阳 / 月亮**不用 emoji 字符**，自己画。原因是实测出来的，不是偏好：
#
# ``☀``（U+2600）在 Unicode 里是**文本呈现**字符 —— 规范上它默认就该是单色的，
# 要彩色必须跟一个变体选择符 U+FE0F。而 ``🌙``（U+1F319）虽然是 emoji 呈现，
# 在本机也被 fontconfig 交给了单色字体。实测：整条 UI 字体链
# （``theme.resolve_family()``，末端落在 ``sans-serif`` → Noto Sans CJK SC）
# 上一个彩色 emoji 字体都排不到，``Noto Color Emoji`` 被压在后面永远轮不上，
# 于是两个字形都是黑的。**加 VS16 也救不回来**（Qt5 不会为变体选择符重新挑
# 字体）。更麻烦的是"长什么样"取决于机器上装了什么字体，换台机器就变样。
#
# 自绘之后颜色取 ``theme.*``、形状自己定，跨平台一致，也才有了"太阳是暖的"
# 这件事。
#
# **这是界面 chrome 里唯一一处暖色**，与 ``theme.py`` 开头那条"界面 chrome
# 一律冷调、棋盘是全局唯一的暖色"有出入。18px 的图标，代价可控 —— 换来的是
# "太阳就该是暖的"这个直觉。

_SUN_DOTS = 8                       # 太阳外圈圆点数
_SUN_DISC = 0.62                    # 中心圆盘半径 / r
_SUN_DOT = 0.15                     # 单个外圈圆点半径 / r
_SUN_ORBIT = 0.82                   # 外圈圆点轨道半径 / r
_ICON_PX = 18                       # 图标设计边长；按钮高 32，留白足够


def _is_sun() -> bool:
    """**当前**主题该显示哪个图标：浅色显示太阳、深色显示月亮。

    图标画的是"你正在哪个主题里"，不是"点一下会去哪儿"。这两种恰好差一个
    反向，2026-10-01 用户明确要的是前者 —— 白天配太阳、夜晚配月亮，一眼就
    知道自己在哪一档，不必先反推。
    """
    return theme.current_theme() == "light"


def _sun_color() -> QColor:
    """太阳的暖金**按主题取**，两边都取同族里够亮的那个。

    浅色档用 ``theme.WOOD_DARK``（对 ``SURFACE_2`` 底色 **2.36:1**），
    深色档用 ``theme.WOOD``（6.27:1）。与 ``ACCENT`` 分深/浅两档是同一个做法
    （见 ``theme._PALETTES``）。

    **2.36:1 是明知故犯的。** 这条线本来该有 3:1（非文字图形），够线的只有同族
    里最暗的 ``WOOD_EDGE``（4.49:1）—— 而 2026-10-01 用户的评价正是"颜色这么
    暗淡"。一个 18px 的主题装饰图标，旁边就写着"当前主题(点击以切换)："，
    颜色不是唯一的信息载体，取悦眼睛比守那 0.6 更值。要回退只需把这一行换成
    ``theme.WOOD_EDGE``。
    """
    return QColor(theme.WOOD_DARK if theme.current_theme() == "light"
                  else theme.WOOD)


def _moon_color() -> QColor:
    """银白。取 ``theme.TEXT`` —— 它是这套调色板里唯一的近白中性色
    （深色档为冷调银白，见 ``theme._PALETTES``）。

    **月亮只在深色主题出现**（见 ``_is_sun``），所以"银白对白底看不见"这件
    事眼下不成立。但它是这条映射的**隐含前提**：谁要把 ``_is_sun`` 翻回去，
    必须同时换掉这个颜色，否则月亮会在浅色主题里凭空消失。
    """
    return QColor(theme.TEXT)


class ThemeIcon(QWidget):
    """标题行的太阳 / 月亮。**自绘，不用 emoji**（理由见上面的分节注释）。

    画哪个由 ``_is_sun()`` 决定：浅色主题→太阳，深色主题→月亮。

    颜色在 ``paintEvent`` 里现取 ``theme.*``，所以主题切换后必须 ``update()``
    重画一次 —— QSS 的 repolish 管不到 QPainter，与两张图表同理。
    """

    def __init__(self, sun: bool, size: int = _ICON_PX, parent=None):
        super().__init__(parent)
        self._sun = bool(sun)
        self._size = float(size)
        self.setFixedSize(size, size)
        # 不吃鼠标事件：它盖在按钮正中，吃掉中心那一小块主题按钮就点不动了
        # （与 ``ui_kit.card_button`` 左上角那个序号角标同一条教训）。
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_TranslucentBackground)

    def set_sun(self, sun: bool) -> None:
        """切换太阳 / 月亮。值没变就不重画 —— 重画一次要多跑一轮绘制。"""
        sun = bool(sun)
        if sun != self._sun:
            self._sun = sun
            self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        c = QPointF(self.width() / 2.0, self.height() / 2.0)
        # 外圈圆点的外缘就是 ``_SUN_ORBIT + _SUN_DOT``，量出来 0.97 —— 留的那
        # 0.03 是给抗锯齿的，不然最外那圈点会被控件边缘啃掉一条。
        r = self._size / 2.0
        if self._sun:
            self._paint_sun(p, c, r)
        else:
            self._paint_moon(p, c, r)
        p.end()

    def _paint_sun(self, p: QPainter, c: QPointF, r: float) -> None:
        """一个实心圆盘 + 一圈小圆点 —— 就是 ☀ 的画法。

        这里**刻意不用**放射状长射线。那种"细长尖刺 + 小圆心"是好几家 AI 产品
        的标志形态，缩到 18px 就几乎重合（2026-10-01 用户一眼看出来）。圆点则
        怎么摆都不会撞上任何一家的 logotype。

        三个比例都是量出来的（``/tmp/sun_dots2.py`` 的 V 组）：盘再大一圈就成了
        一个点，点再小一圈就糊进抗锯齿里。
        """
        color = _sun_color()
        disc = r * _SUN_DISC
        dot = r * _SUN_DOT
        orbit = r * _SUN_ORBIT
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(color))
        for i in range(_SUN_DOTS):
            # 减去 π/2：让第一个点落在正上方而不是正右方，看起来才"正"
            a = math.pi * 2.0 * i / _SUN_DOTS - math.pi / 2.0
            p.drawEllipse(QPointF(c.x() + math.cos(a) * orbit,
                                  c.y() + math.sin(a) * orbit), dot, dot)
        p.drawEllipse(c, disc, disc)

    def _paint_moon(self, p: QPainter, c: QPointF, r: float) -> None:
        """月牙 = 大圆**减去**一个偏移的等大圆。

        用 ``QPainterPath.subtracted`` 而不是"在圆上再画一个底色圆"：按钮有
        hover / pressed / disabled 四种底色，拿一个固定颜色去盖，三种状态里
        总有一种会露馅。真正的布尔减不关心底下是什么。
        """
        outer = QPainterPath()
        outer.addEllipse(c, r, r)
        # 偏移量决定月牙最厚处：等大圆错开 d，留下的月牙最厚就是 d。
        # 0.62r 实测既不瘦成一条线，也不胖成一个缺口圆。
        hole = QPainterPath()
        hole.addEllipse(QPointF(c.x() + r * 0.62, c.y() - r * 0.30), r, r)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(_moon_color()))
        p.drawPath(outer.subtracted(hole))


# ==================== 游戏面板（右侧） ====================
class GamePanel(QFrame):
    """右侧信息面板。

    继承 ``QFrame`` 而不是 ``QWidget``：QFrame 是 Qt 已知类，QSS 的
    ``background`` 直接生效，不需要 ``WA_StyledBackground`` 那个补丁。
    """

    theme_clicked = pyqtSignal()

    #: 主题按钮前面那句说明。文案里写了"点击以切换"，所以**整条都是按钮** ——
    #: 只让 18px 的图标可点，这句话就是在骗人。
    _THEME_CAPTION = "当前主题(点击以切换)："

    def __init__(self):
        super().__init__()
        self.setProperty("role", "panel")
        # 走 ``theme.PANEL_W`` 而**不是**模块常量 ``PANEL_W``：后者在 import 那
        # 一刻就绑成了 normal 档的值，换档后不会跟着走。
        self.setFixedWidth(theme.PANEL_W)
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

        # 段 1：标题
        #
        # 设置齿轮**不在这里** —— 它是主窗口右上角那枚固定的按钮，浮在面板
        # 之上，所有页面同一个位置（见 `GomokuGame.settings_btn`）。曾经放这
        # 一行里，被变体 QSS 的 padding 裁成了残缺的一条。
        head = QHBoxLayout()
        head.setSpacing(theme.SPACE_SM)
        head.addWidget(title_label("五子棋 AI", role="panel-title"),
                       1, Qt.AlignVCenter)
        layout.addLayout(head)

        # 段 1b：主题切换
        #
        # **为什么另起一行**：面板内区只有 216px（``PANEL_W`` − 2×``SPACE_XL``），
        # 而 20px 的标题实测 88px、这行说明文字（14px）实测 150px，再加 18px
        # 图标与间距已经是 260px 开外 —— 和标题同排必然溢出，只能挤掉标题。所以
        # 拆成两行；多出来的 32px 高度由下面两张图表的 stretch 吸收。
        #
        # 说明文字与图标**都挂在按钮上**（不是并列的两个控件）：文案写着"点击
        # 以切换"，整条就该是热区，只让 18px 的图标可点是自相矛盾的。
        self.theme_btn = button("", "ghost", height=_px(32))
        self.theme_btn.setToolTip(i18n.t("切换深色 / 浅色主题"))
        self.theme_btn.clicked.connect(self.theme_clicked.emit)

        # 字号取 ``subtitle`` 那一档（14px），不用 ``faint``（12px）：12px 在这
        # 条按钮上又小又灰，用户 2026-10-01 直接指出来了。14px 是这套面板里
        # 次要文字的上限，再往上一档 16px 就顶满 216px 内区了。
        self.theme_caption = subtitle_label(self._THEME_CAPTION)
        self.theme_caption.setAlignment(Qt.AlignCenter)
        self.theme_caption.setParent(self.theme_btn)
        # 长语言里这句说明比面板内区还宽（俄语 532px）：开 wordWrap，宽度与
        # 按钮高度一起在 ``_fit_theme_button`` 里按**最终定下的面板宽度**现算
        # —— 面板宽度本身要先由其余元素的需求定出来。
        self.theme_caption.setWordWrap(True)
        # 先 polish 再量尺寸 —— 14px 是 QSS 里定的，不 polish 量到的是按钮
        # 那一档 16px，算出来的按钮宽度就偏了（与 ``ui_kit.card_button``
        # 量左上角序号同一条教训）。
        self.theme_caption.ensurePolished()
        self.theme_caption.adjustSize()
        self.theme_caption.setAttribute(Qt.WA_TransparentForMouseEvents)

        self.theme_icon = ThemeIcon(_is_sun(), parent=self.theme_btn)
        # 按钮贴左会显得整条歪在一边（面板里其余控件都是满宽居中的），所以居中 ——
        # 但**不拉满内区**：按钮底色只包住内容，才像一个次要控件，而不是第四颗
        # 大按钮。居中的是这颗胶囊，热区仍覆盖"说明 + 图标"整条。
        layout.addWidget(self.theme_btn, 0, Qt.AlignHCenter)

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

        # 段 6（收尾）：面板宽度 —— 基准 `theme.PANEL_W` 与**当前语言的内容
        # 需求**取大者。俄语的「✕ Выйти из игры」单行 272px、难度值
        # 「Гроссмейстер」168px，都摆不进 264px 的内区；窗口设计宽度与页面
        # 可用宽度经由 ``_panel_active_w()`` 跟着这里的长宽走。中文在此路径上
        # 等于基准，视觉与从前逐字不差。
        self.ensurePolished()
        self.setFixedWidth(max(theme.PANEL_W, self._width_need()))
        self._fit_theme_button()

    #: 回合卡可能显示的状态文本（宽度预热用，见 ``_width_need``）。
    #: 与 ``update_info`` / ``set_thinking`` 里实际 set 的文案一一对应。
    _TURN_TEXTS = ("轮到你落子", "AI 思考中…",
                   "黑方获胜", "白方获胜", "你赢了！", "你输了！",
                   "平局", "平局！")

    def _width_need(self) -> int:
        """面板宽度需求：按**运行期值域**而不是构造初值推导。

        构造时各信息行还写着 "—"、回合卡还写着 "准备开始"，直接量
        ``minimumSizeHint`` 会漏掉运行期真实文本（难度名「Гроссмейстер」比
        "—" 宽 168px），长语言下面板就会定窄、把值裁掉。这里把"值会变"的
        两处 —— 难度行与回合卡 —— 挨个试穿值域里的每个文本再恢复。

        记的是**增量**：面板基准值（含内边距与边框，由控件自己算）+ 该行
        变宽出来的差值。构造期面板级的 ``minimumSizeHint`` 有缓存、子控件
        改文本后不会及时刷新，而行级的最小尺寸是即时的 —— 用增量既绕开
        缓存，也不必手工复刻内边距/边框那几个像素（它们已经含在基准里）。
        """
        panel0 = self.minimumSizeHint().width()
        need = panel0
        # 难度行：值会换成任何一档的名字（自定义档的名字最长）。
        row = self.difficulty_row
        row0 = row.minimumSizeHint().width()
        orig = row._value.text()
        for lv in list(engine.DIFFICULTY) + [engine.CUSTOM_LEVEL]:
            row.set_value(i18n.t(engine.difficulty_name(lv)))
            need = max(need, panel0 + row.minimumSizeHint().width() - row0)
        row.set_value(orig)
        # 回合卡：状态文案随对局走（「AI 思考中…」/「Вы победили!」…），
        # 状态文本是整句、可以折行，需求由卡内布局算。
        turn = self.turn_indicator
        turn0 = turn.minimumSizeHint().width()
        orig_turn = turn.label.text()
        texts = list(self._TURN_TEXTS)
        texts += [i18n.tf("%s 行动中", i18n.t(s))
                  for s in ("黑棋 ●", "白棋 ○")]
        for text in texts:
            turn.label.setText(i18n.t(text))
            need = max(need, panel0 + turn.minimumSizeHint().width() - turn0)
        turn.label.setText(orig_turn)
        return need

    def _fit_theme_button(self) -> None:
        """主题胶囊的宽度/高度：按**面板内区**与文案度量现算。

        宽度 = min(单行完整宽, 内区可用)；放不下就换行（caption 已开 wrap），
        按钮高度随行数抬。中文的说明单行短于内区，按钮尺寸与从前一致。
        """
        pad = theme.SPACE_SM
        icon = self.theme_icon
        inner = max(_px(40), self.width() - 2 * theme.SPACE_XL)
        avail = max(_px(40), inner - 2 * pad - icon.width() - theme.SPACE_XS)
        full = self.theme_caption.fontMetrics().horizontalAdvance(
            self.theme_caption.text())
        cap_w = min(full, avail)
        self.theme_caption.setFixedWidth(cap_w)
        cap_h = self.theme_caption.heightForWidth(cap_w)
        if cap_h <= 0:
            cap_h = self.theme_caption.sizeHint().height()
        # 高度也要锁：说明文字不参与按钮布局（手动 move），不锁的话它会停在
        # "启发式折行高度"上，比实际需要少一行、最后一行被裁。
        self.theme_caption.setFixedHeight(cap_h)
        btn_h = max(_px(32), cap_h + 2 * theme.SPACE_XS)
        self.theme_btn.setFixedSize(
            pad + cap_w + theme.SPACE_XS + icon.width() + pad, btn_h)
        # 尺寸从此不再变，一次定位就够（与 ``ui_kit.card_button`` 里那个
        # 角标同理），不必接 resizeEvent。
        self.theme_caption.move(pad, (btn_h - cap_h) // 2)
        icon.move(pad + cap_w + theme.SPACE_XS,
                  (btn_h - icon.height()) // 2)

    def update_theme_button(self) -> None:
        # 图标画的是**当前**主题：浅色配太阳、深色配月亮（见 ``_is_sun``）。
        self.theme_icon.set_sun(_is_sun())
        # 图标与两张图表都是自绘的，颜色在 paint 时取 theme.*，所以主题切换后
        # 必须重画一次 —— QSS 的 repolish 管不到 QPainter。
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

    def set_ai_player(self, player: int) -> None:
        """把 AI 执的子写进评分卡标题（见 ``charts.ScoreChart.set_ai_player``）。"""
        self.score_chart.set_ai_player(player)

    def _refresh_charts(self) -> None:
        self.score_chart.set_decade(self._decade)
        self.score_chart.set_series(self._series)
        self.win_chart.set_series(self._series)

    # ---- 状态更新 ----

    def update_info(self, turn, difficulty, status, undo_count, move_count,
                    human=1, local=False):
        players = {1: "黑棋 ●", 2: "白棋 ○"}
        # 本地对战没有 AI：难度/引擎/端口三行如实写"—"，而不是把上一局的档位
        # 留在那里（面板上那三个数是"这一局用什么引擎在算"，本地局没有答案）。
        if local:
            self.difficulty_row.set_value("—")
            self.engine_row.set_value("—")
            self.port_row.set_value("—")
        else:
            # 显示档位**名称**而不是"N 级"。档位号重构后从 3 档变成 5 档，旧编号
            # 已经没有稳定含义；名称直接来自 engine.DIFFICULTY 那一张表，改表即改
            # 界面，不会出现"界面上写 3 级、代码里是中级"这种两处对不上的情形。
            # InfoRow.set_value 是**值**的入口，不走 i18n（值多为坐标 / 比分 /
            # 计时，逐个查表纯属浪费）。所以这里明确翻一次 —— 难度名是唯一的
            # 例外：它是一句文案，只是碰巧从 engine 的表里来。
            self.difficulty_row.set_value(
                i18n.t(engine.difficulty_name(difficulty)))
            # 引擎名（"C++ 引擎" / "Python (本地)"）**不翻** —— 它与日志里的
            # 写法必须一致，用户拿这个字符串去搜日志时要能搜到。
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
                    self.turn_indicator.set_turn(
                        turn, i18n.tf("%s 行动中", i18n.t(players[turn])))
        elif status in ("黑方获胜", "白方获胜"):
            # 本地对战：谁赢就以谁的子色落定，"轮到你落子"那种措辞在这里没有
            # 主语，所以整句由 `status` 给。
            stone = 1 if status == "黑方获胜" else 2
            self.turn_indicator.set_result(stone, status, "win")
            self.stop_timer()
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
def _page_content_w() -> int:
    """选择页可用的内容宽度（已扣掉页面左右边距）。

    量的是"窗口大概会有多宽"而不是本页当前的 ``width()`` —— 选择页是在
    构造期把自己排好的，那时它还没进布局，``width()`` 是 0。
    """
    w = _design_size()[0]
    if _autofit_enabled():
        scr = QApplication.primaryScreen()
        if scr is not None:
            w = min(w, int(scr.availableGeometry().width() * 0.92))
    return max(_px(320), w - 2 * theme.SPACE_XL)


def _card_fit_size(n: int, gap: int) -> int:
    """``n`` 张卡 + ``(n-1)`` 段 gap 塞进页面内容宽度时的卡片边长。

    封顶 ``theme.CARD_PX``（正常屏幕上一张都不缩，观感与从前一致），下限
    ``_px(88)`` —— 再小就放不下卡面那两行字，宁可让它横向滚动。
    """
    avail = _page_content_w()
    return max(_px(88), min(theme.CARD_PX, (avail - (n - 1) * gap) // n))


def _strength_bar(players, diameter=28, card_px=None):
    """N 颗棋子的强度条：**颗数越多排得越紧，棋子本身不缩小**。

    ``ui_kit.stone_row`` 是个纯 hbox —— 每颗子一个 1.25×d 的方盒（多出来的
    1/4 是留给接触投影的），盒间距固定 ``SPACE_XS``。d=28 时五颗要
    5×35 + 4×4 = 191px，而卡片内容区只有 ``CARD_PX - 2*SPACE_MD`` = 136px，
    最后一颗被卡片右缘切掉，宗师那张肉眼只剩四颗半。

    这里改成**盒子重叠摆放**：颗粒少时步距保持原样（盒宽 + SPACE_XS），
    排不下才收紧，一直收到刚好塞进内容区。压缩的只是盒间距，**直径始终是
    d** —— 棋子的绝对大小必须恒定，"几颗"才只由长度表示；把子改小会让
    5 颗小的看着比 3 颗大的还弱，而这一页的子只表示强度，不表示别的。

    d 是多少不该在这里写死：``StoneFace`` 的方盒边长是它自己的实现细节
    （``round(d * 1.25)``），抄一份过来就等于把两个文件钉死，ui_kit 那边
    一改这里就静默画歪。直接问控件要尺寸。
    """
    # 卡片内容区：card_button 的 contentsMargins 左右各留 SPACE_MD。
    # ``card_px`` 由调用方传**该页实际用的卡片边长** —— 五张卡在小屏上会被
    # `_card_fit_size` 压小，这里再按 ``theme.CARD_PX`` 算宽度就会溢出。
    avail = (theme.CARD_PX if card_px is None else card_px) - 2 * theme.SPACE_MD
    side = StoneFace(players[0], diameter).width()     # 方盒边长，含投影余量
    if len(players) == 1:
        step = side
    else:
        step = min(side + theme.SPACE_XS,                  # 今天的盒间距
                   (avail - side) // (len(players) - 1))   # 排不下时收紧
    bar = QWidget()
    bar.setFixedSize(avail, side)
    x0 = (avail - ((len(players) - 1) * step + side)) // 2
    for i, player in enumerate(players):
        StoneFace(player, diameter, bar).move(x0 + i * step, 0)
    return bar


def _custom_mark():
    """自定义卡卡面上的「?」，边长与同一排难度卡上的棋子方盒同高。

    高度必须**问控件要**，理由与 ``_strength_bar`` 里那段一样：``StoneFace``
    的方盒是 ``round(d * 1.25)``，抄一份数字过来就把它钉死了。同一排卡片的
    face 高度只要一致，六张卡看起来才是同一套。

    **不收卡片边长参数**（``_strength_bar`` 收，是为了算一排子排不排得下）
    —— 这里只有一颗图形，卡片被 ``_card_fit_size`` 压小与否都不影响它。
    """
    return QuestionMark(StoneFace(1, 28).width())


def _fit_card_row(cards, gap):
    """把一组卡片按**当前语言的内容需求**排进选择页：放不下就折行。

    宽度先取基准（``_card_fit_size``：中文下这一排刚好排满一行），再按每张卡
    上"最长的不可断单元"放大 —— 一个单词宽于卡片就会被右缘裁掉，而换行救不了
    它。放大后一行排不下就折成多行（俄语难度页 6 张卡分 3+3 两行），每行居中，
    全部卡片统一宽高。

    中文文案的最长单元都短于基准宽度，所以这条路径在中文下**恒等于原布局**
    （一行、原尺寸）；它只在英语/俄语这类长文案下生效。
    """
    n = len(cards)
    if n == 0:
        return QWidget()
    avail = _page_content_w()
    unit_w = max(card.width() for card in cards)
    for card in cards:
        for lbl in card.findChildren(QLabel):
            if lbl.property("role") in ("card-text", "card-sub"):
                unit_w = max(unit_w,
                             lbl.minimumSizeHint().width() + 2 * theme.SPACE_MD)
    unit_w = min(unit_w, avail)          # 页面本身不许被一张卡撑破
    per_row = n
    if n * unit_w + (n - 1) * gap > avail:
        per_row = max(1, (avail + gap) // (unit_w + gap))
        rows = (n + per_row - 1) // per_row
        per_row = (n + rows - 1) // rows      # 行间尽量均匀：6 张 = 3+3
    # 宽度定了再算高度：卡片高度随换行行数走，统一取这一排的最大值。
    for card in cards:
        card.setFixedWidth(unit_w)
    unit_h = max(max(card.height(), card_fit_height(card, unit_w))
                 for card in cards)
    for card in cards:
        card.setFixedSize(unit_w, unit_h)

    holder = QWidget()
    col = QVBoxLayout(holder)
    col.setContentsMargins(0, 0, 0, 0)
    col.setSpacing(gap)
    for start in range(0, n, per_row):
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(gap)
        lay.addStretch(1)
        for card in cards[start:start + per_row]:
            lay.addWidget(card)
        lay.addStretch(1)
        col.addWidget(row)
    return holder


class SelectionScreen(Screen):
    """对战模式 / 执棋颜色 / AI难度 / 复盘强度 选择。

    四个模式共用 ``Screen`` 的骨架与节奏，差异只剩标题文案与卡片行 —— 历史上
    两条分支各自抄了一份 stretch/spacing（一个 30 一个 25，没有理由）。
    """

    color_selected = pyqtSignal(int)  # 0=黑先, 1=白后
    difficulty_selected = pyqtSignal(int)  # 档位 1-5，对应 engine.DIFFICULTY
    mode_selected = pyqtSignal(int)  # 0=挑战AI, 1=本地对战, 2=局域网联机
    review_level_selected = pyqtSignal(int)  # 复盘强度，档位 1-5

    #: 四个模式的标题/副标题，与 ``setup_ui`` 里的卡片分支一一对应。
    _TITLES = {
        "mode": ("选择对战模式", "挑战 AI、与身边人对坐，或局域网联机"),
        "color": ("选择执棋颜色", "黑棋为先手，白棋为后手"),
        "difficulty": ("选择 AI 难度", "难度越高，AI 思考越深入"),
        "review": ("选择复盘强度",
                   "复盘强度不能低于本局难度（%s）"),
    }

    def __init__(self, mode="color", min_level=1):
        self.mode = mode
        self.min_level = int(min_level)
        title, subtitle = self._TITLES[mode]
        if mode == "review":
            subtitle = i18n.tf(subtitle,
                               i18n.t(engine.difficulty_name(self.min_level)))
        super().__init__(title=title, subtitle=subtitle, backdrop=True)
        self._cards = []
        self.setup_ui()

    def setup_ui(self):
        # 卡片行的间距：难度/复盘页 5 张卡用 LG(16)，颜色/mode 页只有 2 张，
        # 宽间距是那两张页面的节奏。卡片边长按"这一页要放几张"现算，塞不下
        # 就整体缩小（封顶 CARD_PX，正常屏幕上等于不缩）。
        gap = (theme.SPACE_LG if self.mode in ("difficulty", "review")
               else theme.SPACE_XL)

        if self.mode == "mode":
            size = _card_fit_size(3, gap)
            # 三张大卡：挑战 AI / 本地对战 / 局域网联机。face 用棋子本身 ——
            # "对面是程序还是人"这件事，一颗子和两颗子比两个字更容易一眼分
            # 出来；局域网那张同样是两颗子（两个玩家各一台设备）。
            cards = [("挑战 AI", "primary", 0,
                      _strength_bar([1], card_px=size), "与算法对弈"),
                     ("本地对战", "success", 1,
                      _strength_bar([1, 2], card_px=size), "两人同机轮流下"),
                     ("局域网联机", "danger", 2,
                      _strength_bar([1, 2], card_px=size), "和好友各用一台设备")]
            for i, (text, tone, value, face, sub) in enumerate(cards):
                btn = card_button(text, tone, face=face, sub=sub,
                                  index=f"{i + 1:02d}", size=size)
                btn.clicked.connect(lambda _=False, v=value:
                                    self.mode_selected.emit(v))
                self._cards.append(btn)
        elif self.mode == "color":
            size = _card_fit_size(2, gap)
            # 卡面直接放那颗子本身（黑 = player 1），不再用 ⚫/⚪ 字符 ——
            # 那两个字符由 CJK 字体回退渲染成一个小圆点，既不是棋子也不是
            # 那个颜色，是这张卡片最关键的区分信息却最看不清的地方。
            cards = [("黑棋", "black", 0, 1, "先手"),
                     ("白棋", "white", 1, 2, "后手")]
            for i, (text, tone, value, player, sub) in enumerate(cards):
                btn = card_button(text, tone, face=StoneFace(player), sub=sub,
                                  index=f"{i + 1:02d}", size=size)
                btn.clicked.connect(lambda _=False, v=value:
                                    self.color_selected.emit(v))
                self._cards.append(btn)
        elif self.mode in ("difficulty", "review"):
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

            # 复盘页**只列不低于本局难度**的档位（用户点名的口径：初级输了就只能
            # 用初级及以上来复盘）。在这里过滤而不是在 `__init__` 里改 `levels`：
            # `index` 标签写的是真实档位号（"02" 就是第 2 档），过滤后仍要指对。
            if self.mode == "review":
                levels = [lv for lv in levels if lv >= self.min_level]
                # 配色跟着**真实档位号**走（第 2 档是 success，与难度页一致），
                # 而不是把过滤后的列表从头上重新配一遍色。
                tones = tuple(tones[lv - 1] for lv in levels)

            # 强度条的棋子**按主题取色**。棋盘上那套材质是**对着木色**调的：
            # `theme.STONE_B_GRAD` 那三档渐变（也就是黑子本体）压在深色卡面
            # `theme.SURFACE` 上只有 **1.11:1**，靠一圈近乎同色的 `STONE_B_RIM`
            # 与暖色投影撑着 —— 在 d=60 的颜色页上还看得过去，到这张 28px 的
            # 强度条上就糊成几个黑块。白子反过来：对深色卡面 9.92:1，对浅色
            # 卡面却只有 1.02:1。所以没有"安全的那一色"，只能按当前主题挑。
            #
            # （这里写常量名而不是写死十六进制：本文件被
            #   tests/test_no_literal_colors.py 守着，连注释里的颜色字面量都算。）

            #
            # **换色不丢信息**：这一页的子只表示"几颗"（强度），不表示"哪一方"
            # —— 那是上一页的事。反过来，颜色选择页与面板的回合指示**不能**
            # 这么改，那里的子必须如实显示黑白。
            bar_player = 2 if theme.current_theme() == "dark" else 1
            # 「自定义」只出现在**难度页**，复盘页不加。两个理由：
            #  * 复盘页的档位是"用哪一档重算"，而自定义局的复盘**直接跳过这一页**
            #    用本局快照的参数跑（见 GomokuGame._show_review_strength），所以
            #    它在这里没有用武之地；
            #  * 复盘页的过滤是「不低于本局难度」，如果自定义也进这张表，用户
            #    只是想让 AI 从某一档重算，却先要面对一个要填参数的弹窗。
            extra = 1 if self.mode == "difficulty" else 0
            size = _card_fit_size(len(levels) + extra, gap)
            for level, tone in zip(levels, tones):
                # N 颗子当强度条 —— 用的是棋盘上那套材质，不是另画一个图标。
                # 排不下时收紧的是**间隙**，不是棋子（见 _strength_bar）。
                btn = card_button(engine.difficulty_name(level), tone,
                                  face=_strength_bar([bar_player] * level,
                                                     card_px=size),
                                  sub=i18n.tf(
                                      "思考上限 %g 秒",
                                      engine.DIFFICULTY[level]["time"]),
                                  index=f"{level:02d}", size=size)
                # 同一批卡片服务于两个页面，只有"点了发哪个信号"不同。
                sig = (self.review_level_selected if self.mode == "review"
                       else self.difficulty_selected)
                btn.clicked.connect(lambda _=False, l=level, s=sig: s.emit(l))
                self._cards.append(btn)

            if self.mode == "difficulty":
                # 第 6 张卡：**不画强度条**。强度条只表达"几颗子 = 多强"，而
                # 自定义不表示某个固定强度 —— 硬塞五颗会和宗师那张看起来一样。
                # 卡面换成自绘的「?」，说的是"这一档由你来定"（见 ui_kit
                # 的 QuestionMark，那里写了为什么不是文字控件、为什么是问号）。
                custom_btn = card_button(
                    engine.difficulty_name(engine.CUSTOM_LEVEL), "ghost",
                    face=_custom_mark(),
                    sub="算法与深度可调",
                    index=f"{engine.CUSTOM_LEVEL:02d}", size=size)
                custom_btn.clicked.connect(
                    lambda _=False: self.difficulty_selected.emit(
                        engine.CUSTOM_LEVEL))
                self._cards.append(custom_btn)

        # 5 张卡在 SPACE_XL(24) 下是 5×160+4×24 = 896px，仍塞得进 WINDOW_W=1022
        # —— 但只剩 126px 余量。降到 SPACE_LG(16) 得 864px，只调难度/复盘页：
        # 颜色页/mode 页只有 2–3 张卡，宽间距是那两张页面的节奏。
        # 卡片边长已在上面的分支里按窗口宽度现算（`_card_fit_size`）；
        # `_fit_card_row` 再按当前语言的文本度量补齐/折行。
        self.add_content(_fit_card_row(self._cards, gap))

        # 「设置」入口**不在这里挂**：它是主窗口右上角那枚固定的齿轮
        # （`GomokuGame.settings_btn`），页面怎么换都不动它。曾经每页各挂一个，
        # 结果是三种长相三种位置 —— 见 2026-10-05 的反馈。


# ==================== 局域网联机（M4d）====================
class LanMenuScreen(Screen):
    """局域网联机的岔路口：创建房间 / 加入房间。"""

    create_clicked = pyqtSignal()
    join_clicked = pyqtSignal()
    back_clicked = pyqtSignal()

    def __init__(self):
        super().__init__(title="局域网联机",
                         subtitle="创建房间，或加入好友的对局")
        self._cards = []
        cards = [("创建房间", "primary", "等好友输入你的地址加入"),
                 ("加入房间", "success", "输入房主显示的地址")]
        for i, (text, tone, sub) in enumerate(cards):
            btn = card_button(text, tone, sub=sub, index=f"{i + 1:02d}")
            sig = self.create_clicked if i == 0 else self.join_clicked
            btn.clicked.connect(lambda _=False, s=sig: s.emit())
            self._cards.append(btn)
        self.add_content(_fit_card_row(self._cards, theme.SPACE_XL))

        back = button("← 返回", "ghost", width=150)
        back.clicked.connect(self.back_clicked.emit)
        self.add_footer(back)


class LanJoinScreen(Screen):
    """加入房间：填 IP / 端口，点「加入房间」即发起连接。

    房主开房时已经选过颜色，加入方**不再选** —— 服务端把剩下那一席自动
    分配过来（``seat="auto"``）。两人各选一次是两条互相不知道对方的规则，
    必然有一天撞车。

    失败（没有房间 / 房间已满 / 版本不符）不发信号出去，而是**留在本页**
    显示原因，用户可以直接改地址重试 —— 连接失败是联机的常态，不该把
    人弹回模式选择页重新走一遍。
    """

    join_requested = pyqtSignal(str, str)   # host, port
    back_clicked = pyqtSignal()

    def __init__(self, host: str = "", port: str = "", error: str = ""):
        super().__init__(title="加入房间",
                         subtitle="输入房主等待页显示的地址，剩余席位会自动分配")
        form = QVBoxLayout()
        form.setSpacing(theme.SPACE_SM)
        tags = []
        self.host_edit = self._field(form, "房主 IP", host,
                                     "例如 192.168.1.7", tags)
        self.port_edit = self._field(form, "端口", port, "例如 51234", tags)
        # 两行标签统一宽度，下限 72px：俄语「IP хоста」比 72px 宽，定死会把
        # 它裁成「IP хос…」；中文两个标签都短于 72，宽度不变。
        _even_width(tags, _px(72))
        self.add_content(self._as_widget(form))

        join_btn = button("加入房间", "primary", width=150)
        join_btn.clicked.connect(self._emit_join)
        self.add_content(join_btn)

        self.error_label = faint_label("")
        self.add_content(self.error_label)
        if error:
            # 错误文案在 `_lan_join_error_text` 里是一张中文表；统一在这一个
            # 出口翻，那张表就不必感知语言。
            self.error_label.setText(i18n.t(error))

        back = button("← 返回", "ghost", width=150)
        back.clicked.connect(self.back_clicked.emit)
        self.add_footer(back)

    @staticmethod
    def _field(form, label, text, placeholder, tags):
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(theme.SPACE_SM)
        tag = faint_label(label, align=Qt.AlignRight | Qt.AlignVCenter)
        # 宽度等两行都建好后再统一（调用方 `_even_width`）—— 单行 72px 是
        # 中文口径，长语言下要按 sizeHint 放宽，两行还得一样宽。
        tags.append(tag)
        edit = QLineEdit(text)
        edit.setPlaceholderText(i18n.t(placeholder))
        edit.setFixedWidth(260)
        edit.setFixedHeight(theme.CONTROL_H)
        lay.addWidget(tag)
        lay.addWidget(edit)
        form.addWidget(row)
        return edit

    @staticmethod
    def _as_widget(layout):
        """把裸布局塞进一个控件，好交给 ``Screen.add_content``（它要 QWidget）。"""
        box = QWidget()
        box.setLayout(layout)
        return box

    def _emit_join(self) -> None:
        self.join_requested.emit(self.host_edit.text().strip(),
                                 self.port_edit.text().strip())


class LanWaitScreen(Screen):
    """房主等待对手加入：把本机 IP 与房间端口摆出来，可取消整场。"""

    cancel_clicked = pyqtSignal()

    def __init__(self, ip: str, port: int, stone: int):
        super().__init__(
            title="等待对手加入",
            subtitle="请对方在另一台设备上选「加入房间」，并选择另一种颜色")
        self.add_content(title_label(f"{ip}:{port}", role="title"))
        self.add_content(faint_label(
            "你执%s · 对方请选执%s"
            % ("黑" if stone == 1 else "白", "白" if stone == 1 else "黑")))
        cancel = button("✕ 取消等待", "danger", width=150)
        cancel.clicked.connect(self.cancel_clicked.emit)
        self.add_content(cancel)


def _lan_join_error_text(exc) -> str:
    """把连接层错误翻成用户能照着做的一句话。"""
    if isinstance(exc, TimeoutError):
        return "连接超时：检查 IP、端口与两台设备是否在同一局域网"
    if isinstance(exc, OSError):
        return "连接被拒绝或中断：检查 IP、端口与防火墙"
    code = getattr(exc, "code", "")
    known = {
        "seat_taken": "该颜色已被占用，换一种颜色试试",
        "room_full": "房间已满：房主已经在和另一位玩家对局了",
        "no_room": "没有找到这个房间：请确认房主正在等待、地址没有抄错",
        "proto_mismatch": "双方游戏版本不一致，无法联机",
        "already_started": "这一局已经开始了，等下一局再进",
        "timeout": "连接超时：检查 IP、端口与两台设备是否在同一局域网",
        "io": "连接被拒绝或中断：检查 IP、端口与防火墙",
    }
    if code in known:
        return known[code]
    msg = getattr(exc, "message", "") or str(exc)
    return f"连接失败：{msg}"


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
    review_clicked = pyqtSignal()

    def __init__(self, result_text, is_win, can_review=False,
                 can_restart=True):
        """``can_review`` 为真时多一个「算法复盘」按钮。

        **只在"输给 AI"这一种结局上为真**（见 ``_show_game_over``）：赢了没有
        可复盘的东西，本地两人对战则根本没有 AI 参与 —— 没有算法可复盘。

        ``can_restart=False``（房间配置 ``allow_restart=False``）时干脆不放
        「再来一局」按钮：禁用一颗已经画出来的按钮，用户还是要先读一遍它再
        失望；不画才是诚实的。
        """
        super().__init__(root_name="overlayRoot")
        self.result_text = result_text
        self.is_win = is_win
        self.can_review = can_review
        self.can_restart = can_restart
        self.setup_ui()

    def setup_ui(self):
        result = title_label(self.result_text)
        result.setProperty("tone", "win" if self.is_win else "lose")
        self.add_content(result)

        btns = []
        if self.can_restart:
            restart_btn = button("🔄 再来一局", "success", width=_px(150))
            restart_btn.clicked.connect(self.restart_clicked.emit)
            btns.append(restart_btn)
        quit_btn = button("✕ 退出游戏", "danger", width=_px(150))
        quit_btn.clicked.connect(self.quit_clicked.emit)
        btns.append(quit_btn)
        if self.can_review:
            review_btn = button("📊 算法复盘", "primary", width=_px(150))
            review_btn.clicked.connect(self.review_clicked.emit)
            btns.append(review_btn)
        self.add_content(hbox(*btns, spacing=theme.SPACE_LG))


def _stop_animations(root) -> None:
    """停掉控件树里所有在跑的 ``QAbstractAnimation``。

    删除页面/窗口前**必须**先停动画：动画的 target（``QGraphicsOpacityEffect``
    等）与动画对象都是页面的子对象，而 Qt 删除子对象的顺序不保证动画先于
    target 析构 —— 动画若在 target 被删之后还跑一拍或析构，就会碰到悬空的
    target，在本机表现为原生崩溃（0xC0000005）。``anim.fade_in`` 是
    ``DeleteWhenStopped`` 且调用方不留引用，所以只能从控件树里找出来停。
    """
    for child in root.findChildren(QAbstractAnimation):
        child.stop()


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
        dw, dh = _design_size()
        if screen is None or not _autofit_enabled():
            return (dw, dh)
        avail = screen.availableGeometry()
        # 留 8% 余量，避免贴着屏幕边缘（任务栏、窗口阴影、部分 WM 的吸附区）。
        #
        # **可用高度要再扣掉窗口外框。** 屏幕可用区量的是"能给窗口多少"，
        # 而这里算的是**客户区**尺寸 —— 标题栏与边框是加在它外面的。不扣这一下，
        # 客户区贴着可用区上沿时，标题栏就悬到屏幕外了（用户报的正是这个）。
        avail_h = max(1, avail.height() - FRAME_FALLBACK_H)
        k = min(MAX_SCALE,
                (avail.width() * 0.92) / dw,
                (avail_h * 0.92) / dh)
        # **不再有 `k = max(k, 1.0)` 这条下限。** 那正是 1920×1080 @150% 上
        # 窗口 1022×750 比 1280×720 的逻辑屏还高 30px 的原因；而且随之抬上去的
        # `setMinimumSize` 让用户连手动拖小都做不到。下限改由 `setMinimumSize`
        # 单独管，且它也被夹进可用区（见 `__init__`）。
        return (int(dw * k), int(dh * k))

    @classmethod
    def _min_size(cls) -> tuple:
        """窗口最小尺寸：按档位缩放后，夹进**初始尺寸**，再夹进屏幕可用区。

        **夹初始尺寸这一下是硬约束，不是保险。** ``Qt`` 会把 ``resize()`` 静默
        夹回最小尺寸 —— 所以最小尺寸一旦大于按比例算出来的初始尺寸，"启动尺寸
        按比例来"就是一句空话，窗口会直接变成最小尺寸那么大。用户报的
        1920×1080 @150% 上正是这样：初始算出 852×625，最小却被
        ``_build_game_ui`` 抬到 652，窗口被迫长高，加标题栏一起冲出可用区。

        下限只管"再小就不好用了"；装不下的部分由滚动兜底接手
        （``_sync_central_min`` + 中央那层 ``QScrollArea``）。
        """
        iw, ih = cls._initial_size()
        mw, mh = min(_px(MIN_W), iw), min(_px(MIN_H), ih)
        if _autofit_enabled():
            screen = QApplication.primaryScreen()
            if screen is not None:
                av = screen.availableGeometry()
                mw, mh = min(mw, av.width()), min(mh, av.height())
        return (max(1, mw), max(1, mh))

    def _fit_cap_h(self) -> int:
        """``minimumHeight`` 可抬到的上限。

        三处夹紧，缺一不可：

        1. **当前档位的设计高度** ``_design_size()[1]`` —— 原来的写法是模块常量
           ``WINDOW_H``（normal 档的 750）。小屏降档后仍按 750 算，等于把窗口
           最小高度顶回屏幕外，``_initial_size()`` 再怎么算也压不下去。
        2. 可用高度**扣掉窗口外框**（``_frame_h``，量不到就用
           ``FRAME_FALLBACK_H``）再乘系数 —— 可用区不含标题栏。
        3. 上层 ``_build_game_ui`` 还会再夹一次 ``_initial_size()[1]``。

        自动适配关掉时（离屏测试）保持原样返回 ``WINDOW_H``：那些断言全按
        设计尺寸写。
        """
        if not _autofit_enabled():
            return WINDOW_H
        dh = _design_size()[1]
        screen = QApplication.primaryScreen()
        if screen is None:
            return dh
        frame = self._frame_h if self._frame_h else FRAME_FALLBACK_H
        cap = int(max(1, screen.availableGeometry().height() - frame) * 0.96)
        return max(_px(MIN_H), min(dh, cap))

    def _sync_central_min(self):
        """中央容器的最小尺寸 = 当前页需要的，但夹在 [设计下限, 设计尺寸] 之间。

        不能直接用页面自然的 ``minimumSizeHint()``：游戏页在这个环境里量出来是
        **778px**，比设计高度 750 还高 —— 那样默认窗口下就会平白多出一条 28px
        的竖向滚动条，纯粹的观感退化（这套布局一直都是"宁可把面板挤一点"）。

        也不能用一个固定常量 ``MIN_H``：复盘列表页要 620px，窗口缩到 500 时它
        会被硬压进 500 —— 底部按钮被裁掉，而且因为"最小值说放得下"，连滚动条
        都不会给。

        夹一下之后语义就对了：**默认窗口下照旧挤压，窗口比这一页真正需要的还
        小时才滚**。
        """
        if self._syncing_min:
            return
        page = self.central.currentWidget()
        if page is None:
            return
        need = page.minimumSizeHint()
        dw, dh = _design_size()
        w = max(_px(MIN_W), min(need.width(), dw))
        h = max(_px(MIN_H), min(need.height(), dh))
        if self.central.minimumSize() != QSize(w, h):
            self._syncing_min = True
            try:
                self.central.setMinimumSize(w, h)
            finally:
                self._syncing_min = False

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_central_min()
        # 齿轮钉在右上角，窗口一变宽就得跟着走。
        self._place_settings_button()

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
        if not self._frame_h:
            # 客户区之外的那一圈（标题栏、边框）。离屏平台两者相等、量出来是 0，
            # 那就保持 0，由 `_fit_cap_h` 退回 ``FRAME_FALLBACK_H``。
            self._frame_h = max(0, self.frameGeometry().height()
                                - self.geometry().height())
        if not self._centered:
            self._centered = True
            self._center_on_screen()
        # 首次 show 之前 `width()` 还不可信，齿轮的位置在这里再钉一次。
        self._update_settings_button()

    def __init__(self):
        super().__init__()
        # 全局 QSS 必须早于任何 widget 构造。放在这里而不是 main()：
        # gui_smoke 自建 QApplication 后直接构造本窗口、不走 main()，
        # 装在这儿离屏冒烟才会真的执行这套样式。
        theme.install()

        # 上次存下来的自定义难度读回引擎。放在这里而不是 ``main()``：gui_smoke
        # 直接构造本窗口、不走 ``main()``，而它要验的正是"自定义参数真的进了
        # 搜索"。无头程序（server.py / tools/bench.py）不经过这里，照旧用
        # engine.py 里的出厂值。
        _load_custom_settings()

        # 界面语言。**必须早于任何文案被造出来** —— 界面文字是构造期写进控件
        # 的（见 ui_kit 的文本工厂），晚一行就有半屏中文留在原地。放在这里而不是
        # ``main()``：与上面那条同一个理由，gui_smoke 直接构造本窗口。
        _load_language()

        # 小屏自动降档：**只在用户从未在设置页存过档位时**才自作主张，
        # 一旦用户选过就以他为准（`saved_scale()` 返回非 None）。
        # `persist=False` —— 自动判定是按这台机器现算的，不该写进配置里，
        # 否则换台机器会带着上一台的档位。
        screen = QApplication.primaryScreen()
        if _autofit_enabled() and screen is not None and theme.saved_scale() is None:
            av = screen.availableGeometry()
            theme.set_scale(_autofit_scale(av.width(), av.height()), persist=False)

        self.setWindowTitle(i18n.t("五子棋 AI"))
        # 标题栏 + 边框的高度，第一次 show 之后才量得准（见 `showEvent`）。
        # ``_fit_cap_h`` 要从可用高度里扣掉它；量到之前用保守常量兜底。
        self._frame_h = 0
        # 设置弹窗里换过档位没有 —— 换过才需要在关窗时重建底下那一页。
        self._scale_changed_in_settings = False
        # 复盘结果页重建所需的参数（换档后要按同一份材料重建一份）。
        self.review_cancelled = False
        self._review_board_idx = None
        self.setMinimumSize(*self._min_size())
        self.resize(*self._initial_size())
        self._centered = False      # 只在首次 show 时居中一次
        # 这里**不要**再写 setStyleSheet("background-color: ...")。Qt 会把
        # 控件级样式表传播给全部子控件，且优先级高于应用级 —— 一条无选择器的
        # 背景色会把进度条的槽、面板底色一起刷掉（实测槽色直接消失）。
        # 窗口底色由 theme 的 QMainWindow 规则统一给。

        # ---- 对局状态：全部归 Session（唯一规则真源，零 Qt）----
        # 棋盘、走子历史、判胜/判和、悔棋政策、复盘快照都住在 session.py；
        # 本类只保留 UI 与线程侧状态。字段级兼容访问见下面那组 property。
        self.session = Session()

        # UI / 线程侧状态
        self.gamemode = 0  # 0=先手(黑), 1=后手(白)
        self.gamekunnan = 1
        self.playmode = 0  # 0=挑战AI, 1=本地双人对战, 2=局域网联机
        self.ai_thinking = False

        # AI Worker
        self.ai_worker = None       # M4c 起主流程不再启动；保留字段兼容冒烟脚本

        # ---- 单机房间（M4c）----
        # UI 不再是规则真源，只通过 RoomClient 接入应用内的 LocalRoom：人机局
        # 一个客户端（人类席位），本地双人局两个客户端（黑/白各一条）。事件
        # 由 _RoomBridge 排队投递回主线程，见 _on_room_event / _close_room。
        self._room = None           # LocalRoom 或 None
        self._room_bridge = None    # _RoomBridge 或 None
        self._room_clients = {}     # stone -> RoomClient（本 UI 持有的席位）
        self._room_finished = False  # game_over 只收尾一次（双客户端会收到两份）
        self._last_undo = None      # (by, move_no)，undo_applied 去重
        self._move_in_flight = False  # 已发着法、等回显，防止连点重复提交
        # 人类回合开始时（turn 事件）的棋盘快照：复盘 human_moves 的
        # ``before`` 就取它，语义是"落子前"，与旧版逐字一致。
        self._pending_snapshot = None
        #: welcome 下发的 RoomConfig（dict）。None = 没有配置信息（离线构造的
        #: 测试直连），一切按默认全开处理，见 ``_cfg``。
        self._room_config = None
        # 颜色页选完之后去哪：单机走难度页，LAN 开房走开监听 + 等待对手。
        self._color_next = "difficulty"

        # 复盘 Worker（与 AI Worker 同一套收尾方式，见 ``_cancel_review``）
        self.review_worker = None
        # 复盘代数：只在"拆页面"时递增，用来丢弃迟到的 ``completed`` ——
        # 等待有超时上限，超时后线程仍会把结果投递回来，而那一刻页面可能
        # 已经不存在了（与房间事件用"断开信号 + 页面判空"是同一个目的）。
        self._review_generation = 0

        # 复盘得到的结果列表（``ReviewWorker.completed`` 的入参），供结果页读取
        self.review_records = []
        self.review_level = 1

        # 终局遮罩的延迟投递（见 ``GAME_OVER_DELAY_MS``）。用 QTimer 而不是
        # ``time.sleep`` —— 后者会把 UI 线程连同那一秒里的重绘一起冻住，玩家
        # 连五颗子都看不见，正好和这个停顿的目的相反。
        self._game_over_timer = QTimer(self)
        self._game_over_timer.setSingleShot(True)
        self._game_over_timer.timeout.connect(self._show_game_over)

        # 游戏日志
        self.logger = None

        # 中央容器
        #
        # **外面套一层 QScrollArea，包的是 central 本身，不是逐页包。**
        # 逐页包（曾经的想法）是错的：`_init_loading` 直接 `central.addWidget`，
        # 不走 `_switch_page`，会造出一半有 wrapper、一半没有的混合栈，而
        # `_drop_pages` / `_on_review_record_selected` 的 `removeWidget(page)`
        # 也会变成空操作。包在外面之后，`central.currentWidget()` 仍然返回
        # **页面对象本身**，全部身份比较与 `count()` 一字不改。
        #
        # `widgetResizable` 让内容撑满视口；内容最小值（QStackedWidget 取所有
        # 页面最小值的最大者）超过视口时才出滚动条 —— 也就是窗口比内容矮时。
        self.central = QStackedWidget()
        self._syncing_min = False
        # 换页时把中央容器的最小尺寸同步成"这一页真正需要的大小"（封顶设计
        # 尺寸）。挂在 currentChanged 上而不是逐个切页点手写 —— 这个仓库里
        # `setCurrentWidget` 有 5 处（含 `_back_to_review_list` 这种刻意绕开
        # `_switch_page` 的），漏一处就是一条静默的裁切。
        self.central.currentChanged.connect(lambda _=0: self._on_page_changed())
        self._outer = QScrollArea()
        self._outer.setWidgetResizable(True)
        self._outer.setFrameShape(QFrame.NoFrame)
        # 背景透明由 theme 的 QSS 给（在这里 setStyleSheet 会传播给子页面，
        # 把各页的底色一起刷掉）。
        self._outer.setWidget(self.central)
        self.setCentralWidget(self._outer)

        # ---- 全局唯一的「设置」入口：主窗口自己的子控件，永远停在右上角 ----
        #
        # 不做成"每页各挂一个"（曾经的做法）：页面底部一版、对局面板里一版，
        # 两种长相两种位置，而且面板里那版被变体 QSS 的 padding 裁成了残缺的
        # 一条（用户 2026-10-05 的截图）。挂在这里之后，"同一个位置"是**结构**
        # 保证的 —— 页面怎么换它都不动，只是按页类型开关可见性。
        #
        # 它是主窗口的子控件、不在 `central` 里，所以 `_drop_pages` 不会碰它，
        # 换页也不会让它重建。
        self.settings_btn = settings_button()
        self.settings_btn.setParent(self)
        self.settings_btn.clicked.connect(self._open_settings_dialog)

        # 各页面
        self.loading_screen = None
        self.selection_mode = None
        self.selection_color = None
        self.selection_difficulty = None
        self.game_widget = None
        self.board_widget = None
        self.game_panel = None
        self.game_over_overlay = None
        # LAN 页（M4d）：菜单 / 加入表单建在 central 上；等待遮罩叠在棋盘页上
        self.lan_menu = None
        self.lan_join = None
        self.lan_wait = None
        self._game_row = None       # 棋盘行容器（收回等待遮罩时切回它）
        # 设置弹窗：**不占 `central` 的一页**，所以不参与 _drop_pages 的页面表；
        # 关掉即销毁，下次点齿轮重新建（档位/主题可能已经变了，重建即回显）。
        self.settings_dialog = None
        # 自定义难度弹窗（难度页第 6 张卡打开的）
        self.custom_dialog = None
        # 本局开局时快照的自定义参数（非自定义局为 None）。复盘时用它重算 ——
        # 快照而不是现读，是为了"打完之后再改参数"不改写这一局的复盘结果。
        self.game_custom_cfg = None
        # 复盘三页
        self.review_strength = None
        self.review_progress = None
        self.review_list = None
        self.review_board = None

        self._init_loading()

    # ------------------------------------------------------------------
    # 会话状态兼容垫片（M1 过渡期）
    #
    # Session 是唯一状态源；这些 property 让既有测试与 tools/gui_smoke.py、
    # tools/ui_e2e.py 继续按字段名读写（其中少数会直接赋值，于是 setter 也
    # 保留）。新代码一律用 self.session；M4 客户端化时整块移除。
    # ------------------------------------------------------------------
    @property
    def board(self):
        return self.session.board

    @board.setter
    def board(self, value):
        self.session.board = value

    @property
    def move_history(self):
        return self.session.move_history

    @move_history.setter
    def move_history(self, value):
        self.session.move_history = value

    @property
    def move_count(self):
        return self.session.move_count

    @move_count.setter
    def move_count(self, value):
        self.session.move_count = value

    @property
    def game_over(self):
        return self.session.game_over

    @game_over.setter
    def game_over(self, value):
        self.session.game_over = value

    @property
    def gamerule(self):
        return self.session.gamerule

    @gamerule.setter
    def gamerule(self, value):
        self.session.gamerule = value

    @property
    def winner(self):
        return self.session.winner

    @winner.setter
    def winner(self, value):
        self.session.winner = value

    @property
    def output(self):
        return self.session.output

    @output.setter
    def output(self, value):
        self.session.output = value

    @property
    def human_moves(self):
        return self.session.human_moves

    @human_moves.setter
    def human_moves(self, value):
        self.session.human_moves = value

    @property
    def last_move(self):
        return self.session.last_move

    @last_move.setter
    def last_move(self, value):
        self.session.last_move = value

    @property
    def ai_first_move_done(self):
        return self.session.opening_done

    @ai_first_move_done.setter
    def ai_first_move_done(self, value):
        self.session.opening_done = value

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
        self._show_mode_selection()

    def _drop_pages(self):
        """切页时回收旧页面。

        旧代码每个 ``_show_*`` 都是 ``central.addWidget(...)`` 却从不移除 ——
        每重开一局就多留一整棵 widget 树（棋盘连同它的两层 pixmap 缓存）在
        ``QStackedWidget`` 里，永远不会被回收。

        **置空引用和 deleteLater 同样重要。** ``deleteLater`` 只是排队删除，
        C++ 对象随即失效；若 ``self.board_widget`` 仍指向它，之后任何访问都会
        抛 ``RuntimeError: wrapped C/C++ object has been deleted``，而它通常
        发生在 Qt 槽里 —— 直接崩。

        调用方必须**先** ``_close_room()``：房间事件的回调会碰棋盘状态，
        在已删除的 widget 上写日志会抛 ValueError。
        """
        # 先掐掉在途的终局遮罩。延迟这一秒里玩家完全可能已经点了"重新开始"，
        # 定时器若还活着，回调会在 game_widget 已销毁、``self._stack`` 已被置
        # None 之后醒来 —— 那是 AttributeError，在 Qt 槽里抛就是直接崩。
        self._game_over_timer.stop()
        for attr in ("loading_screen", "selection_mode", "selection_color",
                     "selection_difficulty", "game_widget", "lan_menu",
                     "lan_join", "review_strength",
                     "review_progress", "review_list", "review_board"):
            page = getattr(self, attr, None)
            if page is not None:
                # 先停页面树里的在途动画（fade_in / 落子 / 脉动）再删 ——
                # 快速重开时 180ms 的淡入往往没跑完，见 _stop_animations。
                _stop_animations(page)
                self.central.removeWidget(page)
                page.deleteLater()
        # board_widget / game_panel / overlay / lan_wait 是 game_widget 的子控件，
        # 随父一起销毁，不需要（也不能）单独 removeWidget。
        for attr in ("loading_screen", "selection_mode", "selection_color",
                     "selection_difficulty", "game_widget", "board_widget",
                     "game_panel", "game_over_overlay", "_stack", "_game_row",
                     "lan_menu", "lan_join", "lan_wait",
                     "review_strength", "review_progress", "review_list",
                     "review_board"):
            setattr(self, attr, None)
        # 设置弹窗不占 `central`，页面全拆了它照样活着 —— 但它底下那一页已经
        # 没了，留着就是一块悬在空窗口上的板子。关掉它再撤引用。
        if self.settings_dialog is not None:
            self.settings_dialog.close()
            self.settings_dialog = None
        self._scale_changed_in_settings = False
        self._review_board_idx = None
        # 自定义参数快照属于"上一局"，重开就作废。
        self.game_custom_cfg = None

    def _on_page_changed(self):
        """当前页换了：重算中央容器的下限，顺带刷一下右上角齿轮的可见性。"""
        self._sync_central_min()
        self._update_settings_button()

    def _place_settings_button(self):
        """把设置齿轮钉在**当前页主标题文字的右边**。

        不是窗口右上角：那是外框外沿，齿轮摆在那儿会压在页面留白之外、和面板
        的圆角边框叠在一起 —— 用户 2026-10-05 的截图原话是「遮挡 GUI」。跟着
        标题走之后，它在每一页都紧挨着主标题，落点由内容决定。

        主标题的两处几何都算得出来，因为 ``title_label`` 把对齐写死成
        ``AlignCenter``：面板标题行里那个 label 拿的是 sizeHint 宽度，屏幕页
        的标题在 ``AlignCenter`` 下也是 sizeHint 宽度 —— 两种情况下"文字左边
        起点 = 控件左边 + (控件宽 − 文字宽)/2"都成立。
        """
        b = getattr(self, "settings_btn", None)
        if b is None:
            return
        page = self.central.currentWidget()
        if page is None:
            return
        lbl = _page_title_label(page)
        if lbl is None:
            # 没标题的页（当前只有加载页，而它本来就不显示齿轮）：退到页面
            # 右上角，至少不会跑到窗口外。
            corner = page.mapTo(self, QPoint(page.width(), 0))
            b.move(max(0, corner.x() - theme.SPACE_XL - b.width()),
                   max(0, corner.y() + theme.SPACE_XL))
        else:
            tl = lbl.mapTo(self, QPoint(0, 0))
            text_w = lbl.fontMetrics().horizontalAdvance(lbl.text())
            x = tl.x() + (lbl.width() - text_w) // 2 + text_w + theme.SPACE_SM
            y = tl.y() + (lbl.height() - b.height()) // 2
            b.move(max(0, x), max(0, y))
        b.raise_()

    def _update_settings_button(self):
        """按当前页决定齿轮是否显示。

        只有加载页藏起来 —— 那是开场过场，齿轮摆在那儿没有意义。
        （用户 2026-10-05：「他妈的加载界面你放个设置干啥」。）设置既然是弹窗，
        就不再需要"设置页自己藏自己"这一支。
        """
        b = getattr(self, "settings_btn", None)
        if b is None:
            return
        page = self.central.currentWidget()
        show = page is not None and not isinstance(page, LoadingScreen)
        b.setVisible(show)
        if show:
            self._place_settings_button()

    def _switch_page(self, page):
        """统一收口的切页：入栈 + 置当前 + 180ms 淡入。

        加载页→颜色页→难度页→游戏页共 4 处切换点，全部走这里 —— "切页有
        过渡"是结构保证的，不是逐处手抄出来的。
        """
        self.central.addWidget(page)
        self.central.setCurrentWidget(page)
        anim.fade_in(page)

    def _show_mode_selection(self):
        """显示对战模式选择（加载页之后的第一个岔路口）。

        新开一局与"再来一局"都从这里进 —— 两条路的第一步本来就该是同一个
        问题"这一局跟谁下"，各走各的迟早会出现"重开一局跳过了模式选择"这种
        不一致。
        """
        self._drop_pages()
        self.selection_mode = SelectionScreen(mode="mode")
        self.selection_mode.mode_selected.connect(self._on_mode_selected)
        self._switch_page(self.selection_mode)

    # ---- 设置弹窗 ----

    def _open_settings_dialog(self):
        """点齿轮：开设置弹窗。

        **不再是一页。** 以前它借 `central` 占一页，进去要切页、出来要原路返回，
        `_settings_origin` 那一整套登记与回退都是在给"它占一页"收拾残局。做成
        弹窗之后底下那一页在弹窗期间一直没动过，这些机制整体消失。

        用 ``open()`` 而不是 ``exec_()``：``exec_()`` 会进嵌套事件循环，工具的
        驱动方式（QTimer + 手动 pump）会陷在里面出不来。``open()`` 在 Qt5 里
        同样是模态，但不阻塞调用方。
        """
        if self.settings_dialog is not None:
            self.settings_dialog.raise_()
            self.settings_dialog.activateWindow()
            return
        self._scale_changed_in_settings = False
        self._language_changed_in_settings = False
        dlg = SettingsDialog(self)
        dlg.scale_selected.connect(self._on_scale_selected)
        dlg.theme_toggled.connect(self._on_theme_toggled)
        dlg.language_selected.connect(self._on_language_selected)
        dlg.finished.connect(self._on_settings_dialog_closed)
        self.settings_dialog = dlg
        dlg.open()

    def _on_settings_dialog_closed(self, _result=0):
        """弹窗关了：如果档位或语言变过，重建底下那一页。

        两条改动都要重建这一页，但来路不同：换档是**度量**变了（面板宽度、
        棋盘半径这些是构造期算死的），换语言是**文案**变了（每条文字都是构造
        期写进控件的）。表现却一样 —— 不重建的话，关掉弹窗后底下还停在旧字号
        / 旧语言，用户会以为设置没生效。

        弹窗期间对局线程照旧在往那一页推事件（AI 落子、计时），所以这里拿到的
        可能是一张"带着刚到达的新状态"的页面 —— 与从前设置页的情形同构，
        `_rebuild_for_scale` 的快照-回灌路径已经覆盖这一路。
        """
        self.settings_dialog = None
        if not (self._scale_changed_in_settings
                or self._language_changed_in_settings):
            return
        self._scale_changed_in_settings = False
        self._language_changed_in_settings = False
        page = self.central.currentWidget()
        if page is None:
            return
        rebuilt = self._rebuild_for_scale(page)
        # `_replace_page` 只换 widget，不负责置当前 —— 少了这一句底下还是旧页。
        self.central.setCurrentWidget(rebuilt)
        anim.fade_in(rebuilt)

    def _on_scale_selected(self, name):
        """换档：落盘 → 作废棋盘缓存 → 整页重建。

        **缓存必须显式作废。** ``board_render`` 读的是 live 的
        ``theme.RADIUS_*``（属性访问，会跟着档位变），而 ``BoardWidget._static``
        的缓存键只含 ``(width, height, dpr)`` —— 档位不在键里，不清就会画出
        旧圆角的木盘。
        """
        if name == theme.current_scale():
            return
        theme.set_scale(name, persist=True)
        self._scale_changed_in_settings = True
        self._invalidate_board_caches()
        self._rebuild_settings()

    def _on_language_selected(self, code):
        """换语言：切过去 → 落盘 → 重建弹窗。

        **不在这里重建底下那一页** —— 弹窗还开着，重建它会在用户眼皮底下换掉
        他背后的内容；等 ``finished`` 再统一处理（与换档同一条路径）。
        """
        if code == i18n.language():
            return
        i18n.set_language(code)
        _save_language(code)
        self._language_changed_in_settings = True
        self._rebuild_settings()

    def _on_theme_toggled(self):
        theme.toggle_theme(persist=True)
        # 主题不改变任何像素度量，木盘半径也没变 —— 但卡片强度条的**子色**
        # 是按主题挑的（见 `SelectionScreen.setup_ui` 里的 `bar_player`），
        # 所以重建设置页之外，模式页也在下次进入时重建（`_show_mode_selection`
        # 每次都会新建）。
        self._rebuild_settings()

    def _invalidate_board_caches(self):
        """把所有还活着的 `BoardWidget` 的绘制缓存清掉。"""
        for w in self.findChildren(BoardWidget):
            w._static = None
            w._stones = None
            w._cache_key = None
            w._stones_dirty = True
            w.update()

    def _rebuild_settings(self):
        """尺寸/配色变了：重建**弹窗自己**，好让勾选值跟着新档位回显。

        弹窗里的控件度量也是构造期算死的，不重建就出现"点了「特大」，弹窗纹丝
        不动"—— 用户会以为按钮坏了（它当年还是一页时就写下过这条理由）。
        重建期间不动 `central`：底下那一页本来就等着 `finished` 再处理。
        """
        if self.settings_dialog is None:
            return
        dlg = SettingsDialog(self)
        dlg.scale_selected.connect(self._on_scale_selected)
        dlg.theme_toggled.connect(self._on_theme_toggled)
        dlg.language_selected.connect(self._on_language_selected)
        dlg.finished.connect(self._on_settings_dialog_closed)
        old = self.settings_dialog
        self.settings_dialog = dlg
        old.finished.disconnect(self._on_settings_dialog_closed)
        old.close()
        old.deleteLater()
        dlg.open()

    def _rebuild_for_scale(self, page):
        """换档后按新度量重建 ``page``，返回重建出来的页面对象。

        面板宽度、卡片边长、按钮宽度全是**构造期**按档位算死的值，换档之后旧
        页面不会自己跟着变 —— 不重建就是"字号变了、布局还是旧的"。但重建必须
        把状态带过去，否则"调个字号"就变成了"弃局"，那正是这一轮要消灭的事。

        加载页与复盘进度页不重建：前者是过场、马上就被模式页取代；后者正被复盘
        线程持有（``set_progress`` 还在往上推），换掉它等于把进度条丢了 —— 而它
        是秒级的过场，度量过期没有观感影响。
        """
        if page is self.game_widget:
            return self._rebuild_game_page()
        if page is self.review_list and self.review_records:
            self.review_list = ReviewScreen(self.review_records,
                                            self.review_level,
                                            self.review_cancelled)
            self.review_list.record_selected.connect(
                self._on_review_record_selected)
            self.review_list.finish_clicked.connect(self._on_restart)
            self.review_list.exit_clicked.connect(self._on_quit)
            return self._replace_page(page, self.review_list, "review_list")
        if (page is self.review_board and self._review_board_idx is not None
                and self.review_records):
            idx = self._review_board_idx
            if idx < len(self.review_records):
                self.review_board = ReviewBoardScreen(self.review_records[idx])
                self.review_board.back_clicked.connect(self._back_to_review_list)
                return self._replace_page(page, self.review_board,
                                          "review_board")
        if isinstance(page, SelectionScreen):
            return self._rebuild_selection(page)
        return page

    def _replace_page(self, old, new, attr):
        """在栈里**原地替换** ``old``：新页入栈、旧页拆掉，返回新页。"""
        setattr(self, attr, new)
        self.central.addWidget(new)
        self.central.removeWidget(old)
        old.deleteLater()
        return new

    def _rebuild_selection(self, old):
        """按旧页自己的 ``mode``/``min_level`` 重建一个选择页，并重接信号。"""
        new = SelectionScreen(mode=old.mode, min_level=old.min_level)
        sig, slot = {
            "mode": (new.mode_selected, self._on_mode_selected),
            "color": (new.color_selected, self._on_color_selected),
            "difficulty": (new.difficulty_selected, self._on_difficulty_selected),
            "review": (new.review_level_selected, self._on_review_level_selected),
        }[old.mode]
        sig.connect(slot)
        attr = {"mode": "selection_mode", "color": "selection_color",
                "difficulty": "selection_difficulty",
                "review": "review_strength"}[old.mode]
        if getattr(self, attr, None) is old:
            return self._replace_page(old, new, attr)
        # 这一页还留在栈里但没有句柄（理论上不会）：只换页面，不覆盖句柄。
        self.central.addWidget(new)
        self.central.removeWidget(old)
        old.deleteLater()
        return new

    def _rebuild_game_page(self):
        """按新档位重建对局页，**盘面与状态原样带过去**。

        状态清单是照着 ``_start_game`` 的重置表来的（那份表列全了"一局"包含
        什么），外加面板自己那几个不落在棋盘上的量：图表序列、用时、思考态、
        最后一手的落点环、终局连线的红线与遮罩。

        ``self.board`` / ``self.move_history`` 不用备份 —— 它们**就是**当前局面，
        ``_build_game_ui`` 只读不写；要额外救回来的只有面板上的那几个。
        """
        old = self.game_widget
        panel = self.game_panel
        keep_series = keep_decade = None
        keep_elapsed, keep_thinking = 0, False
        if panel is not None:
            keep_series = list(panel._series)
            keep_decade = panel._decade
            keep_elapsed = panel._elapsed
            keep_thinking = panel._thinking
        last = self.last_move
        # 遮罩的延迟弹出必须掐掉：`_build_game_ui` 会把 `_stack` 整个换掉，
        # 那个定时器醒来时看到的已经是新栈，会把遮罩重复叠一层。
        self._game_over_timer.stop()
        overlay_was_up = self.game_over_overlay is not None

        self.game_widget = None
        self._build_game_ui()          # 内部会 setCurrentWidget，随后统一回原页

        if keep_series is not None:
            p = self.game_panel
            p._series = keep_series
            p._decade = keep_decade
            p._elapsed = keep_elapsed
            m, s = divmod(keep_elapsed, 60)
            p.time_row.set_value("%02d:%02d" % (m, s))
            if keep_thinking:
                p.show_thinking(True, 2 if self.gamemode == 0 else 1)
        if last is not None:
            r, c, stone = last
            self.board_widget.set_last_move(r, c, stone)
        if self.gamerule == 2 and self.winner:
            self.board_widget.set_win_cells(win_line(self.board, self.winner),
                                            self.winner)
        if overlay_was_up:
            self._show_game_over(record=False)

        if old is not None:
            self.central.removeWidget(old)
            old.deleteLater()
        return self.game_widget

    def _on_mode_selected(self, mode):
        """选择了对战模式：0=挑战AI（走原来的颜色/难度两步），1=本地对战，
        2=局域网联机（开房 / 加入，M4d）。"""
        self.playmode = int(mode)
        if self.playmode == 1:
            self._start_local_game()
        elif self.playmode == 2:
            self._show_lan_menu()
        else:
            self._show_color_selection()

    def _show_lan_menu(self):
        """局域网联机的岔路口：创建 / 加入。"""
        self._drop_pages()
        self.lan_menu = LanMenuScreen()
        self.lan_menu.create_clicked.connect(self._on_lan_create)
        self.lan_menu.join_clicked.connect(self._open_lan_join)
        self.lan_menu.back_clicked.connect(self._show_mode_selection)
        self._switch_page(self.lan_menu)

    def _on_lan_create(self):
        """创建房间：先选自己的执子色（复用颜色页），再开监听等待。"""
        self._show_color_selection(next_step="lan_host")

    def _open_lan_join(self, error: str = ""):
        """建/重建「加入房间」页（失败后留在原地显示原因）。"""
        self._drop_pages()
        self.lan_join = LanJoinScreen(error=error)
        self.lan_join.join_requested.connect(self._on_lan_join_requested)
        self.lan_join.back_clicked.connect(self._show_lan_menu)
        self._switch_page(self.lan_join)

    def _start_local_game(self):
        """本地双人对战：跳过颜色页与难度页，直接开局（黑先）。

        ``gamemode`` 固定为 0（黑先）：这一模式里"玩家"不是一个确定的人，
        谁执黑由回合决定（见 ``_on_board_click``），沿用 ``gamemode`` 只会
        让下游一堆 ``1 if gamemode == 0 else 2`` 的分支读出无意义的答案。
        """
        self.gamemode = 0
        self._start_game()

    def _show_color_selection(self, next_step="difficulty"):
        """显示执棋颜色选择。

        ``next_step``：选完颜色去哪 —— 单机走难度页；LAN 开房走"绑端口 +
        等待对手"。颜色页本身完全复用，两种流程共用同一个 ``color_selected``。
        """
        self._color_next = next_step
        self._drop_pages()
        self.selection_color = SelectionScreen(mode="color")
        self.selection_color.color_selected.connect(self._on_color_selected)
        self._switch_page(self.selection_color)

    def _on_color_selected(self, mode):
        """选择了执棋颜色"""
        self.gamemode = mode
        if self._color_next == "lan_host":
            self._start_lan_host(mode)
            return
        self._show_difficulty_selection()

    def _show_difficulty_selection(self):
        """显示难度选择"""
        self.selection_difficulty = SelectionScreen(mode="difficulty")
        self.selection_difficulty.difficulty_selected.connect(self._on_difficulty_selected)
        self._switch_page(self.selection_difficulty)

    def _on_difficulty_selected(self, level):
        """选择了难度，开始游戏。

        自定义档先开配置弹窗：「确定」才带着那份参数开局，「取消」退回难度页
        —— 退回时**什么都不改**，引擎里那份自定义配置保持原样。
        """
        if level == engine.CUSTOM_LEVEL:
            self._on_custom_requested()
            return
        self.game_custom_cfg = None
        self.gamekunnan = level
        self._start_game()

    def _on_custom_requested(self):
        """难度页第 6 张卡：开自定义配置弹窗。"""
        if self.custom_dialog is not None:
            self.custom_dialog.raise_()
            self.custom_dialog.activateWindow()
            return
        dlg = CustomDifficultyDialog(engine.custom_config(), self)
        self.custom_dialog = dlg
        dlg.finished.connect(self._on_custom_dialog_closed)
        dlg.open()

    def _on_custom_dialog_closed(self, _result=0):
        dlg = self.custom_dialog
        self.custom_dialog = None
        if dlg is None or dlg.chosen is None:
            return
        cfg = dict(dlg.chosen)
        # 写回引擎（对局路径靠 `_cfg_for(CUSTOM_LEVEL)` 现读模块级那份）并落盘，
        # 同时**快照到本局** —— 复盘读的是快照，见 `ReviewWorker.cfg`。
        engine.set_custom_config(cfg)
        _save_custom_settings(engine.custom_config())
        self.game_custom_cfg = cfg
        self.gamekunnan = engine.CUSTOM_LEVEL
        self._start_game()

    def _start_game(self):
        """初始化游戏：先拆旧房间，再建本局的 LocalRoom + RoomClient。"""
        # 上一局的房间必须整只拆掉：它可能还有在跑的 AI 搜索、在途事件与
        # 日志写入。先拆再建，保证本局收到的每个事件都来自新房间。
        self._close_room()

        # 关闭上局日志
        if self.logger:
            self.logger.close()
        self.logger = GameLogger()
        if self.playmode == 1:
            self.logger.f.write("  模式: 本地双人对战(无AI参与)\n")
        else:
            self.logger.f.write(f"  模式: {'玩家先手(黑)' if self.gamemode == 0 else 'AI先手(黑), 玩家后手(白)'}\n")
        # 记档位名称与 C++ 可执行文件的**可用性**。后者是这份日志里唯一能回答
        # "刚才那局为什么 AI 又慢又弱"的静态信息 —— 一份日志事后翻出来，用时
        # 一列 0.5 秒和 7 秒的差别只能靠它解释。注意措辞：这一行说的是"能不能
        # 用 C++"，而不是"实际用了谁"（那要等第一次搜索之后才有答案）。
        self.logger.f.write(f"  难度: {engine.difficulty_name(self.gamekunnan)}\n")
        if self.gamekunnan == engine.CUSTOM_LEVEL:
            # 只写"自定义"是不够的 —— 事后翻日志要能回答"这一局到底按什么参数
            # 搜的"，而两份自定义配置的档位名逐字相同。这一行的立意与下面那行
            # 「引擎」一样：让日志成为事后唯一说得清现场的东西。
            c = self.game_custom_cfg or engine.custom_config()
            self.logger.f.write(
                "  自定义参数: 时限 %gs / 深度 %d / VCF %g / qply %d / "
                "增强 %s\n" % (float(c["time"]), int(c["max_depth"]),
                              float(c["vcf_budget"]), int(c["qply"]),
                              "开" if c.get("enhance") else "关"))
        self.logger.f.write(f"  引擎: {engine.binary_path() or 'C++ 不可用，将用本地 Python 引擎'}\n\n")
        self.logger.f.flush()

        # 配置本局会话（模式 + 石色）与两个席位。这里的 session 是 UI 侧的
        # **镜像**：规则真源在房间的 Session 里，本会话只保存由事件回放出来的
        # 状态，供渲染与复盘读取（字段级兼容垫片也指向它）。
        if self.playmode == 1:
            self.session.configure("pvp")
            black_spec = SeatSpec("remote")
            white_spec = SeatSpec("remote")
        else:
            human = 1 if self.gamemode == 0 else 2
            self.session.configure("ai", human_stone=human, ai_stone=3 - human)
            if self.gamemode == 0:
                black_spec, white_spec = SeatSpec("remote"), \
                    SeatSpec("ai", level=self.gamekunnan)
            else:
                black_spec, white_spec = SeatSpec("ai", level=self.gamekunnan), \
                    SeatSpec("remote")
        self.session.reset()
        self.ai_thinking = False
        self.review_records = []
        self._room_finished = False
        self._last_undo = None
        self._move_in_flight = False
        self._pending_snapshot = None
        # 上一局（可能是 LAN）的 RoomConfig 不能带进新局；本局配置等 welcome。
        self._room_config = None

        # 清空引擎的跨局面状态（置换表 / history / killer）。房间的对局线程
        # 启动时也会 reset_engine()；这里保留是为了让"新局"的语义在 UI 侧也
        # 成立（本地 Python 降级路径共享同一份全局状态）。
        new_game()

        # 构建游戏界面
        self._build_game_ui()
        # 面板建好才知道有它，而 AI 执哪一色早在 `_on_color_selected` 就定了
        # —— 评分卡的标题要把子色写进去，所以只能在这里补这一笔。本地对战没有
        # AI，这一行不写（标题保持默认）。
        if self.playmode == 0:
            self.game_panel.set_ai_player(2 if self.gamemode == 0 else 1)

        # 建房间并接入。**必须排在 `_build_game_ui` 之后**：`connect` 会同步
        # 触发 welcome 事件，处理它时棋盘与面板必须已经在。
        #
        # 人机局只有一个客户端（人类席位），AI 席位由房间自己算；本地双人局
        # 两个席位都是 remote，于是两条 RoomClient 从同一进程接入 —— 恰好也
        # 走一遍"两个客户端坐进一个房间"的联机路径。
        try:
            room = LocalRoom(black_spec, white_spec)
            if self.playmode == 1:
                specs = [("black", 1), ("white", 2)]
            else:
                seat = "black" if self.session.human_stone == 1 else "white"
                specs = [(seat, self.session.human_stone)]
            # LocalRoom 默认绑 127.0.0.1，但显式给本机地址，语义更清楚。
            self._attach_room(room, specs, host="127.0.0.1")
        except Exception as exc:                    # noqa: BLE001
            # 本机 loopback 理论上不会失败；真失败时给出可见的错误并退回模式
            # 选择页，而不是留在"看着能下、点了没反应"的假对局页上。
            print(f"[房间] 启动失败: {type(exc).__name__}: {exc}")
            self._close_room()
            self._show_mode_selection()

    # ------------------------------------------------------------------
    # 局域网开房 / 加入（M4d）
    # ------------------------------------------------------------------

    def _start_lan_host(self, gamemode):
        """创建局域网房间：绑 0.0.0.0 监听，本机客户端连 127.0.0.1。

        对手在另一台机器上，悔棋走 ``undo_proposed`` 协商（``LocalRoom``
        的 ``auto_consent=False``）—— 不能自动拿别人的棋。等对手就位后房间
        广播 state/turn，``_hide_lan_wait`` 收回等待遮罩露出棋盘。
        """
        self.playmode = 2
        stone = 1 if gamemode == 0 else 2
        seat = "black" if stone == 1 else "white"
        self._prepare_lan_game("局域网联机（房主，执%s）"
                               % ("黑" if stone == 1 else "白"))
        self.session.human_stone = stone
        try:
            room = LocalRoom(SeatSpec("remote"), SeatSpec("remote"),
                             name=LAN_ROOM_NAME, host="0.0.0.0",
                             auto_consent=False)
            # 同机客户端不能连 0.0.0.0（那是监听地址，不是可连接地址）。
            self._attach_room(room, [(seat, stone)], host="127.0.0.1")
        except Exception as exc:                    # noqa: BLE001
            print(f"[房间] 创建局域网房间失败: {type(exc).__name__}: {exc}")
            self._close_room()
            self._show_mode_selection()
            return
        self._show_lan_wait(room.port, stone)

    def _on_lan_join_requested(self, host, port):
        """加入方：握手成功才进对局；失败留在本页显示中文原因。

        席位不再是选出来的：房主开房时定过颜色，加入方走 ``seat="auto"``，
        由服务端分配剩下那一席；真实颜色在 ``_attach_room`` 里从 welcome
        读回，再校准 UI 镜像与日志。
        """
        if not host:
            self._lan_join_error("请输入房主的 IP 地址")
            return
        try:
            port_no = int(port)
            if not (1 <= port_no <= 65535):
                raise ValueError
        except ValueError:
            self._lan_join_error("端口要是 1–65535 的数字")
            return

        self.playmode = 2
        self._prepare_lan_game("局域网联机（加入方）")
        try:
            self._attach_room(None, [("auto", None)], host=host, port=port_no,
                              room_name=LAN_ROOM_NAME)
        except Exception as exc:                    # noqa: BLE001
            self._lan_join_error(_lan_join_error_text(exc))
            return
        if self.logger:
            self.logger.f.write("  我方执: %s\n"
                                % ("黑" if self.session.human_stone == 1 else "白"))
            self.logger.f.flush()
        self._update_panel()

    def _lan_join_error(self, text):
        """连接失败：拆掉半成品对局，重建加入页并显示原因（可原地重试）。"""
        self._close_room()
        self._open_lan_join(error=text)

    def _prepare_lan_game(self, mode_text):
        """LAN 开房 / 加入共用的开局准备。

        与单机 ``_start_game`` 的差别只有日志模式行与席位来源：拆旧局、
        开日志、复位 UI 镜像、建棋盘都在这里，事件处理、渲染、悔棋、终局
        全部走同一条 ``_on_room_event`` 路径 —— 不复制第二套对局逻辑。
        """
        self._close_room()
        if self.logger:
            self.logger.close()
        self.logger = GameLogger()
        self.logger.f.write(f"  模式: {mode_text}\n")
        self.logger.f.write("  难度: 无（玩家对战）\n")
        self.logger.f.write(
            f"  引擎: {engine.binary_path() or 'C++ 不可用，将用本地 Python 引擎'}\n\n")
        self.logger.f.flush()

        # LAN 双方都是 remote 人类席位；规则真源仍在房间的 Session 里，
        # 这里的 session 只是事件回放出来的 UI 镜像（与人机/本地对战一致）。
        self.session.configure("pvp")
        self.session.reset()
        self.ai_thinking = False
        self.review_records = []
        self._room_finished = False
        self._last_undo = None
        self._move_in_flight = False
        self._pending_snapshot = None
        self._room_config = None
        new_game()
        self._build_game_ui()

    def _show_lan_wait(self, port, stone):
        """房主等待遮罩：叠在棋盘页上，对手就位后的首个事件收回它。"""
        self.lan_wait = LanWaitScreen(local_ip(), port, stone)
        self.lan_wait.cancel_clicked.connect(self._on_lan_cancel)
        self._stack.addWidget(self.lan_wait)
        self._stack.setCurrentWidget(self.lan_wait)
        anim.fade_in(self.lan_wait)

    def _hide_lan_wait(self):
        """对手已就位（房间开始了 state/turn 广播）：收回等待遮罩露棋盘。"""
        wait, self.lan_wait = self.lan_wait, None
        if wait is None:
            return
        if self._stack is not None:
            self._stack.removeWidget(wait)
        wait.deleteLater()
        if self._stack is not None and self._game_row is not None:
            self._stack.setCurrentWidget(self._game_row)

    def _on_lan_cancel(self):
        """取消等待：整只拆掉房间与连接，回模式选择。"""
        self._close_room()
        self._show_mode_selection()

    def _attach_room(self, room, client_specs, *, host=None, port=None,
                     room_name=None):
        """把本 UI 接入一个房间（单机 / LAN 共用）。

        ``room`` 非 None：本 UI 创建并持有它（拆局时 ``stop()``）；加入
        远端房间时为 None，地址从 host/port/room_name 给。
        ``client_specs`` 是 ``[(seat_name, stone), ...]``：人机一条（人类
        席位），本地双人两条；LAN 加入方一条 ``("auto", None)`` —— 颜色由
        服务端分配，函数从 welcome 读回真实席位。

        失败时把已建立的连接连桥一起拆掉，再把异常抛给调用方 —— 单机回
        模式选择页，LAN 留在加入页显示原因。
        """
        if room is None and (host is None or port is None
                             or room_name is None):
            raise ValueError("加入远端房间必须给全 host/port/room_name")
        self._room = room
        bridge = _RoomBridge()
        bridge.event.connect(self._on_room_event)
        self._room_bridge = bridge
        try:
            for seat, stone in client_specs:
                client = RoomClient(on_event=bridge.event.emit)
                welcome = client.connect(host or room.host,
                                         port or room.port,
                                         room_name or room.name, seat)
                actual = 1 if welcome.get("seat", seat) == "black" else 2
                self._room_clients[actual] = client
                if stone is None:
                    # auto 入座：颜色由房间分配，UI 镜像照 welcome 校准
                    # （human_stone 决定 LAN 的胜负文案与悬停预览）。
                    self.session.human_stone = actual
                    self.gamemode = 0 if actual == 1 else 1
        except Exception:
            self._close_room()
            raise

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
        # LAN 等待遮罩收回时要切回这一行；保存引用而不是在 QStackedLayout
        # 里按索引找（布局顺序是添加顺序，改天多插一个控件就静默切错）。
        self._game_row = game_row
        row = QHBoxLayout(game_row)
        row.setContentsMargins(theme.SPACE_MD, theme.SPACE_MD,
                               theme.SPACE_MD, theme.SPACE_MD)
        row.setSpacing(theme.SPACE_SM)
        row.addWidget(self.board_widget, 1)
        row.addWidget(self.game_panel, 0)

        # 高度下限按**面板的实测最小值**兜底，不能用模块常量 MIN_H：
        # 那个数依赖字体度量（刻度文字、读数行的高度）与平台控件尺寸，
        # 只有 QApplication 起来之后才量得准。这里量出来比 MIN_H 高就抬上去 ——
        # 抬不上去的后果是面板被挤，图表压成一条缝，而它不会报错。
        need_h = 2 * theme.SPACE_MD + self.game_panel.minimumSizeHint().height()
        # 两道上限。第一道是档位/可用区（见 `_fit_cap_h`）；**第二道是初始尺寸**
        # —— Qt 会把 `resize()` 静默夹回最小尺寸，所以最小高度只要超过
        # `_initial_size()`，"启动尺寸按比例来"就作废了，窗口会直接长成最小尺寸
        # 那么大，连标题栏一起冲出屏幕。宁可面板挤一点，这部分由滚动兜底接住。
        need_h = min(need_h, self._fit_cap_h(), self._initial_size()[1])
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

    def _on_toggle_theme(self):
        """面板上的主题切换：换调色板 → QSS 重装 → 按钮图标翻转。

        棋盘色两套主题共享，board_render 的纹理/sprite/静态层缓存**不需要**
        作废 —— 切换是纯 QSS 操作，不会闪烁或卡顿。
        """
        theme.toggle_theme()
        self.game_panel.update_theme_button()

    def _on_board_click(self, event: QMouseEvent):
        """处理棋盘点击：只做前置检查，然后把着法交给对应的人类客户端。

        M4c 起 UI 不再自己落子/判胜 —— 规则与调度全在房间，本地提交后等
        ``move``/``state`` 事件回显（``_move_in_flight`` 拦住回显前的连点）。
        """
        if self.game_over or self.ai_thinking or self._move_in_flight:
            return

        pos = self.board_widget.get_grid_pos(event.x(), event.y())
        if pos is None:
            return
        r, c = pos
        if self.board[r][c] != 0:
            return

        if self.playmode == 1:
            # 本地双人：黑先交替，按手数奇偶决定这一手属于哪条连接。
            stone = 1 if self.move_count % 2 == 0 else 2
            client = self._room_clients.get(stone)
        else:
            client = self._room_clients.get(self.session.human_stone)
        if client is None:
            return                  # 房间没起来：别在假对局上继续操作

        # 先置在途标记再发送：回显到达前连点会被开头那个检查拦住，不会把
        # 同一手重复提交给房间。
        self._move_in_flight = True
        try:
            client.move(r, c)
        except RuntimeError:
            # 连接已断（重开/退出路径）：恢复交互，让用户可以重试或离开。
            self._move_in_flight = False

    # ------------------------------------------------------------------
    # 房间事件（M4c）
    #
    # UI 不再自己落子/判胜/悔棋，只把房间事件镜像到 Session 与控件上。
    # 槽都在 Qt 主线程执行（跨线程投递见 ``_RoomBridge``）。
    # ------------------------------------------------------------------

    def _on_room_event(self, ev):
        """房间事件的唯一入口（分派）。"""
        t = ev.get("type")
        if t in ("welcome", "state"):
            self._on_room_state(ev)
        elif t == "move":
            self._on_room_move(ev)
        elif t == "turn":
            self._on_room_turn(ev)
        elif t == "game_over":
            self._on_room_game_over(ev)
        elif t == "undo_applied":
            self._on_room_undo_applied(ev)
        elif t == "undo_proposed":
            self._on_room_undo_proposed(ev)
        elif t == "undo_rejected":
            # 我是请求方（对面才有 pending 弹窗）：棋盘与面板都没变，
            # 提示一句即可，不动任何状态。
            print(f"[房间] 对手拒绝了悔棋（席位 {ev.get('by')}）")
        elif t == "error":
            # 服务器权威拒绝（非法着法 / 悔棋不可用 / 协议错）。UI 从未改过
            # 状态，只需恢复可交互并把原因打出来；房间每次拒绝都带明确 code。
            self._move_in_flight = False
            print(f"[房间] {ev.get('code')}: {ev.get('message')}")

    def _on_room_state(self, ev):
        """welcome / state：全量同步（入座、开局、每次落子后、悔棋后）。"""
        if self.board_widget is None or self.game_panel is None:
            return                  # 页面已拆：丢掉迟到的排队事件

        # 房主还在等待屏上时，这个 state 说明对手已入座、对局开始了。
        self._hide_lan_wait()
        if "config" in ev:
            self._room_config = dict(ev["config"])
            self._apply_room_config()

        move_no = int(ev.get("move_no", 0))
        result = int(ev.get("result", 3))

        if move_no < self.move_count:
            # 悔棋后的回退：历史与复盘快照都剪到这一步，最后一手环清掉。
            del self.move_history[move_no:]
            self.human_moves = [m for m in self.human_moves
                                if m["seq"] <= move_no]
            self.session.last_move = None
            self.board_widget.set_last_move(None, None, None)
        elif move_no > self.move_count:
            # 理论上 state 不会领先 move 事件；真出现时用当前盘面补齐历史，
            # 保住"图表序列与 move_history 严格同长"这条不变量。
            self.move_history.extend(
                [self.board.copy()] * (move_no - self.move_count))

        self.session.board = np.array(ev["board"], dtype=int)
        self.session.move_count = move_no
        self.session.winner = int(ev.get("winner", 0))
        if result == 3:
            self.session.gamerule = 3
            self.session.game_over = False
        elif result == 0:
            # 平局：与旧 UI 的判和口径一致（winner=0）。
            self.session.gamerule = 0
            self.session.winner = 0
            self.session.game_over = True
        else:
            # 有人赢：房间的 Session 是 pvp（result 只到 2），"谁赢了"看
            # winner 与 UI 侧 playmode/石色的关系，重放成人机/本地对战/LAN
            # 的既有口径（AI 赢 = 1、人赢 = 2、本地对战都是 2、LAN 按我方色）。
            self.session.game_over = True
            if self.playmode == 1:
                self.session.gamerule = 2
            elif self.playmode == 2:
                self.session.gamerule = (
                    2 if self.session.winner == self.session.human_stone else 1)
            elif self.session.winner == self.session.ai_stone:
                self.session.gamerule = 1
            else:
                self.session.gamerule = 2

        self.board_widget.set_board(self.board)
        self._move_in_flight = False
        self._update_panel()

    def _on_room_move(self, ev):
        """move：镜像一手（棋盘 / 最后一手 / 历史 / 日志 / 追分）。"""
        if self.board_widget is None or self.game_panel is None:
            return
        move_no = int(ev.get("move_no", self.move_count + 1))
        if move_no <= self.move_count:
            # 本地双人局 UI 持有两条连接，同一手会收到两份 —— 只认第一份。
            return
        r, c, stone = int(ev["r"]), int(ev["c"]), int(ev["stone"])
        self.board[r][c] = stone
        self.session.move_count = move_no
        self.session.last_move = (r, c, stone)
        self.move_history.append(self.board.copy())
        self._move_in_flight = False

        self.board_widget.set_board(self.board)
        self.board_widget.set_last_move(r, c, stone)

        if self.playmode in (1, 2) or stone == self.session.human_stone:
            # 人类着法（本地双人 / LAN 双方都是人类）。
            if self.logger:
                self.logger.log_human(move_no, stone, r, c)
                # 每隔约5步记录一次完整棋盘状态
                if move_no % 5 == 1 or move_no <= 3:
                    self.logger.log_board_state(move_no, self.board)
            if self.playmode == 0:
                # 复盘快照：before 是**这一手落子前**的局面，来自人类回合的
                # turn 事件；seq 就是这一手的 move_no（与旧版逐字一致）。
                before = self._pending_snapshot
                if before is not None:
                    self.human_moves.append({
                        "seq": move_no, "r": r, "c": c, "before": before,
                    })
                self._pending_snapshot = None
        else:
            # AI 着法。f.closed 是纵深防御：房间关闭与事件投递之间的窗口里
            # 往已关闭文件写会抛 ValueError —— 在 Qt 槽里传播就是直接崩。
            info = ev.get("info")
            if self.logger and not self.logger.f.closed:
                if info is None:
                    info = {'reason': '未知'}
                self.logger.log_ai(move_no, stone, r, c, info)
                # 每隔约5步记录棋盘状态（与人类步数错开）
                if move_no % 5 == 0 or info.get('reason') in ('威胁检测', '搜索-发现必胜'):
                    self.logger.log_board_state(move_no, self.board,
                        f"AI={info.get('reason','')}")

        self._record_score(ev.get("info"))
        self._update_panel()

    def _on_room_turn(self, ev):
        """turn：轮到谁。人机局借此维护"思考中"与复盘快照；双人局恒空闲。"""
        if self.game_panel is None:
            return
        self._hide_lan_wait()
        stone = int(ev["stone"])
        if self.playmode == 0:
            is_ai = (stone == self.session.ai_stone)
            self.ai_thinking = is_ai
            self.game_panel.show_thinking(is_ai, stone)
            self.game_panel.undo_btn.setEnabled(not is_ai
                                                and self._cfg("allow_undo"))
            if not is_ai:
                # 人类落子前的局面：move 事件记录 human_moves 时取它。
                self._pending_snapshot = self.board.copy()
        elif self.playmode == 2:
            # LAN：没有本地 AI，思考态恒空；悔棋按钮由房间配置控制
            # （对手的悔棋提案另有 _on_room_undo_proposed 的弹窗）。
            self.ai_thinking = False
            self.game_panel.show_thinking(False)
            self.game_panel.undo_btn.setEnabled(self._cfg("allow_undo"))
        else:
            self.ai_thinking = False
            self.game_panel.show_thinking(False)
        # 指示卡文案与悬停预览由 _update_panel 刷新：AI 回合结束、人类回合
        # 开始时必须跑一次，否则面板会停在"AI 行动中"。
        self._update_panel()

    def _on_room_game_over(self, ev):
        """game_over：终局收尾。两条连接可能各收到一份，只处理一次。"""
        if self._room_finished or self.board_widget is None:
            return
        self._room_finished = True
        winner = int(ev.get("winner", 0))
        self.session.winner = winner
        self.session.game_over = True
        if winner == 0:
            self.session.gamerule = 0
        elif self.playmode == 1:
            self.session.gamerule = 2
        elif self.playmode == 2:
            self.session.gamerule = (
                2 if winner == self.session.human_stone else 1)
        elif winner == self.session.ai_stone:
            self.session.gamerule = 1
        else:
            self.session.gamerule = 2

        if winner:
            # 连五高亮 + 延迟遮罩（面板立刻停表，见 _finish_win_or_lose）。
            cells = win_line(self.board, winner)
            self.board_widget.set_win_cells(cells, winner)
            self._finish_win_or_lose()
        else:
            # 平局没有连五可看，不套 GAME_OVER_DELAY_MS（与旧行为一致）。
            self._show_game_over()

    def _on_room_undo_applied(self, ev):
        """undo_applied：图表立即回退；棋盘等紧随其后的 state 全量同步。"""
        key = (ev.get("by"), ev.get("move_no"))
        if key == self._last_undo:
            return                  # 本地双人两条连接收到的同一份，去重
        self._last_undo = key
        # UI 不再自己管悔棋政策，但面板要报"还剩几次"：房间成功悔棋一次，
        # 双方共同消耗一次预算（与 Session 的 output 计数同源）。
        self.session.output = max(0, self.output - 1)
        if self.game_panel is not None:
            self.game_panel.truncate_series(int(ev["move_no"]))

    def _on_room_undo_proposed(self, ev):
        """对手申请悔棋（M4d）：弹窗问用户，经我方连接应答。

        只有 ``auto_consent=False`` 的远程对手会走到这里（单机人机 /
        本地双人自动同意，直接收 undo_applied）；房间在 pending 期间冻结
        对局，所以这里的弹窗可以慢慢等用户点。

        槽在 Qt 主线程（跨线程投递见 ``_RoomBridge``），弹窗安全。
        **自己发起的申请房间也会广播回来**（by == 我方席位），不弹窗。
        """
        by = ev.get("by")
        me = self.session.human_stone
        if not me or by == me:
            print(f"[房间] 悔棋申请已发给对手（席位 {by}）")
            return
        client = self._room_clients.get(me)
        if client is None:
            return
        # 按钮上的 "Yes" / "No" 是 Qt 内置的（跟随 Qt 自己的翻译），我们只
        # 翻标题与正文 —— 界面语言是我们自己管的，Qt 的按钮语言管不着。
        answer = QMessageBox.question(
            self, i18n.t("悔棋请求"), i18n.t("对手请求悔棋，是否同意？"),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        try:
            client.undo_response(answer == QMessageBox.Yes)
        except RuntimeError:
            pass                    # 连接已断（退出/重开路径）

    def _cfg(self, name, default=True):
        """读房间功能开关。

        没有 config（测试直连、旧连接）时按**默认全开**返回 —— 与
        ``RoomConfig`` 的默认值一致，"没有配置"永远不能变成"禁用"。
        """
        conf = self._room_config
        if not conf:
            return default
        return bool(conf.get(name, default))

    def _apply_room_config(self):
        """把 welcome 下发的 RoomConfig 落到界面上。

        面板的悔棋 / 重开按钮与复盘入口是**入口级**禁用：房间服务端该
        拒绝的仍然拒绝（allow_undo / undo_limit），UI 只是别让用户白点。
        """
        if self.game_panel is None:
            return
        self.game_panel.undo_btn.setEnabled(
            self._cfg("allow_undo") and not self.ai_thinking)
        self.game_panel.restart_btn.setEnabled(self._cfg("allow_restart"))
        limit = (self._room_config or {}).get("undo_limit")
        if limit is not None:
            # 面板显示的剩余次数与房间同源（房间在 _run 里把额度设为 config）。
            self.session.output = int(limit)

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
        # 本地对战 / LAN 都没有本地 AI，"AI 视角分值"这条曲线没有主语 ——
        # 不画。留一条从第一手就凭空长出来的曲线，比留一张空图更糟：它会被
        # 当成真实评估读。房间配置 show_ai_scores=False 也走这里。
        if self.playmode in (1, 2):
            return
        if not self._cfg("show_ai_scores"):
            return
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

    def _on_undo(self):
        """悔棋：把申请交给对应的人类客户端，撤几步由房间政策决定。

        * 人机局 → 人类席位那条连接；房间对 AI 对手立即同意（撤到请求方
          上一次落子之前，见 ``room.py``），AI 先手被撤会重下天元；
        * 本地双人 → 最后一手所属的连接（黑先交替，看 move_count 奇偶）；
        * LAN → 我方连接；对手在另一台机器上，房间广播 undo_proposed，
          对手的 UI 弹窗应答（``_on_room_undo_proposed``）。
        """
        if self.game_over or self.ai_thinking or self._move_in_flight:
            return
        # 上限由房间兜底，但 UI 先拦一道：额度用尽的悔棋静默无动作，与旧版
        # 一致，也不会向房间白白请示一次。
        if self.output <= 0:
            return
        if self.playmode == 1:
            stone = 1 if self.move_count % 2 == 1 else 2   # 最后一手是谁下的
            client = self._room_clients.get(stone)
        else:
            client = self._room_clients.get(self.session.human_stone)
        if client is not None:
            client.undo_request()

    def _cancel_ai(self):
        """兼容垫片（M4c）：AIWorker 已退出主流程，房间搜索由 ``_close_room``
        收尾。保留这个方法只因为 ``tools/gui_smoke.py`` 仍会调用它；这里不再
        启动任何线程，正常情况下 ``ai_worker`` 恒为 None。
        """
        w = self.ai_worker
        if w is not None and w.isRunning():
            w.cancel()
            if not w.wait(3000):
                print("[警告] AI 线程未在 3 秒内响应取消")
        self.ai_worker = None
        self.ai_thinking = False

    def _close_room(self):
        """拆掉当前房间：关连接 → 断桥 → 停服务端。

        顺序是刻意的：

        1. 先关 ``RoomClient``（reader 线程 join，最多 1s）。读取线程就此
           停止发射；随后再断开 Qt 信号，避免"disconnect 与 emit 并发"这个
           PyQt 的跨线程竞态窗口。连接断开后 server 的处理线程会 ``detach``，
           房间对局线程随之退出（daemon，即使还在搜索也不挡住退出）；
        2. 断开桥并置空：迟到的排队事件在槽里遇到已置空的页面会直接返回；
        3. 最后停监听并复位本局状态，下一次 ``_start_game`` 从零新建。
        """
        for client in self._room_clients.values():
            client.close()
        self._room_clients = {}
        bridge, self._room_bridge = self._room_bridge, None
        if bridge is not None:
            try:
                bridge.event.disconnect()
            except TypeError:
                pass                # 本来就没有连接
        room, self._room = self._room, None
        if room is not None:
            room.stop()
        self.ai_worker = None
        self.ai_thinking = False
        self._move_in_flight = False
        self._pending_snapshot = None
        self._room_finished = False
        self._last_undo = None
        self._room_config = None

    def _on_restart(self):
        """重新开始：回到**模式选择**，与新开局的第一步一致。

        旧版直接回颜色页，等于把"跟谁下"这个问题跳过去 —— 上一局是本地对战
        的话，重开一局会莫名其妙地变成人机对战。
        """
        self._close_room()
        self._cancel_review(discard=True)
        if self.logger:
            # 打包版不写日志，`filepath` 是 None —— 别报一个"已保存:None"。
            if self.logger.filepath:
                print(f"[日志] 对局日志已保存: {self.logger.filepath}")
            self.logger.close()
        self._show_mode_selection()

    # ---- 战后复盘 ----

    def _cancel_review(self, discard=False):
        """协作式取消复盘线程并等它退出。与 ``_cancel_ai`` 同一套收尾。

        ``discard=False``（进度页上那颗「取消复盘」按钮）：线程停下来后照常走
        ``_on_review_done``，把**已经算完的那部分**展示出来 —— 那正是用户点
        取消时想看的东西。

        ``discard=True``（重开局 / 退出 / 关窗）：这些调用方接着就会把复盘页面
        连同整个 central 拆掉，结果没人收，所以置高代数让 ``_on_review_done``
        直接返回。不这么做的话，那个回调会在页面已被 deleteLater 之后醒来并
        ``_switch_page`` 到一个正在销毁的 widget 上。

        等待有 3 秒上限。超时后线程继续跑完自己的循环 —— 它算的是只读棋盘
        快照、不碰 UI，多跑一会儿无害。
        """
        if discard:
            self._review_generation += 1
        w = self.review_worker
        if w is not None and w.isRunning():
            w.cancel()
            if not w.wait(3000):
                print("[警告] 复盘线程未在 3 秒内响应取消")
        if discard:
            self.review_worker = None

    def _on_review_clicked(self):
        """从终局遮罩进入复盘：先彻底拆掉对局 UI（含房间），再问强度。"""
        self._close_room()
        self._cancel_review(discard=True)
        if self.logger:
            if self.logger.filepath:
                print(f"[日志] 对局日志已保存: {self.logger.filepath}")
            self.logger.close()
            self.logger = None
        self._drop_pages()
        self._show_review_strength()

    def _show_review_strength(self):
        """复盘强度：只列不低于本局难度的档位（``min_level``）。

        **自定义局跳过这一页。** 用户的口径是"直接取自定义难度的算法进行复盘"，
        所以没有可选项可挑；而且这一页的卡片表只装 ``DIFFICULTY`` 里的五档，拿
        ``min_level=6`` 去过滤会得到一张空页。
        """
        if self.gamekunnan == engine.CUSTOM_LEVEL:
            self._on_review_level_selected(engine.CUSTOM_LEVEL)
            return
        self.review_strength = SelectionScreen(mode="review",
                                               min_level=self.gamekunnan)
        self.review_strength.review_level_selected.connect(
            self._on_review_level_selected)
        self._switch_page(self.review_strength)

    def _on_review_level_selected(self, level):
        self.review_level = level
        self.review_progress = ReviewProgressScreen(len(self.human_moves),
                                                    level)
        self.review_progress.cancel_clicked.connect(self._cancel_review)
        self._switch_page(self.review_progress)

        human = 1 if self.gamemode == 0 else 2
        self._review_generation += 1
        gen = self._review_generation
        # 自定义局把**开局那一刻快照的参数**交给复盘；预设档传 None，照旧查表。
        cfg = (self.game_custom_cfg
               if level == engine.CUSTOM_LEVEL else None)
        self.review_worker = ReviewWorker(self.human_moves, human, level,
                                          cfg=cfg)
        self.review_worker.progressed.connect(self._on_review_progress)
        self.review_worker.completed.connect(
            lambda recs, lv=level, g=gen: self._on_review_done(recs, lv, g))
        self.review_worker.start()

    def _on_review_progress(self, done, total):
        # 页面可能已经被取消按钮之后的收尾流程丢掉（``_cancel_review`` 只停
        # 线程，不停页面），这里挡一下 —— 在已 deleteLater 的控件上调方法会抛
        # ``RuntimeError: wrapped C/C++ object has been deleted``。
        if self.review_progress is not None:
            self.review_progress.set_progress(done, total)

    def _on_review_done(self, records, level, generation=None):
        """复盘算完（或被取消）。只留"不是最优点"的那些。"""
        if generation is not None and generation != self._review_generation:
            return          # 迟到的结果：页面已被拆掉（见 ``_cancel_review``）
        w = self.review_worker
        cancelled = bool(w is not None and w.cancelled)
        self.review_worker = None

        # **一手不落地全收**，走对的也收。复盘动辄算几分钟，只留错手的话
        # 列表就断了：用户回头看"我第 9 手到底下哪儿了"会找不到，而棋谱的
        # 说服力恰恰来自连续。`_is_optimal` 负责把走对的那些标出来（绿字 +
        # 「最优」），不是靠把它们删掉。
        self.review_records = list(records)
        # 留着给"换档后重建这一页"用（`_rebuild_for_scale`）—— 它只认这两个
        # 参数加 `review_records` 就能重排一份一模一样的结果页。
        self.review_level = level
        self.review_cancelled = cancelled

        self.review_list = ReviewScreen(self.review_records, level, cancelled)
        self.review_list.record_selected.connect(self._on_review_record_selected)
        # 「结束复盘」接 ``_on_restart``：复盘是这一局的尾声，走完就该回到
        # "跟谁下"那个岔路口，与终局遮罩上的「再来一局」是同一个去处。
        self.review_list.finish_clicked.connect(self._on_restart)
        self.review_list.exit_clicked.connect(self._on_quit)
        self._switch_page(self.review_list)

    def _on_review_record_selected(self, idx):
        """点「棋局显示」：换到该手的复盘棋盘。

        **换一手就回收上一张棋盘。** ``_switch_page`` 只 ``addWidget``，不回收
        —— 连着看十手就会在栈里堆十棵完整的棋盘控件树（每棵还带两层 pixmap
        缓存），与 ``_drop_pages`` 注释里那个"每重开一局留一棵"是同一个毛病。
        """
        if not (0 <= idx < len(self.review_records)):
            return
        if self.review_board is not None:
            self.central.removeWidget(self.review_board)
            self.review_board.deleteLater()
            self.review_board = None
        # 记下"现在看的是第几手"：换档后要按同一手重建这张盘（`_rebuild_for_scale`）。
        self._review_board_idx = idx
        rec = self.review_records[idx]
        self.review_board = ReviewBoardScreen(rec)
        self.review_board.back_clicked.connect(self._back_to_review_list)
        self._switch_page(self.review_board)

    def _back_to_review_list(self):
        """从复盘棋盘回到结果列表。

        **不能走 ``_switch_page``**：那会 ``addWidget`` 一次，于是每看一手就把
        列表页重新入栈一份，来回几次后栈里躺着好几份同样的页面。
        """
        if self.review_list is None:
            return
        self.central.setCurrentWidget(self.review_list)
        anim.fade_in(self.review_list)

    def _on_quit(self):
        """退出"""
        self._close_room()
        self._cancel_review(discard=True)
        if self.logger:
            try:
                self.logger.close()
            except Exception:
                pass
        self.close()

    def closeEvent(self, event):
        """关窗前收掉房间与复盘线程，并停在途动画。

        ``QThread`` 在跑而它所属的对象被销毁时，Qt 会抛
        ``QThread: Destroyed while thread is still running`` 并可能直接崩。
        复盘线程比房间对局线程活得久（十几手 × 每手数秒），窗口管理器上的
        叉号完全可能落在它运行中间；房间的识别线程也一样要 join。
        动画同理由此收掉：窗口可能带着没跑完的淡入/落子动画被销毁。
        """
        self._close_room()
        self._cancel_review(discard=True)
        _stop_animations(self)
        super().closeEvent(event)

    def _update_panel(self):
        """更新右侧面板"""
        local = (self.playmode == 1)
        lan = (self.playmode == 2)
        # 轮次：单机人机要照顾 AI 先手（gamemode=1 时"轮到谁"要从玩家视角
        # 翻过来）；本地对战与 LAN 都是黑先交替，与 gamemode（我执哪色）无关。
        if self.playmode in (1, 2):
            turn = 1 if self.move_count % 2 == 0 else 2
        elif self.gamemode == 0:
            turn = 1 if self.move_count % 2 == 0 else 2
        else:
            turn = 2 if self.move_count % 2 == 0 else 1

        if lan:
            # LAN：双方都是人，报"你"的胜负（human_stone），信息行没有 AI。
            if self.game_over:
                if self.gamerule == 0:
                    status = "平局"
                elif self.winner == self.session.human_stone:
                    status = "你赢了！"
                else:
                    status = "你输了！"
            else:
                status = "进行中"
        elif local:
            # 终局文案由 `winner` 给：本地对战里没有"你"，只有黑方白方。
            if self.game_over:
                if self.gamerule == 0:
                    status = "平局"
                else:
                    status = "黑方获胜" if self.winner == 1 else "白方获胜"
            else:
                status = "进行中"
        elif self.gamerule == 1:
            status = "你输了！"
        elif self.gamerule == 2:
            status = "你赢了！"
        elif self.gamerule == 0:
            status = "平局！"
        else:
            status = "进行中"

        human = 1 if self.gamemode == 0 else 2
        self.game_panel.update_info(turn, self.gamekunnan, status,
                                    self.output, self.move_count, human=human,
                                    local=local or lan)

        # 悬停幽灵子只在**轮到你**时出现，且用你的颜色：AI 思考中还给预览、
        # 或玩家执白却预览黑子，都是在骗人。本地对战与 LAN 两边都是人，
        # 每一手都该有预览 —— 颜色跟着回合走。
        if self.board_widget is not None:
            if local or lan:
                self.board_widget.set_hover_player(
                    turn if not self.game_over else None)
            else:
                self.board_widget.set_hover_player(
                    human if (turn == human and not self.ai_thinking
                              and not self.game_over) else None)

    def _finish_win_or_lose(self):
        """赢或输的收尾：**立刻**停表并把面板刷成终局，**延后**弹遮罩。

        两件事的时间点必须分开：

        * 面板``_update_panel()``立即跑 —— 它才是停表的那一处（``update_info``
          看到 status 不是"进行中"就 ``stop_timer``）。放到延迟之后，"用时"会
          在玩家盯着连五的那一秒里继续跳，那是 2026-10-01 用户点名要修的第二件事。
        * 遮罩延后 ``GAME_OVER_DELAY_MS`` —— 它是整屏的，弹早了就把连五盖掉了。

        平局不走这里（没有连五可看，也就没有等的理由），仍直接 ``_show_game_over``。
        """
        self._update_panel()
        self._game_over_timer.start(GAME_OVER_DELAY_MS)

    def _show_game_over(self, *, record=True):
        """显示游戏结束覆盖层。

        ``record=False`` 是"换档后重建"（`_rebuild_game_page`）用的：终局早在
        第一次弹出时就记进日志了，重建只是把界面按新字号重新摆一遍，不该往
        日志里再写一份结果。
        """
        self._update_panel()

        local = (self.playmode == 1)
        lan = (self.playmode == 2)
        can_review = False
        if lan:
            # LAN：对手是人不是 AI，没有"算法复盘"的素材；文案从"你"的
            # 视角出发（human_stone 是这一局里我执的颜色）。
            if self.gamerule == 0:
                text = "平局！"
                is_win = False
                winner_str = "draw"
            elif self.winner == self.session.human_stone:
                text = "你赢了！"
                is_win = True
                winner_str = "human"
            else:
                text = "你输了！"
                is_win = False
                winner_str = "remote"
        elif local:
            # 本地对战：文案报的是"哪一方"赢，不是"你"。tone 只有 win/lose
            # 两种（theme 里就这两条规则），平局沿用 win —— 它至少不是个
            # "失败的红色"。
            if self.gamerule == 2:
                text = "黑方获胜" if self.winner == 1 else "白方获胜"
            else:
                text = "平局"
            is_win = True
            winner_str = {1: "black", 2: "white"}.get(self.winner, "draw")
        elif self.gamerule == 2:
            text = "你赢了！"
            is_win = True
            winner_str = "human"
        elif self.gamerule == 1:
            text = "你输了！"
            is_win = False
            winner_str = "ai"
            # 复盘入口**只在输给 AI 时**出现：赢了没有可复盘的东西，本地对战
            # 根本没有 AI。判据写在这里而不是让遮罩自己猜，是因为"这一局是不是
            # 输给 AI"只有主窗口知道（playmode + gamerule）。房间配置
            # allow_review=False 时同样不给 —— 入口级禁用。
            can_review = bool(self.human_moves) and self._cfg("allow_review")
        else:
            text = "平局！"
            is_win = False
            winner_str = "draw"

        # 记录对局结果到日志
        if record and self.logger:
            self.logger.log_result(winner_str,
                total_steps=self.move_count, move_count=self.move_count)
            # 记录终局完整棋盘
            self.logger.log_board_state(self.move_count, self.board, "[终局]")

        overlay = GameOverOverlay(text, is_win, can_review=can_review,
                                  can_restart=self._cfg("allow_restart"))
        overlay.restart_clicked.connect(self._on_restart)
        overlay.quit_clicked.connect(self._on_quit)
        overlay.review_clicked.connect(self._on_review_clicked)
        # 遮罩压在棋盘上，但右上角那枚齿轮是主窗口的子控件，浮在它上面 ——
        # 终局之后想改字号同样不必被迫退出。

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


#: 窗口图标文件名。构建期由 packaging/build_in_container.sh 从仓库根那个
#: .ico 生成，随 `--add-data` 打进 PyInstaller 包的根（也就是 sys._MEIPASS）。
#: 名字刻意用 ASCII：它要原样写进 --add-data 的命令行，而容器里未必有
#: UTF-8 locale。
APP_ICON = "gomoku-ai.png"


def _icon_candidates() -> list:
    """按可信度从高到低列出窗口图标的候选路径。

    顺序与 config._candidates() 同源。``sys._MEIPASS`` 排最前：onefile 与
    onedir 都会设这个变量，而 `--add-data "...:."` 正是落在那里 —— 一条就
    覆盖了三种打包形态（安装包 onedir、两个免安装裸文件）。后面两条是给
    源码直接跑的：仓库根有生成物就用，没有就退回原始 .ico 图源。
    """
    out = []

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        out.append(os.path.join(meipass, APP_ICON))

    if getattr(sys, "frozen", False):
        out.append(os.path.join(os.path.dirname(os.path.abspath(sys.executable)),
                                APP_ICON))

    here = os.path.dirname(os.path.abspath(__file__))
    out.append(os.path.join(here, APP_ICON))
    out.append(os.path.join(here, "五子棋.ico"))

    return out


#: 交给 `setWindowIcon()` 的图标边长上限。
#:
#: **这不是省内存，是撞上了一个硬上限。** X11 下 Qt 把图标一次性写进
#: `_NET_WM_ICON` 属性，一次 `xcb_change_property` 写完。X 的单次请求上限是
#: 65535 个 4 字节字，即 262140 字节，而 256×256 的 ARGB 是 262152 字节 ——
#: **不多不少超了 12 个字节**。那次请求失败、属性留空，症状是"任务栏有图标
#: （那走 .desktop），标题栏却是空白"。实测：256×256 时属性为空，128×128
#: 时正常写入。128 给标题栏（约 22px）和 HiDPI 任务栏（约 48px）都绰绰有余。
_ICON_MAX_PX = 128


def _app_icon() -> QIcon:
    """找得到就用，找不到返回空 QIcon。

    **图标缺失不该让程序起不来** —— 它纯属观感，而候选路径里任何一条失效
    （打包漏了 --add-data、源码树被裁过）都不该把启动拦下来。
    """
    for path in _icon_candidates():
        if not os.path.isfile(path):
            continue
        loaded = QIcon(path)
        # 用 availableSizes() 判断而不是 isNull()：文件存在但不是图片时
        # QIcon **依然不是 null**，只是里面一个 pixmap 都没有 —— 拿 isNull()
        # 当门禁会把"读不出来"放过去，窗口照样没有图标。
        sizes = loaded.availableSizes()
        if not sizes:
            continue
        icon = QIcon()
        for size in sizes:
            pixmap = loaded.pixmap(size)
            if pixmap.isNull():
                continue
            if pixmap.width() > _ICON_MAX_PX or pixmap.height() > _ICON_MAX_PX:
                pixmap = pixmap.scaled(_ICON_MAX_PX, _ICON_MAX_PX,
                                       Qt.KeepAspectRatio,
                                       Qt.SmoothTransformation)
            icon.addPixmap(pixmap)
        if icon.availableSizes():
            return icon
    return QIcon()


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

    # 图标必须在这里显式设，**不能只靠安装包里的 .desktop**。
    #
    # Linux 下标题栏与任务栏读的是窗口自身的图标属性：X11 是 `_NET_WM_ICON`
    # （桌面环境对认不出身份的 X11 窗口会退回 X.Org 的 logo），Wayland 是
    # app_id。这两个都只有 QApplication 设过之后才存在 —— .desktop 的 `Icon=`
    # 只管菜单与启动器那一条入口，管不到已经开着的窗口。实测过：装好带图标的
    # .desktop 之后窗口属性里仍然一个图标都没有。
    # `setDesktopFileName` 在 Qt 里是 Unix 专属 API（Windows 构建上根本没有这
    # 个方法），所以这里不能直接调 —— 少了这个判断，Windows 版会在启动第一行
    # 抛 AttributeError。取值必须与 packaging/build_in_container.sh 里那个
    # .desktop 的文件名一致。
    if hasattr(app, "setDesktopFileName"):
        app.setDesktopFileName("gomoku-ai")

    icon = _app_icon()
    if icon.availableSizes():
        app.setWindowIcon(icon)

    window = GomokuGame()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()

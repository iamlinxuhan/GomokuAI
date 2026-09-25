# -*- coding: utf-8 -*-
"""设计系统：双调色板（深/浅）+ 字号 / 间距 / 圆角 / 字体族 / 样式表生成。

全应用**唯一**的样式定义处。改配色只改这里的常量，不是散落各处的 36 处
``setStyleSheet``。

## 双调色板与 PEP 562 代理

界面色按主题分两套（``_PALETTES["dark"]`` / ``_PALETTES["light"]``），经模块级
``__getattr__`` 代理取值 —— 所有 ``theme.ACCENT`` 调用点**零改动**。棋盘色
（木色 / 网格 / 棋子 / 最后一手环）是两套主题共享的模块常量：棋盘是唯一暖色
焦点，主题切换**不**重建任何棋盘 pixmap。

注意：``__getattr__`` 只在属性**未命中**时触发，所以本模块内部引用界面色必须
走 ``_c("NAME")`` 帮助函数 —— 模块内代码查全局名不会经过 ``__getattr__``。

## 为什么 token 只存字符串，不存 ``QColor``

把 ``QColor`` 插值进 f-string 会得到 ``<PyQt5.QtGui.QColor object at 0x...>``,
那是一段非法 CSS —— **Qt 会静默丢弃整条规则**，只打印一行 "Could not parse
stylesheet"，不报位置。历史上 ``_make_card_btn`` 的 hover 色就因此失效过。
只要本模块不产出 ``QColor``，这类错误在结构上不再可能，``_qss_color()`` 那个
补丁函数也就可以退役。自绘处需要 ``QColor`` 时写 ``QColor(theme.WOOD)``。

## 为什么用 ``string.Template`` 而不是 f-string

QSS 里 ``$`` 从不出现在合法语法中，所以 ``$name`` 占位符可以让模板保持
一字不改的 QSS 原貌。f-string 则必须把每对花括号写成 ``{{ }}`` —— 任何新增
选择器都要记得转义，漏一个括号就整条规则静默失效。

而且 ``Template.substitute``（**非** ``safe_substitute``）遇到未定义的 key 会
**当场 KeyError**：把"样式写错"从运行期静默失败变成启动期崩溃。

## 颜色都是实测过对比度的

括号里是 WCAG 对比度最差值 / 要求的阈值。棋盘上的墨色要按**最暗角落**算，
因为木板有光照梯度 —— 详见 ``WOOD_DARK`` 的注释。
"""

from __future__ import annotations

from string import Template

from PyQt5.QtCore import QSettings
from PyQt5.QtGui import QFont, QFontDatabase
from PyQt5.QtWidgets import QApplication

# ==================== 双调色板：界面色 ====================
# 值随主题切换。棋盘色不在这里 —— 见下方"棋盘（共享）"。
#
# 浅色的对比度按承载面取最差值：SURFACE(#ffffff 渐变到 #fbfcfd) 与
# SURFACE_2(#eceef2)。
_PALETTES: dict[str, dict[str, str]] = {
    "dark": dict(
        BG="#12141a",          # 窗口底
        BG_HI="#171a22",       # 窗口渐变亮端
        SURFACE="#1a1d25",     # 面板
        SURFACE_HI="#1f232d",  # 面板渐变亮端
        SURFACE_2="#232833",   # 信息行 / 次级容器
        BORDER="#3c4354",      # 卡片描边。#2e3440 对卡片只有 1.18:1，
                               # 等于没画 —— 卡片面与窗口底本身也只差
                               # 1.25:1，"卡片"的边界靠这条线。
        TRACK="#3c4354",       # 进度条槽
        TEXT="#e9ecf2",        # 主文字     12.47 / 4.5  ✅
        TEXT_DIM="#9aa4b5",    # 次文字      5.87 / 4.5  ✅
        TEXT_FAINT="#8a94a6",  # 弱文字      4.83 / 4.5  ✅
        # 冷青强调。木盘的暖色只留在棋盘上 —— 界面 chrome 一律冷调，棋盘
        # 因此成为全局唯一的暖色，而不是"众多暖色里的一块"。
        # 实测：vs BG 9.28 / vs SURFACE 8.50 / vs SURFACE_2 7.44，全 ✅
        ACCENT="#45c8e0",
        ACCENT_HI="#6ad6ea",   # 渐变亮端 / hover
        ACCENT_LO="#2ba4bd",   # 渐变暗端（vs SURFACE_2 仍有 5.02 ✅）
        ON_ACCENT="#12141a",   # 强调色底上的文字   9.28 ✅
        SUCCESS="#7fd18a",     # 8.01 ✅
        SUCCESS_HI="#98dfa2",
        SUCCESS_LO="#68b673",
        DANGER="#e8747c",      # 5.07 ✅
        DANGER_HI="#f0919a",
        DANGER_LO="#c85f68",
        # 紫。原先是蓝（#6fb3e0），和新的青色强调撞色 —— "AI 思考中"与
        # "可点击的强调"必须是两种颜色。vs SURFACE 6.19 / SURFACE_2 5.42 ✅
        INFO="#a78bfa",
        GHOST_HOVER="#2b3140",
        GHOST_PRESS="#20252f",
        OVERLAY="rgba(10, 12, 16, 195)",   # 结算遮罩底
    ),
    "light": dict(
        BG="#f4f5f8",
        BG_HI="#eef0f5",
        SURFACE="#ffffff",
        SURFACE_HI="#fbfcfd",
        SURFACE_2="#eceef2",
        BORDER="#8791a3",      # 3.18 vs 白卡 —— 非文字 UI 阈值 3.0  ✅
        TRACK="#d5d9e2",
        TEXT="#1c212b",        # 16.5 ✅
        TEXT_DIM="#4a5261",    # 7.87 ✅
        TEXT_FAINT="#5b6372",  # 6.05 ✅
        ACCENT="#0e6f85",      # 深冷青，白字 5.78 ✅（vs BG 5.30，vs SURFACE_2 4.98 ✅）
        ACCENT_HI="#0d7d94",   # 白字 4.80 ✅（渐变顶 / hover 上限）
        ACCENT_LO="#0a5c70",   # 白字 7.56 ✅
        ON_ACCENT="#ffffff",
        SUCCESS="#1e7538",     # 白字 5.74 ✅
        SUCCESS_HI="#238340",  # 白字 4.78 ✅
        SUCCESS_LO="#1a6531",
        DANGER="#b0303a",      # 白字 6.30 ✅
        DANGER_HI="#cc3a45",   # 白字 4.94 ✅
        DANGER_LO="#992a33",
        INFO="#6d3fc4",        # 深紫，SURFACE 6.65 / SURFACE_2 5.72 ✅
        GHOST_HOVER="#e2e5ec",
        GHOST_PRESS="#d5d9e2",
        OVERLAY="rgba(236, 238, 242, 215)",
    ),
}

_current = "dark"
_explicit = False        # 本进程是否显式选过主题（见 set_theme / install）
_SETTINGS_ORG = "GomokuAI"
_SETTINGS_APP = "GomokuAI"
_SETTINGS_KEY = "theme"


def _c(name: str) -> str:
    """模块内部取当前调色板 token（模块内全局名查找不走 ``__getattr__``）。"""
    return _PALETTES[_current][name]


def __getattr__(name: str):
    """PEP 562：``theme.ACCENT`` 等界面色按当前主题取值，调用点零改动。

    棋盘色与度量常量是真实模块属性，不会走到这里。
    """
    try:
        return _PALETTES[_current][name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list:
    return sorted(set(globals()) | set(_PALETTES[_current]))


def current_theme() -> str:
    return _current


def available_themes() -> tuple:
    return tuple(_PALETTES)


def set_theme(name: str, *, persist: bool = True) -> None:
    """切换主题：换调色板 → 重装 QSS（Qt 自动 repolish 全部控件）→ 持久化。

    棋盘色共享，所以**不需要**作废 board_render 的任何缓存 —— 切换是
    纯 QSS 操作，零卡顿。``persist=False`` 供测试使用（不污染用户配置）。
    """
    global _current, _explicit
    if name not in _PALETTES:
        raise ValueError(f"未知主题 {name!r}，可选：{available_themes()}")
    _current = name
    _explicit = True     # 见 install()：本进程内的显式选择优先于落盘的偏好
    app = QApplication.instance()
    if app is not None:
        app.setStyleSheet(app_stylesheet())
    if persist:
        QSettings(_SETTINGS_ORG, _SETTINGS_APP).setValue(_SETTINGS_KEY, name)


def toggle_theme(*, persist: bool = True) -> str:
    """深浅互切，返回切换后的主题名。"""
    nxt = "light" if _current == "dark" else "dark"
    set_theme(nxt, persist=persist)
    return nxt


# ==================== 颜色：棋盘（两套主题共享）====================
# 底色采样自仓库根目录的参考图 input.png（木色中位 #dc8d39，范围
# #ac5212..#f5bc65，光源在左上）。
#
# **但梯度必须压缩。** 参考图那张戏剧性光照下，**没有任何一种墨色能在全盘
# 达标**：坐标色在最暗角落只剩 3.17:1（小字需 4.5）。参考图是无标注的装饰
# 渲染，真棋盘要保证 19 条线与 38 个标注处处可读 —— 所以把梯度压到约 ±10%。
# 下面这组值在最暗角落的对比度见每个常量的注释。
WOOD_DARK  = "#cf8f42"   # 光照暗角（右下）
WOOD       = "#da9d4f"   # 中位
WOOD_LITE  = "#e5ab5c"   # 光照亮角（左上）
WOOD_EDGE  = "#8a6634"   # 木盘外描边（装饰性，不承载信息）
GRID       = "#5f3d15"   # 网格线    最暗 3.54 / 图形阈值 3.0  ✅
STAR       = "#4f2f0c"   # 星位      最暗 4.39 / 3.0  ✅
COORD      = "#41250a"   # 坐标标注  最暗 5.13 / 小字 4.5  ✅
STONE_B    = "#16150f"   # 黑子（暖近黑，对齐参考图实测 #14130d）
STONE_W    = "#f8f3e9"   # 白子（奶白，对齐参考图实测 #f8f3e9）
STONE_W_RIM = GRID       # 白子描边与网格线**同色** —— 设计系统里"墨"只有一份。
                         # 这是**必需**而非装饰：白子与亮角木色只有 1.84:1，
                         # 物理上白子放在浅木上本来就靠边缘与阴影区分。
                         # 描边给它 4.76 vs 最亮木 / 8.77 vs 子。
STONE_B_RIM = "#3a382f"  # 黑子描边（柔和，黑子对木色已有 7.77）
LAST_LIGHT = "#f2ead9"   # 最后一手：黑子上的近白环（15.28:1）
LAST_DARK  = "#4a2c0f"   # 最后一手：白子上的深棕环（11.45:1）

# 棋子的径向渐变停靠点 (位置, 颜色)。光源固定在左上，所以 sprite 可以复用。
# 放在这里而不是绘制模块里，是为了让"颜色只在 theme 里定义一次"这条规则
# 没有例外 —— tests/test_no_literal_colors.py 会强制它。
STONE_B_GRAD = ((0.00, "#5c6169"), (0.55, "#23262b"), (1.00, "#0d0f12"))
STONE_W_GRAD = ((0.00, "#fffdf9"), (0.55, "#efece4"), (1.00, "#cdc6b8"))

# 棋子接触投影（rgba 整数元组，QColor 直接吃）。压在木面上的"重量感"来源。
STONE_SHADOW_NEAR = (74, 44, 15, 70)    # 紧贴棋子的深影
STONE_SHADOW_FAR  = (74, 44, 15, 55)    # 向外扩散的软影
# 内嵌棋盘面的提亮填充
FACE_TINT = (255, 240, 215, 26)
# 木盘内缘光照收边：顶缘提亮、底缘压暗（rgba 元组）。木盘铺满控件短边，
# 外投影没有空间，"厚度"只能靠内缘明暗暗示。
PLATE_TOP_TINT   = (255, 244, 220, 46)
PLATE_BOT_SHADOW = (0, 0, 0, 60)
PLATE_BOT_FADE   = (0, 0, 0, 0)

# ==================== 字号阶 ====================
# 5 档。**刻意不留 13 与 24**：12/13/14 三档差 1px，1x 屏上人眼不可分辨，
# 保留它们等于把"无模数"换个名字留着；20→32 的跳跃是刻意的，XL 是唯一的
# display 档。将来若确实需要中间档，24 是天然的插入点。
SIZE_XS = 12    # 页脚、统计行、坐标标注、次要提示
SIZE_SM = 14    # 信息行正文、副标题
SIZE_MD = 16    # 按钮标签、卡片主标题
SIZE_LG = 20    # 面板标题、区块标题
SIZE_XL = 32    # 屏幕主标题、结算结果

# ==================== 间距阶（4px 基准）====================
SPACE_XS  = 4     # 图标与文字
SPACE_SM  = 8     # 标题<->副标题、组内行距
SPACE_MD  = 12    # 信息行之间、按钮之间、窗口外边距
SPACE_LG  = 16    # 面板内边距、组与组
SPACE_XL  = 24    # 卡片间距、面板内容内边距
SPACE_XXL = 32    # 标题区<->内容区
SPACE_XXXL = 48   # 屏幕外缘（大屏）

# ==================== 圆角阶 ====================
RADIUS_SM = 8     # 按钮、进度条、条目
RADIUS_MD = 12    # 卡片、面板、遮罩
RADIUS_LG = 16    # 棋盘木盘 —— **唯一一档**

# ==================== 控件尺寸 ====================
CONTROL_H = 40    # 标准按钮高度（面板上的重开/退出曾经是 45，属无理由差异）
CARD_PX = 160     # 选择卡片
PANEL_W = 264     # 右侧面板宽度
CHART_PLOT_H = 32  # 面板图表绘图区的**地板值**。真正的下限由 charts._Plot 按
                   # "每条刻度文字一行"现算（3 条刻度 × 13px = 39px）—— 字体
                   # 度量随平台变，写死会在这台机器上刚好、在别人机器上叠成一坨
                   # （实测 28px 时三条刻度确实重叠）。这个常量只是不让极窄的
                   # 布局把绘图区压成一条线。

# ==================== 字体族 ====================
_UI_STACK = ("Microsoft YaHei UI", "Microsoft YaHei", "微软雅黑",   # Windows
             "PingFang SC", "Hiragino Sans GB",                     # macOS
             "Noto Sans CJK SC", "Source Han Sans SC",              # Linux
             "WenQuanYi Micro Hei", "Droid Sans Fallback")
_MONO_STACK = ("Cascadia Mono", "Consolas", "SF Mono", "Menlo",
               "JetBrains Mono", "DejaVu Sans Mono", "Noto Sans Mono CJK SC")

_cached_families: dict[str, str] = {}


def resolve_family(mono: bool = False) -> str:
    """返回可直接写进 QSS ``font-family`` 的候选链字符串。

    必须在 ``QApplication`` 之后调用（``QFontDatabase`` 需要 QGuiApplication）。

    **末尾必须挂通用族。** 实测 Qt5/Linux 下 ``monospace`` / ``sans-serif``
    由 fontconfig 解析（本机分别落到 Noto Sans Mono CJK SC / Noto Sans CJK SC），
    所以即使候选链一个都没装也不会退化成方框 —— 正确性交给平台解析器。

    不要改用 ``QFontMetrics.inFont('五')`` 来判断"有没有中文字体"：实测
    ``QFont("NoSuchFontXYZ").inFont('五')`` **也返回 True**（fontconfig 逐字
    回退），用它写的护栏等于没写。
    """
    key = "mono" if mono else "ui"
    if key not in _cached_families:
        installed = set(QFontDatabase().families())
        stack = _MONO_STACK if mono else _UI_STACK
        chain = [f for f in stack if f in installed]
        chain.append("monospace" if mono else "sans-serif")
        # 每个族名加引号：族名里可能有空格（"Noto Sans CJK SC"），
        # 不加引号 QSS 会把它当成多个族。
        _cached_families[key] = ", ".join(f"'{f}'" for f in chain)
    return _cached_families[key]


def mono_font(size_px: int = SIZE_XS) -> QFont:
    """QPainter 路径专用（QSS 是字符串，吃不到 QFont）。"""
    f = QFont()
    fams = [f.strip("'") for f in resolve_family(mono=True).split(", ")]
    f.setFamily(fams[0])
    f.setPixelSize(size_px)
    return f


def has_cjk_font() -> bool:
    """候选链里**有没有**一个真的装着的简体中文字体。

    供加载页决定是否提示用户。注意这问的是"候选链命中"，不是"能不能显示
    中文" —— 后者由 fontconfig 的逐字回退兜底，无法可靠探测。所以只在
    一个候选都没命中时才警告（此时回退链很可能也拿不到中文）。
    """
    installed = set(QFontDatabase().families())
    return any(f in installed for f in _UI_STACK)


# ==================== 样式表模板 ====================
# QProgressBar：**定量条 + 自绘 chunk**，不是不定量条。
#
# 起因是实测踩到的坑：`setRange(0, 0)`（不定量）配 Fusion 风格时，进度条画成
# 一整条 Fusion 默认蓝 `#61b4ea`，既不是主题色，也没有可见的 busy 动画。而
# 一旦给不定量条写 `::chunk`，又会盖掉 Fusion 的 busy 动画 —— 条直接变空。
# 这两条路都不通，所以走第三条：
#
#   定量条（0→100）由 QTimer 驱动，chunk 用主题强调色（冷青）渐变。
#
# 顺带比原方案更诚实：开场交接的时长是**已知的** `SPLASH_MS`，进度条如实
# 反映"还要等多久"，而不是用一段与真实加载无关的假动画糊弄。
# 将来若真接入异步初始化，把驱动源从定时器换成实际进度即可，样式不用动。
_BASE = Template("""
QLabel                        { color: $text; background: transparent;
                                font-family: $font_ui; font-size: ${s_sm}px; }
QLabel[role="title"]          { font-size: ${s_xl}px; font-weight: bold; }
QLabel[role="subtitle"]       { color: $text_dim; font-size: ${s_sm}px; }
QLabel[role="faint"]          { color: $text_faint; font-size: ${s_xs}px; }
QLabel[role="value"]          { font-weight: bold; }
QLabel[role="panel-title"]    { font-size: ${s_lg}px; font-weight: bold; }
QLabel[role="turn-text"]      { font-size: ${s_md}px; font-weight: bold; }
QLabel[role="mono"]           { font-family: $font_mono; }

/* 面板图表的文字。绘图区是自绘的（charts.py，它的颜色一律走 theme.* 取当前
   主题），这两条只管卡片上那两行字：标题用次文字色，读数用弱文字色 + 等宽
   —— 等宽的用处是数字跳动时宽度不抖。 */
QLabel[role="chart-title"]    { color: $text_dim; font-size: ${s_xs}px; }
QLabel[role="chart-readout"]  { color: $text_faint; font-size: ${s_xs}px;
                                font-family: $font_mono; }

/* 选择卡左上角的序号（01/02/03）。弱文字色是刻意的：它是索引不是内容，
   卡片真正要传达的是棋子图形与"先手/后手"。 */
QLabel[role="card-index"]     { color: $text_faint; font-size: ${s_xs}px;
                                font-family: $font_mono; }

/* 回合指示卡文字的语义色调（与 value 的色调同一套语义）。 */
QLabel[role="turn-text"][tone="win"]   { color: $success; }
QLabel[role="turn-text"][tone="lose"]  { color: $danger; }
QLabel[role="turn-text"][tone="think"] { color: $info; }

/* 值上的语义色调。状态色只落在 SURFACE / BG 上；若将来要用在 SURFACE_2 的
   信息行里，需重新验对比度。 */
QLabel[role="value"][tone="win"]   { color: $success; }
QLabel[role="value"][tone="lose"]  { color: $danger; }
QLabel[role="value"][tone="think"] { color: $info; }
QLabel[role="title"][tone="win"]   { color: $success; }
QLabel[role="title"][tone="lose"]  { color: $danger; }

QMainWindow                   { background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                stop:0 $bg_hi, stop:1 $bg); }
QWidget#screenRoot            { background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                stop:0 $bg_hi, stop:1 $bg); }
QWidget#overlayRoot           { background: $overlay;
                                border-radius: ${r_md}px; }

/* 面板是一块浮起来的卡片，四角都圆：竖向微渐变给"面板有厚度"的质感，
   1px 描边让边界可见（SURFACE vs BG 本身对比不够）。 */
QFrame[role="panel"]          { background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                stop:0 $surface_hi, stop:1 $surface);
                                border: 1px solid $border;
                                border-radius: ${r_md}px; }
QFrame[role="card"]           { background: $surface_2;
                                border: 1px solid $border;
                                border-radius: ${r_md}px; }
QFrame[role="separator"]      { background: $border; border: none;
                                max-height: 1px; }

/* 回合指示卡：常态低调，思考态描边转 INFO 紫并配合呼吸动画。 */
QFrame[role="turn-card"]      { background: $surface_2;
                                border: 1px solid $border;
                                border-radius: ${r_md}px; }
QFrame[role="turn-card"][state="think"] { border-color: $info; }

QProgressBar                  { background: $track; border: none;
                                border-radius: ${r_sm}px; }
QProgressBar::chunk           { background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                stop:0 $accent_hi, stop:1 $accent);
                                border-radius: ${r_sm}px; }

QPushButton                   { background: $surface_2; color: $text;
                                border: 1px solid $border;
                                border-radius: ${r_sm}px;
                                font-family: $font_ui; font-size: ${s_md}px;
                                font-weight: bold; padding: 0 ${sp_lg}px; }
QPushButton:disabled          { background: $surface; color: $text_faint;
                                border-color: $surface; }
""")

# 四态 + focus 一次生成，对**每个**变体都生成。
#
# 三条 Qt 语义（都是踩过的坑）：
#  * 用 `:hover:enabled` 而不是 `:hover` —— Qt 在部分风格下会让**禁用**控件
#    仍然匹配 `:hover`，于是"变灰了但鼠标一碰又亮起来"。
#  * `:disabled` 放在最后 —— 同特异性下后者胜，顺序就是优先级。
#  * 改 `variant` 动态属性后必须 unpolish/polish（见 `set_variant`），
#    Qt 不会因为 setProperty 自动重算样式。
#
# base / hover / press 是**完整的 QSS 颜色串**（纯色或 qlineargradient），
# 由 `_button_variants()` 按当前调色板现算 —— 不再是模块级常量，切主题后
# 重新生成样式表自然带上新色。
_BTN = Template("""
QPushButton[variant="$v"]                 { background: $base; color: $fg;
                                            border: $border;
                                            border-radius: ${r}px;
                                            font-family: $font_ui;
                                            font-size: ${fs}px; font-weight: bold;
                                            padding: 0 ${pad}px; }
QPushButton[variant="$v"]:hover:enabled   { background: $hover; }
QPushButton[variant="$v"]:pressed:enabled { background: $press; }
QPushButton[variant="$v"]:checked         { background: $press; }
QPushButton[variant="$v"]:focus           { border: 2px solid $accent; }
QPushButton[variant="$v"]:disabled        { background: $dis_bg;
                                            color: $dis_fg;
                                            border-color: $dis_bg; }
""")


def _vgrad(hi: str, lo: str) -> str:
    """竖向两停靠点渐变的 QSS 颜色串。QSS 不支持 box-shadow，渐变 + 圆角
    就是 Qt 里最接近"现代按钮"的质感。"""
    return f"qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {hi}, stop:1 {lo})"


def _button_variants() -> dict:
    """按当前调色板现算按钮变体。彩色按钮的渐变两端都必须满足
    ``ON_ACCENT`` 文字的对比度（浅色主题因此整体比深色深一档）。"""
    return {
        "primary": dict(
            base=_vgrad(_c("ACCENT_HI"), _c("ACCENT")),
            hover=_c("ACCENT_HI"),
            press=_vgrad(_c("ACCENT"), _c("ACCENT_LO")),
            fg=_c("ON_ACCENT")),
        "success": dict(
            base=_vgrad(_c("SUCCESS_HI"), _c("SUCCESS")),
            hover=_c("SUCCESS_HI"),
            press=_vgrad(_c("SUCCESS"), _c("SUCCESS_LO")),
            fg=_c("ON_ACCENT")),
        "danger": dict(
            base=_vgrad(_c("DANGER_HI"), _c("DANGER")),
            hover=_c("DANGER_HI"),
            press=_vgrad(_c("DANGER"), _c("DANGER_LO")),
            fg=_c("ON_ACCENT")),
        "ghost": dict(
            base=_c("SURFACE_2"), hover=_c("GHOST_HOVER"),
            press=_c("GHOST_PRESS"), fg=_c("TEXT")),
    }


# 选择卡片：色调是**数据**，不是 5 段手抄的样式表。
#
# **卡片底色一律是 SURFACE**，色调只落在文字上。旧版是五块整面高饱和色板
# （黑卡近黑、白卡近白、难度卡红黄绿），在深色底上读起来像红绿灯，是全部界面
# 里最显旧的一处。现在区分靠**卡面图形**（棋子本身，见 ``ui_kit.StoneFace``）
# 与文字色，底色退回去当承载面。
#
# 文字色实测在 SURFACE 上的对比度（深色盘）：TEXT 14.24 / SUCCESS 9.14 /
# ACCENT 8.50 / DANGER 5.79，小字阈值 4.5，全部达标。**不要再往 SURFACE_2 上
# 放状态色** —— DANGER 在那里只剩 5.07，虽然仍达标但余量已很薄。
# （已经放上去的只有面板图表：ACCENT 7.44 / INFO 5.42，都够。）
#
# hover 用 SURFACE_2 而不是 SURFACE_HI：后者在浅色主题下是 #fbfcfd，落在
# 白卡上等于没变。SURFACE_2 两套主题里都与 SURFACE 有可见差（深色更亮、
# 浅色更暗），这是它成为 hover 面的原因。
def _card_tones() -> dict:
    return {
        "black":   dict(fg=_c("TEXT")),
        "white":   dict(fg=_c("TEXT")),
        "success": dict(fg=_c("SUCCESS")),
        "primary": dict(fg=_c("ACCENT")),
        "danger":  dict(fg=_c("DANGER")),
    }


_CARD = Template("""
QPushButton[variant="card"][tone="$t"]                { background: $surface;
                                                        border: 1px solid $border;
                                                        border-radius: ${r}px; }
QPushButton[variant="card"][tone="$t"]:hover:enabled  { background: $hover;
                                                        border-color: $accent; }
QPushButton[variant="card"][tone="$t"]:pressed:enabled{ background: $press; }
QPushButton[variant="card"][tone="$t"]:focus          { border-color: $accent; }
QPushButton[variant="card"][tone="$t"]:disabled       { background: $surface;
                                                        border-color: $border; }
QLabel[role="card-text"][tone="$t"]                   { color: $fg;
                                                        font-family: $font_ui;
                                                        font-size: ${fs}px;
                                                        font-weight: bold; }
""")

# 卡面子标题（"先手" / "思考上限 3 秒"）。与卡面主标题同为子控件，所以
# 需要一条 role 规则把 _BASE 里 `QLabel{color:$text}` 的默认色压回去。
_CARD_SUB = Template("""
QLabel[role="card-sub"] { color: $text_dim; font-size: ${s_xs}px; }
""")


def _tokens() -> dict:
    """给 ``Template.substitute`` 用的扁平 token 表。

    单独抽出来是为了能被测试直接断言（"每个变体都生成了 :disabled"）。
    界面色经 ``_c()`` 取**当前**调色板 —— 样式表每次重装都重新求值。
    """
    return dict(
        bg=_c("BG"), bg_hi=_c("BG_HI"),
        surface=_c("SURFACE"), surface_hi=_c("SURFACE_HI"),
        surface_2=_c("SURFACE_2"), border=_c("BORDER"), track=_c("TRACK"),
        text=_c("TEXT"), text_dim=_c("TEXT_DIM"), text_faint=_c("TEXT_FAINT"),
        accent=_c("ACCENT"), accent_hi=_c("ACCENT_HI"),
        success=_c("SUCCESS"), danger=_c("DANGER"), info=_c("INFO"),
        overlay=_c("OVERLAY"),
        font_ui=resolve_family(mono=False), font_mono=resolve_family(mono=True),
        s_xs=SIZE_XS, s_sm=SIZE_SM, s_md=SIZE_MD, s_lg=SIZE_LG, s_xl=SIZE_XL,
        sp_lg=SPACE_LG,
        r_sm=RADIUS_SM, r_md=RADIUS_MD, r_lg=RADIUS_LG,
    )


def button_variants() -> tuple:
    """已定义的按钮变体名（``QPushButton[variant=...]`` 的选择器集合）。

    ``_button_variants()`` 是**按当前调色板现算**的，所以变体名不能做成模块
    级常量；对外给一个只读访问器，让 ``tools/gui_smoke.py`` 能断言"界面上每个
    按钮的 variant 都真的落到过某条规则上" —— 拼错一个变体名，按钮会静默地
    没有任何样式（四态全丢），这是纯界面测试看不出来的。
    """
    return tuple(_button_variants())


def _button_rules() -> str:
    out = []
    for name, kw in _button_variants().items():
        out.append(_BTN.substitute(
            v=name, r=RADIUS_SM, fs=SIZE_MD, pad=SPACE_LG,
            border="none", accent=_c("ACCENT"),
            dis_bg=_c("SURFACE"), dis_fg=_c("TEXT_FAINT"),
            font_ui=resolve_family(), **kw))
    return "".join(out)


def _card_rules() -> str:
    out = [_CARD_SUB.substitute(text_dim=_c("TEXT_DIM"), s_xs=SIZE_XS)]
    for tone, kw in _card_tones().items():
        out.append(_CARD.substitute(
            t=tone, r=RADIUS_MD, fs=SIZE_MD, accent=_c("ACCENT"),
            surface=_c("SURFACE"), hover=_c("SURFACE_2"),
            press=_c("SURFACE_HI"), border=_c("BORDER"),
            font_ui=resolve_family(), **kw))
    return "".join(out)


def app_stylesheet() -> str:
    """完整样式表。必须在 ``QApplication`` 之后调用（要用 QFontDatabase）。"""
    return _BASE.substitute(_tokens()) + _button_rules() + _card_rules()


# ==================== 安装 ====================
_installed = False


def install() -> None:
    """幂等地把主题装到 QApplication 上。必须在构造任何控件之前调用。

    调用点是 ``GomokuGame.__init__`` 的第一行而**不是** ``main()``：无头冒烟
    测试自己构造 QApplication 后直接 ``M.GomokuGame()``，不走 ``main()``。
    装在这里，离屏测试才会真的执行这套 QSS —— 否则测试里的控件全是无样式
    状态，"样式回归"在 CI 上等于没测。

    首次安装时从 QSettings 恢复上次选择的主题（无记录 / 记录非法则回深色）。
    但**本进程里显式 set_theme 过就不再恢复**：落盘的偏好是"上一次用户想要
    的颜色"，而显式调用是"这一次程序要的颜色"，后者赢。少了这条，测试会依赖于
    开发者机器上恰好存着什么主题 —— 一个把主题设成浅色的人跑测试就会红，
    而失败点与他改的代码毫无关系。

    ``app.setFont`` 与 QSS ``font-family`` 的优先级：**QSS 胜**。所以两者必须
    出自同一个 ``resolve_family()``，否则会出现"某些控件是雅黑、某些是
    DejaVu"的割裂。历史上 ``QFont("Microsoft YaHei", 10)`` 对每一个写了
    ``font-family`` 的控件都无效。
    """
    global _current, _installed
    app = QApplication.instance()
    if app is None:
        raise RuntimeError("theme.install() 必须在 QApplication 之后调用")
    if not _installed and not _explicit:
        saved = QSettings(_SETTINGS_ORG, _SETTINGS_APP).value(_SETTINGS_KEY, "dark")
        _current = saved if saved in _PALETTES else "dark"
    app.setStyleSheet(app_stylesheet())
    f = QFont()
    f.setFamily(resolve_family().split(",")[0].strip().strip("'"))
    f.setPixelSize(SIZE_SM)          # 与 QSS 同单位，避免 pt/px 混算
    app.setFont(f)
    _installed = True


def set_variant(w, name: str) -> None:
    """给按钮设 ``variant`` 动态属性并**重刷**样式。

    Qt 不会因为 ``setProperty`` 自动重算样式 —— 构造期设置不需要重刷，
    运行期改就必须走 ``unpolish``/``polish``，否则属性变了外观不变。
    """
    w.setProperty("variant", name)
    w.style().unpolish(w)
    w.style().polish(w)
    w.update()


def set_tone(w, tone: str) -> None:
    """同上，用于选择卡片的 ``tone``。"""
    w.setProperty("tone", tone)
    w.style().unpolish(w)
    w.style().polish(w)
    w.update()

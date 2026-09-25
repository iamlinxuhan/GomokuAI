# -*- coding: utf-8 -*-
"""棋盘的离屏渲染：木纹、棋子 sprite、两层缓存。

## 为什么单独一个模块

这里是**纯函数**：``(几何, 尺寸, DPR, 棋盘) -> QPixmap``，不持有 widget、不读
全局状态、不认识 ``BoardWidget``。好处是能被无头测试直接调用（"棋盘中心像素
是不是木色"这类断言不必构造整个窗口），而且 ``main.py`` 可以继续只做组装。

放在 ``theme.py`` 里不合适 —— 那个模块的职责是"样式定义的唯一点"，而这里是
像素生成，要 numpy 和几十行绘制代码。放在 ``main.py`` 里则会让它重新长回
"什么都往里塞"的样子。

## 缓存的分层依据

棋盘的绘制成本分三档，缓存键也因此不同：

* **木纹纹理** —— 768x768 逐像素算出来的，最贵（首次约 15-30ms）。但它只
  取决于调色板，**全进程一张**，resize 时拉伸复用而不是重算。
* **静态层**（木盘 + 木纹 + 内嵌面 + 网格 + 星位 + 坐标）—— 只在几何或 DPR
  变化时重建。每步棋都不变。
* **棋子层** —— 每步棋都要重建，但每颗子只是一次 ``drawPixmap``。
* **最后一手环 / 悬停幽灵** —— 不缓存，每帧现画两个小图形。

这样 ``paintEvent`` 退化成两次 ``drawPixmap``；而 Qt 会按 ``event.rect()``
裁剪，所以"鼠标跨格只重绘两个格子"不需要任何特判代码。
"""

from __future__ import annotations

import numpy as np
from PyQt5.QtCore import QPointF, QRectF, Qt
from PyQt5.QtGui import (QBrush, QColor, QFontMetricsF, QImage, QLinearGradient,
                         QPainter, QPainterPath, QPen, QPixmap, QRadialGradient)

import theme
from gamelog import col_letter

# 木纹纹理边长。768 足够铺满 4K 下的棋盘面（拉伸而非平铺），再大只是徒增
# 首帧耗时。
_TEX_N = 768

# 棋子 sprite 画布相对棋子直径的倍数。多出来的边距是留给接触投影的 ——
# 投影向下偏移并略微外扩，不留边距会被裁掉，棋子看着像"飘"在木面上。
_SPRITE_SCALE = 1.6

_tex_cache: QPixmap | None = None
_sprite_cache: dict = {}


def _rgb(hexstr: str) -> tuple:
    h = hexstr.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _box_blur(a: np.ndarray, r: int) -> np.ndarray:
    """分离式盒式模糊。用累积和实现，每轴 O(n)，与半径无关。

    木纹里的噪点必须糊过 —— 逐像素白噪声在木色上看起来是砂纸，不是木头。
    """
    k = 2 * r + 1
    for axis in (0, 1):
        pad = [(0, 0), (0, 0)]
        pad[axis] = (r, r)
        p = np.pad(a, pad, mode="edge")
        c = np.cumsum(p, axis=axis)
        zero_shape = list(c.shape)
        zero_shape[axis] = 1
        c = np.concatenate([np.zeros(zero_shape, dtype=c.dtype), c], axis=axis)
        n = a.shape[axis]
        hi = [slice(None)] * 2
        lo = [slice(None)] * 2
        hi[axis] = slice(k, k + n)
        lo[axis] = slice(0, n)
        a = (c[tuple(hi)] - c[tuple(lo)]) / k
    return a


def wood_texture() -> QPixmap:
    """程序化木纹，**全进程只生成一次**。

    有两件事是刻意的：

    * **不用贴图资源。** README 里那张 ``input.png`` 是 1024x1024 的整幅效果
      图，不是可平铺的材质，切一块出来会有明显的重复与接缝。
    * **不在 resize 时重算。** 纹理只与调色板有关，与控件尺寸无关；绘制时拉
      伸即可。这样拖拽窗口不会掉帧。
    """
    global _tex_cache
    if _tex_cache is None:
        _tex_cache = _build_wood()
    return _tex_cache


def _build_wood() -> QPixmap:
    n = _TEX_N
    # 固定种子：木纹每次运行都一样。随机种子会让同一个版本在不同机器上
    # 长得不同，截图比对就没法看了。
    rng = np.random.default_rng(20240607)
    yy, xx = np.mgrid[0:n, 0:n].astype(np.float32)

    # 竖向木纹，条带本身是弯的（两个不同周期的正弦叠加），否则是"斑马线"。
    warp = 18.0 * np.sin(xx / 120.0) + 9.0 * np.sin(xx / 37.0 + 1.7)
    grain = np.sin((yy + warp) / 7.5) ** 2 * 0.55      # 主木纹
    fine = np.sin((yy + warp) / 2.3) * 0.18            # 细纹
    noise = _box_blur(rng.normal(0, 1, (n, n)).astype(np.float32), 9) * 0.10

    tex = np.clip(0.5 + 0.5 * (grain + fine + noise), 0.0, 1.0)

    # 光照梯度：光源在左上，压到 ±10% 以内。**这个压缩是有原因的** —— 参考图
    # 那种戏剧性光照下，最暗角落任何墨色都达不到 WCAG（坐标色只剩 3.17:1，
    # 需 4.5）。压缩之后光泽与木纹都还在，而全部墨色在最差角落都有正余量。
    lum = ((n - 1 - xx) + (n - 1 - yy)) / (2.0 * (n - 1))   # 左上 1.0，右下 0.0
    t = np.clip(0.5 * lum + 0.5 * tex, 0.0, 1.0)[..., None]

    dark = np.array(_rgb(theme.WOOD_DARK), np.float32)
    lite = np.array(_rgb(theme.WOOD_LITE), np.float32)
    rgb = (dark * (1.0 - t) + lite * t).astype(np.uint8)

    # QImage 不持有这个 buffer —— 不 .copy() 的话 numpy 数组被回收后，
    # 后续绘制读的是野内存。
    img = QImage(rgb.tobytes(), n, n, 3 * n, QImage.Format_RGB888).copy()
    return QPixmap.fromImage(img)


def stone_sprite(player: int, d: float, dpr: float) -> QPixmap:
    """一颗棋子的 sprite（含镜面高光与接触投影），按 ``(颜色, 直径, DPR)`` 缓存。

    旧实现每颗子、每次重绘都新建一个 ``QRadialGradient`` —— 361 颗子乘以
    鼠标每移动一次，是每秒重建上千个渐变对象。高光方向是固定的（光源恒在
    左上），所以 sprite 可以复用。
    """
    key = (player, round(d, 1), round(dpr, 2))
    hit = _sprite_cache.get(key)
    if hit is not None:
        return hit

    s = d * _SPRITE_SCALE
    pm = QPixmap(max(1, int(round(s * dpr))), max(1, int(round(s * dpr))))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)

    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.translate(s / 2.0, s / 2.0)
    r = d / 2.0

    # 接触投影：两层向下偏移的低透明度椭圆。这是"压在木面上"的重量感来源 ——
    # 没有它棋子看起来是贴上去的贴纸。颜色是 theme 的 rgb token（不是 hex
    # 字面量 —— 颜色守卫只认 hex，但语义上仍归 theme 管）。
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(*theme.STONE_SHADOW_NEAR))
    p.drawEllipse(QPointF(0.0, r * 0.16), r * 0.98, r * 0.98)
    p.setBrush(QColor(*theme.STONE_SHADOW_FAR))
    p.drawEllipse(QPointF(0.0, r * 0.27), r * 0.86, r * 0.86)

    if player == 1:
        stops = theme.STONE_B_GRAD
        rim = theme.STONE_B_RIM
    else:
        stops = theme.STONE_W_GRAD
        # 白子的描边**是必需的，不是装饰**：奶白与亮角木色之间只有 1.84:1，
        # 白子放在浅木上本来就靠边缘和阴影区分。
        rim = theme.STONE_W_RIM

    grad = QRadialGradient(QPointF(-r * 0.32, -r * 0.34), r * 1.45)
    for pos, col in stops:
        grad.setColorAt(pos, QColor(col))
    p.setBrush(QBrush(grad))
    p.setPen(QPen(QColor(rim), max(1.0, r * 0.11)))
    p.drawEllipse(QPointF(0.0, 0.0), r, r)
    p.end()

    _sprite_cache[key] = pm
    return pm


# ------------------------------------------------------------------ 品牌标记

# 标记上的网格线数（4 条 = 3 格）。**不是 19**：19 条线缩到一百来像素，
# 线距不足 4px，会糊成一块灰。标记要的是"一眼认出是棋盘"，不是"能下棋"。
_BRAND_LINES = 4

_brand_cache: dict = {}


def brand_mark(size: float, dpr: float) -> QPixmap:
    """应用标记：一块圆角木牌 + 网格 + 两子（黑先、白应）。

    材质与真棋盘**同源** —— 木纹来自 ``wood_texture()``，棋子来自
    ``stone_sprite()``。所以标记不是"另画一个图标"，而是棋盘本身的一个
    缩影；改了棋盘配色，标记跟着改，不会出现两套木色。

    ``size`` 是木牌的边长（不含投影余量），画布会额外留出阴影空间。
    """
    key = (round(size, 1), round(dpr, 2))
    hit = _brand_cache.get(key)
    if hit is not None:
        return hit

    # 画布外扩：下方与右侧要给投影留位置，否则阴影会被裁成硬边。
    pad = size * 0.10
    canvas = size + 2 * pad
    pm = _new_layer(canvas, canvas, dpr)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.SmoothPixmapTransform, True)

    tile = QRectF(pad, pad, size, size)
    # 圆角随尺寸走，但要留在设计系统的档位附近：104px 时正好是 RADIUS_LG。
    r = theme.RADIUS_LG * max(0.55, min(1.6, size / 104.0))
    path = QPainterPath()
    path.addRoundedRect(tile, r, r)

    # ① 投影：木牌"压在"页面底上。深色主题下几乎看不见，但浅色主题下没有
    #    它，白底上的木牌会像贴纸。三层递减，成本可忽略。
    p.setPen(Qt.NoPen)
    for i, alpha in ((3.0, 26), (2.0, 34), (1.0, 44)):
        p.setBrush(QColor(0, 0, 0, alpha))
        p.drawRoundedRect(tile.adjusted(-i * 0.5, i * 0.6,
                                        i * 0.5, i * 1.6), r, r)

    # ② 木纹铺满圆角内（拉伸复用全进程那一张，不重算）
    p.save()
    p.setClipPath(path)
    tex = wood_texture()
    p.drawPixmap(tile, tex, QRectF(0.0, 0.0, float(tex.width()),
                                   float(tex.height())))
    p.restore()

    # ③ 外描边
    line_w = max(1.0, size * 0.011)
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(QColor(theme.WOOD_EDGE), line_w))
    p.drawRoundedRect(tile.adjusted(line_w / 2, line_w / 2,
                                    -line_w / 2, -line_w / 2), r, r)

    # ④ 网格。内缩量取边长两成 —— 够给棋子留出与木边之间的呼吸。
    inset = size * 0.20
    span = size - 2 * inset
    step = span / (_BRAND_LINES - 1)
    gx, gy = tile.x() + inset, tile.y() + inset
    p.setPen(QPen(QColor(theme.GRID), line_w))
    for i in range(_BRAND_LINES):
        p.drawLine(QPointF(gx + i * step, gy),
                   QPointF(gx + i * step, gy + span))
        p.drawLine(QPointF(gx, gy + i * step),
                   QPointF(gx + span, gy + i * step))

    # ⑤ 两子：黑占中偏左上，白应于右下 —— 与开局前两手同形。
    d = step * 0.94
    half = d * _SPRITE_SCALE / 2.0
    for player, i in ((1, 1), (2, 2)):
        sprite = stone_sprite(player, d, dpr)
        p.drawPixmap(QPointF(gx + i * step - half, gy + i * step - half),
                     sprite)

    p.end()
    _brand_cache[key] = pm
    return pm


# ------------------------------------------------------------------ 图层

def _new_layer(w: float, h: float, dpr: float) -> QPixmap:
    """ARGB32_Premultiplied 是必须的 —— 默认格式下圆角外的透明区会画成黑角。"""
    pm = QPixmap(max(1, int(round(w * dpr))), max(1, int(round(h * dpr))))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    return pm


def render_static(geom, w: float, h: float, dpr: float) -> QPixmap:
    """静态层：木盘 → 木纹 → 内嵌棋盘面 → 外描边 → 网格 → 星位 → 坐标。"""
    pm = _new_layer(w, h, dpr)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.SmoothPixmapTransform, True)

    # 木盘是**正方形**，不等于控件矩形 —— 控件非正方时木纹会被拉成条纹、
    # 圆角会变成椭圆。详见 BoardGeometry.plate_rect。
    px_, py_, side = geom.plate_rect(w, h)
    plate = QRectF(px_, py_, side, side)
    # 圆角随缩放走（有上下限，免得小棋盘上圆得像药丸、大棋盘上又像直角）。
    plate_r = theme.RADIUS_LG * max(0.6, min(1.4, geom.k))
    path = QPainterPath()
    path.addRoundedRect(plate, plate_r, plate_r)

    # ① 木纹铺满木盘（裁剪在圆角内）
    p.save()
    p.setClipPath(path)
    tex = wood_texture()
    p.drawPixmap(plate, tex, QRectF(0.0, 0.0, float(tex.width()),
                                    float(tex.height())))
    # ①' 木盘内的光照收边：顶缘一线提亮、底缘一线压暗（都裁在圆角内）。
    # 木盘铺满整个短边，外投影没有空间，"厚度"只能靠内缘明暗来暗示。
    edge = max(6.0, side * 0.025)
    top = QLinearGradient(plate.x(), plate.y(), plate.x(), plate.y() + edge)
    top.setColorAt(0.0, QColor(*theme.PLATE_TOP_TINT))
    top.setColorAt(1.0, QColor(*theme.PLATE_TOP_TINT[:3], 0))
    p.fillRect(QRectF(plate.x(), plate.y(), side, edge), QBrush(top))
    bot = QLinearGradient(plate.x(), plate.y() + side - edge,
                          plate.x(), plate.y() + side)
    bot.setColorAt(0.0, QColor(*theme.PLATE_BOT_FADE))
    bot.setColorAt(1.0, QColor(*theme.PLATE_BOT_SHADOW))
    p.fillRect(QRectF(plate.x(), plate.y() + side - edge, side, edge),
               QBrush(bot))
    p.restore()

    gx, gy, span, _ = geom.rect()
    line_w = max(1.0, 1.5 * geom.k)

    # ② 内嵌棋盘面：略亮的凹面，制造"棋盘是嵌进木框里的"层次
    face_pad = geom.cell * 0.62
    face = QRectF(gx - face_pad, gy - face_pad,
                  span + 2 * face_pad, span + 2 * face_pad)
    face_r = theme.RADIUS_SM * geom.k
    p.setBrush(QColor(*theme.FACE_TINT))
    p.setPen(QPen(QColor(theme.WOOD_EDGE), line_w))
    p.drawRoundedRect(face, face_r, face_r)

    # ③ 木盘外描边
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(QColor(theme.WOOD_EDGE), line_w))
    p.drawRoundedRect(plate.adjusted(line_w / 2, line_w / 2,
                                     -line_w / 2, -line_w / 2),
                      plate_r, plate_r)

    # ④ 网格：19 个格点 = 18 段
    p.setPen(QPen(QColor(theme.GRID), line_w))
    for i in range(geom.n):
        x = gx + i * geom.cell
        y = gy + i * geom.cell
        p.drawLine(QPointF(x, gy), QPointF(x, gy + span))
        p.drawLine(QPointF(gx, y), QPointF(gx + span, y))

    # ⑤ 星位
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(theme.STAR))
    star_r = max(1.8, geom.cell * 0.11)
    for r, c in _star_points(geom.n):
        x, y = geom.px(r, c)
        p.drawEllipse(QPointF(x, y), star_r, star_r)

    # ⑥ 坐标标注
    _draw_coords(p, geom)

    p.end()
    return pm


def _star_points(n: int):
    """19 路是传统九星；其他尺寸取等距三点网格（棋盘尺寸若将来可变）。"""
    if n == 19:
        return [(3, 3), (3, 9), (3, 15), (9, 3), (9, 9), (9, 15),
                (15, 3), (15, 9), (15, 15)]
    a, b = 3, n - 4
    m = n // 2
    return [(r, c) for r in (a, m, b) for c in (a, m, b)]


def _draw_coords(p: QPainter, geom) -> None:
    """列字母（跳 I）与行号。

    字母来自 ``gamelog.col_letter``，**不再自己写 ``chr(65 + i)``** —— 那份
    不跳 I 的写法会让 19 列里有 11 列对不上日志／棋谱／题库。

    定位一律用 ``QFontMetricsF`` 按**墨迹**算，不用 ``drawText`` 的对齐标志。
    三轮修下来的账（设计基准，cell=34）：

    * **一代：全都 ``AlignCenter``，框宽 ``band`` / ``band*1.4``。** 字在框里
      居中，实际留白 = ``gap`` + 半格空框，而空框尺寸与字形无关 —— 于是"到
      网格的距离"由框决定，两个方向还因框的大小不同而不相等：**行号 17.0 px、
      字母 12.8 px**。这就是"行号偏左、字母偏上"。
    * **二代：改成贴着网格那条边对齐**（行号 ``AlignRight``、字母
      ``AlignBottom``）。留白不再由框决定，但两个方向仍差 2.4 px（字母 9.8、
      行号 7.4）—— 因为这两个标志对的是字体的**包围盒**：``AlignBottom`` 把
      下伸部分留成空白，``AlignRight`` 把字侧边距留成空白，两份空白不相等。
      同一代还暴露了等宽字体的老问题：``"1"`` 字形窄、占位与别的数字同宽，
      按占位右对齐时它的墨迹右沿离网格只有 **1 px**，而 ``"2"``~``"9"`` 在
      8 px —— 整列行号看不出对齐。
    * **三代（现在）：直接算墨迹框。** 行号以**墨迹**右沿贴 ``gap``，1 位与
      2 位共用同一条右边线；字母以**基线**统一（不用逐字墨迹下沿，否则 ``Q``
      带下伸的尾巴会把自己抬高，反而参差）。于是两个方向的留白由同一个
      ``gap`` 定义、天然相等，且与字体、字号、缩放比都无关。
    """
    font = theme.mono_font(max(9, int(round(12 * geom.k))))
    p.setFont(font)
    p.setPen(QColor(theme.COORD))

    cell = geom.cell
    gap = cell * 0.18            # 网格线到标注**墨迹**的呼吸
    gx, gy, span, _ = geom.rect()

    fm = QFontMetricsF(font)
    # 竖直居中的基准：用全体数字的墨迹框，而不是每个串各自的框 —— 否则
    # "1" 与 "19" 的墨迹高度若不同就会有不同的基线，整列行号会上下跳。
    d = fm.boundingRect("0123456789")
    digit_cy = (d.top() + d.bottom()) / 2.0

    for i in range(geom.n):
        ch = col_letter(i)
        br = fm.boundingRect(ch)
        x = gx + i * cell
        p.drawText(QPointF(x - (br.left() + br.right()) / 2.0, gy - gap), ch)

        t = str(i + 1)
        bt = fm.boundingRect(t)
        y = gy + i * cell
        p.drawText(QPointF(gx - gap - bt.right(), y - digit_cy), t)


def render_stones(geom, w: float, h: float, dpr: float, board,
                  exclude=None) -> QPixmap:
    """棋子层。每步棋重建一次，每颗子只是一次 drawPixmap。

    ``exclude=(r, c)``：从缓存层里**剔除**这一格 —— 落子动画期间，正在动画
    的那颗子由 ``BoardWidget.paintEvent`` 覆盖绘制（带位移与淡入），缓存层里
    不能再有它的静态副本，否则动画过程中终点处始终叠着一颗"已到位"的子。
    """
    pm = _new_layer(w, h, dpr)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)

    d = geom.stone_radius * 2.0
    half = d * _SPRITE_SCALE / 2.0
    for player in (1, 2):
        # np.argwhere 给的是 numpy 标量；直接参与几何运算会把 numpy 类型带进
        # 后续计算（Phase 2 踩过这个坑），所以显式转 int。
        cells = np.argwhere(board == player)
        if not len(cells):
            continue
        sprite = stone_sprite(player, d, dpr)
        for rr, cc in cells:
            if exclude is not None and (int(rr), int(cc)) == tuple(exclude):
                continue
            x, y = geom.px(int(rr), int(cc))
            p.drawPixmap(QPointF(x - half, y - half), sprite)

    p.end()
    return pm

# -*- coding: utf-8 -*-
"""颜色字面量只允许出现在 ``theme.py`` 里。

## 这条测试防的是什么

重构前 ``main.py`` 有 36 处 ``setStyleSheet``，颜色字面量散落其间
（``#cdd6f4`` ×6、``#a6adc8`` ×5、``#45475a`` ×3…），而模块顶部另外声明了
7 个"调色板"常量（``COLOR_LINE`` / ``COLOR_BLACK`` / ``COLOR_HIGHLIGHT``…）
**从未被使用** —— 绘制处一律写字面量。于是"声明的调色板"与"实际用的颜色"
是两套，改一处不会报错，也不会生效。

这不是洁癖。同一轮重构里真实发生过的事：颜色选择页的标题落在白底上只有
**2.02:1**（AA 小字要求 4.5:1 的 45%），白卡片在窗口底上只有 **1.04:1**，
而全文件 36 处样式表里**零个** ``:disabled`` 规则 —— AI 思考期间被禁用的悔棋
按钮外观完全正常。散落的字面量让这些问题既看不见、也没法一次改掉。

## 允许清单

``theme.py`` 是唯一定义点。其余 UI 模块一律引用它。

用正则而非 AST：Qt 样式表是**字符串里的 CSS**，AST 看不见里面的 ``#rrggbb``，
而那正是历史上出问题的地方。扫描时剔除注释与文档字符串会引入解析复杂度，
所以这里选择"注释里也不要写颜色字面量" —— 让规则简单到不会出错，注释改用
``theme.XXX`` 的名字来指代颜色。
"""

from __future__ import annotations

import os
import re

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 唯一允许定义颜色的模块
_DEFINITION_SITE = "theme.py"

# UI 层：这些文件里出现任何 #rrggbb / #rgb 都算违规
_GUARDED = (
    "main.py",
    "ui_kit.py",
    "board_render.py",
    "anim.py",
    "charts.py",     # 新增的绘制模块 —— 不列进来它就会成为全 UI 唯一
                     # 允许写 hex 的地方，正是本测试要防的那种回归
)

# #abc / #aabbcc / #aabbccdd
_HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")


def _scan(filename: str):
    path = os.path.join(_ROOT, filename)
    with open(path, encoding="utf-8") as f:
        hits = []
        for lineno, line in enumerate(f, 1):
            for m in _HEX.finditer(line):
                hits.append((lineno, m.group(0), line.strip()))
        return hits


@pytest.mark.parametrize("filename", _GUARDED)
def test_no_color_literals(filename):
    hits = _scan(filename)
    if hits:
        detail = "\n".join(f"  {filename}:{n}  {lit}  ->  {src}"
                           for n, lit, src in hits)
        pytest.fail(
            f"{filename} 里有 {len(hits)} 处颜色字面量。"
            f"把它们移到 {_DEFINITION_SITE} 并在这里引用：\n{detail}")


def test_definition_site_exists_and_has_colors():
    """反向断言：守卫不是靠"主题文件恰好为空"通过的。"""
    hits = _scan(_DEFINITION_SITE)
    assert len(hits) >= 20, (
        f"{_DEFINITION_SITE} 里只有 {len(hits)} 个颜色字面量 —— "
        "要么调色板被搬走了，要么扫描规则失效了")

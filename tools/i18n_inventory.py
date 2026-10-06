# -*- coding: utf-8 -*-
"""从**跑得起来的界面**里收出待翻译文案，而不是靠人肉通读源码。

## 为什么这样做

``i18n.t()`` 查不到就原样返回中文，并把原文记进 ``i18n._misses``。于是只要把
语言切成英语（表是空的，于是**每一条**都会落空），把界面完整走一遍，落空清单
就是"用户真的看得见的那些中文"。

这比逐文件 grep 靠谱的地方在于：源码里躺着的断言消息（``theme.install() 必须在
QApplication 之后调用``）、日志模板（``  难度: ``）、QSS 块、字体名、文件名，
只要没有哪条路把它们送进 QLabel，就**不会**出现在清单里。反过来，把一串片段
在运行期拼起来的（``"第 %d 手   %s"``）也照样会被抓到 —— 那正是 grep 最容易
漏的一类。

## 它抓不到什么

只走**当前这条路**上的界面。错误分支（连不上房间时那几句）、取消分支、以及
任何需要真实对局才能到达的页面，冒烟测试覆盖不到的部分就不会出现在清单里。
所以最终的翻译表 = 本工具的输出 + 一次按文件的人工补扫（``ui_kit`` /
``charts`` / ``theme`` 的标签由工厂统一翻，另算）。

用法::

    .venv/bin/python tools/i18n_inventory.py            # 打印清单
    .venv/bin/python tools/i18n_inventory.py -o out.json
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
from contextlib import redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import i18n  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="收集界面待翻译文案")
    ap.add_argument("-o", "--out", help="写到这个 JSON 文件（默认打印到 stdout）")
    ap.add_argument("--lang", default="en", help="用哪种语言跑（默认 en）")
    args = ap.parse_args()

    import gui_smoke as G
    import main as M

    # 先把"读取落盘语言"这一步摘掉：本工具自己要定语言，而每个 GomokuGame
    # 构造时都会去读 QSettings（读不到就跟随系统）。不摘的话，语言会在每个
    # 新窗口里被重置回去，清单也就跟着断掉。
    M._load_language = lambda: None
    i18n.set_language(args.lang)
    i18n.enable_miss_log()

    # 界面自己会往 stdout 打一堆 [ ok ] / [ FAIL ]。那些在盘点时全是噪音
    # （文本既然翻了，按中文原文写的断言当然会失败），吞掉。
    buf = io.StringIO()
    argv = sys.argv
    try:
        with redirect_stdout(buf):
            # ``G.main()`` 自己会 parse_args，本工具的 ``-o`` 会把它喂崩 ——
            # 跑它之前把命令行清干净。
            sys.argv = argv[:1]
            G.main()
    except SystemExit:
        pass
    finally:
        sys.argv = argv

    misses = i18n.miss_log()
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(misses, f, ensure_ascii=False, indent=1)
        print(f"共 {len(misses)} 条 -> {args.out}")
    else:
        for s in misses:
            print(s)
        print(f"--- 共 {len(misses)} 条", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

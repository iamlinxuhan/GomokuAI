# -*- coding: utf-8 -*-
"""把 Gomocup 2026 官方开局集写成 ``data/gomocup_2026.json``。

**数据来源是官方页面，不是第三方转录。** 2026-11-13 在
<https://live.gomocup.org/openings> 上核对时发现：原先打算采用的 Rust crate
``figrid-board`` 的 ``src/book.rs`` 里那 24 条"2026 官方开局"与官方 PNG
（``gomocup.org/static/tournaments/2026/openings/``）以及该页面的文字坐标
**一条都对不上**——不是旋转、镜像或平移能补上的差别（四条抽样逐条比过）。
官方图与官方页彼此一致，所以这里以官方页为准重写。

官方坐标是**相对盘心**的 ``(x, y)``，x 向右、y 向下为正（把 PNG 里第 1 条
freestyle15 的 ``4,6 / 1,5 / 0,3`` 与图上三颗子的位置逐一对过）。本程序内部
用 15×15 的绝对坐标，于是 ``col = x + 7``、``row = y + 7``；其余尺寸的平移
仍由 ``gomocup_book`` / ``opening_book`` 在运行时按盘心完成。

这里只收 **freestyle15 与 standard** 两组各 12 条：它们是 15×15 上与本程序
「无禁手」变体同口径的两套规则；caro / renju 的规则不同（禁手、恰好五连），
禁手变体本来就不查开局书，收进来只会让书在错误规则下出手。

**应手（response）不是官方数据。** 官方只公布开局着法；每条线的应手由本项目
自己的 C++ 引擎在该局面下搜出来（``--responses``，默认每手 10 秒），并写进
JSON。这样这本书在"官方开局摆好、轮到我方"时给的是一个真正搜过的着法，而
不是从别处抄来的、无法复核的一手。

用法::

    uv run --frozen python tools/build_gomocup_json.py            # 只重写坐标
    uv run --frozen python tools/build_gomocup_json.py --responses # 连应手一起重算
"""

from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

OUT = os.path.join(ROOT, "data", "gomocup_2026.json")

SOURCE = "https://live.gomocup.org/openings"
RETRIEVED = "2026-11-13"

#: 官方页的 ``Opening #N``，坐标相对盘心（x 向右、y 向下）。
OFFICIAL = {
    "freestyle15": {
        1: [(4, 6), (1, 5), (0, 3)],
        2: [(-1, 6), (-1, 3), (-3, 1), (-6, 1), (-4, 5)],
        3: [(0, 0), (1, -1), (3, 2), (-1, -1), (3, -1)],
        4: [(0, 0), (0, -1), (1, -3), (2, -1), (2, 1)],
        5: [(0, 0), (0, -1), (0, -3), (1, -3), (2, -1)],
        6: [(0, 0), (0, -1), (3, -3), (2, -2), (0, -2)],
        7: [(6, 6), (4, 4), (6, 2), (2, 4), (3, 2), (5, 4), (3, 4)],
        8: [(0, 0), (0, -1), (3, -1), (1, 1), (-2, -1)],
        9: [(5, 5), (6, 4), (7, 3), (7, 2), (6, 1), (5, 0), (4, 1),
            (3, 3), (3, 4), (3, 1), (1, 2)],
        10: [(0, 0), (1, -1), (3, -2), (3, -1), (4, 2)],
        11: [(0, 0), (1, -1), (3, 1), (1, 1), (-2, 0)],
        12: [(0, 0), (1, -1), (3, 0), (2, 1), (-1, 2)],
    },
    "standard": {
        1: [(6, 6), (4, 4), (6, 2), (2, 4), (3, 2), (5, 4), (3, 4)],
        2: [(5, 5), (6, 4), (7, 3), (7, 2), (6, 1), (5, 0), (4, 1),
            (3, 3), (3, 4), (3, 1), (1, 2)],
        3: [(-5, 5), (-5, 4), (-5, 3), (-4, 2), (-2, 2)],
        4: [(6, 6), (5, 5), (6, 4), (5, 3), (6, 2), (5, 1)],
        5: [(-4, 5), (-2, 0), (-2, 2), (-2, 1), (-4, 2), (-3, 2)],
        6: [(-5, 5), (-5, 4), (-4, 3), (-2, 2), (-1, 3)],
        7: [(-5, 5), (-4, 4), (-3, 2), (-2, 3), (-1, 2)],
        8: [(-1, 6), (-1, 3), (-3, 1), (-6, 1), (-4, 5)],
        9: [(4, -5), (4, -4), (5, -1), (3, -1), (2, -2)],
        10: [(-5, 5), (-4, 4), (-2, 4), (-2, 3), (-3, 1)],
        11: [(-5, -3), (-3, -4), (-5, 0), (1, -3), (-2, 1),
             (-2, -4), (-5, -2), (-5, -1), (-4, -1), (-3, 0)],
        12: [(-5, 5), (-5, 2), (-1, 2)],
    },
}

NATIVE_CENTER = 7


def absolute(pts):
    """相对盘心的坐标 → 15×15 绝对坐标。"""
    return [(x + NATIVE_CENTER, y + NATIVE_CENTER) for x, y in pts]


def entries_without_responses():
    out = []
    for group in ("freestyle15", "standard"):
        for n in sorted(OFFICIAL[group]):
            out.append({"name": "%s#%d" % (group, n),
                        "moves": [list(p) for p in absolute(OFFICIAL[group][n])],
                        "response": None})
    return out


def existing_responses():
    """旧 JSON 里已经算好的应手，按 ``(name, moves)`` 认领。"""
    try:
        with open(OUT, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return {}
    got = {}
    for e in doc.get("entries", []):
        resp = e.get("response")
        if resp:
            got[(e["name"], tuple(map(tuple, e["moves"])))] = list(resp)
    return got


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--responses", action="store_true",
                    help="用本项目的 C++ 引擎重算每条线的应手")
    ap.add_argument("--seconds", type=float, default=10.0,
                    help="算每条应手给的时间（秒），默认 10")
    args = ap.parse_args()

    entries = entries_without_responses()
    old = existing_responses()
    for e in entries:
        e["response"] = old.get((e["name"], tuple(map(tuple, e["moves"]))))

    if args.responses:
        import numpy as np
        import config
        import engine

        if config.find_binary("free", 15) is None:
            raise SystemExit("没有 15 路的 C++ 引擎，没法算应手")
        client = engine._ServerClient(variant="free", size=15)
        cfg = dict(time=float(args.seconds), max_depth=24, vcf_budget=3.0,
                   qply=12, enhance=True)
        try:
            for e in entries:
                moves = [tuple(p) for p in e["moves"]]
                board = np.zeros((15, 15), dtype=np.uint8)
                for i, (c, r) in enumerate(moves):
                    board[r][c] = 1 if i % 2 == 0 else 2
                me = 1 if len(moves) % 2 == 0 else 2
                print("  应手 %-14s %2d 手，轮到%s…"
                      % (e["name"], len(moves), "黑" if me == 1 else "白"),
                      flush=True)
                idx, info = client.compute(board, me, cfg, None)
                if idx < 0:
                    raise SystemExit("%s 的终局没有合法着法" % e["name"])
                rc = [idx % 15, idx // 15]
                if rc in e["moves"]:
                    raise SystemExit("%s 的应手落在已有子上" % e["name"])
                e["response"] = rc
                print("      → %s（%s，depth=%s）"
                      % (rc, info.get("reason", "?"), info.get("depth")),
                      flush=True)
        finally:
            client.shutdown()

    missing = [e["name"] for e in entries if not e["response"]]
    if missing:
        raise SystemExit("这些线还没有应手，加 --responses：%s" % ", ".join(missing))

    doc = {
        "source": SOURCE,
        "retrieved": RETRIEVED,
        "note": ("Coordinates are (x=column, y=row) on a 15x15 board. "
                 "moves are the official Gomocup 2026 opening stones, in the "
                 "official order (black first). response is NOT official: it "
                 "is the move GomokuAI's own engine chose on the final "
                 "position."),
        "entries": entries,
    }
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    os.replace(tmp, OUT)
    print("写入 %s（%d 条）" % (OUT, len(entries)))


if __name__ == "__main__":
    main()

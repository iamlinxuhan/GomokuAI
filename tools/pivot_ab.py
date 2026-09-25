# -*- coding: utf-8 -*-
"""量化"中级档时限 5.0 → 7.0"这条改动：把第 4 手这个胜负手单独拎出来做对照。

    # 1. 结局对照 —— 第 4 手分别钉死为 K9 / K7 / K11，其余着法仍用 5.0s 预算
    .venv/bin/python tools/pivot_ab.py --budget 5.0

    # 2. 稳定性 —— 第 4 手在 5.0s 预算下重复搜 12 次，看引擎自己选什么
    .venv/bin/python tools/pivot_ab.py --budget 5.0 --stability 12

背景：`game_log_20260922_235240` 那盘（中级执白、人类第 31 手 J8 胜）的胜负手
在第 4 手，盘面只有三颗子。**第 4 层选 K9，第 5 层选 K7/K11**，相邻两层给出
胜负相反的结论。这个脚本回答两个问题：

* **这一手真的决定成败吗？** —— 模式 1：钉死它，看结局会不会变。会变则说明
  它是全局胜负手，后面 27 手都只是症状。
* **旧预算（5.0s）为什么出错？** —— 模式 2：不改任何东西，只重复搜同一局面。
  若同一份输入给出两种答案，那 5.0s 不是"差一点深度"，而是**压在临界点上**，
  选点在掷硬币 —— 这跟"机器够不够快"绑在一起，正是要避免的性质。

两个模式都不改引擎代码，只改"给它多少时间"和"哪一手钉死"。
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine_local as E  # noqa: E402
import gamelog  # noqa: E402
from tools.analyze_log import name, parse, replay  # noqa: E402

LOG_DEFAULT = "game_log_20260922_235240.txt"


def cell_of(text):
    """``'K7'`` → ``(r, c)``。"""
    text = text.strip().upper()
    return int(text[1:]) - 1, gamelog.COL_LETTERS.index(text[0])


def play_out(moves, pivot, forced, level, budget, player):
    """黑方照日志走、白方由引擎走，第 ``pivot`` 手钉死为 ``forced``。

    日志着法撞到已占格就停下并如实报告 —— 白方一旦偏离日志，日志就不再是
    一盘连贯的棋，硬走下去等于凭空替人类改棋。
    """
    board = np.zeros((E.BOARD_SIZE, E.BOARD_SIZE), dtype=np.uint8)
    trace = []
    for ply, side, logged in moves:
        if side == player:
            if ply == pivot:
                mv, dep, val = forced, None, None
                if board[mv] != 0:
                    return trace, None, f"第 {pivot} 手 {name(forced)} 已被占，无法钉死"
            else:
                E.new_game()
                idx, info = E._ENGINE.think(board, side, level, time_limit=budget)
                mv = divmod(idx, E.BOARD_SIZE)
                dep, val = info["actual_depth"], info["best_val"]
        else:
            if board[logged] != 0:
                return trace, None, (f"第 {ply} 手 黑 {name(logged)} 撞上白子 —— "
                                     f"白方已偏离日志，重放到此为止")
            mv, dep, val = logged, None, None
        board[mv] = side
        trace.append((ply, side, mv, dep, val, ply == pivot))
        if E.check_win(board, side):
            return trace, (ply, side, mv), None
    return trace, None, "日志走完，未分胜负"


def mode_force(moves, args):
    print(f"黑方按日志走；白方由 {args.level} 档引擎走；"
          f"**第 {args.ply} 手钉死**；其余着法预算 {args.budget or E.DIFFICULTY[args.level]['time']}s\n")
    for spec in args.force.split(","):
        forced = cell_of(spec)
        t0 = time.monotonic()
        trace, win, stopped = play_out(moves, args.ply, forced, args.level,
                                       args.budget, args.player)
        print(f"=== 第 {args.ply} 手钉死为 {name(forced)} ===")
        for ply, side, mv, dep, val, pinned in trace:
            who = "黑" if side == 1 else "白"
            tail = ""
            if pinned:
                tail = "   <== 钉死"
            elif val is not None:
                tail = f"   val={val}  dep={dep}"
            print(f"  {ply:3d} {who} {name(mv):>4}{tail}")
        # 与日志逐手对照 —— "这一手是病根"的全部说服力就在这里：钉死败着的那一
        # 条分支应当把原局**完整复现**，而不是只有结局相同。
        off = sum(1 for i, (_p, _s, mv, _d, _v, _x) in enumerate(trace)
                  if i >= len(moves) or moves[i][2] != mv)
        print(f"  与日志不同的手数：{off} / {len(trace)}"
              + ("   ← 原局被完整复现" if off == 0 else ""))
        if stopped:
            print(f"  → {stopped}（{time.monotonic() - t0:.0f}s）\n")
        else:
            who = "黑胜" if win[1] == 1 else "白(AI)胜"
            print(f"  → {who} · 第 {win[0]} 手 {name(win[2])}"
                  f"（{time.monotonic() - t0:.0f}s）\n")


def mode_stability(moves, args):
    """同一份输入、同一个预算，重复搜第 ``pivot`` 手，统计引擎选了什么。"""
    budget = args.budget or E.DIFFICULTY[args.level]["time"]
    board = replay(moves, args.ply - 1)
    print(f"=== {args.level} 档 · 预算 {budget}s · 第 {args.ply} 手重复搜 {args.stability} 次 ===")
    print("（每轮都先复刻完整调用路径：新局 → 前面每一手都重搜一遍）")
    tally = {}
    for k in range(args.stability):
        # 前面每一手也重搜：分析日志的调用路径就是这样，热 TT 是真实对局的状态。
        for ply, side, _ in moves[:args.ply - 1]:
            if side == args.player:
                E.new_game()
                E._ENGINE.think(replay(moves, ply - 1), side, args.level, time_limit=budget)
        E.new_game()
        idx, info = E._ENGINE.think(board, args.player, args.level, time_limit=budget)
        mv = divmod(idx, E.BOARD_SIZE)
        key = (name(mv), info["actual_depth"], info["best_val"])
        tally[key] = tally.get(key, 0) + 1
        print(f"  第{k + 1:2d}轮  {name(mv):>4}  {info['actual_depth']} 层  "
              f"val={info['best_val']:>9}  {info['time_ms']:.0f}ms")
    print()
    for (nm, dep, val), n in sorted(tally.items(), key=lambda kv: -kv[1]):
        print(f"  {nm:>4} · {dep} 层 · val={val:>9}   {n}/{args.stability}")
    if len(tally) > 1:
        print("\n  **同一份输入给出多种答案 —— 这个预算压在临界点上。**")
    else:
        print("\n  只有一种答案。")


def main():
    ap = argparse.ArgumentParser(
        description="把某一手钉死或重复搜，量化时限改动的影响。")
    ap.add_argument("log", nargs="?", default=LOG_DEFAULT,
                    help=f"对局日志（默认 {LOG_DEFAULT}）")
    ap.add_argument("--level", type=int, default=2, help="复盘用的档位（默认 2）")
    ap.add_argument("--player", type=int, default=2, help="由引擎走的一方（默认 2=白）")
    ap.add_argument("--ply", type=int, default=4, help="胜负手所在的着数")
    ap.add_argument("--budget", type=float, default=None,
                    help="覆盖时限（秒）；不传则用该档位的当前值")
    ap.add_argument("--force", default="K9,K7,K11", help="分别钉死的着法，逗号分隔")
    ap.add_argument("--stability", type=int, default=0,
                    help="不做钉死对照，改为把 --ply 那一手重复搜 N 次并统计选点")
    args = ap.parse_args()

    moves = parse(args.log)
    if not moves:
        sys.exit(f"没从 {args.log} 里解析出任何着法")
    if args.stability:
        mode_stability(moves, args)
    else:
        mode_force(moves, args)


if __name__ == "__main__":
    main()

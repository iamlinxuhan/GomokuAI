# -*- coding: utf-8 -*-
"""复盘一份对局日志：逐手重放，找出 AI 从哪里开始输。

    .venv/bin/python tools/analyze_log.py game_log_20260919_183202.txt

做法：把日志里的每手棋重放到棋盘上，然后在**AI 该走的那一手之前**用比对局
当时更强的设置重新搜一遍，看两件事：

1. 当时那手棋是不是最优（重新搜出来的最佳着法与它差多少分）；
2. 从哪一手起，**无论怎么走都已经输了** —— 那才是"病根"，之后的每一手都只是
   症状。

第 2 点用 ``is_mate`` 判定：引擎返回的杀棋分是**已证明**的，不是估值。所以
"白方无论走哪都 -WIN_SCORE"这句话是确定的，而不是"引擎觉得不妙"。
"""

from __future__ import annotations

import argparse
import os
import re
import sys

import numpy as np

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import engine_local as E  # noqa: E402
import gamelog  # noqa: E402

MOVE_RE = re.compile(r"^\s*(\d+) \|\s*([黑白])\S*\s*\|\s*([A-T])(\d+)\s*\|")


def parse(path):
    """返回 ``[(ply, player, (r, c)), ...]``，player 1=黑 2=白。"""
    out = []
    for line in open(path, encoding="utf-8"):
        m = MOVE_RE.match(line)
        if not m:
            continue
        ply = int(m.group(1))
        player = 1 if m.group(2) == "黑" else 2
        c = gamelog.COL_LETTERS.index(m.group(3))
        r = int(m.group(4)) - 1
        out.append((ply, player, (r, c)))
    return out


def replay(moves, upto):
    """重放前 ``upto`` 手，返回棋盘。"""
    b = np.zeros((E.BOARD_SIZE, E.BOARD_SIZE), dtype=np.uint8)
    for _, p, (r, c) in moves[:upto]:
        b[r][c] = p
    return b


def name(cell):
    r, c = cell
    return f"{gamelog.col_letter(c)}{r + 1}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("log")
    ap.add_argument("--level", type=int, default=3, help="复盘用的档位（默认 3）")
    ap.add_argument("--budget", type=float, default=None, help="覆盖时间上限（秒）")
    ap.add_argument("--player", type=int, default=2, help="复盘谁的棋（默认 2=白=AI）")
    args = ap.parse_args()

    moves = parse(args.log)
    print(f"共 {len(moves)} 手；复盘 {'黑' if args.player == 1 else '白'}方\n")

    for i, (ply, player, cell) in enumerate(moves):
        if player != args.player:
            continue
        board = replay(moves, i)
        E.new_game()
        idx, info = E._ENGINE.think(board, player, args.level,
                                    time_limit=args.budget)
        br, bc = divmod(idx, E.BOARD_SIZE)
        best = (br, bc)
        mine = -info["best_val"]

        # 这一手与最佳着法的差距：把最佳着法顶掉、换成实际走的那手，
        # 再搜一层看对手怎么答 —— 差距用"实际着法之后的最佳应对"衡量。
        gap = None
        if best != cell:
            b2 = board.copy()
            b2[cell] = player
            E.new_game()
            _, info2 = E._ENGINE.think(b2, 3 - player, args.level,
                                       time_limit=args.budget)
            gap = (-info2["best_val"]) - mine

        mark = "" if best == cell else "  <-- 有更好的"
        gt = f"  差距 {gap:+d}" if gap is not None else ""
        print(f"{ply:3d} 实走 {name(cell):>4}  最佳 {name(best):>4}  "
              f"val={info['best_val']:>9}  dep={info['actual_depth']}  "
              f"{info['time_ms']:>6.0f}ms  {info['vcf_state']}"
              f"{gt}{mark}")


if __name__ == "__main__":
    main()

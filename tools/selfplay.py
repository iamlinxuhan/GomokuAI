# -*- coding: utf-8 -*-
"""引擎自对弈 A/B 回归台。

用法::

    python tools/selfplay.py --black legacy --white engine --games 20 \
        --black-level 3 --white-level 3 --seed 20260919 --tag phase4

设计要点（与方案一致）：
  * 棋盘统一用 numpy uint8 (19,19)，两代引擎共用同一表示。
  * 随机性全部来自显式随机源：开局选择、双方轮换、重复局面判和。
    引擎自身若确定性（旧引擎如此），则整局可复现。
  * **非法走法 / 崩溃 / 超时次数必须为 0**，任一非零即视为该侧失败。
  * 同一局面出现 3 次判和；225 手判和。
  * 输出 JSON 报告，便于跨阶段比较。

引擎由模块名加载，需暴露 ``ai_move(board, ai_player, depth) -> (r, c, info)``
与 ``check_win(board, player) -> bool``。
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import random
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.positions import reset_engine  # noqa: E402 — 需在 sys.path 之后

BOARD_SIZE = 19
BLACK, WHITE = 1, 2
MAX_MOVES = 225

# 天元 + 8 个标准第二手（含对称对，覆盖不同开局风格）
OPENING_SECOND_MOVES = [
    (8, 8), (8, 9), (8, 10), (8, 11),
    (9, 8), (9, 10),
    (10, 9), (10, 10),
]
CENTER = (9, 9)


class IllegalMove(Exception):
    pass


def load_engine(name):
    """按模块名加载引擎；'-' / 'none' 返回 None。"""
    if name in (None, "-", "none"):
        return None
    if name in ("legacy", "tools.legacy_engine"):
        mod = importlib.import_module("tools.legacy_engine")
    elif name in ("engine", "engine_local",
                  "GomokuAI.engine", "GomokuAI.engine_local"):
        # ``engine`` 在重构后是 TCP 客户端（C++ 服务端的门面）。基准与自对弈
        # 要的"进程内可复现的 Python 参考实现"现在是 ``engine_local``，所以
        # 这个历史别名重定向过去；C++ 那一侧走 ``cpp`` / ``remote``。
        mod = importlib.import_module("engine_local")
    elif name in ("cpp", "remote", "GomokuAI.cpp"):
        mod = importlib.import_module("engine")
    else:
        mod = importlib.import_module(name)
    for attr in ("ai_move", "check_win"):
        if not hasattr(mod, attr):
            raise SystemExit(f"引擎 {name} 缺少 {attr}()")
    return mod


def call_ai(eng, board, player, level, cancel=None):
    """调用引擎，兼容旧签名（无 cancel）。返回 (r, c, info, elapsed_s)。"""
    t0 = time.monotonic()
    try:
        if cancel is None:
            r, c, info = eng.ai_move(board, player, level)
        else:
            r, c, info = eng.ai_move(board, player, level, cancel=cancel)
    except TypeError:
        r, c, info = eng.ai_move(board, player, level)
    return r, c, (info or {}), time.monotonic() - t0


def board_key(board):
    return board.tobytes()


def play_game(eng_black, eng_white, black_level, white_level,
              first_second_move, rng, verbose=False):
    """下一整局，返回统计 dict。"""
    # 每局开始重置双方引擎状态（TT/history/killer），否则上一局的缓存会
    # 泄漏到本局，使"同种子可复现"失效。
    reset_engine(eng_black)
    reset_engine(eng_white)

    board = np.zeros((BOARD_SIZE, BOARD_SIZE), dtype=np.uint8)
    board[CENTER] = BLACK

    stats = {
        "winner": None,          # 'black' | 'white' | 'draw'
        "reason": None,          # 'five' | 'max_moves' | 'repetition' | 'illegal' | 'error'
        "moves": [],
        "illegal": 0,
        "errors": 0,
        "max_move_ms": 0.0,
        "total_ms": 0.0,
        "opening": [list(CENTER), list(first_second_move)],
    }

    move_no = 1
    player = WHITE
    r, c = first_second_move
    if board[r][c] != 0:
        return dict(stats, winner="black", reason="error",
                    errors=1, error="非法开局第二手")
    board[r][c] = WHITE
    stats["moves"].append({"n": move_no, "player": "white", "pos": [r, c],
                           "src": "opening", "ms": 0.0})

    seen = {board_key(board): 1}
    game_start = time.monotonic()

    for move_no in range(2, MAX_MOVES + 1):
        player = BLACK if move_no % 2 == 0 else WHITE
        eng = eng_black if player == BLACK else eng_white
        level = black_level if player == BLACK else white_level

        try:
            r, c, info, dt = call_ai(eng, board, player, level)
        except Exception as exc:  # noqa: BLE001 — 任何引擎异常都算该侧失败
            stats["errors"] += 1
            stats["winner"] = "white" if player == BLACK else "black"
            stats["reason"] = "error"
            stats["error"] = f"{type(exc).__name__}: {exc}"
            break

        if not (0 <= r < BOARD_SIZE and 0 <= c < BOARD_SIZE):
            stats["illegal"] += 1
            stats["winner"] = "white" if player == BLACK else "black"
            stats["reason"] = "illegal"
            stats["error"] = f"越界 {r},{c}"
            break
        if board[r][c] != 0:
            stats["illegal"] += 1
            stats["winner"] = "white" if player == BLACK else "black"
            stats["reason"] = "illegal"
            stats["error"] = f"占用格 {r},{c}"
            break

        board[r][c] = player
        recorder = getattr(eng, "record_move", None)
        if recorder is not None:
            try:
                recorder(info)
            except Exception:  # noqa: BLE001 — 统计钩子失败不应影响对局
                pass

        stats["moves"].append({
            "n": move_no,
            "player": "black" if player == BLACK else "white",
            "pos": [int(r), int(c)],
            "ms": round(dt * 1000, 1),
            "score": info.get("best_val"),
            "depth": info.get("actual_depth", info.get("depth")),
            "nodes": info.get("nodes"),
            "nps": info.get("nps"),
            "reason": info.get("reason"),
        })
        stats["total_ms"] += dt * 1000
        stats["max_move_ms"] = max(stats["max_move_ms"], dt * 1000)

        if eng.check_win(board, player):
            stats["winner"] = "black" if player == BLACK else "white"
            stats["reason"] = "five"
            break

        k = board_key(board)
        seen[k] = seen.get(k, 0) + 1
        if seen[k] >= 3:
            stats["winner"] = "draw"
            stats["reason"] = "repetition"
            break
    else:
        stats["winner"] = "draw"
        stats["reason"] = "max_moves"

    stats["total_ms"] = round(stats["total_ms"], 1)
    stats["max_move_ms"] = round(stats["max_move_ms"], 1)
    stats["wall_ms"] = round((time.monotonic() - game_start) * 1000, 1)
    stats["plies"] = len(stats["moves"])
    if verbose:
        print(f"    -> {stats['winner']} by {stats['reason']} "
              f"in {stats['plies']} plies ({stats['wall_ms']/1000:.1f}s)")
    return stats


def build_matchups(black_eng_name, white_eng_name, games, rng,
                   black_level, white_level, swap=True):
    """生成对局列表，保证双方执黑次数尽量均衡。"""
    pairs = []
    for i in range(games):
        second = OPENING_SECOND_MOVES[i % len(OPENING_SECOND_MOVES)]
        # 用镜像开局打破"同一第二手反复出现"的偏差
        if (i // len(OPENING_SECOND_MOVES)) % 2 == 1:
            second = (2 * CENTER[0] - second[0], 2 * CENTER[1] - second[1])
        swap_this = swap and (i % 2 == 1)
        if swap_this:
            pairs.append((white_eng_name, black_eng_name, black_level, white_level,
                          second, True))
        else:
            pairs.append((black_eng_name, white_eng_name, black_level, white_level,
                          second, False))
    return pairs


def main():
    ap = argparse.ArgumentParser(description="GomokuAI 引擎自对弈 A/B 回归")
    ap.add_argument("--black", default="engine", help="执黑引擎模块名（legacy/engine/…）")
    ap.add_argument("--white", default="legacy", help="执白引擎模块名")
    ap.add_argument("--black-level", type=int, default=1)
    ap.add_argument("--white-level", type=int, default=1)
    ap.add_argument("--games", type=int, default=20)
    ap.add_argument("--seed", type=int, default=20260919)
    ap.add_argument("--tag", default="", help="报告标签，写入 JSON 文件名")
    ap.add_argument("--out", default="", help="报告输出路径（默认 tools/reports/…）")
    ap.add_argument("--no-swap", action="store_true", help="不交换先后手")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    np.random.seed(args.seed)

    t_load = time.monotonic()
    registry = {}
    for name in (args.black, args.white):
        if name not in registry:
            registry[name] = load_engine(name)
    load_ms = (time.monotonic() - t_load) * 1000

    pairs = build_matchups(args.black, args.white, args.games, rng,
                           args.black_level, args.white_level,
                           swap=not args.no_swap)

    report = {
        "black": args.black, "white": args.white,
        "black_level": args.black_level, "white_level": args.white_level,
        "seed": args.seed, "games": args.games,
        "load_ms": round(load_ms, 1),
        "results": [], "summary": {},
    }

    wins = {"black": 0, "white": 0, "draw": 0}
    # 以"args.black 这个引擎"为视角统计
    a_wins = a_losses = 0
    illegal = errors = 0
    total_ms = 0.0
    starts = time.monotonic()

    # 报告路径在**进入循环之前**定下来，并且每局覆写一次。
    # 完整档（level 3）一场 A/B 可能跑几十分钟，而它此前只在全部结束后
    # 写一次 —— 中途中断（Ctrl-C、超时、机器睡眠）就什么都留不下，
    # 而且外部无法知道"跑到第几局了"。覆写的代价是每分钟几十 KB，
    # 换来的是可中断、可观察。
    out = args.out
    if not out:
        tag = f"_{args.tag}" if args.tag else ""
        out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "reports", f"selfplay_{args.black}_vs_{args.white}{tag}.json")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)

    def dump():
        """把当前进度落到磁盘。summary 里的 total_wall_s 是**已跑**的时间，
        因此中途读到的胜率是部分样本的结果，不是最终值。"""
        decided_now = a_wins + a_losses
        report["summary"] = {
            "a_name": args.black,
            "a_wins": a_wins, "a_losses": a_losses,
            "draws": wins.get("draw", 0),
            "win_rate": round(a_wins / decided_now, 4) if decided_now else None,
            "illegal": illegal,
            "errors": errors,
            "total_wall_s": round(time.monotonic() - starts, 1),
            "games_done": len(report["results"]),
            "games_planned": len(pairs),
            "partial": len(report["results"]) < len(pairs),
        }
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=1)

    for i, (nb, nw, lb, lw, second, swapped) in enumerate(pairs):
        eb, ew = registry[nb], registry[nw]
        if not args.quiet:
            side = "A执黑" if not swapped else "A执白"
            print(f"[{i+1}/{len(pairs)}] {side} 开={second}")
        st = play_game(eb, ew, lb, lw, second, rng, verbose=not args.quiet)
        st["index"] = i
        st["swapped"] = swapped
        st["a_side"] = "white" if swapped else "black"
        report["results"].append(st)

        wins[st["winner"]] = wins.get(st["winner"], 0) + 1
        a_side = st["a_side"]
        if st["winner"] == "draw":
            pass
        elif st["winner"] == a_side:
            a_wins += 1
        else:
            a_losses += 1
        illegal += st["illegal"]
        errors += st["errors"]
        total_ms += st["wall_ms"]
        dump()

    dump()          # 最后再写一次：这一笔会把 partial 置为 False

    print("\n" + "=" * 64)
    print(f"A = {args.black} (level {args.black_level})   "
          f"B = {args.white} (level {args.white_level})")
    print(f"对局 {len(pairs)}   A胜 {a_wins}   A负 {a_losses}   "
          f"和 {wins.get('draw', 0)}")
    wr = report["summary"]["win_rate"]
    print(f"A 胜率: {'n/a' if wr is None else f'{wr*100:.1f}%'}")
    print(f"非法走法 {illegal}   引擎异常 {errors}   "
          f"总耗时 {report['summary']['total_wall_s']}s")
    print("=" * 64)
    print(f"报告: {out}")
    return 1 if (illegal or errors) else 0


if __name__ == "__main__":
    sys.exit(main())

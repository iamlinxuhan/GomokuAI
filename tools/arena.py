# -*- coding: utf-8 -*-
"""AI-AI 无头基准（M2）：同一引擎、不同难度档位的配对对战。

用法::

    python tools/arena.py --black-level 5 --white-level 4 --games 8 --tag l5_vs_l4
    python tools/arena.py --black-level 1 --white-level 3 --games 4 --seed 7

与 ``selfplay.py`` 的分工：

* ``selfplay`` 比的是**不同引擎实现**（legacy vs 新引擎），按模块名加载；
* ``arena`` 比的是**同一引擎的不同档位**，走 ``players.AIPlayer`` 与无头
  ``match.Match``，不导入 legacy，也不碰 ``engine.py`` 的源码锚点。

配对与开局：每两局一组，A/B 各执黑一次（``--no-swap`` 可关）；开局前缀从
``tools/selfplay.py`` 的 8 个标准第二手里轮换 —— 引擎是确定性的，不制造
开局差异的话 N 局会走出同一盘棋。报告写到 ``tools/reports/arena_<tag>.json``，
字段与 ``selfplay`` 对齐，并每局覆写一次（可中断、可观察）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from match import Match  # noqa: E402
from players import AIPlayer, PlayerSpec, reset_engine  # noqa: E402
from tools.selfplay import OPENING_SECOND_MOVES  # noqa: E402

CENTER = (9, 9)


def build_pairs(games: int, swap: bool):
    """返回 ``[(a_side, second, swapped), ...]``。

    ``a_side`` 是 A 这一局执什么色；``second`` 是白方第二手（开局前缀）。
    """
    pairs = []
    for i in range(games):
        swapped = swap and (i % 2 == 1)
        second = OPENING_SECOND_MOVES[(i // 2 if swap else i)
                                      % len(OPENING_SECOND_MOVES)]
        pairs.append(("white" if swapped else "black", second, swapped))
    return pairs


def main():
    ap = argparse.ArgumentParser(description="GomokuAI 档位/参数 A-A 基准")
    ap.add_argument("--black-level", type=int, default=1, help="A 方的难度档位")
    ap.add_argument("--white-level", type=int, default=1, help="B 方的难度档位")
    ap.add_argument("--games", type=int, default=8)
    ap.add_argument("--seed", type=int, default=20260919,
                    help="只写进报告：当前引擎无随机源，开局由前缀轮换决定")
    ap.add_argument("--tag", default="", help="报告标签，写入文件名")
    ap.add_argument("--out", default="", help="报告输出路径（默认 tools/reports/…）")
    ap.add_argument("--no-swap", action="store_true", help="不交换先后手")
    ap.add_argument("--a-no-book", action="store_true",
                    help="A 方禁用开局库（两方仍使用同一个 Python 本地引擎）")
    ap.add_argument("--b-no-book", action="store_true",
                    help="B 方禁用开局库（两方仍使用同一个 Python 本地引擎）")
    ap.add_argument("--max-moves", type=int, default=225)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    local_ab = args.a_no_book or args.b_no_book
    a_spec = PlayerSpec("ai", level=args.black_level, book=not args.a_no_book,
                        local=local_ab)
    b_spec = PlayerSpec("ai", level=args.white_level, book=not args.b_no_book,
                        local=local_ab)
    pairs = build_pairs(args.games, swap=not args.no_swap)

    out = args.out
    if not out:
        tag = f"_{args.tag}" if args.tag else ""
        out = os.path.join(ROOT, "tools", "reports",
                           f"arena_L{args.black_level}_vs_L{args.white_level}{tag}.json")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)

    report = {
        "a_level": args.black_level, "b_level": args.white_level,
        "seed": args.seed, "games": args.games,
        "results": [], "summary": {},
    }
    wins = {"black": 0, "white": 0, "draw": 0}
    a_wins = a_losses = 0
    illegal = errors = 0
    total_ms = 0.0
    a_book_hits = b_book_hits = 0
    a_book_changed = b_book_changed = 0
    starts = time.monotonic()

    def dump():
        decided = a_wins + a_losses
        report["summary"] = {
            "a_level": args.black_level, "b_level": args.white_level,
            "a_wins": a_wins, "a_losses": a_losses,
            "draws": wins.get("draw", 0),
            "win_rate": round(a_wins / decided, 4) if decided else None,
            "illegal": illegal, "errors": errors,
            "total_wall_s": round(time.monotonic() - starts, 1),
            "games_done": len(report["results"]),
            "games_planned": len(pairs),
            "partial": len(report["results"]) < len(pairs),
            "a_book": not args.a_no_book,
            "b_book": not args.b_no_book,
            "a_book_hits": a_book_hits,
            "b_book_hits": b_book_hits,
            "a_book_changed": a_book_changed,
            "b_book_changed": b_book_changed,
        }
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=1)

    for i, (a_side, second, swapped) in enumerate(pairs):
        # 每局重置引擎的跨局状态（置换表/history/killer），否则上一局的
        # 热表会改变这一局的走法 —— 那测的就不是"档位差异"了。
        reset_engine()
        if a_side == "black":
            black, white = AIPlayer(a_spec), AIPlayer(b_spec)
        else:
            black, white = AIPlayer(b_spec), AIPlayer(a_spec)

        start_moves = [(CENTER[0], CENTER[1], 1), (second[0], second[1], 2)]
        if not args.quiet:
            print(f"[{i+1}/{len(pairs)}] A执{'黑' if a_side == 'black' else '白'}"
                  f" 开={second}", flush=True)

        t0 = time.monotonic()
        result = Match(black, white, start_moves=start_moves,
                       max_moves=args.max_moves).run()
        wall_ms = (time.monotonic() - t0) * 1000

        st = {
            "index": i, "swapped": swapped, "a_side": a_side,
            "winner": {1: "black", 2: "white", 0: "draw"}[result.winner],
            "moves": result.moves, "wall_ms": round(wall_ms, 1),
            "error": result.error,
        }
        a_book_hits += sum(
            1 for _, _, stone, info in result.record
            if ((stone == 1 and a_side == "black")
                or (stone == 2 and a_side == "white"))
            and info.get("reason") == "开局库")
        b_book_hits += sum(
            1 for _, _, stone, info in result.record
            if ((stone == 1 and a_side == "white")
                or (stone == 2 and a_side == "black"))
            and info.get("reason") == "开局库")
        a_book_changed += sum(
            1 for _, _, stone, info in result.record
            if ((stone == 1 and a_side == "black")
                or (stone == 2 and a_side == "white"))
            and info.get("reason") == "开局库"
            and info.get("book_move_same_as_search") is False)
        b_book_changed += sum(
            1 for _, _, stone, info in result.record
            if ((stone == 1 and a_side == "white")
                or (stone == 2 and a_side == "black"))
            and info.get("reason") == "开局库"
            and info.get("book_move_same_as_search") is False)
        st["book_hits"] = {
            "a": sum(
                1 for _, _, stone, info in result.record
                if ((stone == 1 and a_side == "black")
                    or (stone == 2 and a_side == "white"))
                and info.get("reason") == "开局库"),
            "b": sum(
                1 for _, _, stone, info in result.record
                if ((stone == 1 and a_side == "white")
                    or (stone == 2 and a_side == "black"))
                and info.get("reason") == "开局库"),
        }
        report["results"].append(st)

        if result.error:
            if result.error.startswith("非法走法"):
                illegal += 1
            else:
                errors += 1
        else:
            wins[st["winner"]] = wins.get(st["winner"], 0) + 1
            if st["winner"] == "draw":
                pass
            elif st["winner"] == a_side:
                a_wins += 1
            else:
                a_losses += 1
        total_ms += wall_ms
        dump()

    dump()

    print("\n" + "=" * 64)
    print(f"A = level {args.black_level}   B = level {args.white_level}")
    print(f"对局 {len(pairs)}   A胜 {a_wins}   A负 {a_losses}   "
          f"和 {wins.get('draw', 0)}")
    wr = report["summary"]["win_rate"]
    print(f"A 胜率: {'n/a' if wr is None else f'{wr*100:.1f}%'}")
    print(f"非法走法 {illegal}   引擎异常 {errors}   "
          f"总耗时 {report['summary']['total_wall_s']}s")
    print(f"开局库命中 A/B: {a_book_hits}/{b_book_hits}")
    print(f"库手改变现场搜索 A/B: {a_book_changed}/{b_book_changed}")
    print(f"报告: {out}")
    print("=" * 64)


if __name__ == "__main__":
    main()

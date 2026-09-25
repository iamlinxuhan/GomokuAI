# -*- coding: utf-8 -*-
"""引擎性能基准。

固定局面集 × 难度，输出每题的耗时、深度、节点数、nps 等指标，
并把旧引擎的已知基线一并固化在 `LEGACY_BASELINE` 里做自动对照。

用法::

    python tools/bench.py --engine legacy            # 复现旧基线
    python tools/bench.py --engine engine --level 3
    python tools/bench.py --engine legacy --profile  # 附带 cProfile 热点

指标口径：
  * ``nodes``/``nps``/``tt_hit_rate`` 等为**新引擎**才提供的字段，
    旧引擎缺省时记 ``None``（不臆造数字）。
  * ``time_ms`` 为墙钟实测，与 ``info['time_ms']`` 独立，后者是引擎自报。
  * ``compliance`` = 墙钟 <= 该难度的硬上限。旧引擎无硬上限，
    故其 ``compliance`` 记录为 ``None`` 而非 False（避免误判为回归）。
"""

from __future__ import annotations

import argparse
import cProfile
import io
import json
import os
import pstats
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BOARD_SIZE = 19
BLACK, WHITE = 1, 2

# 旧引擎实测基线（Phase 0 冻结，difficulty 3 = 内部 8.0s 预算）。
# 来源：git d232fa7 的 main.py，在下列同一批局面上实测。
# 注意：**绝对调用次数不可跨环境比较**。旧引擎的 _zobrist_table 在 import 时
# 用未播种的 np.random 生成，不同进程得到不同的表 → 不同的 TT 碰撞 → 不同的
# 搜索树大小（实测同一局面在两次独立测量间相差约 20%）。
# 可比较的是热点排序与占比；本目录的工具在 load_engine() 之前播种 np.random，
# 故本目录内生成的数字彼此可复现。详见 tools/BASELINE.md。
LEGACY_BASELINE = {
    "bench_10stones": dict(depth=5, time_ms=8009.7, note="中局 10 子，难度3"),
    "cprofile_10stones": dict(
        total_calls=14_206_400, time_s=6.783,
        hot=[("_check_win_fast", 13_035, 4.680),
             ("_cached_evaluate", 2_639, 2.207),
             ("evaluate_board", 2_035, 1.965),
             ("_analyze_line", 795_184, 0.848),
             ("zobrist_hash", 2_640, 0.238)],
        note="_check_win_fast 占累计耗时约 69-77%（不同测量批次），"
             "是稳定的首要热点"),
}

# 旧引擎的难度时间上限（秒）。**只在旧引擎上用作合规判据的参照** ——
# 新引擎有自己的真源，见 `time_limit_s()`。
LEGACY_TIME_LIMIT = {1: 1.5, 2: 5.0, 3: 15.0}


def time_limit_s(engine, level):
    """本档的硬时间上限（秒）。

    新引擎**从 `engine.DIFFICULTY` 现读**。这里曾经是一张手抄的第二份表
    （原名 `NEW_TIME_LIMIT`），与 `engine.py` 里的真源各改各的 —— 改档位时
    两处必然漂移，而漂移的后果是 bench 的"时间合规"一列悄悄失效：它比的
    不再是引擎真正遵守的那个上限。

    旧引擎的冻结快照里没有 `DIFFICULTY`（那时还没有这个概念），沿用
    `LEGACY_TIME_LIMIT`。那个字典跟着快照一起冻住，不需要跟谁同步。
    """
    cfg = getattr(engine, "DIFFICULTY", None)
    if cfg and level in cfg:
        return cfg[level]["time"]
    return LEGACY_TIME_LIMIT.get(level, 15.0)


def bench_positions():
    """固定基准局面。坐标用 (r, c)，全部为中局形态，避免开局特殊路径。"""
    def b(black, white):
        m = np.zeros((BOARD_SIZE, BOARD_SIZE), dtype=np.uint8)
        for r, c in black:
            m[r][c] = BLACK
        for r, c in white:
            m[r][c] = WHITE
        return m

    return [
        # 中局 10 子（与 Phase 0 基线同形，用于直接对照 8149.8ms / depth 5）
        dict(id="mid_10stones", to_move=WHITE, board=b(
            [(9, 9), (8, 9), (8, 10), (10, 9), (11, 9)],
            [(9, 10), (10, 10), (9, 8), (7, 10), (8, 8)])),
        # 双方各成一片，互有攻守
        dict(id="mid_clash", to_move=BLACK, board=b(
            [(9, 9), (9, 10), (9, 11), (10, 8), (8, 12)],
            [(10, 9), (10, 10), (10, 11), (9, 8), (11, 12)])),
        # 分散布局，候选多（考验生成与排序）
        dict(id="spread_wide", to_move=WHITE, board=b(
            [(3, 3), (6, 9), (9, 15), (12, 6), (15, 12)],
            [(5, 5), (8, 11), (11, 3), (14, 14), (7, 7)])),
        # 单侧密集，威胁多（考验 VCF/quiescence）
        dict(id="dense_threat", to_move=BLACK, board=b(
            [(9, 7), (9, 8), (9, 9), (10, 9), (8, 9), (10, 8)],
            [(9, 10), (10, 10), (8, 10), (11, 9), (7, 9)])),
        # 稀疏，接近空盘（候选最多）
        dict(id="sparse_early", to_move=WHITE, board=b(
            [(9, 9), (9, 8)],
            [(9, 10), (8, 10)])),
    ]


def run_one(engine, pos, level, is_new):
    board = pos["board"]
    t0 = time.monotonic()
    try:
        out = engine.ai_move(board.copy(), pos["to_move"], level)
    except Exception as exc:  # noqa: BLE001
        return dict(id=pos["id"], error=f"{type(exc).__name__}: {exc}")
    dt = (time.monotonic() - t0) * 1000
    info = (out[2] if len(out) > 2 else {}) or {}
    nodes = info.get("nodes")
    limit = time_limit_s(engine, level) * 1000
    return dict(
        id=pos["id"], move=[int(out[0]), int(out[1])],
        wall_ms=round(dt, 1),
        engine_ms=round(info.get("time_ms", 0.0), 1) if info.get("time_ms") else None,
        depth=info.get("actual_depth", info.get("depth")),
        order_depth=info.get("depth"),
        nodes=nodes,
        nps=round(nodes / (dt / 1000)) if nodes else None,
        tt_hit_rate=info.get("tt_hit_rate"),
        qnode_ratio=info.get("qnode_ratio"),
        vcf_nodes=info.get("vcf_nodes"),
        score=info.get("best_val"),
        score_type=info.get("score_type"),
        reason=info.get("reason"),
        compliance=(dt <= limit + 100) if is_new else None,
    )


def profile_one(engine, pos, level):
    pr = cProfile.Profile()
    pr.enable()
    engine.ai_move(pos["board"].copy(), pos["to_move"], level)
    pr.disable()
    s = io.StringIO()
    pstats.Stats(pr, stream=s).sort_stats("cumulative").print_stats(18)
    return s.getvalue()


def main():
    ap = argparse.ArgumentParser(description="GomokuAI 引擎性能基准")
    ap.add_argument("--engine", default="legacy")
    ap.add_argument("--level", type=int, default=3)
    ap.add_argument("--only", default="")
    ap.add_argument("--profile", action="store_true",
                    help="对第一个局面做 cProfile 并打印累计耗时前 18 项")
    ap.add_argument("--tag", default="")
    args = ap.parse_args()

    import tools.selfplay as sp
    np.random.seed(20260919)
    engine = sp.load_engine(args.engine)
    is_new = args.engine not in ("legacy", "tools.legacy_engine")

    positions = bench_positions()
    if args.only:
        positions = [p for p in positions if args.only in p["id"]]

    print(f"引擎={args.engine}  难度={args.level}  局面数={len(positions)}")
    print("-" * 100)
    hdr = (f"{'局面':<16}{'走法':<10}{'墙钟ms':>9}{'深度':>6}"
           f"{'节点':>12}{'nps':>9}{'TT命中':>8}{'合规':>6}")
    print(hdr)
    print("-" * 100)
    results = []
    for pos in positions:
        r = run_one(engine, pos, args.level, is_new)
        results.append(r)
        if "error" in r:
            print(f"{r['id']:<16}ERROR {r['error']}")
            continue
        tt = "-" if r["tt_hit_rate"] is None else f"{r['tt_hit_rate']:.1%}"
        comp = "-" if r["compliance"] is None else ("y" if r["compliance"] else "N")
        print(f"{r['id']:<16}{str(tuple(r['move'])):<10}{r['wall_ms']:>9.1f}"
              f"{str(r['depth']):>6}{str(r['nodes']):>12}{str(r['nps']):>9}"
              f"{tt:>8}{comp:>6}")
    print("-" * 100)

    ok = [r for r in results if "error" not in r]
    if ok:
        walls = [r["wall_ms"] for r in ok]
        deps = [r["depth"] for r in ok if isinstance(r["depth"], int)]
        print(f"墙钟 中位 {sorted(walls)[len(walls)//2]:.0f}ms  "
              f"最大 {max(walls):.0f}ms")
        if deps:
            print(f"深度 最小 {min(deps)}  中位 {sorted(deps)[len(deps)//2]}  "
                  f"最大 {max(deps)}")
        nps = [r["nps"] for r in ok if r["nps"]]
        if nps:
            print(f"nps  中位 {sorted(nps)[len(nps)//2]:,}  最小 {min(nps):,}")
        comp = [r["compliance"] for r in ok if r["compliance"] is not None]
        if comp:
            print(f"时间合规: {sum(comp)}/{len(comp)}")

    if args.profile:
        print("\n" + "=" * 100)
        print(f"cProfile: {positions[0]['id']} @ level {args.level}")
        print("=" * 100)
        print(profile_one(engine, positions[0], args.level))

    if not is_new:
        print("\n旧引擎已知基线（Phase 0 冻结）:")
        for k, v in LEGACY_BASELINE.items():
            print(f"  {k}: {v}")
        print("  注意：旧引擎无硬时间上限，compliance 记为 '-' 而非 False。")

    tag = f"_{args.tag}" if args.tag else ""
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports",
                       f"bench_{args.engine}_L{args.level}{tag}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(dict(engine=args.engine, level=args.level,
                       results=results, legacy_baseline=LEGACY_BASELINE),
                  fh, ensure_ascii=False, indent=1)
    print(f"报告: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

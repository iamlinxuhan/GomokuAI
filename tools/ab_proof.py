# -*- coding: utf-8 -*-
"""DFPN 证明搜索的 A/B 台：``proof_mode = 0 / 1 / 2``。

用法::

    python tools/ab_proof.py positions --level 5 --time 2.0 --jobs 8
    python tools/ab_proof.py selfplay  --level 5 --time 1.5 --games 16 --jobs 8
    python tools/ab_proof.py all       --level 5 --json out.json

``proof_mode``：``0`` = 关（今天的行为）、``1`` = 只走 VCF 走法的 DFPN、
``2`` = 走 VCT 威胁空间的 DFPN。C++ 侧默认是 0，**这个工具是决定能不能把
它打开的闸门**（计划原文：VCT "确认有提升再使用"）。

两个子命令回答两个不同的问题
---------------------------
* ``positions``：固定题库（复用 ``tools/positions.py`` 的手工局面，A 组基础
  棋型 + B 组历史败局）上逐题比较——平均/P95 耗时、报出杀棋的比例、
  由 DFPN 接管结论的比例。它回答"证明搜索有没有把该看见的杀看见"。
* ``selfplay``：固定开局、双方交换先后手的**配对抗**。它回答"同一时限下
  谁更强"。只有胜负这一栏能支撑"默认打开"的决定；耗时是第二道门
  （P95 不能升高）。

为什么必须并行
--------------
A/B 的样本量是唯一能让结论脱离噪声的东西，而每局都是秒级起步。这里的每个
任务（一道题、一整局）彼此独立、共享同一份代码，天然适合进程级并行：用
``ProcessPoolExecutor`` 把任务摊到多个核上。**每个工作进程有自己的 C++ 服务端
与自己的端口**（``config.PORT`` 覆盖 + ``engine._free_port()``），所以两条臂
的置换表不会互相污染——那正是 ``ab_enhance.py`` 用独享端口的同一个理由。

默认 ``--jobs`` 取 ``cpu_count() // 2``：每局自己还要起一条 DFPN 证明线程
（``search.cpp`` 的并行段），一个任务实际占约两个核。

读数字时注意
------------
* 时限被压到秒级（``--time``），两条臂同等压缩。能外推的是"同一预算下谁
  更强"，**不能**把 1.5 秒的胜率外推成 20 秒的胜率。
* 并行会带来 CPU 争用，耗时统计比实际偏高；两条臂被同样污染，比较仍然成立。
* 一局内的 TT 会跨手保留（生产行为），跨局由 ``mark_reset_needed()`` 清掉。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

BLACK, WHITE = 1, 2
BOARD_SIZE = 19
CENTER = (9, 9)
MAX_MOVES = 225

#: 固定开局第二手（与 ``tools/selfplay.py`` 的 ``OPENING_SECOND_MOVES`` 同源）。
OPENINGS = [(8, 8), (8, 9), (8, 10), (8, 11),
            (9, 8), (9, 10), (10, 9), (10, 10)]

#: 工作进程里的客户端字典。**每个 arms 一个**：两条臂各自的置换表互不可见，
#: 与真实对局里两个进程各持一块 TT 的情形一致。
_CLIENTS: dict[int, object] = {}


# ---------------------------------------------------------------- 进程初始化

def _init_worker() -> None:
    """给本进程一个独享端口，并丢掉 fork 继承来的任何连接状态。"""
    import engine

    engine.config.PORT = engine._free_port()
    _CLIENTS.clear()


def _client(arm: int):
    if arm not in _CLIENTS:
        import engine
        _CLIENTS[arm] = engine._ServerClient("free", BOARD_SIZE)
    return _CLIENTS[arm]


def _release_clients() -> None:
    for cl in _CLIENTS.values():
        cl.shutdown()
    _CLIENTS.clear()


# ---------------------------------------------------------------- 请求构造

def _cfg(level: int, time_limit: float, mode: int,
         proof_time: float, proof_nodes: int) -> dict:
    """这一臂的搜索配置。

    ``book=0`` 是刻意的：开局库会直接短路掉搜索，证明搜索根本没有机会跑，
    拿这样的样本比较两条臂等于比较两把空枪。
    """
    import engine

    cfg = dict(engine._cfg_for(level))
    cfg["time"] = float(time_limit)
    cfg["book"] = 0
    # VCF 是**独立预算**的通道（`search.cpp` 的 vcfBudget）。时限被压缩到
    # 1.5 秒时，宗师档原配的 3 秒 VCF 会把整个预算吃光，主搜索根本来不及
    # 开始 —— 那时比较的就不再是两条臂的搜索。两条臂同等处理，不影响公平性。
    cfg["vcf_budget"] = min(float(cfg["vcf_budget"]), 0.2 * time_limit)
    if mode:
        cfg["proof_mode"] = int(mode)
        cfg["proof_time"] = float(proof_time)
        cfg["proof_nodes"] = int(proof_nodes)
    return cfg


def _move(client, board, player, cfg, cancel=None):
    """一次搜索。返回 ``(idx, info, wall_s)``；``wall_s`` 是**秒**，调用方自己
    决定换成毫秒还是原样用。"""
    t0 = time.monotonic()
    idx, info = client.compute(board, player, cfg, cancel)
    return int(idx), info, time.monotonic() - t0


# ---------------------------------------------------------------- 局面栏

def _task_position(task) -> dict:
    """一道题的一次搜索。"""
    pid, board_arr, to_move, level, mode, time_limit, proof_time, nodes = task
    import engine

    board = np.asarray(board_arr, dtype=np.uint8)
    client = _client(0)
    # 每题都从干净状态开始：置换表跨题保留会让后一题借到前一题的结论，
    # 两条臂的受益顺序又不同 —— 那会把"A 臂更慢"变成"A 臂排在后面"。
    client.mark_reset_needed()
    cfg = _cfg(level, time_limit, mode, proof_time, nodes)
    try:
        idx, info, wall = _move(client, board, int(to_move), cfg)
    except Exception as exc:  # noqa: BLE001 — 一题失败不该终止整轮
        return dict(id=pid, mode=mode, ok=False,
                    error=f"{type(exc).__name__}: {exc}", wall=0.0)
    return dict(
        id=pid, mode=mode, ok=True, wall=wall * 1000.0, idx=idx,
        best_val=info.get("best_val"),
        score_type=info.get("score_type"),
        reason=info.get("reason"),
        depth=info.get("actual_depth", info.get("depth")),
    )


def _positions(level, time_limit, proof_time, nodes, jobs):
    from tools.positions import build_positions

    pos = build_positions()
    modes = (0, 1, 2)
    tasks = [(p["id"], p["board"], p["to_move"], level, m,
              time_limit, proof_time, nodes)
             for p in pos for m in modes]
    print(f"局面栏：{len(pos)} 题 × {len(modes)} 臂 = {len(tasks)} 次搜索，"
          f"jobs={jobs}", flush=True)

    with ProcessPoolExecutor(max_workers=jobs, initializer=_init_worker) as ex:
        rows = list(ex.map(_task_position, tasks))
    return _summarize_positions(rows, [p["id"] for p in pos], modes)


def _summarize_positions(rows, pids, modes):
    def stats(vals):
        if not vals:
            return {}
        v = sorted(vals)
        p95 = v[min(len(v) - 1, int(round(0.95 * (len(v) - 1))))]
        return dict(mean=sum(v) / len(v),
                    p50=v[len(v) // 2], p95=p95, max=v[-1])

    out = {}
    for m in modes:
        rs = [r for r in rows if r["mode"] == m]
        good = [r for r in rs if r.get("ok")]
        walls = [r["wall"] for r in good]
        mate = [r for r in good if r.get("score_type") == "mate"]
        dfpn = [r for r in good if r.get("reason") == "DFPN证明"]
        depths = [r["depth"] for r in good if isinstance(r.get("depth"), int)]
        out[m] = dict(
            n=len(rs), ok=len(good),
            errors=[r for r in rs if not r.get("ok")],
            wall=stats(walls),
            mate=len(mate), dfpn=len(dfpn),
            depth=(sum(depths) / len(depths)) if depths else 0.0,
            per_pos={r["id"]: r for r in good},
        )
    return out


# ---------------------------------------------------------------- 自对弈栏

def _play_game(arm_black, arm_white, level, time_limit, proof_time, nodes,
               opening) -> dict:
    """两条臂下一整局。``arm_*`` 是 proof_mode。"""
    import engine_local

    cb = _cfg(level, time_limit, arm_black, proof_time, nodes)
    cw = _cfg(level, time_limit, arm_white, proof_time, nodes)
    clb = _client(0)   # 黑方永远是 arm 0 的客户端（下同）
    clw = _client(1)
    # 一局开始清空两块置换表：跨局保留会让上一局的结论渗进本局。
    clb.mark_reset_needed()
    clw.mark_reset_needed()

    board = np.zeros((BOARD_SIZE, BOARD_SIZE), dtype=np.uint8)
    board[CENTER] = BLACK
    board[opening] = WHITE

    seen = {board.tobytes(): 1}
    #: 按**臂号**记每手耗时，而不是按黑白 —— 结果栏问的是"这条臂贵多少"。
    per_arm = {arm_black: [], arm_white: []}
    #: 这一臂真正接管结论的手数。**这是"证明搜索到底有没有在干活"的唯一
    #: 直接证据** —— 只看胜率的话，一个从不触发的臂和一臂几乎不触发的臂
    #: 会得到同一个数字，而两者的含义完全不同。
    dfpn_moves = {arm_black: 0, arm_white: 0}
    plies = 1
    winner = None
    reason = None

    for move_no in range(2, MAX_MOVES + 1):
        player = BLACK if move_no % 2 == 0 else WHITE
        arm = arm_black if player == BLACK else arm_white
        client = clb if player == BLACK else clw
        cfg = cb if player == BLACK else cw
        try:
            idx, info, wall = _move(client, board, player, cfg)
        except Exception as exc:  # noqa: BLE001
            winner = "white" if player == BLACK else "black"
            reason = f"error:{type(exc).__name__}"
            break
        r, c = divmod(idx, BOARD_SIZE)
        if board[r, c] != 0:
            winner = "white" if player == BLACK else "black"
            reason = f"illegal:{r},{c}"
            break
        board[r, c] = player
        per_arm[arm].append(wall)
        if (info or {}).get("reason") == "DFPN证明":
            dfpn_moves[arm] += 1
        plies = move_no
        if engine_local.check_win(board, player):
            winner = "black" if player == BLACK else "white"
            reason = "five"
            break
        k = board.tobytes()
        seen[k] = seen.get(k, 0) + 1
        if seen[k] >= 3:
            winner = "draw"
            reason = "repetition"
            break
    else:
        winner = "draw"
        reason = "max_moves"

    # 把"哪一方赢了"翻译成"哪条臂赢了"
    if winner == "draw":
        arm_winner = None
    elif winner == "black":
        arm_winner = arm_black
    else:
        arm_winner = arm_white
    return dict(arm_black=arm_black, arm_white=arm_white, winner=winner,
                arm_winner=arm_winner, reason=reason, plies=plies,
                dfpn_moves={a: int(v) for a, v in dfpn_moves.items()},
                ms={a: [round(x * 1000, 1) for x in v]
                    for a, v in per_arm.items()})


def _task_game(task) -> dict:
    gi, opening, mode_a, mode_b, level, time_limit, proof_time, nodes = task
    try:
        out = _play_game(mode_a, mode_b, level, time_limit,
                         proof_time, nodes, tuple(opening))
    except Exception as exc:  # noqa: BLE001
        out = dict(arm_black=mode_a, arm_white=mode_b, winner="draw",
                   arm_winner=None, reason=f"error:{type(exc).__name__}: {exc}",
                   plies=0, ms={}, dfpn_moves={})
    out["game"] = gi
    out["opening"] = list(opening)
    return out


def _selfplay(level, time_limit, proof_time, nodes, games, jobs):
    """配对抗：每组两局同一开局、交换先后手。

    **两条比较线都要排**：arm0-vs-arm1 与 arm0-vs-arm2。只排一条的话，另一条
    在报告里会以"0 胜 16 负"的形式出现 —— 那是"一局都没下"被读成"全败"的
    静默失败。两条线按组交替，样本量各半。
    """
    pairs = max(1, (games + 1) // 2)
    tasks = []
    for p in range(pairs):
        op = OPENINGS[p % len(OPENINGS)]
        arm = 1 if p % 2 == 0 else 2
        gi = p * 2
        tasks.append((gi, op, 0, arm, level, time_limit, proof_time, nodes))
        tasks.append((gi + 1, op, arm, 0, level, time_limit, proof_time, nodes))
    print(f"自对弈：{len(tasks)} 局（{pairs} 组配对，"
          f"arm0 vs arm1 / arm2 交替），jobs={jobs}", flush=True)
    with ProcessPoolExecutor(max_workers=jobs, initializer=_init_worker) as ex:
        rows = list(ex.map(_task_game, tasks))
    return rows


# ---------------------------------------------------------------- 输出

def _match_score(rows, arm_a, arm_b):
    """``arm_a`` 对 ``arm_b`` 的得分（胜 1、平 0.5）。

    **``rows`` 里必须真的出现过 ``arm_a``。** 少了这条断言，"这一臂一局都没
    下"会以"0 胜 N 负"的形式混进报告，看起来像一条结论 —— 这是这个工具最
    坏的失败方式，必须当场炸掉。
    """
    if not rows:
        raise SystemExit(f"没有可统计的对局（arm {arm_a} vs {arm_b}）")
    for r in rows:
        if arm_a not in (r["arm_black"], r["arm_white"]):
            raise SystemExit(
                f"第 {r['game']} 局里没有 arm {arm_a}"
                f"（{r['arm_black']} vs {r['arm_white']}）—— 统计口径错了")
    pts = w = d = l = 0.0
    for r in rows:
        win = r.get("arm_winner")
        if win is None:
            pts += 0.5
            d += 1
        elif win == arm_a:
            pts += 1
            w += 1
        else:
            l += 1
    n = len(rows)
    return dict(n=n, score=pts, rate=(pts / n if n else 0.0),
                win=int(w), draw=int(d), loss=int(l))


def _pct(x):
    return f"{100.0 * x:5.1f}%"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("what", nargs="?", default="all",
                    choices=("positions", "selfplay", "all"))
    ap.add_argument("--level", type=int, default=5,
                    help="难度档位（默认 5 = 宗师）")
    ap.add_argument("--time", type=float, default=1.5,
                    help="每手时限秒（两条臂同等压缩）")
    ap.add_argument("--proof-time", type=float, default=1.0,
                    help="DFPN 墙钟上限秒（C++ 还会再按剩余预算的一半封顶）")
    ap.add_argument("--proof-nodes", type=int, default=1_000_000,
                    help="DFPN 节点上限")
    ap.add_argument("--games", type=int, default=16,
                    help="自对弈局数（取偶，成对交换先后手）")
    ap.add_argument("--jobs", type=int, default=None,
                    help="并行进程数（默认 cpu_count//2，最少 2）")
    ap.add_argument("--json", default=None, help="把原始统计写到这个文件")
    args = ap.parse_args(argv)

    cpu = os.cpu_count() or 4
    # 每个任务占**两个**核：主搜索一条，DFPN 证明线程一条（见
    # search.cpp 的并行段）。按 `cpu // 2` 排满会把两侧都拖慢，而那正是
    # "带证明搜索的臂"多付的那一份成本 —— 让它变成系统性劣势。留三分之一
    # 的余量，两条臂才在同样宽裕的机器上比棋力。
    jobs = args.jobs or max(2, cpu // 3)
    print(f"CPU={cpu}  jobs={jobs}  level={args.level}  time={args.time}s  "
          f"proof_time={args.proof_time}s", flush=True)

    report = {"level": args.level, "time": args.time,
              "proof_time": args.proof_time, "jobs": jobs}

    if args.what in ("positions", "all"):
        t0 = time.monotonic()
        pos = _positions(args.level, args.time, args.proof_time,
                         args.proof_nodes, jobs)
        print(f"\n=== 局面栏（{time.monotonic() - t0:.1f}s） ===")
        print(f"{'臂':<12}{'平均ms':>9}{'P50':>8}{'P95':>8}{'最大':>8}"
              f"{'杀棋':>7}{'DFPN':>7}{'平均深':>8}")
        for m, label in ((0, "0 关"), (1, "1 VCF-only"), (2, "2 VCT")):
            s = pos[m]
            w = s["wall"] or {}
            print(f"{label:<12}{w.get('mean', 0):9.1f}{w.get('p50', 0):8.1f}"
                  f"{w.get('p95', 0):8.1f}{w.get('max', 0):8.1f}"
                  f"{s['mate']:7d}{s['dfpn']:7d}{s['depth']:8.1f}")
            for e in s["errors"]:
                print(f"    ✗ {e['id']}: {e.get('error')}")
        report["positions"] = {str(m): s for m, s in pos.items()}

        # 逐题对照：同题同臂的结论是否一致、结论差在哪
        base = pos[0]["per_pos"]
        for m in (1, 2):
            diff = []
            for pid, r0 in base.items():
                rm = pos[m]["per_pos"].get(pid)
                if rm is None:
                    continue
                if rm["idx"] != r0["idx"]:
                    diff.append((pid, r0["idx"], rm["idx"],
                                 r0.get("best_val"), rm.get("best_val")))
            print(f"\n臂 {m} 与臂 0 着法不同的题：{len(diff)}/{len(base)}")
            for pid, a, b, va, vb in diff[:8]:
                print(f"  {pid}: 臂0 -> {a} (val={va})   臂{m} -> {b} (val={vb})")
        print()

    if args.what in ("selfplay", "all"):
        t0 = time.monotonic()
        rows = _selfplay(args.level, args.time, args.proof_time,
                         args.proof_nodes, args.games, jobs)
        print(f"\n=== 自对弈栏（{time.monotonic() - t0:.1f}s） ===")
        for arm in (1, 2):
            sub = [r for r in rows if arm in (r["arm_black"], r["arm_white"])]
            if not sub:
                print(f"臂 {arm} vs 臂 0：这一轮没有排到对局（样本为 0）")
                continue
            sc = _match_score(sub, arm, 0)
            print(f"臂 {arm} vs 臂 0：{sc['win']} 胜 {sc['draw']} 平 "
                  f"{sc['loss']} 负  得分率 {_pct(sc['rate'])}")
            # 该臂自己的每手耗时
            ms = [x for r in sub for k, v in r.get("ms", {}).items()
                  if int(k) == arm for x in v]
            fired = sum(int(r.get("dfpn_moves", {}).get(arm, 0)) for r in sub)
            played = sum(len(r.get("ms", {}).get(arm, [])) for r in sub)
            print(f"    DFPN 接管 {fired}/{played} 手"
                  f"（{_pct(fired / played) if played else '—'}）")
            if ms:
                ms.sort()
                p95 = ms[min(len(ms) - 1, int(round(0.95 * (len(ms) - 1))))]
                print(f"    修正每手：平均 {sum(ms) / len(ms):.0f} ms，"
                      f"P95 {p95:.0f} ms")
            for r in sub:
                if r.get("reason", "").startswith(("error", "illegal")):
                    print(f"    ✗ 第 {r['game']} 局异常：{r['reason']}")
        report["selfplay"] = rows
        print()

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        print(f"原始统计已写入 {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

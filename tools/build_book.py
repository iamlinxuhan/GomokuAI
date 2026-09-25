# -*- coding: utf-8 -*-
"""开局库生成器：离线跑最强配置，把开局着法固化成一张表。

用法::

    python tools/build_book.py                # 默认 20 s/节点，中断可续跑
    python tools/build_book.py --budget 5     # 快速重跑一遍
    python tools/build_book.py --fresh        # 忽略缓存，全部重搜

产物是 ``GomokuAI/opening_book.py``（**生成物，不要手工编辑**）：一张
``{局面键: (idx, score, depth)}`` 的表，由 ``engine_local.book_lookup`` 查询。

## 为什么表只到"第 4 手"（``BOOK_STONES = 3``）

盘上 0 子时天元是钉死的规则（``opening_move`` 负责，**不进表**）；1/2/3 子时
才是真正需要查表的时刻。再往下建库收益迅速趋零而成本线性上涨 —— 每个节点
是一次宗师搜索，而库外局面一律回落搜索，与今天逐字相同。

## 树的形状：只在**对手**的轮次上分叉

有一处与直觉相反的简化值得写下来。树不是"每个节点展开它自己最好的 K 手"，
而是：

- **我们自己的轮次只存一手**（我们的最优着法）—— 实战里我们**总是**走这一
  手，为"我们自己不下自己最优着法"的世界建库是白花钱。
- **对手的轮次展开 K 手** —— 只有对手会偏离，而每一种偏离都要有答案。

于是"白应天元 → 黑第 3 手 → 白第 4 手"这条链只要 ``1 + 8 + 8*K`` 次搜索。

**注意分支里必须含"我们自己那一手"这个位置。** 库是按
``(局面, 走子方)`` 分区的，黑方走出它的最优着法之后，轮白方 —— 那是一个
**新键**，而且正是概率最高的那个键（人执黑时就会走它）。把它当成"我们已经
处理过了"而跳过，等于在最重要的一条线上留一个洞，白方第 4 手会退化回搜索。

## 对手的"最可能应手"是一个**启发式**，不是引擎的输出

K 个应手按 ``neighbor_count``（半径 2 内棋子数）降序、同值按升序索引取 ——
与搜索自己排 ``tail`` 档用的键一致，且**完全确定**（同一盘面必定长出同一棵树，
可复现）。

选得不准则只损失**覆盖率**：对手走到表外局面 → 查不到 → 回落搜索。**它不可能
让引擎走错**，所以这个启发式不需要很准。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

#: 表覆盖到"盘上几子"。3 = 第 2/3/4 手。
BOOK_STONES = 3
#: 对手在自己的轮次上展开几手（含它自己的最优着一手）。
K_REPLIES = 4
#: 建库档位：宗师 = 最强配置（增强搜索全开）。预算由 ``--budget`` 覆盖。
BUILD_LEVEL = 5
CACHE = os.path.join(ROOT, "tools", "reports", "opening_book_build.json")
#: ``ROOT`` 就是包目录（``engine.py`` / ``config.py`` 所在处），生成物与
#: ``engine_local.py`` 并排 —— 它由 ``engine_local`` 直接 import。
OUT = os.path.join(ROOT, "opening_book.py")


def _bootstrap_if_missing():
    """产物不存在时先落一张空表。

    **必须在 import ``engine`` / ``engine_local`` 之前做。** 那两者在 import
    期就 `from opening_book import BOOK`（缺了它要立刻炸，理由见那里的注释），
    而本工具又要用它们的 ``Board`` 与 ``_TT_SALT_W`` —— 于是"第一次建库"会
    死在产物还不存在上。一张空表就把这一环解开：它对 ``engine_local`` 是合法
    输入（查不到任何局面 → 回落搜索），几分钟后被真正的表覆盖。
    """
    if os.path.exists(OUT):
        return
    print("产物不存在，先写一张空表引导 import：%s" % OUT)
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write("# -*- coding: utf-8 -*-\n"
                 '"""引导用的空表（tools/build_book.py 的第一次运行会覆盖它）。"""\n'
                 "\nBOOK_STONES = %d\n\nBOOK = {}\n\nPROVENANCE = {}\n"
                 % BOOK_STONES)


_bootstrap_if_missing()

import engine as E                                       # noqa: E402
import engine_local as EL                                # noqa: E402
from tools.positions import coord_to_sgf                 # noqa: E402
from tools.selfplay import CENTER, OPENING_SECOND_MOVES  # noqa: E402

BOARD = EL.BOARD_SIZE


def book_key(board_arr, player):
    """``(局面, 走子方)`` → 64 位键。

    ``Board.hash`` 只由棋子决定、**与走子方无关**（``_ZOBRIST`` 的用法：
    调用点手工异或 ``_TT_SALT_W``）。开局库里同一盘面不可能轮到两方，但键里
    仍然带上走子方 —— 与置换表同一套约定，而且"表按走子方分区"这件事从此
    写在键上，不靠读者的推理。
    """
    h = EL.Board.from_array(board_arr).hash
    return h ^ (0 if player == 1 else EL._TT_SALT_W)


def _fmt(idx):
    return coord_to_sgf(*divmod(idx, BOARD))


def _label(board_arr, idx):
    return "%s(%d)" % (_fmt(idx), idx)


def reply_set(board_arr, best_idx, k):
    """对手最可能的 ``k`` 个应手，**保证含它自己的最优着一手**。

    ``best_idx`` 是我们替对手算出来的最优着法（对黑方第 3 手而言就是库里
    那一项），把它排除在外会在最高概率的那条线上留一个洞，见模块 docstring。
    """
    picks = [best_idx]
    for mv in _plausible(board_arr, k + 1):
        if mv != best_idx and len(picks) < k:
            picks.append(mv)
    return picks


def _plausible(board_arr, k):
    """按 ``(半径 2 内棋子数降序, 索引升序)`` 取 ``k`` 个候选。"""
    bd = EL.Board.from_array(board_arr)
    nc = bd.neighbor_count
    cands = bd.candidates()
    cands.sort(key=lambda i: (-nc[i], i))
    return cands[:k]


def build(args):
    cache = {}
    if not args.fresh and os.path.exists(CACHE):
        with open(CACHE, encoding="utf-8") as fh:
            cache = json.load(fh)
        print("缓存命中 %d 个节点：%s" % (len(cache), CACHE))

    # 自定义预算走同一条 TCP 通道。``RemoteAIPlayer.ai_move`` 只吃档位号、
    # 吃不下自定义 cfg，所以这里直接用它的底层客户端 —— 工具用私有名，
    # 换来的是**被测的那条路径**（报文构造、超时、重连）与生产逐字相同。
    cfg = dict(E.DIFFICULTY[BUILD_LEVEL], time=args.budget)
    client = E._ServerClient()
    entries = {}
    stats = {"searched": 0, "cache": 0, "seconds": 0.0}

    def lookup(board_arr, player, label):
        key = book_key(board_arr, player)
        hk = "%016X" % key
        if hk in cache:
            ent = tuple(cache[hk])
            stats["cache"] += 1
            print("  [缓存] %-34s 走 %-5s val=%-11s dep=%s"
                  % (label, _label(board_arr, ent[0]), ent[1], ent[2]))
            return ent
        t0 = time.monotonic()
        idx, info = client.compute(board_arr, player, cfg, None)
        dt = time.monotonic() - t0
        if not (0 <= idx < BOARD * BOARD) or board_arr[idx // BOARD][idx % BOARD] != 0:
            raise SystemExit("引擎返回了非法着法 idx=%r（%s）" % (idx, label))
        ent = (int(idx), int(info["best_val"]), int(info["actual_depth"]))
        # 每个节点落一次盘：中断（Ctrl-C / 机器睡眠）之后重跑只补缺口。
        cache[hk] = list(ent)
        with open(CACHE, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, sort_keys=True)
        stats["searched"] += 1
        stats["seconds"] += dt
        print("  [搜索] %-34s 走 %-5s val=%-11s dep=%-2s %.1fs"
              % (label, _label(board_arr, ent[0]), ent[1], ent[2], dt))
        return ent

    def put(board_arr, player, ent):
        entries[book_key(board_arr, player)] = ent

    empty = np.zeros((BOARD, BOARD), dtype=np.uint8)
    b1 = empty.copy()
    b1[CENTER] = 1

    print("第 2 手：白应天元")
    put(b1, 2, lookup(b1, 2, "白2"))
    w2_best = entries[book_key(b1, 2)][0]

    # 第 2 层的分支 = 8 个标准第二手（对手不按我们的最优下时也要有答案）。
    print("第 3 手：黑应 8 个标准第二手")
    layer2 = []
    for sec in OPENING_SECOND_MOVES:
        b2 = b1.copy()
        b2[sec] = 2
        idx = (sec[0] * BOARD + sec[1])
        note = "（= 库内最优）" if idx == w2_best else ""
        ent = lookup(b2, 1, "黑3 应 %s%s" % (_fmt(idx), note))
        put(b2, 1, ent)
        layer2.append((b2, ent))

    # 第 3 层：白应黑第 3 手 —— 分叉 = 黑方第 3 手的备选（其最优着一手在内）。
    print("第 4 手：白应黑第 3 手的最可能 %d 手" % K_REPLIES)
    for b2, ent in layer2:
        for mv in reply_set(b2, ent[0], K_REPLIES):
            b3 = b2.copy()
            # `mv` 是**线性格索引**（`candidates()` / 库里的 `idx` 都是这个口径），
            # 落子要拆成行列 —— 直接 `b3[mv] = 1` 会在 19x19 上越界，
            # 或者在 19 以内悄悄下到**另一个位置**（那是更坏的失败）。
            b3[mv // BOARD][mv % BOARD] = 1
            put(b3, 2, lookup(b3, 2, "白4 应 %s" % _label(b3, mv)))

    client.shutdown()
    emit(entries, args, stats)
    return entries


def emit(entries, args, stats):
    """写生成物。键**排序输出**：重新生成时的 diff 只反映真实变化。"""
    doc = [
        '# -*- coding: utf-8 -*-',
        '"""开局库表。**生成物：由 tools/build_book.py 生成，不要手工编辑。**',
        '',
        '重新生成::',
        '',
        '    python tools/build_book.py',
        '',
        '表项是 ``{键: (线性格索引, 走子方视角的分值, 搜到的层数)}``。',
        '键 = ``Board.hash ^ (0 if 走子方是黑 else _TT_SALT_W)``，也就是',
        '「局面 + 走子方」。查询入口见 ``engine_local.book_lookup``。',
        '',
        '只覆盖盘上 %d 子以内的局面（即第 2/3/4 手）。空盘的天元由' % BOOK_STONES,
        '``opening_move`` 的规则给出，不进表；表外局面一律回落搜索。',
        '',
        '**分值如实回传**：表里存的是建库时的真实评估（走子方视角），命中时',
        '原样上报。所以入门档在开局头几手会在胜率图上显示出高手级读数 ——',
        '库里存的就是高手级评估，填 0 反而是撒谎。',
        '"""',
        '',
        "BOOK_STONES = %d" % BOOK_STONES,
        '',
        "BOOK = {",
    ]
    for key in sorted(entries):
        idx, score, dep = entries[key]
        doc.append("    0x%016X: (%d, %d, %d),   # %s"
                   % (key, idx, score, dep, _fmt(idx)))
    doc += [
        "}",
        '',
        "#: 建库时的规模与环境，供审计（程序不读它）。",
        "PROVENANCE = {",
        '    "level": %d,' % BUILD_LEVEL,
        '    "budget_s": %r,' % float(args.budget),
        '    "k_replies": %d,' % K_REPLIES,
        '    "entries": %d,' % len(entries),
        '    "searched_nodes": %d,' % stats["searched"],
        '    "cached_nodes": %d,' % stats["cache"],
        '    "search_seconds": %.1f,' % stats["seconds"],
        '    "enhanced": 1, "lmr": 1, "extend": 1,',
        '    "binary": %r,' % E.binary_path(),
        '    "built_at": %r,' % time.strftime("%Y-%m-%d %H:%M:%S"),
        "}",
        "",
    ]
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(doc))
    print("\n写入 %s：%d 个表项（新搜 %d 个节点 / %.0f 秒，缓存命中 %d）"
          % (OUT, len(entries), stats["searched"], stats["seconds"],
             stats["cache"]))


def main():
    ap = argparse.ArgumentParser(description="生成开局库")
    ap.add_argument("--budget", type=float, default=20.0,
                    help="每个节点的搜索预算（秒）。默认 20 = 宗师的真实预算")
    ap.add_argument("--fresh", action="store_true", help="忽略缓存重新搜")
    ap.add_argument("--port", type=int, default=0,
                    help="另起一个服务端端口。默认 0 = 用 config.PORT(8888)。"
                         "**游戏开着的时候必须给一个别的端口** —— 服务端的规矩"
                         "是'最后连上的客户端胜出'，用它自己的 8888 会把游戏"
                         "那条连接顶掉，游戏会静默降级到 Python 本地实现")
    args = ap.parse_args()
    if E.binary_path() is None:
        raise SystemExit(
            "找不到 C++ 引擎可执行文件。开局库必须由最强配置生成 —— "
            "用 Python 本地实现代替会慢两个数量级、而且更弱。")
    if args.port:
        # `_spawn_and_wait` 在**调用时**读 config.HOST/PORT，所以改在这里
        # 就能让这次建库自成一个服务端，不碰 8888。
        E.config.PORT = args.port
    build(args)


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""局面题库 + 双引擎解题率评估。

两层价值：
  1. **回归题库**：手工构造、约束明确的局面，断言引擎必须走出正确着法。
     不依赖"谁更强"，只依赖棋理，因此不受自对弈噪声干扰。
  2. **历史败局**：从 game_log_*.txt 逐字提取的真实对局局面（含旧引擎当日
     实际走出的错着与排序分），是最有说服力的"改前"证据。

用法::

    python tools/positions.py --engine legacy --level 3
    python tools/positions.py --engine engine --level 3 --tag phase5
    python tools/positions.py --engine legacy --level 3 --dump-json

坐标记法与日志一致：列字母跳过 I（与 main.py 的 coord_to_sgf 相同）。

**注意**：`stones` 里的字母是**日志/UI 记法**，解析时已按跳过 I 反解；
本文件内的手工局面则统一用 `xy=[(r,c), ...]` 直接写行列，避免二义。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BOARD_SIZE = 19
BLACK, WHITE = 1, 2


# ---------------------------------------------------------------- 坐标记法

def coord_to_sgf(r, c):
    """与 main.py / gamelog.py 的 GameLogger.coord_to_sgf 逐字一致。"""
    col_letter = chr(ord('A') + c + (1 if c >= 8 else 0))  # 跳过I
    return f"{col_letter}{r + 1}"


def sgf_to_coord(s):
    """coord_to_sgf 的逆（日志里从 I 开始列字母跳过了 I）。"""
    s = s.strip()
    i = ord(s[0].upper()) - ord('A')
    c = i - 1 if i >= 9 else i
    return int(s[1:]) - 1, c


def parse_stones(s):
    """把 '*K10 OK11 *J9' 解析成 (r, c, player) 列表。'*'=黑(1)，'O'=白(2)。"""
    out = []
    for tok in s.split():
        player = BLACK if tok[0] == '*' else WHITE
        r, c = sgf_to_coord(tok[1:])
        out.append((r, c, player))
    return out


def make_board(stones):
    b = np.zeros((BOARD_SIZE, BOARD_SIZE), dtype=np.uint8)
    for r, c, p in parse_stones(stones):
        b[r][c] = p
    return b


def board_from_xy(black_xy, white_xy):
    b = np.zeros((BOARD_SIZE, BOARD_SIZE), dtype=np.uint8)
    for r, c in black_xy:
        b[r][c] = BLACK
    for r, c in white_xy:
        b[r][c] = WHITE
    return b


# ---------------------------------------------------------------- 题库

def _p(pid, group, source, board, to_move, note, **expect):
    return dict(id=pid, group=group, source=source, board=board,
                to_move=to_move, note=note, expect=expect)


def build_positions():
    P = []

    # ============================================================
    # A 组 — 基础棋型（手工构造，棋理上无歧义）
    # ============================================================
    # A1 白方冲四（左端被黑堵），黑方唯一堵点
    P.append(_p(
        "A1_block_closed_four", "basic",
        "手工构造",
        board_from_xy(black_xy=[(9, 4), (0, 0), (0, 4), (0, 8)],
                      white_xy=[(9, 5), (9, 6), (9, 7), (9, 8)]),
        BLACK,
        "白方 (9,5)-(9,8) 四连，左端 (9,4) 被黑占 → 冲四，(9,9) 是唯一成五点。"
        "黑走别处则白 (9,9) 成五。",
        must_play=[(9, 9)],
        avoid=[(9, 10), (10, 10), (8, 8)],
    ))

    # A2 黑方有成五点 + 白方有醒目活三 → 必须选取胜而非防守（B1 核心）
    P.append(_p(
        "A2_win_in_one", "basic",
        "手工构造",
        board_from_xy(black_xy=[(9, 5), (9, 6), (9, 7), (9, 8)],
                      white_xy=[(8, 5), (8, 6), (8, 7), (0, 0)]),
        BLACK,
        "黑方活四 (9,5)-(9,8)，(9,4)/(9,9) 任一即成五；同时白方行 8 有活三"
        "（静态评估会给出可观的防守价值）。这是 B1 的最小复现："
        "取胜线必须压过任何静态分。"
        "注：胜局只硬性检查着法；分数仅作记录（旧引擎经威胁层快速通道落子，"
        "返回 val=0/dep=0，分数无意义）。",
        must_play=[(9, 4), (9, 9)],
        mate_warn=+1,
    ))

    # A3 白方活四，黑方必败 → 必须报出负将杀分
    P.append(_p(
        "A3_lost_to_live_four", "basic",
        "手工构造",
        board_from_xy(black_xy=[(0, 0), (0, 4), (0, 8), (0, 12)],
                      white_xy=[(9, 5), (9, 6), (9, 7), (9, 8)]),
        BLACK,
        "白方活四，(9,4) 与 (9,9) 两处成五点，黑只能堵一处 → 必败。"
        "要求引擎报出负的将杀分，而不是被静态分掩盖。",
        mate_sign=-1,
    ))

    # A4 黑方双冲四 → 必胜
    P.append(_p(
        "A4_double_four", "basic",
        "手工构造",
        board_from_xy(black_xy=[(9, 6), (9, 7), (9, 8),
                                (6, 9), (7, 9), (8, 9)],
                      white_xy=[(9, 5), (5, 9), (0, 1), (0, 5), (0, 9), (0, 13)]),
        BLACK,
        "黑走 (9,9) 同时形成两个冲四：行 9 的 (9,6)-(9,9)（左端 (9,5) 被白堵，"
        "成五点 (9,10)）与列 9 的 (6,9)-(9,9)（上端 (5,9) 被白堵，成五点 (10,9)）。"
        "白只能堵一处 → 黑必胜。验证跨线组合（双四）是否被判为必胜。"
        "注：同 A2，胜局只硬性检查着法；旧引擎此处返回 val=79438.9（静态量级），"
        "说明组合棋型的识别依赖威胁层而非搜索。",
        must_play=[(9, 9)],
        mate_warn=+1,
    ))

    # A5 活三必须挡（B7 回归：活三与跳活三必须同级）
    P.append(_p(
        "A5_block_open_three", "basic",
        "手工构造",
        board_from_xy(black_xy=[(0, 0), (0, 4), (0, 8)],
                      white_xy=[(9, 7), (9, 8), (9, 9)]),
        BLACK,
        "白方行 9 活三，(9,6) 与 (9,10) 皆空。黑必须挡一端，"
        "否则白成活四必杀。允许堵任一端。",
        must_play=[(9, 6), (9, 10)],
        avoid=[(10, 10), (8, 8), (9, 11)],
    ))

    # ============================================================
    # B 组 — 历史败局（逐字取自 game_log_*.txt，含旧引擎当日实际走出的错着）
    # ============================================================
    # B1 见 game_log_20260606_233336.txt @9
    #    黑第 9 手 J9 形成主对角线活三 (6,6)/(7,7)/(8,8)/(9,9)/(10,10) 中的
    #    (8,8),(9,9),(10,10)，两端 (7,7)=H8 与 (11,11)=M12 皆空。
    #    白方（AI）必须挡其一。当日实际走 K9 并于 4 手后败。
    #    旧引擎实测：1/2 级走 K9，3 级走 H13，**三个难度都未走挡点**；
    #    候选排序 K9=34511710 vs 挡点=34201320（差 0.9%，B7 跳活三=1e6 的噪声）。
    P.append(_p(
        "B1_blunder_233336_p10", "historic",
        "game_log_20260606_233336.txt 第 10 手（白）",
        make_board("*L9 *M9 OJ10 *K10 OL10 OK11 *L11 OJ12 *J9"),
        WHITE,
        "黑方**双活三**：主对角线 (8,8)(9,9)(10,10)，以及行 8 的跳活三 "
        "(8,8)(8,10)(8,11)（(8,9) 一落即活四）。两者共用 (8,8)，白挡得"
        "住一条挡不住另一条 —— **白方已必败**。"
        "当日白走 K9=(8,9)：挡掉行 8，黑随即 H8=(7,7) 在主对角线成活四，"
        "白 M12 挡一端，黑 G7 连五。"
        "\n"
        "**期望曾经写错**：原为 must_play=[(7,7),(11,11)]（即\"必须挡对角线\"），"
        "那是以旧引擎的视角写的 —— 旧引擎看不到行 8 的跳活三，于是把它当成"
        "一个只有单侧威胁的局面。手工核过：白不论挡哪条都输，所以这条题的正"
        "确期望是 `mate_sign=-1`（报出必败），而不是\"走哪一格\"。"
        "实测 L1/L2/L3 都返回 val=-9999995（五步内被将死）。",
        mate_sign=-1,
    ))

    # B2 同一局的终局前：game_log_20260606_233336.txt @11（黑刚 H8 成活四）
    #    白方已必败 —— 黑在 (6,6)/(11,11) 两端都能成五。
    P.append(_p(
        "B2_lost_233336_p12", "historic",
        "game_log_20260606_233336.txt 第 12 手（白）",
        make_board("*H8 *J9 OK9 *L9 *M9 OJ10 *K10 OL10 OK11 *L11 OJ12"),
        WHITE,
        "黑方主对角线活四 (7,7)-(10,10)，(6,6)/(11,11) 两处成五，白必败。"
        "当日白走 M12（候选分 580700440 ≈ 连五分值的 58 倍），黑 G7 连五。"
        "要求：负将杀分能被识别（旧引擎此处已返回 -10000002 但着法仍错）。",
        mate_sign=-1,
    ))

    # B3 game_log_20260606_232014.txt，日志 @31 快照 + 第 32 手 M11(白) + 第 33 手 L13(黑)
    P.append(_p(
        "B3_blunder_232014_p34", "historic",
        "game_log_20260606_232014.txt 第 34 手（白），由 @31 快照 + 第 32/33 手推得",
        make_board("*O7 OH8 *L8 OM8 ON8 OO8 OP8 *Q8 *J9 *L9 OM9 *D10 OJ10 *K10 "
                   "OL10 OE11 *J11 OK11 *L11 OF12 *G12 *H12 *J12 *K12 OL12 "
                   "*M12 OG13 OM11 *L13 *M13 ON13 OH14 *J15"),
        WHITE,
        "当日白走 M14（候选分 34239660），旧引擎 val=-10000000 进入拼命模式；"
        "黑随后 K14，再于第 37 手 H16 连五。"
        "\n"
        "**期望曾经写错**：原为 mate_sign=-1（认为白必败），那是照抄旧引擎"
        "的 -1e7 写的 —— 而那个数正是旧引擎\"进入拼命模式\"的标志，不是"
        "它算出了必败。白方在这里其实**有杀**：列 11 上白有 (7,11)(8,11)(10,11)，"
        "落 M7=(6,11) 后 (9,11) 成为成五点，此后白一路冲四（M7 → O9 → L6 → K5），"
        "黑每一步都被迫去挡，第四手白连五。"
        "实测 L1/L2/L3 都返回 val=9999994（六步内成五），且这条线在 "
        "tests/test_search_mate.py 里会被重走一遍对账（分值距离 vs 实际手数）。",
        mate_sign=+1,
    ))

    # B4 自对弈 legacy vs legacy (seed=7) 第 2 局第 17 手（白）
    #    旧引擎此处返回 -11797248.7，仍在第 18/20 手被黑方威胁层连五。
    #    价值：证明该失败模式在无人干预的自对弈中同样复现，非偶发。
    P.append(_p(
        "B4_selfplay_g2_p17", "historic",
        "tools/reports/selfplay_legacy_vs_legacy_smoke.json 第 2 局第 17 手（白）",
        board_from_xy(
            black_xy=[(9, 9), (9, 10), (10, 7), (5, 12), (6, 11),
                      (8, 10), (8, 5), (10, 9), (11, 8)],
            white_xy=[(8, 9), (9, 8), (7, 10), (8, 8),
                      (8, 7), (8, 6), (7, 6), (10, 8)]),
        WHITE,
        "旧引擎在此返回 -11797248.7（标为必败），随后黑方经威胁层于第 18/20 手"
        "连五。用于确认失败模式在自对弈中可复现。"
        "\n"
        "**期望改过一次，改的理由值得记下来。** 原来写的是 `mate_sign=-1`"
        "（\"引擎会报出白方必败\"）。2026-09-19 复核发现这个期望是错的，"
        "而且错得有意思："
        "\n"
        "① 旧引擎的 -11797248.7 **不是**它算出来的必败。它的真将杀分约定是"
        "`±(1e7 + depth)`，而 -1.18e7 落在复合静态分的量程里 —— 那是\"拼命"
        "模式\"（`legacy_engine.py` 的攻防加权混合）给出的一个很大的负数。"
        "把这个数当成\"已识别必败\"，正是旧引擎分值体系不可读的后果之一。"
        "\n"
        "② 黑方**确实**有连续冲四杀（Phase 5 的 VCF 证明）：黑 (7,4) 造出"
        "五 E10=(9,6)，白被迫去挡；黑再走 (8,11) 直接成活四（成五点 N8 与 "
        "H13），白挡不住。VCF 给出 5 手、3 手两级深度。"
        "\n"
        "③ 但 VCF 证明的是\"**轮到黑方时**黑方能连冲四取胜\"，而这里轮到的是"
        "**白方** —— 白先动手就有机会去占黑的起手点。逐格验过：白的 7 个"
        "防守候选（黑的所有成四点 (7,4)/(7,12)/(8,11)/(9,6)/(12,7)/(12,9)/"
        "(13,6)）走完，黑方**每一条**仍然是 VCF 必胜 —— 所以这条威胁白拆"
        "不干净，但它仍然不等于\"白已必败\"：白或许有非冲四的守法，白自己也"
        "没有比对手更快的冲四。**VCF 是威胁检测器，不是败局证明器。**"
        "\n"
        "④ 因此这条题现在的期望是 `opp_vcf=True` —— 断言那个**已被证明**的"
        "事实（对手存在 VCF），而不是那个没被证明的（白必败）。新引擎在"
        "这里返回静态分 (-557920)，主搜索 24 层内看不到这条 5 手的 VCF，"
        "因为白方在根上会去拆它 —— 引擎的\"不表态\"与\"白没输\"是两回事。",
        opp_vcf=True,
    ))

    return P


# ---------------------------------------------------------------- 评估

def reset_engine(engine):
    """清空引擎的跨局面状态。

    新引擎提供 ``new_game()``；冻结的旧引擎把 TT/history/killer/eval 缓存
    全放在模块全局，没有对应的重置入口，这里显式清空以模拟同样的语义。

    **这不是可有可无的礼貌**：旧引擎的搜索结果依赖调用顺序（同一局面在
    冷/热 TT 下会给出不同着法），若不对双方一视同仁地重置，A/B 对比的
    就不是算法差异而是"谁捡到了更暖的缓存"。
    """
    fn = getattr(engine, "new_game", None)
    if callable(fn):
        fn()
        return
    cache = getattr(engine, "_eval_cache", None)
    if cache is not None:
        cache.clear()
    hist = getattr(engine, "_history_table", None)
    if hist is not None:
        hist[:] = 0
    for slot in getattr(engine, "_killer_moves", []) or []:
        slot[0] = slot[1] = None
    tt = getattr(engine, "_transposition_table", None)
    if tt is not None:
        for i in range(len(tt)):
            tt[i] = None
    if hasattr(engine, "_tt_age"):
        engine._tt_age = 0


# 旧引擎的将杀分约定（tools/legacy_engine.py 的两处 return）：
#     return 10000000 + depth  /  return -10000000 - depth
# 也就是**恰好 1e7 再加一个很小的深度**。这个约定是唯一可靠的判据 ——
# 量级阈值一定错，因为旧引擎的复合静态分（威胁组合 + 拼命模式）能到
# ±3.45e7，比它自己的将杀分**还大**。实测 B1 上旧引擎报 -12527470.65，
# 那是一个"拼命模式"的综合分，不是杀棋；用 abs(score) > 9e6 判会把
# 每个必败局面都误判成"报出了杀棋"，于是 B1 的 mate_sign=-1 被旧引擎
# 白白满足，"旧引擎在这里犯错"的断言就失去了依据。
_LEGACY_MATE_BASE = 1.0e7
_LEGACY_MATE_TAIL = 64.0        # depth 的量级；迭代加深封顶 24 层，取 64 留足余量


def _claims_mate(info, score):
    """引擎是否**明确声称**这是一个将杀分。

    新引擎有 `score_type` 字段，直接信它（`'mate'` / `'static'` 由搜索
    自己判定，不存在猜测成分）。旧引擎没有这个字段，只能按它的分值约定
    反推：落在 `1e7 ± 32` 这条窄带里的才算，其余一律当静态分。
    """
    st = info.get("score_type")
    if st is not None:
        return st == "mate"
    if score is None:
        return False
    return _LEGACY_MATE_BASE <= abs(score) <= _LEGACY_MATE_BASE + _LEGACY_MATE_TAIL


def _opponent_has_vcf(engine, pos):
    """对手（`pos['to_move']` 的另一方）在这个局面上是否有连续冲四杀。

    注意它问的是"**如果轮到对手**，他有没有一条连着冲四的杀"—— 这与
    "我现在是不是已经输了"**不是**同一件事：题目轮到的是我，我可以去占掉
    他的起手点。所以这条断言只说"威胁存在"，不说"我败了"。
    `tools/positions.py` 里 B4 的注释把这两件事的差别写清楚了。
    """
    opp = 3 - pos["to_move"]
    eng = getattr(engine, "_ENGINE", None)
    if eng is None:
        return False
    bd = engine.Board.from_array(pos["board"])
    st, _, _ = eng.vcf(bd, opp, 1.5)
    return st == engine.VCF_WIN


def evaluate_position(engine, pos, level):
    """在局面 pos 上让 engine 执 pos['to_move'] 方走一步，返回结果 dict。"""
    board = pos["board"]
    reset_engine(engine)
    t0 = time.monotonic()
    try:
        out = engine.ai_move(board.copy(), pos["to_move"], level)
    except Exception as exc:  # noqa: BLE001
        return dict(id=pos["id"], ok=False, error=f"{type(exc).__name__}: {exc}",
                    elapsed_ms=(time.monotonic() - t0) * 1000)
    r, c, info = out[0], out[1], (out[2] if len(out) > 2 else {})
    dt = (time.monotonic() - t0) * 1000
    info = info or {}

    exp = pos["expect"]
    score = info.get("best_val")
    checks = {}
    notes = {}

    is_mate = _claims_mate(info, score)

    # --- 硬性检查（决定 PASS/FAIL）---
    if "must_play" in exp:
        checks["must_play"] = (r, c) in exp["must_play"]
    if "avoid" in exp:
        checks["avoid"] = (r, c) not in exp["avoid"]
    if "mate_sign" in exp:
        # 仅用于"已必败"局面：要求引擎报出负的将杀分而非被静态分掩盖。
        want = exp["mate_sign"]
        checks["mate_detected"] = bool(is_mate)
        if is_mate:
            checks["mate_sign"] = (score > 0) if want > 0 else (score < 0)

    if "opp_vcf" in exp:
        # "对手存在一条连续冲四杀" —— 这是对**局面**的断言，不是对引擎输出
        # 的断言，所以它由 VCF 子系统独立证明，与搜索深度、时间、TT 全无关。
        # 只在引擎提供 VCF 时成立（旧引擎没有这个子系统，记成 note 而不是
        # 失败，否则这条题会把"旧引擎没有 VCF"读成"这个局面不满足期望"）。
        if hasattr(engine, "VCF_WIN"):
            checks["opp_vcf"] = _opponent_has_vcf(engine, pos) == exp["opp_vcf"]
        else:
            # 门是 `hasattr(engine, "VCF_WIN")` 而不是"有没有 vcf 命令"：这个探针
            # 直接摸引擎的模块级 `_ENGINE`，只有**本地**实现能回答。
            #
            # 新门面 `engine.py` **刻意不**再导出 VCF_WIN。导出的后果是
            # `_opponent_has_vcf` 里 `getattr(engine, "_ENGINE", None)` 拿到 None、
            # 直接 `return False` —— 于是"这条断言测不了"会伪装成"测出来是假的"，
            # 一道本该被看见的缺口变成一次静默通过。缺 VCF_WIN 时走 note 分支，
            # 报告里明写"测不了"。
            #
            # 注意措辞：C++ 服务端**有**完整的 VCF 子系统，只是没有把探针挂到
            # TCP 协议上。写成"引擎无 VCF 子系统"会让人误以为 C++ 那边没实现。
            notes["opp_vcf"] = "此门面无法访问 VCF 探针（需直接对 engine_local 运行）"

    # --- 记录性检查（不影响 PASS/FAIL）---
    if "mate_warn" in exp:
        notes["mate_seen"] = bool(is_mate)

    return dict(
        id=pos["id"], group=pos["group"], source=pos["source"],
        to_move=pos["to_move"],
        move=[int(r), int(c)], move_sgf=coord_to_sgf(r, c),
        score=score, score_type=info.get("score_type"),
        depth=info.get("actual_depth", info.get("depth")),
        reason=info.get("reason"),
        elapsed_ms=round(dt, 1),
        checks=checks,
        notes=notes,
        # 三态：True 通过 / False 失败 / **None 表示"这道题的断言一条都跑不了"**。
        #
        # 以前这里是 `if checks else False`，把"测不了"和"测出来是错的"合并成
        # 一个 False。后果是 B4（唯一期望是 opp_vcf）在任何拿不到 VCF 探针的
        # 引擎上都恒判 FAIL，于是解题率的分母里永远压着一道不可能通过的题 ——
        # 看起来像引擎不行，实际是工具测不了。
        ok=all(checks.values()) if checks else None,
        note=pos["note"],
    )


def _fmt_note(key, value):
    """note 的值可能是字符串（"测不了"的原因），也可能是个 bool。

    以前一律按 bool 渲染成 ``=y``/``=n``，于是一条 "此门面无法访问 VCF 探针"
    的字符串 note 会被打印成 ``opp_vcf=y`` —— 读起来正好是**相反的意思**。
    """
    if isinstance(value, str):
        return f"{key}={value}"
    return f"{key}={'y' if value else 'n'}"


def main():
    ap = argparse.ArgumentParser(description="GomokuAI 局面题库评估")
    ap.add_argument("--engine", default="legacy")
    ap.add_argument("--level", type=int, default=3)
    ap.add_argument("--only", default="", help="只跑 id 含该子串的局面")
    ap.add_argument("--tag", default="")
    ap.add_argument("--dump-json", action="store_true")
    args = ap.parse_args()

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import tools.selfplay as sp
    np.random.seed(20260919)
    engine = sp.load_engine(args.engine)

    positions = build_positions()
    if args.only:
        positions = [p for p in positions if args.only in p["id"]]

    results = []
    print(f"引擎={args.engine}  难度={args.level}  题数={len(positions)}")
    print("-" * 78)
    for pos in positions:
        res = evaluate_position(engine, pos, args.level)
        results.append(res)
        mark = {True: "PASS", False: "FAIL", None: "N/A "}[res["ok"]]
        detail = " ".join(f"{k}={'y' if v else 'n'}"
                          for k, v in res["checks"].items())
        if res["notes"]:
            detail += "  | " + " ".join(_fmt_note(k, v)
                                        for k, v in res["notes"].items())
        print(f"[{mark}] {res['id']:<26} 走 {res['move_sgf']:<4} "
              f"val={res['score']!s:<12} dep={res['depth']!s:<3} "
              f"{res['elapsed_ms']:>7.0f}ms  {detail}")
        if res["ok"] is not True:
            print(f"        期望: {pos['expect']}")
            print(f"        说明: {pos['note'][:110]}")

    # 解题率的分母**只算能评估的题**。把 N/A 计进分母会让"工具测不了"表现为
    # "引擎答错了"，而两者要采取的行动完全相反。
    scored = [r for r in results if r["ok"] is not None]
    n_pass = sum(1 for r in scored if r["ok"])
    print("-" * 78)
    if scored:
        line = f"解题率: {n_pass}/{len(scored)} = {n_pass/len(scored)*100:.1f}%"
    else:
        line = "解题率: 无题可评估"
    skipped = len(results) - len(scored)
    if skipped:
        ids = ", ".join(r["id"] for r in results if r["ok"] is None)
        line += f"   （另有 {skipped} 题无法评估: {ids}）"
    print(line)

    if args.dump_json:
        tag = f"_{args.tag}" if args.tag else ""
        out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports",
                           f"positions_{args.engine}_L{args.level}{tag}.json")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(dict(engine=args.engine, level=args.level, results=results,
                           pass_rate=(n_pass / len(scored)) if scored else None,
                           n_scored=len(scored),
                           n_unevaluable=len(results) - len(scored)), fh,
                      ensure_ascii=False, indent=1)
        print(f"报告: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

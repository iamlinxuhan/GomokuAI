// RIF 禁手判定的 C++ 实现 —— `ban_rules.py` 的等价物。
//
// 这是**纯函数、无状态**的一层：只读 `Board` 与 `tables`，不改任何东西。判定
// 语义逐条对齐 `ban_rules.py`，两边靠 `tests/test_ban_rules.py` 的固定局面表
// 与 `tests/test_ban_parity.py` 的 `forbiddenPoints` 对账。
//
// ## 三条容易搞反的地方（与 ban_rules.py 的文档一字不改）
//
//  * **只有黑方受禁**，白方怎么下都不禁（长连也算胜）。
//  * **五连优先于全部禁手，含长连。** 落子后任一方向形成**恰好 5 连** → 黑胜，
//    此后不再看任何禁手。
//  * **长连是 ≥6，五连是恰好 5。** 无禁手引擎里 `hasFiveBits` / `makesFive`
//    把两者都当五（那是全项目唯一的胜负判定，禁手模式**不动它**）；本模块把
//    两者分开。
//
// ## 「四」与「活三」的记法
//
// 一个「四」= 一组 4 颗己方子 + 一个空位，补上即成五连（且不是长连）。两个四
// 不同 ⟺ **这 4 颗子的位置集合**不同 —— 活四 `_XXXX_` 的两个成五点对应同一组
// 子，仍是**一个**四；`X_XXX_X` 的两组子不同，同线双四。本模块用"线上位置的
// 位掩码"表示这组子，靠它去重。
//
// 「活三」同理：某个补点 `e` 补出的活四，去掉 `e` 剩下的 3 颗子记一组；同线的
// `_XXX_` 补左补右是同一组。**某方向一旦成立一个四，该方向就不再计活三** ——
// 否则 `_XXXX_` 的 3 子子集会凭空造出三三。
//
// ## free / ban 两种编译
//
// `GOMOKU_BAN` 未定义或为 0 时，本头文件编译成空实现（`classify` 恒 OK、
// `forbiddenPoints` 恒空），`gomoku_engine` 的目标码因此与今天逐位相同。
// 所有 ban 相关的接入点（`search.cpp` / `tcp_server.cpp` / `main.cpp`）都包在
// `#if GOMOKU_BAN` 里，free 版一个字节都不执行到。

#pragma once

#include <cstdint>
#include <vector>

#include "board.h"
#include "bits361.h"
#include "constants.h"
#include "tables.h"

namespace gomoku {
namespace ban {

//: 禁手种类。`NONE` 同时表示"这一手合法"与"这一手直接成五"两种情形 —— 成五由
//: 调用方另判（`fiveAt` / `classify`），本模块不把"赢"混进禁手种类里。
enum class Kind { NONE = 0, OVERLINE = 1, DOUBLE_FOUR = 2, DOUBLE_THREE = 3 };

//: 搜索侧对黑方一手的归类（对应 `engine_ban._black_ban` 的 BAN_*）。
enum class Outcome { OK = 0, WIN = 1, LOSE = 2 };

#if GOMOKU_BAN

namespace detail {

//: 假设 `cells[pos]` 是 `me`（它现在可能是空点）时，过 `pos` 的最大连续段长。
//: **不读 `cells[pos]` 本身** —— 本模块所有"假设在此落子"的判定的共同基础。
inline int runAt(const int8_t* cells, int n, int pos, int me) {
    int lo = pos;
    while (lo - 1 >= 0 && cells[lo - 1] == me) --lo;
    int hi = pos;
    while (hi + 1 < n && cells[hi + 1] == me) ++hi;
    return hi - lo + 1;
}

//: 本方向上、**包含 center** 的「四」的**位置集合数**（按子集合去重）。
//: `outSets` 收到的每个掩码 = 该四的 4 颗子的线上位置。
inline int fourCount(const int8_t* cells, int n, int center, int me,
                     uint32_t* outSets) {
    int cnt = 0;
    if (n < 5) return 0;
    const int lo = center - 4 < 0 ? 0 : center - 4;
    const int hi = (n - 5 < center) ? (n - 5) : center;
    for (int start = lo; start <= hi; ++start) {
        int stones = 0, empties = 0, e = -1;
        for (int k = 0; k < 5; ++k) {
            const int v = cells[start + k];
            if (v == me) ++stones;
            else if (v == 0) {
                ++empties;
                e = start + k;
            }
        }
        if (stones != 4 || empties != 1) continue;
        // 补这一子出来的是长连，不是五连 —— 按定义它成不了五，不算四。
        if (runAt(cells, n, e, me) > 5) continue;
        uint32_t mask = 0;
        for (int k = 0; k < 5; ++k) {
            if (cells[start + k] == me) mask |= (1u << (start + k));
        }
        bool dup = false;
        for (int j = 0; j < cnt; ++j) {
            if (outSets[j] == mask) {
                dup = true;
                break;
            }
        }
        if (!dup) outSets[cnt++] = mask;
    }
    return cnt;
}

//: 本方向上、**包含 center** 的「活三」的**位置集合数**（按子集合去重）。
//: 会就地修改 `cells`（临时补点），返回前复原。
inline int threeCount(int8_t* cells, int n, int center, int me,
                      uint32_t* outSets) {
    int cnt = 0;
    if (n < 4) return 0;
    const int elo = center - 4 < 0 ? 0 : center - 4;
    const int ehi = (n - 1 < center + 4) ? (n - 1) : (center + 4);
    for (int e = elo; e <= ehi; ++e) {
        if (cells[e] != 0) continue;
        cells[e] = static_cast<int8_t>(me);
        for (int i = 0; i + 3 < n; ++i) {
            bool ok = true;
            for (int k = 0; k < 4; ++k) {
                if (cells[i + k] != me) {
                    ok = false;
                    break;
                }
            }
            if (!ok) continue;
            if (i - 1 < 0 || i + 4 >= n) continue;
            if (cells[i - 1] != 0 || cells[i + 4] != 0) continue;
            // 活四两端的补五都必须合法：补成六连的那个方向不算活。
            if (i - 2 >= 0 && cells[i - 2] == me) continue;
            if (i + 5 < n && cells[i + 5] == me) continue;
            if (!(i <= center && center <= i + 3 && i <= e && e <= i + 3)) {
                continue;
            }
            uint32_t mask = 0;
            for (int j = 0; j < 4; ++j) {
                if (i + j != e) mask |= (1u << (i + j));
            }
            bool dup = false;
            for (int j = 0; j < cnt; ++j) {
                if (outSets[j] == mask) {
                    dup = true;
                    break;
                }
            }
            if (!dup) outSets[cnt++] = mask;
        }
        cells[e] = 0;
    }
    return cnt;
}

//: 本方向的 `(四数, 活三数)`。有四分的方向不计活三。
inline void lineJudge(int8_t* cells, int n, int center, int me, int* f,
                      int* t) {
    uint32_t sets[32];
    const int nf = fourCount(cells, n, center, me, sets);
    if (nf > 0) {
        *f = nf;
        *t = 0;
        return;
    }
    *f = 0;
    *t = threeCount(cells, n, center, me, sets);
}

//: 取过 `idx` 的第 k 条**整条线**到 `cells`，并把"假设落下的那一子"写进去。
//: 取整条线（长度 ≤ 19）而不取 ±5 的小窗：长连判定要看到窗外，整条线天然把墙
//: 放在线端，没有边界特例。
inline void extractLine(const Board& bd, int idx, int k, int8_t* cells, int* n,
                        int* center, int player) {
    const tables::CellLinePos& cp = tables::cellLinePos[idx][k];
    const tables::LineView& lv = tables::lineViews[cp.lid];
    for (int i = 0; i < lv.length; ++i) {
        cells[i] = static_cast<int8_t>(bd.get(lv.cells[i]));
    }
    *n = lv.length;
    *center = cp.t;
    cells[*center] = static_cast<int8_t>(player);
}

//: 黑方刚落 `idx` 后的禁手种类。**不做占用校验**（调用方保证 `idx` 是空点，
//: 或刚落下的黑子）—— `classify` 走的正是"落子之后"这条路径。
inline Kind coreKind(const Board& bd, int idx) {
    int8_t lines[4][BOARD_SIZE];
    int lens[4], centers[4];
    for (int k = 0; k < 4; ++k) {
        extractLine(bd, idx, k, lines[k], &lens[k], &centers[k], 1);
    }
    // 第一步：恰好五连 —— 黑胜，此后不再看任何禁手。
    for (int k = 0; k < 4; ++k) {
        if (detail::runAt(lines[k], lens[k], centers[k], 1) == 5) {
            return Kind::NONE;
        }
    }
    // 第二步：长连。放在数四之前 —— 长连一旦成立，这一手与什么样的四/三已无
    // 关系（且此时它不可能同时成五，否则第一步就返回了）。
    for (int k = 0; k < 4; ++k) {
        if (detail::runAt(lines[k], lens[k], centers[k], 1) > 5) {
            return Kind::OVERLINE;
        }
    }
    // 第三步：数四与活三。**先把四个方向都数完再下结论** —— 边数边判会在
    // "两个方向各只有一个四"的地方漏判四四。
    int nFour = 0, nThree = 0;
    for (int k = 0; k < 4; ++k) {
        int f = 0, t = 0;
        detail::lineJudge(lines[k], lens[k], centers[k], 1, &f, &t);
        nFour += f;
        nThree += t;
    }
    if (nFour >= 2) return Kind::DOUBLE_FOUR;
    // nFour == 1 时是**四三** —— 黑方的正常胜形，不是禁手。
    if (nFour == 0 && nThree >= 2) return Kind::DOUBLE_THREE;
    return Kind::NONE;
}

}  // namespace detail

//: 在**空点** `idx` 落 `player` 后是否形成**恰好五连**（长连不算）。
inline bool fiveAt(const Board& bd, int idx, int player) {
    for (int k = 0; k < 4; ++k) {
        int8_t cells[BOARD_SIZE];
        int n = 0, center = 0;
        detail::extractLine(bd, idx, k, cells, &n, &center, player);
        if (detail::runAt(cells, n, center, player) == 5) return true;
    }
    return false;
}

//: 在**空点** `idx` 落子后的禁手种类。`player` 不是黑、或该点已有子时恒返回
//: `NONE`（白方无禁手）。**判定顺序就是规则本身**：恰好五连 → 长连 → 数四三。
inline Kind foulKind(const Board& bd, int idx, int player = 1) {
    if (player != 1) return Kind::NONE;
    if (idx < 0 || idx >= CELLS) return Kind::NONE;
    if (!bd.isEmpty(idx)) return Kind::NONE;
    return detail::coreKind(bd, idx);
}

//: 黑方刚落 `idx` 后这一手**有没有可能**是禁手（便宜预筛）。
//:
//: **必须在 `Board::make(idx, 1)` 之后调用** —— 它读 `Board::lineLv[0]` 的增量
//: 状态，那里已经反映了这一手。判据是两个必要条件：四四 ⟹ 至少一条过 idx 的
//: 线达到"四"（同线双四那条线是活四档，所以活四档记两分）；三三 ⟹ 至少两条
//: 线达到活三以上（**同线双三不存在**，故"两条线"完备）。多报无害、漏报有害。
inline bool hint(const Board& bd, int idx) {
    int nFour = 0, nThree = 0;
    for (int k = 0; k < 4; ++k) {
        const int lid = tables::cellLinePos[idx][k].lid;
        const int lv = bd.lineLv[0][lid];
        if (lv >= LV_FOUR_LIVE) nFour += 2;
        else if (lv == LV_FOUR) nFour += 1;
        else if (lv == LV_THREE_LIVE) nThree += 1;
    }
    if (nFour >= 2) return true;
    return nFour == 0 && nThree >= 2;
}

//: 黑方刚落 `idx`（`made` 是 `Board::make` 的返回值）之后的归类。
//:
//: **成五优先于全部禁手**：`made` 为真时先问"是不是恰好五连"，是就判胜、此后
//: 不看任何禁手。`made` 为真又凑不出恰好五连，那只能是长连（`make` 的返回值
//: 就是"过 idx 的某个五窗全黑"，不是 5 就是 ≥6，没有第三种）。
//:
//: ⚠️ 只在**黑方**的调用点使用（`me == 1`）；白方无禁手。
inline Outcome classify(const Board& bd, int idx, bool made) {
    if (made) {
        return fiveAt(bd, idx, 1) ? Outcome::WIN : Outcome::LOSE;
    }
    if (hint(bd, idx) && detail::coreKind(bd, idx) != Kind::NONE) {
        return Outcome::LOSE;
    }
    return Outcome::OK;
}

//: 把 `fivePoints` 的结果按禁手过滤 —— **黑方的长连点不是成五点**。
//: 对应 `engine_ban._fives_for`；`player` 不是黑、或 `mask` 为空时原样返回。
inline Bits361 fivesFor(const Board& bd, const Bits361& mask, int player) {
    if (player != 1 || mask.none()) return mask;
    Bits361 out = Bits361::zero();
    for (int idx = 0; idx < CELLS; ++idx) {
        if (mask.test(idx) && classify(bd, idx, true) == Outcome::WIN) {
            out.set(idx);
        }
    }
    return out;
}

//: 当前局面下黑方落子即构成禁手的空点（行优先序的线性格索引）。
//: 对应 `ban_rules.forbidden_points`；`tcp_server` 的 `bancheck` 与对账工具用它。
inline std::vector<int> forbiddenPoints(const Board& bd) {
    std::vector<int> out;
    for (int idx = 0; idx < CELLS; ++idx) {
        if (!bd.isEmpty(idx)) continue;
        if (foulKind(bd, idx) != Kind::NONE) out.push_back(idx);
    }
    return out;
}

#else  // !GOMOKU_BAN —— 空实现，free 版一个字节都不执行到

inline bool fiveAt(const Board&, int, int) { return false; }
inline Kind foulKind(const Board&, int, int = 1) { return Kind::NONE; }
inline bool hint(const Board&, int) { return false; }
inline Outcome classify(const Board&, int, bool) { return Outcome::OK; }
inline Bits361 fivesFor(const Board&, const Bits361& mask, int) { return mask; }
inline std::vector<int> forbiddenPoints(const Board&) { return {}; }

#endif

}  // namespace ban
}  // namespace gomoku

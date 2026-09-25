#include "board.h"

namespace gomoku {

// ==================== 位运算工具（走法排序与 VCF 用） ====================

bool hasFiveBits(const Bits361& bits) {
    for (int d = 0; d < 4; ++d) {
        const int s = DIR_STEPS[d];
        Bits361 t = bits;
        t &= Bits361::shr(bits, s);
        t &= Bits361::shr(bits, 2 * s);
        t &= Bits361::shr(bits, 3 * s);
        t &= Bits361::shr(bits, 4 * s);
        if ((t & tables::fiveStarts[d]).any()) return true;
    }
    return false;
}

namespace {

//: 每个方向上"窗口内不得有对方子"的起点掩码。
//:
//: ``_FIVE_STARTS[d] & ~(obits | obits>>s | ... | obits>>4s)`` —— 对手的连五
//: 起点集合。对手已经有五连时 `~oppWin` 会把相关起点全部抹掉，这正是
//: Python 版 `limit = _FIVE_STARTS[d] & ~opp_win` 的行为。
inline Bits361 windowLimit(int d, const Bits361& obits) {
    const int s = DIR_STEPS[d];
    Bits361 oppWin = obits;
    oppWin |= Bits361::shr(obits, s);
    oppWin |= Bits361::shr(obits, 2 * s);
    oppWin |= Bits361::shr(obits, 3 * s);
    oppWin |= Bits361::shr(obits, 4 * s);
    return tables::fiveStarts[d] & ~oppWin;
}

}  // namespace

Bits361 fivePoints(const Bits361& bits, const Bits361& obits) {
    const Bits361 occ = bits | obits;
    Bits361 out = Bits361::zero();
    for (int d = 0; d < 4; ++d) {
        const int s = DIR_STEPS[d];
        const Bits361 limit = windowLimit(d, obits);
        if (limit.none()) continue;
        const Bits361 b0 = bits;
        const Bits361 b1 = Bits361::shr(bits, s);
        const Bits361 b2 = Bits361::shr(bits, 2 * s);
        const Bits361 b3 = Bits361::shr(bits, 3 * s);
        const Bits361 b4 = Bits361::shr(bits, 4 * s);
        // 第 j 项 = 除 b_j 之外的四项全为 1（= 窗口内恰有 4 颗己方子，
        // 空位就在 j）→ 空点是成五点。
        out |= (b1 & b2 & b3 & b4 & limit);
        out |= Bits361::shl(b0 & b2 & b3 & b4 & limit, s);
        out |= Bits361::shl(b0 & b1 & b3 & b4 & limit, 2 * s);
        out |= Bits361::shl(b0 & b1 & b2 & b4 & limit, 3 * s);
        out |= Bits361::shl(b0 & b1 & b2 & b3 & limit, 4 * s);
    }
    return out & ~occ;
}

Bits361 hotPoints(const Bits361& bits, const Bits361& obits) {
    const Bits361 occ = bits | obits;
    Bits361 out = Bits361::zero();
    for (int d = 0; d < 4; ++d) {
        const int s = DIR_STEPS[d];
        const Bits361 b[5] = {
            bits,
            Bits361::shr(bits, s),
            Bits361::shr(bits, 2 * s),
            Bits361::shr(bits, 3 * s),
            Bits361::shr(bits, 4 * s),
        };
        const Bits361 limit = windowLimit(d, obits);
        if (limit.none()) continue;

        // pre[k] = b0 & ... & b{k-1}，suf[k] = b{k} & ... & b4
        Bits361 pre[6];
        Bits361 suf[6];
        const Bits361 ones = Bits361::all();
        pre[0] = ones;
        for (int k = 0; k < 5; ++k) pre[k + 1] = pre[k] & b[k];
        suf[5] = ones;
        for (int k = 4; k >= 0; --k) suf[k] = suf[k + 1] & b[k];
        // mid[a][c] = b{a+1} & ... & b{c-1}（a < c）
        Bits361 mid[5][5];
        for (int a = 0; a < 5; ++a) {
            Bits361 acc = ones;
            for (int c = a + 1; c < 5; ++c) {
                mid[a][c] = acc;
                acc &= b[c];
            }
        }

        Bits361 cells = Bits361::zero();
        // 恰 3 子（空位 a、c）：两空点都是四点
        for (int a = 0; a < 5; ++a) {
            if (pre[a].none()) continue;
            for (int c = a + 1; c < 5; ++c) {
                const Bits361 m = pre[a] & mid[a][c] & suf[c + 1] & limit;
                if (m.any()) {
                    cells |= Bits361::shl(m, a * s);
                    cells |= Bits361::shl(m, c * s);
                }
            }
        }
        // 恰 4 子（空位 g）：该空点是五点
        for (int g = 0; g < 5; ++g) {
            const Bits361 m = pre[g] & suf[g + 1] & limit;
            if (m.any()) cells |= Bits361::shl(m, g * s);
        }

        out |= cells & ~occ;
    }
    return out;
}

// ==================== Board ====================

Board::Board() {
    bBits = Bits361::zero();
    wBits = Bits361::zero();
    hash = 0;
    for (int i = 0; i < CELLS; ++i) neighborCount[i] = 0;
    candMask = Bits361::zero();
    nBlack = 0;
    nWhite = 0;
    for (int p = 0; p < 2; ++p) {
        for (int i = 0; i < N_LINES; ++i) {
            lineBits[p][i] = 0;
            lineLv[p][i] = LV_NONE;
        }
        scoreSum[p] = 0;
        // 初值是"112 条线全在 LV_NONE"，不是全零 —— 它必须自始至终等于
        // "等级为 lv 的线数"，增量维护才可能与全量重算逐字段相等。
        threatAgg[p][0] = N_LINES;
        for (int k = 1; k < 9; ++k) threatAgg[p][k] = 0;
        threatPower[p] = 0;
    }
}

bool Board::hasFive(int player) const { return hasFiveBits(bitsOf(player)); }

bool Board::make(int idx, int player) {
    const int wi = idx >> 6;
    const uint64_t bit = 1ULL << (idx & 63);
    if (player == 1) {
        bBits.w[wi] |= bit;
        ++nBlack;
    } else {
        wBits.w[wi] |= bit;
        ++nWhite;
    }
    hash ^= tables::zobrist[player - 1][idx];
    candMask.clearBit(idx);

    const Bits361 occ = bBits | wBits;
    const tables::NeighborList& nl = tables::neighbors[idx];
    for (int i = 0; i < nl.count; ++i) {
        const int j = nl.idx[i];
        const uint8_t v = static_cast<uint8_t>(neighborCount[j] + 1);
        neighborCount[j] = v;
        // 只在 0→1 时入候选。另外必须判空：一颗孤子自身 nc 为 0，邻近处
        // 补上一子时它的 nc 会变成 1，但它是占着的，不属于候选。
        if (v == 1 && !occ.test(j)) candMask.set(j);
    }

    // 增量线评分：只重算过该点的 4 条线。**两条线都要重算双方** —— 落一子
    // 除了形成己方棋型，还可能**打断**对手的跳活三，只更己方会让对手的分虚高。
    const int p = player - 1;
    for (int k = 0; k < 4; ++k) {
        const tables::CellLinePos& cp = tables::cellLinePos[idx][k];
        lineBits[p][cp.lid] |= 1u << cp.t;
        rescoreLine(cp.lid);
    }
    return makesFive(player, idx);
}

void Board::unmake(int idx, int player) {
    const int wi = idx >> 6;
    const uint64_t bit = 1ULL << (idx & 63);
    if (player == 1) {
        bBits.w[wi] &= ~bit;
        --nBlack;
    } else {
        wBits.w[wi] &= ~bit;
        --nWhite;
    }
    hash ^= tables::zobrist[player - 1][idx];

    const tables::NeighborList& nl = tables::neighbors[idx];
    for (int i = 0; i < nl.count; ++i) {
        const int j = nl.idx[i];
        const uint8_t v = static_cast<uint8_t>(neighborCount[j] - 1);
        neighborCount[j] = v;
        // v==0 即该格周围再没有棋子。若 j 正被占着，它的候选位本来就是 0，
        // 这里清一次是无害的空操作。
        if (v == 0) candMask.clearBit(j);
    }
    // 撤掉之后 idx 变空，若有邻居则回到候选
    if (neighborCount[idx]) candMask.set(idx);

    const int p = player - 1;
    for (int k = 0; k < 4; ++k) {
        const tables::CellLinePos& cp = tables::cellLinePos[idx][k];
        lineBits[p][cp.lid] &= ~(1u << cp.t);
        rescoreLine(cp.lid);
    }
}

void Board::rescoreLine(int lid) {
    const int length = tables::lineViews[lid].length;
    // 折差值而不是全量求和：全量要扫 112 条线，而一次落子只影响 4 条。
    //
    // `threatAgg` 连 `LV_NONE` 一起计数，不做"0 就跳过"的省事优化 ——
    // 那是错的：省掉之后 `threatAgg[p][0]` 的含义变成"曾经非零、现在归零过
    // 多少次"，不再等于"等级为 NONE 的线数"，而**增量状态必须是盘面的函数**，
    // 否则逐字段对照的 `diff` 就失去意义。
    for (int q = 0; q < 2; ++q) {
        const int oldLv = lineLv[q][lid];
        threatAgg[q][oldLv] -= 1;
        scoreSum[q] -= LINE_SCORES[oldLv];
        if (oldLv >= LV_THREE_LIVE) threatPower[q] -= 1;

        const int newLv =
            cachedLineLevel(lineBits[q][lid], lineBits[1 - q][lid], length);
        lineLv[q][lid] = static_cast<uint8_t>(newLv);
        threatAgg[q][newLv] += 1;
        scoreSum[q] += LINE_SCORES[newLv];
        if (newLv >= LV_THREE_LIVE) threatPower[q] += 1;
    }
}

Board Board::fromBits(const Bits361& b, const Bits361& w) {
    Board bd;
    bd.bBits = b;
    bd.wBits = w;
    bd.nBlack = b.count();
    bd.nWhite = w.count();

    uint64_t h = 0;
    forEachBitAscending(b, [&](int idx) { h ^= tables::zobrist[0][idx]; });
    forEachBitAscending(w, [&](int idx) { h ^= tables::zobrist[1][idx]; });
    bd.hash = h;

    const Bits361 occ = b | w;
    forEachBitAscending(occ, [&](int idx) {
        const tables::NeighborList& nl = tables::neighbors[idx];
        for (int i = 0; i < nl.count; ++i) ++bd.neighborCount[nl.idx[i]];
    });
    for (int idx = 0; idx < CELLS; ++idx) {
        if (bd.neighborCount[idx] && !occ.test(idx)) bd.candMask.set(idx);
    }

    // 线状态：逐子折进四条线（与 make 的更新路径一致），再统一重算等级。
    forEachBitAscending(b, [&](int idx) {
        for (int k = 0; k < 4; ++k) {
            const tables::CellLinePos& cp = tables::cellLinePos[idx][k];
            bd.lineBits[0][cp.lid] |= 1u << cp.t;
        }
    });
    forEachBitAscending(w, [&](int idx) {
        for (int k = 0; k < 4; ++k) {
            const tables::CellLinePos& cp = tables::cellLinePos[idx][k];
            bd.lineBits[1][cp.lid] |= 1u << cp.t;
        }
    });
    for (int lid = 0; lid < N_LINES; ++lid) bd.rescoreLine(lid);
    return bd;
}

Board Board::fromArray(const uint8_t* cells) {
    Bits361 b = Bits361::zero();
    Bits361 w = Bits361::zero();
    for (int i = 0; i < CELLS; ++i) {
        if (cells[i] == 1) b.set(i);
        else if (cells[i] == 2) w.set(i);
    }
    return fromBits(b, w);
}

int Board::candidateCount() const {
    if (candMask.none()) return stoneCount() < CELLS ? 1 : 0;
    return candMask.count();
}

void Board::candidates(int* out, int* n) const {
    if (candMask.none()) {
        if (stoneCount() < CELLS) {
            out[0] = CENTER_IDX;
            *n = 1;
        } else {
            *n = 0;
        }
        return;
    }
    int c = 0;
    forEachBitAscending(candMask, [&](int idx) { out[c++] = idx; });
    *n = c;
}

void Board::diff(const Board& o, const char** out, int* n) const {
    int c = 0;
    auto put = [&](const char* name) { out[c++] = name; };
    if (bBits != o.bBits) put("b_bits");
    if (wBits != o.wBits) put("w_bits");
    if (hash != o.hash) put("hash");
    if (candMask != o.candMask) put("cand_mask");
    if (nBlack != o.nBlack) put("_n_black");
    if (nWhite != o.nWhite) put("_n_white");
    bool ncDiff = false;
    for (int i = 0; i < CELLS; ++i) {
        if (neighborCount[i] != o.neighborCount[i]) { ncDiff = true; break; }
    }
    if (ncDiff) put("neighbor_count");
    bool lbDiff = false, lvDiff = false;
    for (int p = 0; p < 2 && !(lbDiff && lvDiff); ++p) {
        for (int i = 0; i < N_LINES; ++i) {
            if (lineBits[p][i] != o.lineBits[p][i]) lbDiff = true;
            if (lineLv[p][i] != o.lineLv[p][i]) lvDiff = true;
        }
    }
    if (lbDiff) put("line_bits");
    if (lvDiff) put("line_lv");
    if (scoreSum[0] != o.scoreSum[0] || scoreSum[1] != o.scoreSum[1]) {
        put("score_sum");
    }
    bool aggDiff = false, tpDiff = false;
    for (int p = 0; p < 2; ++p) {
        for (int k = 0; k < 9; ++k) {
            if (threatAgg[p][k] != o.threatAgg[p][k]) aggDiff = true;
        }
        if (threatPower[p] != o.threatPower[p]) tpDiff = true;
    }
    if (aggDiff) put("threat_agg");
    if (tpDiff) put("threat_power");
    *n = c;
}

}  // namespace gomoku

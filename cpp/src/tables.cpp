#include "tables.h"

#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <mutex>
#include <set>

#include "zobrist_table.inc"

namespace gomoku {
namespace tables {
namespace {

std::once_flag g_once;

//: 判 `(r, c)` 是否落在盘内。
inline bool inBoard(int r, int c) {
    return r >= 0 && r < BOARD_SIZE && c >= 0 && c < BOARD_SIZE;
}

void buildLines() {
    int lid = 0;
    int perDir[4] = {0, 0, 0, 0};
    int cellPosCount[CELLS] = {0};
    for (int d = 0; d < 4; ++d) {
        const int dr = DIRS[d][0], dc = DIRS[d][1];
        for (int r = 0; r < BOARD_SIZE; ++r) {
            for (int c = 0; c < BOARD_SIZE; ++c) {
                if (inBoard(r - dr, c - dc)) continue;   // 只从线首出发
                int len = 0;
                int rr = r, cc = c;
                while (inBoard(rr, cc)) {
                    const int idx = rr * BOARD_SIZE + cc;
                    lineViews[lid].cells[len] = static_cast<uint16_t>(idx);
                    cellLinePos[idx][cellPosCount[idx]++] =
                        CellLinePos{static_cast<uint8_t>(lid),
                                    static_cast<uint8_t>(len)};
                    ++len;
                    rr += dr;
                    cc += dc;
                }
                lineViews[lid].length = static_cast<uint8_t>(len);
                ++lid;
                ++perDir[d];
            }
        }
    }
    // 方向内计数必须是 19 / 19 / 37 / 37（横竖各 19 条全长线，两条对角各 37）。
    (void)perDir;
}

void buildLineWindows() {
    for (int L = 0; L <= BOARD_SIZE; ++L) {
        const int n = L >= 5 ? L - 4 : 0;
        lineWindows[L].count = static_cast<uint8_t>(n);
        for (int i = 0; i < n; ++i) {
            lineWindows[L].masks[i] = 0b11111u << i;
        }
    }
    for (int lid = 0; lid < N_LINES; ++lid) {
        lineMask[lid] = (1u << lineViews[lid].length) - 1u;
    }
    for (int L = 0; L <= BOARD_SIZE; ++L) {
        for (int b = 0; b < BOARD_SIZE; ++b) {
            uint32_t acc = 0;
            for (int i = 0; i < lineWindows[L].count; ++i) {
                const uint32_t win = lineWindows[L].masks[i];
                if (win >> b & 1u) acc |= win;
            }
            touchMask[L][b] = acc;
        }
    }
}

void buildWinMasks() {
    int used[CELLS] = {0};
    for (int d = 0; d < 4; ++d) {
        const int dr = DIRS[d][0], dc = DIRS[d][1];
        for (int r = 0; r < BOARD_SIZE; ++r) {
            for (int c = 0; c < BOARD_SIZE; ++c) {
                if (inBoard(r - dr, c - dc)) continue;
                // 沿这条线滑动 5 连窗口
                int rr = r, cc = c;
                while (inBoard(rr, cc)) {
                    const int tr = rr + dr * 4, tc = cc + dc * 4;
                    if (!inBoard(tr, tc)) break;
                    Bits361 m = Bits361::zero();
                    int wr = rr, wc = cc;
                    for (int t = 0; t < 5; ++t) {
                        m.set(wr * BOARD_SIZE + wc);
                        wr += dr;
                        wc += dc;
                    }
                    wr = rr;
                    wc = cc;
                    for (int t = 0; t < 5; ++t) {
                        const int idx = wr * BOARD_SIZE + wc;
                        winMasks[idx].masks[used[idx]++] = m;
                        wr += dr;
                        wc += dc;
                    }
                    rr += dr;
                    cc += dc;
                }
            }
        }
    }
    for (int i = 0; i < CELLS; ++i) {
        winMasks[i].count = static_cast<uint8_t>(used[i]);
    }
}

void buildNeighbors() {
    for (int idx = 0; idx < CELLS; ++idx) {
        const int r = idx / BOARD_SIZE, c = idx % BOARD_SIZE;
        int n = 0;
        for (int dr = -2; dr <= 2; ++dr) {
            for (int dc = -2; dc <= 2; ++dc) {
                if (dr == 0 && dc == 0) continue;
                const int rr = r + dr, cc = c + dc;
                if (!inBoard(rr, cc)) continue;
                neighbors[idx].idx[n++] =
                    static_cast<uint16_t>(rr * BOARD_SIZE + cc);
            }
        }
        neighbors[idx].count = static_cast<uint8_t>(n);
    }
}

void buildFiveStarts() {
    for (int d = 0; d < 4; ++d) {
        const int dr = DIRS[d][0], dc = DIRS[d][1];
        Bits361 m = Bits361::zero();
        for (int r = 0; r < BOARD_SIZE; ++r) {
            for (int c = 0; c < BOARD_SIZE; ++c) {
                if (inBoard(r + dr * 4, c + dc * 4)) {
                    m.set(r * BOARD_SIZE + c);
                }
            }
        }
        fiveStarts[d] = m;
    }
}

void buildZobrist() {
    for (int p = 0; p < 2; ++p) {
        for (int i = 0; i < CELLS; ++i) {
            zobrist[p][i] = ZOBRIST_TABLE[p][i];
        }
    }
}

void doInit() {
    // 静态存储已零初始化；这里只做一次填充。
    buildLines();
    buildLineWindows();
    buildWinMasks();
    buildNeighbors();
    buildFiveStarts();
    buildZobrist();
}

}  // namespace

void init() { std::call_once(g_once, doInit); }

bool verify(std::string* err) {
    init();
    auto fail = [&](const std::string& msg) {
        if (err) *err = msg;
        return false;
    };

    // ---- 1. 线号顺序：与 engine_local._build_lines 的字典序一致 ----
    //
    // 这一段同时替换掉"按步长反推方向"的写法：对角线方向的线长为 1..19
    // 全谱，而长度 1 的线（四个角）无法由相邻两格之差判方向 —— 那会让
    // 撇/捺各少算 2 条。这里直接照 Python 的循环重走一遍，既校验了内容
    // 也校验了**顺序**（顺序才是真正容易差一位的东西）。
    {
        int perDir[4] = {0, 0, 0, 0};
        int expectLid = 0;
        for (int d = 0; d < 4; ++d) {
            const int dr = DIRS[d][0], dc = DIRS[d][1];
            for (int r = 0; r < BOARD_SIZE; ++r) {
                for (int c = 0; c < BOARD_SIZE; ++c) {
                    if (r - dr >= 0 && r - dr < BOARD_SIZE &&
                        c - dc >= 0 && c - dc < BOARD_SIZE) {
                        continue;
                    }
                    const int len = lineViews[expectLid].length;
                    int rr = r, cc = c, t = 0;
                    while (rr >= 0 && rr < BOARD_SIZE &&
                           cc >= 0 && cc < BOARD_SIZE) {
                        if (lineViews[expectLid].cells[t] != rr * BOARD_SIZE + cc) {
                            char buf[160];
                            std::snprintf(buf, sizeof(buf),
                                          "线 %d 的第 %d 格不是 (%d,%d)",
                                          expectLid, t, rr, cc);
                            return fail(buf);
                        }
                        ++t; rr += dr; cc += dc;
                    }
                    if (t != len) {
                        char buf[160];
                        std::snprintf(buf, sizeof(buf),
                                      "线 %d 长度 %d 与实际 %d 不符",
                                      expectLid, len, t);
                        return fail(buf);
                    }
                    ++expectLid;
                    ++perDir[d];
                }
            }
        }
        if (expectLid != N_LINES) {
            char buf[128];
            std::snprintf(buf, sizeof(buf),
                          "线总数应为 %d，实为 %d", N_LINES, expectLid);
            return fail(buf);
        }
        if (perDir[0] != 19 || perDir[1] != 19 ||
            perDir[2] != 37 || perDir[3] != 37) {
            char buf[160];
            std::snprintf(buf, sizeof(buf),
                          "各方向的线数应为 19/19/37/37，实为 %d/%d/%d/%d",
                          perDir[0], perDir[1], perDir[2], perDir[3]);
            return fail(buf);
        }
    }

    // ---- 3. cellLinePos 与线表互为逆映射；每格恰属 4 条线 ----
    {
        int seen[CELLS] = {0};
        for (int lid = 0; lid < N_LINES; ++lid) {
            for (int t = 0; t < lineViews[lid].length; ++t) {
                const int idx = lineViews[lid].cells[t];
                bool ok = false;
                for (int k = 0; k < 4; ++k) {
                    if (cellLinePos[idx][k].lid == lid &&
                        cellLinePos[idx][k].t == t) {
                        ok = true;
                        break;
                    }
                }
                if (!ok) {
                    char buf[160];
                    std::snprintf(buf, sizeof(buf),
                                  "格 %d 的 cellLinePos 缺少 (线 %d, 位置 %d)",
                                  idx, lid, t);
                    return fail(buf);
                }
                ++seen[idx];
            }
        }
        for (int idx = 0; idx < CELLS; ++idx) {
            if (seen[idx] != 4) {
                char buf[160];
                std::snprintf(buf, sizeof(buf),
                              "格 %d 应属于 4 条线，实为 %d", idx, seen[idx]);
                return fail(buf);
            }
        }
    }

    // ---- 4. 窗口表：长度与 bit 宽度 ----
    {
        for (int L = 0; L <= BOARD_SIZE; ++L) {
            const int n = (L >= 5 ? L - 4 : 0);
            if (lineWindows[L].count != n) {
                char buf[128];
                std::snprintf(buf, sizeof(buf),
                              "长度 %d 的窗口数应为 %d，实为 %d",
                              L, n, lineWindows[L].count);
                return fail(buf);
            }
            for (int i = 0; i < n; ++i) {
                if (lineWindows[L].masks[i] != (0b11111u << i)) {
                    char buf[128];
                    std::snprintf(buf, sizeof(buf),
                                  "长度 %d 的第 %d 个窗口掩码不对", L, i);
                    return fail(buf);
                }
            }
        }
        // touchMask 独立地用"连续段"公式验一遍：包含 b 的窗口并集必然是
        // [max(0,b-4), min(w-1,b+4)]。与上面按窗口逐项求和的写法互为对照，
        // 两者不一致说明窗口表本身有问题。
        for (int L = 0; L <= BOARD_SIZE; ++L) {
            for (int b = 0; b < BOARD_SIZE; ++b) {
                uint32_t expect = 0;
                if (L >= 5 && b < L) {
                    const int lo = std::max(0, b - 4);
                    const int hi = std::min(L - 1, b + 4);
                    expect = ((1u << (hi - lo + 1)) - 1u) << lo;
                }
                if (touchMask[L][b] != expect) {
                    char buf[160];
                    std::snprintf(buf, sizeof(buf),
                                  "touchMask[%d][%d] 应为 %#x，实为 %#x",
                                  L, b, expect, touchMask[L][b]);
                    return fail(buf);
                }
            }
        }
        for (int idx = 0; idx < CELLS; ++idx) {
            if (winMasks[idx].count == 0 || winMasks[idx].count > 20) {
                char buf[128];
                std::snprintf(buf, sizeof(buf),
                              "格 %d 的窗口数非法: %d", idx, winMasks[idx].count);
                return fail(buf);
            }
            for (int i = 0; i < winMasks[idx].count; ++i) {
                const Bits361& m = winMasks[idx].masks[i];
                if (m.count() != 5) {
                    char buf[128];
                    std::snprintf(buf, sizeof(buf),
                                  "格 %d 的第 %d 个窗口不是 5 格", idx, i);
                    return fail(buf);
                }
                if (!m.test(idx)) {
                    char buf[128];
                    std::snprintf(buf, sizeof(buf),
                                  "格 %d 的第 %d 个窗口不含它自己", idx, i);
                    return fail(buf);
                }
            }
        }
    }

    // ---- 5. 邻居表 ----
    {
        for (int idx = 0; idx < CELLS; ++idx) {
            const int r = idx / BOARD_SIZE, c = idx % BOARD_SIZE;
            if (neighbors[idx].count == 0) {
                char buf[128];
                std::snprintf(buf, sizeof(buf), "格 %d 没有邻居", idx);
                return fail(buf);
            }
            for (int i = 0; i < neighbors[idx].count; ++i) {
                const int j = neighbors[idx].idx[i];
                if (j == idx) {
                    char buf[128];
                    std::snprintf(buf, sizeof(buf), "格 %d 的邻居含自身", idx);
                    return fail(buf);
                }
                const int jr = j / BOARD_SIZE, jc = j % BOARD_SIZE;
                const int d = std::max(std::abs(jr - r), std::abs(jc - c));
                if (d < 1 || d > 2) {
                    char buf[128];
                    std::snprintf(buf, sizeof(buf),
                                  "格 %d 的邻居 %d 距离不是 1..2", idx, j);
                    return fail(buf);
                }
            }
        }
        // 对称性：j ∈ N(i) ⟺ i ∈ N(j)
        for (int i = 0; i < CELLS; ++i) {
            for (int k = 0; k < neighbors[i].count; ++k) {
                const int j = neighbors[i].idx[k];
                bool back = false;
                for (int t = 0; t < neighbors[j].count; ++t) {
                    if (neighbors[j].idx[t] == i) { back = true; break; }
                }
                if (!back) {
                    char buf[128];
                    std::snprintf(buf, sizeof(buf),
                                  "邻居关系不对称: %d -> %d", i, j);
                    return fail(buf);
                }
            }
        }
    }

    // ---- 6. fiveStarts：每个方向的起点数必须是 15*19 或 19*15 之类 ----
    {
        for (int d = 0; d < 4; ++d) {
            int expect = 0;
            const int dr = DIRS[d][0], dc = DIRS[d][1];
            for (int r = 0; r < BOARD_SIZE; ++r) {
                for (int c = 0; c < BOARD_SIZE; ++c) {
                    if (r + dr * 4 >= 0 && r + dr * 4 < BOARD_SIZE &&
                        c + dc * 4 >= 0 && c + dc * 4 < BOARD_SIZE) {
                        ++expect;
                    }
                }
            }
            if (fiveStarts[d].count() != expect) {
                char buf[128];
                std::snprintf(buf, sizeof(buf),
                              "方向 %d 的 fiveStarts 应为 %d 位，实为 %d",
                              d, expect, fiveStarts[d].count());
                return fail(buf);
            }
        }
    }

    // ---- 7. Zobrist：表内互不相同，两表无交集 ----
    {
        for (int p = 0; p < 2; ++p) {
            std::set<uint64_t> s;
            for (int i = 0; i < CELLS; ++i) {
                if (!s.insert(zobrist[p][i]).second) {
                    char buf[128];
                    std::snprintf(buf, sizeof(buf),
                                  "Zobrist 表 %d 的第 %d 项与前面重复", p, i);
                    return fail(buf);
                }
                if (zobrist[p][i] == 0) {
                    char buf[128];
                    std::snprintf(buf, sizeof(buf),
                                  "Zobrist 表 %d 的第 %d 项为 0", p, i);
                    return fail(buf);
                }
            }
        }
        std::set<uint64_t> s0, s1;
        for (int i = 0; i < CELLS; ++i) { s0.insert(zobrist[0][i]); }
        for (int i = 0; i < CELLS; ++i) {
            if (s0.count(zobrist[1][i])) {
                char buf[128];
                std::snprintf(buf, sizeof(buf),
                              "两张 Zobrist 表在格 %d 上撞值", i);
                return fail(buf);
            }
        }
    }

    // ---- 8. Bits361 的移位语义（与 Python 大整数逐例对照）----
    {
        // 抽几个代表性位置：字边界、跨字、361 位边缘
        const int probes[][2] = {{0, 0}, {0, 1}, {0, 18}, {0, 19}, {0, 20},
                                 {63, 1}, {64, 1}, {319, 1}, {320, 1},
                                 {360, 1}, {180, 19}, {360, 20},
                                 {0, 76}, {100, 77}, {200, 17}};
        for (const auto& pr : probes) {
            const int idx = pr[0], s = pr[1];
            if (idx < 0 || idx >= CELLS || s <= 0) continue;
            const Bits361 one = Bits361::bit(idx);
            const Bits361 r = Bits361::shr(one, s);
            const int want = idx - s;
            if (want < 0) {
                if (r.any()) {
                    char buf[160];
                    std::snprintf(buf, sizeof(buf),
                                  "shr: 位 %d 右移 %d 应为空", idx, s);
                    return fail(buf);
                }
            } else {
                if (r.count() != 1 || !r.test(want)) {
                    char buf[160];
                    std::snprintf(buf, sizeof(buf),
                                  "shr: 位 %d 右移 %d 应落到 %d", idx, s, want);
                    return fail(buf);
                }
            }
            const Bits361 l = Bits361::shl(one, s);
            const int wantL = idx + s;
            if (wantL >= CELLS) {
                if (l.any()) {
                    char buf[160];
                    std::snprintf(buf, sizeof(buf),
                                  "shl: 位 %d 左移 %d 应为空", idx, s);
                    return fail(buf);
                }
            } else {
                if (l.count() != 1 || !l.test(wantL)) {
                    char buf[160];
                    std::snprintf(buf, sizeof(buf),
                                  "shl: 位 %d 左移 %d 应落到 %d", idx, s, wantL);
                    return fail(buf);
                }
            }
        }
        if (Bits361::all().count() != CELLS) {
            return fail("Bits361::all() 的位数不是 361");
        }
    }

    return true;
}

}  // namespace tables
}  // namespace gomoku

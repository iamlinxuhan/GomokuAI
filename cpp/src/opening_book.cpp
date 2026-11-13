// Gomocup 开局书：尺寸平移 + D4 展开 + 根节点 O(1) 查询。
//
// Python 镜像在 gomocup_book.py，两边逐行对应：
//
//   * 平移：col = n/2 + (x - 7)、row = n/2 + (y - 7)；任一子越界整条线丢弃；
//   * 前缀：前 k 手的局面 → 第 k+1 手；最后局面 → 本引擎算出的应手；
//   * 走子方：走了 k 手之后轮到黑 iff k 为偶数；
//   * D4 的 8 个变换与 xform() 逐行相同；
//   * 冲突决胜：同一键落到不同应手时取**最小线性格索引**。
//
// 任何一处两边不一致，tests/test_gomocup_book.py 的跨语言对拍会报出来。

#include "opening_book.h"

#include <algorithm>
#include <cstdio>
#include <unordered_map>
#include <utility>
#include <vector>

#include "board.h"
#include "constants.h"
#include "gomocup_book_data.h"

namespace gomoku {
namespace {

struct Built {
    std::unordered_map<uint64_t, int> table;
    int lines = 0;
};

//: 15×15 原生坐标 → 本二进制尺寸的 (row, col)。越界返回 false。
bool translate(int x, int y, int* r, int* c) {
    const int m = BOARD_SIZE / 2;
    const int cc = m + (x - 7);
    const int rr = m + (y - 7);
    if (rr < 0 || rr >= BOARD_SIZE || cc < 0 || cc >= BOARD_SIZE) return false;
    *r = rr;
    *c = cc;
    return true;
}

//: D4 的 8 个变换。**必须与 gomocup_book.py 的 _xform 逐行相同。**
void xform(int t, int r, int c, int* rr, int* cc) {
    switch (t) {
        case 0: *rr = r; *cc = c; break;                        // 恒等
        case 1: *rr = c; *cc = BOARD_SIZE - 1 - r; break;       // 顺 90°
        case 2: *rr = BOARD_SIZE - 1 - r;
                *cc = BOARD_SIZE - 1 - c; break;                // 180°
        case 3: *rr = BOARD_SIZE - 1 - c; *cc = r; break;       // 270°
        case 4: *rr = r; *cc = BOARD_SIZE - 1 - c; break;       // 水平镜像
        case 5: *rr = BOARD_SIZE - 1 - r; *cc = c; break;       // 垂直镜像
        case 6: *rr = c; *cc = r; break;                        // 主对角转置
        default: *rr = BOARD_SIZE - 1 - c;
                 *cc = BOARD_SIZE - 1 - r; break;               // 副对角转置
    }
}

Built build() {
    Built out;
    out.table.reserve(4096);
    for (int e = 0; e < GOMOCUP_RAW_COUNT; ++e) {
        const GomocupRawEntry& ent = GOMOCUP_RAW[e];
        int rs[32], cs[32];
        int rr = 0, rc = 0;
        bool ok = translate(ent.response[0], ent.response[1], &rr, &rc);
        for (int i = 0; i < ent.n && ok; ++i) {
            if (!translate(ent.moves[i][0], ent.moves[i][1], &rs[i], &cs[i])) {
                ok = false;
            }
        }
        if (!ok) continue;                       // 这条线在本尺寸上放不下
        ++out.lines;

        for (int k = 1; k <= ent.n; ++k) {
            const int mr = (k < ent.n) ? rs[k] : rr;
            const int mc = (k < ent.n) ? cs[k] : rc;
            const int side = (k % 2 == 0) ? 1 : 2;
            for (int t = 0; t < 8; ++t) {
                Board bd;
                bool legal = true;
                for (int i = 0; i < k && legal; ++i) {
                    int tr, tc;
                    xform(t, rs[i], cs[i], &tr, &tc);
                    const int idx = tr * BOARD_SIZE + tc;
                    if (!bd.isEmpty(idx)) { legal = false; break; }
                    bd.make(idx, (i % 2 == 0) ? 1 : 2);
                }
                if (!legal) continue;
                int tr, tc;
                xform(t, mr, mc, &tr, &tc);
                const int idx = tr * BOARD_SIZE + tc;
                const uint64_t key =
                    bd.hash ^ (side == 1 ? 0ULL : TT_SALT_W);
                auto it = out.table.find(key);
                // 冲突决胜：最小线性格索引。Python 侧逐字相同。
                if (it == out.table.end() || idx < it->second) {
                    out.table[key] = idx;
                }
            }
        }
    }
    return out;
}

const Built& built() {
    static const Built b = build();
    return b;
}

}  // namespace

int openingBookLookup(uint64_t key) {
    const Built& b = built();
    auto it = b.table.find(key);
    return it == b.table.end() ? -1 : it->second;
}

std::size_t openingBookSize() { return built().table.size(); }

int openingBookLines() { return built().lines; }

void openingBookDump() {
    const Built& b = built();
    std::vector<std::pair<uint64_t, int>> items(b.table.begin(), b.table.end());
    std::sort(items.begin(), items.end());
    for (const auto& kv : items) {
        std::printf("%016llX %d\n",
                    static_cast<unsigned long long>(kv.first), kv.second);
    }
}

}  // namespace gomoku

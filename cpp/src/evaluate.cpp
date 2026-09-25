#include "evaluate.h"

#include "board.h"
#include "tables.h"

namespace gomoku {
namespace {

inline int popcount32(uint32_t x) { return __builtin_popcount(x); }

//: `m & -m` 的 uint32 版本（Python 的 `_mask_cells` 用的就是它）。
inline uint32_t lowestBit(uint32_t m) {
    return m & static_cast<uint32_t>(-static_cast<int32_t>(m));
}

//: ``(F, 是否有五)``：``F`` 是"落子即连成五"的空点**集合**（位图）。
//:
//: **是集合不是窗口计数。** 计数会把 `XXXX_XXXX` 中间那个点算两次（左右两个
//: 窗口各含 4 颗子），把一个冲四误判成活四 —— 而活四是必胜、冲四不是。
uint32_t completions(uint32_t my, uint32_t opp, int w, bool* five) {
    const tables::LineWindows& lw = tables::lineWindows[w];
    uint32_t comp = 0;
    for (int i = 0; i < lw.count; ++i) {
        const uint32_t win = lw.masks[i];
        if (win & opp) continue;
        const uint32_t freeCells = win & ~my;
        const int n = popcount32(freeCells);
        if (n == 0) {          // 整窗皆我 = 五连
            *five = true;
            return 0;
        }
        if (n == 1) comp |= freeCells;
    }
    *five = false;
    return comp;
}

//: 在 `cand` 里补 `k` 子（`k <= 2`），能得到的最大 `|F|`。
//:
//: 上限截到 2 —— 判活四只需要"至少两个成五点"，再大的值没有意义，早点收敛
//: 能让短路退出更早生效。
int maxFAfter(uint32_t my, uint32_t opp, int w, uint32_t cand, int k) {
    int best = 0;
    uint32_t m = cand;
    while (m) {
        const uint32_t low = lowestBit(m);
        m ^= low;
        const uint32_t my2 = my | low;
        if (k == 1) {
            bool five = false;
            const uint32_t f = completions(my2, opp, w, &five);
            if (five) return 3;
            const int n = popcount32(f);
            if (n > best) best = n;
            if (best >= 2) return best;
        } else {
            // 第二子只需考虑"能与第一子一起进入某个窗口"的空点。这一条是
            // **语义相关**的（Python 里也是这么写的），不是可以顺手去掉的
            // 性能优化：去掉之后 `cand` 里那些永远进不了窗口的点会被当成
            // 可分第二子。
            const int sub = maxFAfter(
                my2, opp, w,
                cand & ~low & tables::touchMask[w][__builtin_ctz(low)], 1);
            if (sub > best) best = sub;
            if (best >= 2) return best;
        }
    }
    return best;
}

//: 线评分缓存。键是**压缩后的段**，不是整条线 —— 这才是命中的来源。
//:
//: **直接映射表，不是哈希容器。** Python 用 dict（精确、无冲突），这里用
//: 固定大小的直接映射数组：槽位被别的键占用时只是**当次未命中**，值照常
//: 算出来再覆盖进去，所以缓存无论怎么冲突都不会影响结果 —— 它只是"算过的
//: 别再算"。换成 unordered_map 会在每个 `make` 的 8 次查询上引入分配与
//: 指针追逐，而这个是内存里的一段连续数组。
//:
//: 键 0 恒为"空槽"：`lineLevel` 只在 `my != 0` 时被调用，`my` 占键的低 19 位，
//: 所以真实键永远非零。这条不变式是"不必额外存有效位"的全部依据。
constexpr uint32_t CACHE_BITS = 18;
constexpr uint32_t CACHE_SIZE = 1u << CACHE_BITS;
constexpr uint32_t CACHE_MASK = CACHE_SIZE - 1;

struct CacheEntry {
    uint64_t key;
    uint8_t lv;
};

CacheEntry g_cache[CACHE_SIZE];        // 静态零初始化 → 全部 key == 0

inline uint64_t mix(uint64_t x) {
    x ^= x >> 33;
    x *= 0xff51afd7ed558ccdULL;
    x ^= x >> 33;
    x *= 0xc4ceb9fe1a85ec53ULL;
    x ^= x >> 33;
    return x;
}

}  // namespace

Segment segment(uint32_t my, uint32_t opp, int length) {
    if (!my) return Segment{0, 0, 0};
    const int lo = __builtin_ctz(my);
    const int hi = 31 - __builtin_clz(my);
    int a = lo - PAD;
    int b = hi + PAD;
    if (a < 0) a = 0;
    if (b > length - 1) b = length - 1;
    const int w = b - a + 1;
    const uint32_t mask = (1u << w) - 1u;
    return Segment{(my >> a) & mask, (opp >> a) & mask, w};
}

int lineLevel(uint32_t my, uint32_t opp, int w) {
    if (!my || w < 5) return LV_NONE;

    bool five = false;
    const uint32_t comp = completions(my, opp, w, &five);
    if (five) return LV_FIVE;
    const int n = popcount32(comp);
    if (n >= 2) return LV_FOUR_LIVE;
    if (n == 1) return LV_FOUR;

    // 所有窗口的并集 == (1<<w)-1（窗口是 5 连连续段，且 w>=5 时覆盖每一位）。
    uint32_t cand = 0;
    const tables::LineWindows& lw = tables::lineWindows[w];
    for (int i = 0; i < lw.count; ++i) cand |= lw.masks[i];
    cand &= ~(my | opp);
    if (!cand) return LV_ONE;      // 全被堵死：给底分（表里没有"眠一"档）

    // Python 把 `_max_f_after(..., 1)` 算了两次（`>= 2` 与 `>= 1` 各一次）。
    // 它是纯函数，这里算一次即可 —— 注意两次调用之间**没有任何状态变化**，
    // 所以合并与逐次调用逐位等价。
    const int f1 = maxFAfter(my, opp, w, cand, 1);
    if (f1 >= 2) return LV_THREE_LIVE;
    if (f1 >= 1) return LV_THREE_SLEEP;
    const int f2 = maxFAfter(my, opp, w, cand, 2);
    if (f2 >= 2) return LV_TWO_LIVE;
    if (f2 >= 1) return LV_TWO_SLEEP;
    return LV_ONE;
}

int cachedLineLevel(uint32_t my, uint32_t opp, int length) {
    if (!my) return LV_NONE;
    const Segment s = segment(my, opp, length);
    if (!s.my) return LV_NONE;
    const uint64_t key = static_cast<uint64_t>(s.my) |
                         (static_cast<uint64_t>(s.opp) << 19) |
                         (static_cast<uint64_t>(s.w) << 38);
    CacheEntry& e = g_cache[mix(key) & CACHE_MASK];
    if (e.key == key) return e.lv;
    const int lv = lineLevel(s.my, s.opp, s.w);
    e.key = key;
    e.lv = static_cast<uint8_t>(lv);
    return lv;
}

void clearLineCache() {
    for (uint32_t i = 0; i < CACHE_SIZE; ++i) {
        g_cache[i].key = 0;
        g_cache[i].lv = 0;
    }
}

int64_t comboBonus(const int32_t* agg) {
    const int32_t fours = agg[LV_FOUR] + agg[LV_FOUR_LIVE];
    if (fours >= 2) return BONUS_DOUBLE_FOUR;
    if (fours && agg[LV_THREE_LIVE]) return BONUS_FOUR_THREE;
    if (agg[LV_THREE_LIVE] >= 2) return BONUS_DOUBLE_THREE;
    return 0;
}

int32_t evaluate(const Board& b, int me) {
    const int p = me - 1, q = 2 - me;
    const int64_t v = b.scoreSum[p] + comboBonus(b.threatAgg[p]) -
                      b.scoreSum[q] - comboBonus(b.threatAgg[q]);
    if (v > STATIC_MAX) return STATIC_MAX;
    if (v < -STATIC_MAX) return -STATIC_MAX;
    return static_cast<int32_t>(v);
}

}  // namespace gomoku

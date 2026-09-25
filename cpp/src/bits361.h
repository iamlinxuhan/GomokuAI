// 361 位位图 —— Python 大整数的逐位替换。
//
// 位 i 对应 ``(r, c) = divmod(i, BOARD_SIZE)``，与 engine_local 完全一致。
// 6 个 uint64：字 0..4 各 64 位，字 5 只用低 41 位（320+41 = 361）。
//
// **为什么不用 std::bitset<361>**：本引擎的热路径需要 `>>`（`_five_points` /
// `_hot_points` 每节点各做 4 组、每组 4 次移位）、`& -` 语义的"取最低位"、
// 以及跨 64 位边界的任意移位。`std::bitset` 的 `>>` 返回的是新对象但底层实现
// 是逐位搬移，而这里需要的是**字对齐的整字移位**；自己写 6 个字反而更短更快，
// 也更容易与 Python 的语义逐条对上。
//
// **所有 `~` 都必须带 361 位掩码。** Python 的 `~0` 是无限个 1，`~x` 对任意
// 非负大整数都成立；C++ 的 `~` 只有 64 位。`_five_points` / `_hot_points` 里的
// `~0` 是"这一项不计入”，语义上是"全 1"，所以这里提供 `all()` 并让
// `operator~` 主动掩掉高位 —— 漏掉这一步会让 361 位之外的位混进来，而
// 结果最终还要 `& ~occ`，症状会表现为"某些点被误判成四/五点"而非崩溃。

#pragma once

#include <cstdint>

#include "constants.h"

namespace gomoku {

struct Bits361 {
    static constexpr int WORDS = 6;
    static constexpr int BITS = CELLS;                  // 361
    //: 字 5 的有效位数：361 - 5*64 = 41
    static constexpr int TOP_BITS = BITS - 64 * (WORDS - 1);
    static constexpr uint64_t TOP_MASK = (uint64_t(1) << TOP_BITS) - 1;

    uint64_t w[WORDS];

    // ------------------------------------------------------------ 构造

    static Bits361 zero() { return Bits361{{0, 0, 0, 0, 0, 0}}; }
    static Bits361 all() {
        return Bits361{{~0ULL, ~0ULL, ~0ULL, ~0ULL, ~0ULL, TOP_MASK}};
    }
    static Bits361 bit(int idx) {
        Bits361 r = zero();
        r.w[idx >> 6] = 1ULL << (idx & 63);
        return r;
    }

    // ------------------------------------------------------------ 查询

    bool none() const {
        uint64_t acc = 0;
        for (int i = 0; i < WORDS; ++i) acc |= w[i];
        return acc == 0;
    }
    bool any() const { return !none(); }
    explicit operator bool() const { return any(); }

    int count() const {
        int n = 0;
        for (int i = 0; i < WORDS; ++i) n += __builtin_popcountll(w[i]);
        return n;
    }

    bool test(int idx) const { return (w[idx >> 6] >> (idx & 63)) & 1ULL; }

    //: 最低位置 1 的位号；全零返回 -1。对应 Python 的 `m & -m` + `bit_length`。
    int lowestIndex() const {
        for (int i = 0; i < WORDS; ++i) {
            if (w[i]) return (i << 6) + __builtin_ctzll(w[i]);
        }
        return -1;
    }
    //: 最高位置 1 的位号；全零返回 -1。
    int highestIndex() const {
        for (int i = WORDS - 1; i >= 0; --i) {
            if (w[i]) return (i << 6) + 63 - __builtin_clzll(w[i]);
        }
        return -1;
    }

    bool operator==(const Bits361& o) const {
        for (int i = 0; i < WORDS; ++i) if (w[i] != o.w[i]) return false;
        return true;
    }
    bool operator!=(const Bits361& o) const { return !(*this == o); }

    // ------------------------------------------------------------ 变更

    Bits361& set(int idx) { w[idx >> 6] |= 1ULL << (idx & 63); return *this; }
    Bits361& clearBit(int idx) { w[idx >> 6] &= ~(1ULL << (idx & 63)); return *this; }

    Bits361& operator|=(const Bits361& o) {
        for (int i = 0; i < WORDS; ++i) w[i] |= o.w[i];
        return *this;
    }
    Bits361& operator&=(const Bits361& o) {
        for (int i = 0; i < WORDS; ++i) w[i] &= o.w[i];
        return *this;
    }
    Bits361& operator^=(const Bits361& o) {
        for (int i = 0; i < WORDS; ++i) w[i] ^= o.w[i];
        return *this;
    }
    Bits361 operator|(const Bits361& o) const { Bits361 r = *this; r |= o; return r; }
    Bits361 operator&(const Bits361& o) const { Bits361 r = *this; r &= o; return r; }
    Bits361 operator^(const Bits361& o) const { Bits361 r = *this; r ^= o; return r; }

    //: 按位取反，**掩到 361 位**（见文件头）。
    Bits361 operator~() const {
        Bits361 r;
        for (int i = 0; i < WORDS; ++i) r.w[i] = ~w[i];
        r.w[WORDS - 1] &= TOP_MASK;
        return r;
    }

    // ------------------------------------------------------------ 移位

    //: 右移 s 位（`s >= 0`），对应 Python 的 `m >> s`。
    static Bits361 shr(const Bits361& a, int s) {
        Bits361 r = zero();
        const int ws = s >> 6, bs = s & 63;
        for (int i = 0; i + ws < WORDS; ++i) {
            uint64_t v = a.w[i + ws] >> bs;         // bs 可为 0
            if (bs && i + ws + 1 < WORDS) v |= a.w[i + ws + 1] << (64 - bs);
            r.w[i] = v;
        }
        return r;
    }

    //: 左移 s 位（`s >= 0`），对应 Python 的 `m << s`。
    static Bits361 shl(const Bits361& a, int s) {
        Bits361 r = zero();
        const int ws = s >> 6, bs = s & 63;
        for (int i = WORDS - 1; i - ws >= 0; --i) {
            uint64_t v = a.w[i - ws] << bs;          // bs 可为 0
            if (bs && i - ws - 1 >= 0) v |= a.w[i - ws - 1] >> (64 - bs);
            r.w[i] = v;
        }
        r.w[WORDS - 1] &= TOP_MASK;
        return r;
    }
};

//: `bits` 是否包含 `m` 的全部置位（即 `(bits & m) == m`）。
//: 对应 Python 的 `bits & m == m`。
inline bool containsMask(const Bits361& bits, const Bits361& m) {
    for (int i = 0; i < Bits361::WORDS; ++i) {
        if ((bits.w[i] & m.w[i]) != m.w[i]) return false;
    }
    return true;
}

//: 按 **升序**遍历置位。顺序不是细节：它决定了 `candidates()`、着法排序的
//: head/tail、`vcf` 的候选顺序 —— 与 Python 的 `_mask_cells`（`m & -m` 取最低
//: 位）逐位一致。**不要先收集成 vector 再排序**，逐字升序就是这个函数。
template <typename F>
inline void forEachBitAscending(const Bits361& b, F&& f) {
    for (int wi = 0; wi < Bits361::WORDS; ++wi) {
        uint64_t m = b.w[wi];
        while (m) {
            const int bit = __builtin_ctzll(m);
            m &= m - 1;                 // 清掉最低位
            f((wi << 6) + bit);
        }
    }
}

static_assert(sizeof(Bits361) == 48, "Bits361 应为 6 个 uint64，无填充");

}  // namespace gomoku

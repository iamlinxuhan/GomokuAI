// 位运算内建函数的跨编译器封装。
//
// GCC / Clang 给的是 `__builtin_popcountll` / `__builtin_ctzll` /
// `__builtin_clzll`，MSVC 一个都没有 —— 它在 `<intrin.h>` 里给的是
// `__popcnt64` / `_BitScanForward64` / `_BitScanReverse64`，后两个还是
// "置进位标志 + 出参" 的老式签名（`unsigned long*`），不能直接当函数用。
// 这个头把两套收敛成同一组名字，别处不再直接写 `__builtin_*`。
//
// **GCC/Clang 那条分支展开出来逐字就是原来的 `__builtin_*`** —— 一行汇编都
// 不变，Linux 侧的行为与性能不受这次移植影响。
//
// 移植时踩到的：这套代码在 Windows 上从没编译过，第一次跑 MSVC 就是
// `error C3861: '__builtin_popcountll': identifier not found`。这个头是为了
// 让"再漏一个"这件事不可能发生 —— 内建函数在这里集中一次。

#pragma once

#include <cstdint>

#if defined(_MSC_VER)
#include <intrin.h>
#endif

namespace gomoku {

// ---------------------------------------------------------------- 位数统计

//: 64 位里 1 的个数。
//: MSVC 走 `__popcnt64`（POPCNT 指令，需 SSE4.2 —— x64 上 2008 年之后的
//: CPU 都有；这是 MSVC 文档里推荐的做法，不是自选）。
inline int popcount64(uint64_t x) {
#if defined(_MSC_VER)
    return static_cast<int>(__popcnt64(x));
#else
    return __builtin_popcountll(x);
#endif
}

//: 32 位里 1 的个数。
inline int popcount32(uint32_t x) {
#if defined(_MSC_VER)
    return static_cast<int>(__popcnt(x));
#else
    return __builtin_popcount(x);
#endif
}

// ------------------------------------------------------------ 末尾零 / 前导零
//
// **`x` 必须非零**，三个 `ctz` / `clz` 都是。这不是本封装的限制，是内建函数
// 本身的契约：GCC 的 `__builtin_ctzll(0)` 是未定义行为，MSVC 的
// `_BitScanForward64` 在 x==0 时返回 0 并把出参原样留着（于是悄悄给出一个
// **旧值**）。调用方一律先判非零 —— 本仓库里那几个点都是
// `if (w[i])` / `while (m)` 包着的。

inline int ctz64(uint64_t x) {
#if defined(_MSC_VER)
    unsigned long i;
    _BitScanForward64(&i, x);
    return static_cast<int>(i);
#else
    return __builtin_ctzll(x);
#endif
}

inline int ctz32(uint32_t x) {
#if defined(_MSC_VER)
    unsigned long i;
    _BitScanForward(&i, x);
    return static_cast<int>(i);
#else
    return __builtin_ctz(x);
#endif
}

inline int clz64(uint64_t x) {
#if defined(_MSC_VER)
    unsigned long i;
    _BitScanReverse64(&i, x);
    // `_BitScanReverse64` 给的是最高位**位号**，`__builtin_clzll` 给的是它
    // 前面还有多少个零 —— 差一个 `63 -`，漏掉就是全盘错位。
    return 63 - static_cast<int>(i);
#else
    return __builtin_clzll(x);
#endif
}

inline int clz32(uint32_t x) {
#if defined(_MSC_VER)
    unsigned long i;
    _BitScanReverse(&i, x);
    return 31 - static_cast<int>(i);
#else
    return __builtin_clz(x);
#endif
}

}  // namespace gomoku

// 注：MSVC 的 `_BitScanForward64` / `_BitScanReverse64` 只在 64 位目标上存在。
// 本仓库的 Windows 产物固定是 x86_64（见 .github/workflows/build.yml），所以
// 没有为 32 位 MSVC 准备退路 —— 真要出 x86 版，这里得先补一对 32 位的实现。

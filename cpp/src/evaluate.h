// 棋型分类器与静态评估。
//
// 这是整个移植里风险最高的一段（engine_local.py 的注释也这么写）。要点是
// **集合语义**而不是"数一数、看一看两端"：
//
//     F(S) = { 空点 e : 在 e 落子即连成五 }
//     |F| >= 2 → 活四        |F| == 1 → 冲四
//     三/二用**有界两层前瞻**（补一子 / 补两子后 |F| 是多少）
//
// 三个必须逐字复刻、不能"顺手改好"的地方：
//
//  1. `completions` 累加的是**位图 OR**，不是计数。计数会把 `XXXX_XXXX`
//     中间那个点算两次（左右两窗各含 4 子），把一个冲四误判成活四 —— 而
//     活四是必胜、冲四不是，这个误判直接改变搜索结论。
//  2. `maxFAfter` 的 k=2 剪枝（第二子只考虑"与第一子同处于某个窗口"的空点）
//     是**语义相关**的，不是性能优化，删掉结果会变。
//  3. 段缓存（`segment` + 线缓存）的键必须**含段长** `w`。漏掉它，不同长度
//     的段会共用键 → 静默错分。缓存满了整体 `clear()`，不做 LRU。
//
// 已求证并在此处直接使用的等价式（`--verify-tables` 里有对照）：
//   * "覆盖位 b 的窗口并集" == `[max(0,b-4), min(w-1,b+4)]`（见 tables::touchMask）
//   * "所有窗口的并集" == `(1<<w)-1`（见 maxFAfter 的调用点）

#pragma once

#include <cstdint>

#include "constants.h"

namespace gomoku {

class Board;

//: `segment()` 的结果：压到"与该方有关的那一小段"之后的局部位图与段长。
struct Segment {
    uint32_t my;
    uint32_t opp;
    int w;
};

//: 把一条线压到"与该方有关的那一小段"。
//:
//: 段 = 该方首子 - PAD .. 该方末子 + PAD，再按线的两端夹紧。夹紧是**必需的**：
//: 线的端点就是墙，夹紧之后"窗口不能越过段端"恰好等价于"窗口不能越过线端"。
Segment segment(uint32_t my, uint32_t opp, int length);

//: 段内某方的棋型等级（`LV_*`）。`w` 是段长（决定了窗口表）。
int lineLevel(uint32_t my, uint32_t opp, int w);

//: `lineLevel` 的带缓存版本。`my`/`opp` 是**整条线**的局部位图。
int cachedLineLevel(uint32_t my, uint32_t opp, int length);

//: 跨线组合加成。`agg` 是 `Board::threatAgg[p]`（等级 → 线数）。
//:
//: 按"最贵的那一种"取、**不叠加**：双四本身就蕴含"有活三"，再叠一次四三
//: 会把它算得比活四还贵。偏序 活四 > 双四 > 四三 > 双活三 > 冲四 由这一条
//: if 链保证。
int64_t comboBonus(const int32_t* agg);

//: 从 `me` 视角的静态分。**严格零和**：`evaluate(b,1) == -evaluate(b,2)`。
//:
//: 零和不是美学要求，是 negamax 的前提。**不含任何 tempo/先行方常数** ——
//: 给行棋方加定值同样会破坏零和（命题 `eval(pos,黑) == -eval(镜像(pos),白)`
//: 在两边都 +K 之后就不再成立）。
//:
//: ⚠️ **难度偏置（"贪吃系数"）不在这里。** 它只在根节点选点施加，见
//: search.cpp 的 `rootBias`。把它塞进这里会让 evaluate 不再零和，进而使
//: negamax 失去健全性，并让 `info['best_val']`（面板两张图的唯一数据源）
//: 带上随行棋方变化的偏置。
int32_t evaluate(const Board& b, int me);

//: 该分值是否为杀棋分（含必胜与被杀）。
inline bool isMate(int32_t v) { return v > STATIC_MAX || v < -STATIC_MAX; }

//: 清空线缓存。只为自检/复现性准备 —— 正常运行期不清（它是纯缓存）。
void clearLineCache();

}  // namespace gomoku

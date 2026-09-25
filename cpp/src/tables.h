// 预计算表。全部在启动时构建一次（`init()` 幂等），之后只读。
//
// **112 条线的编号必须与 engine_local 逐字相同**：方向 0 横 lid=0..18、
// 方向 1 竖 19..37、方向 2 撇 38..74、方向 3 捺 75..111，每个方向内按
// `for r: for c:` 的字典序，只从"前驱在盘外"的格子出发。
//
// 严格说，C++ 侧自己算一套编号也能自洽（`_CELL_LINE_POS` 与 `_LINE_VIEWS`
// 同源），编号本身不影响任何结论。仍然逐字复刻的理由有两条：一是它让
// "两边的同一条线"这句话有确定含义，`--verify-tables` 与将来任何跨语言
// 对照都不必再做映射；二是这段枚举只有 15 行，复刻的成本远小于解释
// "为什么可以不一样"的成本。
//
// 这是整个移植里**唯一会静默出错**的地方 —— 差一位，`rescoreLine` 每次只会
// 更新错的 4 条线，全盘 score_sum 缓慢错位，表现为"分数略偏"而不是报错。
// `verify()` 就是为它准备的。

#pragma once

#include <cstdint>
#include <string>

#include "bits361.h"
#include "constants.h"

namespace gomoku {
namespace tables {

//: 每条线的长度与线上的全局格索引（局部坐标 0..length-1 沿线排列）。
struct LineView {
    uint8_t length;
    uint16_t cells[BOARD_SIZE];
};

//: 过某格的四条线的 `(线号, 局部位置)`。
struct CellLinePos {
    uint8_t lid;
    uint8_t t;
};

//: 一条线上的 5 连窗口（局部坐标，长度 ≤19 故用 uint32）。
//: 最长 19 的线有 19-4 = 15 个窗口。
struct LineWindows {
    uint8_t count;
    uint32_t masks[BOARD_SIZE];
};

//: 包含某格的全部 5 连窗口（**全局**格索引掩码）。
//: 每格最多 4 方向 × 5 窗口 = 20 个。
//:
//: 掩码是 `Bits361` 而不是 `uint32_t`：窗口横跨全局索引 0..360，32 位装不下
//: —— 用 uint32 会让索引 ≥32 的窗口静默截断成"部分窗口"，而 `makesFive`
//: 的判据是"窗口里的 5 格全是我"，截断后必然不成立，症状是**成五被判漏**。
struct CellWindows {
    uint8_t count;
    Bits361 masks[20];
};

//: 某格切比雪夫距离 ≤2 的格子（不含自身），最多 24 个。
struct NeighborList {
    uint8_t count;
    uint16_t idx[24];
};

inline LineView lineViews[N_LINES];
inline CellLinePos cellLinePos[CELLS][4];
inline LineWindows lineWindows[BOARD_SIZE + 1];
inline CellWindows winMasks[CELLS];
inline NeighborList neighbors[CELLS];
inline Bits361 fiveStarts[4];
inline uint64_t zobrist[2][CELLS];

//: 每条线的 `(1 << length) - 1`，即该线局部位图的有效位掩码。
inline uint32_t lineMask[N_LINES];

//: ``touchMask[w][b]`` = 长度 w 的线上，**覆盖位置 b** 的那些 5 连窗口的并集。
//:
//: 对应 Python 的 ``_windows_touching(wins, bit)``。窗口是长度为 5 的连续段，
//: 所以"包含 b 的窗口"的并集必然是 `[max(0,b-4), min(w-1,b+4)]` 这一段，
//: 但这里仍按 Python 的原循环逐窗口求和 —— 两者的等价性依赖"窗口是 5 连
//: 连续段"这一条，而那正是最不该在优化里被重新论证的东西。表只有
//: 20×19 项，按循环建一次即可。
inline uint32_t touchMask[BOARD_SIZE + 1][BOARD_SIZE];

//: 构建全部表。幂等，可重复调用。
void init();

//: 自检。返回 false 时 `err` 写明哪里不一致。
//:
//: 这不是"跨语言对拍"，是**进程内的结构一致性检查** —— 它挡的正是"线号差
//: 一位"这一类静默 bug：线的 `(lid, length, cells)` 自洽、`cellLinePos` 与
//: 线表互相指得回对方、每个格子恰好属于 4 条线、Zobrist 表无重复值。
//: 最后一项对应 engine_local._zobrist_row docstring 里记的那个真实事故
//: （发生器建在循环里导致 361 项全相同，而所有一致性测试仍然全绿）。
bool verify(std::string* err);

}  // namespace tables
}  // namespace gomoku

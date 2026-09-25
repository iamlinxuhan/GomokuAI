// 位棋盘 + 全增量状态。
//
// 与 engine_local.Board 逐字段对应：
//
//   b_bits / w_bits      → Bits361
//   hash                 → uint64_t（Zobrist，增量维护）
//   neighbor_count       → uint8_t[361]
//   cand_mask            → Bits361
//   _n_black / _n_white  → int32_t
//   line_bits[2][112]    → uint32_t[2][112]（**每条线只有 19 位** —— 这是最大
//                          的一处性能红利：Python 大整数换成了单个字）
//   line_lv[2][112]      → uint8_t[2][112]
//   score_sum[2]         → int64_t[2]（**必须 int64**：112 条线 × WIN_SCORE
//                          可到 1.12e9，int32 虽勉强放得下但毫无余量）
//   threat_agg[2][9]     → int32_t[2][9]（**初值 [112,0,...]，不是全零**）
//   threat_power[2]      → int32_t[2]
//
// `make`/`unmake` **严格对称**（XOR + ±1 计数），因此允许乱序 unmake：只要
// 每颗子都被撤销过一次，状态就位级还原，无需维护撤销栈。这一点对搜索很关键
// —— 有了它，剪枝路径上提前 return 也不用清理。
//
// `make` **不做判空校验**（Python 版也不做）：它在搜索热路径上，而每节点多
// 一次判空是不必要的开销。违反前提不会崩，只会让哈希与子数静默偏移 ——
// 调用方负责。

#pragma once

#include <cstdint>

#include "bits361.h"
#include "constants.h"
#include "evaluate.h"
#include "tables.h"

namespace gomoku {

class Board {
public:
    Bits361 bBits;
    Bits361 wBits;
    uint64_t hash;
    uint8_t neighborCount[CELLS];
    Bits361 candMask;
    int32_t nBlack;
    int32_t nWhite;
    uint32_t lineBits[2][N_LINES];
    uint8_t lineLv[2][N_LINES];
    int64_t scoreSum[2];
    int32_t threatAgg[2][9];
    int32_t threatPower[2];

    Board();

    // ------------------------------------------------------------ 查询

    Bits361 occupied() const { return bBits | wBits; }
    int stoneCount() const { return nBlack + nWhite; }

    const Bits361& bitsOf(int player) const { return player == 1 ? bBits : wBits; }
    uint32_t* lineBitsOf(int player) { return lineBits[player - 1]; }
    const uint32_t* lineBitsOf(int player) const { return lineBits[player - 1]; }

    int get(int idx) const {
        if (bBits.test(idx)) return 1;
        if (wBits.test(idx)) return 2;
        return 0;
    }
    bool isEmpty(int idx) const { return !occupied().test(idx); }

    bool hasFive(int player) const;

    //: 刚落下的这一子是否形成五连（含长连）。只查包含 idx 的窗口。
    //: 调用前提是**落子前无五连** —— 若双方都已有五连在盘上，本方法只能
    //: 回答"这一子是否参与了其中一个"。
    bool makesFive(int player, int idx) const {
        const Bits361& bits = bitsOf(player);
        const tables::CellWindows& cw = tables::winMasks[idx];
        for (int i = 0; i < cw.count; ++i) {
            if (containsMask(bits, cw.masks[i])) return true;
        }
        return false;
    }

    // ------------------------------------------------------------ 变更

    //: 落子并增量维护全部状态；返回该子是否成五。
    bool make(int idx, int player);
    //: 撤销落子。与 `make` 严格对称，故可在任意顺序下调用。
    void unmake(int idx, int player);

    // ------------------------------------------------------------ 转换

    //: 从 361 位的两方位图一次构建。**这是 C++ 侧的入口**（Python 侧从
    //: ndarray 走 `from_array`，TCP 协议传的也是棋盘数组，由 json 层解析
    //: 成位图后调这里）。
    static Board fromBits(const Bits361& b, const Bits361& w);

    //: 从棋盘数组（361 项，0/空 1/黑 2/白）构建。TCP 层解析完 JSON 后走这里。
    //: 结果必须与"逐子 make 到同一盘面"完全一致 —— `--selftest` 逐字段比对
    //: 两者，那是增量正确性的依据（对应 Python 的 `Board.from_array`）。
    static Board fromArray(const uint8_t* cells);

    //: 候选落点（升序）。`candMask` 为空有两种情形：空盘，或盘面已被填满。
    //: 前者给天元，后者给空。
    int candidateCount() const;
    void candidates(int* out, int* n) const;

    //: 逐字段比对，返回不一致的字段名（空表示一致）。自检用。
    void diff(const Board& other, const char** out, int* n) const;

private:
    void rescoreLine(int lid);
};

//: 全盘五连检测（含长连）。四个方向各 9 次位运算，无逐格循环。
//:
//: **这是全局唯一的胜负判定实现** —— `check_win` / `Board::hasFive` /
//: `Board::make` 的返回值 / 搜索内部都收敛到它或它的增量版本。
bool hasFiveBits(const Bits361& bits);

//: 落子即成五的空点集合（``_five_points``）。
Bits361 fivePoints(const Bits361& bits, const Bits361& obits);

//: 空点中「落子即形成四子连窗」的集合（``_hot_points``，含五点）。
Bits361 hotPoints(const Bits361& bits, const Bits361& obits);

}  // namespace gomoku

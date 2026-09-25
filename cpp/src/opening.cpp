#include "opening.h"

#include "constants.h"

namespace gomoku {

int openingMove(const uint8_t* cells, int player) {
    (void)player;      // 与 Python 版一样：着法只由盘面决定，与行棋方无关

    int stones = 0;
    int first = -1;
    for (int i = 0; i < CELLS; ++i) {
        if (cells[i] != 0) {
            ++stones;
            if (first < 0) first = i;
            if (stones > 1) return -1;     // 其余情况交由正规搜索
        }
    }
    if (stones == 0) {
        return (BOARD_SIZE / 2) * BOARD_SIZE + BOARD_SIZE / 2;    // 天元
    }

    // 盘上仅一子：紧贴该子，偏移按固定顺序取第一个空点。
    //
    // 顺序必须逐字保持 —— 它决定了开局第一步的走向，换一个顺序会得到另一个
    // （同样合法但不同的）着法，于是与 Python 侧以及历史棋谱对不上。
    static const int OFFS[8][2] = {{0, -1}, {0, 1},  {-1, 0}, {1, 0},
                                   {-1, -1}, {-1, 1}, {1, -1}, {1, 1}};
    const int r0 = first / BOARD_SIZE;
    const int c0 = first % BOARD_SIZE;
    for (const auto& o : OFFS) {
        const int r = r0 + o[0];
        const int c = c0 + o[1];
        if (r < 0 || r >= BOARD_SIZE || c < 0 || c >= BOARD_SIZE) continue;
        const int idx = r * BOARD_SIZE + c;
        if (cells[idx] == 0) return idx;
    }
    return -1;
}

}  // namespace gomoku

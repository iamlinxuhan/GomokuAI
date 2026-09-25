// 开局着法。
//
// **权威实现在 Python 侧**（`engine_local.opening_move`）：`ai_move` 在调搜索
// 之前就要用 `stone_count == 0` 判定空盘，走 TCP 会给第一步加一次往返，而开局
// 是唯一必须零延迟的时刻；而且 `reason='开局库'` 这个取值只有本地能产出。
//
// C++ 这一份挂在下述两条上：`--selftest` 的对照，以及将来若有人真的需要把
// 开局也挪到服务端。它与 Python 版逐字等价（含"仅对手一子"的那个分支）。

#pragma once

#include <cstdint>

namespace gomoku {

//: 返回线性格索引，或 -1（"其余情况交由正规搜索处理"）。
//:
//: - 空盘：天元
//: - 盘上仅一子：紧贴该子，偏移按固定顺序取第一个空点
//: - 其余：-1
int openingMove(const uint8_t* cells, int player);

}  // namespace gomoku

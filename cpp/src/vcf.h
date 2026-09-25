// 连续冲四（VCF）的类型。
//
// **三态是这套东西的全部要点**（engine_local 里修 B15 的那一段）：
//
//     VCF_WIN        证明存在一条对手挡不住的连续冲四
//     VCF_NO_WIN     证明**不存在**（所有起手、所有应手都枚举完了）
//     VCF_EXHAUSTED  预算用完了，**没有结论**
//
// 把 EXHAUSTED 当成 NO_WIN 是本仓库最贵的一个 bug 形态：旧引擎的
// `_find_forced_win` 正是这么写的，"搜不完"于是被读成"没有必胜"，一条更慢的
// 杀棋被判为不存在。调用方只能这样用：**只有 WIN 可以据此落子**，NO_WIN 只
// 可用于排除，EXHAUSTED 一律"不表态"。
//
// AND/OR 语义同样在这里：**我的**着法是 OR（任一起手赢即赢），**对手的**应手
// 是 AND（所有应手都输才是赢）。旧实现两边都用 OR —— 只要对手*某个*应手走完
// 还"看起来能赢"就宣布必胜，那是"进攻方假必胜"这类反例的根源。
//
// ## 防错的写法
//
// C++ 里刻意**不用 enum class** 而用一组 `constexpr int` + 一个只读的结构体：
// `enum class` 会诱使人写 `if (state)` 或与 `0`/`-1` 直接比较，而这两件事
// 正是把三态压成两态的入口。用 `VcfResult::state` 时，比较对象只能是
// `VCF_WIN` / `VCF_NO_WIN` / `VCF_EXHAUSTED` 这三个具名常量。

#pragma once

#include <cstdint>

#include "constants.h"

namespace gomoku {

struct VcfResult {
    //: VCF_WIN / VCF_NO_WIN / VCF_EXHAUSTED 之一。
    int state;
    //: 仅在 WIN 时有效（线性格索引）。
    int move;
    //: 从当前节点算起**还有几手落下那颗成五的子**（WIN 时有效）。
    int dist;
};

//: VCF 自己的预算（节点数 / 深度 / 独立时间）用完了 —— 与 `SearchAborted`
//: **刻意分开**：前者是"这套方法算不完了"（结果是 EXHAUSTED），后者是"整个
//: 搜索该停了"（向上穿透到 `think`）。混成一个会让预算耗尽被当成取消。
struct VcfStop {};

}  // namespace gomoku

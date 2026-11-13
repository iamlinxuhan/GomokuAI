// DFPN（Depth-First Proof-Number search）—— 证明数搜索的三态封装。
//
// **三态是这个模块的全部要点**，与 `vcf.h` 同一条纪律：
//
//     PROOF_WIN       根节点被证明：进攻方存在一条对手挡不住的杀
//     PROOF_NO_WIN    根节点被反驳：所有起手、所有应手都枚举完了
//     PROOF_UNKNOWN   节点上限 / 时间到 / 取消 / 深度上限，或抽象不完整
//
// **只有 WIN 可以据此落子。** NO_WIN 只可用于"这条路线排除了"，UNKNOWN 一律
// 不表态。把 UNKNOWN 当成 NO_WIN 是本仓库最贵的一个 bug 形态（见 `vcf.h`）。
//
// ## 两种模式
//
//   * `mode = 1`（VCF-only）：进攻方只走**冲四 / 成五**。走法集与 `vcfRec`
//     完全同源（`hotPoints` + `fivePoints`），守方在"被冲四"时只有唯一应手
//     （挡五点；不挡就直接输），所以**攻守两边的集合都是完整的** —— WIN 与
//     NO_WIN 都可信。
//   * `mode = 2`（VCT）：进攻方额外考虑**活三**。活三不是强制着，守方在活三
//     之后的应手集是**全部空点**（一个不漏 —— 任何一格都可能反冲出四）。这
//     让 AND 节点在威胁空间里仍然完整，于是 WIN 依旧可信；但进攻方在"要回去
//     应守方的四"时只枚举冲四/成五，走法集不完整，因此 **NO_WIN 一律降级为
//     UNKNOWN**（见 `dfpnSearch` 的返回处）。这条规则的代价是 VCT 只能用来
//     "找杀"，不能用来"排除杀"——宁可算不出，也不许把抽象里的结论说成实战
//     结论。
//
// ## 内存
//
// 一个 arena：**线性增长、一次分配不逐节点 free**，`nodeCap` 是硬上限，用尽即
// UNKNOWN。初始只分配一小块，按需翻倍 —— 一个 10 个节点就能证完的局面不该为
// "可能要用 100 万"先掏 24 MB。
//
// ## 线程
//
// `dfpnSearch` **不读写 Engine 的任何状态**：棋盘是调用方给的副本，arena 在
// 自己身上，唯一共享的是只读的 `cancel` 原子。所以它可以与主搜索并行跑。

#pragma once

#include <atomic>
#include <cstdint>

#include "board.h"
#include "constants.h"
#include "vcf.h"

namespace gomoku {

//: 与 `VCF_*` 同一套"用具名常量、不用 enum"的写法，理由见 `vcf.h`。
constexpr int PROOF_WIN = 1;
constexpr int PROOF_NO_WIN = -1;
constexpr int PROOF_UNKNOWN = 0;

struct DfpnConfig {
    //: 1 = VCF-only，2 = VCT（见文件头）。其它值一律按 0 处理（关闭）。
    int mode = 1;
    //: 墙钟上限（秒）。到点即 UNKNOWN。
    double timeLimit = 1.0;
    //: 节点上限。默认 100 万；用尽即 UNKNOWN，不扩容。
    int64_t nodeCap = 1000000;
    //: 从根算起的最大手数。超过即 UNKNOWN。
    int maxDepth = MAX_PLY - 2;
};

struct ProofResult {
    //: PROOF_WIN / PROOF_NO_WIN / PROOF_UNKNOWN 之一。
    int state;
    //: 仅在 WIN 时有效（线性格索引，根节点的一手）。
    int move;
    //: 仅在 WIN 时有效：**从根算起还有几手落下那颗成五的子**（根上成五 = 1）。
    //: 与 `VcfResult::dist` 同一口径。
    int dist;
    //: 本次搜索展开的节点数（诊断用，不进 `Info`）。
    int64_t nodes;
};

//: 证明搜索。`root` 不会被修改；`cancel` 可为 nullptr。
ProofResult dfpnSearch(const Board& root, int me, const DfpnConfig& cfg,
                       const std::atomic<bool>* cancel);

}  // namespace gomoku

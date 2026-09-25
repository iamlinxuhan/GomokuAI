// 常量表。**每一项都与 engine_local.py 逐字同名同值** —— 不是风格偏好，
// 而是因为 analysis.py 的胜率锚点直接钉在这些数上：
//
//     tests/test_analysis.py::test_anchors_are_engine_constants
//
// 逐个校验 活三=30000 / 双四=700000 / STATIC_MAX=9999872。C++ 侧只要有一个数
// 与 Python 不同，右侧两张图（symlog 评分折线 + 0–100% 胜率曲线）就会在
// 不报错的前提下画错。
//
// **难度参数不在这里。** 五档的 time / max_depth / vcf_budget / qply 由 Python
// 随每次请求下发（见 engine.py 的 DIFFICULTY 注释）：校准不必重编译，而且
// "难度"这个概念留在 UI 层 —— 与"PvP 时 AI 只是其中一个玩家"的抽象一致。

#pragma once

#include <cstdint>

namespace gomoku {

// ==================== 棋盘 ====================
constexpr int BOARD_SIZE = 19;
constexpr int CELLS = BOARD_SIZE * BOARD_SIZE;                  // 361
constexpr int CENTER_IDX = (BOARD_SIZE / 2) * BOARD_SIZE + BOARD_SIZE / 2;  // 180

// 四个方向 (dr, dc) 及其在线性索引上的步长：
//   0 横 → 1     1 竖 → 19     2 撇 \ → 20     3 捺 / → 18
constexpr int DIRS[4][2] = {{0, 1}, {1, 0}, {1, 1}, {1, -1}};
constexpr int DIR_STEPS[4] = {1, BOARD_SIZE, BOARD_SIZE + 1, BOARD_SIZE - 1};

// 112 条线 = 19 横 + 19 竖 + 37 撇 + 37 捺
constexpr int N_LINES = 112;

// 段两侧各留的空格数。**取 4 是为了正确性，不是调优参数** —— 见
// engine_local._PAD 的长注释：含 ≥2 颗该方子的 5 连窗口必然落在
// [首子-4, 末子+4] 内，所以"只在小段上算窗口"与"在全 19 格线上算"结论一致。
constexpr int PAD = 4;

// ==================== 棋型等级 ====================
// 整数比较即可判强弱（"至少是活三"这类判断全靠它）。
constexpr int LV_NONE = 0;
constexpr int LV_ONE = 1;           // 活一
constexpr int LV_TWO_SLEEP = 2;     // 眠二
constexpr int LV_TWO_LIVE = 3;      // 活二
constexpr int LV_THREE_SLEEP = 4;   // 眠三
constexpr int LV_THREE_LIVE = 5;    // 活三
constexpr int LV_FOUR = 6;          // 冲四
constexpr int LV_FOUR_LIVE = 7;     // 活四
constexpr int LV_FIVE = 8;          // 五连

// ==================== 分值 ====================
constexpr int WIN_SCORE = 10000000;                     // 连五分值

// 单线分值。与 engine_local.LINE_SCORES 逐项相同。
constexpr int64_t LINE_SCORES[9] = {
    0, 10, 100, 500, 1000, 30000, 100000, 1000000, WIN_SCORE,
};

// 跨线组合加成。**绝对量、不是倍乘，且总量严格小于活四**，否则"双活三"会被
// 算得比活四值钱，搜索会为了凑双三放弃成四。
constexpr int64_t BONUS_DOUBLE_FOUR = 700000;   // 两条线 ≥ 冲四
constexpr int64_t BONUS_FOUR_THREE = 600000;    // 一条 ≥ 冲四，另一条 ≥ 活三
constexpr int64_t BONUS_DOUBLE_THREE = 500000;  // 两条线 ≥ 活三

// 杀棋分与静态分的分界：静态分钳进 ±STATIC_MAX，杀棋分落在
// [WIN_SCORE - MAX_PLY, WIN_SCORE]，中间留出 128 - MAX_PLY 的真空带。
constexpr int STATIC_MAX = WIN_SCORE - 128;             // 9999872
constexpr int MAX_PLY = 64;

static_assert(MAX_PLY < WIN_SCORE - STATIC_MAX,
              "杀棋分与静态分必须不重叠");

// ==================== 搜索 ====================
constexpr int TT_EXACT = 0;
constexpr int TT_LOWER = 1;
constexpr int TT_UPPER = 2;

constexpr int INF = 1 << 30;
constexpr int ABORT_MASK = 1023;        // 每 1024 个节点轮询一次时间/取消
constexpr uint64_t TT_SALT_W = 0x5D5D5D5D5D5D5D5DULL;   // 白方行棋的键扰动

// ---- 置换表的几何 ----
//
// 这里曾经有一个 `TT_MAX = 1 << 20`，与 `search.cpp` 的 `TT_SIZE` 等值，但
// **全树没有任何读取点** —— 一个看起来像"条目上限"的死常量，而真正生效的上限
// 在 search.cpp 里。要么删掉，要么让它成为真的。现在它是真的：
//
//  * `TT_LEGACY_SIZE` —— 直接映射时代的表长，也是**混合几何里的低半区**。
//  * `TT_WAYS` —— 每桶的路数。增强搜索用双路桶。
//
// 两种几何共用同一块 2^21 条目的数组，靠 `TT_BUCKET_BITS` 这一个下标掩码
// 切换（见 search.cpp 的 `ttIndex`），于是**关闭增强时的内存布局与旧版逐位
// 相同** —— 包括碰撞与覆盖的顺序。这很重要：`tools/positions.py` 上"C++ 初级
// 与 Python 参考引擎逐字同着"那条一致性是移植正确性的唯一廉价护栏，而它依赖
// 的正是"低档走的是原来那张表"。条目 24 B × 2^21 = **48 MiB 常驻**。
constexpr int TT_BUCKET_BITS = 20;
constexpr int TT_LEGACY_SIZE = 1 << TT_BUCKET_BITS;     // 2^20，旧版表长
constexpr int TT_WAYS = 2;                              // 每桶路数
constexpr int TT_ENTRIES = TT_LEGACY_SIZE * TT_WAYS;    // 2^21，条目总数
constexpr int ASPIRATION = 120;         // 渴望窗口初值
constexpr int ASP_WINDOW_MUL = 4;       // 失败后窗口放大倍数
constexpr int ASP_FAILS = 3;            // 连续失败几次后放弃渴望、走全窗口

constexpr double RESERVE = 0.15;        // 留给回传与 UI 的余量，从时间上限里扣

// ==================== 增强搜索（只有高级/宗师启用） ====================
//
// 这几个数都是"保守到不像是优化"的取值，理由见各自的注释。它们**只在
// `SearchConfig::enhanced` 为真时生效**，所以低档的行为与旧版逐位相同。

//: LMR 的最低深度。低于它不削减：浅节点上"削减一层"占的比例太大，而浅节点
//: 的总量又极大，削减出来的收益不成比例、误判的代价却是满额的。
constexpr int LMR_MIN_DEPTH = 3;
//: 从这个着法序号起多削一层。前 6 手已经由 `nPriority` 与序号共同保护。
constexpr int LMR_MORE_MOVES = 6;
//: 深度到这条线以上再多削一层。深树上多削一层的相对损失更小。
constexpr int LMR_DEEP = 6;

//: 强制着法延伸的最低深度。
constexpr int EXT_MIN_DEPTH = 3;
//: **一条线上最多允许欠多少层。**
//:
//: 这是延伸不发散的关键。五子棋的强制着法极其密集，一条"冲四 → 挡 → 冲四"
//: 的线可以永远延伸下去，而 `depth` 永不下降 —— 表现为"时限内连一层都搜不
//: 完"，比不延伸还糟。用"路径欠账"而不是全局计数器来限量：全局计数器会被
//: 搜索前半段耗光，于是后半段等于没有延伸；欠账是**每条线各自**的额度，每条
//: 强制线都能延伸，但都限于 `EXT_MAX_DEBT` 层。
//:
//: 欠账 = `ply + depth - 本轮的根深度`：不延伸时每步 `ply+1, depth-1`，和不变；
//: 延伸时 `depth` 不减，和加一。所以它恰好等于"这条线上累计延伸了几层"。
constexpr int EXT_MAX_DEBT = 8;

//: 时间管理：预估下一轮成本超过剩余时间的这个倍数，就不开新一轮。
constexpr double TIME_SOFT_MUL = 1.5;

//: 时间管理：**预算至少要花掉这个比例，早退才被允许**。
//:
//: 这条不是调参，是补一个实测出来的洞。`TIME_SOFT_MUL` 单独用时会**过度
//: 保守**，因为它乘的是一个被夹到 8 的、噪声很大的分支因子估计
//: （`prevIterNodes / prevPrevIterNodes`）。短预算下症状很刺眼 ——
//: `tools/bench.py` 用 1.5 s 预算跑增强臂：
//:
//: | 局面 | 关（基线） | 开（增强） |
//: |---|---|---|
//: | `spread_wide` | 深度 4 · 1320 ms（跑满） | 深度 4 · **332 ms**（早退） |
//: | `sparse_early` | 深度 6 · 1324 ms（跑满） | 深度 5 · **368 ms**（早退） |
//:
//: 也就是**花掉 26% 的预算就收工**，`sparse_early` 上还比基线浅了一层。而
//: "尝试一轮"与"直接早退"的代价对比是**不对称**的：试了没搜完 = 同样的深度
//: + 多花的时间；直接早退 = 同样的深度 + 省下的时间。**试一次是免费的期权**
//: —— 只有"预估成本远超剩余时间"时它才真的白花。所以早退必须建立在
//: "预算已经花掉相当一部分"之上，否则这条启发式省下的不是浪费的时间，而是
//: 本该拿到的深度。
//:
//: 长预算下的原有效果不受影响：`dense_threat` 在 15 s 档上于 8.6 s 完成第 9
//: 层并早退（已用 67% ≥ 50%），实测深度 6 → 9。
constexpr double TIME_SOFT_MIN_USED = 0.5;

// ==================== 连续冲四（VCF） ====================
// **三态，且 EXHAUSTED 绝不可当 NO_WIN 用。** 把"算不完"读成"没有必胜"是本
// 仓库最贵的一个 bug 形态（旧引擎的 `_find_forced_win` 就是这么写的）。
constexpr int VCF_WIN = 1;
constexpr int VCF_NO_WIN = 0;
constexpr int VCF_EXHAUSTED = -1;

constexpr int VCF_MAX_PLY = 24;         // 一条 VCF 最多 24 手（12 回合）

// `_vcf_defence` 的两个否定答案，**它们不是一回事**：NONE 是"候选集扫完了，
// 没有一手能破坏对手的 VCF"，UNKNOWN 是"预算用尽，没扫完"。混成一个返回值
// 会让调用方分不清"证明它没用"与"还没算"。
constexpr int VCF_DEFENCE_NONE = -1;
constexpr int VCF_DEFENCE_UNKNOWN = -2;

constexpr int VCF_POLL_MASK = 255;      // 每 256 个 VCF 节点轮询一次预算

// 一次 VCF 阶段的节点上限：时间之外的兜底闸门。
//
// ⚠️ **这个值从 Python 的 120000 放大了。** Python 里 12 万节点约 2.4 秒，大于
// 最高档 1.5 秒的 VCF 预算，所以基本不触发；C++ 里 12 万节点只需约 0.05 秒，
// 于是它会在**每一档都**触发，把大量本可证明的杀棋判成 EXHAUSTED（=不表态），
// 引擎在 VCF 上反而比 Python 版更弱 —— 与"C++ 更强"的直觉相反。放大到这个量级
// 后它退回"兜底闸门"的本分，时间预算才是真正的约束。校准走请求里的
// `vcf_node_cap` 字段，不必重编译。
constexpr int64_t VCF_NODE_CAP = 3000000;

// ==================== Zobrist ====================
constexpr uint64_t ZOBRIST_SEED_BB = 0x9E3779B9ULL;

}  // namespace gomoku

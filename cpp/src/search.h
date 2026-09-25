// 搜索：negamax + PVS + 置换表 + 静止搜索 + 迭代加深 + 渴望窗口 +
// 硬时间上限 + 协作式取消 + 根节点 VCF 快速通道。
//
// 与 engine_local.py 的 Phase 3+4/5 段一一对应。几条**不能"顺手改好"**的：
//
//  1. `flagOf` 的边界是 `value <= alpha_orig → UPPER` / `>= beta → LOWER`。
//     写成 `> alpha_orig` 判 EXACT 是历史 bug（B2）："恰好等于下界"的失败低
//     被当成精确值存下来，后续搜索会据此**直接返回一个上界冒充精确值**。
//  2. PVS 的零窗口重搜条件是 `alpha < v < beta`，**严格不等**。
//  3. `toTT`/`fromTT` 的杀棋分 ply 归一化：存 `value + ply`、取 `value - ply`，
//     于是同一局面在第 3 层与第 7 层存下的是同一个数。
//  4. 静止搜索**禁用 stand-pat**：对手有成五点时我必须有动作，此时若还允许
//     "原地不动"取静态分，等于让搜索选一个现实中不存在的着法。
//  5. 根节点**不做** PVS 零窗口：根要返回的永远是分值最高的那条路，不必为
//     "某条线路被剪掉"负责。
//
// **杀棋分与静态分分带**：静态分钳在 ±STATIC_MAX，杀棋分在 WIN_SCORE-MAX_PLY
// 以上，中间留真空带，`isMate()` 靠它而不是靠猜。
//
// **杀棋分的数值编码：`WIN_SCORE - k` = "成五的那一手落在第 k 层"**（根节点
// k=0）。这一条必须记住，否则极易把正常的杀棋分误读成溢出：`9999996` 不是
// "溢出了 4"，而是"4 层之内成五"；`9999993` 就是"7 层之内成五"。**分值越接近
// WIN_SCORE 表示杀得越快**，所以"几条败线里挑最长的"等价于"挑分值最小的"。
//
// 两个常被误判成 bug 的现象，都属于正常输出：
//  * `dep=1, val=9999996, 5ms` —— `dep` 是**主搜索**完成的迭代数。短杀由
//    VCF 快通道或静止搜索（`qply` 层强制着法）证出，根本不经过主搜索，所以
//    "1 层 + 5 毫秒 + 杀棋分"三者可以同时成立（`testMateTruth` 钉住这一点）。
//  * 同一个局面先报静态分、再报杀棋分 —— 置换表跨请求保留，前一次长搜索在
//    **被时间截断的那一轮迭代**里证到的杀棋会把条目留在表里，后续浅搜索直接
//    读出更深的结果。数值是真的，只是"来自哪一轮迭代"变了。

#pragma once

#include <atomic>
#include <cstdint>
#include <vector>

#include "board.h"
#include "constants.h"
#include "evaluate.h"
#include "vcf.h"

namespace gomoku {

//: 搜索在时间/取消到期时抛出，向上穿透到 `think`，由它决定用**哪一轮完整
//: 迭代**的结果。
//:
//: 抛出时 `Board` 可能停在中途状态 —— 这是刻意的：每节点包一次 try/finally
//: 的代价高于收益。约束是"一旦抛出就不得再用这个 Board"，`think` 严格遵守。
struct SearchAborted {
    const char* why;
};

//: 一次请求的全部可调参数。**由 Python 随报文下发**，C++ 侧不写死难度表：
//: 校准不必重编译，而且"难度"这个概念留在 UI 层。
struct SearchConfig {
    double timeLimit = 3.0;         // 秒，硬上限
    int maxDepth = 4;
    double vcfBudget = 0.0;         // 秒，VCF 阶段独立预算（0 = 关闭）
    int qply = 4;                   // 静止搜索的额外层数
    int64_t vcfNodeCap = VCF_NODE_CAP;
    // 入门档的"贪吃系数"。0 表示关闭（其余四档）。
    // 见 search.cpp 的 `applyBias` —— 它**只在根节点选点施加**，不注入
    // `evaluate()`：evaluate 必须严格零和，而 negamax 依赖这一点。
    double biasAttack = 0.0;
    double biasDefence = 0.0;
    double biasTolerance = 0.0;

    // ---- 增强搜索（只有高级/宗师两档下发 1） ----
    //
    // **默认全 0 是这套设计的核心。** `enhanced == 0` 时下面的每一条代码路径
    // 都与"没有这套机制"逐位相同，于是三件事同时成立：
    //
    //  1. `main.cpp` 的 `testMateTruth`（钉死着法 `8*19+12` 与分值
    //     `WIN_SCORE-2`）、`testOpening`、`testSearchSmoke` 在一字不改的前提下
    //     继续通过 —— 新代码没有污染旧路径。
    //  2. 初级/中级（3 s / 7 s）与 Python 参考实现 `engine_local` 的**逐字一致**
    //     继续成立。那条一致性是这套 C++ 移植正确性的唯一廉价护栏：跑
    //     `tools/positions.py --engine cpp --level 2` 与
    //     `--engine local --level 1`，两者的着法与分值必须一个字不差。
    //  3. LMR 这类会改变搜索结果的机制**不可能**悄悄漏进低档 —— 用户明确要求
    //     "迁移，然后拔高来拉开梯度，不是降智"，这条开关就是那句话的机械保障。
    //
    // `lmr` / `extend` 是 `enhanced` 之下的**隔离测量开关**：做 A/B 时要把
    // LMR 的贡献与延伸的贡献分开量，混在一起就只能得到一个总数。
    int enhanced = 0;               // 总开关：LMR + 延伸 + TT 桶 + 排序改进 + 时间管理
    int lmr = 0;                    // 晚着法削减（`enhanced=0` 时无意义）
    int extend = 0;                 // 强制着法延伸（`enhanced=0` 时无意义）
};

//: 一次搜索的诊断结果。字段名与 Python `info` 逐字对应 —— 它是跨进程契约，
//: `analysis.readout_line` 与两张图都按名字读。
struct Info {
    const char* reason = "PVS搜索";
    int depth = 0;
    int actualDepth = 0;
    int32_t bestVal = 0;
    double timeMs = 0.0;
    int64_t nodes = 0;
    int64_t nps = 0;
    double ttHitRate = 0.0;
    double qnodeRatio = 0.0;
    const char* scoreType = "static";
    //: VCF 三态。`vcfActive == false` 时 JSON 输出 `null`（**不是 0**：
    //: 0 是 VCF_NO_WIN，一个真实的结论）。
    bool vcfActive = false;
    int vcfState = 0;
    int vcfDist = 0;
    int64_t vcfNodes = 0;
};

class Engine {
public:
    Engine();

    //: 清空跨局面状态（重开局入口 `new_game` 走这里）。
    //:
    //: 置换表**跨步保留**是刻意的：同一局里前几步算过的子树在后续搜索中仍然
    //: 有效，这是迭代加深最大的收益来源。跨**局**保留则是缺陷。
    void reset();

    //: 搜索一步。`board` 是棋盘数组（361 项，0/空 1/黑 2/白），**不会被修改**。
    //:
    //: 返回线性格索引；无合法着法时返回 -1。`info` 被填充。
    //: `cancel` 可为 nullptr；非空时每 1024 个节点读一次，置位即中止。
    int think(const uint8_t* board, int me, const SearchConfig& cfg,
              const std::atomic<bool>* cancel, Info* info);

    //: 公开的 VCF 探针（`tools/positions.py` 的 `opp_vcf` 用得上）。
    VcfResult vcf(Board& bd, int me, double budget, bool hasDeadline,
                  double deadline);

private:
    // ---- 置换表 ----
    struct TTEntry {
        uint64_t key;
        int32_t value;
        int16_t depth;
        uint8_t flag;
        int16_t move;
        //: 写入时的 `ttAge_`（每次 `think` 自增一次）。**增强搜索的替换策略
        //: 才读它** —— 旧版无条件覆盖，`ttAge_` 自增了却从来没有读者。
        //: 加上它之后 payload 是 18 B，仍被 `uint64_t` 对齐到 24 B，不涨内存。
        uint8_t age;
    };

    struct Probe { int32_t value; int move; bool hit; };

    Probe lookup(const Board& bd, int me, int depth, int32_t alpha,
                 int32_t beta, int ply);
    void save(const Board& bd, int me, int depth, int32_t value, int flag,
              int mv, int ply);

    // ---- 走法排序 ----
    //:
    //: 结果写进调用方给的缓冲区（`moveBuf_[ply]`），不返回 vector —— 每个
    //: 节点一次堆分配在 2M nps 的量级上是纯浪费，而候选数上限就是 361。
    //:
    //: `MoveOrder` 里的档位边界只服务于**增强搜索**。只返回着法索引的话，搜索
    //: 循环里没有任何办法知道"这一手是不是强制着法" —— 而 LMR 的全部安全性都
    //: 建立在"只削减安静着法"之上，延伸的全部收益都来自"认出强制着法"。
    struct MoveOrder {
        int n = 0;          //: 着法总数
        //: 强制着法区间 `[hotFirst, hotFirst + hotCount)`：己方的成五点与冲四
        //: 点，也就是"这一手自己造出了必须应的威胁"。**注意它不从 0 开始** ——
        //: 置换表着法排在它前面，所以这里必须记起点而不能只记个数。
        int hotFirst = 0;
        int hotCount = 0;
        //: 前段总长 `[0, nPriority)` = `ttMove + 强制 + 挡点 + 杀手`。这一段整体
        //: 视为已经很强的着法，**一律不削减**。
        int nPriority = 0;
    };

    void orderedMoves(const Board& bd, int me, int ttMove, int ply, int* out,
                      MoveOrder* ord);
    void recordCutoff(int me, int mv, int ply, int depth);

    //: 入门档的"贪吃系数"：在根节点拿到各候选着法的分值后，于容差带内按
    //: 「己方进攻 × biasAttack − 对手威胁 × biasDefence」重排。返回选中的
    //: 着法，或 -1（没有可用的重排）。
    int applyBias(Board& bd, int me, const SearchConfig& cfg,
                  const std::vector<std::pair<int, int32_t>>& vals,
                  int32_t bestVal);

    // ---- 静止搜索 / 主搜索 ----
    int32_t quiesce(Board& bd, int32_t alpha, int32_t beta, int me, int ply,
                    int qleft);
    int32_t negamax(Board& bd, int depth, int32_t alpha, int32_t beta, int me,
                    int ply);
    //: 根节点的一轮搜索。`collect` 非空时把每个根着法的分值一并记下
    //: （只在一档的偏置重排里用，见 search.cpp 的 `applyBias`）。
    void root(Board& bd, int me, int depth, int firstMove, int32_t alpha,
              int32_t beta, int* outMove, int32_t* outValue,
              std::vector<std::pair<int, int32_t>>* collect);

    // ---- VCF ----
    VcfResult vcfRec(Board& bd, int me, int ply, int left);
    void vcfTick();
    int vcfDefence(Board& bd, int me, int opp, double deadline);

    // ---- 时间/取消 ----
    void poll();
    void decayHistory();

    // ---- 状态 ----
    std::vector<TTEntry> tt_;
    int ttAge_ = 0;
    std::vector<int32_t> history_[2];
    int killers_[MAX_PLY + 2][2];
    //: 每个 ply 一份着法缓冲区。逐 ply 分开是为了让父节点遍历自己的着法表
    //: 时，子节点可以往另一个缓冲区里写而不互相踩。
    int moveBuf_[MAX_PLY + 2][CELLS + 1];

    int64_t nodes_ = 0;
    int64_t qnodes_ = 0;
    int64_t ttHits_ = 0;
    int64_t vcfNodes_ = 0;
    int64_t vcfNodeCap_ = VCF_NODE_CAP;
    int64_t cfgVcfNodeCap_ = VCF_NODE_CAP;
    double deadline_ = 0.0;
    double vcfDeadline_ = 0.0;
    const std::atomic<bool>* cancel_ = nullptr;
    bool timedOut_ = false;
    int qply_ = 8;
    int me_ = 1;

    // ---- 增强搜索的状态 ----
    //: 从 `cfg` 抄过来的开关。抄一份而不是每节点读 `cfg`：`negamax` 拿不到
    //: `cfg`，而它正是需要做削减决策的地方。
    bool enhanced_ = false;
    bool lmr_ = false;
    bool extend_ = false;
    //: **当前迭代的根深度。** 延伸的"路径欠账"靠它算：
    //: `debt = ply + depth - iterRootDepth_`，恰好等于这条线上累计延伸了几层。
    //: 每轮迭代在 `think` 里更新（`root` 以 `depth-1` 进入 ply=1，所以根的
    //: 子树起点恰好是 debt == 0）。
    int iterRootDepth_ = 0;

    // ---- "普通对弈的深度"（见 constants.h 的 ESCAPE_EXTRA） ----
    //: 本档**近期在没有杀棋结论的局面里**搜到的深度，也就是"普通对弈的 dep"。
    //:
    //: 用实测值而非配置里的 `maxDepth`：后者是上限不是常态（中级配置 10、
    //: 实测 5），照配置算会把"全力找出路"的上限顶到 11 层。实测值还自带两个
    //: 好处 —— 随机器快慢自适应，随局面阶段自适应（开局的深度虚高，中局才是
    //: 真本事）。
    //:
    //: **取最近一次而不是最大值。** 最大值会锚在开局那个更深的层数上：中级
    //: 实测序列是 7,6,6,5,6,6,5,5,5,5，最大值 7 会把上限顶到 8，而中局真实的
    //: 普通深度是 5。
    //:
    //: `reset()` 会清掉它 —— 见那里的注释：留着它会让同一进程里先后跑的多局
    //: 互相影响，测试与 A/B 就不可复现了。
    int normalDepth_ = 0;
    //: `normalDepth_` 属于哪一档（按 `maxDepth` 认）。档位一变就作废：同一个
    //: `Engine` 会连着服务不同档位，宗师搜到 12 层之后切到入门档，留着 12 会
    //: 让入门档的"全力找出路"变成 13 层。
    int normalDepthLevel_ = -1;
    //: 这一手"全力找出路"允许搜到第几层 = `普通深度 + ESCAPE_EXTRA`。
    //: 每次 `think` 在迭代开始前算一次，迭代中途不变。
    int escapeTarget_ = 1;
};

}  // namespace gomoku

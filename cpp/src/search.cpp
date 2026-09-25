#include "search.h"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <utility>

namespace gomoku {
namespace {

//: 单调时钟（秒）。对应 Python 的 `time.monotonic` —— **不是墙钟**：
//: 时间预算不该受系统时间调整影响。
inline double nowSec() {
    using clock = std::chrono::steady_clock;
    return std::chrono::duration<double>(clock::now().time_since_epoch()).count();
}

//: 置换表条目类型。**抽成纯函数是为了能单测**（B2 回归）。
//:
//: 边界是这条函数的全部难点：`value == alpha_orig` 必须判为 UPPER 而不是
//: EXACT。旧版写成 `value > alpha_orig` 判 EXACT，于是"恰好等于下界"的失败低
//: 被当成精确值存下来，后续搜索会据此**直接返回一个上界冒充精确值**。
inline int flagOf(int32_t value, int32_t alphaOrig, int32_t beta) {
    if (value <= alphaOrig) return TT_UPPER;
    if (value >= beta) return TT_LOWER;
    return TT_EXACT;
}

//: 杀棋分归一化：从"距当前节点多少步"换成"距根多少步"再存。
//:
//: 不归一化的话，同一局面在树的不同深度被存进来会得到**不同的分值**，置换表
//: 命中的那一刻就等于用一个深度错误的杀棋分覆盖真实值。
inline int32_t toTT(int32_t value, int ply) {
    if (value > STATIC_MAX) return value + ply;
    if (value < -STATIC_MAX) return value - ply;
    return value;
}

//: `toTT` 的逆。
inline int32_t fromTT(int32_t value, int ply) {
    if (value > STATIC_MAX) return value - ply;
    if (value < -STATIC_MAX) return value + ply;
    return value;
}

//: 作用域内落子、出作用域撤销（对应 Python 的 `try/finally: bd.unmake(...)`）。
//:
//: C++ 的栈回退保证了"抛出时也撤干净" —— 这一点在 VCF 上不是洁癖：
//: `VcfStop` 会被 `vcf()` 就地捕获成 EXHAUSTED，之后 `_search` 还要接着用
//: 同一个 Board 迭代加深；残留的子会让 `scoreSum` 与盘面错位。
struct Unmaker {
    Board* bd;
    int idx;
    int player;
    ~Unmaker() { bd->unmake(idx, player); }
};

//: 置换表下标。**两种几何共用一块数组**，靠 `enhanced` 分支切换：
//:
//:  * `enhanced == false` —— 直接映射：`k & (2^20 - 1)`，落在数组的低半区。
//:    这与旧版 `tt_[k & TT_MASK]` 的映射**逐位相同**（同一个掩码、同样的步长
//:    1、同样的碰撞与覆盖顺序），因此关闭增强时的搜索结果与旧版不可区分。
//:  * `enhanced == true` —— 双路桶：同一个掩码选出桶号，桶内两路连续存放。
//:
//: 两种模式共用同一块内存意味着**跨模式会互相看见对方的条目**（增强搜索写进
//: way 0 的条目，低档搜索在同一个 `k & mask` 上能读到）。这不是缺陷：每个条目
//: 的键都会被逐位比对，读到的是真值，只是"来自哪次搜索"变了 —— 与"置换表跨
//: 请求保留"是同一类现象（见 search.h 里那条注释）。换档位时 `new_game` 会清表。
inline int ttIndex(uint64_t k, bool enhanced, int way) {
    const uint64_t b = k & static_cast<uint64_t>(TT_LEGACY_SIZE - 1);
    return enhanced ? static_cast<int>(b) * TT_WAYS + way
                    : static_cast<int>(b);
}

}  // namespace

// ==================== 生命周期 ====================

Engine::Engine() {
    tt_.assign(TT_ENTRIES, TTEntry{0, 0, 0, 0, -1, 0});
    history_[0].assign(CELLS, 0);
    history_[1].assign(CELLS, 0);
    for (int i = 0; i < MAX_PLY + 2; ++i) {
        killers_[i][0] = -1;
        killers_[i][1] = -1;
    }
}

void Engine::reset() {
    // 整体填零而不是逐条清：置换表是纯缓存，重建只是多搜一点。
    std::fill(tt_.begin(), tt_.end(), TTEntry{0, 0, 0, 0, -1, 0});
    ttAge_ = 0;
    std::fill(history_[0].begin(), history_[0].end(), 0);
    std::fill(history_[1].begin(), history_[1].end(), 0);
    for (int i = 0; i < MAX_PLY + 2; ++i) {
        killers_[i][0] = -1;
        killers_[i][1] = -1;
    }
    // "普通对弈的深度"也要清（见 `normalDepth_` 的注释）。它不是缓存，而是
    // **这盘棋学到的统计量**；留着它会让同一进程里先后跑的几局互相影响 ——
    // 测试与 A/B 就不可复现了，而"可复现"正是这仓库 parity 护栏的全部前提。
    normalDepth_ = 0;
    normalDepthLevel_ = -1;
}

void Engine::poll() {
    if (cancel_ != nullptr && cancel_->load(std::memory_order_relaxed)) {
        throw SearchAborted{"cancelled"};
    }
    // `deadline_ == 0.0` 表示"当前没有搜索在跑"。不认这个约定的话，任何在
    // `think` 之外调用搜索路径的人（`--selftest`、VCF 探针）都会立刻撞上
    // `SearchAborted("timeout")`，因为单调时钟当然大于 0。
    if (deadline_ > 0.0 && nowSec() >= deadline_) {
        timedOut_ = true;
        throw SearchAborted{"timeout"};
    }
}

void Engine::decayHistory() {
    // 不清零而衰减：清零会把"上一步学到的好着"一起丢掉，而跨步的着法偏好
    // 在开局到中局是连续有用的；衰减则让老信息自然过期。
    for (int p = 0; p < 2; ++p) {
        int32_t* h = history_[p].data();
        for (int i = 0; i < CELLS; ++i) {
            if (h[i]) h[i] >>= 1;
        }
    }
}

// ==================== 置换表 ====================

Engine::Probe Engine::lookup(const Board& bd, int me, int depth, int32_t alpha,
                             int32_t beta, int ply) {
    // ⚠️ 这里是相对 Python 的一处**有意的实现差异**：Python 用 dict（精确、
    // 无冲突、满了整体 clear），这里用固定大小的**直接映射**数组，冲突时后写
    // 覆盖先写。
    //
    // 这不影响正确性：键会被逐位比对，撞上别的键就是"未命中"，绝不会读到
    // 别人的分值。差异只在于偶尔丢掉一个本来可以命中的条目 —— 而置换表本来
    // 就是缓存，丢条目只会多搜一点。换来的是每个节点一次数组下标访问，而不是
    // 一次哈希容器的探查与指针追逐（后者在 2M nps 的量级上不可接受）。
    const uint64_t k = bd.hash ^ (me == 1 ? 0ULL : TT_SALT_W);

    // 关闭增强时只认单槽 —— 与旧版逐位相同。开启时在桶内两路里挑**键匹配且
    // 最深**的那条：同键的两条深度不同时，深的那条才是可信的结论。
    //
    // **两套几何的下标是不同的数，别混用。** 旧几何是 `b`，增强几何是
    // `b * TT_WAYS + way` —— 于是 `ttIndex(k, false, 0) != ttIndex(k, true, 0)`。
    // 这里曾经先取旧几何的地址当默认值、再让增强分支去覆盖，结果"只有 way 0
    // 命中"那条路径不会覆盖它：`e` 仍指着 `tt_[b]`，下一行拿它比键，几乎必然
    // 不等，于是**这次命中被扔掉**。实测后果是增强档的 TT 采纳命中率从
    // 1.5%~5.9% 掉到 0.0%~0.2%（`tools/bench.py` 的 TT命中 列）—— 搜索带着
    // 一张近乎失效的置换表在跑，深度不掉、着法变差，非常难看出来。
    const TTEntry* e = nullptr;
    if (enhanced_) {
        const TTEntry* w0 = &tt_[ttIndex(k, true, 0)];
        const TTEntry* w1 = &tt_[ttIndex(k, true, 1)];
        const bool k0 = w0->key == k;
        const bool k1 = w1->key == k;
        if (k0 && k1) {
            e = (w1->depth > w0->depth) ? w1 : w0;
        } else if (k0) {
            e = w0;
        } else if (k1) {
            e = w1;
        } else {
            return Probe{0, -1, false};
        }
    } else {
        e = &tt_[ttIndex(k, false, 0)];
        if (e->key != k) return Probe{0, -1, false};
    }
    if (e->depth < depth) return Probe{0, e->move, false};

    // `ttHits_` 只统计**真正被采用的**条目（下面三条出口），不是"键存在"。
    // 两者差得很远：条目深度不够、或界与当前窗口不相交时，这次查询只贡献了
    // 一个排序用的着法，搜索照样要展开 —— 把它算进"命中率"会让这个数看着很
    // 高而对实际剪枝量一无所知。
    const int32_t v = fromTT(e->value, ply);
    if (e->flag == TT_EXACT) {
        ++ttHits_;
        return Probe{v, e->move, true};
    }
    if (e->flag == TT_LOWER && v >= beta) {
        ++ttHits_;
        return Probe{v, e->move, true};
    }
    if (e->flag == TT_UPPER && v <= alpha) {
        ++ttHits_;
        return Probe{v, e->move, true};
    }
    return Probe{0, e->move, false};
}

void Engine::save(const Board& bd, int me, int depth, int32_t value, int flag,
                  int mv, int ply) {
    const uint64_t k = bd.hash ^ (me == 1 ? 0ULL : TT_SALT_W);
    int slot = ttIndex(k, false, 0);

    if (enhanced_) {
        // 替换策略：**同键优先复用**，否则挑一个牺牲者。
        //
        // 旧版是无条件覆盖，于是"浅搜索的半成品条目挤掉深搜索的好条目"是常态
        // —— 在被时间截断的那一轮迭代里，这个损失尤其明显：那一轮的条目全部
        // 是半成品，却因为它们最后写入而留下。这里用 `age` + `depth` 挡住：
        // 先牺牲上一轮遗留的（`age` 更小），同龄时牺牲更浅的。
        int victim = -1;
        for (int w = 0; w < TT_WAYS; ++w) {
            if (tt_[ttIndex(k, true, w)].key == k) {
                victim = w;
                break;
            }
        }
        if (victim < 0) {
            victim = 0;
            for (int w = 1; w < TT_WAYS; ++w) {
                const TTEntry& a = tt_[ttIndex(k, true, w)];
                const TTEntry& b = tt_[ttIndex(k, true, victim)];
                const bool aEmpty = (a.depth == 0);
                const bool bEmpty = (b.depth == 0);
                if (bEmpty) continue;
                if (aEmpty || a.age < b.age ||
                    (a.age == b.age && a.depth < b.depth)) {
                    victim = w;
                }
            }
        }
        slot = ttIndex(k, true, victim);
    }

    TTEntry& e = tt_[slot];
    e.key = k;
    e.value = toTT(value, ply);
    e.depth = static_cast<int16_t>(depth);
    e.flag = static_cast<uint8_t>(flag);
    e.move = static_cast<int16_t>(mv);
    e.age = static_cast<uint8_t>(ttAge_);
}

// ==================== 走法排序 ====================

void Engine::orderedMoves(const Board& bd, int me, int ttMove, int ply, int* out,
                          MoveOrder* ord) {
    *ord = MoveOrder();
    if (bd.candMask.none()) {
        return;
    }
    const int opp = 3 - me;

    // 分四档，**档与档之间不混用同一套量纲**：置换表着法 → 己方威胁点 →
    // 对方的威胁点（必须应） → 杀手 / 历史。旧 `_order_moves` 是把静态分与
    // 历史分**相加**的，于是"访问次数多的平庸着"能压过"没访问过的杀着"；
    // 这里档位是主键，历史只在最后一档内排序。
    int head = 0;
    Bits361 cand2 = bd.candMask;
    if (ttMove >= 0 && bd.candMask.test(ttMove)) {
        out[head++] = ttMove;
        cand2.clearBit(ttMove);
    }

    // 威胁点图约 12µs/次（Python），**只在真有活三以上棋型时才算** ——
    // 判断本身是 O(1) 的 threatPower。
    Bits361 five = Bits361::zero();
    Bits361 hot = Bits361::zero();
    Bits361 block = Bits361::zero();
    if (bd.threatPower[me - 1] || bd.threatPower[opp - 1]) {
        if (enhanced_) {
            // 增强搜索把"这一手直接成五"从 `hot` 里**单独提出来**排在最前。
            // 旧版把它混在 `hot` 里按升序枚举，于是"左边那个冲四点"完全可能
            // 排在自己的成五点前面 —— 而后者下一手就赢。这不是深度问题，是
            // 排序问题：搜索照样会展开成五点，但要多花一道零窗口试探才能认出
            // 它，而每个节点都这么浪费一次，代价就摊到整棵树上。
            five = fivePoints(bd.bitsOf(me), bd.bitsOf(opp)) & cand2;
        }
        hot = hotPoints(bd.bitsOf(me), bd.bitsOf(opp)) & cand2 & ~five;
        block = hotPoints(bd.bitsOf(opp), bd.bitsOf(me)) & cand2 & ~(five | hot);
    }
    Bits361 rest = cand2 & ~(five | hot | block);

    if (five.any()) {
        forEachBitAscending(five, [&](int i) { out[head++] = i; });
    }
    ord->hotFirst = head;
    ord->hotCount = 0;
    if (five.any()) {
        // `hotFirst` 落在成五点的起点上，所以强制着法区间要把成五点算进来。
        // 它们同样是"自己造出了必须应的威胁"，延伸判据对两者的处理一致。
        ord->hotCount += static_cast<int>(five.count());
    }
    if (hot.any()) {
        forEachBitAscending(hot, [&](int i) { out[head++] = i; });
        ord->hotCount += static_cast<int>(hot.count());
    }
    if (block.any()) {
        forEachBitAscending(block, [&](int i) { out[head++] = i; });
    }
    for (int k = 0; k < 2; ++k) {
        const int kv = killers_[ply][k];
        if (kv >= 0 && rest.test(kv)) {
            out[head++] = kv;
            rest.clearBit(kv);
        }
    }
    ord->nPriority = head;

    int* tail = out + head;
    int tailN = 0;
    forEachBitAscending(rest, [&](int i) { tail[tailN++] = i; });
    if (tailN > 1) {
        // **必须是 `stable_sort`。** Python 的 `list.sort` 是稳定排序，
        // `reverse=True` 让同键元素保持原来的升序索引顺序。换成 `std::sort`
        // 会让同键着法的相对顺序变成未定义 —— 而"升序"正是 `_mask_cells`
        // 传下来的顺序，它决定了 VCF 与根节点的候选次序。
        const int32_t* hist = history_[me - 1].data();
        const uint8_t* nc = bd.neighborCount;
        std::stable_sort(tail, tail + tailN, [&](int a, int b) {
            if (hist[a] != hist[b]) return hist[a] > hist[b];
            return nc[a] > nc[b];
        });
    }
    ord->n = head + tailN;
}

void Engine::recordCutoff(int me, int mv, int ply, int depth) {
    int* k = killers_[ply];
    if (k[0] != mv) {
        k[1] = k[0];
        k[0] = mv;
    }
    history_[me - 1][mv] += depth * depth;
}

// ==================== 静止搜索 ====================

int32_t Engine::quiesce(Board& bd, int32_t alpha, int32_t beta, int me, int ply,
                        int qleft) {
    ++nodes_;
    ++qnodes_;
    if (!(nodes_ & ABORT_MASK)) poll();
    if (ply >= MAX_PLY - 1 || qleft <= 0) return evaluate(bd, me);

    const int opp = 3 - me;

    // 没有活三以上棋型 ⇒ 双方都不存在四点，更不存在五点 ⇒ 没有任何强制着法
    // 可展开。这一句挡掉了绝大多数静止节点，而且结论与往下走完全一致。
    if (!(bd.threatPower[me - 1] || bd.threatPower[opp - 1])) {
        return evaluate(bd, me);
    }

    const Bits361 myBits = bd.bitsOf(me);
    const Bits361 opBits = bd.bitsOf(opp);

    const Bits361 myFive = fivePoints(myBits, opBits);
    if (myFive.any()) return WIN_SCORE - ply;      // 轮到我，且我能一步成五

    const Bits361 oppFive = fivePoints(opBits, myBits);
    Bits361 cand;
    int32_t best;
    if (oppFive.any()) {
        if (oppFive.count() > 1) {
            // 两个成五点，挡一个漏一个 —— 必输，且**距离是已知的**：我在这里
            // (ply) 落子挡一个，对手在下个节点(ply+1)成五。分值与上面「我能
            // 成五」共用同一套 ply 约定。写成 -2 会让"必输"的距离少算一步，
            // 在"几条败线里挑最长的"时选错。
            return -(WIN_SCORE - ply - 1);
        }
        cand = oppFive;
        best = -INF;                     // 禁止 stand-pat：必须挡
    } else {
        cand = hotPoints(myBits, opBits) | hotPoints(opBits, myBits);
        if (cand.none()) return evaluate(bd, me);
        best = evaluate(bd, me);
        if (best >= beta) return best;
        if (best > alpha) alpha = best;
    }

    for (int wi = 0; wi < Bits361::WORDS; ++wi) {
        uint64_t m = cand.w[wi];
        while (m) {
            const int bit = __builtin_ctzll(m);
            m &= m - 1;
            const int mv = (wi << 6) + bit;
            int32_t val;
            const bool five = bd.make(mv, me);
            if (five) {
                bd.unmake(mv, me);
                val = WIN_SCORE - ply;
            } else {
                val = -quiesce(bd, -beta, -alpha, opp, ply + 1, qleft - 1);
                bd.unmake(mv, me);
            }
            if (val > best) best = val;
            if (val > alpha) alpha = val;
            if (alpha >= beta) return best;
        }
    }
    return best;
}

// ==================== 主搜索 ====================

int32_t Engine::negamax(Board& bd, int depth, int32_t alpha, int32_t beta,
                        int me, int ply) {
    // 搜索窗口内**必然没有五连**（成五的分支在上一层就已返回），因此这里不
    // 需要任何"检查终局"的动作 —— 这既是位棋盘增量胜负判定的前提，也是旧版
    // 每节点一次全盘扫描被彻底去掉的原因。
    ++nodes_;
    if (!(nodes_ & ABORT_MASK)) poll();
    if (ply >= MAX_PLY - 1) return evaluate(bd, me);
    if (depth <= 0) return quiesce(bd, alpha, beta, me, ply, qply_);

    const int32_t alphaOrig = alpha;      // 必须在改动之前留存 —— B2 的根因
    const Probe p = lookup(bd, me, depth, alpha, beta, ply);
    if (p.hit) return p.value;

    MoveOrder ord;
    orderedMoves(bd, me, p.move, ply, moveBuf_[ply], &ord);
    if (ord.n == 0) return evaluate(bd, me);       // 满盘
    int* moves = moveBuf_[ply];

    //: 这条线上累计延伸了几层（见 `EXT_MAX_DEBT`）。不延伸时每步
    //: `ply+1, depth-1` 和不变，延伸时 `depth` 不减、和加一 —— 所以它恰是欠账。
    const int debt = ply + depth - iterRootDepth_;

    const int nxt = 3 - me;
    int32_t best = -INF;
    int bestMove = moves[0];
    for (int i = 0; i < ord.n; ++i) {
        const int mv = moves[i];
        if (bd.make(mv, me)) {
            bd.unmake(mv, me);
            best = WIN_SCORE - ply;       // 一步成五，不可能更好
            bestMove = mv;
            break;
        }

        // ---- 强制着法延伸 ----
        // 这一手自己造出了成五/冲四（`hot` 区间），对手**必须**应招，于是这条
        // 线是强制的、不是普通的安静分支，不该按普通一手折损一层深度。这正是
        // "把算力花在真正的变化上"：五子棋的胜负几乎都发生在强制序列里。
        int nd = depth - 1;
        if (extend_ && depth >= EXT_MIN_DEPTH && debt < EXT_MAX_DEBT &&
            i >= ord.hotFirst && i < ord.hotFirst + ord.hotCount) {
            nd = depth;
        }

        int32_t v;
        if (i == 0) {
            v = -negamax(bd, nd, -beta, -alpha, nxt, ply + 1);
        } else {
            // ---- 晚着法削减（LMR）----
            // 只削 `i >= nPriority` 的**安静着法**：置换表着法、强制着法、被动
            // 挡点、杀手全部豁免 —— 削减一个"必须应"的着法等于让搜索看不见
            // 对手的杀棋，那是这类引擎最贵的错误形态。
            int r = 0;
            if (lmr_ && nd >= LMR_MIN_DEPTH && i >= ord.nPriority) {
                r = 1 + (i >= LMR_MORE_MOVES ? 1 : 0) + (nd >= LMR_DEEP ? 1 : 0);
                if (nd - r < 1) r = nd - 1;
            }
            v = -negamax(bd, nd - r, -alpha - 1, -alpha, nxt, ply + 1);
            if (r > 0 && v > alpha) {
                // 被削减的那一手打赢了零窗口 —— **必须按未削减的深度重搜**。
                // 不重搜就等于拿浅搜索的结论冒充深搜索的结论，而削减的前提本来
                // 就是"这一手大概不行"；它一旦行了，前提就没了。这一步是 LMR
                // 能保持健全的全部理由。
                v = -negamax(bd, nd, -alpha - 1, -alpha, nxt, ply + 1);
            }
            // PVS 零窗口试探；重搜条件是 **严格不等** `alpha < v < beta`。
            if (alpha < v && v < beta) {
                v = -negamax(bd, nd, -beta, -alpha, nxt, ply + 1);
            }
        }
        bd.unmake(mv, me);
        if (v > best) {
            best = v;
            bestMove = mv;
        }
        if (v > alpha) alpha = v;
        if (alpha >= beta) {
            recordCutoff(me, mv, ply, depth);
            break;
        }
    }

    save(bd, me, depth, best, flagOf(best, alphaOrig, beta), bestMove, ply);
    return best;
}

void Engine::root(Board& bd, int me, int depth, int firstMove, int32_t alpha,
                  int32_t beta, int* outMove, int32_t* outValue,
                  std::vector<std::pair<int, int32_t>>* collect) {
    // 根与普通节点的区别只有一处：**不必为"某条线路被剪掉"负责**，因为根层
    // 要返回的永远是分值最高的那条路。所以这里不做 PVS 的零窗口试探 ——
    // 根层少一层间接，且根层节点数占总数的比例极小。
    MoveOrder ord;
    orderedMoves(bd, me, firstMove, 0, moveBuf_[0], &ord);
    if (ord.n == 0) {
        *outMove = -1;
        *outValue = 0;
        return;
    }
    int* moves = moveBuf_[0];
    const int nxt = 3 - me;
    int32_t best = -INF;
    int bestMove = moves[0];
    if (collect != nullptr) collect->clear();

    // 对手的**即成五点**（下一手落上就成五）与**四点**（下一手造出四或五）。
    //
    // 并列的**杀棋分**、且是**我方被杀**时，优先占住这样的点。理由：分值相同
    // 意味着"怎么走都是同一个死法"，此时 `v > best` 不再含任何信息，选点退化
    // 成由排序（TT 着法 / 杀手 / 历史）决定 —— 实测会往角上点 A1。而占住对手的
    // 要点是唯一还有意义的一手：对手若走出缓手，这一手就是生路。
    //
    // **只在我方被杀的杀棋分并列时生效**：静态分并列与"我方必胜"并列都保持
    // 原样，正常着法路径一字不动，因此低档与参考实现的逐字一致不受影响
    // （`engine_local._root` 同款）。必胜时不做这个偏好是刻意的 —— 那时并列的
    // 几手本来就都能赢，改成防守只会让人疑惑"你怎么不直接赢"。
    const Bits361 oppFive = fivePoints(bd.bitsOf(nxt), bd.bitsOf(me));
    const Bits361 oppHot = hotPoints(bd.bitsOf(nxt), bd.bitsOf(me));
    int bestRank = -1;

    for (int i = 0; i < ord.n; ++i) {
        const int mv = moves[i];
        if (bd.make(mv, me)) {
            bd.unmake(mv, me);
            if (collect != nullptr) collect->push_back({mv, WIN_SCORE});
            *outMove = mv;
            *outValue = WIN_SCORE;
            return;
        }
        const int32_t v = -negamax(bd, depth - 1, -beta, -alpha, nxt, 1);
        bd.unmake(mv, me);
        if (collect != nullptr) collect->push_back({mv, v});
        // 这一手占住对手要点的程度：挡即成五点 2 分，挡四点 1 分，其余 0 分。
        const int rank =
            oppFive.test(mv) ? 2 : (oppHot.test(mv) ? 1 : 0);
        if (v > best) {
            best = v;
            bestMove = mv;
            bestRank = rank;
        } else if (v == best && isMate(v) && v < 0 && rank > bestRank) {
            // 并列的杀棋分：换成那手占住对手要点的。**分值不变**，只是不再由
            // 排序噪声决定 —— 见上面 `oppFive` / `oppHot` 的注释。
            bestMove = mv;
            bestRank = rank;
        }
        if (v > alpha) alpha = v;
        if (alpha >= beta) break;
    }
    *outMove = bestMove;
    *outValue = best;
}

// ==================== 连续冲四（VCF） ====================

VcfResult Engine::vcf(Board& bd, int me, double budget, bool hasDeadline,
                      double deadline) {
    const double now = nowSec();
    if (!hasDeadline) {
        vcfNodes_ = 0;
        vcfNodeCap_ = cfgVcfNodeCap_;
        deadline = now + budget;
    }
    // 主搜索的截止时刻也约束 VCF，但**只在上一次 `think` 还没结束、或它的
    // 截止时刻本来就在未来时才生效**：`think` 返回后 `deadline_` 会留在过去，
    // 若照着它算，任何 `think` 之外的独立 VCF 调用都会立刻 EXHAUSTED。
    if (now < deadline_) deadline = std::min(deadline, deadline_);
    vcfDeadline_ = deadline;
    try {
        return vcfRec(bd, me, 0, VCF_MAX_PLY);
    } catch (const VcfStop&) {
        return VcfResult{VCF_EXHAUSTED, -1, 0};
    }
}

void Engine::vcfTick() {
    ++vcfNodes_;
    // 第 1 个节点也查一次表：预算小到"一个节点都跑不完"时，等到第 256 个
    // 节点才检查意味着这几百个节点白跑。
    if (vcfNodes_ > 1 && (vcfNodes_ & VCF_POLL_MASK)) return;
    // `poll()` 会让 `SearchAborted` 穿透上去（那是"整个搜索该停了"），而本地
    // 预算耗尽抛 `VcfStop`（那是"这套方法算不完了"）。两者在 `vcf()` 里被分开
    // 处理 —— 混成一个会让预算耗尽被当成取消。
    poll();
    if (nowSec() >= vcfDeadline_ || vcfNodes_ >= vcfNodeCap_) throw VcfStop{};
}

VcfResult Engine::vcfRec(Board& bd, int me, int ply, int left) {
    vcfTick();
    if (left <= 0) throw VcfStop{};

    const int opp = 3 - me;
    const Bits361 myBits = bd.bitsOf(me);
    const Bits361 opBits = bd.bitsOf(opp);

    const Bits361 myFive = fivePoints(myBits, opBits);
    if (myFive.any()) {
        // 我这一手就成五。dist=1 与「再走一手落子」的字面意思一致。
        return VcfResult{VCF_WIN, myFive.lowestIndex(), 1};
    }
    // **进攻方节点上一旦对手有成五点，本节点立即 NO_WIN**，因为四压不住五
    // —— 我下一手做四，对手直接成五。这是"进攻方假必胜"唯一需要的一行判定。
    if (fivePoints(opBits, myBits).any()) return VcfResult{VCF_NO_WIN, -1, 0};

    const Bits361 cand = hotPoints(myBits, opBits);
    if (cand.none()) return VcfResult{VCF_NO_WIN, -1, 0};  // 连四都做不出来

    bool exhausted = false;
    for (int wi = 0; wi < Bits361::WORDS; ++wi) {
        uint64_t m = cand.w[wi];
        while (m) {
            const int bit = __builtin_ctzll(m);
            m &= m - 1;
            const int mv = (wi << 6) + bit;

            const bool five = bd.make(mv, me);
            Unmaker outer{&bd, mv, me};
            if (five) return VcfResult{VCF_WIN, mv, 1};

            const Bits361 f = fivePoints(bd.bitsOf(me), bd.bitsOf(opp));
            if (f.count() >= 2) {
                // 活四 / 双四：两个成五点，对手只能堵一个。距离是 3 ——
                // 我(ply)成四，他(ply+1)堵一个，我(ply+2)成五。
                return VcfResult{VCF_WIN, mv, 3};
            }
            if (f.any()) {
                // 恰有一个成五点 ⇒ 对手的应手是**唯一**的（不堵就输），
                // 所以这里对"所有应手"的 AND 只剩一项。
                const int bidx = f.lowestIndex();
                bd.make(bidx, opp);
                int st = VCF_NO_WIN;
                int sub = 0;
                {
                    Unmaker inner{&bd, bidx, opp};
                    const VcfResult r = vcfRec(bd, me, ply + 2, left - 1);
                    st = r.state;
                    sub = r.dist;
                }
                if (st == VCF_WIN) return VcfResult{VCF_WIN, mv, sub + 2};
                if (st == VCF_EXHAUSTED) {
                    // **不当作 NO_WIN**：这条线的结论是"不知道"。记下来，
                    // 继续试别的起手 —— 别的起手若真的赢，WIN 仍然可信。
                    exhausted = true;
                }
            }
        }
    }
    return exhausted ? VcfResult{VCF_EXHAUSTED, -1, 0}
                     : VcfResult{VCF_NO_WIN, -1, 0};
}

int Engine::vcfDefence(Board& bd, int me, int opp, double deadline) {
    // 候选集是"对手会用来起手做四的所有格点"—— 对手的 VCF 只能从这些格子里
    // 起步，我占掉其中一格，那条线就没了。逐格试过去、每格都重跑一次对手的
    // VCF，要求结果是 `VCF_NO_WIN`（**`VCF_EXHAUSTED` 不算挡住**，那只是
    // "没算完"）。
    //
    // `deadline` 是**整个 VCF 阶段**的绝对截止时刻，由 `think` 统一给出 ——
    // 若每个候选各自计时，总时长会变成"候选数 × 预算"。
    const Bits361 cand = hotPoints(bd.bitsOf(opp), bd.bitsOf(me));
    for (int wi = 0; wi < Bits361::WORDS; ++wi) {
        uint64_t m = cand.w[wi];
        while (m) {
            const int bit = __builtin_ctzll(m);
            m &= m - 1;
            const int mv = (wi << 6) + bit;
            if (nowSec() >= deadline) return VCF_DEFENCE_UNKNOWN;
            bd.make(mv, me);
            int st;
            {
                Unmaker um{&bd, mv, me};
                st = vcf(bd, opp, 0.0, true, deadline).state;
            }
            if (st == VCF_NO_WIN) return mv;
        }
    }
    // 候选集扫完了，没有一手能破坏它。**这不等于必败**：候选集只是"对手可以
    // 用来做四的格点"，挡点可能在集合之外，也可以靠反冲四解。所以它只能用来
    // 排序，不能拿它编一个杀棋分。
    return VCF_DEFENCE_NONE;
}

// ==================== 入门档的贪吃系数 ====================

int Engine::applyBias(Board& bd, int me, const SearchConfig& cfg,
                      const std::vector<std::pair<int, int32_t>>& vals,
                      int32_t bestVal) {
    // 搜索本身照常跑（健全、零和），这里只在**根节点**拿到各候选着法的分值
    // 之后做一次重排：先按 `bestVal - biasTolerance` 划出容差带，带内再按
    // 「己方进攻 × biasAttack − 对手威胁 × biasDefence」选一个。
    //
    // 为什么不是"把偏置注入 evaluate()"：`evaluate` 必须**严格零和**
    // （`evaluate(b,1) == -evaluate(b,2)`，`tests/test_eval.py` 钉住），而
    // negamax 的正确性完全依赖零和。若按「己方攻×1.5、对手威胁×0.5」注入
    // evaluate，两者不再互为相反数 → 搜索变成不健全的，且 `best_val`（面板
    // 两张图的唯一数据源）会带上随 `me` 变化的偏置，图表被污染。
    //
    // 与 plan 的公式 `val + W_attack×own − W_defence×opp` 的差别只有一处：
    // 这里把 `val` 的作用交给**容差带**（带外的直接出局）而不是当成一个加项。
    // 两者在带宽趋于 0 时等价，而不加权地把量纲不同的 `val`（搜索分，可达
    // ±1e7）与 `own/opp`（静态分，可达 1e9）直接相加会让加法项彻底淹没 val。
    // 用户要的观感 ——「对手活三(30000) 时我自己做个冲四(20000×1.5)」要能赢过
    // 防守 —— 由 `1.5×own − 0.5×opp` 这一项直接给出，与容差带的宽窄无关。
    const int opp = 3 - me;
    const int32_t floorVal = bestVal - static_cast<int32_t>(cfg.biasTolerance);
    int pick = -1;
    double pickScore = 0.0;
    for (const auto& kv : vals) {
        if (kv.second < floorVal) continue;
        if (isMate(kv.second)) continue;   // 杀棋结论不可被偏置改写
        const int mv = kv.first;
        bd.make(mv, me);
        const int64_t own =
            bd.scoreSum[me - 1] + comboBonus(bd.threatAgg[me - 1]);
        const int64_t oth =
            bd.scoreSum[opp - 1] + comboBonus(bd.threatAgg[opp - 1]);
        bd.unmake(mv, me);
        const double s = cfg.biasAttack * static_cast<double>(own) -
                         cfg.biasDefence * static_cast<double>(oth);
        if (pick < 0 || s > pickScore) {
            pick = mv;
            pickScore = s;
        }
    }
    return pick;
}

// ==================== 顶层调度 ====================

int Engine::think(const uint8_t* board, int me, const SearchConfig& cfg,
                  const std::atomic<bool>* cancel, Info* info) {
    int result = -1;
    try {
        cancel_ = cancel;
        me_ = me;
        qply_ = cfg.qply;
        // 抄一份到成员里：`negamax` 拿不到 `cfg`，而它正是要做削减决策的地方。
        // `enhanced_ == false` 时下面每一条路径都与"没有这套机制"逐位相同。
        enhanced_ = cfg.enhanced != 0;
        lmr_ = enhanced_ && cfg.lmr != 0;
        extend_ = enhanced_ && cfg.extend != 0;
        iterRootDepth_ = 0;
        timedOut_ = false;
        nodes_ = 0;
        qnodes_ = 0;
        ttHits_ = 0;
        vcfNodes_ = 0;
        vcfNodeCap_ = cfg.vcfNodeCap;
        cfgVcfNodeCap_ = cfg.vcfNodeCap;
        ++ttAge_;
        decayHistory();

        const double t0 = nowSec();
        deadline_ = t0 + cfg.timeLimit * (1.0 - RESERVE);

        Board bd = Board::fromArray(board);

        *info = Info();
        int cand[CELLS + 1];
        int ncand = 0;
        bd.candidates(cand, &ncand);
        if (ncand == 0) {
            deadline_ = 0.0;
            cancel_ = nullptr;
            return -1;
        }

        int bestMove = cand[0];
        int32_t bestVal = 0;
        int done = 0;
        int first = bestMove;

        // ---- VCF 快速通道 ----
        //
        // VCF 的结论是**已证明**的，但它只回答"赢不赢"，不回答"多快"。因此它
        // 不直接落子，而是做两件事：把已证明的杀棋线记下来当**兜底结论**，再让
        // 主搜索照常跑一遍 —— 搜索若找到**更短**的杀棋就用搜索的（更短的杀棋
        // 更不容易在下棋过程中走错），找不到就用 VCF 的。两者都是可证的。
        //
        // 对手存在 VCF 时同理只改**走法排序**：VCF 能证明的只是"对手那条冲四
        // 链被破坏了"，这个局面的分值它一无所知，拿它当返回值就是编分数。
        const double vcfBudget = cfg.vcfBudget;
        const double vcfPhaseDeadline = t0 + vcfBudget;
        bool haveVcfWin = false;
        int vcfWinMove = -1;
        int32_t vcfWinVal = 0;
        int vcfDef = VCF_DEFENCE_NONE;
        if (vcfBudget > 0.0) {
            try {
                const VcfResult r = vcf(bd, me, vcfBudget, false, 0.0);
                info->vcfActive = true;
                info->vcfState = r.state;
                info->vcfNodes = vcfNodes_;
                if (r.state == VCF_WIN) {
                    // dist 是"还有几手落下那颗成五的子"。按 ply 约定（在 ply
                    // 这个节点上成五 → WIN_SCORE - ply）：五落在 ply = dist-1。
                    haveVcfWin = true;
                    vcfWinMove = r.move;
                    vcfWinVal = WIN_SCORE - (r.dist - 1);
                    first = bestMove = r.move;
                    info->vcfDist = r.dist;
                } else {
                    const int opp = 3 - me;
                    const VcfResult o = vcf(bd, opp, 0.0, true, vcfPhaseDeadline);
                    if (o.state == VCF_WIN) {
                        // 威胁有多深是**已经算出来的事实**，两个分支都记 ——
                        // 只在找到挡点时记，日志里就看不出"对手还有几手成杀"，
                        // 而诊断败局时缺的正是这个数。
                        info->vcfDist = o.dist;
                        const int d = vcfDefence(bd, me, opp, vcfPhaseDeadline);
                        if (d >= 0) {
                            // 只把它排到第一位，**不写 reason** —— 搜索完全
                            // 可能找到更好的着法（比如自己的杀棋），那时说
                            // "这是 VCF 挡点"就是假话。reason 在循环后按最终
                            // 着法补。
                            first = bestMove = d;
                            vcfDef = d;
                        } else if (d == VCF_DEFENCE_UNKNOWN) {
                            // 没扫完 ≠ 没有挡点。这两种情况以前共用一个 -1，
                            // 日志里说同一句话，等于把"不知道"讲成了"算过了"。
                            info->reason = "PVS搜索(对手有VCF·未算完)";
                        } else {
                            info->reason = "PVS搜索(对手有VCF·无挡点)";
                        }
                    }
                }
            } catch (const SearchAborted&) {
                // VCF 阶段撞上主搜索的截止/取消：改成"不表态"，照常进入迭代
                // 加深。这里**不能**把 EXHAUSTED 当 NO_WIN —— 那正是 B15。
                info->vcfActive = true;
                info->vcfState = VCF_EXHAUSTED;
                timedOut_ = false;      // 交给迭代循环自己处理时间
            }
        }

        const bool biasOn = cfg.biasAttack != 0.0 || cfg.biasDefence != 0.0;
        std::vector<std::pair<int, int32_t>> rootVals;
        std::vector<std::pair<int, int32_t>> curVals;

        // ---- 时间管理的记账 ----
        // 每轮迭代的实测成本（节点数与墙钟）是预估下一轮的唯一样本。
        int64_t prevIterNodes = 0;        // 上一轮消耗的节点数
        int64_t prevPrevIterNodes = 0;    // 上上轮，用来估有效分支因子
        double lastIterSec = 0.0;

        // ---- 被判死刑时的加码额度（见 constants.h 的 ESCAPE_EXTRA） ----
        // 档位变了就把"普通深度"作废：同一个 `Engine` 会连着服务不同档位。
        if (cfg.maxDepth != normalDepthLevel_) {
            normalDepth_ = 0;
            normalDepthLevel_ = cfg.maxDepth;
        }
        const int normalDep =
            normalDepth_ > 0 ? normalDepth_ : ESCAPE_FALLBACK_NORMAL;
        escapeTarget_ = std::min(cfg.maxDepth, normalDep + ESCAPE_EXTRA);

        for (int depth = 1; depth <= cfg.maxDepth; ++depth) {
            // ---- 这一轮值不值得开 ----
            //
            // 旧版是**二元**的：开一轮，跑到 `deadline_` 就被 `SearchAborted`
            // 丢掉全部结果。而丢掉的代价不只是时间 —— TT 写入**不回滚**，被截断
            // 那一轮留下的全是半成品条目。实测"预算 15 s → 20 s 在宽局面上多
            // 0 层"正是这个机制：多出来的时间没换成深度，只换成了更多的废条目。
            //
            // 判据刻意宽松（要超过剩余时间的 `TIME_SOFT_MUL` 倍才放弃）：预估
            // 本身很粗，宁可让它试 —— 试了如果刚好搜完就是净赚。只掐掉那种
            // "预估成本远大于剩余时间"的、明确无望的一轮。
            //
            // `done > 0` 是硬条件：一轮都没完成时没有结果可返回，不能跳过。
            if (enhanced_ && done > 0 && lastIterSec > 0.0) {
                double ebf = 3.0;         // 名义有效分支因子
                if (prevPrevIterNodes > 0) {
                    ebf = static_cast<double>(prevIterNodes) /
                          static_cast<double>(prevPrevIterNodes);
                }
                ebf = std::min(8.0, std::max(1.5, ebf));
                const double predicted = lastIterSec * ebf;
                const double remaining = deadline_ - nowSec();
                // 早退还要过第二道闸：**预算得先花掉相当一部分**。理由与实测
                // 数字见 `constants.h` 的 `TIME_SOFT_MIN_USED` —— 单独用上面那
                // 条判据时，短预算下会在只花掉 26% 时就收工，比基线还浅一层。
                const double budget = cfg.timeLimit * (1.0 - RESERVE);
                const double used = budget - remaining;
                if (used >= budget * TIME_SOFT_MIN_USED &&
                    predicted > remaining * TIME_SOFT_MUL) {
                    break;
                }
            }

            // 渴望窗口与根节点全量收集（偏置重排要用）不能并存：渴望窗口会用
            // 窄窗剪掉一部分根着法，那些着法就没有分值可比。
            const bool aspiration =
                !biasOn && !(depth < 3 || bestVal <= -STATIC_MAX);
            int mv = -1;
            int32_t val = 0;
            // 延伸的"路径欠账"以这一轮的根深度为基准（见 EXT_MAX_DEBT）。
            iterRootDepth_ = depth;
            const int64_t iterT0 = nodes_;
            const double iterSecT0 = nowSec();
            try {
                if (aspiration) {
                    int d = ASPIRATION;
                    val = bestVal;
                    mv = first;
                    bool ok = false;
                    for (int a = 0; a < ASP_FAILS; ++a) {
                        root(bd, me, depth, first, bestVal - d, bestVal + d, &mv,
                             &val, nullptr);
                        if (bestVal - d < val && val < bestVal + d) {
                            ok = true;
                            break;
                        }
                        d *= ASP_WINDOW_MUL;
                    }
                    if (!ok) {
                        root(bd, me, depth, first, -INF, INF, &mv, &val, nullptr);
                    }
                } else {
                    root(bd, me, depth, first, -INF, INF, &mv, &val,
                         biasOn ? &curVals : nullptr);
                }
            } catch (const SearchAborted&) {
                break;
            }
            if (mv < 0) break;
            bestMove = mv;
            bestVal = val;
            done = depth;
            first = mv;
            if (biasOn) rootVals.swap(curVals);
            prevPrevIterNodes = prevIterNodes;
            prevIterNodes = nodes_ - iterT0;
            lastIterSec = nowSec() - iterSecT0;
            // 已找到**必胜**就不必再深搜：更深的迭代只会重复同一结论，却要
            // 花掉数倍时间。
            //
            // ⚠️ 这里**只对"我赢了"成立**。`isMate(val)` 对"我被将死"同样为真，
            // 而那正是绝不能收工的情形 —— 那正是"直接放弃"。被判死刑时继续
            // 加深，用更广的搜索找出路；允许搜到第几层由 `escapeTarget_` 卡死
            // （普通深度 +1，且严格小于 +2，见 constants.h 的 ESCAPE_EXTRA）。
            if (isMate(val)) {
                if (val > 0) break;
                if (depth >= escapeTarget_) break;
            }
        }

        // VCF 兜底：搜索没找到**更快**的杀棋时，用 VCF 的结论。判据只需比较
        // 分值 —— 杀棋分随距离单调（越短越大），所以"搜索的分不更大"就等价于
        // "搜索的杀棋不更快、或搜索压根没看到杀棋（静态分上限 STATIC_MAX 比
        // 任何杀棋分都小）"。两条线都是可证的。
        //
        // 这里用 `<=` 而不是 `<`：相等意味着**同一手、同样快**，走法本来就已
        // 经被 VCF 排在第一位（见上面 `first = bestMove = r.move`），搜索只是
        // 复述了一遍。此时若判给"PVS搜索"，日志里就会出现"nodes=0"配
        // "reason=PVS搜索"这种自相矛盾的行 —— 只有立刻成五的近路会返回 0 个
        // 节点，那个零正说明搜索什么都没做。
        if (haveVcfWin && bestVal <= vcfWinVal) {
            bestMove = vcfWinMove;
            bestVal = vcfWinVal;
            info->reason = "连续冲四(VCF)";
        } else if (vcfDef >= 0 && bestMove == vcfDef) {
            // 搜索最终选的正是 VCF 算出来的那个挡点。"挡点"是 VCF 证明的，
            // 分值仍是搜索给的 —— reason 只说走法的来路，不说分值的来路。
            info->reason = "连续冲四防守(VCF)";
        }

        if (biasOn && !rootVals.empty() && !isMate(bestVal)) {
            // ⚠️ `bd` 从搜索里回来时**可能是脏的**，重建一次再交给偏置。
            //
            // 迭代加深的最后一轮被时限打断时，`SearchAborted` 从 `negamax` 里抛
            // 出来，沿途每一层的 `bd.make()` 都没走到配对的 `unmake`（这条搜索
            // 路径刻意不维护撤销栈，见 board.h 的注释），于是那条被放弃的路径上
            // 的棋子全部留在根棋盘上。搜索本身不受影响 —— 抛出的那一刻这一轮的
            // 结果就被丢掉了。但 `applyBias` 是搜索**之后**读这张棋盘的，它读到
            // 的会是"真实局面 + 半条搜索路径"，`own/opp` 因此完全失真。
            //
            // 这不是推测：`[start] 子=5` 与 `[entry] 子=13` 是实测（同一局面、
            // 同一进程）。失真的后果是偏置的选点**随机器负载漂移** —— 被放弃的
            // 路径取决于在哪一毫秒撞上截止，而不同路径给出不同的 `own/opp`。
            // 未修之前，入门档的"贪吃系数"实际是在一张幻影盘面上做重排。
            //
            // 修法是从请求重建根棋盘，而不是给搜索加撤销栈：重建是一次 361 格
            // 的拷贝，只在一轮被打断之后发生，代价可忽略；撤销栈则要在热路径上
            // 多一次 push/pop 并改掉所有 make/unmake 的配对方式。
            bd = Board::fromArray(board);
            const int picked = applyBias(bd, me, cfg, rootVals, bestVal);
            if (picked >= 0) bestMove = picked;
        }

        // ---- 记下"普通对弈的深度"（见 constants.h 的 ESCAPE_EXTRA） ----
        // **只记没有杀棋结论的那些手**：出了杀棋就不再是"普通对弈"，把它当
        // 样本会让本档的普通深度被自己的应急搜索一层层顶高。位置在这里而不是
        // 迭代循环里，是因为下面的 VCF 兜底**可以**把 `bestVal` 换成杀棋分
        // （`haveVcfWin` 那一支），而那只手同样不该算样本。
        if (done > 0 && !isMate(bestVal)) normalDepth_ = done;

        const double dt = (nowSec() - t0) * 1000.0;
        info->depth = done;
        info->actualDepth = done;
        info->bestVal = bestVal;
        info->timeMs = dt;
        info->nodes = nodes_;
        info->nps =
            dt > 0.0 ? static_cast<int64_t>(static_cast<double>(nodes_) /
                                            (dt / 1000.0))
                     : 0;
        info->ttHitRate =
            nodes_ ? static_cast<double>(ttHits_) / static_cast<double>(nodes_)
                   : 0.0;
        info->qnodeRatio =
            nodes_ ? static_cast<double>(qnodes_) / static_cast<double>(nodes_)
                   : 0.0;
        info->scoreType = isMate(bestVal) ? "mate" : "static";
        result = bestMove;
    } catch (const SearchAborted&) {
        // 理论上到不了这里（每一层都就地捕获了），留着是为了**不让异常穿出
        // 服务端** —— 一次崩溃的搜索不该带走整个进程。
        result = -1;
    }
    // **无论正常返回还是抛异常，`deadline_` 与 `cancel_` 都会被清掉。**
    // 这不是洁癖：`poll` 用 `deadline_ > 0.0` 表示"当前有搜索在跑"，而 `vcf()`
    // 是公开 API，可以在 `think` 之外被调用。留着上一次搜索的截止时刻，那个
    // 时刻**早已过去**，于是独立调用的 `vcf()` 会在第一个节点上撞
    // `SearchAborted`。
    deadline_ = 0.0;
    cancel_ = nullptr;
    return result;
}

}  // namespace gomoku

// DFPN 的实现。设计、三态与两种模式的取舍见 `dfpn.h`。

#include "dfpn.h"

#include <algorithm>
#include <chrono>
#include <vector>

#if GOMOKU_BAN
#include "ban.h"
#endif

namespace gomoku {

namespace {

//: 证明数的"无穷大"。取 UINT32_MAX/4 而不是 UINT32_MAX：求和要能饱和而
//: 不溢出，留出的余量足够容纳任何真实的分支数（≤ CELLS）。
constexpr uint32_t PROOF_INF = UINT32_MAX / 4;

inline uint32_t addSat(uint32_t a, uint32_t b) {
    const uint64_t s = static_cast<uint64_t>(a) + static_cast<uint64_t>(b);
    return s >= PROOF_INF ? PROOF_INF : static_cast<uint32_t>(s);
}

//: 节点的终局类别。**没有 UNKNOWN** —— 算不完一律抛 `DfpnStop`，由整个搜索
//: 变成 UNKNOWN。把"这个节点不知道"留在树里会让证明数上下不闭合（父节点既
//: 不能被证明也不能被反驳，却又没有可展开的子节点），df-pn 的阈值循环会在
//: 那里空转。
enum : uint8_t { TERM_NONE = 0, TERM_WIN = 1, TERM_NO_WIN = 2 };

struct DfpnNode {
    uint32_t pn = 1;
    uint32_t dn = 1;
    uint32_t firstChild = 0;
    uint32_t nChildren = 0;
    //: 从父节点走到这里的着法（线性格索引）；根节点为 -1。
    int16_t move = -1;
    //: WIN 时"成五那一手从根算落了几手"，与 `VcfResult::dist` 同口径。
    int16_t dist = 0;
    uint8_t side = 1;        //: 本节点轮到谁走（1 = 攻方，2 = 守方）
    uint8_t terminal = TERM_NONE;
    uint8_t expanded = 0;
    uint8_t pad = 0;
};

//: 这套方法算不完了（节点上限 / 时间到 / 取消 / 深度上限）。与 `VcfStop`
//: 同一角色：不是"整个搜索该停了"，而是"这次证明没有结论"。
struct DfpnStop {};

inline double nowSec() {
    using namespace std::chrono;
    return duration<double>(steady_clock::now().time_since_epoch()).count();
}

}  // namespace

class DfpnSearch {
public:
    DfpnSearch(const Board& root, int me, const DfpnConfig& cfg,
               const std::atomic<bool>* cancel)
        : bd_(root),
          me_(me),
          opp_(3 - me),
          cfg_(cfg),
          cancel_(cancel),
          deadline_(nowSec() + cfg.timeLimit),
          depthCap_(std::min(cfg.maxDepth, MAX_PLY - 2)) {
        pool_.resize(kInitialNodes);
    }

    ProofResult run() {
        const uint32_t ri = allocNode();
        DfpnNode& root = pool_[ri];
        root = DfpnNode();
        root.side = static_cast<uint8_t>(me_);
        bool stopped = false;
        try {
            search(ri, PROOF_INF, PROOF_INF, 0);
        } catch (const DfpnStop&) {
            // 预算用尽或取消。**树不能扔**：根节点的证明数就是到目前为止
            // 已经证出来的结论，若它已经是 0，那条杀是真的。
            stopped = true;
        }

        ProofResult out{PROOF_UNKNOWN, -1, 0, nodes_};
        const DfpnNode& r = pool_[ri];
        if (r.pn == 0) {
            out.state = PROOF_WIN;
            out.dist = r.dist;
            out.move = winningChildMove(ri);
            if (out.move < 0) {
                // 根上自己就成五（`expandAttacker` 的第一条），没有子节点。
                const Bits361 f = fivePointsFor(me_);
                if (f.any()) out.move = f.lowestIndex();
            }
            if (out.move < 0) out.state = PROOF_UNKNOWN;
            return out;
        }
        // NO_WIN 要三个条件同时成立：搜完了（没有中途停下）、没有哪条线是
        // 因为深度上限被截断的、并且走完了全部反驳。VCT 还要再加一条 ——
        // 攻方的走法集不完整（不枚举防守应手），见 dfpn.h 文件头。
        if (!stopped && !depthLimited_ && r.dn == 0 && cfg_.mode == 1) {
            out.state = PROOF_NO_WIN;
        }
        return out;
    }

private:
    static constexpr size_t kInitialNodes = 4096;

    // ------------------------------------------------------------ arena

    //: 分配一个节点。`pool_` 按需翻倍，**扩容会让所有引用失效** —— 因此任何
    //: 持着 `DfpnNode&` 的地方都不许在中间调用 `allocNode()`（本文件里每个
    //: 展开函数都是先分配、后取引用）。
    uint32_t allocNode() {
        const size_t lim = static_cast<size_t>(cfg_.nodeCap) + 1;
        if (used_ >= lim) throw DfpnStop{};
        if (used_ >= pool_.size()) {
            pool_.resize(std::min(lim, pool_.size() * 2));
        }
        return used_++;
    }

    void tick() {
        ++nodes_;
        if (nodes_ > 1 && (nodes_ & 4095) != 0) return;
        if (nodes_ > cfg_.nodeCap) throw DfpnStop{};
        if (cancel_ != nullptr && cancel_->load(std::memory_order_relaxed)) {
            throw DfpnStop{};
        }
        if (nowSec() >= deadline_) throw DfpnStop{};
    }

    // ------------------------------------------------------------ 规则面

    //: `player` 的成五点（禁手变体里黑方的长连点不算）。空点集合。
    Bits361 fivePointsFor(int player) const {
        const Bits361 raw =
            fivePoints(bd_.bitsOf(player), bd_.bitsOf(3 - player));
#if GOMOKU_BAN
        return ban::fivesFor(bd_, raw, player);
#else
        return raw;
#endif
    }

    //: `player` 这一手是否被禁手吃掉（只有黑方可能）。必须在 `make` 之后、
    //: `unmake` 之前调用 —— 与 `vcfRec` 同一个顺序。
    bool isFoulFor(int mv, bool made, int player) const {
#if GOMOKU_BAN
        return player == 1 &&
               ban::classify(bd_, mv, made) == ban::Outcome::LOSE;
#else
        (void)mv;
        (void)made;
        (void)player;
        return false;
#endif
    }

    //: 落子后，过 `mv` 的某条线达到活三（或以上）。
    bool makesLiveThree(int mv) const {
        for (int k = 0; k < 4; ++k) {
            const int lid = tables::cellLinePos[mv][k].lid;
            if (bd_.lineLv[me_ - 1][lid] >= LV_THREE_LIVE) return true;
        }
        return false;
    }

    // ------------------------------------------------------------ 终局与展开

    void setWin(uint32_t ni, int dist) {
        DfpnNode& n = pool_[ni];
        n.terminal = TERM_WIN;
        n.pn = 0;
        n.dn = PROOF_INF;
        n.dist = static_cast<int16_t>(dist);
    }

    void setNoWin(uint32_t ni) {
        DfpnNode& n = pool_[ni];
        n.terminal = TERM_NO_WIN;
        n.pn = PROOF_INF;
        n.dn = 0;
        n.dist = 0;
    }

    void expand(uint32_t ni, int depth) {
        if (depth >= depthCap_) {
            // 到了深度上限：这一条线**证明不了**，但它不等于"不存在"。按
            // NO_WIN 收尾，让搜索继续试别的线（WIN 仍然可信）；同时记下
            // "有截断"，根节点的 NO_WIN 因此不再可信（见 `run`）。
            depthLimited_ = true;
            setNoWin(ni);
            return;
        }
        if (pool_[ni].side == me_) {
            expandAttacker(ni, depth);
        } else {
            expandDefender(ni, depth);
        }
    }

    //: 分配一个子节点并把它接到 `ni` 的第 `count` 个位置。**只写 `pool_[ci]`，
    //: 不碰 `pool_[ni]`** —— 扩容可能已经让 `ni` 的引用失效。
    uint32_t newChild(int mv, int side) {
        const uint32_t ci = allocNode();
        DfpnNode& c = pool_[ci];
        c = DfpnNode();
        c.move = static_cast<int16_t>(mv);
        c.side = static_cast<uint8_t>(side);
        return ci;
    }

    void expandAttacker(uint32_t ni, int depth) {
        // 1) 我这一手就成五。
        const Bits361 myFive = fivePointsFor(me_);
        if (myFive.any()) {
            setWin(ni, depth + 1);
            return;
        }
        // 2) 对手已经能成五 —— 冲四压不住五。与 `vcfRec` 同一行判定。
        if (fivePointsFor(opp_).any()) {
            setNoWin(ni);
            return;
        }
        // 3) 候选：能做出四的空点（`hotPoints` 含五点，五点已在第 1 步处理）。
        Bits361 cand = hotPoints(bd_.bitsOf(me_), bd_.bitsOf(opp_));
        if (cfg_.mode == 2) cand = cand | liveThreePoints(cand);
        if (cand.none()) {
            // 攻方集合是完整的（冲四 + 成五）：连四都做不出来 ⇒ 没有冲四必胜。
            setNoWin(ni);
            return;
        }

        // 4) **子节点顺序 = 威胁强度**：落子后的成五点数（双四点优先）。df-pn
        //    不靠评估函数排序，所有新子节点的证明数都是 1 —— 并列时选哪一支
        //    只看创建次序。按线性格索引排会让"先深挖一条不相干的链"变成常态，
        //    实测一个三手杀要五十万节点才回得来；按威胁强度排，双四点第一支
        //    就是结论。顺序不影响正确性，只影响什么时候算出来。
        struct Cand {
            int eight;      //: 落子后的成五点数（威胁强度）
            int mv;
        };
        Cand list[CELLS];
        int n = 0;
        for (int wi = 0; wi < Bits361::WORDS; ++wi) {
            uint64_t m = cand.w[wi];
            while (m) {
                const int bit = ctz64(m);
                m &= m - 1;
                const int mv = (wi << 6) + bit;
                const bool made = bd_.make(mv, me_);
                const bool foul = isFoulFor(mv, made, me_);
                const int eight = foul ? 0 : fivePointsFor(me_).count();
                bd_.unmake(mv, me_);
                if (foul) continue;
                list[n].eight = eight;
                list[n].mv = mv;
                ++n;
            }
        }
        if (n == 0) {
            setNoWin(ni);
            return;
        }
        std::sort(list, list + n, [](const Cand& a, const Cand& b) {
            if (a.eight != b.eight) return a.eight > b.eight;
            return a.mv < b.mv;          // 同强度按索引升序，保持可复现
        });

        uint32_t first = 0;
        for (int i = 0; i < n; ++i) {
            const uint32_t ci = newChild(list[i].mv, opp_);
            if (i == 0) first = ci;
        }
        DfpnNode& node = pool_[ni];
        node.firstChild = first;
        node.nChildren = static_cast<uint32_t>(n);
        node.expanded = 1;
    }

    void expandDefender(uint32_t ni, int depth) {
        // 守方自己有成五 —— 先手在他，攻方的"杀"不存在。
        if (fivePointsFor(opp_).any()) {
            setNoWin(ni);
            return;
        }
        const Bits361 attFive = fivePointsFor(me_);
        const int nFive = attFive.count();
        if (nFive >= 2) {
            // 两个成五点，守方只能堵一个；攻方在 depth+1 层落五。
            setWin(ni, depth + 2);
            return;
        }
        uint32_t first = 0, count = 0;
        if (nFive == 1) {
            // 被冲四：应手唯一且被迫（不挡就输）。这是一个**完整的** AND 节点。
            // 与 `vcfRec` 一致地**不查**这里的禁手：黑方挡在禁手上等于无论如何
            // 都输，攻方的结论不变。
            first = newChild(attFive.lowestIndex(), me_);
            count = 1;
        } else {
            // 活三之后的守方：应手集是**全部空点**。少枚举一格，那个格子上的
            // 反冲四就可能被漏掉，WIN 就变成了假话。禁手点不是合法着法，剔除
            // 它不会漏掉任何真实应手。
            const Bits361 empty = ~bd_.occupied();
            for (int wi = 0; wi < Bits361::WORDS; ++wi) {
                uint64_t m = empty.w[wi];
                while (m) {
                    const int bit = ctz64(m);
                    m &= m - 1;
                    const int mv = (wi << 6) + bit;
                    const bool made = bd_.make(mv, opp_);
                    const bool foul = isFoulFor(mv, made, opp_);
                    bd_.unmake(mv, opp_);
                    if (foul) continue;
                    const uint32_t ci = newChild(mv, me_);
                    if (count == 0) first = ci;
                    ++count;
                }
            }
            if (count == 0) {
                // 盘面满了还不分胜负：不会发生在真实对局里，但别返回一个
                // "零子节点、未被证明"的节点。
                setNoWin(ni);
                return;
            }
        }
        DfpnNode& n = pool_[ni];
        n.firstChild = first;
        n.nChildren = count;
        n.expanded = 1;
    }

    //: mode 2 的活三点：候选区里落子后某条线达到活三。
    Bits361 liveThreePoints(const Bits361& hot) {
        Bits361 out = Bits361::zero();
        const Bits361 cand = bd_.candMask;
        for (int wi = 0; wi < Bits361::WORDS; ++wi) {
            uint64_t m = cand.w[wi];
            while (m) {
                const int bit = ctz64(m);
                m &= m - 1;
                const int mv = (wi << 6) + bit;
                if (hot.test(mv)) continue;
                const bool made = bd_.make(mv, me_);
                const bool foul = isFoulFor(mv, made, me_);
                const bool three = !foul && !made && makesLiveThree(mv);
                bd_.unmake(mv, me_);
                if (three) out.set(mv);
            }
        }
        return out;
    }

    // ------------------------------------------------------------ 证明数

    void recompute(uint32_t ni) {
        DfpnNode& n = pool_[ni];
        if (n.terminal != TERM_NONE) return;
        const bool orNode = n.side == me_;
        uint32_t pnMin = PROOF_INF, dnMin = PROOF_INF;
        uint32_t pnSum = 0, dnSum = 0;
        int bestDist = 0;
        bool provenChild = false;
        for (uint32_t i = 0; i < n.nChildren; ++i) {
            const DfpnNode& c = pool_[n.firstChild + i];
            pnMin = std::min(pnMin, c.pn);
            dnMin = std::min(dnMin, c.dn);
            pnSum = addSat(pnSum, c.pn);
            dnSum = addSat(dnSum, c.dn);
            if (c.pn != 0) continue;
            if (orNode) {
                // OR 节点由**一手**证明：取最快的那条。
                if (!provenChild || c.dist < bestDist) {
                    bestDist = c.dist;
                    provenChild = true;
                }
            } else {
                // AND 节点要全部子节点都赢；五落在最后落子的那条线上。
                bestDist = std::max(bestDist, static_cast<int>(c.dist));
                provenChild = true;
            }
        }
        if (orNode) {
            n.pn = pnMin;
            n.dn = dnSum;
        } else {
            n.pn = pnSum;
            n.dn = dnMin;
        }
        n.dist = provenChild ? static_cast<int16_t>(bestDist) : 0;
    }

    uint32_t selectMinPn(uint32_t ni) const {
        const DfpnNode& n = pool_[ni];
        uint32_t best = 0, bestPn = PROOF_INF, bestDn = PROOF_INF;
        for (uint32_t i = 0; i < n.nChildren; ++i) {
            const uint32_t ci = n.firstChild + i;
            const DfpnNode& c = pool_[ci];
            if (c.pn < bestPn || (c.pn == bestPn && c.dn < bestDn)) {
                best = ci;
                bestPn = c.pn;
                bestDn = c.dn;
            }
        }
        return best;
    }

    uint32_t selectMinDn(uint32_t ni) const {
        const DfpnNode& n = pool_[ni];
        uint32_t best = 0, bestDn = PROOF_INF, bestPn = PROOF_INF;
        for (uint32_t i = 0; i < n.nChildren; ++i) {
            const uint32_t ci = n.firstChild + i;
            const DfpnNode& c = pool_[ci];
            if (c.dn < bestDn || (c.dn == bestDn && c.pn < bestPn)) {
                best = ci;
                bestDn = c.dn;
                bestPn = c.pn;
            }
        }
        return best;
    }

    uint32_t secondMinPn(uint32_t ni, uint32_t skip) const {
        const DfpnNode& n = pool_[ni];
        uint32_t best = PROOF_INF;
        for (uint32_t i = 0; i < n.nChildren; ++i) {
            const uint32_t ci = n.firstChild + i;
            if (ci == skip) continue;
            best = std::min(best, pool_[ci].pn);
        }
        return best;
    }

    uint32_t secondMinDn(uint32_t ni, uint32_t skip) const {
        const DfpnNode& n = pool_[ni];
        uint32_t best = PROOF_INF;
        for (uint32_t i = 0; i < n.nChildren; ++i) {
            const uint32_t ci = n.firstChild + i;
            if (ci == skip) continue;
            best = std::min(best, pool_[ci].dn);
        }
        return best;
    }

    int winningChildMove(uint32_t ni) const {
        const DfpnNode& n = pool_[ni];
        int mv = -1;
        int bestDist = 0;
        bool found = false;
        for (uint32_t i = 0; i < n.nChildren; ++i) {
            const DfpnNode& c = pool_[n.firstChild + i];
            if (c.pn != 0) continue;
            if (!found || c.dist < bestDist ||
                (c.dist == bestDist && c.move < mv)) {
                mv = c.move;
                bestDist = c.dist;
                found = true;
            }
        }
        return found ? mv : -1;
    }

    // ------------------------------------------------------------ 主循环

    //: 阈值更新用 64 位中间值再饱和 —— `thdn + n.dn - c.dn` 的中间结果可以
    //: 超过 `PROOF_INF`，先钳到 `PROOF_INF` 再参与比较。
    static uint32_t thresh(uint32_t th, uint32_t whole, uint32_t part) {
        if (whole >= PROOF_INF || part >= PROOF_INF) return PROOF_INF;
        const uint64_t v = static_cast<uint64_t>(th) +
                           static_cast<uint64_t>(whole - part);
        return v >= PROOF_INF ? PROOF_INF : static_cast<uint32_t>(v);
    }

    //: "次小值 + 1"。**那个 +1 是 df-pn 能收敛的关键**：并列最小的两个子节点
    //: 用次小值当阈值时，阈值恰好等于子节点当前的 pn，递归进去一个节点都不
    //: 展开就原样返回，父节点的证明数永远不变 —— 死循环。
    static uint32_t bump(uint32_t v) {
        return v >= PROOF_INF ? PROOF_INF : v + 1;
    }

    void search(uint32_t ni, uint32_t thpn, uint32_t thdn, int depth) {
        if (pool_[ni].terminal != TERM_NONE) return;
        tick();
        if (!pool_[ni].expanded) expand(ni, depth);
        if (pool_[ni].terminal != TERM_NONE) return;
        recompute(ni);

        while (true) {
            DfpnNode& n = pool_[ni];
            // 证明/反驳成立就收工。**这一行不能只靠 `while` 条件**：证明成立时
            // `pn == 0 < thpn` 仍为真，循环会继续选一个已证明的子节点、再原样
            // 返回，永远转不完。
            if (n.pn == 0 || n.dn == 0) return;
            if (!(n.pn < thpn && n.dn < thdn)) return;

            const int mover = n.side;
            uint32_t ci;
            uint32_t cThpn;
            uint32_t cThdn;
            if (mover == me_) {
                ci = selectMinPn(ni);
                if (ci == 0) return;
                const DfpnNode& c = pool_[ci];
                // OR 节点按 pn 选子，子阈值是"次小的 pn"；dn 阈值放宽到父节点
                // 的 dn（父 dn 是各子 dn 之和）。
                cThpn = std::min(thpn, bump(secondMinPn(ni, ci)));
                cThdn = thresh(thdn, n.dn, c.dn);
            } else {
                ci = selectMinDn(ni);
                if (ci == 0) return;
                const DfpnNode& c = pool_[ci];
                cThdn = std::min(thdn, bump(secondMinDn(ni, ci)));
                cThpn = thresh(thpn, n.pn, c.pn);
            }
            // **棋盘必须随着下降走**：`bd_` 是唯一的状态，节点只存"从父节点
            // 走到这里的着法"。漏掉这一对 make/unmake，子节点看到的还是父节点
            // 的局面 —— 症状是 `expandDefender` 把所有空点当成活三应手。
            const int mv = pool_[ci].move;
            bd_.make(mv, mover);
            search(ci, cThpn, cThdn, depth + 1);
            bd_.unmake(mv, mover);
            recompute(ni);
        }
    }

    Board bd_;
    const int me_;
    const int opp_;
    const DfpnConfig cfg_;
    const std::atomic<bool>* cancel_;
    const double deadline_;
    const int depthCap_;
    std::vector<DfpnNode> pool_;
    uint32_t used_ = 0;
    int64_t nodes_ = 0;
    //: 有节点因为深度上限被截断 —— 根节点的 NO_WIN 从此不可信（WIN 不受影响）。
    bool depthLimited_ = false;
};

ProofResult dfpnSearch(const Board& root, int me, const DfpnConfig& cfg,
                       const std::atomic<bool>* cancel) {
    if ((cfg.mode != 1 && cfg.mode != 2) || cfg.timeLimit <= 0.0 ||
        cfg.nodeCap <= 0) {
        return ProofResult{PROOF_UNKNOWN, -1, 0, 0};
    }
    DfpnSearch s(root, me, cfg, cancel);
    return s.run();
}

}  // namespace gomoku

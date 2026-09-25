// 入口。三个用途：
//
//   1. `--port/--host`（默认）→ 跑 TCP 服务端，供 Python 侧的 `engine.py` 调用。
//   2. `--verify-tables`    → 只做预计算表自检后退出（挡"线号差一位"那类静默 bug）。
//   3. `--selftest`         → 表自检 + 增量正确性 + 棋型暴力比对 + 零和 +
//                             开局库 + 短杀真伪 + 极小搜索冒烟。
//
// 自检刻意**不做跨语言对拍**：对拍工具链的维护成本会超过它挡住的错误，而且
// 一旦为了"对齐"去改引擎本身就本末倒置。这里只做**进程内的结构一致性**检查
// —— 它们挡的正是那几类"不报错、只是结果慢慢偏掉"的 bug。

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <utility>
#include <vector>

#include "board.h"
#include "constants.h"
#include "evaluate.h"
#include "opening.h"
#include "search.h"
#include "tables.h"
#include "tcp_server.h"

namespace {

int g_failed = 0;

void check(bool ok, const std::string& what) {
    if (ok) {
        std::printf("  \xe2\x9c\x93 %s\n", what.c_str());
    } else {
        std::printf("  \xe2\x9c\x97 %s\n", what.c_str());
        ++g_failed;
    }
}

void section(const char* name) {
    std::printf("\n== %s ==\n", name);
}

// 确定性伪随机：自检必须**可复现**。用 std::random_device 会让"偶发失败"
// 变成一句无法追查的报告。
class Rng {
public:
    explicit Rng(uint64_t seed) : s_(seed) {}
    uint64_t next() {
        s_ ^= s_ << 13;
        s_ ^= s_ >> 7;
        s_ ^= s_ << 17;
        return s_;
    }
    int below(int n) { return static_cast<int>(next() % static_cast<uint64_t>(n)); }

private:
    uint64_t s_;
};

// ------------------------------------------------------------ 自检

void testTables() {
    section("预计算表");
    std::string err;
    check(gomoku::tables::verify(&err), err.empty() ? "结构一致性" : err);

    // 编号与内容的独立复核：**从线自身的数据**判定它的方向与长度，而不是
    // 重走一遍 `_build_lines` 的枚举公式（那是 verify() 做的，两处同一个写法
    // 就挡不住同一处笔误）。
    //
    // 判据是**极大性**：一条线的首格沿其方向的前驱必须在盘外，末格的后继也
    // 必须在盘外，且线上相邻格步长恒为一个 DIR_STEP。
    //
    // ⚠️ 不能靠 `cells[1] - cells[0]` 判方向 —— 盘上存在 **4 条长度为 1 的线**
    // （两个对角方向的 (0,18) 与 (18,0)），它们没有"第二个格"，那样判会全部
    // 落到方向 0，数出 23/19/35/35。这正是"只看前两位"的经典陷阱。
    int perDir[4] = {0, 0, 0, 0};
    int lenOne = 0;
    bool ok = true;
    std::string detail;
    for (int lid = 0; lid < gomoku::N_LINES; ++lid) {
        const auto& lv = gomoku::tables::lineViews[lid];
        const int len = lv.length;
        if (len < 1 || len > gomoku::BOARD_SIZE) {
            ok = false;
            detail = "线 " + std::to_string(lid) + " 长度非法";
            break;
        }
        // 首格沿方向 d 的前驱 / 末格沿方向 d 的后继是否都在盘外。
        const int r0 = lv.cells[0] / gomoku::BOARD_SIZE;
        const int c0 = lv.cells[0] % gomoku::BOARD_SIZE;
        const int rl = lv.cells[len - 1] / gomoku::BOARD_SIZE;
        const int cl = lv.cells[len - 1] % gomoku::BOARD_SIZE;
        auto maximalFor = [&](int k) {
            const int pr = r0 - gomoku::DIRS[k][0];
            const int pc = c0 - gomoku::DIRS[k][1];
            const int nr = rl + gomoku::DIRS[k][0];
            const int nc = cl + gomoku::DIRS[k][1];
            const bool predOff = pr < 0 || pr >= gomoku::BOARD_SIZE || pc < 0 ||
                                 pc >= gomoku::BOARD_SIZE;
            const bool succOff = nr < 0 || nr >= gomoku::BOARD_SIZE || nc < 0 ||
                                 nc >= gomoku::BOARD_SIZE;
            return predOff && succOff;
        };

        int d = -1;
        if (len >= 2) {
            // 方向取自格间步长。四个 DIR_STEP 互不相同（1/19/20/18），所以
            // 这是**无歧义**的，不需要再去猜。
            const int step = static_cast<int>(lv.cells[1]) -
                             static_cast<int>(lv.cells[0]);
            for (int k = 0; k < 4; ++k) {
                if (step == gomoku::DIR_STEPS[k]) d = k;
            }
            if (d < 0) {
                ok = false;
                detail = "线 " + std::to_string(lid) + " 首步不是四个方向之一";
                break;
            }
            for (int i = 1; i < len; ++i) {
                const int diff = static_cast<int>(lv.cells[i]) -
                                 static_cast<int>(lv.cells[i - 1]);
                if (diff != gomoku::DIR_STEPS[d]) {
                    ok = false;
                    detail = "线 " + std::to_string(lid) + " 内步长不一致";
                }
            }
            if (!ok) break;
            if (!maximalFor(d)) {
                ok = false;
                detail = "线 " + std::to_string(lid) + " 不是极大的";
                break;
            }
        } else {
            // 长度 1 的线没有"第二个格"，只能靠极大性判方向。
            //
            // 注意**不能**对长度 ≥ 2 的线也用极大性判方向：一行横贯第 0 行的
            // 横线，其两端沿对角方向同样都在盘外，极大性会给出两个候选。长度
            // ≥ 2 时步长才是可信的信号。
            ++lenOne;
            int nFound = 0;
            for (int k = 0; k < 4; ++k) {
                if (maximalFor(k)) {
                    d = k;
                    ++nFound;
                }
            }
            if (nFound != 1) {
                ok = false;
                detail = "长度 1 的线 " + std::to_string(lid) + " 方向不唯一（" +
                         std::to_string(nFound) + " 个候选）";
                break;
            }
        }
        ++perDir[d];
    }
    check(ok, ok ? std::string("每条线都极大且步长一致")
                 : ("线表不一致: " + detail));
    check(perDir[0] == 19 && perDir[1] == 19 && perDir[2] == 37 &&
              perDir[3] == 37,
          "四方向线数 19/19/37/37（实得 " + std::to_string(perDir[0]) + "/" +
              std::to_string(perDir[1]) + "/" + std::to_string(perDir[2]) + "/" +
              std::to_string(perDir[3]) + "）");
    check(lenOne == 4, "长度为 1 的线恰好 4 条（实得 " + std::to_string(lenOne) +
                           "）");
}

void testIncremental() {
    section("增量正确性");
    using namespace gomoku;
    Rng rng(0x1234567890ABCDEFULL);

    for (int trial = 0; trial < 8; ++trial) {
        uint8_t cells[CELLS] = {0};
        Board inc;
        std::vector<int> order;
        const int n = 1 + rng.below(120);
        int player = 1;
        for (int i = 0; i < n; ++i) {
            const int idx = rng.below(CELLS);
            if (cells[idx] != 0) continue;
            cells[idx] = static_cast<uint8_t>(player);
            inc.make(idx, player);
            order.push_back(idx);
            player = (player == 1) ? 2 : 1;
        }

        Board built = Board::fromArray(cells);
        const char* flds[16];
        int nf = 0;
        inc.diff(built, flds, &nf);
        check(nf == 0, "第 " + std::to_string(trial) + " 盘：逐子 make 与 fromArray " +
                           "逐字段一致" +
                           (nf ? std::string("（不一致字段: ") + flds[0] + "）" : ""));
        if (nf != 0) break;

        // 乱序 unmake 必须回到空盘 —— make/unmake 严格对称，所以不依赖撤销栈。
        for (int i = static_cast<int>(order.size()) - 1; i > 0; --i) {
            std::swap(order[static_cast<size_t>(i)],
                      order[static_cast<size_t>(rng.below(i + 1))]);
        }
        // 注意：乱序指的是"撤子顺序与落子顺序无关"，但每颗子必须用**它自己的**
        // 行棋方去撤。这里重新按 cells 反推方色。
        for (const int idx : order) {
            inc.unmake(idx, cells[idx]);
        }
        const Board empty;
        nf = 0;
        inc.diff(empty, flds, &nf);
        check(nf == 0, "第 " + std::to_string(trial) + " 盘：乱序 unmake 后回到空盘");
        if (nf != 0) break;
    }

    // 哈希**必须能把不同局面区分开**。
    //
    // 这一条看着像废话，但它挡的正是本仓库真实踩过的那个坑：Zobrist 发生器
    // 建在循环里，361 项全相同 —— 此时"增量维护的 hash 等于全量重建的 hash"
    // 这类一致性检查**依然全绿**（两边都是同一个常量），但置换表会把整个
    // 搜索变成随机数发生器。所以必须单独断言"不同局面 → 不同哈希"。
    {
        std::vector<uint64_t> hashes;
        hashes.push_back(Board().hash);
        for (int trial = 0; trial < 400; ++trial) {
            uint8_t cells[CELLS] = {0};
            Board bd;
            const int n = 1 + rng.below(60);
            for (int i = 0; i < n; ++i) {
                const int idx = rng.below(CELLS);
                if (cells[idx] != 0) continue;
                const int p = 1 + (i & 1);
                cells[idx] = static_cast<uint8_t>(p);
                bd.make(idx, p);
            }
            hashes.push_back(bd.hash);
        }
        std::sort(hashes.begin(), hashes.end());
        const size_t uniq =
            static_cast<size_t>(std::unique(hashes.begin(), hashes.end()) -
                                hashes.begin());
        check(uniq == hashes.size(),
              "400 个不同局面的哈希互不相同（实得 " + std::to_string(uniq) +
                  "/" + std::to_string(hashes.size()) + "）");

        // 落一子必须**改变**哈希：若某个 Zobrist 值为 0，那一格落子不改变
        // 哈希，于是"落了子"与"没落子"是两个共享键的局面。
        bool nonzero = true;
        for (int p = 0; p < 2; ++p) {
            for (int i = 0; i < CELLS; ++i) {
                if (tables::zobrist[p][i] == 0) nonzero = false;
            }
        }
        check(nonzero, "Zobrist 表无零值");
    }
}

void testFive() {
    section("成五判定");
    using namespace gomoku;
    // 五行皆测：横、竖、撇、捺，以及贴在盘边的一条。
    struct Case { int r, c, dr, dc; };
    const Case cases[] = {{9, 3, 0, 1}, {3, 9, 1, 0}, {4, 5, 1, 1}, {4, 14, 1, -1},
                          {0, 0, 0, 1}};
    for (const auto& cs : cases) {
        Board bd;
        for (int k = 0; k < 4; ++k) {
            bd.make((cs.r + cs.dr * k) * BOARD_SIZE + (cs.c + cs.dc * k), 1);
        }
        check(!bd.hasFive(1) && !bd.hasFive(2), "四子未成五");
        const int last =
            (cs.r + cs.dr * 4) * BOARD_SIZE + (cs.c + cs.dc * 4);
        check(bd.make(last, 1), "第五子判定为成五");
        check(bd.hasFive(1), "hasFive 与 make 返回值一致");
        check(!bd.hasFive(2), "对手不成五");
    }
    // 六连（长连）也算成五。
    Board bd;
    for (int k = 0; k < 5; ++k) {
        bd.make((9 * BOARD_SIZE) + 3 + k, 1);
    }
    check(bd.make(9 * BOARD_SIZE + 8, 1), "长连也判成五");
}

//: 棋型判定的暴力比对。
//:
//: `fivePoints` / `hotPoints` 是位移与掩码的杂技，**只看代码是看不出对错的**
//: —— 它们依赖 `shr`/`shl` 在 361 位边界上的行为，而一个把第 18 列的子和下
//: 一行的子当成同一窗口的位移错误，长得和正确版本一模一样。这里用最笨的定义
//: 逐格比对：五点 = "落子即成五"，四子连窗 = "落子后存在一个含 ≥4 颗己方子
//: 的 5 连窗口"。
void testPatterns() {
    section("棋型判定（暴力比对）");
    using namespace gomoku;
    Rng rng(0x5EED12345678ULL);

    auto windowHasN = [](const Bits361& mine, int r0, int c0, int dr, int dc,
                         int need) {
        int cnt = 0;
        for (int k = 0; k < 5; ++k) {
            const int r = r0 + dr * k, c = c0 + dc * k;
            if (mine.test(r * BOARD_SIZE + c)) ++cnt;
        }
        return cnt >= need;
    };
    //: 含 idx 的 5 连窗口中，是否有 ≥need 颗 `mine`。
    auto anyWindow = [&](const Bits361& mine, int idx, int need) {
        const int r0 = idx / BOARD_SIZE, c0 = idx % BOARD_SIZE;
        for (int d = 0; d < 4; ++d) {
            const int dr = DIRS[d][0], dc = DIRS[d][1];
            for (int off = 0; off < 5; ++off) {      // 窗口起点相对 idx 的位置
                const int r = r0 - dr * off, c = c0 - dc * off;
                const int re = r + dr * 4, ce = c + dc * 4;
                if (r < 0 || r >= BOARD_SIZE || c < 0 || c >= BOARD_SIZE)
                    continue;
                if (re < 0 || re >= BOARD_SIZE || ce < 0 || ce >= BOARD_SIZE)
                    continue;
                if (windowHasN(mine, r, c, dr, dc, need)) return true;
            }
        }
        return false;
    };

    int fiveBad = 0, hotBad = 0, makeBad = 0, fiveBad2 = 0;
    std::string exFive, exHot, exMake;
    for (int trial = 0; trial < 40; ++trial) {
        Bits361 b = Bits361::zero(), w = Bits361::zero();
        Board bd;
        const int n = rng.below(45);
        for (int i = 0; i < n; ++i) {
            const int idx = rng.below(CELLS);
            if ((b | w).test(idx)) continue;
            const int p = 1 + (i & 1);
            if (p == 1) b.set(idx); else w.set(idx);
            bd.make(idx, p);
        }
        if (hasFiveBits(b) || hasFiveBits(w)) continue;   // 已终局，跳过

        for (int p = 1; p <= 2; ++p) {
            const Bits361 mine = (p == 1) ? b : w;
            const Bits361 opp = (p == 1) ? w : b;
            const Bits361 got = fivePoints(mine, opp);
            for (int idx = 0; idx < CELLS; ++idx) {
                if ((mine | opp).test(idx)) continue;

                // 五点：落子即成五。
                Bits361 after = mine;
                after.set(idx);
                const bool wantFive = hasFiveBits(after);
                if (got.test(idx) != wantFive) {
                    ++fiveBad;
                    if (exFive.empty())
                        exFive = "第 " + std::to_string(trial) + " 盘 方" +
                                 std::to_string(p) + " 格" +
                                 std::to_string(idx) + " 五点=" +
                                 (got.test(idx) ? "真" : "假") + " 实际=" +
                                 (wantFive ? "真" : "假");
                }
                // 四子连窗：落子后存在一个含 ≥4 颗己方子的窗口。
                const bool wantHot = anyWindow(after, idx, 4);
                const Bits361 gotHot = hotPoints(mine, opp);
                if (gotHot.test(idx) != wantHot) {
                    ++hotBad;
                    if (exHot.empty())
                        exHot = "第 " + std::to_string(trial) + " 盘 方" +
                                std::to_string(p) + " 格" +
                                std::to_string(idx) + " 四窗=" +
                                (gotHot.test(idx) ? "真" : "假") + " 实际=" +
                                (wantHot ? "真" : "假");
                }
                // `make` 的返回值必须与"这一子是否参与成五"一致。
                Board probe = bd;
                const bool mk = probe.make(idx, p);
                if (mk != wantFive) {
                    ++makeBad;
                    if (exMake.empty())
                        exMake = "第 " + std::to_string(trial) + " 盘 方" +
                                 std::to_string(p) + " 格" +
                                 std::to_string(idx) + " make=" +
                                 (mk ? "真" : "假") + " 实际=" +
                                 (wantFive ? "真" : "假");
                }
                // 对侧的五点必须为空（对称性）。
                if (fivePoints(opp, mine).test(idx) && !hasFiveBits(opp)) {
                    ++fiveBad2;
                }
            }
        }
    }
    check(fiveBad == 0, fiveBad ? "五点判定: " + exFive
                                : "五点 = 落子即成五（40 盘 × 361 格全一致）");
    check(hotBad == 0, hotBad ? "四子连窗: " + exHot
                              : "四子连窗 = 落子后含 4 颗己方子的窗口");
    check(makeBad == 0, makeBad ? "make 返回值: " + exMake
                                : "Board::make 的返回值与成五判定一致");
    check(fiveBad2 == 0, "对方无五连时其五点集为空（对称性）");
}

void testEvaluate() {
    section("评估");
    using namespace gomoku;
    Rng rng(0xDEADBEEFCAFEULL);
    bool ok = true;
    for (int trial = 0; trial < 40; ++trial) {
        uint8_t cells[CELLS] = {0};
        Board bd;
        const int n = rng.below(40);
        int player = 1;
        for (int i = 0; i < n; ++i) {
            const int idx = rng.below(CELLS);
            if (cells[idx] != 0) continue;
            cells[idx] = static_cast<uint8_t>(player);
            bd.make(idx, player);
            player = (player == 1) ? 2 : 1;
        }
        const int32_t v1 = evaluate(bd, 1);
        const int32_t v2 = evaluate(bd, 2);
        if (v1 != -v2) {
            ok = false;
            std::printf("    第 %d 盘: evaluate(1)=%d evaluate(2)=%d\n", trial,
                        v1, v2);
            break;
        }
        if (v1 > STATIC_MAX || v1 < -STATIC_MAX) {
            ok = false;
            std::printf("    第 %d 盘: %d 超出 ±STATIC_MAX\n", trial, v1);
            break;
        }
    }
    // 严格零和是 negamax 健全性的前提，不是"差不多就行"。
    check(ok, "evaluate 在 40 个随机盘面上严格零和且在 ±STATIC_MAX 内");
}

void testOpening() {
    section("开局库");
    using namespace gomoku;
    uint8_t empty[CELLS] = {0};
    check(openingMove(empty, 1) == CENTER_IDX, "空盘 → 天元");

    uint8_t one[CELLS] = {0};
    one[0] = 1;                                  // 角上落一子
    const int idx = openingMove(one, 2);
    check(idx == 1, "仅一子 → 按固定偏移序取第一个空点（角上应为右邻）");

    uint8_t many[CELLS] = {0};
    many[0] = 1;
    many[1] = 2;
    check(openingMove(many, 1) == -1, "两子以上 → 交由正规搜索");
}

void testSearchSmoke() {
    section("搜索冒烟");
    using namespace gomoku;
    uint8_t cells[CELLS] = {0};
    // 黑在 (9,5)..(9,8) 连四，两端皆空；黑先手必须一手成五。
    for (int k = 0; k < 4; ++k) cells[9 * BOARD_SIZE + 5 + k] = 1;
    // 白给一点干扰，但不成大威胁。
    cells[5 * BOARD_SIZE + 5] = 2;
    cells[13 * BOARD_SIZE + 13] = 2;

    SearchConfig cfg;
    cfg.timeLimit = 3.0;
    cfg.maxDepth = 8;
    cfg.qply = 4;

    Engine engine;
    Info info;
    const int mv = engine.think(cells, 1, cfg, nullptr, &info);
    const int r = mv / BOARD_SIZE, c = mv % BOARD_SIZE;
    check(mv == 9 * BOARD_SIZE + 4 || mv == 9 * BOARD_SIZE + 9,
          "连四局面下一手补成五（实得 r=" + std::to_string(r) +
              " c=" + std::to_string(c) + "）");
    check(info.depth >= 1 && info.nodes > 0, "诊断字段已填充");
    std::printf("    depth=%d actual=%d val=%d nodes=%lld time=%.1fms reason=%s\n",
                info.depth, info.actualDepth, info.bestVal,
                static_cast<long long>(info.nodes), info.timeMs, info.reason);

    // 对手有活四时，引擎必须识别出已无解（分值落在杀棋带）。
    uint8_t lost[CELLS] = {0};
    for (int k = 0; k < 4; ++k) lost[9 * BOARD_SIZE + 4 + k] = 2;   // 白活四
    lost[0] = 1;
    Info lInfo;
    Engine e2;
    const int lmv = e2.think(lost, 1, cfg, nullptr, &lInfo);
    check(lmv >= 0, "败局下仍返回一个合法着法");
    check(isMate(lInfo.bestVal), "对手活四被识别为杀棋分（val=" +
                                     std::to_string(lInfo.bestVal) + "）");

    // 取消位：置位后必须**很快**返回，而不是把 3 秒跑满。
    uint8_t mid[CELLS] = {0};
    mid[CENTER_IDX] = 1;
    mid[CENTER_IDX + 1] = 2;
    std::atomic<bool> cancelFlag{true};     // 一进搜索就该中止
    SearchConfig cfg2 = cfg;
    cfg2.timeLimit = 10.0;
    Info cInfo;
    Engine e3;
    const auto t0 = std::chrono::steady_clock::now();
    e3.think(mid, 1, cfg2, &cancelFlag, &cInfo);
    const double ms = std::chrono::duration<double, std::milli>(
                          std::chrono::steady_clock::now() - t0)
                          .count();
    check(ms < 1500.0, "取消位置位时迅速返回（" + std::to_string(ms) + " ms）");
}

//: 杀棋分是真的吗？—— 一个**已被两条独立途径证实**的短杀局面。
//:
//: 这个局面（14 子，黑先）曾经被当成"评估函数溢出/必胜幻觉"报上来：浅搜索
//: 秒回 `val=9999998`（= WIN_SCORE - 2，"2 层内成五"），看起来像编的分数。
//: 它其实是**真杀**：(6,10)-(7,11)-(9,13) 的斜线上补 (8,12) 成为四连，两端
//: (5,9) 与 (10,14) **同时**成五点，对手只能挡一端。
//:
//: 独立验证（都不经过本引擎的搜索、评估、置换表）：
//:   * 朴素网格暴力枚举（`/tmp/verify_win.py` 那一版）：唯一杀着 (8,12)，
//:     且穷举对手全部应手后确认无一能同时挡住两个五点；
//:   * 参考实现 `engine_local.Engine.vcf`：`VCF_WIN, move=(8,12), dist=3`。
//:
//: 因此这条用例是**强度回归**而不是正确性回归：它同时钉住"引擎找得到这个
//: 三手杀"和"杀棋分的数值编码 = WIN_SCORE - k"。
void testMateTruth() {
    section("短杀与杀棋分编码");
    using namespace gomoku;
    uint8_t cells[CELLS] = {0};
    const int stones[][3] = {{6, 10, 1}, {7, 6, 2}, {7, 8, 1},  {7, 11, 1},
                             {8, 11, 1}, {9, 9, 1},  {9, 13, 1}, {10, 6, 2},
                             {10, 9, 2}, {10, 10, 2}, {11, 8, 2}, {12, 11, 2},
                             {12, 13, 1}, {13, 10, 2}};
    for (const auto& s : stones) cells[s[0] * BOARD_SIZE + s[1]] = s[2];

    // 双方都没有五点 —— 所以"秒回杀棋分"才显得可疑，也才值得钉住。
    const Board bd = Board::fromArray(cells);
    check(fivePoints(bd.bitsOf(1), bd.bitsOf(2)).none() &&
              fivePoints(bd.bitsOf(2), bd.bitsOf(1)).none(),
          "出题：双方此刻都没有即成五点（杀在两层之后）");

    SearchConfig cfg;
    cfg.timeLimit = 5.0;
    cfg.maxDepth = 4;
    cfg.qply = 8;
    cfg.vcfBudget = 0.0;          // 关掉 VCF 快通道：结论必须由搜索独立得出

    Engine engine;
    Info info;
    const int mv = engine.think(cells, 1, cfg, nullptr, &info);
    const int r = mv / BOARD_SIZE, c = mv % BOARD_SIZE;
    check(mv == 8 * BOARD_SIZE + 12,
          "找到三手杀 (8,12)（实得 r=" + std::to_string(r) +
              " c=" + std::to_string(c) + "）");
    check(isMate(info.bestVal),
          "报出杀棋分（val=" + std::to_string(info.bestVal) + "）");
    check(info.bestVal == WIN_SCORE - 2,
          "杀棋分编码正确：WIN_SCORE-2 表示第 2 层成五（实得 " +
              std::to_string(info.bestVal) + "）");
    check(info.scoreType != nullptr && std::string(info.scoreType) == "mate",
          "score_type 标为 mate");
    std::printf("    val=%d dep=%d nodes=%lld time=%.1fms score_type=%s\n",
                info.bestVal, info.depth, static_cast<long long>(info.nodes),
                info.timeMs, info.scoreType);

    // 同一个引擎**不清表**再搜一次：结论必须一致。这正是"跨请求保留置换表"
    // 会不会让分值失控"的探针 —— 曾经被误会成 bug 的地方（第二次搜索反而给出
    // 更深的杀），本质是前一次搜索留在表里的更深结果被复用了。
    Info info2;
    const int mv2 = engine.think(cells, 1, cfg, nullptr, &info2);
    check(mv2 == mv && isMate(info2.bestVal),
          "同一引擎二次搜索结论一致（第二次 val=" +
              std::to_string(info2.bestVal) + "）");
}

//: **增强搜索会不会把杀棋算丢？** —— LMR 的全部风险就在这里。
//:
//: LMR 的健全性依赖"被削减的那些着法本来就不重要"，而五子棋的杀棋恰恰可能
//: 藏在一条看起来安静的线里。这个测试拿同一批局面把**增强开启后**的结论与
//: 上面 `testMateTruth` 的结论对照：
//:
//:  * 杀棋**必须照旧找到**，且着法相同。分值**只断 `isMate`，不断
//:    `WIN_SCORE - k` 那个精确的 ply 编码** —— 延伸会改变某些线路的层数，
//:    精确编码是 `enhanced=0` 那条路径的契约，不是这套机制的。
//:  * 必败局面**必须照旧认出**。这一条比上面那条更容易被削减破掉：败局意味着
//:    搜索要在被削减的分支里"认输"，而认输的分值不会像杀棋那样主动浮上来。
//:
//: 另外顺带钉住三件"开了增强也不许变"的事：返回合法着法、时间不超、取消仍
//: 生效 —— LMR 与延伸都不该动到调度层。
void testEnhancedSearch() {
    section("增强搜索（LMR + 强制着法延伸）");
    using namespace gomoku;

    // 与 `testMateTruth` 同一个局面：真杀 (8,12)，杀在两层之后。
    uint8_t cells[CELLS] = {0};
    const int stones[][3] = {{6, 10, 1}, {7, 6, 2}, {7, 8, 1},  {7, 11, 1},
                             {8, 11, 1}, {9, 9, 1},  {9, 13, 1}, {10, 6, 2},
                             {10, 9, 2}, {10, 10, 2}, {11, 8, 2}, {12, 11, 2},
                             {12, 13, 1}, {13, 10, 2}};
    for (const auto& s : stones) cells[s[0] * BOARD_SIZE + s[1]] = s[2];

    SearchConfig cfg;
    cfg.timeLimit = 5.0;
    cfg.maxDepth = 4;
    cfg.qply = 8;
    cfg.vcfBudget = 0.0;          // 关掉 VCF 快通道：结论必须由搜索独立得出
    cfg.enhanced = 1;
    cfg.lmr = 1;
    cfg.extend = 1;

    Engine engine;
    Info info;
    const int mv = engine.think(cells, 1, cfg, nullptr, &info);
    const int r = mv / BOARD_SIZE, c = mv % BOARD_SIZE;
    check(mv == 8 * BOARD_SIZE + 12,
          "增强开启后仍找到三手杀 (8,12)（实得 r=" + std::to_string(r) +
              " c=" + std::to_string(c) + "）");
    check(isMate(info.bestVal),
          "增强开启后仍报出杀棋分（val=" + std::to_string(info.bestVal) + "）");
    std::printf("    val=%d dep=%d nodes=%lld time=%.1fms\n", info.bestVal,
                info.depth, static_cast<long long>(info.nodes), info.timeMs);

    // 必败局面：对手活四。增强搜索必须照旧把它读成杀棋带（负号）。
    uint8_t lost[CELLS] = {0};
    for (int k = 0; k < 4; ++k) lost[9 * BOARD_SIZE + 4 + k] = 2;
    lost[0] = 1;
    Info lInfo;
    Engine e2;
    const int lmv = e2.think(lost, 1, cfg, nullptr, &lInfo);
    check(lmv >= 0, "增强开启后败局仍返回一个合法着法");
    check(isMate(lInfo.bestVal) && lInfo.bestVal < 0,
          "增强开启后对手活四仍被读成必败（val=" +
              std::to_string(lInfo.bestVal) + "）");

    // 普通中局：只要求给出合法着法、不超时、诊断字段成对。
    uint8_t mid[CELLS] = {0};
    mid[CENTER_IDX] = 1;
    mid[CENTER_IDX + 1] = 2;
    mid[CENTER_IDX + BOARD_SIZE] = 1;
    mid[CENTER_IDX + BOARD_SIZE + 1] = 2;
    mid[CENTER_IDX - 1] = 1;
    SearchConfig midCfg = cfg;
    midCfg.timeLimit = 2.0;
    midCfg.maxDepth = 24;
    Info mInfo;
    Engine e3;
    const int mmv = e3.think(mid, 1, midCfg, nullptr, &mInfo);
    check(mmv >= 0 && mid[mmv] == 0, "增强开启后中局返回一个空点着法");
    check(mInfo.timeMs <= midCfg.timeLimit * 1000.0,
          "增强开启后仍守住时间上限（" + std::to_string(mInfo.timeMs) +
              " ms / " + std::to_string(midCfg.timeLimit * 1000.0) + " ms）");
    check(mInfo.depth > 0 && mInfo.nodes > 0, "增强开启后诊断字段已填充");

    // **置换表真的被采纳了吗。** 双路桶的查表曾经只认 way 1：`lookup` 先用
    // 旧几何取默认地址、增强分支再去覆盖，于是"只有 way 0 命中"那条路径不会
    // 覆盖它，命中被静默扔掉（见 `search.cpp` 的 `lookup`）。后果不体现在
    // 深度上 —— 但 `ttHitRate`（**只统计被采纳的条目**）会从几个百分点掉到
    // 0.0%，实测就是这样：`tools/bench.py` 的 TT命中 列 0.0%~0.2%。
    //
    // 门设 0.5% 是按实测留的余量：修好之后这个中局约 2%。注意它不能设成
    // `> 0`：偶发于 way 1 的命中会让坏版本也勉强过关，那这个护栏就等于没有。
    check(mInfo.ttHitRate > 0.005,
          "增强开启后置换表仍在被采纳（ttHitRate=" +
              std::to_string(mInfo.ttHitRate * 100.0) + "%，修好双路桶查表前是 0.0%）");
    std::printf("    depth=%d val=%d nodes=%lld time=%.1fms tt=%.1f%%\n",
                mInfo.depth, mInfo.bestVal, static_cast<long long>(mInfo.nodes),
                mInfo.timeMs, mInfo.ttHitRate * 100.0);

    // 取消仍必须生效（LMR/延伸不许影响调度层）。
    std::atomic<bool> cancelFlag{true};
    SearchConfig ccfg = cfg;
    ccfg.timeLimit = 10.0;
    ccfg.maxDepth = 24;
    Info cInfo;
    Engine e4;
    const auto ct0 = std::chrono::steady_clock::now();
    e4.think(mid, 1, ccfg, &cancelFlag, &cInfo);
    const double cms = std::chrono::duration<double, std::milli>(
                           std::chrono::steady_clock::now() - ct0)
                           .count();
    check(cms < 1500.0,
          "增强开启后取消仍迅速返回（" + std::to_string(cms) + " ms）");
}

}  // namespace

int main(int argc, char** argv) {
    gomoku::ServerOptions opt;
    bool verifyTables = false;
    bool selftest = false;

    for (int i = 1; i < argc; ++i) {
        const std::string a = argv[i];
        auto nextArg = [&](const char* name) -> const char* {
            if (i + 1 >= argc) {
                std::fprintf(stderr, "%s 需要一个参数\n", name);
                std::exit(2);
            }
            return argv[++i];
        };
        if (a == "--port") {
            opt.port = std::atoi(nextArg("--port"));
        } else if (a == "--host") {
            opt.host = nextArg("--host");
        } else if (a == "--verify-tables") {
            verifyTables = true;
        } else if (a == "--selftest") {
            selftest = true;
        } else if (a == "--version") {
            std::printf("gomoku_engine 1.0.0\n");
            return 0;
        } else if (a == "-h" || a == "--help") {
            std::printf(
                "用法: gomoku_engine [选项]\n"
                "  --host <addr>      监听地址（默认 127.0.0.1，只监听回环）\n"
                "  --port <n>         监听端口（默认 8888）\n"
                "  --verify-tables    只做预计算表自检后退出\n"
                "  --selftest         表自检 + 增量/零和/开局/搜索 冒烟\n"
                "  --version          打印版本\n"
                "  -h, --help         显示本帮助\n");
            return 0;
        } else {
            std::fprintf(stderr, "未知参数: %s（试 --help）\n", a.c_str());
            return 2;
        }
    }

    gomoku::tables::init();

    if (verifyTables || selftest) {
        testTables();
        if (selftest) {
            testIncremental();
            testFive();
            testPatterns();
            testEvaluate();
            testOpening();
            testMateTruth();
            testSearchSmoke();
            testEnhancedSearch();
        }
        std::printf("\n%s（%d 项失败）\n", g_failed == 0 ? "自检通过" : "自检失败",
                    g_failed);
        return g_failed == 0 ? 0 : 1;
    }

    return gomoku::runServer(opt);
}

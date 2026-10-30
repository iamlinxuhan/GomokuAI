// 给浏览器用的 C 接口：把 search / board / ban 这三块暴露成几个扁平函数。
//
// 与 `tcp_server.cpp` 的关系是**平行**而不是"复用"：那边面对的是 JSON 行协议
// 与线程，这边没有线程、没有 socket，一次调用就是一次完整的搜索。两者共用
// 同一份 `Engine`、同一份 `SearchConfig`，所以"网页版的棋力"与桌面版是同一个
// 二进制内核 —— 这一点比省下几十行适配代码重要得多。
//
// 几个刻意的取舍：
//
//  1. **不做取消。** 浏览器侧的取消是"把 Worker 干掉"，那是唯一一个能打断
//     同步阻塞调用的手段；在这里塞一个原子标志只会让人以为它有用。
//  2. **返回值是一段 JSON 字符串。** 指针指向进程内的静态缓冲，下一次调用即
//     失效 —— JS 侧必须当场读走。用结构体回传能少一次格式化，但要额外维护
//     一份字段顺序约定，而这份约定在桌面版里已经由 `Info` 与 Python 的 `info`
//     字典对好了。
//  3. `gm_bancheck` 只在 `GOMOKU_BAN=1` 编译出来的那份里有内容；无禁手那份
//     走 `ban.h` 的空实现，恒返回空数组 —— 与桌面版的 `gomoku_engine` 同构。

#include <cstdint>
#include <cstdio>
#include <string>
#include <utility>
#include <vector>

#include "ban.h"
#include "board.h"
#include "constants.h"
#include "search.h"
#include "tables.h"

#ifdef __EMSCRIPTEN__
#include <emscripten/emscripten.h>
#define GM_EXPORT EMSCRIPTEN_KEEPALIVE
#else
#define GM_EXPORT
#endif

namespace {

gomoku::Engine& engine() {
    static gomoku::Engine instance;
    return instance;
}

//: 一块够大的静态缓冲。最坏情况是候选表满（361 条）+ 全部诊断字段，
//: 实测不到 8 KB。
char g_buf[16384];

}  // namespace

extern "C" {

//: 建表。幂等，`tables::init` 是唯一必须在搜索前跑的东西 —— 少了它
//: `lineMask` / `touchMask` 全零，搜索会给出"看起来很合理"的错棋。
GM_EXPORT int gm_init() {
    gomoku::tables::init();
    return 1;
}

//: 新开一局。只清置换表与历史表，不动表本身。
GM_EXPORT void gm_new_game() { engine().reset(); }

//: 一步棋。参数与 `SearchConfig` 一一对应，`0` 表示"用默认值"。
//:
//: `collect != 0` 时额外回传根节点候选表（战后复盘用，见 `analyze`）。
GM_EXPORT const char* gm_think(const uint8_t* cells, int me, double timeLimit,
                               int maxDepth, double vcfBudget, int qply,
                               int enhanced, double biasAttack,
                               double biasDefence, double biasTolerance,
                               double reserveSec, int collect) {
    gomoku::SearchConfig cfg;
    cfg.timeLimit = timeLimit;
    cfg.maxDepth = maxDepth;
    cfg.vcfBudget = vcfBudget;
    cfg.qply = qply;
    cfg.reserveSec = reserveSec;
    if (enhanced != 0) {
        // 与 `engine.py` 的 `_search_request` 同一套：增强是一个总开关，
        // LMR 与延伸各自跟随它。分开量 A/B 时才需要拆成两个参数。
        cfg.enhanced = 1;
        cfg.lmr = 1;
        cfg.extend = 1;
    }
    cfg.biasAttack = biasAttack;
    cfg.biasDefence = biasDefence;
    cfg.biasTolerance = biasTolerance;

    gomoku::Info info;
    std::vector<std::pair<int, int32_t>> roots;
    const int idx = engine().think(cells, me, cfg, nullptr, &info,
                                   collect != 0 ? &roots : nullptr);
    if (idx < 0) {
        std::snprintf(g_buf, sizeof g_buf, "{\"move\":-1}");
        return g_buf;
    }

    int used = std::snprintf(
        g_buf, sizeof g_buf,
        "{\"move\":%d,\"depth\":%d,\"actual\":%d,\"val\":%d,"
        "\"ms\":%.1f,\"nodes\":%lld,\"nps\":%lld,\"scoreType\":\"%s\"",
        idx, info.depth, info.actualDepth, static_cast<int>(info.bestVal),
        info.timeMs, static_cast<long long>(info.nodes),
        static_cast<long long>(info.nps), info.scoreType);
    if (used > 0 && collect != 0 && used < static_cast<int>(sizeof g_buf) - 32) {
        used += std::snprintf(g_buf + used, sizeof g_buf - used, ",\"root\":[");
        for (size_t i = 0; i < roots.size(); ++i) {
            const int room = static_cast<int>(sizeof g_buf) - used;
            if (room < 32) break;
            used += std::snprintf(g_buf + used, room, "%s[%d,%d]",
                                  i == 0 ? "" : ",", roots[i].first,
                                  static_cast<int>(roots[i].second));
        }
        used += std::snprintf(g_buf + used, sizeof g_buf - used, "]");
    }
    std::snprintf(g_buf + used, sizeof g_buf - used, "}");
    return g_buf;
}

//: 禁手点。只有黑方会被约束；白方或空盘恒返回空数组。
GM_EXPORT const char* gm_bancheck(const uint8_t* cells, int player) {
    std::vector<int> points;
    if (player == 1) {
        const gomoku::Board bd = gomoku::Board::fromArray(cells);
        points = gomoku::ban::forbiddenPoints(bd);
    }
    int used = std::snprintf(g_buf, sizeof g_buf, "{\"points\":[");
    for (size_t i = 0; i < points.size(); ++i) {
        const int room = static_cast<int>(sizeof g_buf) - used;
        if (room < 32) break;
        used += std::snprintf(g_buf + used, room, "%s[%d,%d]", i == 0 ? "" : ",",
                              points[i] / gomoku::BOARD_SIZE,
                              points[i] % gomoku::BOARD_SIZE);
    }
    std::snprintf(g_buf + used, sizeof g_buf - used, "]}");
    return g_buf;
}

}  // extern "C"

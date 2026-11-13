// Gomocup 2026 官方开局集（Python 镜像见仓库根的 gomocup_book.py）。
//
// 只在**根节点**查一次，绝不进搜索树；`analyze` / 复盘路径不查。
// 未命中返回 -1，调用方照常搜索 —— 报文与搜索路径逐字节不变。
//
// 数据来源：data/gomocup_2026.json（Gomocup 2026 官方开局集，
// https://live.gomocup.org/openings），由 tools/gen_gomocup_data.py 生成
// gomocup_book_data.h。**不要手改生成物。**

#pragma once

#include <cstdint>
#include <cstddef>

namespace gomoku {

//: 查表。`key` 是 ``hash ^ (走子方 == 黑 ? 0 : TT_SALT_W)``。
//: 命中返回线性格索引；未命中返回 -1。
int openingBookLookup(uint64_t key);

//: 当前尺寸上展开后的条目数（D4 之后的键数，审计用）。
std::size_t openingBookSize();

//: 当前尺寸上保留下来的原始开局线条数（越界丢弃之后）。
int openingBookLines();

//: 把展开后的全部条目按 ``key 升序`` 打到 stdout（``--dump-book``）。
//: 供 ``tests/test_gomocup_book.py`` 与 Python 表逐项对拍；生产路径不调用。
void openingBookDump();

}  // namespace gomoku

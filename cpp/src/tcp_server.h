// 本地 TCP 服务端（JSON-lines 协议）。
//
// **这是唯一允许出现平台头文件的文件。** `winsock2.h` / `sys/socket.h` 的差异
// 全部收敛在这里，核心引擎（board / evaluate / search / vcf）保持纯标准 C++17。
//
// 线程模型：**一个读线程 + 一个工作线程**。
//
//   * 读线程持续解析 JSON 行。`cancel` **不进队列，直接置 std::atomic<bool>**
//     —— 这是关键：搜索正在工作线程里跑，如果 cancel 也排进同一个队列，它要
//     等搜索结束才会被看到，取消就成了摆设。
//   * 工作线程只处理 `compute`（以及顺序敏感的 `reset`），每 1024 个节点读一次
//     那个原子量。
//   * 两个线程都可能往同一个 socket 写（工作线程写搜索结果，读线程写 pong），
//     所以写操作统一走一把写锁。
//
// **优雅停机**：收到 `shutdown` 后置标志并**取消当前搜索**，等 worker 自然
// 结束再关连接 —— 不中途杀线程。中途杀会让响应写一半，Python 侧读到坏 JSON。

#pragma once

#include <string>

namespace gomoku {

struct ServerOptions {
    std::string host = "127.0.0.1";     // 默认只监听回环
    int port = 8888;
};

//: 跑服务端。**接受多个连接**（逐个服务）—— Python 侧断开重连时不必重启进程。
//: 返回进程退出码。
int runServer(const ServerOptions& opt);

}  // namespace gomoku

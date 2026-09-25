#include "tcp_server.h"

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <cstring>
#include <deque>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "board.h"
#include "constants.h"
#include "json_util.h"
#include "opening.h"
#include "search.h"
#include "tables.h"

#ifdef _WIN32
#include <winsock2.h>
#include <ws2tcpip.h>
using socket_t = SOCKET;
static const socket_t kBadSocket = INVALID_SOCKET;
#else
#include <arpa/inet.h>
#include <csignal>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>
using socket_t = int;
static const socket_t kBadSocket = -1;
#endif

namespace gomoku {
namespace {

constexpr const char* kVersion = "1.0.0";

void closeSocket(socket_t s) {
#ifdef _WIN32
    ::closesocket(s);
#else
    ::close(s);
#endif
}

void shutdownSocket(socket_t s) {
#ifdef _WIN32
    ::shutdown(s, SD_BOTH);
#else
    ::shutdown(s, SHUT_RDWR);
#endif
}

bool sendAll(socket_t fd, const char* data, size_t len) {
    size_t off = 0;
    while (off < len) {
        int flags = 0;
#ifdef MSG_NOSIGNAL
        flags = MSG_NOSIGNAL;      // 对端已关时返回 EPIPE 而不是发 SIGPIPE
#endif
        const auto n = ::send(fd, data + off, static_cast<int>(len - off), flags);
        if (n <= 0) return false;
        off += static_cast<size_t>(n);
    }
    return true;
}

struct Server;

//: 一次连接的全部共享状态。读线程与工作线程都持有它（通过 shared_ptr），
//: 两个线程都退出后才析构 —— 所以这里不需要引用计数以外的任何生命周期管理。
struct Shared {
    //: **`fd` 的一切访问都必须在 `writeMu` 之下。** 它不只是"写锁"：fd 会被
    //: `close`，而 close 之后那个**编号会被系统回收给别的线程**，此时任何
    //: 残留的 `send`/`shutdown` 都会打到别人的 socket 上。这类 bug 极难复现，
    //: 所以规矩定死：读也锁、写也锁、关也锁。
    socket_t fd = kBadSocket;
    Server* srv = nullptr;
    std::mutex writeMu;
    std::atomic<bool> cancel{false};
    std::atomic<bool> stop{false};         // shutdown、被新连接顶掉、或对端断开
    std::mutex mu;
    std::condition_variable cv;
    std::deque<json::Value> jobs;
    bool readerDone = false;
};

//: 进程级状态。**`Engine` 只有一份**：置换表、历史表、杀手着法都是跨着法
//: 累积的状态，两份 Engine 等于把迭代加深的收益丢掉一半，而且内存翻倍
//: （TT 是 1M 条 × 16 字节）。
struct Server {
    Engine engine;
    //: 串行化 `think` / `reset`。多个连接**可以**同时存在（见 `runServer` 的
    //: "最后连接的客户端胜出"），所以必须有一把锁把 Engine 圈起来。
    std::mutex engineMu;
    socket_t listenFd = kBadSocket;
    std::atomic<bool> quit{false};
    std::mutex connMu;
    std::shared_ptr<Shared> current;
    std::atomic<int> active{0};
    std::mutex doneMu;
    std::condition_variable doneCv;
};

void sendLine(Shared& sh, const std::string& line) {
    std::lock_guard<std::mutex> lk(sh.writeMu);
    if (sh.fd == kBadSocket) return;
    sendAll(sh.fd, line.data(), line.size());
    sendAll(sh.fd, "\n", 1);
}

//: 叫醒卡在 `recv` 里的读线程，但**不关 fd**（真正的 `close` 由连接自己的
//: 线程做）。用 `shutdown` 而不是 `close`：`close` 一个别的线程正在使用的 fd
//: 是未定义行为，而且不会唤醒阻塞中的 `recv`。
void interruptConn(Shared& sh) {
    std::lock_guard<std::mutex> lk(sh.writeMu);
    if (sh.fd == kBadSocket) return;
    shutdownSocket(sh.fd);
}

//: 关闭并作废 fd。作废这一步和 `close` 同等重要 —— 少了它，第二次调用就会
//: 关掉一个可能已被系统回收给别人的编号。
void closeConn(Shared& sh) {
    std::lock_guard<std::mutex> lk(sh.writeMu);
    if (sh.fd == kBadSocket) return;
    shutdownSocket(sh.fd);
    closeSocket(sh.fd);
    sh.fd = kBadSocket;
}

void writeError(Shared& sh, const json::Value& req, const std::string& msg) {
    std::string buf;
    json::ObjWriter w(&buf);
    w.str("type", "error");
    const json::Value* id = req.find("id");
    if (id != nullptr && id->isNum()) w.i64("id", id->intOr(-1));
    w.str("error", msg);
    w.close();
    sendLine(sh, buf);
}

//: `{"type":"ok"}` —— 给那些**没有结果、但需要回执**的请求（`reset` /
//: `shutdown`）。
//:
//: 回执不是可有可无的礼节：客户端若把"每个请求恰好一个响应"当作不变式来写
//: 读循环（这是唯一能让读循环保持简单的写法），那么一个不回应的 `reset` 就会
//: 让客户端把**下一条** compute 的结果当成 reset 的回执，从此整体错位一格。
//: 而且 `new_game()` 需要知道置换表真的清干净了，才敢发下一步。
void writeOk(Shared& sh, const json::Value& req) {
    std::string buf;
    json::ObjWriter w(&buf);
    w.str("type", "ok");
    const json::Value* id = req.find("id");
    if (id != nullptr && id->isNum()) w.i64("id", id->intOr(-1));
    w.close();
    sendLine(sh, buf);
}

// ------------------------------------------------------------ 报文解析

bool parseBoard(const json::Value& v, uint8_t* cells, std::string* err) {
    if (!v.isArr() || static_cast<int>(v.arr.size()) != BOARD_SIZE) {
        *err = "board 必须是 " + std::to_string(BOARD_SIZE) + " 行的二维数组";
        return false;
    }
    for (int r = 0; r < BOARD_SIZE; ++r) {
        const json::Value& row = v.arr[static_cast<size_t>(r)];
        if (!row.isArr() ||
            static_cast<int>(row.arr.size()) != BOARD_SIZE) {
            *err = "board 第 " + std::to_string(r) + " 行不是 " +
                   std::to_string(BOARD_SIZE) + " 列";
            return false;
        }
        for (int c = 0; c < BOARD_SIZE; ++c) {
            const json::Value& cell = row.arr[static_cast<size_t>(c)];
            if (!cell.isNum()) {
                *err = "board 里有非数字格";
                return false;
            }
            const int64_t p = cell.intOr(-1);
            if (p < 0 || p > 2) {
                *err = "board 里出现了 0/1/2 之外的取值";
                return false;
            }
            cells[r * BOARD_SIZE + c] = static_cast<uint8_t>(p);
        }
    }
    return true;
}

double clampD(double v, double lo, double hi) {
    return v < lo ? lo : (v > hi ? hi : v);
}
int64_t clampI(int64_t v, int64_t lo, int64_t hi) {
    return v < lo ? lo : (v > hi ? hi : v);
}

//: 从请求里读搜索参数。**权威在 Python** —— C++ 侧不写死难度表，所以这里的
//: 每一项都有默认值，但正常情况下全部由报文给出。
SearchConfig readConfig(const json::Value& req) {
    SearchConfig cfg;
    auto num = [&](const char* k, double dflt) {
        const json::Value* v = req.find(k);
        return v != nullptr ? v->numOr(dflt) : dflt;
    };
    cfg.timeLimit = clampD(num("time_limit", 3.0), 0.01, 3600.0);
    cfg.maxDepth =
        static_cast<int>(clampI(static_cast<int64_t>(num("max_depth", 4)), 1,
                                MAX_PLY - 1));
    cfg.vcfBudget = clampD(num("vcf_budget", 0.0), 0.0, 3600.0);
    cfg.qply = static_cast<int>(
        clampI(static_cast<int64_t>(num("qply", 4)), 0, MAX_PLY));
    cfg.vcfNodeCap = clampI(static_cast<int64_t>(num("vcf_node_cap",
                                                     double(VCF_NODE_CAP))),
                            1000, int64_t(1) << 40);
    cfg.biasAttack = clampD(num("bias_attack", 0.0), -10.0, 10.0);
    cfg.biasDefence = clampD(num("bias_defence", 0.0), -10.0, 10.0);
    cfg.biasTolerance = clampD(num("bias_tolerance", 0.0), 0.0, 1e9);
    // 增强搜索。**默认 0**：没显式要求就走旧路径，于是初级/中级与 Python
    // 参考实现 `engine_local` 的逐字一致继续成立（见 search.h 里那段说明）。
    cfg.enhanced = static_cast<int>(
        clampI(static_cast<int64_t>(num("enhanced", 0)), 0, 1));
    cfg.lmr =
        static_cast<int>(clampI(static_cast<int64_t>(num("lmr", 0)), 0, 1));
    cfg.extend =
        static_cast<int>(clampI(static_cast<int64_t>(num("extend", 0)), 0, 1));
    return cfg;
}

// ------------------------------------------------------------ 响应

void writeMove(Shared& sh, const json::Value& req, int idx, const Info& info) {
    const int r = idx / BOARD_SIZE;
    const int c = idx % BOARD_SIZE;
    std::string buf;
    json::ObjWriter w(&buf);
    w.str("type", "move");
    const json::Value* id = req.find("id");
    if (id != nullptr && id->isNum()) w.i64("id", id->intOr(-1));
    // 「x」是行号、「y」是列号，与 `divmod(idx, BOARD_SIZE)` 的顺序一致。
    w.i32("x", r);
    w.i32("y", c);
    w.i32("idx", idx);
    w.i32("score", info.bestVal);
    w.i32("depth", info.depth);
    w.i32("actual_depth", info.actualDepth);
    w.i64("nodes", info.nodes);
    w.i64("nps", info.nps);
    w.dbl("time_ms", info.timeMs, 3);
    w.dbl("tt_hit_rate", info.ttHitRate, 6);
    w.dbl("qnode_ratio", info.qnodeRatio, 6);
    w.str("reason", info.reason);
    w.str("score_type", info.scoreType);
    // `vcf_state` 的非激活态必须真的输出 `null` —— 输出 0 会被读成
    // VCF_NO_WIN，那是一个**真实的结论**，与"这一档根本没开 VCF"不是一回事。
    if (info.vcfActive) {
        w.i32("vcf_state", info.vcfState);
    } else {
        w.null("vcf_state");
    }
    w.i32("vcf_dist", info.vcfDist);
    w.i64("vcf_nodes", info.vcfNodes);
    w.close();
    sendLine(sh, buf);
}

// ------------------------------------------------------------ 任务处理

void handleJob(Shared& sh, const json::Value& req) {
    Server& srv = *sh.srv;
    const json::Value* tv = req.find("type");
    const std::string type = tv != nullptr ? tv->strOr("") : "";

    if (type == "reset") {
        // `new_game()` 走这里。**必须经过队列**（而不是在读线程里直接调）：
        // 直接 reset 会和正在计算的 worker 抢同一个 Engine 的置换表与历史表。
        // 经过队列就与 compute 天然有序：TCP 是可靠有序的，队列是 FIFO 的，
        // 所以"先 reset 再 compute"在两边的观感完全一致。
        std::lock_guard<std::mutex> lk(srv.engineMu);
        srv.engine.reset();
        writeOk(sh, req);
        return;
    }
    if (type == "opening") {
        // 开局库的镜像。**权威实现在 Python 侧**，这里只是让协议自洽。
        std::vector<uint8_t> cells(CELLS, 0);
        std::string err;
        const json::Value* bv = req.find("board");
        if (bv == nullptr || !parseBoard(*bv, cells.data(), &err)) {
            writeError(sh, req, err.empty() ? "缺少 board" : err);
            return;
        }
        const int idx = openingMove(cells.data(), 1);
        std::string buf;
        json::ObjWriter w(&buf);
        w.str("type", "opening");
        if (idx >= 0) {
            w.i32("x", idx / BOARD_SIZE);
            w.i32("y", idx % BOARD_SIZE);
        } else {
            w.null("x");
            w.null("y");
        }
        w.close();
        sendLine(sh, buf);
        return;
    }
    if (type != "compute") {
        writeError(sh, req, "未知的 type: " + type);
        return;
    }

    std::vector<uint8_t> cells(CELLS, 0);
    std::string err;
    const json::Value* bv = req.find("board");
    if (bv == nullptr || !parseBoard(*bv, cells.data(), &err)) {
        writeError(sh, req, err.empty() ? "缺少 board" : err);
        return;
    }
    const json::Value* pv = req.find("player");
    const int64_t player = pv != nullptr ? pv->intOr(0) : 0;
    if (player != 1 && player != 2) {
        writeError(sh, req, "player 必须是 1 或 2");
        return;
    }

    const SearchConfig cfg = readConfig(req);
    Info info;
    int idx = -1;
    {
        // Engine 的跨着法状态由这把锁保护。等锁期间也可能被取消/顶掉，所以
        // 拿到锁之后再判一次 —— 否则一个已经被顶掉的连接会在这里把整盘搜完。
        std::lock_guard<std::mutex> lk(srv.engineMu);
        if (sh.stop.load(std::memory_order_relaxed)) return;
        sh.cancel.store(false);       // 清掉上一轮可能留下的取消位
        idx = srv.engine.think(cells.data(), static_cast<int>(player), cfg,
                               &sh.cancel, &info);
    }
    if (sh.stop.load(std::memory_order_relaxed)) return;   // 收件人已经走了
    if (idx < 0) {
        writeError(sh, req, "没有合法着法");
        return;
    }
    writeMove(sh, req, idx, info);
}

// ------------------------------------------------------------ 线程

void readerLoop(Shared& sh) {
    // fd 只读一次：读线程是 fd 生命周期的**所有者**（`closeConn` 在
    // `reader.join()` 之后才跑），所以这里不必逐次加锁。别的线程能对它做的
    // 只有 `shutdown`，那不会让 fd 编号失效。
    const socket_t fd = sh.fd;
    std::string buf;
    std::vector<char> tmp(8192);
    bool shutdown = false;
    while (!sh.stop.load(std::memory_order_relaxed)) {
        const auto n = ::recv(fd, tmp.data(), static_cast<int>(tmp.size()), 0);
        if (n <= 0) break;                 // 对端断开
        buf.append(tmp.data(), static_cast<size_t>(n));

        size_t pos = 0;
        while (!shutdown && (pos = buf.find('\n')) != std::string::npos) {
            std::string line = buf.substr(0, pos);
            buf.erase(0, pos + 1);
            if (!line.empty() && line.back() == '\r') line.pop_back();
            if (line.empty()) continue;
            if (line.size() > 4u * 1024 * 1024) {
                std::string out;
                json::ObjWriter w(&out);
                w.str("type", "error");
                w.str("error", "报文过长");
                w.close();
                sendLine(sh, out);
                continue;
            }

            json::Value v;
            std::string err;
            if (!json::parse(line, &v, &err)) {
                std::string out;
                json::ObjWriter w(&out);
                w.str("type", "error");
                w.str("error", "JSON 解析失败: " + err);
                w.close();
                sendLine(sh, out);
                continue;
            }

            const json::Value* tv = v.find("type");
            const std::string type = tv != nullptr ? tv->strOr("") : "";
            if (type == "cancel") {
                // **不进队列，就地置原子标志。** 搜索正在工作线程里跑；如果
                // cancel 也排进同一个队列，它要等搜索结束才会被看到 —— 那正是
                // 旧引擎"接受 cancel 参数但从不读取"的等价物。
                sh.cancel.store(true, std::memory_order_relaxed);
                continue;
            }
            if (type == "ping") {
                std::string out;
                json::ObjWriter w(&out);
                w.str("type", "pong");
                w.str("version", kVersion);
                w.close();
                sendLine(sh, out);
                continue;
            }
            if (type == "shutdown") {
                // 优雅停机：置标志 + 取消当前搜索，**不中途杀线程**。让 worker
                // 自然收尾，否则响应会写一半，对端读到坏 JSON。
                writeOk(sh, v);
                sh.cancel.store(true, std::memory_order_relaxed);
                sh.stop.store(true, std::memory_order_relaxed);
                shutdown = true;
                break;
            }
            if (type == "hello") {
                std::string out;
                json::ObjWriter w(&out);
                w.str("type", "hello");
                w.str("version", kVersion);
                w.i32("board_size", BOARD_SIZE);
                w.i32("n_lines", N_LINES);
                w.i32("win_score", WIN_SCORE);
                w.i32("static_max", STATIC_MAX);
                w.i32("vcf_node_cap", static_cast<int>(VCF_NODE_CAP));
                w.close();
                sendLine(sh, out);
                continue;
            }

            // compute / reset / 未知类型：排队，由工作线程按序处理。
            {
                std::lock_guard<std::mutex> lk(sh.mu);
                sh.jobs.push_back(std::move(v));
            }
            sh.cv.notify_one();
        }
        if (shutdown) break;
    }

    {
        std::lock_guard<std::mutex> lk(sh.mu);
        sh.readerDone = true;
    }
    sh.cv.notify_one();
}

void workerLoop(Shared& sh) {
    while (true) {
        json::Value job;
        {
            std::unique_lock<std::mutex> lk(sh.mu);
            sh.cv.wait(lk, [&] {
                return !sh.jobs.empty() || sh.readerDone || sh.stop.load();
            });
            if (sh.jobs.empty()) {
                if (sh.readerDone || sh.stop.load()) return;
                continue;
            }
            job = std::move(sh.jobs.front());
            sh.jobs.pop_front();
        }
        handleJob(sh, job);
    }
}

//: 服务一个客户端。返回 true 表示收到了 `shutdown`（进程该退出了）。
bool serveClient(const std::shared_ptr<Shared>& conn) {
    Shared& sh = *conn;
    std::thread reader(readerLoop, std::ref(sh));
    std::thread worker(workerLoop, std::ref(sh));

    reader.join();

    // 读线程退出（对端断开、被顶掉、或收到 shutdown）之后：
    //   * 取消当前搜索 —— 否则对端断开后工作线程还会把整局搜完；
    //   * 丢掉还没开始的排队任务 —— 它们已经没有收件人了。
    sh.cancel.store(true, std::memory_order_relaxed);
    {
        std::lock_guard<std::mutex> lk(sh.mu);
        sh.jobs.clear();
        sh.readerDone = true;
    }
    sh.cv.notify_all();
    worker.join();

    closeConn(sh);
    return sh.stop.load();
}

//: 让当前连接立刻收摊。见 `runServer` 里"最后连接的客户端胜出"那段。
void evictCurrent(Server& srv) {
    std::shared_ptr<Shared> old;
    {
        std::lock_guard<std::mutex> lk(srv.connMu);
        old = srv.current;
        srv.current = nullptr;
    }
    if (!old) return;
    old->cancel.store(true, std::memory_order_relaxed);
    old->stop.store(true, std::memory_order_relaxed);
    interruptConn(*old);
}

}  // namespace

int runServer(const ServerOptions& opt) {
#ifdef _WIN32
    WSADATA wsa;
    if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0) {
        std::fprintf(stderr, "WSAStartup 失败\n");
        return 1;
    }
#else
    // 对端关闭后继续写会发 SIGPIPE，默认动作是杀进程。服务端必须忽略它 ——
    // 我们已经用返回值判断写失败了。
    std::signal(SIGPIPE, SIG_IGN);
#endif

    tables::init();
    std::string err;
    if (!tables::verify(&err)) {
        std::fprintf(stderr, "表自检失败: %s\n", err.c_str());
        return 2;
    }

    const socket_t lfd = ::socket(AF_INET, SOCK_STREAM, 0);
    if (lfd == kBadSocket) {
        std::fprintf(stderr, "创建监听套接字失败\n");
        return 1;
    }
    int yes = 1;
    ::setsockopt(lfd, SOL_SOCKET, SO_REUSEADDR,
                 reinterpret_cast<const char*>(&yes), sizeof(yes));

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(static_cast<uint16_t>(opt.port));
    if (::inet_pton(AF_INET, opt.host.c_str(), &addr.sin_addr) != 1) {
        std::fprintf(stderr, "非法的监听地址: %s\n", opt.host.c_str());
        closeSocket(lfd);
        return 1;
    }
    if (::bind(lfd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0) {
        std::fprintf(stderr, "绑定 %s:%d 失败（端口被占？）\n", opt.host.c_str(),
                     opt.port);
        closeSocket(lfd);
        return 1;
    }
    if (::listen(lfd, 8) != 0) {
        std::fprintf(stderr, "listen 失败\n");
        closeSocket(lfd);
        return 1;
    }

    // 这一行是 Python 侧判断"服务端已就绪"的依据之一（另一个是轮询端口）。
    std::printf("{\"ready\":true,\"host\":\"%s\",\"port\":%d,\"version\":\"%s\"}\n",
                opt.host.c_str(), opt.port, kVersion);
    std::fflush(stdout);

    Server srv;
    srv.listenFd = lfd;

    int consecFail = 0;
    while (!srv.quit.load()) {
        const socket_t cfd = ::accept(lfd, nullptr, nullptr);
        if (cfd == kBadSocket) {
            if (srv.quit.load()) break;
            // 单个连接失败不该让服务端退出；但持续失败（例如监听套接字被
            // 外部关掉）就必须退，否则这里会变成忙等。
            if (++consecFail > 100) {
                std::fprintf(stderr, "accept 连续失败，退出\n");
                break;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
            continue;
        }
        consecFail = 0;

        // **最后连接的客户端胜出。** 本地单机场景下唯一合理的策略：桌面应用
        // 开两个窗口 = 两个 Python 进程 = 两条连接，而 Engine 只有一份。
        // 若改成"先到先服务"，第二个窗口的每一步都会**无声地永远等待**
        // —— 最坏的一种失败：没有报错、没有超时、界面就那样卡着。
        // 顶掉旧连接后，旧的那个 Python 会在 readline 上读到 EOF，进而降级到
        // 本地 Python 引擎继续下棋（见 engine.py 的降级路径），仍是可用状态。
        evictCurrent(srv);

        auto conn = std::make_shared<Shared>();
        conn->fd = cfd;
        conn->srv = &srv;
        {
            std::lock_guard<std::mutex> lk(srv.connMu);
            srv.current = conn;
        }
        srv.active.fetch_add(1);

        std::thread([&srv, conn] {
            const bool wantShutdown = serveClient(conn);
            if (wantShutdown) {
                srv.quit.store(true);
                // 唤醒卡在 `accept` 里的主循环。对监听套接字 `shutdown` 在各
                // 平台上都会让 `accept` 立刻失败返回。
                shutdownSocket(srv.listenFd);
            }
            {
                std::lock_guard<std::mutex> lk(srv.connMu);
                if (srv.current == conn) srv.current = nullptr;
            }
            if (srv.active.fetch_sub(1) == 1) {
                std::lock_guard<std::mutex> lk(srv.doneMu);
                srv.doneCv.notify_all();
            }
        }).detach();
    }

    // 收尾：evict 会把连接线程赶走，但要等它们真的把 Engine 放开才能析构。
    {
        std::unique_lock<std::mutex> lk(srv.doneMu);
        srv.doneCv.wait(lk, [&] { return srv.active.load() == 0; });
    }

    closeSocket(lfd);
#ifdef _WIN32
    WSACleanup();
#endif
    return 0;
}

}  // namespace gomoku

# -*- coding: utf-8 -*-
"""引擎门面：``ai_move`` 走 C++ 服务端，其余一切留在本地。

## 这个文件现在是什么

重构前 ``engine.py`` 是 1936 行的完整引擎（位棋盘 / 全增量评估 / Negamax+PVS
/ 置换表 / 静止搜索 / VCF）。那些算法**一字未改**地搬到了 ``engine_local.py``，
本模块只剩"服务端连接 + 玩家抽象 + 降级"三件事。这样做的理由有两条：

1. **算法要能被独立测试。** ``tests/`` 与 ``tools/`` 里 300 多个用例直接 import
   ``engine_local`` 的私有实现（``_ZOBRIST`` / ``_LINE_MASKS`` / ``Board.diff``…）。
   把算法留在原处、只改导入路径，这些用例逻辑零改动即可继续守护重构不回归。
2. **降级必须是免费的。** C++ 服务端没编译、崩溃、或端口被占时，``ai_move``
   直接落到 ``engine_local.ai_move``。用户永远能下棋，只是慢一点。

## 为什么只有 ``ai_move`` 走 TCP

``main.py`` 从本模块取 9 个名字，但它们的**执行线程完全不同**：

* ``ai_move`` —— 在 ``AIWorker(QThread)`` 子线程里跑，是唯一的重活，也是唯一
  值得用 C++ 换算力的地方。
* ``check_win`` / ``win_line`` / ``opening_move`` / ``evaluate`` —— **全部在 UI
  主线程同步调用**（每落一子都要跑一遍）。把它们挪到 TCP 上等于让界面每手同步
  阻塞一次网络往返，与"UI 零卡顿"直接冲突。

另有一处硬约束：``main.py:1177`` 写的是 ``evaluate(Board.from_array(board), player)``
—— ``evaluate`` 的首参是 ``Board`` **实例**而非 ndarray，而 ``Board`` 有 12 个
``__slots__`` 字段的深度实现。保留本地 ``Board`` 是唯一能让那行不改的办法。

## 玩家抽象（为 PvP 预留）

``ai_move`` 不再直接调引擎，而是委托给一个 ``Player``。本轮有两个实现：
``RemoteAIPlayer``（TCP 请求 C++ 服务端）与 ``LocalAIPlayer``（本地兜底）。
将来的"玩家 vs 玩家"只需新增 ``HumanPlayer`` / 远程人类玩家，UI 一行不用改。

## 失败只降级、不报错（一条硬规则）

远程路径上的**任何**问题 —— 找不到可执行文件、bind 失败、``hello`` 对不上、
报文不合法、超时、对端关闭 —— 一律被翻译成"这次用本地引擎"，然后继续。
理由：这是一层纯粹的性能优化，它没有资格让一局正在下的棋中断。唯一**例外**
的是 ``evaluate`` 的刻度校验（见 ``_handshake``）：接错程序时宁可不连，因为
分值刻度错了会静默污染右栏那两张图，比慢一点糟得多。
"""

from __future__ import annotations

import atexit
import json
import os
import socket
import subprocess
import sys
import threading
import time

import config
import engine_local as _local

# ---------------------------------------------------------------------------
# 1. 再导出：算法之外的公开名字全部来自本地实现
# ---------------------------------------------------------------------------
#
# 这些名字被两个"不许改"的消费方按名字取用，少一个就 ImportError：
#
# * ``main.py:32``     —— BOARD_SIZE, Board, ai_move, check_win, evaluate,
#                         is_mate, new_game, opening_move, win_line
# * ``analysis.py:48`` —— BONUS_*, LINE_SCORES, LV_*, STATIC_MAX, is_mate
#
# ``ai_move`` 与 ``new_game`` 不在下面这段里 —— 它们是本模块重写的两个入口。

from engine_local import (  # noqa: E402
    BOARD_SIZE,
    BONUS_DOUBLE_FOUR,
    BONUS_DOUBLE_THREE,
    BONUS_FOUR_THREE,
    LINE_SCORES,
    LV_FOUR,
    LV_FOUR_LIVE,
    LV_ONE,
    LV_THREE_LIVE,
    LV_THREE_SLEEP,
    LV_TWO_LIVE,
    LV_TWO_SLEEP,
    STATIC_MAX,
    Board,
    check_win,
    evaluate,
    is_mate,
    opening_move,
    win_line,
)

__all__ = [
    "BOARD_SIZE", "Board", "ai_move", "check_win", "evaluate", "is_mate",
    "new_game", "opening_move", "win_line",
    "BONUS_DOUBLE_FOUR", "BONUS_DOUBLE_THREE", "BONUS_FOUR_THREE",
    "LINE_SCORES", "LV_FOUR", "LV_FOUR_LIVE", "LV_ONE", "LV_THREE_LIVE",
    "LV_THREE_SLEEP", "LV_TWO_LIVE", "LV_TWO_SLEEP", "STATIC_MAX",
    "engine_label", "binary_path", "DIFFICULTY", "DIFFICULTY_NAMES",
    "difficulty_name",
]


# ---------------------------------------------------------------------------
# 2. 五档难度
# ---------------------------------------------------------------------------
#
# 重构前是三档（3/7/15 秒，深度上限 4/10/24）。C++ 把算力提了一个量级之后，
# 那三档会在同样的深度上**趋同** —— 用户看到的是"中级和高级走出一样的棋，但
# 高级要多等 8 秒"。所以这里刻意用「时间 + 深度上限 + VCF 预算」三者共同把
# 梯度压开，并扩到五档。
#
# **权威在 Python**：每次请求把这组参数随报文下发，C++ 侧不写死难度表。好处是
# 校准参数不必重编译，而且"难度"这个概念留在 UI 层 —— 与"PvP 时 AI 只是其中
# 一个玩家"的抽象一致。
#
# ⚠️ ``time`` / ``max_depth`` / ``vcf_budget`` / ``qply`` 是**外推值**（按实测
# 有效分支因子 b≈20、C++ 约 40× 加速折算），不是终值。编译后必须用
# ``tools/bench.py`` / ``tools/positions.py`` / ``tools/selfplay.py`` 实测回填。

DIFFICULTY = {
    # 新加的档，位置在原来三档之下。**偏置最强的一档**：它的容差带是 0，于是
    # 只在"分值完全相同"的着法之间重排，一分不牺牲。
    1: dict(name="入门", time=0.5, max_depth=2, vcf_budget=0.0, qply=2,
            enhance=False,
            bias=dict(attack=1.5, defence=0.5, tolerance=0.0)),
    # ---- 以下三档是**旧三档的原值平移**，一个数字都没动 ----
    #
    # 这里曾经改成过 `2: time=1.5, vcf_budget=0.0, qply=2`，是错的，而且错得
    # 不止"变弱"：`vcf_budget=0.0` **把初级档的 VCF 关了**，正是 README 里
    # 记着的那条已修复错误 —— "关掉之后初级既算不出自己的冲四链，也看不见
    # 对手的，那不是难度低，是失明"。用户一眼就看出来了（"初级和中级智能性
    # 有所降低"）。
    #
    # 教训：新增档位时可以另起一组参数，但**已有档位的数字只能平移或抬高，
    # 不能顺手重写**。重写时任何一项归零都会顺手删掉一个子系统，而删子系统
    # 从来不是"调低难度"该有的方式。
    #
    # `enhance=False` 不是"低档用不起增强"，而是**刻意留出的护栏**：不开增强时
    # C++ 的搜索路径与 Python 参考实现 `engine_local` 逐位同源，于是
    # `tools/positions.py --engine cpp --level 2` 与 `--engine local --level 1`
    # 必须给出**逐字相同**的着法与分值（实测：K10/−2410、G10/−2810、
    # H13/−9999995、M7/9999994…）。那条一致性是这套 C++ 移植正确性的唯一廉价
    # 检验，而增强一旦漏进低档它就没了 —— 用户要求的"不许降智"也就失去了
    # 机械保障。**不要把这两档的 enhance 打开。**
    #
    # ---- 进攻偏置（bias）：**试过、量过、撤了** ----
    #
    # 用户报"中级进攻欲望不强、一直很被动"。机械来路是清楚的：旧
    # `evaluate_board` 的末行是 `return ai_score - human_score * 0.85`，注释写着
    # 「减少AI对自身局面的过度悲观 / 配合进攻激励增强，让AI在均势时不再偏向纯
    # 防守」。Phase 3 为了 negamax 的零和性删掉了它（它让评估与"谁在评估"有关），
    # 于是新引擎在**风格**上比旧引擎更偏防守。所以试过把它以"根节点容差带"的
    # 形式迁回来（见 `engine_local._apply_bias` 与 `search.cpp` 的 `applyBias`）。
    #
    # **实测结论：这条迁不回来，它是纯粹的棋力倒退。** 同档自我对打（偏置开 vs
    # 偏置关、交替执黑、同一份开局集，台账见 `tools/PLAN_ENGINE.md` 的 B24）：
    #
    #   初级 20 局  带宽 3000 → 30%    带宽 0（纯同分重排）→ 30%    对称权重 → 35%
    #   初级  8 局  带宽 3000 → 25%
    #   中级  8 局  带宽 3000 → **0%（0—8）**
    #
    # 连"只在分值**完全相同**的一手之间重排"都输 6—14，这排除了"带宽给多了"
    # 这个解释。真因是**这个判据与搜索的判断相反**：任何成四的一手都把 `own`
    # 抬到约 100000，于是它压倒性地赢得重排；而在本档的深度（4—6）下"造一个
    # 对手随手应住的冲四"并不等于进展 —— 搜索看得见，粗粒度的棋型分看不见。
    # 旧引擎能用那个 0.85，是因为它的搜索是**围绕它建的**；在一个已经正确的
    # 零和搜索上再叠一层重排，只会把正确的结果改坏。
    #
    # 与 B23 同一条教训：**"更进取"和"更弱"必须先分开量了再谈**。用户的要求是
    # "不是降智"，所以这两档保持 `bias=None` —— 也就是保持搜索的原判。
    #
    # 入门档的 `bias` 是另一回事：它在本轮之前就存在，且是那一档**刻意的性格**
    # （"贪吃系数"，深度 2 的贪心），本轮不动它。它唯一的真实缺陷是 B23。
    2: dict(name="初级", time=3.0, max_depth=4, vcf_budget=0.3, qply=4,
            enhance=False, bias=None),
    3: dict(name="中级", time=7.0, max_depth=10, vcf_budget=0.5, qply=8,
            enhance=False, bias=None),
    # ---- 以下两档开启增强搜索 ----
    #
    # 增强 = LMR（只削减安静着法）+ 强制着法延伸 + 双路桶置换表 + 时间管理。
    # 时限**一分没涨**：15 s 与 20 s 是原值，所以层数的提升只可能来自算法。
    # 这是用户那句"要真的提升算法强度，不是把时限堆高"的直接落实。
    4: dict(name="高级", time=15.0, max_depth=24, vcf_budget=1.5, qply=10,
            enhance=True, bias=None),
    # 宗师：全火力。**它的棋力要靠算法改进挣，不靠把时间堆到 20 秒** ——
    # 实测过堆时间这条路：预算 6.5s→14.5s 只多 1 层，15s→20s 在宽局面上
    # 0 层，B4 上古与宗师给出逐字相同的着法与分值。当前算法在约 6 层饱和，
    # 所以这一档的真正上限由搜索能到多深决定 —— 兑现它的就是这一档的
    # `enhance=True`（见 `cpp/src/search.cpp` 的 LMR 段与延伸段）。
    5: dict(name="宗师", time=20.0, max_depth=24, vcf_budget=3.0, qply=12,
            enhance=True, bias=None),
}

#: 档位 → 中文名。UI 的难度卡与面板读数都用它，避免"3 级"这种脱离语境的编号。
DIFFICULTY_NAMES = {k: v["name"] for k, v in DIFFICULTY.items()}

#: 本地兜底引擎的最强档（``engine_local.DIFFICULTY`` 保持三档不变 ——
#: ``tests/test_vcf.py`` 会在运行期改 ``DIFFICULTY[3]['vcf_budget']`` 并期望生效，
#: 那条测试指向本地引擎，扩表会把它改成另一件事）。
#:
#: 降级时 4/5 两档就落在这里：``engine_local._search`` 用
#: ``DIFFICULTY.get(level) or DIFFICULTY[max(DIFFICULTY)]``，超出本表就取最强
#: 那档 —— **降级应当降成"慢但强"，不是"少搜几层"**。下面这条断言保证
#: "最强档"确实等于 ``_LOCAL_MAX_LEVEL``：表里若有空洞（比如只有 1、3 两档），
#: `max()` 仍会给出 3 而 `get(2)` 会落空 —— 那正是这条断言要挡的形态。
_LOCAL_MAX_LEVEL = max(_local.DIFFICULTY)
assert set(_local.DIFFICULTY) == set(range(1, _LOCAL_MAX_LEVEL + 1)), \
    "engine_local.DIFFICULTY 的档位编号必须连续：%r" % (sorted(_local.DIFFICULTY),)

#: 只用于注释里说明"余量由谁扣"。**不要再拿它去乘下发的参数。**
#:
#: 这里踩过一次坑，记下来：以为"本地引擎的硬截止是 ``limit * (1 - RESERVE)``，
#: 所以下发前先把余量扣掉，两边墙钟就一致了"——错。**扣余量是引擎自己的事**：
#: C++ 侧 ``search.cpp`` 的 ``deadline_ = t0 + cfg.timeLimit * (1.0 - RESERVE)``
#: 与 ``engine_local.py:1716`` 逐字同义。在下发前再乘一次，余量就被扣了两遍，
#: 每档实际只拿到名义时间的 ``0.85² ≈ 72%``。
#:
#: 症状很好认但要先想到：高级档名义 9 s、实测墙钟稳定在 6544 ms（= 9 × 0.727），
#: 宗师档名义 20 s、实测 14492 ms（= 20 × 0.725）。**两个不同的档位扣出同一个
#: 系数**，说明多出来的那一次折算与档位无关 —— 它在下发路径上，不在引擎里。
#:
#: 同理 **VCF 预算不扣余量**：两侧都是 ``vcf_phase_deadline = t0 + vcf_budget``
#: 原样使用（``search.cpp:622`` / ``engine_local.py:1746``），它本身就是一段
#: 独立的、远小于主预算的时间。
_RESERVE = _local.RESERVE  # 仅作文档锚点，见上


def binary_path():
    """C++ 可执行文件的路径；这台机器上找不到则返回 ``None``。

    给日志与手工排查用。它答的是"**能不能**走 C++"，不是"这次实际用了谁" ——
    后者要等第一次搜索之后才有答案（见 ``engine_label``）。两者刻意分开：在
    开局那一刻，"这次用谁"这个问题在物理上还没有答案。
    """
    return config.find_binary()


def difficulty_name(level: int) -> str:
    """档位 → 名称；非法档位回退到最高档的名字（与引擎的取档语义一致）。"""
    return DIFFICULTY_NAMES.get(level, DIFFICULTY_NAMES[max(DIFFICULTY)])


def _cfg_for(level: int) -> dict:
    """取档位参数。非法档位回退到最高档 —— 与 ``engine_local`` 的取档语义一致。

    ``main.py`` 的档位来自难度卡，正常范围内；这条回退是给"存档里存着旧版本
    的档位号"这类情况兜底，**不抛异常**：抛出去会让 AIWorker 报 AI异常，
    用户看到的是"不能下棋"，而正确答案是"下一档最强的"。
    """
    return DIFFICULTY.get(level, DIFFICULTY[max(DIFFICULTY)])


# ---------------------------------------------------------------------------
# 3. 引擎状态指示
# ---------------------------------------------------------------------------

#: UI 面板上那一行显示什么。由玩家层在每次**搜索**之后更新 —— 它记录的是
#: "上一次搜索实际用的是谁"，而不是"谁能用"。写它的是 AI 子线程，读它的是
#: UI 线程：一个字符串的赋值在 CPython 里是原子的，不需要锁。
#:
#: 初值是 ``None`` 而不是 ``"local"``：**"还没搜过"与"降级了"是两件事。**
#: 一局棋在第一次搜索发生之前，这个问题在物理上没有答案，此时报"Python
#: (本地)"会让用户以为已经降级 —— 而实际上 C++ 完全正常。
_ENGINE_ACTIVE = None

_LABELS = {"cpp": "C++", "local": "Python (本地)"}

#: 还没发生过搜索时的占位符。留着这个状态而不是猜一个答案。
_UNKNOWN = "—"


def engine_label() -> str:
    """当前引擎的显示名。UI 用它渲染面板上的"引擎"一行。

    **不做探测。** 这里不主动去连服务端 —— 那会在 UI 主线程上引入一次可能
    超时的网络调用，而这一行只是给用户一个提示。真实状态由 ``ai_move`` 在
    后台线程里更新，UI 下一帧刷新时自然读到新值。
    """
    return _LABELS.get(_ENGINE_ACTIVE, _UNKNOWN)


def _note(active: str, exc: BaseException = None) -> None:
    """切换指示器；失败时在 stderr 上留一行**限流**的原因。

    限流不是洁癖：降级之后每一步都会失败一次，一局 40 手就是 40 行 —— 而
    它们说的是同一件事。只报第一次，之后静音，用户想看细节时看第一次就够。
    """
    global _ENGINE_ACTIVE
    if active != _ENGINE_ACTIVE:
        if active == "local" and exc is not None:
            _warn_once(f"[引擎] 降级到本地 Python 引擎：{type(exc).__name__}: {exc}")
        _ENGINE_ACTIVE = active


_warned = False


def _warn_once(msg: str) -> None:
    global _warned
    if _warned:
        return
    _warned = True
    print(msg, file=sys.stderr)


# ---------------------------------------------------------------------------
# 4. 服务端连接
# ---------------------------------------------------------------------------


class _RemoteError(RuntimeError):
    """"这次远程请求用不了"的统一出口。

    调用方只需要知道"退回去用本地引擎"，不需要区分是连接被拒、报文不合法
    还是超时 —— 三者的处置完全相同，为它们各写一条 ``except`` 只会让降级
    路径出现三种略有差异的行为。
    """


class _ServerClient:
    """到 C++ 服务端的一条长连接。

    **线程模型**：``ai_move`` 在 ``AIWorker`` 子线程里跑；在此之外还会碰它的
    有取消 watcher（**只写** ``cancel``）与 ``atexit``（写 ``shutdown``）。
    所以规矩是：

    * 所有写都在 ``_write_mu`` 之下**逐行**发出，行与行不会交错；
    * **读永远只有一个线程**（发起请求的那条）。watcher 不读 —— 并发读会让
      两条请求互相偷对方的响应，而这里的协议是严格的"一请求一响应"配对。
    * 连接与重建在 ``_conn_mu`` 之下串行，避免两步棋同时拉起两个服务端。

    连接在**每次请求内自愈**：``_request`` 失败一次就丢掉连接、重连、再试
    一次，仍失败才向上抛。这一步是必要的：服务端可能被上一次运行的残留进程
    占着、也可能被另一个窗口顶掉（C++ 侧"最后连接的客户端胜出"）。
    """

    def __init__(self) -> None:
        self._sock = None
        self._proc = None
        self._desc = ""            # 已连上的服务端版本信息，供日志/排查
        self._needs_reset = False
        self._write_mu = threading.Lock()
        self._conn_mu = threading.Lock()
        self._read_buf = b""

    # ---- 生命周期 ----

    def mark_reset_needed(self) -> None:
        """请求下一次搜索前先清空服务端的跨局面状态。**不发送、不阻塞。**"""
        self._needs_reset = True

    def shutdown(self) -> None:
        """尽力让服务端退出。``atexit`` 调的，**绝不抛异常**。

        先 ``shutdown`` 报文（服务端会置标志、等自己收尾、再正常退出），
        超时未退就 ``terminate``。为什么不直接杀：服务端在写响应写一半时被
        杀，对端（可能是另一个还活着的窗口）会读到坏 JSON。
        """
        proc = self._proc
        sock = self._sock
        self._sock = None
        self._proc = None
        if sock is not None:
            try:
                self._send_on(sock, {"type": "shutdown"})
            except Exception:
                pass
            try:
                sock.close()
            except Exception:
                pass
        if proc is None:
            return
        try:
            proc.wait(timeout=config.SHUTDOWN_TIMEOUT)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    # ---- 连接 ----

    def _connect(self):
        """建立连接并完成 ``hello`` 校验。**调用方必须持有 ``_conn_mu``。**

        返回已连接的 socket。任何一步失败都抛 ``_RemoteError``。
        """
        # 先试着连现成的监听者：可能是上一次运行留下的服务端，也可能是
        # 另一个窗口拉起来的。**能连上就不要再拉一个** —— 两个进程抢同一个
        # 端口，第二个会在 bind 上失败。
        sock = None
        try:
            sock = socket.create_connection((config.HOST, config.PORT),
                                            config.CONNECT_TIMEOUT)
        except OSError:
            sock = None

        if sock is None:
            if not config.AUTOSTART:
                raise _RemoteError("没有服务端在监听，且已禁用自动拉起")
            sock = self._spawn_and_wait()

        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            self._handshake(sock)
        except Exception:
            try:
                sock.close()
            except Exception:
                pass
            raise
        self._sock = sock
        self._read_buf = b""
        return sock

    def _spawn_and_wait(self):
        """拉起服务端并等它 bind 成功。"""
        path = config.find_binary()
        if path is None:
            raise _RemoteError("找不到 %s（先跑 cmake --build cpp/build）"
                               % config.binary_name())
        try:
            proc = subprocess.Popen(
                [path, "--host", config.HOST, "--port", str(config.PORT)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                # 独立进程组：退出时只杀我们自己拉起的这一个，不波及父进程。
                close_fds=True,
            )
        except OSError as exc:
            raise _RemoteError("拉起 %s 失败: %s" % (path, exc))

        # 就绪判据用**轮询端口**而不是读 stdout 的第一行：读管道会阻塞，
        # 而"能被连上"正好等于"已经 bind 完成"，服务端也是在 bind 之后、
        # accept 之前才打印 ready 的。轮询顺带覆盖了"进程启动即退出"这一
        # 情形 —— 那条路径上我们能从 stderr 拿到真实原因。
        deadline = time.monotonic() + config.START_TIMEOUT
        while True:
            if proc.poll() is not None:
                raise _RemoteError("引擎启动即退出（code=%s）%s"
                                   % (proc.returncode, _drain_stderr(proc)))
            try:
                sock = socket.create_connection((config.HOST, config.PORT),
                                                config.CONNECT_TIMEOUT)
            except OSError:
                if time.monotonic() >= deadline:
                    _kill(proc)
                    raise _RemoteError("引擎启动超时（%.1fs）"
                                       % config.START_TIMEOUT)
                time.sleep(0.02)
                continue
            self._proc = proc
            return sock

    def _handshake(self, sock) -> None:
        """``hello`` 校验：确认对面是同一套刻度和同一个棋盘。

        **这是唯一"宁可不连"的检查。** 分值刻度不一致时接上会静默污染右栏
        那两张图（``analysis.py`` 的胜率锚点直接钉在 ``STATIC_MAX`` 上），
        比慢一点糟得多；棋盘尺寸不一致更会直接算错。
        """
        rep = self._exchange(sock, {"type": "hello"}, config.CONNECT_TIMEOUT)
        if rep.get("type") != "hello":
            raise _RemoteError("hello 的应答不是 hello: %r" % (rep.get("type"),))
        if rep.get("board_size") != BOARD_SIZE:
            raise _RemoteError("棋盘尺寸不一致: %r != %d"
                               % (rep.get("board_size"), BOARD_SIZE))
        if rep.get("win_score") != _local.WIN_SCORE:
            raise _RemoteError("分值刻度不一致: WIN_SCORE %r != %d（接错程序？）"
                               % (rep.get("win_score"), _local.WIN_SCORE))
        if int(rep.get("static_max", 0)) != STATIC_MAX:
            raise _RemoteError("分值刻度不一致: STATIC_MAX %r != %d"
                               % (rep.get("static_max"), STATIC_MAX))
        self._desc = "%s v%s" % (config.binary_name(), rep.get("version", "?"))

    def _ensure(self):
        """返回可用连接；已连上的直接返回。**调用方不能持有 ``_conn_mu``。**"""
        with self._conn_mu:
            if self._sock is not None:
                return self._sock
            return self._connect()

    def _drop(self) -> None:
        """丢弃当前连接（对端已死/出错），下次请求会重连。"""
        with self._conn_mu:
            sock, self._sock = self._sock, None
            self._read_buf = b""
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass

    # ---- 收发 ----

    def _send_on(self, sock, obj) -> None:
        line = (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")
        with self._write_mu:
            sock.sendall(line)

    def _send(self, obj) -> None:
        """在已连接的 socket 上写一行。watcher 线程也走这里。"""
        sock = self._sock
        if sock is None:
            return
        self._send_on(sock, obj)

    def _exchange(self, sock, obj, timeout):
        """写一行、读一行。**只有发起请求的线程可以调用。**"""
        self._read_buf = b""
        line = (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")
        with self._write_mu:
            sock.sendall(line)
        return self._read(sock, timeout)

    def _read(self, sock, timeout):
        sock.settimeout(timeout)
        while True:
            pos = self._read_buf.find(b"\n")
            if pos >= 0:
                raw, self._read_buf = self._read_buf[:pos], self._read_buf[pos + 1:]
                try:
                    return json.loads(raw.decode("utf-8"))
                except Exception as exc:
                    raise _RemoteError("应答不是合法 JSON: %s" % exc)
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                raise _RemoteError("等待应答超时（%.1fs）" % timeout)
            except OSError as exc:
                raise _RemoteError("读应答失败: %s" % exc)
            if not chunk:
                raise _RemoteError("服务端关闭了连接")
            self._read_buf += chunk

    # ---- 请求 ----

    def _request(self, obj, timeout) -> dict:
        """发一条请求并等它的应答。**失败会自动重连重试一次。**"""
        last = None
        for attempt in (0, 1):
            try:
                sock = self._ensure()
                if self._needs_reset:
                    # reset 必须排在 compute **之前**（服务端按 FIFO 处理队列）。
                    rep = self._exchange(sock, {"type": "reset"}, config.CONNECT_TIMEOUT)
                    if rep.get("type") != "ok":
                        raise _RemoteError("reset 的应答不是 ok: %r" % (rep.get("type"),))
                    self._needs_reset = False
                rep = self._exchange(sock, obj, timeout)
            except _RemoteError as exc:
                last = exc
                self._drop()
                if attempt == 1:
                    break
                continue
            kind = rep.get("type")
            if kind == "error":
                # 服务端明确拒绝了这条请求（棋盘不合法 / 没有合法着法…）。
                # 重连再试没有意义 —— 同样的报文会得到同样的拒绝。
                raise _RemoteError("服务端拒绝: %s" % rep.get("error"))
            return rep
        raise last if last is not None else _RemoteError("请求失败")

    def compute(self, board, me, cfg, cancel):
        """一次搜索。返回 ``(idx, info)``：``idx`` 是线性格索引，``info`` 是诊断字典。

        ``idx`` 单独返回而不塞进 ``info``：``info`` 是**跨进程契约**，它的键集合
        与 ``engine_local`` 的逐字对应，多一个 ``idx`` 就会让"两边 info 相等"
        这类比较（测试与 ``gamelog`` 都在做）无声地失败。
        """
        req = {
            "type": "compute",
            "board": _board_to_json(board),
            "player": int(me),
            # 原样下发 —— 余量与 VCF 预算的解释权都在引擎侧（见 `_RESERVE`）。
            "time_limit": max(0.01, float(cfg["time"])),
            "max_depth": int(cfg["max_depth"]),
            "vcf_budget": max(0.0, float(cfg["vcf_budget"])),
            "qply": int(cfg["qply"]),
        }
        bias = cfg.get("bias")
        if bias:
            req["bias_attack"] = float(bias["attack"])
            req["bias_defence"] = float(bias["defence"])
            req["bias_tolerance"] = float(bias["tolerance"])

        # 增强搜索（LMR + 强制着法延伸）。**只有高级/宗师下发**，其余档一个键
        # 都不发 —— C++ 侧这三个字段默认 0，于是不开增强时它的搜索路径与
        # `engine_local` 逐位同源。那条逐字一致是移植正确性的护栏，也是用户
        # 要求的"不许降智"的机械保障，见 `DIFFICULTY` 里 2/3 档的注释。
        #
        # `lmr` / `extend` 分开下发是为了能做 A/B 隔离测量：要分别量出削减与
        # 延伸各自的贡献，混成一个开关就只能得到一个总数。
        if cfg.get("enhance"):
            req["enhanced"] = 1
            req["lmr"] = 1
            req["extend"] = 1

        # 读超时 = 服务端的硬上限 + 余量。服务端的"硬"是真的硬（它在搜索循环
        # 里查截止时刻），所以这里超时基本等于"对面不是我们的程序"或"机器被
        # 压垮了"，两种情况都该降级而不是无限等。
        timeout = req["time_limit"] + config.COMPUTE_SLACK + config.CONNECT_TIMEOUT
        stop = _start_cancel_watcher(self, cancel)
        try:
            rep = self._request(req, timeout)
        finally:
            if stop is not None:
                stop.set()
        return int(rep.get("idx", -1)), _info_from_reply(rep)


def _kill(proc) -> None:
    try:
        proc.kill()
        proc.wait(timeout=1.0)
    except Exception:
        pass


def _drain_stderr(proc) -> str:
    """进程已经退出时读它的 stderr（此时读不会阻塞）。"""
    try:
        data = proc.stderr.read()
    except Exception:
        return ""
    finally:
        for f in (proc.stdout, proc.stderr):
            try:
                f.close()
            except Exception:
                pass
    text = (data or b"").decode("utf-8", "replace").strip()
    return ("，stderr: " + text[-400:]) if text else ""


def _start_cancel_watcher(client, cancel):
    """协作式取消：``cancel`` 一置位就往同一条连接写一行 ``cancel``。

    返回一个 ``threading.Event``（置位即结束），``cancel`` 为 ``None`` 时返回
    ``None``。为什么必须是**独立线程**：主线程正阻塞在 ``recv`` 上等搜索结果，
    而取消必须同时发生 —— 让主线程自己轮询就等于要么放弃读、要么放弃取消。

    服务端收到 ``cancel`` 只置一个原子标志（**不进任务队列**），所以它能在
    搜索中途被看到。写与主线程的写共用 ``_write_mu``，行不会交错；服务端
    读线程按行解析，因此一条插进来的 ``cancel`` 不会破坏 ``compute`` 的配对。
    """
    if cancel is None:
        return None
    stop = threading.Event()

    def watch():
        while True:
            if cancel.is_set():
                try:
                    client._send({"type": "cancel"})
                except Exception:
                    pass
                return
            if stop.wait(config.CANCEL_POLL_INTERVAL):
                return

    threading.Thread(target=watch, name="gomoku-cancel", daemon=True).start()
    return stop


# ---------------------------------------------------------------------------
# 5. 报文 ↔ info 字典
# ---------------------------------------------------------------------------


def _board_to_json(board):
    """棋盘 → 嵌套 list。C++ 侧要求 19×19 的整数矩阵。

    ``main.py`` 的棋盘是 ndarray，``tools/`` 里有用 list 的，所以两条都要走。
    ``tolist()`` 出来的 Python int 正好就是 JSON 整数 —— **不能用 float**：
    C++ 侧 ``parseBoard`` 只接受数字格，但 float 会让 ``player`` 之类的整数
    比较在多一层浮点后变得不可预测。
    """
    tolist = getattr(board, "tolist", None)
    if tolist is not None:
        return tolist()
    return [list(row) for row in board]


def _info_from_reply(rep: dict) -> dict:
    """服务端应答 → ``info`` 字典。

    字段名**必须**与 ``engine_local`` 产出的逐字相同：``gamelog`` 按名字写日志
    （``best_val`` / ``actual_depth`` / ``time_ms``），``analysis.readout_line``
    读 ``depth`` / ``nodes`` / ``nps`` / ``time_ms``，``main.py`` 的两张图只认
    ``best_val``。服务端那边叫 ``score``（"搜索分"），这里翻成 ``best_val``
    —— 翻译只在这一处发生，别处一律看不到 ``score``。
    """
    info = {
        "reason": rep.get("reason", "PVS搜索"),
        "depth": int(rep.get("depth", 0)),
        "actual_depth": int(rep.get("actual_depth", 0)),
        "best_val": int(rep.get("score", 0)),
        "time_ms": float(rep.get("time_ms", 0.0)),
        "nodes": int(rep.get("nodes", 0)),
        "nps": int(rep.get("nps", 0)),
        "tt_hit_rate": float(rep.get("tt_hit_rate", 0.0)),
        "qnode_ratio": float(rep.get("qnode_ratio", 0.0)),
        "score_type": rep.get("score_type", "static"),
    }
    # ``vcf_state`` 的非激活态在协议里是真正的 ``null``。缺字段与"没开 VCF"
    # 必须区分开：0 是 VCF_NO_WIN，一个**真实的结论**。
    state = rep.get("vcf_state")
    if state is not None:
        info["vcf_state"] = int(state)
        info["vcf_dist"] = int(rep.get("vcf_dist", 0))
        info["vcf_nodes"] = int(rep.get("vcf_nodes", 0))
    return info


# ---------------------------------------------------------------------------
# 6. 玩家抽象
# ---------------------------------------------------------------------------


class LocalAIPlayer:
    """本地 Python 引擎。**永远是可用答案**，也是降级的目标。"""

    name = "local"

    def ai_move(self, board, me, level, cancel=None):
        return _local.ai_move(board, me, level, cancel=cancel)


class RemoteAIPlayer:
    """C++ 服务端。**任何失败都抛 ``_RemoteError``，由 ``ai_move`` 翻译成降级。**

    它自己不做降级 —— 于是"什么时候用本地"这条规则只在一个地方定义。
    """

    name = "cpp"

    def __init__(self, client: _ServerClient) -> None:
        self._client = client

    def ai_move(self, board, me, level, cancel=None):
        cfg = _cfg_for(level)
        idx, info = self._client.compute(board, me, cfg, cancel)
        if idx < 0:
            # 服务端返回了"没有合法着法"。本地引擎在同样情形下会退到
            # ``bd.candidates()[0]``，这里也照做 —— 让调用方永远拿到一个
            # 走法，而不是一个需要它自己判断的 -1。
            fallback = _local.Board.from_array(board).candidates()[0]
            return (fallback // BOARD_SIZE, fallback % BOARD_SIZE, info)
        return (idx // BOARD_SIZE, idx % BOARD_SIZE, info)


_LOCAL_PLAYER = LocalAIPlayer()
_CLIENT = _ServerClient()
_REMOTE_PLAYER = RemoteAIPlayer(_CLIENT)

atexit.register(_CLIENT.shutdown)


# ---------------------------------------------------------------------------
# 7. 搜索入口
# ---------------------------------------------------------------------------


def _has_stones(board) -> bool:
    """盘上是否已有棋子。用于把空盘路由给本地开局库。"""
    any_ = getattr(board, "any", None)
    if any_ is not None:
        return bool(any_())
    return any(v for row in board for v in row)


def ai_move(board, ai_player, depth, cancel=None):
    """搜索一步。

    签名与重构前**逐字相同**（``cancel`` 是第 4 个位置参数，非 keyword-only），
    返回 ``(r, c, info)``。``depth`` 的语义是**难度档位 1..5**，不是搜索深度。

    路由规则只有两条：

    1. **空盘与开局库命中走本地。** 开局是唯一必须零延迟的一手（用户点
       "开始"之后立刻要看到落子），一次网络往返换不来任何东西；而且
       ``reason='开局库'`` 只有本地能产出，挪到服务端会让日志里出现一个
       服务端没有的理由。
    2. **其余走远程，失败即降级。** 降级是**静默**的：不弹窗、不抛异常，
       只在 stderr 留一行原因，面板上那一行改成「Python (本地)」。

    **开局库的闸门必须是"查表命中"，不能是"盘上子少"。** 写成
    ``stone_count <= BOOK_STONES`` 就把 `opening_move` 里那条"紧贴对手第一子、
    偏移按固定顺序取第一个空点"的规则一并激活了 —— 那条规则只管一种局面、
    从不搜索，于是对手第一手下在非标准位置（比如角上）时，白方会从一个固定
    偏移里挑一手，而不是搜索。那是**降智**，而且旧版 `_OPENING_BOOK` 正因为
    键格式不匹配从未走到过那里，所以它今天不是现有行为。
    """
    if not _has_stones(board):
        # **不动指示器。** 开局库走本地是设计，不是降级 —— 把这一手记成
        # "Python (本地)" 会让面板在整局第一步就报出一个假警。它也不改变
        # 任何东西：开局库是查表，不是搜索。
        return _local.ai_move(board, ai_player, depth, cancel=cancel)
    if _local.book_lookup(board, ai_player) is not None:
        # 同上：查表不是降级，指示器不动。
        return _local.ai_move(board, ai_player, depth, cancel=cancel)

    try:
        out = _REMOTE_PLAYER.ai_move(board, ai_player, depth, cancel)
    except Exception as exc:
        # 兜住所有 Exception：这一层是纯粹的性能优化，没有资格让一局棋中断。
        # BaseException（KeyboardInterrupt / SystemExit）不在此列，照常穿透。
        _note("local", exc)
        return _local.ai_move(board, ai_player, depth, cancel=cancel)
    _note("cpp")
    return out


def new_game():
    """清空跨局面的持久状态。

    **本地与远程都要清，但只有本地能立刻清。** 本地引擎的置换表活在模块级
    ``_ENGINE`` 里，同步调用即完成；服务端的置换表同样跨请求保留（这是迭代
    加深收益的来源，跨**局**保留则是缺陷，见 ``search.h`` 的 ``reset``）。

    这里**只打一个标记**、不发网络请求，理由是 ``main.py:911`` 在 UI 主线程上
    调用它 —— 那是启动路径的一部分（``main.py:126`` 记了启动成本），一次连接
    超时就是一次肉眼可见的界面卡顿。真正的 ``reset`` 由下一次 ``ai_move`` 在
    子线程里补发，服务端按 FIFO 处理，语义与"先 reset 再 compute"完全一致。
    """
    _local.new_game()
    _CLIENT.mark_reset_needed()

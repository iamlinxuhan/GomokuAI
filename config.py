# -*- coding: utf-8 -*-
"""C++ 引擎服务端的定位与连接参数。

## 为什么单独一个文件

``engine.py`` 要能同时在四种布局下找到可执行文件：源码树（本机开发）、
PyInstaller 解包目录（``sys._MEIPASS``）、与主程序同目录的裸可执行文件
（免安装分发）、以及 PATH。这四条的判断互相独立，混在引擎门面里会让那个
文件变成"一堆路径猜测 + 一点网络代码"。

**只做路径解析，不做 IO** —— 本模块 import 时不会碰磁盘、不会连网、不会
拉起进程。调用方（``engine.py``）决定何时去试这些路径。

## 端口为什么是一个池子，而不是一个固定值

``main.py`` 没有 ``aboutToQuit`` / ``closeEvent``（只有 ``_on_quit``），所以
进程可能被强杀，服务端会活下来。下一次启动时那个残留进程仍占着端口 ——
**端口可预测**让"先连一下试试"成为唯一需要的探测手段：连上了就是它，
连不上再拉起新的。纯随机端口会让每次启动都留下一个孤儿进程。

但"固定"不等于"唯一一个数字"，原来写死的是 8888，实测撞上过：同一台
机器上放着一份**旧版本程序**，它的引擎叫 ``gomoku_server``、协议里还没有
``hello``，而 ``--port`` 默认值同样是 8888。改成池子（见 ``PORT_POOL``）
之后，撞车只是"换下一个"，而不是整块丢掉 C++ 引擎。
"""

from __future__ import annotations

import os
import sys

#: 只监听回环。桌面单机应用没有任何理由让引擎对外可达。
HOST = "127.0.0.1"

#: 候选端口池：**15 个不常用端口**，客户端按这个顺序逐个试。
#:
#: 为什么不是一个固定的 8888（原来就是）：固定端口一定会撞上别人，而 8888
#: 恰好是"最容易被随手占用"的那一类。实测撞上的是同一台机器上一份**旧版本
#: 程序** —— 握手不过，于是被判成"远程不可用"，整个 C++ 引擎被静默丢掉，
#: 面板上只写一行「Python (本地)」，症状与病根隔着一整层。
#:
#: 为什么落在 49000~49100：避开 IANA 的临时端口段（Linux 默认
#: ``net.ipv4.ip_local_port_range = 32768~60999``），不与内核自动分配出去的
#: 端口抢；这一段里没有注册过的知名服务。**用不到的端口代价是零**，
#: 用的是"探测一次"。
#:
#: **顺序有意义**：靠前的会被优先复用（另一个窗口拉起的服务端、上一次运行
#: 留下的残留进程）。池子里的端口每次运行都一样，所以残留进程仍然认得出来。
PORT_POOL = (
    49001, 49007, 49013, 49019, 49027,
    49031, 49037, 49043, 49051, 49059,
    49067, 49073, 49079, 49087, 49093,
)

#: 显式指定端口；``None`` = 走端口池。
#:
#: **测试与工具靠它把客户端钉在一个确定的端口上**（``tools/ab_enhance.py``
#: 与 ``tools/build_book.py`` 都需要一个"不与游戏共享"的服务端）。这与池子是
#: **互斥的两条路**：设了就只用这一个，被外人占了就退到内核端口，**不回到
#: 池子** —— 详见 ``engine._port_candidates`` 里"为什么不能兜底"。
PORT = None

#: **禁手引擎另有一个端口池，不与上面那个共用。**
#:
#: 共用一个池的理由曾经是想让"两个引擎靠 hello 的 rules 字段区分"成为必要 ——
#: 那条理由站不住。共用池的**实际后果**是：同一台机器上开两个窗口（一个无禁手
#: 一个禁手）会互相抢占对方想用的端口，15 格更快耗尽，一旦退到
#: ``engine._free_port()`` 的内核随机端口，**下一次运行就认不出上次留下的残留
#: 服务端** —— 那恰好废掉了池子存在的理由（见上面那段）。
#:
#: 分开两个池更简单，而且不需要 rules 握手也不会串。rules 握手仍然要加，但它是
#: **第二道保险**（把"连得上"升级成"连对了"），不是共用池的前提。
PORT_POOL_BAN = (
    49003, 49009, 49017, 49021, 49023,
    49029, 49033, 49039, 49047, 49057,
    49061, 49069, 49081, 49091, 49099,
)

#: 可执行文件名（Windows 下补 ``.exe``）。
BINARY_STEM = "gomoku_engine"

#: 禁手引擎的可执行文件名。**另一份构建产物**（同一份源码 + ``-DGOMOKU_BAN=1``），
#: 不是同一个程序的参数 —— 它是并列的第二套引擎。
BAN_BINARY_STEM = "gomoku_engine_ban"

#: 环境变量覆盖。测试与手工排查用它指向任意一份构建产物。
ENV_OVERRIDE = "GOMOKU_ENGINE"

#: 禁手引擎的环境变量覆盖。
BAN_ENV_OVERRIDE = "GOMOKU_ENGINE_BAN"

#: 变体名 → 文件名 / 环境变量 / 端口池。**只认这两个键**。
#:
#: 用变体名索引而不是把 ``(stem, override)`` 当两个自由参数传：那样可以拼出
#: "禁手的文件名 + 无禁手的环境变量"这种半对半的组合，症状是接上一份**别的**
#: 引擎却不报错。查表命中不到就 ``KeyError``，比一个安静的错配好。
VARIANT_BINARY_STEM = {"free": BINARY_STEM, "ban": BAN_BINARY_STEM}
VARIANT_ENV_OVERRIDE = {"free": ENV_OVERRIDE, "ban": BAN_ENV_OVERRIDE}
VARIANT_PORT_POOL = {"free": PORT_POOL, "ban": PORT_POOL_BAN}


def variant_of(name: str) -> str:
    """规范化变体名，顺便把拼错的名字挡在入口 —— 见 ``VARIANT_*`` 的注释。"""
    if name not in VARIANT_BINARY_STEM:
        raise ValueError("未知引擎变体: %r（只有 'free' / 'ban'）" % (name,))
    return name

#: 允许自动拉起服务端。关掉之后只连不拉（降级验证用）。
AUTOSTART = True

# ---- 超时（秒）----
#
# 三个超时对应三种"卡住"，**不能合并成一个**：
#
# * ``CONNECT_TIMEOUT`` —— 连一个没有人监听的端口。本机上是立刻 ECONNREFUSED，
#   给 0.5s 已经非常宽裕（留出量是给 Windows 上防火墙审计之类的慢路径）。
# * ``START_TIMEOUT`` —— 拉起进程后等它 bind 成功。冷启动要建 112 条线表与
#   72 万条 Zobrist，实测约 30ms，但首次读盘 + 杀软扫描可能到秒级。
# * ``COMPUTE_SLACK`` —— 读一次搜索结果。C++ 侧的时间是**硬上限**，正常必然
#   按时回；这里的余量是给"机器负载极高、线程被抢"这种不是 bug 的迟到。
CONNECT_TIMEOUT = 0.5
START_TIMEOUT = 5.0
COMPUTE_SLACK = 5.0

#: 取消轮询的间隔。``main.py`` 的 ``_cancel_ai`` 只等 3 秒（``w.wait(3000)``），
#: 从置位到 C++ 读到标志要经过"本线程轮询"+"网络"+"C++ 每 1024 节点轮询"三段，
#: 20ms 的本地轮询开销在这三段里可以忽略。
CANCEL_POLL_INTERVAL = 0.02

#: 退出时等服务端收尾的上限。超过就强杀 —— 用户关窗口不该等一个僵住的进程。
SHUTDOWN_TIMEOUT = 1.0


def binary_name(variant: str = "free") -> str:
    """当前平台下该变体的可执行文件名。"""
    return VARIANT_BINARY_STEM[variant_of(variant)] + (
        ".exe" if os.name == "nt" else "")


def _candidates(variant: str = "free") -> list:
    """按可信度从高到低列出候选路径。

    ``sys._MEIPASS`` 排在最前：打包后 ``__file__`` 指向解包目录内部的模块，
    而冻结的模块层级与原仓库不同，"源码树"那一条在打包版里指向一个不存在
    的路径 —— 顺序反了会先试一个必然失败的路径。
    """
    name = binary_name(variant)
    out = []

    # 1) 打包运行时（PyInstaller onedir / onefile 都是这个变量）
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        out.append(os.path.join(meipass, name))

    # 2) 环境变量：测试与手工排查
    override = os.environ.get(VARIANT_ENV_OVERRIDE[variant_of(variant)])
    if override:
        out.append(override)

    here = os.path.dirname(os.path.abspath(__file__))

    # 3) 源码树：cpp/build/（本机 CMake 的默认输出位置）
    out.append(os.path.join(here, "cpp", "build", name))

    # 4) 与主程序同目录：免安装裸可执行文件的分发形态
    out.append(os.path.join(here, name))
    if getattr(sys, "frozen", False):
        out.append(os.path.join(os.path.dirname(sys.executable), name))

    # 5) PATH
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if d:
            out.append(os.path.join(d, name))

    return out


def find_binary(variant: str = "free"):
    """返回该变体第一个存在的候选路径；一个都没有则返回 ``None``。

    **存在即可执行 ≠ 能跑**：架构不匹配、动态库缺失、或者根本不是我们的程序
    都会在这里通过。真正的把关在 ``engine.py`` 的 ``hello`` 校验 —— 它比对
    棋盘尺寸、``WIN_SCORE`` 与 ``rules``，能在第一次请求之前就把"接错东西了"
    挡下来。
    """
    for path in _candidates(variant):
        try:
            if os.path.isfile(path) and os.access(path, os.X_OK):
                return path
        except OSError:
            continue
    return None

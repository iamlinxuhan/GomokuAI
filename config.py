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

#: 可执行文件名（Windows 下补 ``.exe``）。
BINARY_STEM = "gomoku_engine"

#: 环境变量覆盖。测试与手工排查用它指向任意一份构建产物。
ENV_OVERRIDE = "GOMOKU_ENGINE"

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


def binary_name() -> str:
    """当前平台的可执行文件名。"""
    return BINARY_STEM + (".exe" if os.name == "nt" else "")


def _candidates() -> list:
    """按可信度从高到低列出候选路径。

    ``sys._MEIPASS`` 排在最前：打包后 ``__file__`` 指向解包目录内部的模块，
    而冻结的模块层级与原仓库不同，"源码树"那一条在打包版里指向一个不存在
    的路径 —— 顺序反了会先试一个必然失败的路径。
    """
    name = binary_name()
    out = []

    # 1) 打包运行时（PyInstaller onedir / onefile 都是这个变量）
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        out.append(os.path.join(meipass, name))

    # 2) 环境变量：测试与手工排查
    override = os.environ.get(ENV_OVERRIDE)
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


def find_binary():
    """返回第一个存在的候选路径；一个都没有则返回 ``None``。

    **存在即可执行 ≠ 能跑**：架构不匹配、动态库缺失、或者根本不是我们的程序
    都会在这里通过。真正的把关在 ``engine.py`` 的 ``hello`` 校验 —— 它比对
    棋盘尺寸与 ``WIN_SCORE``，能在第一次请求之前就把"接错东西了"挡下来。
    """
    for path in _candidates():
        try:
            if os.path.isfile(path) and os.access(path, os.X_OK):
                return path
        except OSError:
            continue
    return None

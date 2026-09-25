# -*- coding: utf-8 -*-
"""远程客户端的**端口选取与端口冲突**行为。

两条设计，各有一组用例：

**一、端口池（``config.PORT_POOL``，15 个不常用端口）**

原来写死一个 8888。实测撞上过：同一台机器上放着一份**旧版**本程序
（``GomokuAI for 83/``），它的引擎可执行文件叫 ``gomoku_server``、协议里还没有
``hello``，而 ``--port`` 的默认值同样是 8888。于是新版这里发生的是：

    先连现成的监听者 → 连上了（但那是旧引擎）
    → 握手拿回 ``{"type":"error","message":"未知的消息类型：hello"}``
    → 抛 ``_RemoteError`` → **静默降级到本地 Python**

面板上只写一行「Python (本地)」，用户看到的现象是"初级都要想 3 秒"，而 C++
引擎其实好端端地躺在磁盘上 —— 症状与病根隔着一整层，没有任何一处报错指向端口。

现在按池子逐个试，**每一步都过 hello 校验**：拿到我们的引擎就用它；占着端口
的是别人就换下一格；15 格全不可用才退到"让内核给一个空闲端口"，最后才是降级。

**两条路互斥**：设了 ``config.PORT``（``tools/ab_enhance.py`` 这类工具与测试
才设）就**只**用那一个端口，被外人占了退到内核端口，**不回池子** —— 池子是
应用自己的路径，回过去就可能与游戏、或与 A/B 的另一条臂共用服务端。

**二、"连得上"不等于"是我们的引擎"**

这是上一版真正的漏洞。``_try_port`` 的顺序是"先看有没有人在监听，再决定是
复用还是自己拉"：有人在监听就必须过握手（是另一个窗口拉起的服务端就复用，
是别人的程序就换下一格）；**不能只看连得上就执行搜索** —— 那会把别人的服务端
当成自己的，而分值刻度不同会静默污染右栏那两张图。

需要 ``cpp/build/gomoku_engine`` 存在，没有就跳过（CI 的 test job 不建 C++）。
"""

from __future__ import annotations

import json
import socket
import threading
import time

import numpy as np
import pytest

import config
import engine


class _Squatter:
    """占着端口的冒充者：对任何报文都回一个 ``type=error``。

    这就是旧版 ``gomoku_server`` 对新版 hello 的真实应答 —— 它不认识
    ``hello`` 这个类型，于是走"未知消息类型"的错误分支。
    """

    def __init__(self, port=0):
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._mu = threading.Lock()
        self._conns = []            # 已被 accept、还没关掉的连接
        self._closed = False
        # 上一条用例的**收尾**有几十毫秒的窗口（`close()` 里等线程退出、
        # TIME_WAIT 的记账），所以这里重试而不是一 bind 失败就 skip —— 这不是
        # 被测代码的问题，机器上真被别人占着才会一直失败到底。真正的收尾在
        # `close()` 里做（超时 + join，见那里的注释）。
        for _ in range(20):
            try:
                self.srv.bind((config.HOST, port))  # 0 = 让内核给一个空闲端口
                break
            except OSError as exc:
                last = exc
                time.sleep(0.05)
        else:
            self.srv.close()
            pytest.skip("本机占着端口 %d（%s），这条用例无从验证" % (port, last))
        self.srv.listen(8)
        # **必须给监听器设超时。** 没有它，`_serve` 会一直阻塞在 `accept()` 里，
        # 而我们 `close()` 掉监听 socket 并不能让那个阻塞中的 accept 醒来 ——
        # 内核会替它把 fd 留着，于是 `127.0.0.1:49001` 一直停在 LISTEN，下一条
        # 用例 bind 同一格就 EADDRINUSE（实测：只关 socket 不改这里，用例 4 会
        # 稳定 skip）。超时让线程每 0.1s 自己醒一次、看到 `_closed` 就退出，
        # fd 这才真正释放。
        self.srv.settimeout(0.1)
        self.port = self.srv.getsockname()[1]
        self.hits = 0                               # 被探测/被误连的次数
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._closed:
            try:
                conn, _ = self.srv.accept()
            except socket.timeout:
                continue                            # 定时醒来，回去看 _closed
            except OSError:
                return                              # close() 之后 accept 报错，正常退出
            self.hits += 1
            with self._mu:
                if self._closed:
                    conn.close()
                    return
                self._conns.append(conn)
            try:
                conn.recv(4096)
                conn.sendall(json.dumps(
                    {"type": "error", "message": "未知的消息类型：hello"},
                    ensure_ascii=False).encode("utf-8") + b"\n")
            except OSError:
                pass
            finally:
                with self._mu:
                    if conn in self._conns:
                        self._conns.remove(conn)
                conn.close()

    def close(self):
        """关掉监听 socket **以及已建立的那些连接**。

        后者是必须的：``bind`` 失败与否看的是"这个端口上还有没有连接"，而
        ``srv.close()`` 不动已 accept 的 socket —— 只关监听器，会把这条用例的
        残留留给下一条用例（表现为下一条莫名其妙地 skip）。
        """
        with self._mu:
            self._closed = True
            conns, self._conns = self._conns, []
            try:
                self.srv.close()
            except OSError:
                pass
        for conn in conns:
            try:
                conn.shutdown(socket.SHUT_RDWR)     # 让还在 recv 的那条线程立刻返回
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass
        # 等线程真的退出：它可能还卡在 accept/recv 里，而只要它还持有那个 fd，
        # 这一格就还是"被占"。`close()` 返回时端口必须已经干净。
        self._thread.join(timeout=0.5)


def mk():
    """一个非空、且**不在开局库里**的局面（开局库那一手走本地，绕开远程）。"""
    m = np.zeros((19, 19), dtype=np.uint8)
    m[9][9] = 1
    m[9][10] = 2
    m[8][9] = 1
    return m


def _cfg():
    cfg = dict(engine._cfg_for(1))
    cfg["time"] = 0.3           # 这些用例测的是连接，不是棋力
    return cfg


@pytest.fixture
def require_binary():
    if config.find_binary() is None:
        pytest.skip("没有构建 cpp/build/gomoku_engine，这些用例无从验证")


@pytest.fixture
def client(require_binary):
    """一个**独立**的客户端（不动模块级单例，也不注册 atexit）。"""
    c = engine._ServerClient()
    yield c
    c.shutdown()


def _server_port(c):
    """客户端连上的**服务端端口**。这是"它最终用了哪一格"的权威答案。"""
    return c._sock.getpeername()[1]


def test_cpp_engine_survives_a_squatted_port(monkeypatch, client):
    """显式指定的端口被别人占着时，仍然要用上 C++ 引擎（而不是静默退回 Python）。

    显式指定端口 = "要一个**独享**的服务端"（``tools/ab_enhance.py`` 的两条臂
    靠它互不共享）。所以被外人占了之后**不能退到池子**：池子是应用自己的路径，
    退过去就可能与游戏、或与另一条臂共用同一个服务端与同一块置换表。正确的
    下一站是"让内核给一个空闲端口"（见 ``engine._port_candidates``）。

    **反向验证**：把 ``_try_port`` 换成"握手不过就抛"（等价于修复前的行为），
    这条用例立刻变红 —— 它会死在 ``compute`` 抛出的 ``_RemoteError`` 上。
    """
    squatter = _Squatter()
    monkeypatch.setattr(config, "PORT", squatter.port)
    try:
        idx, info = client.compute(mk(), 2, _cfg(), None)
        assert squatter.hits >= 1, "应当先探测过那个被占的端口"
        assert client._desc.startswith(config.binary_name()), (
            "连上的不是我们的引擎：%r —— 端口被占时必须换一个端口自己拉一个，"
            "而不是退回 Python" % (client._desc,))
        used = _server_port(client)
        assert used != squatter.port, "不能复用冒充者的端口"
        assert used not in config.PORT_POOL, (
            "显式指定的端口被占，不该退到池子（用了 %d），池子是应用自己的路径"
            % used)
        assert 0 <= idx < 361, f"着法越界：{idx}"
        assert info["reason"] != "开局库", "开局库那一手走本地，不该经过远程"
    finally:
        squatter.close()


def test_panel_port_follows_the_live_connection(monkeypatch, client):
    """面板上「当前 TCP 端口」那一行的数据源：连之前必须是空，连上之后是**真的那一格**。

    这一行是给排查用的（"它到底连在哪一格"），所以**报错的方向只能是"少报"**：
    还没有连接时编一个池子里的号出来，会让用户以为连接已经建立、进而去查一个
    根本不存在的连接。连接之后则必须与服务端实际监听的端口一致 ——
    `getpeername()` 给的答案，不是我们自己记下来的意图。
    """
    monkeypatch.setattr(engine, "_CLIENT", client)
    assert engine.current_port() is None, "还没连过就报端口，等于编了一个连接出来"
    client.compute(mk(), 2, _cfg(), None)
    assert engine.current_port() == _server_port(client)


def test_explicit_port_never_falls_into_the_pool(monkeypatch, client):
    """显式端口与池子是**互斥**的两条路 —— 候选列表上就不该出现池子里的号。

    这条比上面那条更硬：上面量的是"最后用了哪个端口"，这一条直接量候选表。
    它挡的是"A/B 两条臂各自独享一个端口"这个前提被悄悄破坏 —— 一旦某条臂被
    挤到池子里，两条臂就可能共用服务端与置换表，而它们一个是 ``enhanced=0``、
    一个是 ``enhanced=1``，索引几何都不同，结论会被系统性地拉向"打平"。
    """
    monkeypatch.setattr(config, "PORT", 43210)
    assert engine._port_candidates() == [43210]
    monkeypatch.setattr(config, "PORT", None)
    assert engine._port_candidates() == list(config.PORT_POOL)


def test_squatter_is_probed_but_never_used(monkeypatch, client):
    """冒充者只会被**探测**，不会被执行搜索 —— 两件事必须分得开。

    "连得上就复用"这个优化本身是对的（另一个窗口拉起的服务端要能被复用，
    服务端是单客户端的，多拉一个反而会互相顶掉），错的只是把"连得上"当成
    "是同一个引擎"。冒充者收到的只能是握手探测。
    """
    squatter = _Squatter()
    monkeypatch.setattr(config, "PORT", squatter.port)
    try:
        client.compute(mk(), 2, _cfg(), None)
        assert squatter.hits <= 2, \
            f"冒充者收到了 {squatter.hits} 次连接，说明搜索被它接走了"
        assert client._desc.startswith(config.binary_name())
    finally:
        squatter.close()


def test_pool_is_walked_in_order(require_binary, client):
    """池子第一格被别人的程序占着 → 用第二格，**不是**跳过整个池子。

    这是"逐次通信"的直接断言：``getpeername()`` 给出服务端真实端口，因此
    "它到底用了哪一格"是量出来的，不是推断的。
    """
    squatter = _Squatter(config.PORT_POOL[0])
    try:
        client.compute(mk(), 2, _cfg(), None)
        assert squatter.hits >= 1, "池子第一格应当被探测过"
        assert _server_port(client) == config.PORT_POOL[1], (
            "第一格被占时应当用第二格 %d，实际用了 %d"
            % (config.PORT_POOL[1], _server_port(client)))
    finally:
        squatter.close()


def test_pool_exhausted_falls_back_to_a_kernel_port(require_binary, client):
    """15 格**全部**不可用时，退到"让内核给一个空闲端口" —— 仍然不丢 C++ 引擎。

    降级是这条路线的最后一站，不是第一站。这里断言的是"没到那一站"：
    连接照常建立、握手照常通过，只是端口号落在池子之外、不可预测。
    """
    holds = [_Squatter(p) for p in config.PORT_POOL]
    try:
        client.compute(mk(), 2, _cfg(), None)
        port = _server_port(client)
        assert client._desc.startswith(config.binary_name()), \
            "池子全被占时仍然必须用上 C++ 引擎"
        assert port not in config.PORT_POOL, \
            "池子全被占了却说用的是池子里的端口 %d，这不可能是真的" % port
        assert all(h.hits >= 1 for h in holds), "池子里每一格都应当被探测过"
    finally:
        for h in holds:
            h.close()

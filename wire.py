# -*- coding: utf-8 -*-
"""房间协议线格式（M3）：JSON + 换行，双向版本校验。

风格与教训都来自 ``engine.py`` 的引擎协议：

* 一行一个 JSON 对象，UTF-8，``\\n`` 结尾；
* ``hello`` 必须双向核对版本 —— "连得上"不等于"是同类程序"；
* 解析失败 / 超时 / 断线统一收敛成 ``WireError``，由调用方决定回错误还是
  关连接（与引擎门面的"任何问题只降级"不同：房间是双人对局，错误必须
  让用户看见）。

本模块只做线格式，不认识业务消息；房间语义在 ``room.py``。
"""

from __future__ import annotations

import json
import socket
import threading

#: 协议版本。任何不兼容的报文结构变更都要 +1，服务端会明确拒绝旧版本。
PROTO_VERSION = 1
SERVER_NAME = "gomoku-room"


class WireError(Exception):
    """线上错误：``code`` 给机器看，``message`` 给人看。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def encode(obj) -> bytes:
    return (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")


class WireConnection:
    """一条连接：线程安全地发一行、阻塞地收一行。

    只保证"报文边界"与"JSON 合法性"；``type`` 字段必须存在（业务消息都有），
    但具体类型不在这里校验。
    """

    def __init__(self, sock, *, recv_timeout=None):
        self.sock = sock
        self.recv_timeout = recv_timeout
        self._buf = b""
        self._send_lock = threading.Lock()
        self.closed = False

    @classmethod
    def connect(cls, host: str, port: int, timeout: float = 5.0):
        sock = socket.create_connection((host, port), timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return cls(sock)

    def send(self, obj) -> None:
        data = encode(obj)
        with self._send_lock:
            if self.closed:
                raise WireError("closed", "连接已关闭")
            try:
                self.sock.sendall(data)
            except OSError as exc:
                self.closed = True
                raise WireError("io", "发送失败: %s" % exc)

    def recv(self, timeout=None) -> dict:
        timeout = self.recv_timeout if timeout is None else timeout
        while True:
            pos = self._buf.find(b"\n")
            if pos >= 0:
                raw, self._buf = self._buf[:pos], self._buf[pos + 1:]
                try:
                    obj = json.loads(raw.decode("utf-8"))
                except Exception as exc:
                    raise WireError("bad_json", "不是合法 JSON: %s" % exc)
                if not isinstance(obj, dict) or "type" not in obj:
                    raise WireError("bad_message", "报文缺少 type 字段")
                return obj
            try:
                self.sock.settimeout(timeout)
                chunk = self.sock.recv(65536)
            except socket.timeout:
                raise WireError("timeout", "等待报文超时")
            except OSError as exc:
                self.closed = True
                raise WireError("io", "读取失败: %s" % exc)
            if not chunk:
                self.closed = True
                raise WireError("closed", "对端关闭了连接")
            self._buf += chunk

    def close(self) -> None:
        self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass

# -*- coding: utf-8 -*-
"""对局客户端（M4a）：UI 与对局之间的唯一接口。

两种连接形态共用同一个协议（``wire.py``）：

* 加入专用/其他玩家开的房间：``RoomClient.connect(host, port, room, seat)``；
* 应用内“对局域网开放”的本地房间：``LocalRoom`` 在 127.0.0.1 的临时端口上
  起一个真正的 ``RoomServer``，客户端照常走 TCP 连它 —— 与 MC 的内置服务端
  同一个思路：**单机与联机只有一条代码路径**。

事件回调 ``on_event``：``welcome`` 在 ``connect`` 返回前**同步**发出，其余
在读取线程上发出。Qt 侧由 M4b 的适配层投递回主线程（``pyqtSignal``）。
"""

from __future__ import annotations

import threading

from room import Room, SeatSpec
from server import RoomServer
from wire import PROTO_VERSION, WireConnection, WireError


class LocalRoom:
    """应用内房间：``RoomServer(127.0.0.1:0)`` + 一个房间。

    ``port`` 是内核分配的真实端口 —— "对局域网开放"时把这个服务端绑到
    0.0.0.0 并公布端口即可（M4c）。

    ``auto_consent=True``：同进程内的对手（本地双人的另一条连接）悔棋无需
    协商，沿用单机旧行为。专用服务器不受影响（``Room`` 默认 False）。
    """

    def __init__(self, black: SeatSpec, white: SeatSpec, name: str = "local",
                 host: str = "127.0.0.1"):
        self.host = host
        self.name = name
        self.server = RoomServer(host)
        self.room = Room(name, black, white, auto_consent=True)
        self.server.add_room(self.room)
        self.port = self.server.start(0)

    @property
    def result(self):
        return self.room.result

    def stop(self) -> None:
        self.server.stop()


class RoomClient:
    """一条房间连接。线程安全的 ``send``；事件经 ``on_event`` 回调。"""

    def __init__(self, on_event=None):
        self.on_event = on_event or (lambda event: None)
        self.seat = None
        self.room_name = None
        self._wire = None
        self._stop = threading.Event()
        self._reader = None

    # ------------------------------------------------------------ 连接

    def connect(self, host: str, port: int, room: str, seat: str,
                timeout: float = 5.0) -> dict:
        """握手 + 入座；成功返回 ``welcome`` 报文（并已发给 on_event）。"""
        if self._wire is not None:
            raise RuntimeError("这个客户端已经连接")
        wire = WireConnection.connect(host, port, timeout)
        wire.send({"type": "hello", "proto": PROTO_VERSION, "name": "ui"})
        rep = wire.recv(timeout=timeout)
        if rep.get("type") != "hello_ok":
            raise WireError(rep.get("code", "hello_failed"),
                            rep.get("message", "握手失败"))
        wire.send({"type": "join", "room": room, "seat": seat})
        rep = wire.recv(timeout=timeout)
        if rep.get("type") != "welcome":
            raise WireError(rep.get("code", "join_failed"),
                            rep.get("message", "入座失败"))

        self._wire = wire
        self.seat = seat
        self.room_name = room
        self.on_event(rep)
        self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                        name="room-client")
        self._reader.start()
        return rep

    # ------------------------------------------------------------ 上行

    def move(self, r: int, c: int) -> None:
        self._send({"type": "move", "r": int(r), "c": int(c)})

    def ping(self) -> None:
        self._send({"type": "ping"})

    def undo_request(self) -> None:
        self._send({"type": "undo_request"})

    def _send(self, obj) -> None:
        wire = self._wire
        if wire is None:
            raise RuntimeError("尚未连接")
        wire.send(obj)

    # ------------------------------------------------------------ 下行

    def _read_loop(self) -> None:
        while not self._stop.is_set():
            try:
                msg = self._wire.recv(timeout=1.0)
            except WireError as exc:
                if exc.code == "timeout":
                    continue
                if not self._stop.is_set():
                    self.on_event({"type": "error", "code": exc.code,
                                   "message": exc.message})
                return
            self.on_event(msg)

    def close(self) -> None:
        self._stop.set()
        wire, self._wire = self._wire, None
        if wire is not None:
            wire.close()
        if self._reader is not None and self._reader.is_alive():
            self._reader.join(timeout=1.0)
        self._reader = None

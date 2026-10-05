# -*- coding: utf-8 -*-
"""房间服务端（M3）：监听、房间注册表、专用模式。

用法::

    .venv/bin/python server.py --port 48900 --room demo \
        --black remote --white remote
    .venv/bin/python server.py --port 0 --room boss \
        --black remote --white ai --white-level 3

连接生命周期：``hello``（版本校验）→ ``join``（房间 + 席位）→ ``welcome``
→ ``move`` / ``ping``。每个连接一个处理线程；房间自己的对局线程在
``room.py``。内置形态（应用内开房间）在 M4 接上同一个 ``RoomServer``。
"""

from __future__ import annotations

import argparse
import socket
import threading
import time

from room import Room, SeatSpec
from wire import PROTO_VERSION, SERVER_NAME, WireConnection, WireError

#: 房间服务的默认端口。与引擎端口池（config.PORT_POOL，49001–49093）
#: **错开**：它们服务两个完全不同的东西，"撞车"不该发生在自家组件之间。
DEFAULT_PORT = 48900


class RoomServer:
    def __init__(self, host: str = "127.0.0.1"):
        self.host = host
        self.port = None
        self.rooms = {}
        self._sock = None
        self._stop = threading.Event()
        self._thread = None

    def add_room(self, room: Room) -> None:
        self.rooms[room.name] = room
        room.start()

    def start(self, port: int = DEFAULT_PORT) -> int:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, int(port)))
        sock.listen(8)
        self._sock = sock
        self.port = sock.getsockname()[1]
        self._thread = threading.Thread(target=self._accept_loop, daemon=True,
                                        name="room-server")
        self._thread.start()
        return self.port

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _addr = self._sock.accept()
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,),
                             daemon=True).start()

    def _handle(self, sock) -> None:
        wire = WireConnection(sock)
        room = None
        try:
            try:
                msg = wire.recv(timeout=10)
            except WireError:
                raise WireError("bad_hello", "没有收到 hello")
            if msg.get("type") != "hello":
                raise WireError("bad_hello", "第一个报文必须是 hello")
            if int(msg.get("proto", -1)) != PROTO_VERSION:
                wire.send({"type": "error", "code": "proto_mismatch",
                           "message": "协议版本不匹配：服务端 %d，客户端 %r"
                                      % (PROTO_VERSION, msg.get("proto"))})
                return
            wire.send({"type": "hello_ok", "proto": PROTO_VERSION,
                       "server": SERVER_NAME})

            try:
                msg = wire.recv(timeout=30)
            except WireError:
                raise WireError("bad_join", "没有收到 join")
            if msg.get("type") != "join":
                raise WireError("bad_join", "hello 之后必须是 join")

            room = self.rooms.get(msg.get("room"))
            if room is None:
                wire.send({"type": "error", "code": "no_room",
                           "message": "没有这个房间：%r" % (msg.get("room"),)})
                room = None
                return
            seat_name = msg.get("seat")
            stone = {"black": 1, "white": 2}.get(seat_name)
            if stone is None:
                wire.send({"type": "error", "code": "bad_seat",
                           "message": "席位只能是 black / white"})
                room = None
                return
            room.attach(stone, wire)
            wire.send({"type": "welcome", "proto": PROTO_VERSION,
                       "room": room.name, "seat": seat_name,
                       "seats": room.seats_json(),
                       "config": room.config.to_json(),
                       **room.state_payload()})

            while not self._stop.is_set():
                msg = wire.recv()
                t = msg.get("type")
                if t == "move":
                    try:
                        room.submit_move(stone, msg.get("r"), msg.get("c"))
                    except WireError as exc:
                        wire.send({"type": "error", "code": exc.code,
                                   "message": exc.message})
                elif t == "undo_request":
                    try:
                        room.submit_undo_request(stone)
                    except WireError as exc:
                        wire.send({"type": "error", "code": exc.code,
                                   "message": exc.message})
                elif t == "undo_response":
                    try:
                        room.submit_undo_response(stone,
                                                  bool(msg.get("accept")))
                    except WireError as exc:
                        wire.send({"type": "error", "code": exc.code,
                                   "message": exc.message})
                elif t == "ping":
                    wire.send({"type": "pong"})
                else:
                    wire.send({"type": "error", "code": "unknown_type",
                               "message": "未知消息类型：%r" % (t,)})
        except WireError as exc:
            try:
                wire.send({"type": "error", "code": exc.code,
                           "message": exc.message})
            except WireError:
                pass
        finally:
            if room is not None:
                room.detach(wire)
            wire.close()


def _parse_seat(kind: str, level: int) -> SeatSpec:
    return SeatSpec("ai", level=level) if kind == "ai" else SeatSpec("remote")


def main():
    ap = argparse.ArgumentParser(description="GomokuAI 房间服务端（专用模式）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help="0 = 让内核分配（启动后会打印实际端口）")
    ap.add_argument("--room", default="demo", help="房间名")
    ap.add_argument("--black", choices=("remote", "ai"), default="remote")
    ap.add_argument("--black-level", type=int, default=1)
    ap.add_argument("--white", choices=("remote", "ai"), default="remote")
    ap.add_argument("--white-level", type=int, default=1)
    args = ap.parse_args()

    srv = RoomServer(args.host)
    srv.add_room(Room(args.room,
                      _parse_seat(args.black, args.black_level),
                      _parse_seat(args.white, args.white_level)))
    port = srv.start(args.port)
    print("房间服务端已启动：%s:%d 房间=%s 黑=%s 白=%s"
          % (args.host, port, args.room, args.black, args.white), flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        srv.stop()


if __name__ == "__main__":
    main()

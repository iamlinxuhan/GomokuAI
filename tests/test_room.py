# -*- coding: utf-8 -*-
"""房间服务端：协议、席位与完整对局的 socket 级测试（M3）。

全程走真实 loopback socket；服务端 AI 席位用替身玩家（monkeypatch
``room.AIPlayer``），不拉起真实搜索引擎 —— 那属于 tools/bot_client.py 的
手工冒烟，放进来会让套件依赖引擎与端口状态。
"""

from __future__ import annotations

import threading

import pytest

import room as room_module
from room import Room, SeatSpec
from server import RoomServer
from wire import PROTO_VERSION, WireConnection


@pytest.fixture
def server():
    srv = RoomServer("127.0.0.1")
    room = Room("demo", SeatSpec("remote"), SeatSpec("remote"))
    srv.add_room(room)
    port = srv.start(0)
    yield srv, room, port
    srv.stop()


def _hello_join(port, room="demo", seat="black", proto=PROTO_VERSION):
    wire = WireConnection.connect("127.0.0.1", port, timeout=3.0)
    wire.send({"type": "hello", "proto": proto})
    rep = wire.recv(timeout=3)
    if rep.get("type") != "hello_ok":
        return wire, rep
    wire.send({"type": "join", "room": room, "seat": seat})
    return wire, wire.recv(timeout=3)


class _ScriptedClient(threading.Thread):
    """连上房间，轮到自己时按脚本出招，收集全部服务端事件。"""

    def __init__(self, port, seat, moves, room="demo"):
        super().__init__(daemon=True)
        self.port, self.seat, self.room = port, seat, room
        self.moves = list(moves)
        self.events = []
        self.error = None
        self.my_stone = 1 if seat == "black" else 2

    def run(self):
        wire = None
        try:
            wire = WireConnection.connect("127.0.0.1", self.port, timeout=3.0)
            wire.send({"type": "hello", "proto": PROTO_VERSION})
            rep = wire.recv(timeout=3)
            if rep.get("type") != "hello_ok":
                self.events.append(rep)
                return
            wire.send({"type": "join", "room": self.room, "seat": self.seat})
            rep = wire.recv(timeout=3)
            self.events.append(rep)
            if rep.get("type") != "welcome":
                return
            while True:
                msg = wire.recv(timeout=10)
                self.events.append(msg)
                t = msg.get("type")
                if t == "turn" and msg["stone"] == self.my_stone:
                    if not self.moves:
                        return
                    r, c = self.moves.pop(0)
                    wire.send({"type": "move", "r": r, "c": c})
                elif t == "game_over":
                    return
        except Exception as exc:                     # noqa: BLE001
            self.error = exc
        finally:
            if wire is not None:
                wire.close()


class _ColAI:
    """服务端 AI 替身：黑填第 0 列，白填第 18 列 —— 九手内黑胜，确定性。"""

    def __init__(self, spec):
        self.spec = spec

    def choose_move(self, board, stone, cancel=None):
        col = 0 if stone == 1 else 18
        for r in range(19):
            if board[r][col] == 0:
                return r, col, {}
        raise RuntimeError("没有可下的位置")


# ==================== 完整对局 ====================

def test_full_game_over_loopback(server):
    _srv, room, port = server
    black = _ScriptedClient(port, "black",
                            [(9, 0), (9, 1), (9, 2), (9, 3), (9, 4)])
    white = _ScriptedClient(port, "white",
                            [(8, 0), (8, 1), (8, 2), (8, 3)])
    black.start()
    white.start()

    assert room.done.wait(10), "对局没有在时限内结束"
    black.join(5)
    white.join(5)
    assert black.error is None and white.error is None
    assert room.result == {"winner": 1, "reason": "five", "moves": 9}

    for cli in (black, white):
        types = [e.get("type") for e in cli.events]
        assert "welcome" in types and "game_over" in types
        welcome = next(e for e in cli.events if e["type"] == "welcome")
        assert welcome["room"] == "demo" and welcome["seat"] == cli.seat
        assert len(welcome["board"]) == 19
        final = next(e for e in cli.events if e["type"] == "game_over")
        assert final["winner"] == 1 and final["moves"] == 9


def test_server_side_ai_seat(monkeypatch):
    monkeypatch.setattr(room_module, "AIPlayer", _ColAI)
    srv = RoomServer("127.0.0.1")
    room = Room("solo", SeatSpec("ai", level=1), SeatSpec("remote"))
    srv.add_room(room)
    port = srv.start(0)
    try:
        white = _ScriptedClient(port, "white",
                                [(18, 0), (18, 1), (18, 2), (18, 3)],
                                room="solo")
        white.start()
        assert room.done.wait(10)
        white.join(5)
        assert white.error is None
        assert room.result == {"winner": 1, "reason": "five", "moves": 9}
    finally:
        srv.stop()


def test_multiple_rooms_run_independently(monkeypatch):
    monkeypatch.setattr(room_module, "AIPlayer", _ColAI)
    srv = RoomServer("127.0.0.1")
    ra = Room("a", SeatSpec("ai"), SeatSpec("ai"))
    rb = Room("b", SeatSpec("ai"), SeatSpec("ai"))
    srv.add_room(ra)
    srv.add_room(rb)
    srv.start(0)
    try:
        assert ra.done.wait(10) and rb.done.wait(10)
        for r in (ra, rb):
            assert r.result == {"winner": 1, "reason": "five", "moves": 9}
    finally:
        srv.stop()


# ==================== 协议错误路径 ====================

def test_proto_mismatch_is_rejected(server):
    _srv, _room, port = server
    wire = WireConnection.connect("127.0.0.1", port, timeout=3.0)
    try:
        wire.send({"type": "hello", "proto": PROTO_VERSION + 99})
        rep = wire.recv(timeout=3)
        assert rep["type"] == "error" and rep["code"] == "proto_mismatch"
    finally:
        wire.close()


def test_unknown_room_is_rejected(server):
    _srv, _room, port = server
    wire, rep = _hello_join(port, room="nope")
    try:
        assert rep["type"] == "error" and rep["code"] == "no_room"
    finally:
        wire.close()


def test_seat_taken_is_rejected(server):
    _srv, _room, port = server
    w1, r1 = _hello_join(port, seat="black")
    w2, r2 = _hello_join(port, seat="black")
    try:
        assert r1["type"] == "welcome"
        assert r2["type"] == "error" and r2["code"] == "seat_taken"
    finally:
        w1.close()
        w2.close()


def test_join_into_ai_seat_is_rejected(monkeypatch):
    monkeypatch.setattr(room_module, "AIPlayer", _ColAI)
    srv = RoomServer("127.0.0.1")
    srv.add_room(Room("solo", SeatSpec("ai"), SeatSpec("remote")))
    port = srv.start(0)
    try:
        wire, rep = _hello_join(port, room="solo", seat="black")
        try:
            assert rep["type"] == "error" and rep["code"] == "seat_not_remote"
        finally:
            wire.close()
    finally:
        srv.stop()


def test_unknown_type_and_ping(server):
    _srv, _room, port = server
    wire, rep = _hello_join(port, seat="white")
    try:
        assert rep["type"] == "welcome"
        wire.send({"type": "dance"})
        rep = wire.recv(timeout=3)
        assert rep["type"] == "error" and rep["code"] == "unknown_type"
        wire.send({"type": "ping"})
        assert wire.recv(timeout=3)["type"] == "pong"
    finally:
        wire.close()

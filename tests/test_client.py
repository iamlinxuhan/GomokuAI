# -*- coding: utf-8 -*-
"""RoomClient 与 LocalRoom 的 socket 级测试（M4a）。"""

from __future__ import annotations

import threading
import time

import pytest

from client import LocalRoom, RoomClient, local_ip
from room import Room, RoomConfig, SeatSpec
from server import RoomServer
from wire import WireError


def _wait(pred, timeout=10.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_local_room_two_clients_play():
    local = LocalRoom(SeatSpec("remote"), SeatSpec("remote"))
    events = {1: [], 2: []}
    lock = threading.Lock()

    def collector(stone):
        def _on(event):
            with lock:
                events[stone].append(event)
        return _on

    c1, c2 = RoomClient(collector(1)), RoomClient(collector(2))
    try:
        rep1 = c1.connect(local.host, local.port, local.name, "black")
        rep2 = c2.connect(local.host, local.port, local.name, "white")
        assert rep1["type"] == "welcome" and rep1["seat"] == "black"
        assert rep2["type"] == "welcome" and rep2["seat"] == "white"
        # welcome 是同步事件：connect 返回前就已经投递
        assert events[1][0]["type"] == "welcome"

        # 各自的着法进各自席位的队列，由房间按回合消费 —— 可以一次发完
        for mv in [(9, 0), (9, 1), (9, 2), (9, 3), (9, 4)]:
            c1.move(*mv)
        for mv in [(8, 0), (8, 1), (8, 2), (8, 3)]:
            c2.move(*mv)

        assert _wait(lambda: any(e.get("type") == "game_over"
                                 for e in events[1]))
        assert _wait(lambda: any(e.get("type") == "game_over"
                                 for e in events[2]))
        assert local.room.result == {"winner": 1, "reason": "five", "moves": 9}
        final2 = next(e for e in events[2] if e["type"] == "game_over")
        assert final2["winner"] == 1 and final2["moves"] == 9
    finally:
        c1.close()
        c2.close()
        local.stop()


def test_ping_pong_event():
    local = LocalRoom(SeatSpec("remote"), SeatSpec("remote"))
    events = []
    c = RoomClient(events.append)
    try:
        c.connect(local.host, local.port, local.name, "black")
        c.ping()
        assert _wait(lambda: any(e.get("type") == "pong" for e in events), 5)
    finally:
        c.close()
        local.stop()


def test_connect_unknown_room_surfaces_error():
    srv = RoomServer("127.0.0.1")
    srv.add_room(Room("demo", SeatSpec("remote"), SeatSpec("remote")))
    port = srv.start(0)
    c = RoomClient()
    try:
        with pytest.raises(WireError) as ei:
            c.connect("127.0.0.1", port, "nope", "black")
        assert ei.value.code == "no_room"
    finally:
        c.close()
        srv.stop()


def test_close_stops_reader_and_rejects_sends():
    local = LocalRoom(SeatSpec("remote"), SeatSpec("remote"))
    c = RoomClient()
    try:
        c.connect(local.host, local.port, local.name, "black")
        c.close()
        assert not (c._reader and c._reader.is_alive())
        with pytest.raises(RuntimeError):
            c.move(9, 9)
    finally:
        local.stop()


def test_local_room_passes_config_through_welcome():
    """LocalRoom 的 config 透传到 Room，随 welcome 下发给每个客户端。"""
    cfg = RoomConfig(allow_undo=False, undo_limit=1, allow_review=False,
                     allow_restart=False, show_ai_scores=False)
    local = LocalRoom(SeatSpec("remote"), SeatSpec("remote"), config=cfg)
    c = RoomClient()
    try:
        rep = c.connect(local.host, local.port, local.name, "black")
        assert rep["config"] == cfg.to_json()
    finally:
        c.close()
        local.stop()


def test_local_ip_is_a_string():
    """local_ip() 在有无网络时都必须给出可显示的地址，不能抛异常。"""
    ip = local_ip()
    assert isinstance(ip, str) and ip


class _ColAI:
    """服务端 AI 替身：黑填第 0 列，白填第 18 列。"""

    def __init__(self, spec):
        pass

    def choose_move(self, board, stone, cancel=None):
        col = 0 if stone == 1 else 18
        for r in range(19):
            if board[r][col] == 0:
                return r, col, {"reason": "stub", "depth": 1, "best_val": 0.0}
        raise RuntimeError("没有可下的位置")


def test_client_undo_with_ai_opponent(monkeypatch):
    import room as room_module
    monkeypatch.setattr(room_module, "AIPlayer", _ColAI)

    local = LocalRoom(SeatSpec("ai", level=1), SeatSpec("remote"))
    events = []
    c = RoomClient(events.append)
    try:
        c.connect(local.host, local.port, local.name, "white")
        assert _wait(lambda: any(e.get("type") == "state"
                                 and e.get("move_no") == 1 for e in events))
        c.undo_request()
        assert _wait(lambda: any(e.get("type") == "undo_applied"
                                 for e in events), 5)
        # AI 重下需要一点时间：等第 1 手重新出现再断言
        assert _wait(lambda: local.room.session.move_count == 1, 5)
        assert local.room.result is None            # 悔棋后对局继续
        assert local.room.session.output == 2
    finally:
        c.close()
        local.stop()

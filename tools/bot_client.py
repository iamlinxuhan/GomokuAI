# -*- coding: utf-8 -*-
"""AI Bot 客户端（M3）：以网络玩家身份接入房间，本机算棋后发送 move。

用法::

    .venv/bin/python tools/bot_client.py --port 48900 --room demo --seat black --level 3

与"服务端 AI 席位"（``server.py --black ai``）的区别：这个进程自己算棋，
只把着法发过去。也正因此，两个不同档位（甚至不同机器）的 AI 可以被放进
同一桌，或者用它来压测房间协议。
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from players import AIPlayer, PlayerSpec  # noqa: E402
from wire import PROTO_VERSION, WireConnection  # noqa: E402

SEAT_STONE = {"black": 1, "white": 2}


def run_bot(host: str, port: int, room: str, seat: str, level: int, *,
            timeout: float = 900.0, verbose: bool = True) -> dict:
    """连上房间下完一局，返回服务端的 ``game_over`` 报文。"""
    my_stone = SEAT_STONE[seat]
    wire = WireConnection.connect(host, port, timeout=5.0)
    try:
        wire.send({"type": "hello", "proto": PROTO_VERSION,
                   "name": "bot-%s" % seat})
        reply = wire.recv(timeout=5)
        if reply.get("type") != "hello_ok":
            raise SystemExit("握手失败：%r" % (reply,))

        wire.send({"type": "join", "room": room, "seat": seat})
        player = AIPlayer(PlayerSpec("ai", level=level))
        board = np.zeros((19, 19), dtype=int)

        while True:
            msg = wire.recv(timeout=timeout)
            t = msg.get("type")
            if t == "welcome":
                board = np.array(msg["board"], dtype=int)
                if verbose:
                    print("[%s] 已加入房间 %s（轮到 %s）"
                          % (seat, msg.get("room"), msg.get("turn")),
                          flush=True)
            elif t == "state":
                board = np.array(msg["board"], dtype=int)
            elif t == "move":
                board[msg["r"]][msg["c"]] = msg["stone"]
            elif t == "turn":
                if msg["stone"] == my_stone:
                    t0 = time.time()
                    r, c, _info = player.choose_move(board, my_stone)
                    wire.send({"type": "move", "r": int(r), "c": int(c)})
                    if verbose:
                        print("[%s] 第 %s 手 -> (%d,%d)  %.2fs"
                              % (seat, msg.get("move_no", "?"), r, c,
                                 time.time() - t0), flush=True)
            elif t == "game_over":
                return msg
            elif t == "error":
                raise SystemExit("服务端错误：%s %s"
                                 % (msg.get("code"), msg.get("message")))
    finally:
        wire.close()


def main():
    ap = argparse.ArgumentParser(description="GomokuAI 网络 Bot 客户端")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--room", default="demo")
    ap.add_argument("--seat", choices=("black", "white"), required=True)
    ap.add_argument("--level", type=int, default=1)
    ap.add_argument("--timeout", type=float, default=900.0)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    result = run_bot(args.host, args.port, args.room, args.seat, args.level,
                     timeout=args.timeout, verbose=not args.quiet)
    print("对局结束：winner=%s reason=%s moves=%s"
          % (result.get("winner"), result.get("reason"), result.get("moves")))
    sys.exit(0)


if __name__ == "__main__":
    main()

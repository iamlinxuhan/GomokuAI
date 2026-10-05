# -*- coding: utf-8 -*-
"""单房间：Session + 席位 + 成员消息路由（M3）。

一局棋一个 Room。席位两种：

* ``SeatSpec("ai", level=N)`` —— 服务端算（AIPlayer）；
* ``SeatSpec("remote")`` —— 由接入的连接驱动（人类客户端或 Bot 客户端）。

线程模型：Room 有一条自己的游戏线程；远程席位从队列取着法，AI 席位直接
算。出站消息发给 ``_members`` 里所有连接（每条连接自带发送锁，整行原子）。
AI 并发的约定见 ``tools/PLAN_SESSION.md`` 第 6 节：**一个进程内串行化**
（``players`` 的全局互斥），真并行靠多进程部署。
"""

from __future__ import annotations

import queue
import threading

from players import AIPlayer, PlayerSpec, reset_engine
from session import Session
from wire import WireError

#: 内部哨兵：状态被悔棋改变（回主循环重新判断轮次）/ 中止。
_RECHECK = object()
_ABORT = object()


class SeatSpec:
    __slots__ = ("kind", "level", "name")

    def __init__(self, kind: str, level: int = 1, name: str = ""):
        if kind not in ("ai", "remote"):
            raise ValueError("席位类型只能是 ai / remote")
        self.kind = kind
        self.level = int(level)
        self.name = name or (("AI-%d" % self.level) if kind == "ai" else "remote")

    def to_json(self):
        return {"kind": self.kind, "level": self.level, "name": self.name}


class Room:
    def __init__(self, name: str, black: SeatSpec, white: SeatSpec):
        self.name = name
        self.specs = {1: black, 2: white}
        self.session = Session()
        self.session.configure("pvp")   # 无人类席位：不记复盘快照
        self.session.reset()
        self.result = None              # {"winner","reason","moves"}
        self.started = False
        self.done = threading.Event()
        self._conns = {1: None, 2: None}     # stone -> WireConnection
        self._members = []                   # [(conn, stone), ...]
        self._queues = {1: queue.Queue(), 2: queue.Queue()}
        self._cv = threading.Condition()
        #: 保护"落子 + 广播"与"悔棋"这两件会改 Session 的事；搜索在快照上跑，
        #: 不持有它。远程席位等待着法时也不持有（见 ``_await_move``）。
        self._game_lock = threading.RLock()
        self._recheck = False
        self._stop = threading.Event()
        self._thread = None

    # ------------------------------------------------------------ 成员

    def attach(self, stone: int, conn) -> None:
        with self._cv:
            if stone not in self.specs:
                raise WireError("bad_seat", "未知席位")
            if self.specs[stone].kind != "remote":
                raise WireError("seat_not_remote", "该席位由服务端 AI 占用")
            if self._conns[stone] is not None:
                raise WireError("seat_taken", "席位已被占用")
            if self.session.move_count > 0 or self.session.game_over:
                raise WireError("already_started", "对局已开始")
            self._conns[stone] = conn
            self._members.append((conn, stone))
            self._cv.notify_all()

    def detach(self, conn) -> None:
        with self._cv:
            self._members = [(c, s) for c, s in self._members if c is not conn]
            for stone, c in self._conns.items():
                if c is conn:
                    self._conns[stone] = None
            aborted = self.started and not self.session.game_over
            self._cv.notify_all()
        if aborted and not self._stop.is_set():
            # 对局中掉线：终止本局（重连 / 续弈留给 M5）
            self._stop.set()

    def seats_json(self):
        return {str(s): self.specs[s].to_json() for s in (1, 2)}

    def state_payload(self):
        return {
            "board": self.session.board.tolist(),
            "move_no": self.session.move_count,
            "turn": 1 if self.session.move_count % 2 == 0 else 2,
            "result": self.session.gamerule,
            "winner": self.session.winner,
        }

    def broadcast(self, obj) -> None:
        for conn, _seat in list(self._members):
            try:
                conn.send(obj)
            except WireError:
                pass

    def send_to(self, stone: int, obj) -> None:
        conn = self._conns.get(stone)
        if conn is not None:
            try:
                conn.send(obj)
            except WireError:
                pass

    def submit_move(self, stone: int, r, c) -> None:
        if stone not in self.specs:
            raise WireError("bad_seat", "未知席位")
        if self.specs[stone].kind != "remote":
            raise WireError("seat_not_remote", "该席位由服务端 AI 占用")
        try:
            self._queues[stone].put((int(r), int(c)))
        except (TypeError, ValueError):
            raise WireError("bad_move", "着法坐标不是整数：%r, %r" % (r, c))

    # ------------------------------------------------------------ 悔棋

    def submit_undo_request(self, stone: int) -> None:
        """悔棋申请。

        对手是服务端 AI → **立即同意**（单机内置房间走这条）；
        对手是远程玩家 → 协商尚未实现（M4d），明确回错而不是静默忽略。
        """
        if stone not in self.specs:
            raise WireError("bad_seat", "未知席位")
        with self._game_lock:
            if self.session.game_over:
                raise WireError("undo_unavailable", "对局已结束")
            if not self.session.can_undo():
                raise WireError("undo_unavailable", "没有可悔的棋")
            if self.specs[3 - stone].kind != "ai":
                raise WireError("undo_needs_consent",
                                "远程对手的悔棋协商尚未实现")
            plan = self._undo_plan(stone)
            if plan is None:
                raise WireError("undo_unavailable", "没有可悔的棋")
            self.session.output -= 1
            self.session.rewind(plan)
            self._recheck = True
            self.broadcast({"type": "undo_applied", "by": stone,
                            "move_no": self.session.move_count})
            self.broadcast({"type": "state", **self.state_payload()})

    def _undo_plan(self, stone: int):
        """本房间的悔棋政策：撤到请求方上一次落子**之前**。

        * 最后一手是请求方自己的 → 撤 1；
        * 否则（对手刚落子）：请求方已经下过 → 撤 2（自己那手 + 对手回应）；
        * 请求方一手未下（对手先手）→ 撤 1，让对手重下。
        """
        count = self.session.move_count
        if count == 0 or self.session.last_move is None:
            return None
        if self.session.last_move[2] == stone:
            return 1
        mine = (count + 1) // 2 if stone == 1 else count // 2
        return 1 if mine == 0 else 2

    # ------------------------------------------------------------ 对局

    def start(self) -> None:
        """启动游戏线程；远程席位未到齐时在游戏线程里等待。"""
        with self._cv:
            if self.started:
                return
            self.started = True
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="room-%s" % self.name)
        self._thread.start()

    def _run(self) -> None:
        self.broadcast({"type": "players", "seats": self.seats_json()})

        with self._cv:
            while not self._stop.is_set() and any(
                    self.specs[s].kind == "remote" and self._conns[s] is None
                    for s in (1, 2)):
                self._cv.wait(0.1)
        if self._stop.is_set():
            self._finish_aborted()
            return

        reset_engine()
        self.broadcast({"type": "state", **self.state_payload()})

        while (not self.session.game_over) and (not self._stop.is_set()):
            stone = 1 if self.session.move_count % 2 == 0 else 2
            self.broadcast({"type": "turn", "stone": stone})
            got = self._await_move(stone)
            if got is _ABORT:
                self._finish_aborted()
                return
            if got is _RECHECK:
                continue
            r, c, info = got
            with self._game_lock:
                if self._recheck:
                    self._recheck = False
                    continue
                if self.session.game_over or self._stop.is_set():
                    break
                current = 1 if self.session.move_count % 2 == 0 else 2
                if current != stone:
                    continue        # 悔棋改掉了轮次：这一手作废
                if self.session.apply_stone(r, c, stone) is None:
                    # 非法着法不打翻整局：明确回错，等这一席重发（人类
                    # 客户端的一次误点不该终结对局）。
                    self.send_to(stone, {"type": "error",
                                         "code": "illegal_move",
                                         "message": "非法走法 (%d,%d)" % (r, c)})
                    continue
                self.broadcast({"type": "move", "r": r, "c": c,
                                "stone": stone,
                                "move_no": self.session.move_count,
                                "info": info})
                self.broadcast({"type": "state", **self.state_payload()})

        if self.session.game_over:
            reason = "draw" if self.session.gamerule == 0 else "five"
            self.result = {"winner": self.session.winner, "reason": reason,
                           "moves": self.session.move_count}
            self.broadcast({"type": "game_over", "winner": self.session.winner,
                            "reason": reason, "moves": self.session.move_count})
        self.done.set()

    def _await_move(self, stone: int):
        """等待/计算当前席位的着法。返回 ``(r, c, info)``、``_RECHECK`` 或
        ``_ABORT``。远程席位等待着法时不持锁；AI 在棋盘快照上搜索。"""
        spec = self.specs[stone]
        if spec.kind == "ai":
            board = self.session.board.copy()
            player = AIPlayer(PlayerSpec("ai", level=spec.level))
            r, c, info = player.choose_move(board, stone)
            with self._game_lock:
                if self._recheck:
                    return _RECHECK
                if self._stop.is_set():
                    return _ABORT
                if self.session.game_over:
                    return _ABORT
                current = 1 if self.session.move_count % 2 == 0 else 2
                if current != stone:
                    return _RECHECK
                return int(r), int(c), info

        while not self._stop.is_set():
            if self._recheck:
                self._recheck = False
                return _RECHECK
            try:
                r, c = self._queues[stone].get(timeout=0.1)
            except queue.Empty:
                continue
            with self._game_lock:
                if self._recheck:
                    self._recheck = False
                    return _RECHECK
                current = 1 if self.session.move_count % 2 == 0 else 2
                if current != stone:
                    return _RECHECK
                return int(r), int(c), None
        return _ABORT

    def _finish_aborted(self) -> None:
        if not self.done.is_set():
            self.broadcast({"type": "error", "code": "aborted",
                            "message": "对局中止"})
            self.done.set()

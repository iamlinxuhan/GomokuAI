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
import time
from dataclasses import dataclass

from players import AIPlayer, PlayerSpec, reset_engine
from session import Session
from wire import WireError

#: 内部哨兵：状态被悔棋改变（回主循环重新判断轮次）/ 中止。
_RECHECK = object()
_ABORT = object()


@dataclass
class RoomConfig:
    """房间功能开关（M4d）。默认值 = 今天的单机行为，一个不多一个不少。

    随 ``welcome`` 下发，客户端据此禁用入口（悔棋按钮、复盘、重开、
    评分曲线）。**服务端该守的仍然守**：``allow_undo`` 与 ``undo_limit``
    在 :meth:`Room.submit_undo_request` 里权威校验，UI 的按钮只是第一道
    —— 客户端可以改自己的界面，但改不了别人的房间。
    """

    allow_undo: bool = True
    undo_limit: int = 3
    allow_review: bool = True
    allow_restart: bool = True
    show_ai_scores: bool = True

    def to_json(self) -> dict:
        return {
            "allow_undo": self.allow_undo,
            "undo_limit": self.undo_limit,
            "allow_review": self.allow_review,
            "allow_restart": self.allow_restart,
            "show_ai_scores": self.show_ai_scores,
        }


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
    def __init__(self, name: str, black: SeatSpec, white: SeatSpec,
                 *, auto_consent: bool = False, config: RoomConfig | None = None):
        self.name = name
        self.specs = {1: black, 2: white}
        self.session = Session()
        self.session.configure("pvp")   # 无人类席位：不记复盘快照
        self.session.reset()
        self.config = config if config is not None else RoomConfig()
        self.result = None              # {"winner","reason","moves"}
        self.started = False
        self.done = threading.Event()
        #: 远程对手的悔棋是否自动同意。应用内房间（``LocalRoom``）为 True：
        #: 两个客户端都在同一进程里，沿用单机"悔棋直接生效"的旧行为；
        #: 专用服务器的远程对手保持 False，走 M4d 的协商流程；LAN 房间
        #: （对手在另一台机器上）也传 False。
        self.auto_consent = bool(auto_consent)
        #: 待应答的悔棋提案 ``{"by": stone, "plan": n}``；非 None 期间
        #: :meth:`_await_move` 冻结对局（见那里的注释）。只许在 ``_game_lock``
        #: 内读写。
        self._pending_undo = None
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

    def free_remote_seats(self) -> list:
        """还没人坐的 ``remote`` 席位（黑先顺序）。

        给 ``seat="auto"`` 的加入者用：房主已经定了颜色，加入方不该再选
        —— 服务端把剩下那一席给他，两个人不可能选重。
        """
        with self._cv:
            return [s for s in (1, 2)
                    if self.specs[s].kind == "remote" and self._conns[s] is None]

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

        对手是服务端 AI → **立即同意**（单机内置房间走这条）；对手是同一
        进程内的本地房间成员（``auto_consent``）→ 同样立即同意（本地双人
        沿用单机旧行为）；其余远程对手 → 进入 M4d 的协商流程：存下方案并
        广播 ``undo_proposed``，等对手调用 :meth:`submit_undo_response`。

        开关（``config.allow_undo``）与额度（``config.undo_limit``）由房间
        权威校验；已有 pending 时明确回 ``undo_pending``，不覆盖已存方案。
        """
        if stone not in self.specs:
            raise WireError("bad_seat", "未知席位")
        with self._game_lock:
            if not self.config.allow_undo:
                raise WireError("undo_disabled", "这个房间禁用了悔棋")
            if self.session.game_over:
                raise WireError("undo_unavailable", "对局已结束")
            # can_undo 把"额度用尽"和"没有可悔的棋"合并成一条；对客户端
            # 都表现为"悔不了"，沿用同一个错误码。
            if not self.session.can_undo():
                raise WireError("undo_unavailable", "没有可悔的棋")
            if self._pending_undo is not None:
                raise WireError("undo_pending", "已有悔棋申请等待对手应答")
            plan = self._undo_plan(stone)
            if plan is None:
                raise WireError("undo_unavailable", "没有可悔的棋")
            if self.specs[3 - stone].kind == "ai" or self.auto_consent:
                self._apply_undo(stone, plan)
                return
            # 远程对手：存下方案、冻结对局，等应答。方案不会过时 ——
            # pending 期间 _await_move 不落子（否则"撤几步"当场失效）。
            self._pending_undo = {"by": stone, "plan": plan}
        self.broadcast({"type": "undo_proposed", "by": stone})

    def submit_undo_response(self, stone: int, accept: bool) -> None:
        """对 ``undo_proposed`` 的应答（M4d）。

        只有提案的**对手**能应答（``stone`` 是应答者席位）。同意就用申请
        时存下的方案执行；拒绝只广播结果、棋盘不动。两种结果都清 pending
        并唤醒主循环 —— 主循环在 :meth:`_await_move` 里轮询等待它。
        """
        with self._game_lock:
            pending = self._pending_undo
            if pending is None:
                raise WireError("no_pending_undo", "当前没有待应答的悔棋申请")
            if stone != 3 - pending["by"]:
                raise WireError("bad_seat", "只有对手可以应答悔棋申请")
            self._pending_undo = None
            if accept:
                self._apply_undo(pending["by"], pending["plan"])
        if not accept:
            self.broadcast({"type": "undo_rejected", "by": pending["by"]})
        with self._cv:
            self._cv.notify_all()

    def _apply_undo(self, stone: int, plan: int) -> None:
        """执行一次已批准的悔棋。**调用方必须持有 ``_game_lock``**：
        与主循环的"落子 + 广播"串行，避免撤到一半时有人落子。"""
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

        最后一手的颜色由 **move_count 奇偶**推出（黑先交替），不读
        ``session.last_move`` —— 后者在 ``rewind`` 之后会被清空（那是给 UI
        清最后一手环用的），用它判断会让"悔棋后立刻再悔一次"被误判成
        "没有可悔的棋"（M4c 的连续悔棋用例正好走到这条路径）。
        """
        count = self.session.move_count
        if count == 0:
            return None
        last_stone = 1 if count % 2 == 1 else 2
        if last_stone == stone:
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
        # 悔棋额度以 config 为准（session.reset() 里的 3 只是默认值）。
        # 放在这里而不是 __init__：等所有席位就位后再定，客户端拿到的
        # welcome / state 与真实额度一致。
        self.session.output = int(self.config.undo_limit)
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
                if self._pending_undo is not None:
                    # 极窄的窗口：提案恰好在 `_await_move` 返回之后到达。
                    # 不落子；远程着法**退回队列**而不是丢弃 —— 丢掉的话
                    # 客户端会停在"在途"状态，等不到回显。
                    if self.specs[stone].kind == "remote":
                        self._queues[stone].put((r, c))
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
        ``_ABORT``。远程席位等待着法时不持锁；AI 在棋盘快照上搜索。

        M4d：悔棋协商 pending 期间**冻结对局** —— 两个分支在开新工作前
        都等 ``_pending_undo`` 清空。不这么做的话，双方会继续落子，而
        申请时按当时手数算好的悔棋方案（撤几步）在应答到达时就失效了。
        """
        spec = self.specs[stone]
        if spec.kind == "ai":
            while not self._stop.is_set():
                if self._pending_undo is not None:
                    time.sleep(0.1)
                    continue
                # 快照必须在协商结束后重取：同意的悔棋会改棋盘。
                board = self.session.board.copy()
                player = AIPlayer(PlayerSpec("ai", level=spec.level))
                # 把房间的中止信号作为**协作取消**传下去（与旧 AIWorker 同一条
                # 协议）：重开/退出时连接断开 → detach → `_stop` 置位 → 搜索在
                # 下一次节点轮询就退出，而不是跑满时间预算后还占着进程内的 AI
                # 互斥锁，让同进程的下一局 reset_engine 一直等它。
                r, c, info = player.choose_move(board, stone, cancel=self._stop)
                with self._game_lock:
                    if self._recheck:
                        return _RECHECK
                    if self._stop.is_set():
                        return _ABORT
                    if self.session.game_over:
                        return _ABORT
                    if self._pending_undo is not None:
                        # 提案在搜索期间到达：这一手基于旧局面，作废重算
                        # （释放 _game_lock 后回到循环顶等待协商结果）。
                        continue
                    current = 1 if self.session.move_count % 2 == 0 else 2
                    if current != stone:
                        return _RECHECK
                    return int(r), int(c), info
            return _ABORT

        held = None     # pending 期间先攥住、协商结束后再校验的着法
        while not self._stop.is_set():
            if held is None:
                if self._pending_undo is not None:
                    time.sleep(0.1)
                    continue
                if self._recheck:
                    self._recheck = False
                    return _RECHECK
                try:
                    held = self._queues[stone].get(timeout=0.1)
                except queue.Empty:
                    continue
            if self._pending_undo is not None:
                # 协商中：手头这一手先攥住（不能丢，也不能落）。
                time.sleep(0.1)
                continue
            with self._game_lock:
                if self._pending_undo is not None:
                    continue        # 释放锁，回循环顶等协商结束
                if self._recheck:
                    self._recheck = False
                    return _RECHECK
                current = 1 if self.session.move_count % 2 == 0 else 2
                if current != stone:
                    return _RECHECK
                return int(held[0]), int(held[1]), None
        return _ABORT

    def _finish_aborted(self) -> None:
        if not self.done.is_set():
            self.broadcast({"type": "error", "code": "aborted",
                            "message": "对局中止"})
            self.done.set()

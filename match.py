# -*- coding: utf-8 -*-
"""无头对局驱动（M2）。

把 Session（规则）与 Player（谁在下棋）接起来跑完一局，零 Qt。UI 的
本地对局在 M4 之前仍由 ``main.py`` 驱动；本模块服务 AI-AI 基准，M3 起
服务器端的 Room 也会复用它。

与 ``main.py`` 的关系：规则与状态全部走 ``Session``，不存在第二套判定；
区别只在"谁来出招"——这里是同步的 ``choose_move``，UI 那边是人类点击与
``AIWorker`` 线程。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from session import BOARD_SIZE, Session


@dataclass
class MatchResult:
    winner: int                              # 1/2；0 = 平局/未决出/作废
    moves: int
    elapsed_s: float
    record: list = field(default_factory=list)   # [(r, c, stone, info), ...]
    error: str = ""                          # 非空 = 这局作废（非法/异常）


class Match:
    """黑先、双方交替，直到终局或触到步数上限。

    ``start_moves`` 是预置开局前缀 ``[(r, c, stone), ...]``。引擎是确定性
    的，基准要跑多局就必须制造开局差异；前缀把"开局库已覆盖的前几手"钉成
    一组标准变化，其余交给玩家搜索（与 ``tools/selfplay.py`` 的
    ``OPENING_SECOND_MOVES`` 同一思路）。
    """

    def __init__(self, black, white, *, start_moves=(), cancel=None,
                 max_moves: int = BOARD_SIZE * BOARD_SIZE):
        self.black = black
        self.white = white
        self.cancel = cancel
        self.max_moves = int(max_moves)
        self.session = Session()
        # 无人类席位：不记复盘快照、赢了也不区分"你/对方"——但 winner 仍然
        # 给出子色。模式语义只在 UI 层有差别，规则层公用。
        self.session.configure("pvp")
        self.session.reset()
        for r, c, stone in start_moves:
            if self.session.apply_stone(r, c, int(stone)) is None:
                raise ValueError("预置开局与现有棋子冲突: (%d,%d)" % (r, c))
        self.record = []

    def run(self) -> MatchResult:
        t0 = time.monotonic()
        while (not self.session.game_over
               and self.session.move_count < self.max_moves):
            if self.cancel is not None and self.cancel.is_set():
                return self._result(0, t0, "已取消")

            stone = 1 if self.session.move_count % 2 == 0 else 2
            player = self.black if stone == 1 else self.white
            try:
                r, c, info = player.choose_move(self.session.board, stone,
                                                self.cancel)
            except Exception as exc:                     # noqa: BLE001
                return self._result(0, t0, "%s: %s" % (type(exc).__name__, exc))

            res = self.session.apply_stone(r, c, stone)
            if res is None:
                return self._result(0, t0, "非法走法 (%r, %r)" % (r, c))
            self.record.append((r, c, stone, info))

        # 触及步数上限且未终局：按平局记（不臆造胜方）。
        winner = self.session.winner if self.session.game_over else 0
        return self._result(winner, t0, "")

    def _result(self, winner: int, t0: float, error: str) -> MatchResult:
        return MatchResult(winner, self.session.move_count,
                           time.monotonic() - t0, self.record, error)

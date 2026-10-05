# -*- coding: utf-8 -*-
"""对局会话：权威状态机（零 Qt、零 socket）。

M1 从 ``main.py`` 抽出的"棋局状态 + 规则"：棋盘、走子历史、判胜/判和、
悔棋政策、复盘快照。UI 只负责渲染与输入，Session 负责"这局棋现在是什么
状态"。

当前形态与 ``main.py`` 原有语义**逐字对应**（mode "ai"/"pvp"、
human_stone/ai_stone、悔棋上限 3、human_moves 快照）。行为契约见
``tools/PLAN_SESSION.md`` 第 4 节与 ``tests/test_game_flow.py``；本模块
自己的规则单测在 ``tests/test_session.py``。

后续里程碑：M2 把 mode/石色升级为 Player 席位，M3+ 由 Room 持有 Session。
所以本文件现在刻意**不大改语义** —— 纯抽取，不顺手"修"任何现状（包括
``tools/PLAN_SESSION.md`` 标为疑似缺陷的 C11）。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from engine_local import BOARD_SIZE, check_win, win_line

#: 悔棋次数上限。原 ``main.py`` 的 ``self.output = 3``。
UNDO_LIMIT = 3


@dataclass
class MoveResult:
    """一次落子的结果。``outcome`` ∈ {"continue", "win", "draw"}。"""

    stone: int
    outcome: str
    winner: int = 0
    line: tuple = ()


@dataclass
class UndoResult:
    removed: int
    #: AI 先手只走了天元：撤掉后需要立刻重下。这是旧 UI 政策（``undo()``）
    #: 的产物；M4c 起主流程走房间，重下由 ``room.py`` 的 ``_recheck`` 触发，
    #: 本字段只留给 ``Session.undo()`` 的调用方（测试）。
    replay_opening: bool = False


class Session:
    """一局棋的权威状态。

    ``mode``：

    * ``"ai"``  —— 人机：``human_stone`` / ``ai_stone`` 固定，玩家按自己的
      石色落子（旧 ``playmode=0``）；
    * ``"pvp"`` —— 本地双人：按手数奇偶换色（旧 ``playmode=1``）。
    """

    def __init__(self) -> None:
        self.mode = "pvp"
        self.human_stone = 0
        self.ai_stone = 0
        self.reset()

    # ------------------------------------------------------------ 生命周期

    def configure(self, mode: str, human_stone: int = 0,
                  ai_stone: int = 0) -> None:
        self.mode = mode
        self.human_stone = int(human_stone)
        self.ai_stone = int(ai_stone)

    def reset(self) -> None:
        self.board = np.zeros((BOARD_SIZE, BOARD_SIZE), dtype=int)
        self.move_history = []      # 每步落子后的棋盘快照
        self.move_count = 0
        self.last_move = None
        self.game_over = False
        #: 1=AI 赢、2=人（或本地对战里的胜方）赢、3=进行中、0=平局。
        #: 人机与本地双人的下游文案不同，但都靠 winner 辨认胜方。
        self.gamerule = 3
        self.winner = 0
        self.output = UNDO_LIMIT    # 剩余悔棋次数
        #: 玩家（执黑或执白的那一方）每一步落子**之前**的局面快照，
        #: ``[{'seq', 'r', 'c', 'before'}, ...]``。战后复盘只分析这些局面；
        #: 只有人机模式记录（本地双人没有"复盘对象"）。
        self.human_moves = []
        self.opening_done = False

    # ------------------------------------------------------------ 落子

    def apply_stone(self, r: int, c: int, stone: int):
        """通用落子入口：坐席与回合由调用方决定（无头对局 / 服务器用）。

        校验只有两条：终局、已占格。返回 ``MoveResult``；非法返回 ``None``。
        人类/AI 的既有政策入口在它上面实现，因此规则只有这一份。
        """
        if self.game_over or self.board[r][c] != 0 or not stone:
            return None
        return self._place(r, c, int(stone))

    def apply_human_move(self, r: int, c: int):
        """玩家落子。返回 ``MoveResult``；非法时返回 ``None``（防御性）。"""
        if self.game_over or self.board[r][c] != 0:
            return None
        if self.mode == "ai":
            self._record_player_snapshot(r, c)
            stone = self.human_stone
        else:
            # 本地对战：黑先，按手数奇偶换色。
            stone = 1 if self.move_count % 2 == 0 else 2
        return self.apply_stone(r, c, stone)

    def apply_ai_move(self, r: int, c: int):
        if not self.ai_stone:
            return None
        return self.apply_stone(r, c, self.ai_stone)

    def apply_opening_move(self, r: int, c: int):
        """AI 先手的第一着（天元，由 ``engine.opening_move`` 决定）。"""
        if self.game_over or self.board[r][c] != 0:
            return None
        self.opening_done = True
        return self.apply_stone(r, c, 1)

    def _record_player_snapshot(self, r: int, c: int) -> None:
        self.human_moves.append({
            "seq": self.move_count + 1,
            "r": r, "c": c,
            "before": self.board.copy(),
        })

    def _place(self, r: int, c: int, stone: int) -> MoveResult:
        self.board[r][c] = stone
        self.last_move = (r, c, stone)
        self.move_count += 1
        self.move_history.append(self.board.copy())

        if check_win(self.board, stone):
            self.game_over = True
            self.winner = stone
            if self.mode == "ai":
                self.gamerule = 2 if stone == self.human_stone else 1
            else:
                self.gamerule = 2
            return MoveResult(stone, "win", stone,
                              tuple(win_line(self.board, stone)))

        if self.move_count >= BOARD_SIZE * BOARD_SIZE:
            self.game_over = True
            self.gamerule = 0
            self.winner = 0
            return MoveResult(stone, "draw")

        return MoveResult(stone, "continue")

    # ------------------------------------------------------------ 悔棋

    def can_undo(self) -> bool:
        return (not self.game_over) and self.output > 0 and self.move_count > 0

    def rewind(self, n: int) -> UndoResult:
        """通用回退：弹 ``n`` 步（棋盘 / 历史 / 复盘记录），**不涉及政策与预算**。

        UI 的悔棋政策（撤几步、AI 先手特例）在 ``undo()`` 里；房间的政策在
        ``room.py`` 里 —— 两边都必须经过这里，规则只有一份。
        """
        result = UndoResult(self._pop(n))
        self.human_moves = [m for m in self.human_moves
                            if m["seq"] <= self.move_count]
        self.last_move = None
        return result

    def undo(self):
        """按现有 UI 政策悔棋，返回 ``UndoResult``；不可悔时返回 ``None``。

        政策（与 ``main.py`` 逐字对应）：

        * 本地双人：撤 1 手；
        * 人机且已有 ≥2 手：撤 2 手（玩家 + AI 回应）；
        * 人机、AI 先手、且只有天元这一手：撤 1 手并要求重下天元；
        * 其余：撤 1 手。
        """
        if not self.can_undo():
            return None
        self.output -= 1

        if self.mode == "pvp":
            return self.rewind(1)
        if self.move_count >= 2:
            return self.rewind(2)
        if self.mode == "ai" and self.ai_stone == 1:
            # AI 先手、盘上只有它这一手：撤掉后要求重下天元。
            self.opening_done = False
            result = self.rewind(1)
            result.replay_opening = True
            return result
        return self.rewind(1)

    def _pop(self, n: int) -> int:
        removed = 0
        for _ in range(n):
            if self.move_history:
                self.move_history.pop()
                removed += 1
        self.move_count -= removed
        self.board = (self.move_history[-1].copy() if self.move_history
                      else np.zeros((BOARD_SIZE, BOARD_SIZE), dtype=int))
        return removed

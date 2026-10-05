# -*- coding: utf-8 -*-
"""玩家抽象（M2）。

把"谁在下棋"从 UI 里抽出来：人类与 AI 都是玩家。本模块现在服务两件事：

* 无头对局（``match.Match`` / ``tools/arena.py``）的 AI 玩家；
* 为 M4 的房间/席位模型定接口形状（远程人类、网络 Bot 随后加）。

约定：``choose_move(board, stone, cancel=None) -> (r, c, info)``。
``board`` 是 19×19 numpy 棋盘快照，玩家**不得原地修改**（无头对局复用
同一块盘，改它等于破坏对局记录）。
"""

from __future__ import annotations

from dataclasses import dataclass

import engine


@dataclass(frozen=True)
class PlayerSpec:
    """席位的可序列化描述（M3 会随协议下发）。

    ``kind``：``"human"`` | ``"ai"``；``level`` 仅对 AI 有意义（难度档位
    1..5，与 ``engine.DIFFICULTY`` 同源）。``name`` 是显示名，空则由工厂
    生成（如 ``AI-3``）。
    """

    kind: str = "human"
    name: str = ""
    level: int = 1


def make_player(spec: PlayerSpec):
    """按描述创建进程内玩家。人类玩家由 UI 驱动，这里不提供。"""
    if spec.kind == "ai":
        return AIPlayer(spec)
    raise ValueError("本地人类玩家由 UI 驱动；无头对局只支持 AI 席位")


class AIPlayer:
    """进程内 AI 玩家：调用 ``engine.ai_move``。

    开局库、C++/本地路由与静默降级、协作式取消全部复用同一条既有路径 ——
    这一层只负责"以玩家的身份调用它"。
    """

    def __init__(self, spec: PlayerSpec) -> None:
        self.spec = spec
        self.name = spec.name or f"AI-{spec.level}"

    def choose_move(self, board, stone, cancel=None):
        return engine.ai_move(board, stone, self.spec.level, cancel=cancel)


class ScriptedPlayer:
    """按预置着法出招的玩家（测试 / 复现用）。着法用尽即抛错。"""

    def __init__(self, moves, name: str = "scripted") -> None:
        self._moves = [(int(r), int(c)) for r, c in moves]
        self.name = name

    def choose_move(self, board, stone, cancel=None):
        if not self._moves:
            raise RuntimeError("ScriptedPlayer 的着法用完了")
        r, c = self._moves.pop(0)
        return r, c, {"reason": "脚本", "best_val": 0.0, "depth": 0}

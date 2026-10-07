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

import threading
from dataclasses import dataclass

import engine
import engine_local

#: 进程内 AI 互斥（见 tools/PLAN_SESSION.md 第 6 节）。
#:
#: ``engine`` 的客户端与 ``engine_local`` 的单例（置换表 / history / killer）
#: 都不是线程安全的，也不是按实例隔离的；多房间若同时搜索会串包、互相污染。
#: M3 的约定是"一个进程内 AI 搜索串行化"，并行基准仍旧靠多进程
#: （与 tools/ab_enhance.py 的做法一致）。
_AI_MUTEX = threading.RLock()


def reset_engine() -> None:
    """清空引擎的跨局状态。与 AI 出招共用同一把锁，二者不能交叠。"""
    with _AI_MUTEX:
        engine.new_game()


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
    book: bool = True
    local: bool = False


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
        with _AI_MUTEX:
            if not self.spec.local:
                return engine.ai_move(
                    board, stone, self.spec.level, cancel=cancel)
            # A/B experiments use the same local search implementation on both
            # sides; only the opening-book switch differs.
            if self.spec.book:
                hit = engine_local.book_lookup(board, stone)
                if hit is not None:
                    book_r, book_c = divmod(hit[0], engine_local.BOARD_SIZE)
                    search_r, search_c, _ = engine_local.ai_move(
                        board, stone, self.spec.level, cancel=cancel,
                        use_book=False)
                    r, c, info = engine_local.ai_move(
                        board, stone, self.spec.level, cancel=cancel,
                        use_book=True)
                    info["book_move"] = [int(book_r), int(book_c)]
                    info["search_move"] = [int(search_r), int(search_c)]
                    info["book_move_same_as_search"] = (
                        (int(r), int(c)) == (int(search_r), int(search_c)))
                    return r, c, info
            return engine_local.ai_move(
                board, stone, self.spec.level, cancel=cancel,
                use_book=self.spec.book)


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

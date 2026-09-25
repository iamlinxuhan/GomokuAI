# -*- coding: utf-8 -*-
"""对局日志记录器。

自 `main.py` 的原 `GameLogger` 逐字迁出，**行为与输出格式完全不变** ——
`game_log_*.txt` 的排版是诊断 AI 决策的主要依据，也是 `tools/positions.py`
中历史局面题库的来源，格式一旦变动那些局面就无法与旧日志对照。

迁出动机：`engine.py` 与 `main.py` 都需要坐标格式化，而 `engine.py` 不能
反向 import UI 层，故把这一小块放入独立的、零依赖的模块。

依赖：仅标准库 `os` / `time`。
"""

from __future__ import annotations

import os
import time

BOARD_SIZE = 19

# 列名字母表：跳 I（与棋谱惯例一致）。**全应用唯一定义** —— 棋盘上的坐标标注
# 必须调 `col_letter()`，不要再写 `chr(65 + i)`。历史上棋盘用的是不跳 I 的
# 版本，于是 19 列里有 11 列（I 之后的全部）用户从棋盘上读到的坐标与日志／
# 棋谱／题库对不上。
COL_LETTERS = tuple(ch for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if ch != "I")


def col_letter(c: int) -> str:
    """第 ``c`` 列（0-based）的棋谱字母。

    **反解（坐标 → 列号）不能写 ``ord(ch) - ord('A')``。** 那个式子凭空造出
    一个 'I' 列（'J' 会算成 9 而不是 8），于是从 I 往右的所有列全部错一位，
    而且错得很安静 —— 解析出来仍是一个合法列号，只是指向棋盘上另一路。
    这个坑真实踩过：用错的映射去核对一份争议日志，推出过"终局没有五连却判
    胜负"的伪矛盾。

    正确写法是查表，且**必须用同一张表**：

        _COL_INDEX = {ch: i for i, ch in enumerate(COL_LETTERS[:BOARD_SIZE])}

    ``tools/positions.py`` 的 ``sgf_to_coord`` 是同一件事的另一种写法
    （``i - 1 if i >= 9 else i``），结果一致。新增反解请复用它或上面这张表，
    不要就地推导。
    """
    return COL_LETTERS[c]


class GameLogger:
    """
    单局游戏日志记录器 — 将每步操作写入 txt 文件便于诊断AI决策。

    记录内容：
      - 每步落子：步数、执棋方、坐标(如H8)、决策原因、评分/搜索深度
      - 威胁检测命中详情
      - AI搜索参数(target_depth, 实际搜到层数, best_val)
      - 最终胜负结果

    用法：
      logger = GameLogger()           # 创建（自动生成带时间戳的文件名）
      logger.log_human(step, r, c)    # 记录人类落子
      logger.log_ai(step, r, c, info) # 记录AI落子+决策元信息
      logger.log_result(winner)       # 记录结果
      logger.close()                  # 关闭文件
    """

    def __init__(self, log_dir=None):
        if log_dir is None:
            log_dir = os.path.dirname(os.path.abspath(__file__))
        os.makedirs(log_dir, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        self.filepath = os.path.join(log_dir, f"game_log_{ts}.txt")
        self.f = open(self.filepath, 'w', encoding='utf-8')
        self._write_header()

    def _write_header(self):
        self.f.write("=" * 70 + "\n")
        self.f.write("  五子棋AI 对局日志\n")
        self.f.write(f"  生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        self.f.write(f"  日志文件: {self.filepath}\n")
        self.f.write("=" * 70 + "\n\n")
        self.f.write(f"{'步骤':>4} | {'执棋':>4} | {'坐标':>5} | {'决策原因':>20} | {'评分/信息':>25}\n")
        self.f.write("-" * 75 + "\n")
        self.f.flush()

    @staticmethod
    def coord_to_sgf(r, c):
        """行列号转棋谱坐标 (如 row=7,col=7 -> H8)"""
        return f"{col_letter(c)}{r + 1}"

    def log_move(self, step, player, r, c, reason="", detail=""):
        player_name = "黑*" if player == 1 else "白O"
        coord = self.coord_to_sgf(r, c)
        self.f.write(
            f"{step:>4} | {player_name:>4} | {coord:>5} | {reason:>20} | {detail}\n"
        )
        self.f.flush()

    def log_human(self, step, player, r, c, detail=""):
        self.log_move(step, player, r, c, "人类手动", detail)

    def log_ai(self, step, player, r, c, decision_info):
        """
        AI落子 - decision_info 是字典:
          'reason'/'depth'/'actual_depth'/'best_val'/'time_ms'/'threat_detail'/'top_moves'
        """
        reason = decision_info.get('reason', '未知')
        dp = []
        if 'best_val' in decision_info:
            dp.append(f"val={decision_info['best_val']:.0f}")
        if 'actual_depth' in decision_info:
            dp.append(f"dep={decision_info['actual_depth']}")
        if 'time_ms' in decision_info:
            dp.append(f"{decision_info['time_ms']:.0f}ms")
        if 'threat_detail' in decision_info:
            dp.append(f"[{decision_info['threat_detail']}]")
        detail = ", ".join(dp) if dp else ""
        self.log_move(step, player, r, c, reason, detail)

        if 'top_moves' in decision_info:
            self.f.write(f"     候选走法: {decision_info['top_moves']}\n")
            self.f.flush()

    def log_threat_analysis(self, step, text):
        self.f.write(f"     [威胁分析@{step}] {text}\n")
        self.f.flush()

    def log_board_state(self, step, board, note=""):
        self.f.write(f"\n  --- 棋盘状态 @{step} {note} ---\n")
        stones = []
        for r in range(BOARD_SIZE):
            for c in range(BOARD_SIZE):
                if board[r][c] != 0:
                    p = "*" if board[r][c] == 1 else "O"
                    stones.append(f"{p}{self.coord_to_sgf(r,c)}")
        self.f.write(f"  棋子({len(stones)}): {' '.join(stones)}\n\n")
        self.f.flush()

    def log_result(self, winner, total_steps, move_count):
        self.f.write("\n" + "-" * 75 + "\n")
        if winner == "ai":
            result = "[AI 获胜]"
        elif winner == "human":
            result = "[人类获胜]"
        else:
            result = "[平局]"
        self.f.write(f"  结果: {result}  |  总回合: {total_steps}  |  总落子: {move_count}\n")
        self.f.write("=" * 70 + "\n")

    def close(self):
        if hasattr(self, 'f') and self.f and not self.f.closed:
            try:
                self.f.close()
            except Exception:
                pass

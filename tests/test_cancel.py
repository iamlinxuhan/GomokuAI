# -*- coding: utf-8 -*-
"""中途取消：**硬时间上限与协作式取消必须真的生效。**

这两件事在旧引擎里都是"参数存在但没人读"：`ai_move(..., cancel=...)` 收下
一个 `threading.Event` 就再也没碰过它，主界面点"重开"时只能等 `_cancel_ai`
的三秒余量走完。用户看到的现象是"点了重开，界面卡住三秒"，而代码里
完全找不到"谁在等"。

取消不是可有可无的礼貌，它有一个**正确性**要求：取消必须发生在**着法边界
之外**。搜索是"做一步 / 递归 / 撤销"的循环，若在循环中间退出而没撤销，
棋盘就带着一颗幽灵子回到调用者手里 —— 下一次搜索会在错误的局面上进行，
而且越走越错。所以这里除了测"多快返回"，还要测"棋盘有没有被弄脏"。
"""

from __future__ import annotations

import threading
import time

import numpy as np

import engine_local as E


def midgame_board():
    """一个正常的开局盘面 —— 分支够多，不会因为立刻找到杀棋而秒退。"""
    m = np.zeros((19, 19), dtype=np.uint8)
    for r, c, p in ((9, 9, 1), (9, 10, 2), (10, 8, 1), (8, 10, 2),
                    (10, 10, 1), (8, 9, 2), (7, 9, 1), (11, 11, 2)):
        m[r][c] = p
    return m


CANCEL_DEADLINE_MS = 100.0


def test_cancel_returns_promptly(new_engine):
    """`cancel` 置位后必须**立刻**返回，而不是等完整个时间预算。

    测的是"从置位到返回"的延迟，不是总耗时 —— 3 档的预算是 15 秒，
    若取消不起作用，这个用例会跑满 15 秒而不是 100 毫秒。
    """
    board = midgame_board()
    cancel = threading.Event()
    box = {}

    def run():
        t0 = time.monotonic()
        box["out"] = new_engine.ai_move(board.copy(), 2, 3, cancel=cancel)
        box["dt"] = (time.monotonic() - t0) * 1000.0

    new_engine.new_game()
    th = threading.Thread(target=run)
    th.start()
    time.sleep(0.05)                     # 让搜索真的进到中局深度
    t_set = time.monotonic()
    cancel.set()
    th.join(timeout=5.0)
    lat = (time.monotonic() - t_set) * 1000.0

    assert not th.is_alive(), "cancel 置位后搜索没有在 5 秒内退出"
    assert lat <= CANCEL_DEADLINE_MS, f"取消延迟 {lat:.1f}ms，超出 {CANCEL_DEADLINE_MS}ms"

    r, c, info = box["out"]
    assert 0 <= r < 19 and 0 <= c < 19, f"取消后返回了非法着法 ({r},{c})"
    # 不只是"比 15 秒短" —— 那样的话跑满 14 秒也能过。这里要求它停在
    # 置位时刻附近，即取消**取代**了时间预算，而不是刚好没超。
    assert box["dt"] < 500.0, f"取消没有取代时间预算：总耗时 {box['dt']:.0f}ms"


def test_cancel_before_start_is_still_legal(new_engine):
    """进搜索之前就把 cancel 置位 —— 仍然要给出合法着法。

    主界面"重开"有可能正好落在两次搜索之间，此时不该抛异常、不该返回
    空着法。返回什么都没关系，只要求坐标合法。
    """
    board = midgame_board()
    cancel = threading.Event()
    cancel.set()

    new_engine.new_game()
    r, c, _ = new_engine.ai_move(board.copy(), 2, 3, cancel=cancel)
    assert 0 <= r < 19 and 0 <= c < 19


def test_cancel_stress_leaves_no_ghost_stones(new_engine):
    """20 轮"搜索中途取消"：不崩、不脏盘。

    一半轮次在**搜索进行中**取消（这才是"撤销写漏了"会暴露的路径：中断
    发生在 `make` 与 `unmake` 之间），另一半在**搜索开始前**就置位（重开
    正好落在两次搜索之间的路径）。

    "不脏盘"的判据有两层：

    * 传进去的 ndarray **一位都不能变**（`think` 的文档承诺"不会被修改"）；
    * 每轮用**同一盘面**，结果必须逐字段一致 —— 若上一轮中途退出时留下了
      未撤销的子，第二轮的局面就不同了，分值/深度会漂。
    """
    board = midgame_board()
    before = board.copy()
    outs = []

    for i in range(20):
        cancel = threading.Event()
        new_engine.new_game()
        box = {}

        def run():
            box["out"] = new_engine.ai_move(board, 2, 3, cancel=cancel)

        th = threading.Thread(target=run)
        th.start()
        if i % 2:
            time.sleep(0.01)             # 进到递归里再取消
        cancel.set()
        th.join(timeout=5.0)
        assert not th.is_alive(), f"第 {i + 1} 轮没有退出"

        r, c, info = box["out"]
        assert 0 <= r < 19 and 0 <= c < 19, f"第 {i + 1} 轮返回非法着法 ({r},{c})"
        assert (board == before).all(), f"第 {i + 1} 轮把调用者的棋盘改了"
        outs.append((r, c, info["best_val"], info["actual_depth"]))

    assert len(set(outs)) == 1, f"重复同一局面得到不同结果：{set(outs)}"


def test_new_game_cancels_nothing_but_clears_state(new_engine):
    """`new_game()` 必须能在**没有搜索在跑**的时候安全调用（切页/重开路径）。

    这条挡的是"重开时正在搜"之外的另一种路径：`_on_restart` 在搜索已经
    结束（或从未开始）时也会调 `new_game()`。此时不得抛异常，且状态确实
    被清空。
    """
    eng = new_engine._ENGINE
    new_engine.new_game()
    assert eng.tt == {} or len(eng.tt) == 0
    new_engine.ai_move(midgame_board(), 2, 1)
    new_engine.new_game()
    assert len(eng.tt) == 0
    assert engine_state_clean(eng)


def engine_state_clean(eng):
    return (not any(any(h) for h in eng.history)
            and all(k[0] == -1 and k[1] == -1 for k in eng.killers))

# -*- coding: utf-8 -*-
"""逐候选分析（战后复盘的引擎侧）。

复盘的整个可信度都压在一条上：**交出去的那张候选表必须是完整的**。它不完整
时不会报错 —— 只是"有些手没被列出来"，于是玩家的一步坏棋查不到、被当成最优，
或者相反。所以这里的主用例不是"能算出分"，而是"表里的候选数与
`Board.candidates()` 一模一样"。

第二条是"收集不改变结论"：`collect=True` 只是把根节点本来就算出来的东西留下，
不该让搜索选出另一个着法。这条与 C++ 侧 `testAnalyze` 的断言一一对应。
"""

from __future__ import annotations

import threading
import time

import numpy as np

import engine_local as E


def midgame_board():
    """一个正常的开局盘面：分支够多，且没有现成的杀棋可以秒退。"""
    m = np.zeros((19, 19), dtype=np.uint8)
    for r, c, p in ((9, 9, 1), (9, 10, 2), (10, 8, 1), (8, 10, 2),
                    (10, 10, 1), (8, 9, 2), (7, 9, 1), (11, 11, 2)):
        m[r][c] = p
    return m


def test_candidate_table_is_complete(new_engine):
    """候选表必须覆盖**全部**合法候选点，一个不多一个不少。

    这是渴望窗口有没有关干净的唯一判据：渴望窗口的窄窗会剪掉一部分根着法，
    那些着法连分值都没有，而"最大值等于 best_val"这种检查**抓不到**这种残缺
    （被剪掉的都是低分着法）。
    """
    board = midgame_board()
    expected = len(E.Board.from_array(board).candidates())
    _idx, _val, cands, _info = new_engine.analyze(board, 1, 1)
    assert len(cands) == expected
    # 同一个点不该出现两次（重复会让"取这一手的值"读到哪一条变成随机）。
    assert len({i for i, _ in cands}) == len(cands)


def test_max_candidate_equals_best_value(new_engine):
    """无杀棋结论时，候选表的最大值就是 ``best_val``。"""
    board = midgame_board()
    idx, val, cands, info = new_engine.analyze(board, 1, 1)
    assert cands
    assert max(v for _, v in cands) == val
    # 最优着法本身必须在表里，且分值就是 best_val。
    assert dict(cands)[idx] == val
    assert info['best_val'] == val


def test_analyze_agrees_with_ai_move(new_engine):
    """同一局面、同一档位，``analyze`` 的最优着与 ``ai_move`` 必须一致。

    不一致意味着"复盘说该这么下、对局时引擎却不是这么下的" —— 复盘的解释力
    当场归零。两边走的都是全量收集的那条路（`collect=False` 时 collect_on
    只取决于偏置，而 1 档的偏置两处同源）。
    """
    board = midgame_board()
    new_engine.new_game()
    r, c, _info = new_engine.ai_move(board, 1, 1)
    idx, _val, _cands, _i2 = new_engine.analyze(board, 1, 1)
    assert idx == r * E.BOARD_SIZE + c


def test_collect_does_not_change_the_move(new_engine):
    """``collect=True`` 不该改变搜索结论（只是多留一张表）。"""
    board = midgame_board()
    ref, _info = new_engine.Engine().think(board, 2, 1, collect=False)
    got, info = new_engine.Engine().think(board, 2, 1, collect=True)
    assert got == ref
    assert 'root_vals' in info and info['root_vals']
    # 不收集时这些键**不能**出现：`info` 的键集合是对外契约，多一个恒为空的
    # 字段会让"两边 info 相等"这类比较无声地失败。
    assert 'root_vals' not in _info


def test_worst_candidate_has_positive_delta(new_engine):
    """表里最差的那个候选点，Δ 必须为正 —— 复盘就是靠这个找"坏棋"的。

    局面是黑的一个活三（下一手成活四），所以既不是"随便走走都赢"的必胜局面，
    也不会触发根节点的提前返回。
    """
    m = np.zeros((19, 19), dtype=np.uint8)
    for c in (5, 6, 7):
        m[9][c] = 1
    m[2][2] = 2
    m[3][3] = 2

    best_idx, best_val, cands, _info = new_engine.analyze(m, 1, 1)
    assert len(cands) == len(E.Board.from_array(m).candidates())
    worst_idx, worst_val = min(cands, key=lambda kv: kv[1])
    assert worst_idx != best_idx
    assert best_val - worst_val > 0


def test_win_in_one_table_is_short(new_engine):
    """已成"下一手成五"时，根节点会**提前返回**，表里只剩那一手。

    这是已知且**有意**的行为（`_root` 找到即成五就当场返回，不再给后面的
    候选打分，与 C++ 的 `root()` 逐字一致）。钉住它是为了让上面那条消费逻辑
    的前提可查：复盘侧据此把"没出现在表里、却确实是合法候选点"的那些手判成
    「错失必胜」，而不是「偏离战场」。**改掉这个提前返回会让本用例失败** ——
    那时要同步改的是 `main.ReviewWorker._one` 里那个判断。
    """
    m = np.zeros((19, 19), dtype=np.uint8)
    for c in (5, 6, 7, 8):
        m[9][c] = 1                     # 补 (9,4) 或 (9,9) 即成五
    m[2][2] = 2

    idx, val, cands, _info = new_engine.analyze(m, 1, 1)
    assert E.is_mate(val) and val > 0
    assert len(cands) == 1
    assert cands[0][0] == idx
    # 表短不等于盘上没有别的候选点 —— 消费方要能分清这两件事。
    assert len(E.Board.from_array(m).candidates()) > 1


def test_empty_board_has_no_candidates(new_engine):
    """空盘没有候选可比 → ``(-1, 0, [], info)``，调用方只需判 ``not cands``。"""
    empty = np.zeros((19, 19), dtype=np.uint8)
    idx, val, cands, _info = new_engine.analyze(empty, 1, 1)
    assert (idx, val, cands) == (-1, 0, [])


def test_analyze_cancel_returns_promptly(new_engine):
    """``cancel`` 置位后必须立刻返回，而不是跑满这一档的时间预算。

    与 ``test_cancel.py`` 同一条理由：复盘是十几手连着的，取消慢一拍就是
    用户眼里的"点了取消没反应"。
    """
    board = midgame_board()
    cancel = threading.Event()
    box = {}

    def run():
        t0 = time.monotonic()
        box['out'] = new_engine.analyze(board.copy(), 1, 3, cancel=cancel)
        box['dt'] = (time.monotonic() - t0) * 1000.0

    th = threading.Thread(target=run)
    th.start()
    time.sleep(0.05)
    t_set = time.monotonic()
    cancel.set()
    th.join(timeout=5.0)
    latency = (time.monotonic() - t_set) * 1000.0

    assert not th.is_alive(), "取消后线程没有退出"
    # 3 档的预算是 15 秒；200ms 只是留足调度抖动，远小于"取消没生效"。
    assert latency < 200.0, f"取消耗时 {latency:.1f}ms"
    assert box['dt'] < 5000.0


def test_analyze_uses_a_fresh_engine(new_engine):
    """复盘**不能**污染模块级引擎的置换表与 ``_normal_depth`` 统计。

    ``_normal_depth`` 会改变"全力找出路"的深度上限（见 ``_ESCAPE_EXTRA``），
    被复盘带偏之后，下一局的实际强度就与档位表对不上了 —— 而这**不会报错**，
    只是棋力悄悄变了。
    """
    board = midgame_board()
    new_engine.new_game()
    sentinel = new_engine.Engine()
    before = sentinel._normal_depth
    sentinel_tt = len(sentinel.tt)

    new_engine.analyze(board, 1, 2)

    assert new_engine._ENGINE._normal_depth == 0
    assert len(new_engine._ENGINE.tt) == 0
    # 与哨兵无关的那两个值本就该原样不动（哨兵没参与这次分析）。
    assert sentinel._normal_depth == before
    assert len(sentinel.tt) == sentinel_tt

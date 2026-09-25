# -*- coding: utf-8 -*-
"""引擎的模块边界与状态可重置性。

**这个文件在 Phase 3+4 之后不再测"等价性"。** Phase 1 时它断言
``engine.py`` 与冻结的 ``legacy_engine.py`` 逐手同着 —— 那在纯搬迁阶段是
比自对弈胜率更精确的护栏。Phase 3+4 换掉了整套评估与搜索，**同着从"必须"
变成了"不该"**：一个改动行为的新搜索如果还逐步同着，说明它没生效。

所以原地的三条同着断言被删掉了，取而代之的是：

* 模块边界（`test_engine_has_no_qt_or_torch`）—— 与算法无关，必须一直成立；
* 状态可重置（`test_engine_state_is_resettable`）—— 重写过的状态归属，
  必须仍然可清空。

**Phase 3+4 的回归靠别的东西**：`tests/test_eval.py`（评估的偏序与零和性）、
`tests/test_tt.py`（置换表标志与杀棋分归一化）、
`tests/test_search_mate.py`（B1：搜索必须找得到杀棋，也必须认得必败）、
`tests/test_incremental.py`（增量状态与全量重算一致），以及
``tools/selfplay.py`` 的自对弈胜率（对新搜索而言，这才是"更好了吗"的判据）。
"""

from __future__ import annotations

import numpy as np


def test_engine_has_no_qt_or_torch(new_engine):
    """engine.py 必须零 Qt、零 torch 依赖，且不 import main。

    用 AST 而非文本匹配：文档里会出现 "PyQt5"、"torch" 这些词（说明"已移除"），
    文本 grep 会把它们误判成依赖 —— 这正是 tools/BASELINE.md 里
    `grep -n torch` 那类检查的固有毛病。这里只看真实的 import 语句与被引用的名字。
    """
    import ast
    import io

    tree = ast.parse(io.open(new_engine.__file__, encoding="utf-8").read())

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module.split(".")[0])

    forbidden = {"torch", "PyQt5", "PyQt6", "PySide2", "PySide6",
                 "main", "intel_extension_for_pytorch"}
    hit = imported & forbidden
    assert not hit, f"engine.py 仍 import 了应被移除的模块: {sorted(hit)}"

    # 再确认没有对这些名字的运行时引用（字符串/注释不算）
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    leaked = names & {"torch", "F", "QColor", "QApplication", "QtWidgets"}
    assert not leaked, f"engine.py 仍引用了应被移除的名字: {sorted(leaked)}"


def test_engine_state_is_resettable(new_engine):
    """`new_game()` 必须能真正清空跨局面状态。

    Phase 3+4 把 TT / history / killer 从**模块全局**搬进了 `Engine` 实例。
    这个测试跟着改成检查实例属性 —— 但它守的语义没变：搜索完一步之后，
    跨局面状态必须非空（否则这个测试本身失效），而 `new_game()` 之后必须为空。

    "非空"这一半同样重要：如果 `ai_move` 因为某个早期 return（比如开局库）
    根本没建状态，那么"清空后为空"就是一句废话，测试会变成一个永远为真的
    假护栏。

    **那句话在开局库落地时真的应验了。** 原先这里用的是三颗子的盘面
    （`K10/OJ10/K9`），而开局库覆盖的正是"盘上 3 子以内"—— 一旦库里含这个
    局面，`ai_move` 就查表提前返回，下面三条"搜过之后应留下状态"全部落空，
    而**测试会通过**（`0 > 0` 为假 → 其实会失败，但换成任何"清空后为空"的
    形式就会静默变成废话）。所以盘面改用五颗子，并加一条显式断言把"盘面
    被库覆盖"这件事从静默变响 —— 换局面的理由写在断言里。
    """
    eng = new_engine._ENGINE
    board = np.zeros((19, 19), dtype=np.uint8)
    for r, c, p in ((9, 9, 1), (9, 10, 2), (10, 10, 1), (8, 9, 2), (10, 9, 1)):
        board[r][c] = p

    assert new_engine.book_lookup(board, 1) is None, \
        "这条用例的盘面被开局库覆盖了 —— 换个盘面，否则它不再检验任何东西"
    assert new_engine.book_lookup(board, 2) is None, \
        "同上（另一走子方）"

    new_engine.new_game()
    new_engine.ai_move(board.copy(), 2, 2)

    assert len(eng.tt) > 0, "一次搜索后应留下置换表条目，否则此测试无意义"
    assert any(any(h) for h in eng.history), "一次搜索后应留下历史表记账"
    assert any(k[0] >= 0 for k in eng.killers), "一次搜索后应留下杀手着法"

    new_engine.new_game()
    assert len(eng.tt) == 0
    assert eng.tt_age == 0
    assert not any(any(h) for h in eng.history)
    assert all(k[0] == -1 and k[1] == -1 for k in eng.killers)


def test_new_game_is_idempotent_and_reproducible(new_engine):
    """重置之后重复同一次搜索，结果必须逐字段一致。

    这条直接对着旧版的病灶：旧引擎的搜索结果依赖调用顺序（同一局面在冷/热
    置换表下给出**不同着法**）。现在状态归属实例、`new_game()` 清空它，于是
    "清空 → 搜索 → 记下结果 → 再清空 → 再搜索"必须得到同一个着法与同一个分值。

    **必须走 1 档，因为搜索是时间预算制的。** 档位不是深度：``ai_move`` 的
    第三个参数是档位，``DIFFICULTY[2]`` 是"5 秒 + 深度上限 10"。上限没够到
    时，**墙钟成了实际约束**，只搜完 4 层还是 5 层取决于机器当时有多忙 ——
    实测同一局面连跑两次：4.27s 一次停在第 4 层（best_val=-200），一次停在
    第 5 层（best_val=790）。那不是"状态没清干净"，是这条测试问错了问题：
    它要钉的是**状态重置**，而时间预算下的迭代深度本来就不该可复现。

    1 档（1.5s / 深度上限 4）在这个局面上约 0.39s 就跑满 4 层，深度上限先于
    时钟生效，于是结果只由局面决定。**光靠"1 档够快"是不够的** —— 那是拿
    余量赌 CI 机器的负载，所以这里再显式给一个宽裕的 ``time_limit``：搜索
    照旧在第 4 层停下（耗时不变），但"时钟不会成为约束"从期望变成了保证。
    下面的 ``actual_depth`` 断言把这个前提显式钉住，哪天它不成立，这里会
    直说"深度没跑满"，而不是伪装成一条"结果不可复现"的神秘失败。

    直接调 ``_ENGINE.think`` 而不是 ``ai_move``：只有 ``think`` 收
    ``time_limit``。这不算绕过封装 —— 本文件上面那条用例读的就是
    ``eng.tt`` / ``eng.history`` / ``eng.killers``。
    """
    board = np.zeros((19, 19), dtype=np.uint8)
    for r, c, p in ((9, 9, 1), (9, 10, 2), (10, 10, 1), (8, 9, 2), (10, 9, 1)):
        board[r][c] = p

    eng = new_engine._ENGINE
    cap = new_engine.DIFFICULTY[1]["max_depth"]
    out = []
    for _ in range(2):
        new_engine.new_game()
        idx, info = eng.think(board.copy(), 2, 1, time_limit=30.0)
        r, c = divmod(idx, new_engine.BOARD_SIZE)
        assert info["actual_depth"] == cap, (
            f"1 档没跑满深度上限 {cap}（只到 {info['actual_depth']}，"
            f"{info['time_ms']:.0f}ms）—— 时钟成了实际约束，"
            f"此局面下无法再断言可复现性")
        out.append((r, c, info["best_val"], info["actual_depth"]))

    assert out[0] == out[1], f"重置后结果不可复现：{out[0]} vs {out[1]}"

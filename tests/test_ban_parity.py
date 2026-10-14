# -*- coding: utf-8 -*-
"""C++ 与 Python 的禁手判定**逐点对账**。

`ban_rules.py`（Python）与 `cpp/src/ban.h`（C++）是同一套 RIF 规则的两份实现，
靠同一批局面上的 `forbiddenPoints` 结果互证。这不是"锦上添花的等价性测试"——
禁手规则里最贵的几处（同线双四、假活三、成五对长连的优先）都是**判据写错也不
报错**的那一类：C++ 会把一个禁手点当成好棋搜下去，输的操作手会指向"AI 变笨"
而看不到规则本身。

对账走服务端的 `bancheck` 命令（只读、不搜索、不碰置换表，与 `hello` 同一条
就地应答路径），所以**不受墙钟影响、可复现** —— 这一点很关键：引擎搜索是
"按墙钟有界 + 置换表跨局面保留"的，两份搜索结果本来就不可比，能拿来做机械
对账的只剩这种纯函数。

需要 `cpp/build/gomoku_engine_ban` 存在；没有就跳过 —— 这条是给"本机没编 C++"
留的方便，**CI 里不能拿它兜底**：`build.yml` 的 `test-ban` job 先建两个引擎、
再显式断言 `find_binary("free")` 与 `find_binary("ban")` 都找得到，然后才跑本
文件。少了那条断言，构建因为 target 改名之类的原因少产一个二进制时，这里会
整体 skip，而 `pytest` 的退出码不受 skip 影响 —— 一片绿，什么也没测。
"""

from __future__ import annotations

import numpy as np
import pytest

import ban_rules
import config
import engine

BOARD_SIZE = 19
BLACK, WHITE = 1, 2


@pytest.fixture(scope="module")
def ban_client():
    if config.find_binary("ban") is None:
        pytest.skip("没有构建 cpp/build/gomoku_engine_ban，两侧无从对账")
    c = engine._ServerClient(variant="ban")
    yield c
    c.shutdown()


@pytest.fixture(scope="module")
def free_client():
    if config.find_binary("free") is None:
        pytest.skip("没有构建 cpp/build/gomoku_engine")
    c = engine._ServerClient(variant="free")
    yield c
    c.shutdown()


def _board(stones):
    m = np.zeros((BOARD_SIZE, BOARD_SIZE), dtype=np.uint8)
    for r, c, p in stones:
        m[r][c] = p
    return m


def _both(ban_client, board, player=BLACK):
    """同一局面的两侧答案，各自规范化成可比的形状。"""
    py = sorted(ban_rules.forbidden_points(board, player))
    cpp = sorted(ban_client.bancheck(board, player))
    return py, cpp


#: 手工局面。每一条都盯着一条**具体**的判据，不是为了凑覆盖率 —— 随机局面
#: 里三三/四四极难自发出现，只靠随机等于没测这几条最贵的规则。
#: 坐标一律以天元 (9, 9) 为参照写着，读起来能直接对着规则核。
_CASES = {
    "空盘": [],
    "单子": [(9, 9, BLACK)],
    "白子不受禁": [(9, 9, WHITE), (9, 10, WHITE), (8, 10, WHITE),
                   (10, 10, WHITE)],
    "双活三（黑）": [(9, 8, BLACK), (9, 10, BLACK), (8, 9, BLACK), (10, 9, BLACK)],
    "冲四不成禁": [(9, 6, BLACK), (9, 7, BLACK), (9, 8, BLACK), (9, 10, BLACK)],
    "四四（十字）": [(9, 8, BLACK), (9, 7, BLACK), (9, 6, BLACK),
                     (8, 9, BLACK), (7, 9, BLACK), (6, 9, BLACK)],
    "同线双四（变形四四）": [(9, 5, BLACK), (9, 7, BLACK), (9, 8, BLACK),
                            (9, 9, BLACK), (9, 11, BLACK)],
    "长连可补": [(9, 5, BLACK), (9, 6, BLACK), (9, 7, BLACK), (9, 8, BLACK),
                 (9, 10, BLACK)],
    "成五优先于长连": [(9, 5, BLACK), (9, 6, BLACK), (9, 7, BLACK),
                       (9, 8, BLACK), (9, 9, BLACK), (9, 10, BLACK)],
    "贴边": [(0, 0, BLACK), (0, 1, BLACK), (0, 2, BLACK), (1, 0, BLACK),
             (2, 0, BLACK)],
    "角落十字": [(0, 1, BLACK), (1, 0, BLACK), (1, 1, BLACK), (2, 1, BLACK),
                 (1, 2, BLACK), (2, 2, BLACK)],
    "混合棋形": [(9, 9, BLACK), (9, 10, BLACK), (10, 9, BLACK), (9, 8, WHITE),
                 (8, 8, WHITE), (10, 10, BLACK), (10, 8, BLACK), (8, 10, WHITE)],
}


@pytest.mark.parametrize("name", sorted(_CASES))
def test_lib_cases_agree_with_the_cpp_engine(ban_client, name):
    """手工局面：两侧的禁手点集合逐个相同。"""
    board = _board(_CASES[name])
    py, cpp = _both(ban_client, board)
    assert py == cpp, (
        "局面「%s」两侧不一致：\n  只有 Python 判到 %r\n  只有 C++ 判到 %r"
        % (name, sorted(set(py) - set(cpp)), sorted(set(cpp) - set(py))))


def test_white_never_has_forbidden_points(ban_client):
    """白方怎么下都不禁 —— 两侧都必须回空表。"""
    board = _board(_CASES["双活三（黑）"])
    assert _both(ban_client, board, WHITE) == ([], [])


def test_a_hand_made_double_three_is_actually_caught(ban_client):
    """先证明这批局面**真的**含禁手，再谈两侧一致。

    少了这一条，"两侧都回空表"与"两侧都答对了"是同一个断言 —— 引擎或规则
    整体失灵时，上面那些用例会一起变绿。这里钉死一个已知的三三点。
    """
    board = _board(_CASES["双活三（黑）"])
    py, cpp = _both(ban_client, board)
    assert (9, 9) in py, "这个棋形里天元应当是三三点（Python 侧）"
    assert py == cpp
    assert ban_rules.is_foul(board, 9, 9)


# --------------------------------------------------------------- 随机对账

def _random_board(rng, n_stones, size=BOARD_SIZE):
    """随机局面：黑子略多，三三/四四才有机会出现。

    **固定种子**。这份对账的价值全在"下一次跑是同一批局面"：换了种子，红/绿
    就只是一次抽样，而禁手判据的错法（比如同线双四漏判）在随机抽样下出现率
    极低 —— 一个随机的种子会让它时红时绿，最后被当成抖动忽略掉。
    """
    m = np.zeros((size, size), dtype=np.uint8)
    cells = rng.permutation(size * size)
    for i, idx in enumerate(cells[:n_stones]):
        r, c = divmod(int(idx), size)
        m[r][c] = BLACK if i % 2 == 0 else WHITE
    return m


@pytest.mark.parametrize("n_stones", [6, 12, 20, 30])
def test_random_positions_agree_with_the_cpp_engine(ban_client, n_stones):
    """每个密度各 30 个固定随机局面，两侧逐点相同。"""
    rng = np.random.default_rng(20261014 + n_stones)
    for i in range(30):
        board = _random_board(rng, n_stones)
        py, cpp = _both(ban_client, board)
        assert py == cpp, (
            "第 %d 个随机局面（%d 子）两侧不一致：\n  只有 Python %r\n"
            "  只有 C++ %r" % (i, n_stones,
                              sorted(set(py) - set(cpp)),
                              sorted(set(cpp) - set(py))))


def test_random_dense_positions_actually_have_forbidden_points(ban_client):
    """随机盘面上真的判出了禁手点。

    没有这一条，上面那条在"`forbidden_points` 恒返回空表"的实现上也会绿 ——
    而两侧同时恒空正是**共同的 bug** 最可能的样子（例如共用了一条写错的
    "空点"判断）。
    """
    rng = np.random.default_rng(20261014)
    total = 0
    for _ in range(30):
        total += len(ban_rules.forbidden_points(_random_board(rng, 30)))
    assert total > 0, "30 个 30 子的随机局面里一个禁手点都没有，抽样失效"


# --------------------------------------------------------------- 编译开关

def test_the_free_build_reports_no_ban_rules(free_client):
    """无禁手版对同一个局面回**空表** —— `#if GOMOKU_BAN` 的空实现确实生效。

    这一条同时是"两个 target 不能共用一份状态"的旁证：free 版把 `ban.h` 编译
    成空实现，但如果接入点漏了守卫，free 的搜索结果就会跟着变 —— 那由
    `tests/test_engine_parity.py` 那侧的逐位一致守着。
    """
    board = _board(_CASES["双活三（黑）"])
    assert free_client.bancheck(board, BLACK) == []
    assert ban_rules.forbidden_points(board, BLACK), \
        "这个局面在 Python 侧是有禁手的，free 版回空表才说明守卫生效"


def test_a_rules_mismatch_is_refused_by_the_search_path(ban_client):
    """带着 ``rules:"free"`` 去问 ban 服务端搜一步 → 明确报错，不静默照算。

    静默照算的后果是分值刻度不同却混进同一条曲线 —— 库里那两张胜负图会
    悄悄画错。协议层用一个普通 ``type:error`` 拒绝，方向沿用既有约定。
    """
    board = _board([(9, 9, BLACK), (9, 10, WHITE), (8, 9, BLACK)])
    cfg = dict(engine._cfg_for(1))
    cfg["time"] = 0.3
    req = ban_client._search_request("compute", board, WHITE, cfg)
    req["rules"] = "free"                      # 冒充无禁手客户端
    with pytest.raises(engine._RemoteError):
        ban_client._request(req, config.COMPUTE_SLACK + 2.0)

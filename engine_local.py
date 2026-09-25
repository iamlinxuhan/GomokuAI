# -*- coding: utf-8 -*-
"""五子棋 AI 引擎（纯 CPU，零 Qt，零 torch）。

**当前进度：Phase 2 / 3+4 / 5 已完成。** Phase 3+4 **整体替换**了静态评估
与搜索，旧的 `evaluate_board` / `alpha_beta` 及其全部辅助函数已删除 —— 评估
不能半新半旧（新搜索按新棋型的偏序排序，若叶值仍由旧表给出，搜索会稳定地
追逐错误的目标）。Phase 5 把连续冲四从一个"静止搜索顺带涌现的现象"变成
带独立预算、**三态返回**的子系统（`vcf` / `_vcf` / `_vcf_defence`）。
Phase 6（打包收尾）未开始。

分五段，自上而下：

* **位棋盘核心（Phase 2）**：`Board` 类 + 预计算表。全增量维护候选集 /
  邻居计数 / 哈希 / 威胁聚合，`make`/`unmake` 严格对称因而支持乱序撤销。
  公开的 `check_win` 与搜索内部共用同一份 `_has_five_bits`。
* **棋型与评估（Phase 3）**：`_line_level` 是棋型等级的唯一语义来源 —— 按
  **集合语义**判定（``F(S) = {空点 e : 在 e 落子即成五}``，`|F|≥2` 为活四、
  `|F|=1` 为冲四，三/二用有界两层前瞻），而不是旧版那个把 `.XX.X.` 与
  `.X.XX.` 判成同一个 key 的三元组 `(count, open_ends, has_jump)`。
  `evaluate` 严格零和（``evaluate(pos, 黑) == -evaluate(pos, 白)``），
  **没有**旧版的 `ai - human * 0.85` 这类不对称系数 —— 那类系数会让同一局面
  在奇数层与偶数层得出不同结论。
* **搜索（Phase 4）**：negamax + PVS + 置换表 + 静态搜索 + 历史/杀手着法
  排序 + 迭代加深 + 志向窗口 + 硬时间上限 + 协作式取消。
  杀棋分与静态分**分带**：静态分钳在 ±`STATIC_MAX`，杀棋分在
  `WIN_SCORE - MAX_PLY` 以上，中间留 128 的真空带，于是 `is_mate()` 可靠。
* **连续冲四（Phase 5）**：根节点的 VCF 快速通道。静止搜索能看穿 10 手以内
  的强制序列，更长的那部分只能靠 VCF —— 它每一步都是"不挡就成五"，因此
  再长也能走到底。返回 **WIN / NO_WIN / EXHAUSTED 三态**，`EXHAUSTED`
  绝不可当 `NO_WIN` 用；防守走 AND 语义（对手所有应手都输才算赢）。

三处值得单独记住的设计（都是被实测逼出来的，不是偏好）：

  1. **威胁图用位图整体算，不逐个候选评分。** 逐候选算四条线的棋型实测
     136.5µs/节点，而 30k nps 的预算是 33µs/节点。现在用五个平移副本上的
     前缀/后缀/中间乘积求"恰好 3 子 / 恰好 4 子"的五窗口（`_hot_points`
     10.9µs、`_five_points` 3.2µs），并靠 `Board.threat_power`（活三以上的
     条数，增量维护）以 O(1) 把绝大多数静止节点整个跳过。
  2. **TT 的杀棋分按局面归一化。** 存 `value + ply`、取 `value - ply`，
     于是同一个局面在第 3 层与第 7 层被搜到时存下的是同一个数。
     不做这件事，深层搜索会读到浅层存的"距根步数"并当成"距我这儿步数"。
  3. **搜索状态归实例，不归模块。** 旧版把 TT / history / killer 放在模块
     全局，上一局的残留会改变这一局的着法（同一局面在冷/热表下走出不同的
     棋）。`_ENGINE` 单例 + `new_game()` 清空，`tests/test_tt.py` 有断言。

验证：`tests/test_eval.py`（棋型偏序与零和性）、`tests/test_tt.py`（条目类型
与杀棋分归一化）、`tests/test_search_mate.py`（找得到杀、认得必败，并把引擎
自己报出的杀棋线走一遍对账）、`tests/test_cancel.py`（取消的延迟与"不留幽灵
子"）、`tests/test_difficulty.py`（时间 / 深度 / 吞吐三档门槛）、
`tests/test_vcf.py`（真链找得到 + 假必胜否得掉 + 三态可分 + `dist` 可复现），
`tools/positions.py`（题库）与 `tools/selfplay.py`（对旧引擎的 A/B 胜率）。

历史：Phase 1 搬迁删掉了全部 PyTorch 代码（原版那些路径从未生效 —— 原 72-74
行无条件把 `_gpu_type` 覆盖回 `'cpu'`，使 `_ensure_torch()` 恒为 False）、
删掉了 PyQt5 依赖（日志类移到 gamelog.py），并把 Zobrist 表改为固定种子
（原版用未播种的 `np.random`，同一进程每次启动得到不同的表，实测搜索树大小
偏差约 20%）。Phase 1 的等价性断言在 Phase 3+4 之后**已删除**：一个换掉了
搜索的改动如果还逐步同着，说明它没生效。

对外 API：
    ai_move(board, ai_player, depth, cancel=None) -> (r, c, info)
    check_win(board, player) -> bool
    get_gpu_type() -> str          # 恒为 'cpu'
    new_game()                     # 清空跨局面状态
    BOARD_SIZE, WIN_SCORE
"""

import random
import time
import numpy as np

#: 开局库是一张**生成物**（由 `tools/build_book.py` 跑最强配置生成），随仓库
#: 一起存在。这里是**直接 import 而不是 try/except**：缺了它就该立刻炸。
#: 静默退回"没有开局库"是最坏的失败方式 —— 表没生效与表里没有这个局面在
#: 行为上完全一样，而后者是正常的（对手走出标准开局集就回落搜索）。把
#: "没装上"伪装成"查不到"，就再也没有任何线索能指出真正的原因。
from opening_book import BOOK, BOOK_STONES  # noqa: E402

# 这里曾有一行 `from gamelog import GameLogger`（注释称"ai_move 的 top_moves
# 诊断用其格式化坐标"）。实际上诊断信息只用了 `info` 里已经存好的字符串，
# 这个 import 从 Phase 1 搬迁起就没人引用过；`sys` / `math` 同理。
# 删掉后 engine.py 只剩 `random` / `time` / `numpy` 三个依赖，
# `test_engine_parity.py` 的模块边界断言比这里更严格，而它管的是
# "不许 import 什么"，管不到"多 import 了什么" —— 这条由 pyflakes 类检查兜底。

# ==================== 设备 ====================
# 原版此处的 torch 检测块已被删除：它检测出的结果会被紧随其后的赋值无条件
# 覆盖，GPU 路径从未生效。引擎现在只有一条 CPU 路径，get_gpu_type() 恒返回
# 'cpu'，UI 的 gpu_type 分支自动落到 CPU 分支。


def get_gpu_type():
    """返回当前 GPU 类型。本引擎为纯 CPU 实现，恒返回 'cpu'。

    保留此函数是为了让 main.py 的显示逻辑无需分支改写。
    """
    return 'cpu'

# ==================== 常量 ====================
BOARD_SIZE = 19
WIN_SCORE = 10000000        # 连五分值；搜索中表示为 WIN_SCORE - ply

# ==================== 位棋盘核心（Phase 2） ====================
# 棋盘原语 + 全增量状态。除"从 ndarray 建位图"这一个入口外不依赖 numpy，
# 也不依赖 Qt/torch，因此可以独立单测。
#
# 胜负判定全局只有一份实现（`_has_five_bits`），搜索内部与公开 API
# `check_win` 都走它 —— UI 看到的判定与搜索用的判定是同一种，不存在
# "两套判定偶尔不一致"这种状态。（Phase 1 搬迁期间确实并存过两套，
# Phase 3+4 之后旧的那套随旧搜索一起删掉了。）
#
# 下面 `_five_points` / `_hot_points` 只做**位运算**，不碰 Board 的增量状态：
# 它们要求调用方已经把 `opp_win`（对手的连五点掩码）准备好，这样同一个
# 函数既能用在"当前局面"上，也能用在"假设落子之后"的局面上。

CELLS = BOARD_SIZE * BOARD_SIZE              # 361
_FULL_MASK = (1 << CELLS) - 1
_CENTER_IDX = (BOARD_SIZE // 2) * BOARD_SIZE + BOARD_SIZE // 2

# 四个方向 (dr, dc) 及其在线性索引上的步长：
#   0 横 → 1     1 竖 → 19     2 撇 \ → 20     3 捺 / → 18
_DIRS = ((0, 1), (1, 0), (1, 1), (1, -1))
_DIR_STEPS = (1, BOARD_SIZE, BOARD_SIZE + 1, BOARD_SIZE - 1)

# Zobrist 表用独立实例 + 固定种子，不碰全局 random，也不受调用顺序影响。
# 与下方旧引擎的 _zobrist_table 是两张**互不相同**的表：新表服务于 Board
# 的增量哈希，旧表服务于旧 TT。两者不共享，也不应共享 —— 键空间不同。
_ZOBRIST_SEED_BB = 0x9E3779B9


def _zobrist_row(seed):
    """抽 361 个 64 位随机数。

    随机数发生器**必须在循环外创建一次**。写成
    ``tuple(random.Random(seed).getrandbits(64) for _ in range(CELLS))``
    看着等价，实则每次迭代都新建一个同种子发生器、取它的**第一个**输出 ——
    于是 361 个元素全部相同。那一版真的在这里存在过：整张表退化成常量，
    `Board.hash` 随之退化成"棋子数的奇偶性"（异或同一个数，偶数颗得 0），
    置换表只剩两个键，深度 6 之后每轮迭代都命中同一个错误条目，
    搜索报出 depth=24 而实际只走到 ply=5。

    **这个错误不会让任何一致性测试变红**：`from_array` 与 `make` 用的是同一张
    表，逐字段比对它们仍然相等。缺的是"表里元素互不相同"这条断言，
    见 `tests/test_incremental.py::test_zobrist_table_has_no_duplicates`。
    """
    rng = random.Random(seed)
    return tuple(rng.getrandbits(64) for _ in range(CELLS))


_ZOBRIST = (_zobrist_row(_ZOBRIST_SEED_BB), _zobrist_row(_ZOBRIST_SEED_BB + 1))


def _mask_cells(m):
    """把位掩码拆成升序的格子索引列表（`m & -m` 取最低位再异或掉）。"""
    out = []
    while m:
        low = m & -m
        out.append(low.bit_length() - 1)
        m ^= low
    return out


def _popcount(m):
    return bin(m).count('1')


def _build_lines():
    """枚举四个方向上的全部极大连续段。

    返回 ``(line_masks, cell_lines)``：

    * ``line_masks[d]`` —— 方向 d 上所有线的位掩码，共 19+19+37+37 = **112** 条；
    * ``cell_lines[idx]`` —— 经过该格的 4 条线的 ``(方向, 线号)``。

    线号在 ``cell_lines`` 里的用途是 Phase 3 的增量线评分：落子后只需重算
    经过它的这 4 条线。这里先把索引关系固化下来，避免那时再算一遍。
    """
    line_masks = []
    cell_lines = [[] for _ in range(CELLS)]
    for d, (dr, dc) in enumerate(_DIRS):
        masks = []
        for r in range(BOARD_SIZE):
            for c in range(BOARD_SIZE):
                pr, pc = r - dr, c - dc        # 只从线首出发：前驱已在盘外
                if 0 <= pr < BOARD_SIZE and 0 <= pc < BOARD_SIZE:
                    continue
                m = 0
                rr, cc = r, c
                while 0 <= rr < BOARD_SIZE and 0 <= cc < BOARD_SIZE:
                    idx = rr * BOARD_SIZE + cc
                    m |= 1 << idx
                    cell_lines[idx].append((d, len(masks)))
                    rr += dr
                    cc += dc
                masks.append(m)
        line_masks.append(masks)
    return line_masks, [tuple(x) for x in cell_lines]


_LINE_MASKS, _CELL_LINES = _build_lines()


def _build_win_masks():
    """每个格子上、所有**包含它**的连五窗口。

    这是增量胜负判定的依据：既然落子之前没有五连，那么现在若有五连，
    它必然包含刚落下的那一子 —— 于是只需检查这些窗口（中心格 ≤20 个），
    而不是像旧 ``_check_win_fast`` 那样每次全盘扫 1170 个单元。

    按线构建：每条线上每个窗口的掩码只算一次，再分发给窗口内的 5 格。
    """
    acc = [None] * CELLS
    for d in range(len(_DIRS)):
        for m in _LINE_MASKS[d]:
            cells = _mask_cells(m)
            for k in range(len(cells) - 4):
                w = 0
                for t in range(k, k + 5):
                    w |= 1 << cells[t]
                for t in range(k, k + 5):
                    i = cells[t]
                    if acc[i] is None:
                        acc[i] = []
                    acc[i].append(w)
    return tuple(tuple(x) if x else () for x in acc)


_WIN_MASKS = _build_win_masks()


def _build_neighbors():
    """每格周围切比雪夫距离 ≤2 的格子（**不含自身**），共 ≤24 个。

    半径 2 与旧 ``_generate_moves(around_only=True)`` 的 5×5 邻域一致。
    """
    out = []
    for idx in range(CELLS):
        r, c = divmod(idx, BOARD_SIZE)
        ns = []
        for dr in (-2, -1, 0, 1, 2):
            for dc in (-2, -1, 0, 1, 2):
                if dr == 0 and dc == 0:
                    continue
                rr, cc = r + dr, c + dc
                if 0 <= rr < BOARD_SIZE and 0 <= cc < BOARD_SIZE:
                    ns.append(rr * BOARD_SIZE + cc)
        out.append(tuple(ns))
    return tuple(out)


_NEIGHBORS = _build_neighbors()


def _build_five_starts():
    """每个方向上"从此格起的 5 格都在同一条线内"的起点掩码。

    全盘连五检测用 ``bits & bits>>s & bits>>2s & ...``，而线性索引会把
    上一行末尾与下一行开头**接在一起**（如 (0,18) 与 (0+1,0)）——这个掩码
    就是用来滤掉那类假起点的，否则行末四子 + 下一行首子会被误判成五连。
    """
    out = []
    for dr, dc in _DIRS:
        m = 0
        for r in range(BOARD_SIZE):
            for c in range(BOARD_SIZE):
                if 0 <= r + dr * 4 < BOARD_SIZE and 0 <= c + dc * 4 < BOARD_SIZE:
                    m |= 1 << (r * BOARD_SIZE + c)
        out.append(m)
    return tuple(out)


_FIVE_STARTS = _build_five_starts()


def _has_five_bits(bits):
    """位图里是否存在五连（含长连）。四个方向各 9 次大整数运算，无逐格循环。

    **这是全局唯一的胜负判定实现**：``check_win``、``Board.has_five``、
    ``Board.makes_five`` 与 Phase 3 的搜索都收敛到它或它的增量版本。
    """
    for d, s in enumerate(_DIR_STEPS):
        t = bits
        t &= bits >> s
        t &= bits >> (2 * s)
        t &= bits >> (3 * s)
        t &= bits >> (4 * s)
        if t & _FIVE_STARTS[d]:
            return True
    return False


def _bits_from_array(board, player):
    """ndarray → 该方的 361 位位图。用 packbits 走 C 层，避免 361 次标量索引。"""
    flat = np.asarray(board).ravel() == player
    packed = np.packbits(flat, bitorder='little').tobytes()
    return int.from_bytes(packed, 'little') & _FULL_MASK


# ==================== 棋型分类器（Phase 3） ====================
# 静态评估的唯一语义来源。旧引擎的棋型判定有两套（CPU 的 `_analyze_line`
# 三元组 + `_SCORE_TABLE` 查表），而三元组 `(count, open_ends, has_jump)`
# **是有损的**：`_XX_X_` 与 `_X_XX_` 都变成 `(3, True, True)`，但前者一步
# 成四、后者不是。有损的输入无法靠换权重表补救，只能换判定方式。
#
# 这里改成**集合语义**的精确判定，而非"数一数、看一看两端"：
#
#     F(S) = { 空点 e : 在 e 落子即连成五 }        （S 为某方在一条线上的子集）
#
# `|F|` 直接给出四族棋型，而且**天然去重**（集合而非窗口计数）：
#   |F| >= 2 → 活四（两个成五点，一步堵不住）
#   |F| == 1 → 冲四（只有一个成五点）
# 再往上递归地问"补一子能不能变成更好的棋型"，就得到三、二、一：
#   存在 e 使 S∪{e} 成为活四 → 活三；成为四 → 眠三；
#   成为活三 → 活二；成为眠三 → 眠二；成为活二/眠二 → 活一。
#
# 由此得到的偏序是**从定义推出来的**，不是拍脑袋排的权重表：
#   活四 > 冲四 > 活三 > 眠三 > 活二 > 眠二 > 活一。
# 这恰好修掉了旧表的两处倒挂 —— 旧表里"跳活三"(1M) 比活四(10M) 低一档却
# 比冲四(100K) 高一档，而"双活三"(1M) 与活三(10K) 差 100 倍。
#
# 组合棋型（双四、四三、双活三）**不在单线里表达**，由跨线的 threat_agg
# 计数加成 —— 单线 key 里放组合语义正是旧表把"双活三"塞进
# `(3, True, True)` 那条的来历，而它在单线上根本无法被正确识别。

# 棋型等级。整数比较即可判强弱（"至少是活三"这类判断全靠它）。
LV_NONE = 0
LV_ONE = 1          # 活一
LV_TWO_SLEEP = 2    # 眠二
LV_TWO_LIVE = 3     # 活二
LV_THREE_SLEEP = 4  # 眠三
LV_THREE_LIVE = 5   # 活三
LV_FOUR = 6         # 冲四
LV_FOUR_LIVE = 7    # 活四
LV_FIVE = 8         # 五连

# 单线分值。量级阶梯与方案 §2 的表一致：
# 活四 1M >> 冲四 100K >> 活三 30K >> 眠三 1K >> 活二 500 > 眠二 100 > 活一 10。
# **组合加成是绝对加成、且严格小于活四**（见 _combo_bonus），否则"双活三"
# 会被算得比活四还值钱，搜索会为了凑双三放弃成四。
LINE_SCORES = {
    LV_NONE: 0,
    LV_ONE: 10,
    LV_TWO_SLEEP: 100,
    LV_TWO_LIVE: 500,
    LV_THREE_SLEEP: 1000,
    LV_THREE_LIVE: 30000,
    LV_FOUR: 100000,
    LV_FOUR_LIVE: 1000000,
    LV_FIVE: WIN_SCORE,
}

# 段两侧各留的空格数。**取 4 是为了正确性，不是调优参数。**
#
# 分类只看"双方中某一方"的子，段按**该方**的跨度定。结论：所有与该方有关的
# 5 连窗口都落在段内。
#
#   含 ≥2 颗该方子的窗口，其左端 ≥ 首子 - 4、右端 ≤ 末子 + 4
#   （窗口只有 5 格，含 ≥2 颗子时不可能整体偏出这个范围）。
#
# 所以段外一格都不可能构成"含 ≥2 子的窗口"，而含 ≤1 子的窗口对任何棋型
# （四、三、二）都不产生贡献。段外的**对手**子也无所谓 —— 能挡住段内窗口的
# 对手子必然和那个窗口一起落在段内。
#
# 于是"只在小段上算窗口"与"在全 19 格线上算窗口"**结论完全一致**，而段的
# 长度通常只有 5~9，cache key 短、命中率高、分类器要扫的窗口也少。
_PAD = 4


def _build_line_views():
    """每条线的**局部坐标视图**与每格的线上位置。

    ``_LINE_VIEWS[lid] = (长度, 全局格索引元组)``，局部坐标 0..L-1 沿线的
    走向排列 —— 所以"局部 0 / L-1 就是线端"天然成立，分类器不必再传墙标志。

    ``_CELL_LINE_POS[idx]`` = 过该格的四条线的 ``(线号, 局部位置)``：
    ``make``/``unmake`` 靠它把一颗子折进/折出四条线的局部位图，O(1)。
    """
    views = []
    cell_pos = [[] for _ in range(CELLS)]
    for d in range(len(_DIRS)):
        for m in _LINE_MASKS[d]:
            cells = _mask_cells(m)
            views.append((len(cells), tuple(cells)))
            for t, idx in enumerate(cells):
                cell_pos[idx].append((len(views) - 1, t))
    return tuple(views), tuple(tuple(x) for x in cell_pos)


_LINE_VIEWS, _CELL_LINE_POS = _build_line_views()
_N_LINES = len(_LINE_VIEWS)

# 长度 -> 该长度的线上的 5 连窗口表。窗口是纯粹的"位置"量，与盘面无关，
# 因此同长度的线共用一张表（全盘只有长度 1..19 共 19 种）。
_LINE_WINDOWS_BY_LEN = {}
for _L in range(BOARD_SIZE + 1):
    _LINE_WINDOWS_BY_LEN[_L] = tuple(0b11111 << i for i in range(_L - 4))


def _segment(my, opp, length):
    """把一条线压到"与该方有关的那一小段"，返回 ``(my, opp, 窗口表)``。

    段 = 该方首子 - ``_PAD`` .. 该方末子 + ``_PAD``，再按线的两端夹紧。
    夹紧是**必需的**：线的端点就是墙，夹紧之后"窗口不能越过段端"恰好等价于
    "窗口不能越过线端"。段内没有对手子时 ``opp`` 为 0，键因此大量复用。
    """
    if not my:
        return 0, 0, ()
    lo = (my & -my).bit_length() - 1
    hi = my.bit_length() - 1
    a = lo - _PAD
    b = hi + _PAD
    if a < 0:
        a = 0
    if b > length - 1:
        b = length - 1
    w = b - a + 1
    mask = (1 << w) - 1
    return (my >> a) & mask, (opp >> a) & mask, _LINE_WINDOWS_BY_LEN[w]


def _completions(my, opp, wins):
    """``(F, 是否有五)``：``F`` 是"落子即连成五"的空点**集合**（位图）。

    **是集合不是窗口计数。** 计数会把 ``XXXX_XXXX`` 中间那个点算两次
    （左右两个窗口各含 4 颗子），把一个冲四误判成活四 —— 而活四是必胜、
    冲四不是，这个误判会直接改变搜索结论。
    """
    comp = 0
    for w in wins:
        if w & opp:
            continue
        free = w & ~my
        n = free.bit_count()
        if n == 0:
            return 0, True          # 整窗皆我 = 五连
        if n == 1:
            comp |= free
    return comp, False


def _max_f_after(my, opp, wins, cand, k):
    """在 ``cand`` 里补 ``k`` 子（``k <= 2``），能得到的最大 ``|F|``。

    上限截到 2 —— 判活四只需要"至少两个成五点"，再大的值没有意义，早点
    收敛能让短路退出更早生效。
    """
    best = 0
    m = cand
    while m:
        low = m & -m
        m ^= low
        my2 = my | low
        if k == 1:
            f, five = _completions(my2, opp, wins)
            if five:
                return 3
            n = f.bit_count()
            if n > best:
                best = n
            if best >= 2:
                return best
        else:
            # 第二子只需考虑"能与第一子一起进入某个窗口"的空点。
            sub = _max_f_after(my2, opp, wins,
                               cand & ~low & _windows_touching(wins, low), 1)
            if sub > best:
                best = sub
            if best >= 2:
                return best
    return best


def _windows_touching(wins, bit):
    """``wins`` 里覆盖 ``bit`` 那些窗口的并集 —— 第二子只可能在这些格子里。"""
    out = 0
    for w in wins:
        if w & bit:
            out |= w
    return out


def _line_level(my, opp, wins):
    """段内某方的棋型等级（``LV_*``）。``my``/``opp`` 是**段内局部**位图。

    四个"更高档"的判定直接照定义写，不做深度递归：

        活四 ⟺ |F| ≥ 2          冲四 ⟺ |F| = 1
        活三 ⟺ 补一子后 |F| ≥ 2   眠三 ⟺ 补一子后 |F| = 1
        活二 ⟺ 补两子后 |F| ≥ 2   眠二 ⟺ 补两子后 |F| = 1

    每一档都是**深度 ≤ 2 的有限搜索**（``_max_f_after``），因此必然终止。
    反过来，"递归地问上一层能变成什么"那种写法在子力散乱时**没有自然上界**
    —— 每补一子都还要再问下一层，实测会退化成对整个 2^n 子集空间的遍历。
    这里只问两层：够判到眠二/活二，再低就只有活一这一档，直接给底分。
    """
    if not my or not wins:
        return LV_NONE

    comp, five = _completions(my, opp, wins)
    if five:
        return LV_FIVE
    n = comp.bit_count()
    if n >= 2:
        return LV_FOUR_LIVE
    if n == 1:
        return LV_FOUR

    cand = 0
    for w in wins:
        cand |= w
    cand &= ~(my | opp)
    if not cand:
        return LV_ONE             # 全被堵死：给底分（表里没有"眠一"档）

    if _max_f_after(my, opp, wins, cand, 1) >= 2:
        return LV_THREE_LIVE
    if _max_f_after(my, opp, wins, cand, 1) >= 1:
        return LV_THREE_SLEEP
    if _max_f_after(my, opp, wins, cand, 2) >= 2:
        return LV_TWO_LIVE
    if _max_f_after(my, opp, wins, cand, 2) >= 1:
        return LV_TWO_SLEEP
    return LV_ONE


# 线评分缓存。键是**压缩后的段**（见 _segment），不是整条线 —— 这才是命中的
# 来源：`_X_` 这类形状在 19 条不同的线上、不同的局面里反复出现。
#
# **有上限。** 段是任意位图，键空间理论上是发散的（对局久了条目会一直涨）。
# 清空而不是 LRU：这里的条目是纯缓存，重建代价只有几微秒，维护 LRU 的
# 记账成本反而更高。清空的瞬间命中率掉一次，随即回升。
_line_cache = {}
_LINE_CACHE_MAX = 200_000


def line_score(my, opp, length=BOARD_SIZE):
    """线上某方的 ``(分值, 等级)``。``my``/``opp`` 是**整条线**的局部位图。

    返回等级是给跨线组合计数用的（``threat_agg``）—— 分值本身分不清
    "两个冲四"与"一个活四"，而这两者在棋理上完全不同。
    """
    if not my:
        return 0, LV_NONE
    seg = _segment(my, opp, length)
    if not seg[0]:
        return 0, LV_NONE
    hit = _line_cache.get(seg)
    if hit is None:
        hit = _line_level(seg[0], seg[1], seg[2])
        if len(_line_cache) >= _LINE_CACHE_MAX:
            _line_cache.clear()
        _line_cache[seg] = hit
    return LINE_SCORES[hit], hit


# ---- 跨线组合 ----
# 单线只能表达"一条线上最好的那个棋型"，而双四 / 四三 / 双活三**天然是跨线的**
# —— 这正是旧表把"双活三"塞进单线 key `(3, True, True)` 的来历，而单线 key
# 根本无法正确识别它（任何一条线上都只有"活三"）。所以组合在这里按**计数**
# 表达。
#
# **加成是绝对量、不是倍乘，且总量严格小于活四。** 否则"双活三"会被算得比
# 活四值钱，搜索会为了凑双三而放弃成四。实测偏序：
#     活四 1.00M > 双四 0.90M > 四三 0.73M > 双活三 0.56M > 冲四 0.10M
# 与棋理一致（活四 > 四三 = 双四 > 双活三 > 冲四）。
BONUS_DOUBLE_FOUR = 700_000     # 两条线 ≥ 冲四
BONUS_FOUR_THREE = 600_000      # 一条线 ≥ 冲四，另一条 ≥ 活三
BONUS_DOUBLE_THREE = 500_000    # 两条线 ≥ 活三


# ==================== 静态评估（Phase 3） ====================

# 杀棋分与静态分的分界。杀棋分落在 [WIN_SCORE - MAX_PLY, WIN_SCORE]，静态分
# 被钳进 ±STATIC_MAX，中间**留出 128 - MAX_PLY 的真空带** —— 于是
# `abs(v) > STATIC_MAX` 就是"这是杀棋分"的判据，`is_mate()` 靠它，不靠猜。
STATIC_MAX = WIN_SCORE - 128
MAX_PLY = 64
assert MAX_PLY < WIN_SCORE - STATIC_MAX, "杀棋分与静态分必须不重叠"


def _combo_bonus(agg):
    """跨线组合加成。``agg`` 是 ``Board.threat_agg[p]``（等级 → 线数）。

    按"最贵的那一种"取，不叠加：双四本身就蕴含"有活三"，再叠一次四三会把
    它算得比活四还贵。偏序（见 BONUS_* 注释）由这一条 `if` 链保证。
    """
    fours = agg[LV_FOUR] + agg[LV_FOUR_LIVE]
    if fours >= 2:
        return BONUS_DOUBLE_FOUR
    if fours and agg[LV_THREE_LIVE]:
        return BONUS_FOUR_THREE
    if agg[LV_THREE_LIVE] >= 2:
        return BONUS_DOUBLE_THREE
    return 0


def evaluate(board, me):
    """从 ``me`` 视角的静态分。**严格零和**：``evaluate(b, 1) == -evaluate(b, 2)``。

    零和不是美学要求，是 negamax 的前提：negamax 用 ``-evaluate(child)`` 传播
    叶值，只有反号对称的函数才能让"同一局面在奇数层与偶数层被评估"得到一致
    结论。旧 ``evaluate_board`` 里的 ``ai_score - human_score * 0.85`` 破坏了
    这一点（它让评估与"谁在评估"有关），Phase 3 一并移除。

    **不含任何 tempo/先行方常数。** 给"行棋方"加定值同样会破坏零和 —— 命题
    `eval(pos, 黑) == -eval(镜像(pos), 白)` 在两边都 +K 之后变成 `d + K` 对
    `-d + K`。测试 `test_eval.py` 的零和性断言就是钉住这一条的。
    """
    p, q = me - 1, 2 - me
    v = (board.score_sum[p] + _combo_bonus(board.threat_agg[p])
         - board.score_sum[q] - _combo_bonus(board.threat_agg[q]))
    if v > STATIC_MAX:
        return STATIC_MAX
    if v < -STATIC_MAX:
        return -STATIC_MAX
    return v


def is_mate(value):
    """该分值是否为杀棋分（含必胜与被杀）。见 STATIC_MAX 处的真空带说明。"""
    return value > STATIC_MAX or value < -STATIC_MAX


# ---- 威胁点图（走法排序用，不是评估）----
#
# 走法排序要回答一个具体问题："哪些空点落下去会让某方出现四子连窗？"——
# 这些点（成四 / 成五）是攻防的必争点，必须排在前面。
#
# 直接做法是"逐个候选落子再问等级"，但那是 ~136µs/节点（实测），比整个节点
# 预算还大。这里改用位运算一次算完整张图：一个 5 窗内若有恰好 3 枚某方子，
# 则该窗的两个空点都是四点；恰有 4 枚，则唯一空点是五点。用"前缀积 / 后缀积
# 删掉指定位置"把这两种情形枚举出来，每个方向 ~65 次大整数运算。
#
# **有代价（约 12µs），所以调用方必须先在 threat_agg 上判"有没有棋型"再调。**
# 这与旧 `_find_winning_moves`（逐点全盘扫）的区别是：那个是 O(361) 次
# Python 循环，这里是 O(1) 次大整数运算。

_DIR5 = tuple(range(5))


def _five_points(bits, obits):
    """落子即成五的空点集合。

    `_hot_points` 的"恰 4 子"那一半单独拎出来 —— 它是**强制着法**的判据
    （成五 / 挡五），在每个静止节点都要问一次，而"恰 3 子"那一半只在确认
    存在活三以上棋型之后才需要。省下的是每节点约 8µs。
    """
    occ = bits | obits
    out = 0
    for d in range(4):
        s = _DIR_STEPS[d]
        opp_win = obits | (obits >> s) | (obits >> (2 * s)) \
                  | (obits >> (3 * s)) | (obits >> (4 * s))
        limit = _FIVE_STARTS[d] & ~opp_win
        if not limit:
            continue
        b0 = bits
        b1 = bits >> s
        b2 = bits >> (2 * s)
        b3 = bits >> (3 * s)
        b4 = bits >> (4 * s)
        out |= ((~0 & b1 & b2 & b3 & b4 & limit)) << 0
        out |= ((b0 & ~0 & b2 & b3 & b4 & limit)) << s
        out |= ((b0 & b1 & ~0 & b3 & b4 & limit)) << (2 * s)
        out |= ((b0 & b1 & b2 & ~0 & b4 & limit)) << (3 * s)
        out |= ((b0 & b1 & b2 & b3 & ~0 & limit)) << (4 * s)
    return out & ~occ


def _hot_points(bits, obits):
    """空点中「落子即形成四子连窗」的集合（含五点，五点是四点的子集）。

    ``bits`` 为己方、``obits`` 为对方。返回值为 361 位位图。
    """
    occ = bits | obits
    out = 0
    for d in range(4):
        s = _DIR_STEPS[d]
        b0 = bits
        b1 = bits >> s
        b2 = bits >> (2 * s)
        b3 = bits >> (3 * s)
        b4 = bits >> (4 * s)

        # 窗口起点掩码：窗口内不得有对方子
        opp_win = obits | (obits >> s) | (obits >> (2 * s)) \
                  | (obits >> (3 * s)) | (obits >> (4 * s))
        limit = _FIVE_STARTS[d] & ~opp_win
        if not limit:
            continue

        bb = (b0, b1, b2, b3, b4)
        # pre[k] = b0 & ... & b{k-1}；suf[k] = b{k} & ... & b4
        pre = [~0] * 6
        for k in range(5):
            pre[k + 1] = pre[k] & bb[k]
        suf = [~0] * 6
        for k in range(4, -1, -1):
            suf[k] = suf[k + 1] & bb[k]
        # mid[a][c] = b{a+1} & ... & b{c-1}（a<c）
        mid = [[~0] * 5 for _ in _DIR5]
        for a in _DIR5:
            acc = ~0
            for c in range(a + 1, 5):
                mid[a][c] = acc
                acc &= bb[c]

        cells = 0
        # 恰 3 子（两个空位 a、c）：两空点都是四点
        for a in _DIR5:
            pa = pre[a]
            if not pa:
                continue
            for c in range(a + 1, 5):
                m = pa & mid[a][c] & suf[c + 1] & limit
                if m:
                    cells |= (m << (a * s)) | (m << (c * s))
        # 恰 4 子（一个空位 g）：该空点是五点
        for g in _DIR5:
            m = pre[g] & suf[g + 1] & limit
            if m:
                cells |= m << (g * s)

        out |= cells & ~occ
    return out


class Board:
    """位棋盘 + 全增量状态。

    ``b_bits`` / ``w_bits`` 是 361 位整数位图，bit i 对应
    ``(r, c) = divmod(i, BOARD_SIZE)``。用 Python 大整数而非 numpy：位运算在
    C 层完成，省掉 numpy 标量索引开销 —— 旧 ``zobrist_hash`` 的 361 次标量
    索引正是其开销来源之一。

    ``neighbor_count[idx]``：idx 周围半径 2 内的**棋子数**（不含 idx 自身）。
    ``cand_mask``：所有"为空且有邻居"的格子，与旧 ``_generate_moves``
    的候选集逐格等价。

    ``make``/``unmake`` 是**严格对称**的（XOR + ±1 计数），因此允许乱序
    unmake：只要每颗子都被撤销过一次，状态就位级还原，无需维护撤销栈。
    这一点对搜索很关键 —— 有了它，剪枝路径上提前 return 也不用清理。
    """

    __slots__ = ("b_bits", "w_bits", "hash", "neighbor_count", "cand_mask",
                 "_n_black", "_n_white",
                 "line_bits", "line_lv", "score_sum", "threat_agg",
                 "threat_power")

    def __init__(self):
        self.b_bits = 0
        self.w_bits = 0
        self.hash = 0
        self.neighbor_count = bytearray(CELLS)
        self.cand_mask = 0
        self._n_black = 0
        self._n_white = 0
        # ---- 增量线评分（Phase 3）----
        # line_bits[p][lid]：第 p 方在 112 条线上的**局部位图**。
        # line_lv[p][lid]：该方在那条线上的棋型等级（LV_*），与 line_bits 同步
        #   维护 —— 存下来是为了"减掉旧值、加上新值"时不必重算旧等级。
        # score_sum[p]：Σ LINE_SCORES[line_lv[p][lid]]，全盘静态分。
        # threat_agg[p][lv]：等级 lv 出现在多少条线上，供跨线组合计数。
        # 初值是"112 条线全在 LV_NONE"，不是全零 —— 它必须自始至终等于
        # "等级为 lv 的线数"，增量维护才可能与全量重算逐字段相等。
        self.line_bits = ([0] * _N_LINES, [0] * _N_LINES)
        self.line_lv = ([LV_NONE] * _N_LINES, [LV_NONE] * _N_LINES)
        self.score_sum = [0, 0]
        self.threat_agg = [[_N_LINES] + [0] * 8, [_N_LINES] + [0] * 8]
        # threat_power[p]：「活三或更强」的线数。它不是新信息，只是
        # threat_agg 的一个常用聚合 —— 搜索里"要不要花 12µs 算威胁点图"
        # 这个判断每个节点都做，必须 O(1)，不能每次现加 4 个列表项。
        self.threat_power = [0, 0]

    # ---------------------------------------------------------- 查询

    @property
    def occupied(self):
        return self.b_bits | self.w_bits

    @property
    def stone_count(self):
        return self._n_black + self._n_white

    def bits_of(self, player):
        return self.b_bits if player == 1 else self.w_bits

    def get(self, idx):
        bit = 1 << idx
        if self.b_bits & bit:
            return 1
        if self.w_bits & bit:
            return 2
        return 0

    def is_empty(self, idx):
        return not (self.occupied >> idx) & 1

    def has_five(self, player):
        return _has_five_bits(self.bits_of(player))

    def makes_five(self, player, idx):
        """刚落下的这一子是否形成五连（含长连）。

        只查包含 idx 的窗口。调用前提是**落子前无五连** —— 若双方都已有
        五连在盘上，本方法只能回答"这一子是否参与了其中一个"。
        """
        bits = self.bits_of(player)
        for m in _WIN_MASKS[idx]:
            if bits & m == m:
                return True
        return False

    def candidates(self):
        """候选落点（升序索引）。

        cand_mask 为空有两种情形：空盘，或盘面已被填满。前者返回天元
        （空盘的开局着法），后者返回空表。
        """
        if not self.cand_mask:
            return [_CENTER_IDX] if self.stone_count < CELLS else []
        return _mask_cells(self.cand_mask)

    # ---------------------------------------------------------- 变更

    def make(self, idx, player):
        """落子并增量维护全部状态；返回该子是否成五。

        只更新 ``_NEIGHBORS[idx]`` 这 ≤24 格，不做任何全盘扫描。

        **前提：``idx`` 当前必须是空格。** 这里不做校验 —— 它在搜索的热路径
        上，而每节点多一次判空是不必要的开销。违反该前提不会抛异常，只会
        让哈希与子数静默偏移（``b_bits |= bit`` 本身是幂等的，所以盘面看着
        还对，错的是哈希与计数），是最难查的一类 bug。调用方负责。

        ``idx`` 先经 ``int()`` 收口。这不是多余的：``np.argwhere`` 之类
        返回的 ``np.int64`` 一旦参与位移，``1 << idx`` 会得到 numpy 标量，
        于是 ``cand_mask`` 被静默换成 numpy 类型；等某次位移 ≥63 位时就抛
        ``OverflowError: Python int too large to convert to C long`` ——
        报错点离真正的原因隔了很远，且此时棋盘状态已经被污染了。
        """
        idx = int(idx)      # 见下方说明：必须在任何位移之前收口
        bit = 1 << idx
        if player == 1:
            self.b_bits |= bit
            self._n_black += 1
        else:
            self.w_bits |= bit
            self._n_white += 1
        self.hash ^= _ZOBRIST[player - 1][idx]
        self.cand_mask &= ~bit

        occ = self.b_bits | self.w_bits
        nc = self.neighbor_count
        for j in _NEIGHBORS[idx]:
            v = nc[j] + 1
            nc[j] = v
            # 只在 0→1 时入候选。另外必须判空：一颗孤子自身 nc 为 0，
            # 邻近处补上一子时它的 nc 会变成 1，但它是占着的，不属于候选。
            if v == 1 and not (occ >> j) & 1:
                self.cand_mask |= 1 << j

        # 增量线评分：只重算过该点的 4 条线。**两条线都要重算双方** ——
        # 落一子除了形成己方棋型，还可能**打断**对手的跳活三（`_X_X_` 变成
        # `_XOX_`），只更己方会让对手的分虚高。
        p = player - 1
        bits = self.line_bits
        for lid, t in _CELL_LINE_POS[idx]:
            bits[p][lid] |= 1 << t
            self._rescore_line(lid)
        return self.makes_five(player, idx)

    def _rescore_line(self, lid):
        """重算第 ``lid`` 条线上**双方**的等级，并把差值折进 score_sum / threat_agg。

        折差值而不是全量求和：全量要扫 112 条线，而一次落子只影响 4 条。

        ``threat_agg`` 连 ``LV_NONE`` 一起计数，不做"0 就跳过"的省事优化 ——
        那是错的：省掉之后 ``threat_agg[p][0]`` 的含义变成"曾经非零、
        现在归零过多少次"，不再等于"等级为 NONE 的线数"，而**增量状态必须
        是盘面的函数**，否则 `Board.diff` 这类逐字段对照的测试就失去意义。
        """
        length = _LINE_VIEWS[lid][0]
        lb = self.line_bits
        lv = self.line_lv
        agg = self.threat_agg
        ss = self.score_sum
        tp = self.threat_power
        for q in (0, 1):
            old = lv[q][lid]
            agg[q][old] -= 1
            ss[q] -= LINE_SCORES[old]
            if old >= LV_THREE_LIVE:
                tp[q] -= 1
            new = line_score(lb[q][lid], lb[1 - q][lid], length)[1]
            lv[q][lid] = new
            agg[q][new] += 1
            ss[q] += LINE_SCORES[new]
            if new >= LV_THREE_LIVE:
                tp[q] += 1

    def unmake(self, idx, player):
        """撤销落子。与 make 严格对称，故可在任意顺序下调用。"""
        idx = int(idx)                  # 同 make：必须在位移前收口
        bit = 1 << idx
        if player == 1:
            self.b_bits &= ~bit
            self._n_black -= 1
        else:
            self.w_bits &= ~bit
            self._n_white -= 1
        self.hash ^= _ZOBRIST[player - 1][idx]

        nc = self.neighbor_count
        for j in _NEIGHBORS[idx]:
            v = nc[j] - 1
            nc[j] = v
            # v==0 即该格周围再没有棋子。若 j 正被占着，它的候选位本来就是 0，
            # 这里清一次是无害的空操作。
            if v == 0:
                self.cand_mask &= ~(1 << j)
        if nc[idx]:                      # 撤掉之后 idx 变空，若有邻居则回到候选
            self.cand_mask |= bit

        p = player - 1
        bits = self.line_bits
        for lid, t in _CELL_LINE_POS[idx]:
            bits[p][lid] &= ~(1 << t)
            self._rescore_line(lid)

    # ---------------------------------------------------------- 转换

    @classmethod
    def from_array(cls, arr):
        """从 ndarray 一次构建（批量算邻居计数，不经 make）。

        结果必须与"逐子 make 到同一盘面"完全一致 ——
        ``tests/test_incremental.py`` 逐字段比对两者，那是增量正确性的依据。
        """
        b = cls()
        a = np.asarray(arr)
        b.b_bits = _bits_from_array(a, 1)
        b.w_bits = _bits_from_array(a, 2)
        b._n_black = _popcount(b.b_bits)
        b._n_white = _popcount(b.w_bits)

        h = 0
        for idx in _mask_cells(b.b_bits):
            h ^= _ZOBRIST[0][idx]
        for idx in _mask_cells(b.w_bits):
            h ^= _ZOBRIST[1][idx]
        b.hash = h

        nc = b.neighbor_count
        occ = b.b_bits | b.w_bits
        for idx in _mask_cells(occ):
            for j in _NEIGHBORS[idx]:
                nc[j] += 1
        cm = 0
        for idx in range(CELLS):
            if nc[idx] and not (occ >> idx) & 1:
                cm |= 1 << idx
        b.cand_mask = cm

        # 线状态：逐子折进四条线（与 make 的更新路径一致），再统一重算等级。
        lb = b.line_bits
        for idx in _mask_cells(b.b_bits):
            for lid, t in _CELL_LINE_POS[idx]:
                lb[0][lid] |= 1 << t
        for idx in _mask_cells(b.w_bits):
            for lid, t in _CELL_LINE_POS[idx]:
                lb[1][lid] |= 1 << t
        for lid in range(_N_LINES):
            b._rescore_line(lid)
        return b

    def to_array(self):
        out = np.zeros((BOARD_SIZE, BOARD_SIZE), dtype=np.uint8)
        for idx in _mask_cells(self.b_bits):
            out[idx // BOARD_SIZE][idx % BOARD_SIZE] = 1
        for idx in _mask_cells(self.w_bits):
            out[idx // BOARD_SIZE][idx % BOARD_SIZE] = 2
        return out

    def diff(self, other):
        """逐字段比对两个 Board，返回不一致的字段名列表。

        比 `==` 有用：失败时直接告诉你**是哪个增量量**错了 —— 候选集、邻居
        计数、哈希、还是子数。
        """
        bad = []
        for f in ("b_bits", "w_bits", "hash", "cand_mask",
                  "_n_black", "_n_white"):
            if getattr(self, f) != getattr(other, f):
                bad.append(f)
        if bytes(self.neighbor_count) != bytes(other.neighbor_count):
            bad.append("neighbor_count")
        if self.line_bits != other.line_bits:
            bad.append("line_bits")
        if self.line_lv != other.line_lv:
            bad.append("line_lv")
        if self.score_sum != other.score_sum:
            bad.append("score_sum")
        if self.threat_agg != other.threat_agg:
            bad.append("threat_agg")
        if self.threat_power != other.threat_power:
            bad.append("threat_power")
        return bad

    def __repr__(self):
        return (f"<Board 黑{self._n_black} 白{self._n_white} "
                f"候选{_popcount(self.cand_mask)} hash={self.hash:#x}>")


# ==================== 开局库 (Opening Book) ====================
def opening_move(board, player):
    """开局着法（确定性，无随机）。替代原版的 _OPENING_BOOK / _board_to_fen。

    原版那套"棋谱键 -> 着法"的映射从未生效过：_board_to_fen 对天元黑子产出
    "cj10_1"，而书里的键写的是 "c9,9_b1"，格式对不上，除空盘外永不命中。
    这里改为直接看棋盘，不再经过中间字符串。

    - 空盘：天元
    - 盘上仅对手一子：紧贴该子，偏移按固定顺序取第一个空点
      （这正是原版 "c9,9_b1" 键想表达、却因为键对不上而没走到的着法）
    - 其余：返回 None，交由正规搜索处理
    """
    stones = np.argwhere(board != 0)
    if len(stones) == 0:
        return (BOARD_SIZE // 2, BOARD_SIZE // 2)
    if len(stones) == 1:
        r0, c0 = int(stones[0][0]), int(stones[0][1])
        for dr, dc in ((0, -1), (0, 1), (-1, 0), (1, 0),
                       (-1, -1), (-1, 1), (1, -1), (1, 1)):
            r, c = r0 + dr, c0 + dc
            if 0 <= r < BOARD_SIZE and 0 <= c < BOARD_SIZE and board[r][c] == 0:
                return (r, c)
    return None


def book_lookup(board, player):
    """开局库查表。命中返回 ``(idx, score, depth)``，未命中返回 ``None``。

    三项的含义与搜索输出逐字对齐：``idx`` 是线性格索引，``score`` 是**走子方
    视角**的分值（与 ``best_val`` 同向、同量纲），``depth`` 是建库时搜到的层数。

    **键 = 局面 + 走子方。** ``Board.hash`` 只由棋子决定、与走子方无关（见
    文件头 Zobrist 那一段），所以这里照置换表的约定异或 ``_TT_SALT_W``。
    同一盘面不可能轮到两方，带上走子方是为了让"表按走子方分区"写在键上。

    **只在 ``stone_count <= BOOK_STONES``（盘上 3 子以内）时查。** 表是按
    "第 2/3/4 手"建的，中盘局面的哈希不可能命中；这道闸只是省掉每次搜索前的
    一次哈希计算，不是正确性的一部分。

    命中时**分值如实回传**，不填 0：那是这个局面的真实评估（建库时由宗师在
    完整预算下算出）。代价是入门档在开局头几手会显示出高手级读数 —— 详见
    ``tools/build_book.py`` 的模块文档。
    """
    bd = Board.from_array(board)
    if bd.stone_count > BOOK_STONES:
        return None
    return BOOK.get(bd.hash ^ (0 if player == 1 else _TT_SALT_W))


# ==================== 搜索（Phase 3+4） ====================
#
# 整段是对旧 `alpha_beta` 的重写。三条结构性的改变，每一条都对应一个已确认的
# 缺陷（编号见 tools/BASELINE.md）：
#
#  1. **状态从模块全局搬进 `Engine` 实例**（B19）。旧版把 TT / history / killer
#     放在模块全局且从不清空，于是同一局面在冷/热 TT 下给出**不同着法**，上一
#     局的搜索结果也会泄漏到下一局。`new_game()` 是给这个设计打的补丁；改成
#     实例属性后，泄漏在结构上不可能发生。
#  2. **真时间控制 + 协作取消**（B12/B19）。旧版"接受 cancel 参数但从不读取"，
#     思考中途重开局只能等满 UI 的 3 秒余量；`gui_smoke` 的两条 WARN 就是它。
#     现在每 1024 个节点轮询一次 deadline 与 cancel。
#  3. **静态分与杀棋分分层**（B1）。旧版在 `evaluate_board` 里按 ±1e8 直接判定
#     "某方已成五"并返回，于是静态评估可以盖过搜索找到的杀棋 —— 搜索会避开
#     真正的杀线。现在杀棋分只由搜索产生，且严格落在静态分带之外。

TT_EXACT, TT_LOWER, TT_UPPER = 0, 1, 2

INF = 1 << 30
_ABORT_MASK = 1023          # 每 1024 个节点轮询一次时间/取消
_TT_MAX = 1 << 20           # 置换表条目上限（约 100 万条，内存 ~200MB 上限内）
_TT_SALT_W = 0x5D5D5D5D5D5D5D5D     # 白方行棋的键扰动
_ASPIRATION = 120           # 渴望窗口初值
_ASP_WINDOW_MUL = 4         # 失败后窗口放大倍数
_ASP_FAILS = 3              # 连续失败几次后放弃渴望、走全窗口


def _flag_of(value, alpha_orig, beta):
    """置换表条目类型。**抽成纯函数是为了能单测**（B2 回归）。

    边界是这条函数的全部难点：`value == alpha_orig` 必须判为 UPPER 而不是
    EXACT。旧版写成 `value > alpha_orig` 判 EXACT，于是"恰好等于下界"的
    失败低被当成精确值存下来，后续搜索会据此**直接返回一个上界冒充精确值**。
    """
    if value <= alpha_orig:
        return TT_UPPER
    if value >= beta:
        return TT_LOWER
    return TT_EXACT


def _to_tt(value, ply):
    """杀棋分归一化：从"距当前节点多少步"换成"距根多少步"再存。

    不归一化的话，同一局面在树的不同深度被存进来会得到**不同的分值**，
    置换表命中的那一刻就等于用一个深度错误的杀棋分覆盖真实值。归一化之后
    条目是盘面的函数，与在哪一层被存无关。
    """
    if value > STATIC_MAX:
        return value + ply
    if value < -STATIC_MAX:
        return value - ply
    return value


def _from_tt(value, ply):
    """`_to_tt` 的逆。"""
    if value > STATIC_MAX:
        return value - ply
    if value < -STATIC_MAX:
        return value + ply
    return value


class SearchAborted(Exception):
    """搜索被取消或超时。

    向上穿透到 `Engine.think`，由它决定用**哪一轮完整迭代**的结果。

    注意：抛出时 `Board` 可能停在中途状态（make 了却没 unmake）。这是刻意的
    —— 每节点包一次 try/finally 的代价高于收益。约束是"一旦抛出就不得再用
    这个 Board"，`think` 严格遵守：捕获后立即返回，不再落任何子。
    """


# ---- 连续冲四（VCF，Phase 5）----
#
# 主搜索看不见的东西：一条 8-12 手的**连续冲四**杀。它每一步都是绝对强制
# （对手不挡就立刻成五），所以杀棋的长度与搜索深度无关 —— 深度 24 在第 12 层
# 剪掉的那条线，VCF 能一路走到底。这就是 plan §4 说的"短搜索看不见的多步
# 连续冲四杀"。
#
# **三态是这套东西的全部要点**（修 B15）：
#
#   VCF_WIN        证明存在一条对手挡不住的连续冲四
#   VCF_NO_WIN     证明**不存在**（所有起手、所有应手都枚举完了）
#   VCF_EXHAUSTED  预算用完了，**没有结论**
#
# 把 EXHAUSTED 当成 NO_WIN 是本仓库最贵的一个 bug 形态：旧引擎的
# `_find_forced_win` 正是这么写的，"搜不完"于是被读成"没有必胜"，一条更慢
# 的杀棋被判为不存在。调用方只能这样用：**只有 WIN 可以据此落子**，
# NO_WIN 只可用于排除，EXHAUSTED 一律"不表态"。
#
# AND/OR 语义同样在这里：**我的**着法是 OR（任一起手赢即赢），**对手的**
# 应手是 AND（所有应手都输才是赢）。旧实现（main.py:1344-1353）两边都用 OR
# —— 只要对手*某个*应手走完还"看起来能赢"就宣布必胜。那是"进攻方假必胜"
# 这一类反例的根源。
VCF_WIN = 1
VCF_NO_WIN = 0
VCF_EXHAUSTED = -1

VCF_MAX_PLY = 24            # 一条 VCF 最多 24 手（12 回合），覆盖实战杀棋

# `_vcf_defence` 的两个否定答案。**它们不是一回事**：NONE 是"候选集扫完了，
# 没有一手能破坏对手的 VCF"，UNKNOWN 是"预算用尽，没扫完"。混成一个返回值
# 会让调用方分不清"证明它没用"与"还没算"，于是日志里两种情况说同一句话 ——
# 诊断 `game_log_20260922_235240` 那局时正是被这句话绊住的。
#
# 注意 NONE **不等于"必败"**：候选集只是"对手可以用来做四的格点"，挡点可能
# 在集合之外，也可以靠反冲四解。所以它只能用来排序，不能拿它编一个杀棋分
# （见 `tests/test_vcf.py` 的 `test_vcf_defence_does_not_invent_a_score`）。
VCF_DEFENCE_NONE = -1       # 扫完了，没有一手能破坏它
VCF_DEFENCE_UNKNOWN = -2    # 预算用尽，没扫完
_VCF_POLL_MASK = 255        # 每 256 个 VCF 节点轮询一次预算
_VCF_NODE_CAP = 120000      # 一次 VCF 阶段的节点上限：时间之外的兜底闸门


class _VcfStop(Exception):
    """VCF 自己的预算（节点数 / 深度 / 独立时间）用完了 —— 与 `SearchAborted`
    刻意分开：前者是"这套方法算不完了"（结果是 EXHAUSTED），后者是"整个搜索
    该停了"（向上穿透到 `think`）。混成一个会让预算耗尽被当成取消。"""


# 难度档位。时间是**硬上限**而非目标值，`max_depth` 只是安全阀 —— 实际深度由
# 时间驱动。
#
# **低档的区分度来自深度上限，不来自缺失一个子系统。** 1 档曾经写成
# `vcf_budget=0.0`，那让整个 VCF 阶段被跳过：既算不出自己的冲四链，也看不见
# 对手的。后果不是"初级"，是**失明** —— 实测一局败局（`game_log_20260919_183202`）
# 里，人类黑方用一条 11 手 VCF 链取胜，而 1 档从头到尾没有任何机制能看见它。
# 更糟的是那 1.275 秒预算只够搜到第 3 层，而第 3 层对该局面的判断是**定性
# 错误**的（报 -560，实际已必败；第 4 层要 1.55 秒，报 -698910）。两个旋钮
# 于是必须一起调：3.0 秒给出的 2.55 秒预算，扣掉 VCF 最多 0.3 秒，主搜索仍余
# 2.25 秒 > 第 4 层所需的 1.55 秒。
#
# 代价要如实说：低档之间在开阔中盘会到达相近的深度，阶梯的真正分野是
# `max_depth` 4/10/24 —— 要在窄树上才拉得开。
#
# **2 档的 7.0 秒是被一个具体局面定出来的，不是拍的。** 那局
# （`game_log_20260922_235240`，2 档执白，人类第 31 手 J8 胜）的胜负手在第 4 手：
# 盘面只有三颗子时，第 4 层选 K9（−1000），第 5 层选 K7/K11（−170）。把这一手
# 钉死、其余着法仍用旧预算重放（`tools/pivot_ab.py --budget 5.0`）：走 K9 得到
# 黑胜·第 31 手 J8，而且**与日志逐手相同（31 手一手不差）**；走 K7 / K11 则是
# 白方第 12 / 18 手反杀。
#
# **旧预算的问题不是"差一点深度"，是压在临界点上。** 同一份输入重复搜这一手，
# 5.0 秒会给出两种答案（`--stability`）：空载 12 次里 3 次走 K9、另一批 8 次里
# 0 次，而起 6 个满载进程后 **4/4 全走 K9** —— 稍忙的机器上就固定下出那盘败局。
#
#     time=5.0（旧值）  预算 4250ms  K7(5层) 与 K9(4层) 交替 —— 随负载翻面
#     time=6.0          预算 5100ms  K7 · 5 层   5/5 稳定
#     time=7.0          预算 5950ms  K7 · 5 层   4/4 稳定
#
# 取 7.0 而不是 6.0：够不够得着第 5 层由 6.0 决定，7.0 在其上多留约 0.85 秒，
# 买的是"不随负载翻面"—— 这正是 5.0 出事的原因。
#
# **这是把地平线推远，不是治好了。** 任何固定预算都有地平线，下一个局面总会
# 有需要更深一层才看得见的棋。这条时限只保证"这一个局面看得见"，而且它是对着
# 一条人类着法线定的：换成同级引擎对手，白方并不因此就必胜。
DIFFICULTY = {
    1: dict(time=3.0, max_depth=4, vcf_budget=0.3, qply=4),
    2: dict(time=7.0, max_depth=10, vcf_budget=0.5, qply=8),
    3: dict(time=15.0, max_depth=24, vcf_budget=1.5, qply=10),
}
RESERVE = 0.15              # 留给回传与 UI 的余量，从时间上限里扣


class Engine:
    """对局级的搜索引擎：置换表跨步保留，搜索态每次 `think` 重置。

    一个 `Engine` 可以连服务一整局（`ai_move` 用的就是模块级的 `_ENGINE`），
    也可以每步新建一个。跨步保留 TT 是**刻意的**：同一局里前几步算过的子树
    在后续搜索中仍然有效，这是迭代加深最大的收益来源。跨**局**保留则是缺陷，
    所以 `new_game()` 会调用 `reset()`。
    """

    def __init__(self):
        self.tt = {}
        self.tt_age = 0
        self.history = [[0] * CELLS, [0] * CELLS]
        self.killers = [[-1, -1] for _ in range(MAX_PLY + 2)]
        self.nodes = 0
        self.qnodes = 0
        self.tt_hits = 0
        self._board = None
        self._me = 1
        self._deadline = 0.0
        self._cancel = None
        self._qply = 8
        self.timed_out = False
        self._vcf_nodes = 0
        self._vcf_node_cap = _VCF_NODE_CAP
        self._vcf_deadline = 0.0

    # ------------------------------------------------------------ 生命周期

    def reset(self):
        """清空跨局面状态。重开局入口（`new_game`）走这里。"""
        self.tt.clear()
        self.tt_age = 0
        self.history = [[0] * CELLS, [0] * CELLS]
        self.killers = [[-1, -1] for _ in range(MAX_PLY + 2)]

    def _poll(self):
        """时间/取消轮询。**只在 `_ABORT_MASK` 的整数倍处调用** —— 见 `_tick`。

        ``_deadline == 0.0`` 表示"当前没有搜索在跑"（`Engine.__init__` 的初值，
        以及 `think` 之外的独立调用）。不认这个约定的话，任何在 `think` 之外
        调用搜索路径的人——测试、`tools/` 里的探针——都会立刻撞上
        `SearchAborted("timeout")`，因为单调时钟当然大于 0。
        """
        if self._cancel is not None and self._cancel.is_set():
            raise SearchAborted("cancelled")
        if self._deadline > 0.0 and time.monotonic() >= self._deadline:
            self.timed_out = True
            raise SearchAborted("timeout")

    def _decay_history(self):
        """历史表整体减半。每轮根搜索前一次。

        不清零而衰减：清零会把"上一步学到的好着"一起丢掉，而跨步的着法偏好
        在开局到中局是连续有用的；衰减则让老信息自然过期。
        """
        for h in self.history:
            for i, v in enumerate(h):
                if v:
                    h[i] = v >> 1

    # ------------------------------------------------------------ 走法排序

    def _ordered_moves(self, bd, me, tt_move, ply):
        """按"最可能是最优着"的顺序返回候选落点。

        分四档，**档与档之间不混用同一套量纲**：置换表着法 → 己方威胁点 →
        对方的威胁点（必须应） → 杀手 / 历史。旧 `_order_moves` 是把静态分与
        历史分**相加**的，于是"访问次数多的平庸着"能压过"没访问过的杀着"；
        这里档位是主键，历史只在最后一档内排序。

        威胁点图 `_hot_points` 约 12µs/次，**只在真有活三以上棋型时才算** ——
        判断本身是 O(1) 的 `threat_power`。
        """
        cand = bd.cand_mask
        if not cand:
            return []
        opp = 3 - me

        head = []
        if tt_move >= 0 and (cand >> tt_move) & 1:
            head.append(tt_move)
            cand2 = cand & ~(1 << tt_move)
        else:
            cand2 = cand

        if bd.threat_power[me - 1] or bd.threat_power[opp - 1]:
            hot = _hot_points(bd.bits_of(me), bd.bits_of(opp)) & cand2
            block = _hot_points(bd.bits_of(opp), bd.bits_of(me)) & cand2 & ~hot
        else:
            hot = block = 0
        rest = cand2 & ~(hot | block)

        if hot:
            head += _mask_cells(hot)
        if block:
            head += _mask_cells(block)
        for k in self.killers[ply]:
            if k >= 0 and (rest >> k) & 1:
                head.append(k)
                rest &= ~(1 << k)

        tail = _mask_cells(rest)
        if tail:
            hist = self.history[me - 1]
            nc = bd.neighbor_count
            tail.sort(key=lambda i: (hist[i], nc[i]), reverse=True)
        return head + tail if head else tail

    def _record_cutoff(self, bd, me, mv, ply, depth):
        """记账 beta 截断的着法（供后续排序）。"""
        k = self.killers[ply]
        if k[0] != mv:
            k[1] = k[0]
            k[0] = mv
        self.history[me - 1][mv] += depth * depth

    # ------------------------------------------------------------ 置换表

    def _lookup(self, bd, me, depth, alpha, beta, ply):
        """返回 ``(value, tt_move, hit)``。`value` 仅在 `hit` 为真时有意义。

        ``tt_hits`` 只统计**真正被采用的**条目（`hit=True` 那三条出口），
        不是"键存在"。两者差得很远：条目深度不够、或界与当前窗口不相交时，
        这次查询只贡献了一个排序用的着法，搜索照样要展开 —— 把它算进
        "命中率"会让这个数看着很高而对实际剪枝量一无所知。这个数会显示在
        诊断里，所以它必须回答"省了多少功夫"，而不是"查表找到过多少东西"。
        """
        ent = self.tt.get(bd.hash ^ (0 if me == 1 else _TT_SALT_W))
        if ent is None:
            return 0, -1, False
        d, val, flag, mv = ent
        if d < depth:
            return 0, mv, False
        v = _from_tt(val, ply)
        if flag == TT_EXACT:
            self.tt_hits += 1
            return v, mv, True
        if flag == TT_LOWER and v >= beta:
            self.tt_hits += 1
            return v, mv, True
        if flag == TT_UPPER and v <= alpha:
            self.tt_hits += 1
            return v, mv, True
        return 0, mv, False

    def _save(self, bd, me, depth, value, flag, mv, ply):
        if len(self.tt) >= _TT_MAX:
            # 整体清空而不是 LRU：这里的记账成本高于收益，而条目重建只是
            # 多搜一点。清空的瞬间命中率掉一次，随即回升。
            self.tt.clear()
            self.tt_age += 1
        self.tt[bd.hash ^ (0 if me == 1 else _TT_SALT_W)] = (
            depth, _to_tt(value, ply), flag, mv)

    # ------------------------------------------------------------ 静止搜索

    def _quiesce(self, bd, alpha, beta, me, ply, qleft):
        """只展开**强制着法**的静止搜索（Phase 4）。

        所谓强制，是指"不走就立刻输"或"走了立刻赢"，只有两类：

          1. 成五点 —— 直接判胜，不递归（`|F| >= 1` 一步成五）。
          2. 对方的成五点 —— 必须挡。`|F| >= 2` 时挡一个漏一个，判负。
          3. 双方的四点 —— 成四 / 挡四。它们不是绝对强制，但棋型交换在这种
             层面发生，不展开就是"地平线效应"：搜索在对手活三刚成四的那一层
             被截断，评估看到的是"我还没输"，而实际已经输了。

        **`stand-pat` 只在没有成五威胁时才允许。** 对手有成五点时我必须有动作，
        此时若还允许"原地不动"取静态分，等于让搜索选一个现实中不存在的着法。

        `ply` 全局递增（不只是为了杀棋分归一化）—— `MAX_PLY` 是硬上限，
        静止搜索也必须计入，否则长将局会撑爆栈。
        """
        self.nodes += 1
        self.qnodes += 1
        if not (self.nodes & _ABORT_MASK):
            self._poll()
        if ply >= MAX_PLY - 1 or qleft <= 0:
            return evaluate(bd, me)

        opp = 3 - me

        # 没有活三以上棋型 ⇒ 双方都不存在四点，更不存在五点 ⇒ 没有任何强制
        # 着法可展开。这一句挡掉了绝大多数静止节点（那里成五判定的 3.2µs × 2
        # 与威胁图的 11µs × 2 都是纯开销），而且结论与往下走完全一致。
        if not (bd.threat_power[me - 1] or bd.threat_power[opp - 1]):
            return evaluate(bd, me)

        my_bits = bd.bits_of(me)
        op_bits = bd.bits_of(opp)

        my_five = _five_points(my_bits, op_bits)
        if my_five:
            return WIN_SCORE - ply          # 轮到我，且我能一步成五

        opp_five = _five_points(op_bits, my_bits)
        if opp_five:
            if opp_five.bit_count() > 1:
                # 两个成五点，挡一个漏一个 —— 必输，且**距离是已知的**：
                # 我在这里(ply)落子挡一个，对手在下个节点(ply+1)成五。
                # 分值与上面「我能成五」共用同一套 ply 约定（谁在 ply 这个
                # 节点上做成五，谁就拿 WIN_SCORE - ply），于是我的值取负号
                # 后是 -(WIN_SCORE - (ply + 1))。写成 -2 会让"必输"的
                # 距离少算一步，在"几条败线里挑最长的"时选错。
                return -(WIN_SCORE - ply - 1)
            cand = opp_five
            best = -INF                          # 禁止 stand-pat：必须挡
        else:
            # 双方都没有成五点，剩下的是四点交换（成四 / 挡四）。
            cand = (_hot_points(my_bits, op_bits)
                    | _hot_points(op_bits, my_bits))
            if not cand:
                return evaluate(bd, me)
            best = evaluate(bd, me)
            if best >= beta:
                return best
            if best > alpha:
                alpha = best

        for mv in _mask_cells(cand):
            if bd.make(mv, me):
                bd.unmake(mv, me)
                val = WIN_SCORE - ply
            else:
                val = -self._quiesce(bd, -beta, -alpha, opp, ply + 1, qleft - 1)
                bd.unmake(mv, me)
            if val > best:
                best = val
            if val > alpha:
                alpha = val
            if alpha >= beta:
                break
        return best

    # ------------------------------------------------------------ 主搜索

    def _negamax(self, bd, depth, alpha, beta, me, ply):
        """negamax + PVS + 置换表。

        搜索窗口内**必然没有五连**（成五的分支在上一层就已返回），因此这里
        不需要任何"检查终局"的动作 —— 这既是位棋盘增量胜负判定的前提，也是
        旧版每节点一次 `_check_win_fast`（全盘 1170 次单元读取，实测约 0.44ms
        /次，占旧引擎累计耗时 69-77%）被彻底去掉的原因。
        """
        self.nodes += 1
        if not (self.nodes & _ABORT_MASK):
            self._poll()

        if ply >= MAX_PLY - 1:
            return evaluate(bd, me)
        if depth <= 0:
            return self._quiesce(bd, alpha, beta, me, ply, self._qply)

        alpha_orig = alpha                # 必须在改动之前留存 —— B2 的根因
        val, tt_move, hit = self._lookup(bd, me, depth, alpha, beta, ply)
        if hit:
            return val

        moves = self._ordered_moves(bd, me, tt_move, ply)
        if not moves:
            return evaluate(bd, me)       # 满盘

        nxt = 3 - me
        best = -INF
        best_move = moves[0]
        for i, mv in enumerate(moves):
            if bd.make(mv, me):
                bd.unmake(mv, me)
                best = WIN_SCORE - ply    # 一步成五，不可能更好
                best_move = mv
                break
            if i == 0:
                v = -self._negamax(bd, depth - 1, -beta, -alpha, nxt, ply + 1)
            else:
                v = -self._negamax(bd, depth - 1, -alpha - 1, -alpha, nxt, ply + 1)
                if alpha < v < beta:
                    v = -self._negamax(bd, depth - 1, -beta, -alpha, nxt, ply + 1)
            bd.unmake(mv, me)
            if v > best:
                best = v
                best_move = mv
            if v > alpha:
                alpha = v
            if alpha >= beta:
                self._record_cutoff(bd, me, mv, ply, depth)
                break

        self._save(bd, me, depth, best, _flag_of(best, alpha_orig, beta),
                   best_move, ply)
        return best

    # ------------------------------------------------------------ 根节点

    def _root(self, bd, me, depth, first_move, alpha, beta):
        """根节点的一轮搜索。返回 ``(move, value)``。

        根与普通节点的区别只有一处：**不必为"某条线路被剪掉"负责**，因为根层
        要返回的永远是分值最高的那条路。所以这里不做 PVS 的零窗口试探 ——
        根层少一层间接，且根层节点数占总数的比例极小。
        """
        moves = self._ordered_moves(bd, me, first_move, 0)
        if not moves:
            return (-1, 0)
        nxt = 3 - me
        best = -INF
        best_move = moves[0]
        for mv in moves:
            if bd.make(mv, me):
                bd.unmake(mv, me)
                return (mv, WIN_SCORE)
            v = -self._negamax(bd, depth - 1, -beta, -alpha, nxt, 1)
            bd.unmake(mv, me)
            if v > best:
                best = v
                best_move = mv
            if v > alpha:
                alpha = v
            if alpha >= beta:
                break
        return (best_move, best)

    # ------------------------------------------------------------ 连续冲四

    def vcf(self, bd, me, budget_s, *, deadline=None):
        """在根局面 ``bd``（轮到 ``me``）上找一条**连续冲四**杀。

        返回 ``(state, move, dist)``：``state`` 取 `VCF_WIN` / `VCF_NO_WIN` /
        `VCF_EXHAUSTED`，``move`` 仅在 WIN 时有效（线性格索引），``dist`` 是从
        当前节点算起**还有几手落下那颗成五的子**（WIN 时有效）。

        **预算独立于主搜索**：`DIFFICULTY[level]['vcf_budget']` 秒，同时受主搜索
        的 `_deadline` 与 `cancel` 约束 —— 三者取先到的那个，所以 VCF 不可能
        把总时间拖出上限。

        ``deadline`` 用于**一次搜索里的多次 VCF 调用共享一个截止时刻**（防守
        搜索要逐个候选重跑 VCF，各自计时会让总时长等于"候选数 × 预算"）。
        传了它就意味着"预算由调用方统一管"，此时不重置节点计数。
        """
        now = time.monotonic()
        if deadline is None:
            self._vcf_nodes = 0
            self._vcf_node_cap = _VCF_NODE_CAP
            deadline = now + budget_s
        # 主搜索的截止时刻也约束 VCF，但**只在上一次 `think` 还没结束、或它的
        # 截止时刻本来就在未来时才生效**：`think` 返回后 `_deadline` 会留在
        # 过去，若照着它算，任何 `think` 之外的独立 VCF 调用（测试、工具）都会
        # 立刻 EXHAUSTED。
        if now < self._deadline:
            deadline = min(deadline, self._deadline)
        self._vcf_deadline = deadline
        try:
            return self._vcf(bd, me, 0, VCF_MAX_PLY)
        except _VcfStop:
            return VCF_EXHAUSTED, -1, 0

    def _vcf_tick(self):
        """VCF 的预算轮询：节点数 / 时间 / 取消。

        时间每 256 个节点才看一次表（`time.monotonic` 在热路径上不便宜），
        一次轮询之间跑掉的节点在 256 的量级 —— 单个 VCF 节点是"一次
        `_hot_points` + 几个位运算"，所以这个粒度约几毫秒，远小于预算本身。

        **`self._poll()` 会让 `SearchAborted` 穿透上去**（那是"整个搜索该停了"），
        而本地预算耗尽抛 `_VcfStop`（那是"这套方法算不完了"）。两者在
        `vcf()` 里被分开处理 —— 混成一个会让预算耗尽被当成取消。
        """
        self._vcf_nodes += 1
        # 第 1 个节点也查一次表：预算小到"一个节点都跑不完"时（测试里的
        # EXHAUSTED 场景、以及被主搜索压到毫秒级的剩余预算），等到第 256 个
        # 节点才检查意味着这几百个节点白跑。
        if self._vcf_nodes > 1 and self._vcf_nodes & _VCF_POLL_MASK:
            return
        self._poll()
        if (time.monotonic() >= self._vcf_deadline
                or self._vcf_nodes >= self._vcf_node_cap):
            raise _VcfStop

    def _vcf(self, bd, me, ply, left):
        """VCF 递归：``me`` 是**进攻方**（他落子）。

        返回值与 `vcf` 同构（``ply``/``left`` 只是内部递推参数）。三条返回
        路径的判据都在下面各自的注释里，这里只强调一件事：**进攻方节点上
        一旦对手有成五点，本节点立即 NO_WIN**，因为四压不住五 —— 我下一手
        做四，对手直接成五。这是"进攻方假必胜"（plan §4 的反例 b）唯一
        需要的一行判定。
        """
        self._vcf_tick()
        if left <= 0:
            raise _VcfStop

        opp = 3 - me
        my_bits = bd.bits_of(me)
        op_bits = bd.bits_of(opp)

        my_five = _five_points(my_bits, op_bits)
        if my_five:
            # 我这一手就成五。dist=1 与「再走一手落子」的字面意思一致。
            return VCF_WIN, _mask_cells(my_five)[0], 1
        if _five_points(op_bits, my_bits):
            return VCF_NO_WIN, -1, 0

        cand = _hot_points(my_bits, op_bits)
        if not cand:
            return VCF_NO_WIN, -1, 0      # 连四都做不出来 ⇒ 这条线无从谈起

        exhausted = False
        for mv in _mask_cells(cand):
            if bd.make(mv, me):
                bd.unmake(mv, me)
                return VCF_WIN, mv, 1
            try:
                f = _five_points(bd.bits_of(me), bd.bits_of(opp))
                if f.bit_count() >= 2:
                    # 活四 / 双四：两个成五点，对手只能堵一个。
                    # 距离是 3 —— 我(ply)成四，他(ply+1)堵一个，我(ply+2)成五。
                    return VCF_WIN, mv, 3
                if not f:
                    continue              # 这一手没做出四，不是 VCF 着法
                # 恰有一个成五点 ⇒ 对手的应手是**唯一**的（不堵就输），
                # 所以这里对"所有应手"的 AND 只剩一项。
                bidx = (f & -f).bit_length() - 1
                bd.make(bidx, opp)
                try:
                    st, _, sub = self._vcf(bd, me, ply + 2, left - 1)
                finally:
                    bd.unmake(bidx, opp)
                if st == VCF_WIN:
                    return VCF_WIN, mv, sub + 2
                if st == VCF_EXHAUSTED:
                    # **不当作 NO_WIN**：这条线的结论是"不知道"。记下来，
                    # 继续试别的起手 —— 别的起手若真的赢，WIN 仍然可信。
                    exhausted = True
            finally:
                bd.unmake(mv, me)

        return (VCF_EXHAUSTED if exhausted else VCF_NO_WIN), -1, 0

    def _vcf_defence(self, bd, me, opp, deadline):
        """对手存在 VCF 时，找一手能**彻底破坏**它的棋。

        候选集是"对手会用来起手做四的所有格点"（`_hot_points(opp, me)`）——
        对手的 VCF 只能从这些格子里起步，我占掉其中一格，那条线就没了。逐格
        试过去、每格都重跑一次对手的 VCF，要求结果是 `VCF_NO_WIN`
        （**`VCF_EXHAUSTED` 不算挡住**，那只是"没算完"）。

        `deadline` 是**整个 VCF 阶段**的绝对截止时刻，由 `think` 统一给出 ——
        若每个候选各自计时，总时长会变成"候选数 × 预算"，那正是 plan §风险里
        写的"VCF 防守候选集爆炸"。

        返回 ``-1`` 以上的索引表示找到；负数有两个取值，**含义不同**：

        - `VCF_DEFENCE_NONE`（-1）—— 候选集扫完了，没有一手能破坏它。
          **这不等于必败**：候选集只是"对手可以用来做四的格点"，挡点可能在
          集合之外，也可以靠反冲四解。所以它只能用来排序，不能拿它编杀棋分。
        - `VCF_DEFENCE_UNKNOWN`（-2）—— 预算用尽，没扫完。连"有没有挡点"
          都没算出来。

        两种情况下调用方都退回常规搜索。这里刻意**不实现** plan 里"以攻对攻，
        走己方 VCF 最深进展着法"那一条：主搜索（深度 24 + 静止搜索）对同一
        局面的判断严格强于"挑一条 VCF 走得最远的着法"这种启发式，旧引擎的
        "拼命模式"正是这类启发式的失败案例。
        """
        cand = _hot_points(bd.bits_of(opp), bd.bits_of(me))
        for mv in _mask_cells(cand):
            if time.monotonic() >= deadline:
                return VCF_DEFENCE_UNKNOWN
            bd.make(mv, me)
            try:
                st, _, _ = self.vcf(bd, opp, 0.0, deadline=deadline)
            finally:
                bd.unmake(mv, me)
            if st == VCF_NO_WIN:
                return mv
        return VCF_DEFENCE_NONE

    def think(self, board, me, level, *, cancel=None, time_limit=None):
        """搜索一步棋。``board`` 是 ndarray（**不会被修改**）。

        返回 ``(idx, info)``，``idx`` 是线性格索引；无合法着法时返回 ``(-1, info)``。

        时间是**硬上限**：超时那一轮的半成品结果被丢弃，返回上一轮完整迭代
        的结果 —— 半成品里"已搜完的分支比未搜完的多"，直接取用会让引擎在
        时间压力下走出比上一轮更差的着法。

        **无论正常返回还是抛异常，`_deadline` 与 `_cancel` 都会被清掉。**
        这不是洁癖：`_poll` 用 `_deadline > 0.0` 表示"当前有搜索在跑"，而
        `vcf()` 是公开 API，可以在 `think` 之外被调用（`tools/positions.py` 的
        `opp_vcf` 探针就是这样）。留着上一次搜索的截止时刻，那个时刻**早已
        过去**，于是独立调用的 `vcf()` 会在第一个节点上撞 `SearchAborted` ——
        异常在 `think` 内部被捕获，在外部没有任何人接，直接炸穿到调用方。
        清掉之后，独立调用只剩自己的预算与 `cancel` 约束，与文档一致。
        """
        try:
            return self._search(board, me, level,
                                cancel=cancel, time_limit=time_limit)
        finally:
            self._deadline = 0.0
            self._cancel = None

    def _search(self, board, me, level, *, cancel=None, time_limit=None):
        # 档位超出本表（4/5 = 高级/宗师）时**退到最强的一档**，而不是报错：
        # 这条路只在 C++ 引擎不可用时才会走到，而"降级"应当降成"慢但强"，
        # 不是降成"少搜几层"。``_LOCAL_MAX_LEVEL`` 由 `engine.py` 从本表推导
        # —— 两侧不再是各自写死的两个 3。
        cfg = DIFFICULTY.get(level) or DIFFICULTY[max(DIFFICULTY)]
        limit = cfg["time"] if time_limit is None else time_limit
        self._cancel = cancel
        self._qply = cfg["qply"]
        self.timed_out = False
        self.nodes = 0
        self.qnodes = 0
        self.tt_hits = 0
        self.tt_age += 1
        self._decay_history()

        t0 = time.monotonic()
        self._deadline = t0 + limit * (1.0 - RESERVE)

        bd = Board.from_array(board)
        self._board = bd
        self._me = me

        info = {'reason': 'PVS搜索', 'depth': 0, 'actual_depth': 0,
                'best_val': 0, 'time_ms': 0.0, 'nodes': 0, 'nps': 0,
                'score_type': 'static', 'vcf_state': None, 'vcf_dist': 0,
                'vcf_nodes': 0}
        moves = bd.candidates()
        if not moves:
            return -1, info

        best_move = moves[0]
        best_val = 0
        done = 0
        first = best_move

        # ---- VCF 快速通道（Phase 5）----
        #
        # VCF 的结论是**已证明**的，但它只回答"赢不赢"，不回答"多快"。因此
        # 它不直接落子，而是做两件事：把已证明的杀棋线记下来当**兜底结论**，
        # 再让主搜索照常跑一遍 —— 搜索若找到**更短**的杀棋就用搜索的（更短的
        # 杀棋更不容易在下棋过程中走错），找不到（线长于搜索深度、或搜索看到
        # 的静态分很糟）就用 VCF 的。两者都是可证的，不存在编造。
        #
        # 对手存在 VCF 时同理只改**走法排序**：VCF 能证明的只是"对手那条冲四
        # 链被破坏了"，这个局面的分值它一无所知，拿它当返回值就是编分数。
        vcf_budget = cfg.get("vcf_budget", 0.0)
        vcf_phase_deadline = t0 + vcf_budget
        vcf_win = None                   # (move, value) 已证明的必胜
        # 已证明能破坏对手 VCF 的一手。初值与 `VCF_DEFENCE_NONE` 同值 —— 两者
        # 都读作"没有可用的挡点"，唯一的消费者是下面那句 `vcf_defence >= 0`。
        vcf_defence = VCF_DEFENCE_NONE
        if vcf_budget > 0.0:
            try:
                vst, vmv, vdist = self.vcf(bd, me, vcf_budget)
                info['vcf_state'] = vst
                info['vcf_nodes'] = self._vcf_nodes
                if vst == VCF_WIN:
                    # dist 是"还有几手落下那颗成五的子"。按 ply 约定（在 ply
                    # 这个节点上成五 → WIN_SCORE - ply）：五落在 ply = dist-1。
                    vcf_win = (vmv, WIN_SCORE - (vdist - 1))
                    first = best_move = vmv
                    # reason 留给"兜底真的生效"那一刻改（见循环后）—— 这里只是
                    # 把 VCF 的着法排在第一位，最后用谁还没定。
                    info.update(vcf_dist=vdist)
                else:
                    opp = 3 - me
                    ost, _, odist = self.vcf(bd, opp, 0.0,
                                             deadline=vcf_phase_deadline)
                    if ost == VCF_WIN:
                        # 威胁有多深是**已经算出来的事实**，两个分支都记 ——
                        # 只在找到挡点时记，日志里就看不出"对手还有几手成杀"，
                        # 而诊断败局时缺的正是这个数。
                        info.update(vcf_dist=odist)
                        d = self._vcf_defence(bd, me, opp, vcf_phase_deadline)
                        if d >= 0:
                            # 只把它排到第一位，**不写 reason** —— 搜索完全可能
                            # 找到更好的着法（比如自己的杀棋），那时说"这是
                            # VCF 挡点"就是假话。reason 在循环后按最终着法补。
                            first = best_move = d
                            vcf_defence = d
                        elif d == VCF_DEFENCE_UNKNOWN:
                            # 没扫完 ≠ 没有挡点。这两种情况以前共用一个 -1，
                            # 日志里说同一句话，等于把"不知道"讲成了"算过了"。
                            info.update(reason='PVS搜索(对手有VCF·未算完)')
                        else:
                            # 扫完了、没找到。**仍然不是"必败"**，所以分值照旧
                            # 由搜索给（见 `_vcf_defence` 的 docstring）。
                            info.update(reason='PVS搜索(对手有VCF·无挡点)')
            except SearchAborted:
                # VCF 阶段撞上主搜索的截止/取消：改成"不表态"，照常进入迭代
                # 加深。这里**不能**把 EXHAUSTED 当 NO_WIN —— 那正是 B15。
                info['vcf_state'] = VCF_EXHAUSTED
                self.timed_out = False       # 交给下面迭代循环自己处理时间

        for depth in range(1, cfg["max_depth"] + 1):
            if depth < 3 or best_val <= -STATIC_MAX:
                aspiration = False
            else:
                aspiration = True
            try:
                if aspiration:
                    d = _ASPIRATION
                    val = best_val
                    mv = first
                    for _ in range(_ASP_FAILS):
                        mv, val = self._root(bd, me, depth, first,
                                             best_val - d, best_val + d)
                        if best_val - d < val < best_val + d:
                            break
                        d *= _ASP_WINDOW_MUL
                    else:
                        mv, val = self._root(bd, me, depth, first, -INF, INF)
                else:
                    mv, val = self._root(bd, me, depth, first, -INF, INF)
            except SearchAborted:
                break
            if mv < 0:
                break
            best_move, best_val, done, first = mv, val, depth, mv
            # 已找到必胜（或被将死）就不必再深搜：更深的迭代只会重复同一结论，
            # 却要花掉数倍时间。`is_mate` 的分带保证了它不会与静态分混淆。
            if is_mate(val):
                break

        # VCF 兜底：搜索没找到**至少一样快**的杀棋时，用 VCF 的结论。
        # 判据只需比较分值 —— 杀棋分随距离单调（越短越大），所以"搜索的分
        # 更小"就等价于"搜索的杀棋更慢、或搜索压根没看到杀棋（静态分上限
        # `STATIC_MAX` 比任何杀棋分都小）"。两条线都是可证的，取大的那个。
        if vcf_win is not None and best_val < vcf_win[1]:
            vmv, vval = vcf_win
            best_move, best_val = vmv, vval
            info['reason'] = '连续冲四(VCF)'
        elif vcf_defence >= 0 and best_move == vcf_defence:
            # 搜索最终选的正是 VCF 算出来的那个挡点。"挡点"是 VCF 证明的，
            # 分值仍是搜索给的 —— reason 只说走法的来路，不说分值的来路。
            info['reason'] = '连续冲四防守(VCF)'

        dt = (time.monotonic() - t0) * 1000.0
        info.update({
            'depth': done,
            'actual_depth': done,
            'best_val': best_val,
            'time_ms': dt,
            'nodes': self.nodes,
            'nps': int(self.nodes / (dt / 1000.0)) if dt > 0 else 0,
            'tt_hit_rate': (self.tt_hits / self.nodes) if self.nodes else 0.0,
            'qnode_ratio': (self.qnodes / self.nodes) if self.nodes else 0.0,
            'score_type': 'mate' if is_mate(best_val) else 'static',
        })
        return best_move, info


_ENGINE = Engine()


def check_win(board, player):
    """公开接口：检查玩家是否获胜。

    与搜索内部的增量判定共用 ``_has_five_bits`` —— 全局只有一份胜负判定实现，
    覆盖它即覆盖搜索内判定。
    """
    return _has_five_bits(_bits_from_array(board, player))


def win_line(board, player):
    """获胜连珠的坐标序列 ``[(r, c), ...]``（长连返回整段）；无五连返回 ``[]``。

    供 UI 在终局高亮那条五连。纯函数、零 Qt 依赖，与 ``check_win`` 共用
    ``_DIRS`` 四方向。只找**极大连续段**（段首的前驱要么出界、要么不是本方
    子 —— 前驱是对手子时该段仍是极大的，不能跳过），返回第一条长度 ≥5 的。
    对局在成五瞬间结束，所以终局时五连至多一条。
    """
    b = np.asarray(board)
    for dr, dc in _DIRS:
        for r in range(BOARD_SIZE):
            for c in range(BOARD_SIZE):
                pr, pc = r - dr, c - dc
                if (0 <= pr < BOARD_SIZE and 0 <= pc < BOARD_SIZE
                        and b[pr][pc] == player):
                    continue        # 前驱是本方子 → 不是极大段的段首
                if b[r][c] != player:
                    continue
                run = []
                rr, cc = r, c
                while (0 <= rr < BOARD_SIZE and 0 <= cc < BOARD_SIZE
                        and b[rr][cc] == player):
                    run.append((int(rr), int(cc)))
                    rr += dr
                    cc += dc
                if len(run) >= 5:
                    return run
    return []


def ai_move(board, ai_player, depth, cancel=None):
    """AI 主入口。``depth`` 是**难度档位**（1/2/3），与主界面三档对应。

    返回 ``(r, c, info)``。``info`` 含 reason / depth / actual_depth /
    best_val / time_ms / nodes / nps / tt_hit_rate / qnode_ratio 等诊断字段。

    **空盘**这一种情况走 `opening_move`（天元），不经搜索 —— 但**必须在搜索
    之前**：旧版把这一步放在 PVS 之后的一大段威胁检测里，而那段检测会先于
    开局判断返回，于是空盘时永远走不到那里。

    注意 `opening_move` 还处理"盘上仅对手一子"的情形，但这里用不到：白方
    面对黑方第一子时 `stone_count == 1`，走的是正规搜索。那个分支是
    `opening_move` 作为公开 API 的一部分（以及旧版 `_OPENING_BOOK` 想表达
    却因键格式不匹配而失效的着法），不在这条路径上。

    **开局库查表排在"空盘天元"之后。** 顺序是刻意的：空盘那一手是钉死的规则
    （README 与 `main.py:_ai_first_move` 都依赖它），排在后面就保证了**任何
    生成物都不可能改掉第一步** —— 哪怕将来有人给空盘也建了一条表项。
    两者都命中不了时才进搜索，与今天逐字相同。
    """
    t0 = time.monotonic()
    bd = Board.from_array(board)
    info = {'reason': 'PVS搜索', 'depth': 0, 'actual_depth': 0,
            'best_val': 0, 'time_ms': 0.0, 'nodes': 0, 'nps': 0}

    if bd.stone_count == 0:
        r, c = opening_move(board, ai_player)
        info.update({'reason': '开局库', 'threat_detail': 'empty',
                     'time_ms': (time.monotonic() - t0) * 1000.0})
        return (r, c, info)

    hit = book_lookup(board, ai_player)
    if hit is not None:
        idx, score, dep = hit
        r, c = divmod(idx, BOARD_SIZE)
        # 分值/层数原样上报（见 `book_lookup`）。`nodes` 与 `nps` 留 0 ——
        # 查表没有节点，填一个搜索规模的数字才是真撒谎；`best_val` 不同，
        # 它是这个局面的**真实评估**，只是不是本次算的。
        info.update({'reason': '开局库', 'best_val': int(score),
                     'depth': int(dep), 'actual_depth': int(dep),
                     'threat_detail': 'book',
                     'time_ms': (time.monotonic() - t0) * 1000.0})
        return (r, c, info)

    idx, info = _ENGINE.think(board, ai_player, depth, cancel=cancel)
    if idx < 0:
        idx = bd.candidates()[0]
    r, c = divmod(idx, BOARD_SIZE)
    info['time_ms'] = (time.monotonic() - t0) * 1000.0
    return (r, c, info)


def new_game():
    """清空跨局面的持久状态。

    现在这个入口是**幂等的且真的必要**：引擎状态活在 `_ENGINE` 实例里，重开局
    若不重置，上一局的置换表会继续服务新局 —— 棋型相同但局面不同的分支会因
    哈希碰撞概率上升而命中错误的杀棋分（这就是旧版"同一局面在冷/热 TT 下给出
    不同着法"的来源）。
    """
    _ENGINE.reset()

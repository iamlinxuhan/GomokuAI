"""
[FROZEN SNAPSHOT] 旧的 GomokuAI AI 引擎 —— 仅供新旧引擎 A/B 对比与回归测试。

来源: main.py 的行 1-1955（git d232fa7），逐字冻结，未做任何算法改动。
用途: tools/selfplay.py 与 tools/bench.py 作为"改前"基线。生产代码不引用本模块。

相对原文件仅做三处**非算法性**裁剪，目的是让它能在不 import Qt 的情况下被加载：
  1. 原 10-55 行的 torch/GPU 检测块 → 替换为 torch/F 名字占位。
     原版 GPU 路径本就从未生效：main.py 的 72-74 行无条件把 _gpu_type 覆盖回
     'cpu'、_device 覆盖为 None，使 _ensure_torch() 恒为 False。此处保留该行为。
  2. 原 56-69 行的 PyQt5 import → 删除（AI 段不引用任何 Qt 符号）。
  3. 原 88-196 行的 GameLogger 类 → 替换为仅含 coord_to_sgf 的桩类，
     该静态方法被 ai_move 的 top_moves 诊断信息调用，实现与原版逐字一致。
  4. 原 281-293 行的 QColor 常量 → 删除（AI 段不引用）。

注意: 本文件包含若干已知缺陷（评分量级倒挂、TT flag 错误、
_check_win_fast 全盘扫描等），**故意保留**以作为基线，请勿"顺手修复"。
"""

"""
五子棋游戏 - PyQt5 版本
AI: PVS+LMR + 固定数组置换表 + 专业权重棋型库 + 增强TSS(攻防) + GPU加速(可选) + 时间控制 + 开局库
"""
import sys
import random
import time
import math
import numpy as np
# ============================================================================
# [FROZEN] 原 main.py:10-55 的 torch/GPU 检测块已移除，见文件头说明。
# ============================================================================
torch = None
F = None


# ==================== PyTorch 设备 ====================
_device = None  # 由 try/except 设置
_torch_available_final = False
_gpu_type = 'cpu'  # 'cuda' | 'xpu' | 'cpu'


def _ensure_torch():
    """检查 PyTorch/GPU 是否真正可用（torch 已导入，此处仅返回状态）"""
    return _torch_available_final


def get_gpu_type():
    """返回当前 GPU 类型：'cuda', 'xpu', 或 'cpu'"""
    return _gpu_type


# ==================== 游戏日志系统 ====================
class GameLogger:
    """[FROZEN] 原 main.py:88-196 的日志类已裁剪，仅保留 AI 段引用的 coord_to_sgf。"""

    @staticmethod
    def coord_to_sgf(r, c):
        """行列号转棋谱坐标 (如 row=7,col=7 -> H8)"""
        col_letter = chr(ord('A') + c + (1 if c >= 8 else 0))  # 跳过I
        return f"{col_letter}{r + 1}"



# ==================== PyTorch 模式检测卷积核 ====================
# 4个方向 (水平, 垂直, 对角线, 反对角线) 的模式内核
# 用于 GPU 批量检测棋盘上的棋型
_pattern_kernels_cached = None

def _get_pattern_kernels():
    """创建/获取模式检测卷积核（延迟创建 + 延迟导入torch）。
    只在有GPU时才创建GPU张量，否则返回None表示不可用。"""
    global _pattern_kernels_cached
    if _pattern_kernels_cached is not None:
        return _pattern_kernels_cached
    
    # 无GPU时直接返回空
    if not _ensure_torch():
        _pattern_kernels_cached = {}  # 标记为已尝试但不可用
        return _pattern_kernels_cached

    def _make_hk(size):  # 1xN 水平核
        return torch.tensor([[[[1.0] * size]]], dtype=torch.float32)

    # === 水平方向 1xN 核 (N=3~7) + 变体 ===
    k_h7 = _make_hk(7); k_h6 = _make_hk(6); k_h5 = _make_hk(5)
    k_h4 = _make_hk(4); k_h3 = _make_hk(3); k_h2 = _make_hk(2)

    # 间隔跳活核
    k_gap1 = torch.tensor([[[[1, 0, 1, 1, 1]]]], dtype=torch.float32)
    k_gap2 = torch.tensor([[[[1, 1, 0, 1, 1]]]], dtype=torch.float32)
    k_gap3 = torch.tensor([[[[1, 1, 1, 0, 1]]]], dtype=torch.float32)

    # 反向变体
    k_h4r = torch.tensor([[[[1, 1, 1, 1, 0]]]], dtype=torch.float32)
    k_h3r = torch.tensor([[[[1, 1, 1, 0, 0]]]], dtype=torch.float32)
    k_h2r = torch.tensor([[[[1, 1, 0, 0, 0]]]], dtype=torch.float32)

    # === 垂直方向 (permute all horizontal) ===
    def _v(k): return k.permute(0, 1, 3, 2)
    k_v7, k_v6, k_v5 = _v(k_h7), _v(k_h6), _v(k_h5)
    k_v4, k_v3, k_v2 = _v(k_h4), _v(k_h3), _v(k_h2)
    k_v4r, k_v3r, k_v2r = _v(k_h4r), _v(k_h3r), _v(k_h2r)

    # === 对角线/反对角线 5x5, 7x7 ===
    def _diag(size):
        k = torch.zeros(1, 1, size, size, dtype=torch.float32)
        for i in range(size): k[0, 0, i, i] = 1
        return k
    def _adiag(size):
        k = torch.zeros(1, 1, size, size, dtype=torch.float32)
        for i in range(size): k[0, 0, i, size - 1 - i] = 1
        return k

    k_d7, k_d6, k_d5, k_d4, k_d3 = _diag(7), _diag(6), _diag(5), _diag(4), _diag(3)
    k_ad7, k_ad6, k_ad5, k_ad4, k_ad3 = _adiag(7), _adiag(6), _adiag(5), _adiag(4), _adiag(3)

    _pattern_kernels_cached = {
        # 标准线型核 (H/V)
        'h7': k_h7.to(_device), 'h6': k_h6.to(_device), 'h5': k_h5.to(_device),
        'h4': k_h4.to(_device), 'h4r': k_h4r.to(_device),
        'h3': k_h3.to(_device), 'h3r': k_h3r.to(_device),
        'h2': k_h2.to(_device), 'h2r': k_h2r.to(_device),
        'v7': k_v7.to(_device), 'v6': k_v6.to(_device), 'v5': k_v5.to(_device),
        'v4': k_v4.to(_device), 'v4r': k_v4r.to(_device),
        'v3': k_v3.to(_device), 'v3r': k_v3r.to(_device),
        'v2': k_v2.to(_device), 'v2r': k_v2r.to(_device),
        # 间隔跳活核 (H)
        'h_gap1': k_gap1.to(_device), 'h_gap2': k_gap2.to(_device),
        'h_gap3': k_gap3.to(_device),
        # 对角线核 (D/AD)
        'd7': k_d7.to(_device), 'd6': k_d6.to(_device), 'd5': k_d5.to(_device),
        'd4': k_d4.to(_device), 'd3': k_d3.to(_device),
        'ad7': k_ad7.to(_device), 'ad6': k_ad6.to(_device), 'ad5': k_ad5.to(_device),
        'ad4': k_ad4.to(_device), 'ad3': k_ad3.to(_device),
    }
    return _pattern_kernels_cached

# ==================== 常量 ====================
BOARD_SIZE = 19
CELL_SIZE = 34
MARGIN = 40
BOARD_PX = BOARD_SIZE * CELL_SIZE
WINDOW_W = BOARD_PX + MARGIN * 2 + 280  # 右侧面板
WINDOW_H = BOARD_PX + MARGIN * 2


# ==================== Zobrist 哈希 ====================
_zobrist_table = np.random.randint(0, 2**63, size=(2, BOARD_SIZE, BOARD_SIZE), dtype=np.uint64)
_zobrist_black_turn = np.random.randint(0, 2**63, dtype=np.uint64)


def zobrist_hash(board):
    """计算当前棋盘的 Zobrist 哈希值"""
    h = np.uint64(0)
    for i in range(BOARD_SIZE):
        for j in range(BOARD_SIZE):
            if board[i][j] == 1:
                h ^= _zobrist_table[0][i][j]
            elif board[i][j] == 2:
                h ^= _zobrist_table[1][i][j]
    return h


# ==================== PyTorch 棋盘评估引擎 ====================
def _board_to_torch(board, player):
    """将 numpy 棋盘转为 PyTorch 张量 (1, 2, 19, 19)
    channel 0 = player 棋子, channel 1 = 对手棋子"""
    opp = 1 if player == 2 else 2
    p_ch = (board == player).astype(np.float32)
    o_ch = (board == opp).astype(np.float32)
    t = np.stack([p_ch, o_ch], axis=0)[np.newaxis, ...]  # (1, 2, 19, 19)
    return torch.from_numpy(t).to(_device)

def _batch_to_torch(boards_p, boards_o):
    """批量转换：boards_p/boards_o 为 (N, 19, 19) numpy 数组，返回 (N, 2, 19, 19) tensor"""
    t = np.stack([boards_p, boards_o], axis=1)  # (N, 2, 19, 19)
    return torch.from_numpy(t.astype(np.float32)).to(_device)

def _eval_board_torch(board_tensor):
    """
    用 GPU 张量运算评估棋盘（方向核）。
    board_tensor: (1, 2, 19, 19), channel0=player, channel1=opponent.
    返回 (player_score, opponent_score) 标量。
    无GPU时回退到CPU评估。
    """
    kernels = _get_pattern_kernels()
    p_chan = board_tensor[:, 0:1, :, :]
    o_chan = board_tensor[:, 1:2, :, :]

    def _score_channel(ch):
        score = 0.0
        # === 水平/垂直线型核 (N=2~7) ===
        for n, thresh in [(7, 6.9), (6, 5.9), (5, 4.9), (4, 3.9), (3, 2.9), (2, 1.9)]:
            for prefix in ['h', 'v']:
                k = kernels.get(f'{prefix}{n}')
                if k is not None:
                    cnt = (F.conv2d(ch, k).squeeze() >= thresh).float().sum().item()
                    if n >= 5:
                        score += cnt * 100000000
                    elif n >= 4:
                        score += cnt * 500000
                    elif n >= 3:
                        score += cnt * 10000
                    else:
                        score += cnt * 200

        # === 反向变体 ===
        for k_name, thresh in [('h4r', 3.9), ('h3r', 2.9), ('h2r', 1.9),
                                ('v4r', 3.9), ('v3r', 2.9), ('v2r', 1.9)]:
            k = kernels.get(k_name)
            if k is not None:
                cnt = (F.conv2d(ch, k).squeeze() >= thresh).float().sum().item()
                score += cnt * 500000 if k.shape[-1] >= 4 else (cnt * 10000 if k.shape[-1] >= 3 else cnt * 200)

        # === 间隔跳活核 ===
        for gname, gw in [('h_gap1', 500000), ('h_gap2', 500000), ('h_gap3', 500000)]:
            k = kernels.get(gname)
            if k is not None:
                cnt = (F.conv2d(ch, k).squeeze() >= (k.numel() - 0.2)).float().sum().item()
                score += cnt * gw

        # === 对角线核 (N=3~7) ===
        for n in [7, 6, 5, 4, 3]:
            for prefix in ['d', 'ad']:
                k = kernels.get(f'{prefix}{n}')
                if k is not None:
                    cnt = (F.conv2d(ch, k).squeeze() >= (n - 0.1)).float().sum().item()
                    if n >= 5:
                        score += cnt * 100000000
                    elif n >= 4:
                        score += cnt * 500000
                    else:
                        score += cnt * 10000

        return score

    with torch.no_grad():
        return _score_channel(p_chan), _score_channel(o_chan)


def _batch_eval_moves(board, moves, player):
    """GPU 批量评估所有候选落子（方向核 × 2色 × N候选），专注有效棋型。
    无GPU时自动回退到CPU排序。""" 
    # 无torch/GPU时立即回退到纯CPU排序
    if not _ensure_torch() or _device.type not in ('cuda', 'xpu'):
        result = [(_quick_eval_move(board, r, c, player), r, c) for r, c in moves]
        result.sort(reverse=True)
        return result
    
    opp = 1 if player == 2 else 2
    n = len(moves)
    if n == 0:
        return []

    with torch.no_grad():
        base_p = (board == player).astype(np.float32)
        base_o = (board == opp).astype(np.float32)
        boards_p = np.tile(base_p, (n, 1, 1))
        boards_o = np.tile(base_o, (n, 1, 1))
        for i, (r, c) in enumerate(moves):
            boards_p[i, r, c] = 1.0
            boards_o[i, r, c] = 0.0

        tensor = _batch_to_torch(boards_p, boards_o)
        kernels = _get_pattern_kernels()
        scores = torch.zeros(n, dtype=torch.float32, device=_device)

        def _add_batch_conv(ch, k, weight):
            conv = F.conv2d(ch, k)
            max_val = conv.amax(dim=[1, 2, 3])
            hits = (max_val >= k.numel() - 0.2).float()
            scores.add_(hits * weight)

        p_ch = tensor[:, 0:1, :, :]
        o_ch = tensor[:, 1:2, :, :]

        for ch, mult in [(p_ch, 1.0), (o_ch, 0.95)]:
            # === 线型核 (H/V N=2~7) ===
            for prefix in ['h', 'v']:
                for n, w in [(7, 500000000), (6, 200000000), (5, 100000000),
                             (4, 500000), (3, 10000), (2, 200)]:
                    k = kernels.get(f'{prefix}{n}')
                    if k is not None:
                        _add_batch_conv(ch, k, w * mult)

            # === 反向变体 ===
            for kname, w in [('h4r', 500000), ('h3r', 10000), ('h2r', 200),
                              ('v4r', 500000), ('v3r', 10000), ('v2r', 200)]:
                k = kernels.get(kname)
                if k is not None:
                    _add_batch_conv(ch, k, w * mult)

            # === 间隔跳活核 ===
            for gname, gw in [('h_gap1', 500000), ('h_gap2', 500000), ('h_gap3', 500000)]:
                k = kernels.get(gname)
                if k is not None:
                    _add_batch_conv(ch, k, gw * mult)

            # === 对角线核 ===
            for n, w in [(7, 500000000), (6, 200000000), (5, 100000000),
                         (4, 500000), (3, 10000)]:
                for prefix in ['d', 'ad']:
                    k = kernels.get(f'{prefix}{n}')
                    if k is not None:
                        _add_batch_conv(ch, k, w * mult)

        # 中心加分（torch向量化）
        move_tensor = torch.tensor(moves, dtype=torch.float32, device=_device)
        center_dist = (move_tensor[:, 0] - 9).abs() + (move_tensor[:, 1] - 9).abs()
        scores.add_(torch.clamp(18 - center_dist, min=0) * 5)

    # 结合 CPU 复合棋型评估
    cpu_scores = scores.cpu().numpy()
    result = []
    for i in range(n):
        r, c = moves[i]
        quick_score = _quick_eval_move(board, r, c, player)
        combined = float(cpu_scores[i]) + quick_score
        result.append((combined, r, c))
    result.sort(reverse=True)
    return result


def _analyze_line(board, r, c, dr, dc, player):
    """分析一条线上某个位置的棋型，返回 (count, open_ends, has_jump)"""
    count = 1
    open_ends = 0
    has_jump = False

    # 正方向
    pos = 1
    while True:
        nr, nc = r + dr * pos, c + dc * pos
        if 0 <= nr < BOARD_SIZE and 0 <= nc < BOARD_SIZE:
            if board[nr][nc] == player:
                count += 1
                pos += 1
            elif board[nr][nc] == 0:
                open_ends += 1
                # 检查跳活
                nr2, nc2 = r + dr * (pos + 1), c + dc * (pos + 1)
                if 0 <= nr2 < BOARD_SIZE and 0 <= nc2 < BOARD_SIZE and board[nr2][nc2] == player:
                    has_jump = True
                break
            else:
                break
        else:
            break

    # 反方向
    pos = 1
    while True:
        nr, nc = r - dr * pos, c - dc * pos
        if 0 <= nr < BOARD_SIZE and 0 <= nc < BOARD_SIZE:
            if board[nr][nc] == player:
                count += 1
                pos += 1
            elif board[nr][nc] == 0:
                open_ends += 1
                nr2, nc2 = r - dr * (pos + 1), c - dc * (pos + 1)
                if 0 <= nr2 < BOARD_SIZE and 0 <= nc2 < BOARD_SIZE and board[nr2][nc2] == player:
                    has_jump = True
                break
            else:
                break
        else:
            break

    return count, open_ends, has_jump


def evaluate_board(board, ai_player):
    """评估棋盘局面（静态），正值对AI有利。使用专业权重 + 组合检测。"""
    human_player = 1 if ai_player == 2 else 2

    # 快速检查五连
    if _check_win_fast(board, ai_player):
        return 100000000
    if _check_win_fast(board, human_player):
        return -100000000

    # ★★★ D2修复: 删除盲目的活四50M返回 ★★★
    # 原代码: if _find_live_four_moves(board, ai_player): return 50000000
    # 问题: 这让PVS搜索中AI误以为"自己有活四=必胜"，无视对手的VCT反击
    # 正确做法: 让复合评估(ai_score - human_score * coef)自然处理
    # 活四在_SCORE_TABLE中已经是10M，远高于其他棋型，无需额外加权
    # 如果确实必胜（活四+对手无同级威胁），搜索自然会发现

    ai_score = _eval_player_composite(board, ai_player)
    human_score = _eval_player_composite(board, human_player)
    # ★ 修复：0.85 替代 0.95，减少AI对自身局面的过度悲观
    # 配合进攻激励增强，让AI在均势时不再偏向纯防守
    return ai_score - human_score * 0.85


# ==================== 专业棋型权重表（Gomocup参考） ====================
# 棋型权重表（与旧版Alpha-Beta Engine保持一致的量级）
# 关键：活四必须 >> 冲四 >> 活三 >> 眠三，量级差距决定搜索正确性
_SCORE_TABLE = {
    # 连五 / 成五
    (5, True, 0): 100000000,   # 成五
    (4, True, True): 10000000,   # 活四(双头) = 10M ★ 与旧版一致
    (4, True, False): 10000000,  # 活四 = 10M ★ 旧版同
    (4, False, True): 100000,    # 冲四(双头) = 100K ★ 旧版同
    (4, False, False): 10000,    # 冲四 = 10K ★ 旧版同
    (3, True, True): 1000000,    # 双活三 = 1M ★ 必胜级
    (3, True, False): 10000,     # 活三 = 10K ★ 旧版同
    (3, False, True): 1000,      # 双眠三 = 1K ★ 旧版同
    (3, False, False): 500,      # 眠三 = 500 ★ 旧版同
    (2, True, True): 1000,       # 双活二
    (2, True, False): 200,       # 活二 ★ 旧版同
    (2, False, False): 50,       # 眠二 ★ 旧版同
    (1, True, False): 10,        # 活一 ★ 旧版同
    (1, False, False): 1,        # 眠一 ★ 旧版同
}


def _get_line_score(count, open_ends, has_jump):
    """根据标准棋型返回单线分值（与旧版SCORE_MAP键格式一致）"""
    cnt = min(count, 5)
    is_live = (open_ends >= 2)  # 两端空 = 活
    key = (cnt, is_live, has_jump)
    if key in _SCORE_TABLE:
        return _SCORE_TABLE[key]
    # 回退：尝试非跳棋型
    fallback = (cnt, is_live, False)
    if fallback in _SCORE_TABLE:
        return _SCORE_TABLE[fallback]
    # 最终回退
    if cnt >= 5:
        return 100000000
    return _SCORE_TABLE.get((cnt, False, False), 0)


# ==================== 静态评估缓存 (eval_cache) ====================
_eval_cache = {}

def _cached_evaluate(board, ai_player):
    """带 Zobrist 缓存的静态评估（避免重复全盘扫描）"""
    h = zobrist_hash(board)
    if h in _eval_cache:
        return _eval_cache[h]
    val = evaluate_board(board, ai_player)
    if len(_eval_cache) > 1 << 18:  # 约26万条
        _eval_cache.clear()
    _eval_cache[h] = val
    return val


def _eval_player_composite(board, player):
    """专业权重复合适配评估单方局面（每个方向独立评估，避免遗漏交叉威胁）"""
    score = 0
    directions = [(1, 0), (0, 1), (1, 1), (1, -1)]
    # 使用 (r,c,dr,dc) 跟踪已评估的线段，避免同线重复但允许交叉方向
    line_evaluated = set()
    # 组合计数器
    live4_cnt = rush4_cnt = dead4_cnt = 0
    live3_cnt = sleep3_cnt = 0
    jump_live3_cnt = 0
    live2_cnt = 0

    for r in range(BOARD_SIZE):
        for c in range(BOARD_SIZE):
            if board[r][c] != player:
                continue
            for dr, dc in directions:
                # 同一线段、同一方向只评估一次
                line_key = (r, c, dr, dc)
                if line_key in line_evaluated:
                    continue
                count, open_ends, has_jump = _analyze_line(board, r, c, dr, dc, player)
                if count >= 5:
                    return 100000000
                if count >= 1:
                    # 标记整条线段上的所有位置（仅当前方向）
                    for k in range(count):
                        nr, nc = r + dr * k, c + dc * k
                        if 0 <= nr < BOARD_SIZE and 0 <= nc < BOARD_SIZE:
                            line_evaluated.add((nr, nc, dr, dc))
                    
                    s = _get_line_score(count, open_ends, has_jump)
                    center_dist = abs(r - 9) + abs(c - 9)
                    center_bonus = max(0, 18 - center_dist) * 0.03
                    score += int(s * (1 + center_bonus))

                    # 统计组合
                    if count >= 4 and open_ends >= 2:
                        live4_cnt += 1
                    elif count >= 4 and open_ends == 1:
                        rush4_cnt += 1
                    elif count >= 4 and open_ends == 0:
                        dead4_cnt += 1
                    elif count == 3 and open_ends >= 2 and not has_jump:
                        live3_cnt += 1
                    elif count == 3 and open_ends >= 2 and has_jump:
                        jump_live3_cnt += 1
                    elif count == 3 and open_ends == 1:
                        sleep3_cnt += 1
                    elif count == 2 and open_ends >= 2:
                        live2_cnt += 1

    # === 组合加成（对手无法同时防守）— 与新评分表量级一致 ===
    # 双活三 → 必胜级（1M）
    if live3_cnt + jump_live3_cnt >= 2:
        score += 1000000  # 双活三必胜
    # 冲四 + 活三 → 必胜级（5M）
    if rush4_cnt >= 1 and (live3_cnt + jump_live3_cnt) >= 1:
        score += 5000000
    # 双冲四 → 高危
    if rush4_cnt >= 2:
        score += 5000000
    # 双眠三
    if sleep3_cnt >= 2:
        score += 2000
    # 活三 + 眠三
    if (live3_cnt + jump_live3_cnt) >= 1 and sleep3_cnt >= 1:
        score += 15000
    # 冲四 + 活二
    if rush4_cnt >= 1 and live2_cnt >= 1:
        score += 50000

    return score


def _check_win_fast(board, player):
    """快速检查是否有五连"""
    # 横向
    for r in range(BOARD_SIZE):
        cnt = 0
        for c in range(BOARD_SIZE):
            cnt = cnt + 1 if board[r][c] == player else 0
            if cnt >= 5:
                return True
    # 纵向
    for c in range(BOARD_SIZE):
        cnt = 0
        for r in range(BOARD_SIZE):
            cnt = cnt + 1 if board[r][c] == player else 0
            if cnt >= 5:
                return True
    # 对角线 (方向: 右下)
    for r in range(BOARD_SIZE - 4):
        for c in range(BOARD_SIZE - 4):
            if all(board[r + k][c + k] == player for k in range(5)):
                return True
    # 对角线 (方向: 左下)
    for r in range(4, BOARD_SIZE):
        for c in range(BOARD_SIZE - 4):
            if all(board[r - k][c + k] == player for k in range(5)):
                return True
    return False


def check_win(board, player):
    """公开接口：检查玩家是否获胜"""
    return _check_win_fast(board, player)


# ==================== 杀手启发 & 历史启发 ====================
MAX_DEPTH = 12
_killer_moves = [[None, None] for _ in range(MAX_DEPTH)]  # 每层2个杀手走法

# 历史启发表：history[player][r][c] 记录该落子导致beta截断的次数
_history_table = np.zeros((2, BOARD_SIZE, BOARD_SIZE), dtype=np.int32)

def _record_killer(depth, r, c):
    """记录杀手走法（LRU风格：新杀手放第一位，旧的移到第二位）"""
    if _killer_moves[depth][0] == (r, c):
        return
    _killer_moves[depth][1] = _killer_moves[depth][0]
    _killer_moves[depth][0] = (r, c)

def _record_history(player, r, c, depth):
    """记录历史启发：用 depth^2 作为增量，越深截断越有价值"""
    _history_table[player - 1][r][c] += depth * depth

def _is_killer(depth, r, c):
    """检查是否为杀手走法"""
    k0, k1 = _killer_moves[depth]
    return (r, c) == k0 or (r, c) == k1

def _get_history(player, r, c):
    """获取历史启发分数（用于排序）"""
    return _history_table[player - 1][r][c]


# ==================== 置换表 (Transposition Table) ====================
TT_SIZE = 1 << 20  # 约100万条 (2^20)
TT_MASK = TT_SIZE - 1
# 固定数组：每个槽存储 (full_hash, depth, value, flag, best_move, age)
# flag: 0=EXACT, 1=UPPERBOUND, 2=LOWERBOUND
_transposition_table = [None] * TT_SIZE
_tt_age = 0  # 全局年龄计数器，用于年龄优先淘汰


def tt_store(hash_key, depth, value, flag, best_move):
    """存入置换表（深度优先 + 年龄优先覆盖策略）"""
    global _tt_age
    idx = int(hash_key) & TT_MASK
    entry = _transposition_table[idx]
    if entry is None or depth >= entry[1] or _tt_age - entry[5] > 10000:
        _transposition_table[idx] = (hash_key, depth, value, flag, best_move, _tt_age)


def tt_lookup(hash_key, depth, alpha, beta):
    """查询置换表 (直接数组索引，O(1))
    flag: 0=EXACT, 1=UPPERBOUND, 2=LOWERBOUND
    """
    idx = int(hash_key) & TT_MASK
    entry = _transposition_table[idx]
    if entry is None:
        return None, None, False
    stored_hash, stored_depth, stored_value, flag, best_move, _ = entry
    if stored_hash != hash_key:
        return None, None, False  # 哈希冲突
    if stored_depth >= depth:
        if flag == 0:  # EXACT
            return stored_value, best_move, True
        elif flag == 1 and stored_value <= alpha:  # UPPERBOUND
            return stored_value, best_move, True
        elif flag == 2 and stored_value >= beta:   # LOWERBOUND
            return stored_value, best_move, True
    return None, best_move, False  # 深度不够，但仍返回建议走法


def _generate_moves(board, around_only=True):
    """生成候选落子位置，优先考虑已有棋子周围2格"""
    if around_only:
        moves = set()
        for r in range(BOARD_SIZE):
            for c in range(BOARD_SIZE):
                if board[r][c] != 0:
                    for dr in range(-2, 3):
                        for dc in range(-2, 3):
                            nr, nc = r + dr, c + dc
                            if 0 <= nr < BOARD_SIZE and 0 <= nc < BOARD_SIZE and board[nr][nc] == 0:
                                moves.add((nr, nc))
        if moves:
            return list(moves)

    moves = []
    for r in range(BOARD_SIZE):
        for c in range(BOARD_SIZE):
            if board[r][c] == 0:
                moves.append((r, c))
    return moves


def _composite_eval(board, r, c, player):
    """
    棋型组合评估（评分与_SCORE_TABLE/旧版保持一致量级）:
    检测落子后在所有方向上的棋型组合。
    修复：活四=10M级，冲四=100K级，活三=10K级
    """
    directions = [(1, 0), (0, 1), (1, 1), (1, -1)]
    live4_cnt = rush4_cnt = dead4_cnt = 0
    live3_cnt = jump_live3_cnt = sleep3_cnt = 0
    live2_cnt = 0
    win = False

    for dr, dc in directions:
        count, open_ends, has_jump = _analyze_line(board, r, c, dr, dc, player)
        if count >= 5:
            win = True
            break
        if count >= 4 and open_ends >= 2:
            live4_cnt += 1
        elif count >= 4 and open_ends == 1:
            rush4_cnt += 1
        elif count >= 4:
            dead4_cnt += 1
        elif count == 3 and open_ends >= 2 and not has_jump:
            live3_cnt += 1
        elif count == 3 and open_ends >= 2 and has_jump:
            jump_live3_cnt += 1
        elif count == 3 and open_ends == 1:
            sleep3_cnt += 1
        elif count == 2 and open_ends >= 2:
            live2_cnt += 1

    if win:
        return 100000000

    # === 权重评分（与新 _SCORE_TABLE 一致） ===
    score = 0

    # 活四: 10M（必胜级）
    if live4_cnt >= 1:
        score += 10000000
    if live4_cnt >= 2:
        score += 50000000  # 双活四

    # 冲四: 10K~100K
    if rush4_cnt >= 1:
        score += 10000
    if rush4_cnt >= 2:
        score += 150000   # 双冲四

    # 眠四: 10K
    if dead4_cnt >= 1:
        score += 10000

    # 活三: 10K，跳活三: 同级
    if live3_cnt >= 1:
        score += 10000
    if jump_live3_cnt >= 1:
        score += 8000

    # 双活三 (含跳活三): 1M（必胜级）
    total_live3 = live3_cnt + jump_live3_cnt
    if total_live3 >= 2:
        score += 1000000

    # 冲四 + 活三 组合: 必胜
    if rush4_cnt >= 1 and total_live3 >= 1:
        score += 5000000

    # 眠三: 500
    if sleep3_cnt >= 1:
        score += 500
    if sleep3_cnt >= 2:
        score += 1000   # 双眠三

    # 活二: 200
    if live2_cnt >= 1:
        score += 200
    if live2_cnt >= 2:
        score += 1000   # 双活二

    # 冲四 + 活二
    if rush4_cnt >= 1 and live2_cnt >= 1:
        score += 50000

    return score


def _quick_eval_move(board, r, c, player):
    """快速评估单个落子的价值（用于启发式排序），进攻权重 > 防守权重。
    恢复旧版显式棋型评分逻辑，确保走法排序质量。"""
    score = 0
    directions = [(1, 0), (0, 1), (1, 1), (1, -1)]
    opp = 1 if player == 2 else 2

    # ===== 进攻评估（自己的棋型） =====
    for dr, dc in directions:
        count, open_ends, has_jump = _analyze_line(board, r, c, dr, dc, player)
        if count >= 5:
            return 100000000  # 直接五连，最高优先级
        if count == 4 and open_ends >= 2:
            score += 7000000   # 活四（必胜），与防守比=2.33x
        elif count == 4 and open_ends == 1:
            score += 700000    # 冲四，与防守比=2.33x
        elif count == 3 and open_ends >= 2:
            score += 80000     # 活三（下一步就活四），与防守比=2.67x
        elif count == 3 and open_ends == 1:
            score += 7000      # 眠三
        elif count == 2 and open_ends >= 2:
            score += 1500      # 活二
        elif count == 2 and open_ends == 1:
            score += 300       # 眠二
        elif count == 1 and open_ends >= 2:
            score += 50        # 活一

    # ===== 防守评估（堵对手的棋型） =====
    # 关键修复：防守分数不能超过同级别的进攻分数
    for dr, dc in directions:
        count, open_ends, has_jump = _analyze_line(board, r, c, dr, dc, opp)
        if count >= 5:
            score += 90000000   # 堵对手五连（仅次于自己五连）
        elif count == 4 and open_ends >= 2:
            score += 3000000    # 堵对手活四
        elif count == 4 and open_ends == 1:
            score += 300000     # 堵对手冲四
        elif count == 3 and open_ends >= 2:
            score += 30000      # 堵对手活三（低于自己活三的50000）
        elif count == 3 and open_ends == 1:
            score += 3000       # 堵对手眠三
        elif count == 2 and open_ends >= 2:
            score += 500        # 堵对手活二

    # 中心位置加分
    center_dist = abs(r - 9) + abs(c - 9)
    score += max(0, 18 - center_dist) * 5

    return score


def _order_moves(move_list, board, current_player, depth, hash_key=None, tt_best_move=None):
    """
    综合排序候选落子：TT最佳 → 杀手 → 历史 → 复合棋型评估。
    PVS 对排序质量要求极高，好的排序让零窗口搜索大概率成功。
    """
    scored = []
    for r, c in move_list:
        # 1. TT 首选
        if tt_best_move and (r, c) == tt_best_move:
            priority = 10000000000
        # 2. 杀手走法最高优先
        elif _is_killer(depth, r, c):
            priority = 5000000000
        else:
            # 3. 历史启发 + 复合棋型评估
            hist = _get_history(current_player, r, c)
            eval_score = _quick_eval_move(board, r, c, current_player)
            priority = hist + eval_score
        scored.append((priority, r, c))
    scored.sort(reverse=True)
    return scored


def alpha_beta(board, depth, alpha, beta, maximizing, ai_player, hash_key=None, ply=0):
    """
    PVS + 置换表 + LMR + 杀手/历史启发 + GPU 批量评估。

    核心改进：
    - PVS 零窗口搜索（第一个分支全窗口，后续零窗口）
    - LMR (Late Move Reduction)：靠后的走法减少搜索深度
      前3个不走法不减，4~8减1，9+减2（历史分高可豁免）
    - 置换表 O(1) 查询/存储
    - 深度 <= 3 时 GPU 批量排序
    """
    global _tt_age
    _tt_age += 1
    human_player = 1 if ai_player == 2 else 2

    # === 置换表查询 ===
    if hash_key is None:
        hash_key = zobrist_hash(board)
    cached_val, cached_move, hit = tt_lookup(hash_key, depth, alpha, beta)
    if hit and depth > 0:
        return cached_val

    # 终局判断
    if _check_win_fast(board, ai_player):
        return 10000000 + depth
    if _check_win_fast(board, human_player):
        return -10000000 - depth
    if depth == 0:
        return _cached_evaluate(board, ai_player)

    all_moves = _generate_moves(board)
    if not all_moves:
        return 0

    current_player = ai_player if maximizing else human_player

    # === 深层节点使用 GPU 批量评估加速（仅当GPU真正可用时） ===
    use_gpu_batch = (_ensure_torch() and _device.type in ('cuda', 'xpu')
                     and depth <= 3 and len(all_moves) >= 10)

    if use_gpu_batch:
        gpu_scores = _batch_eval_moves(board, all_moves, current_player)
        max_branch = 15   # GPU分支因子也收紧
        if len(gpu_scores) > max_branch:
            gpu_scores = gpu_scores[:max_branch]
        move_scores = gpu_scores
    else:
        move_scores = _order_moves(all_moves, board, current_player, depth, hash_key, cached_move)
        max_branch = 20 if depth <= 1 else 15   # 修复：与旧版一致
        if len(move_scores) > max_branch:
            move_scores = move_scores[:max_branch]

    # === LMR 参数（修复：对浅层搜索更保守） ===
    # 五子棋搜索深度较浅，LMR 过激会导致大部分走法几乎不搜
    LMR_FULL_DEPTH_MOVES = 5   # 前5个走法不减深度（增加从3→5）
    LMR_REDUCTION_1 = 1        # 后续走法最多减1层（移除减2层）
    LMR_THRESHOLD_1 = 999      # 不再区分更多减幅（统一只减1）
    LMR_MIN_DEPTH = 5          # 深度 >= 5 才启用LMR（从3→5，浅层不做LMR）

    best_move = None
    first_child = True
    move_index = 0

    if maximizing:
        best_val = float('-inf')
        for _, r, c in move_scores:
            board[r][c] = ai_player
            new_hash = hash_key ^ _zobrist_table[ai_player - 1][r][c]

            # === LMR: Late Move Reduction（修复：统一只减1层） ===
            if not first_child and depth >= LMR_MIN_DEPTH and move_index >= LMR_FULL_DEPTH_MOVES:
                # 统一减1层（不再区分更多减幅）
                reduction = LMR_REDUCTION_1
                # 历史分数高的走法减幅豁免（好棋值得深搜）
                hist = _get_history(ai_player, r, c)
                if hist > 50 * depth:
                    reduction = 0
                reduced_depth = max(1, depth - 1 - reduction)

                # 零窗口 + 降深度搜索
                val = alpha_beta(board, reduced_depth, alpha, alpha + 1, False, ai_player, new_hash, ply + 1)
                if val > alpha:
                    # 降深度搜索发现好棋 → 全深度重搜
                    val = alpha_beta(board, depth - 1, alpha, beta, False, ai_player, new_hash, ply + 1)
            elif first_child:
                # 第一个分支：全窗口搜索
                val = alpha_beta(board, depth - 1, alpha, beta, False, ai_player, new_hash, ply + 1)
                first_child = False
            else:
                # PVS 零窗口
                val = alpha_beta(board, depth - 1, alpha, alpha + 1, False, ai_player, new_hash, ply + 1)
                if alpha < val < beta:
                    val = alpha_beta(board, depth - 1, val, beta, False, ai_player, new_hash, ply + 1)

            board[r][c] = 0

            if val > best_val:
                best_val = val
                best_move = (r, c)
            alpha = max(alpha, val)
            move_index += 1
            if beta <= alpha:
                _record_killer(depth, r, c)
                _record_history(ai_player, r, c, depth)
                break
        flag = 2 if best_val >= beta else (0 if best_val > float('-inf') else 1)
    else:
        best_val = float('inf')
        for _, r, c in move_scores:
            board[r][c] = human_player
            new_hash = hash_key ^ _zobrist_table[human_player - 1][r][c]

            # === LMR: Late Move Reduction (minimizing side) ===
            if not first_child and depth >= LMR_MIN_DEPTH and move_index >= LMR_FULL_DEPTH_MOVES:
                reduction = LMR_REDUCTION_1
                hist = _get_history(human_player, r, c)
                if hist > 50 * depth:
                    reduction = 0
                reduced_depth = max(1, depth - 1 - reduction)

                val = alpha_beta(board, reduced_depth, beta - 1, beta, True, ai_player, new_hash, ply + 1)
                if val < beta:
                    val = alpha_beta(board, depth - 1, alpha, beta, True, ai_player, new_hash, ply + 1)
            elif first_child:
                val = alpha_beta(board, depth - 1, alpha, beta, True, ai_player, new_hash, ply + 1)
                first_child = False
            else:
                val = alpha_beta(board, depth - 1, beta - 1, beta, True, ai_player, new_hash, ply + 1)
                if beta - 1 < val < beta:
                    val = alpha_beta(board, depth - 1, alpha, val, True, ai_player, new_hash, ply + 1)

            board[r][c] = 0

            if val < best_val:
                best_val = val
                best_move = (r, c)
            beta = min(beta, val)
            move_index += 1
            if beta <= alpha:
                _record_killer(depth, r, c)
                _record_history(human_player, r, c, depth)
                break
        flag = 1 if best_val <= alpha else (0 if best_val < float('inf') else 2)

    # 存入置换表
    if best_move:
        tt_store(hash_key, depth, best_val, flag, best_move)

    return best_val


def _count_line(board, r, c, dr, dc, player):
    """计算(r,c)位置在(dr,dc)方向上player的连续棋子数（不含落子本身）"""
    cnt = 0
    for k in range(1, 5):
        nr, nc = r + dr * k, c + dc * k
        if 0 <= nr < BOARD_SIZE and 0 <= nc < BOARD_SIZE and board[nr][nc] == player:
            cnt += 1
        else:
            break
    for k in range(1, 5):
        nr, nc = r - dr * k, c - dc * k
        if 0 <= nr < BOARD_SIZE and 0 <= nc < BOARD_SIZE and board[nr][nc] == player:
            cnt += 1
        else:
            break
    return cnt


def _find_winning_moves(board, player):
    """查找所有能让player直接五连获胜的位置"""
    winning = []
    moves = _generate_moves(board)
    for r, c in moves:
        board[r][c] = player
        if _check_win_fast(board, player):
            winning.append((r, c))
        board[r][c] = 0
    return winning


def _analyze_live_four(board, r, c, dr, dc, player):
    """
    精确检查在(r,c)落子后，沿(dr,dc)方向是否形成真正的活四。
    
    活四定义：在一条直线上恰好有4连子，且两端紧邻位置均为空位，
    这样对手无论堵哪一端，下一步都能形成五连。
    
    返回 True/False。
    """
    opp = 1 if player == 2 else 2
    
    # Step 1: 从落子位置向正方向扫描，找到连续player棋子最远端
    pos_max = 0
    for k in range(1, 6):
        nr, nc = r + dr * k, c + dc * k
        if 0 <= nr < BOARD_SIZE and 0 <= nc < BOARD_SIZE and board[nr][nc] == player:
            pos_max = k
        else:
            break
    
    # Step 2: 从落子位置向反方向扫描，找到连续player棋子最远端
    neg_max = 0
    for k in range(1, 6):
        nr, nc = r - dr * k, c - dc * k
        if 0 <= nr < BOARD_SIZE and 0 <= nc < BOARD_SIZE and board[nr][nc] == player:
            neg_max = k
        else:
            break
    
    # 总连续棋子数 = 正方向 + 反方向 + 落子自身
    total = pos_max + neg_max + 1
    
    # 必须恰好4连（或更多，但活四特指4连）
    if total < 4:
        return False
    if total > 4:
        # 5连及以上已经是赢了，由 _check_win_fast 处理
        # 这里只关心真正的活四
        return False
    
    # Step 3: 检查两端紧邻位置是否都是空位
    # 正方向端点：从最远的连续棋子再往外一格
    nr_pos = r + dr * (pos_max + 1)
    nc_pos = c + dc * (pos_max + 1)
    pos_empty = (0 <= nr_pos < BOARD_SIZE and 0 <= nc_pos < BOARD_SIZE and board[nr_pos][nc_pos] == 0)
    
    # 反方向端点：从最远的连续棋子再往外一格
    nr_neg = r - dr * (neg_max + 1)
    nc_neg = c - dc * (neg_max + 1)
    neg_empty = (0 <= nr_neg < BOARD_SIZE and 0 <= nc_neg < BOARD_SIZE and board[nr_neg][nc_neg] == 0)
    
    return pos_empty and neg_empty


def _has_live_four_after_move(board, r, c, player):
    """
    检查在(r,c)落子后，player是否形成活四。
    在4个方向上分别检查。
    """
    board[r][c] = player
    directions = [(1, 0), (0, 1), (1, 1), (1, -1)]
    result = any(_analyze_live_four(board, r, c, dr, dc, player) for dr, dc in directions)
    board[r][c] = 0
    return result


def _find_live_four_moves(board, player):
    """查找所有能形成活四的位置"""
    live4_moves = []
    moves = _generate_moves(board)
    for r, c in moves:
        if _has_live_four_after_move(board, r, c, player):
            live4_moves.append((r, c))
    return live4_moves


def _get_pattern_types(board, r, c, player):
    """
    获取在(r,c)落子后，player在四个方向上形成的棋型类型。
    返回包含: has_live4, has_rush4, has_live3, has_sleep3
    """
    directions = [(1, 0), (0, 1), (1, 1), (1, -1)]
    board[r][c] = player
    has_live4 = False
    has_rush4 = False
    has_live3 = False
    has_sleep3 = False

    for dr, dc in directions:
        count, open_ends, _ = _analyze_line(board, r, c, dr, dc, player)
        if count >= 5:
            board[r][c] = 0
            return True, True, True, True  # 五连
        if count == 4 and open_ends >= 2:
            has_live4 = True
        elif count == 4 and open_ends == 1:
            has_rush4 = True
        elif count == 3 and open_ends >= 2:
            has_live3 = True
        elif count == 3 and open_ends == 1:
            has_sleep3 = True

    board[r][c] = 0
    return has_live4, has_rush4, has_live3, has_sleep3


def _find_forced_win(board, player, max_depth=6):
    """
    增强 Threat-Space Search: 搜索进攻 + 防守强制获胜序列（VCF/VCT）。
    
    改进：
    - 深度提升到 6（可配合时间控制到 8）
    - 启发式剪枝：只搜索产生新威胁的走法（冲四/活四/活三）
    - 防守 TSS：对手有威胁时搜索防守路线
    """
    import time as _tss_time

    opp = 1 if player == 2 else 2
    _tss_start = _tss_time.time()
    _tss_limit = 2.0  # TSS 上限 2 秒
    _tss_visited = set()
    _tss_node_count = [0]
    _TSS_MAX_NODES = 500000

    def _has_new_threat(board, r, c, p):
        """检查(r,c)落子后是否产生新威胁（冲四/活四/活三）"""
        board[r][c] = p
        has_live4, has_rush4, has_live3, _ = _get_pattern_types(board, r, c, p)
        board[r][c] = 0
        return has_live4 or has_rush4 or has_live3

    def _tss_endpoints(board, r, c, p):
        """找到(r,c)处p棋子形成的4+连子的所有空位端点（对手必堵位置）"""
        ends = set()
        for dr, dc in [(1, 0), (0, 1), (1, 1), (1, -1)]:
            pos_cnt = 1
            for k in range(1, 6):
                nr, nc = r + dr * k, c + dc * k
                if 0 <= nr < BOARD_SIZE and 0 <= nc < BOARD_SIZE and board[nr][nc] == p:
                    pos_cnt += 1
                else:
                    break
            neg_cnt = 1
            for k in range(1, 6):
                nr, nc = r - dr * k, c - dc * k
                if 0 <= nr < BOARD_SIZE and 0 <= nc < BOARD_SIZE and board[nr][nc] == p:
                    neg_cnt += 1
                else:
                    break
            total = pos_cnt + neg_cnt - 1
            if total >= 4:
                nr1, nc1 = r + dr * pos_cnt, c + dc * pos_cnt
                if 0 <= nr1 < BOARD_SIZE and 0 <= nc1 < BOARD_SIZE and board[nr1][nc1] == 0:
                    ends.add((nr1, nc1))
                nr2, nc2 = r - dr * neg_cnt, c - dc * neg_cnt
                if 0 <= nr2 < BOARD_SIZE and 0 <= nc2 < BOARD_SIZE and board[nr2][nc2] == 0:
                    ends.add((nr2, nc2))
        return ends

    def _tt(board_tuple, player, depth):
        nonlocal _tss_start, _tss_limit, _tss_visited, _tss_node_count

        if depth == 0:
            return None
        _tss_node_count[0] += 1
        if _tss_node_count[0] > _TSS_MAX_NODES:
            return None
        if _tss_time.time() - _tss_start > _tss_limit:
            return None

        key = (board_tuple, player, depth)
        if key in _tss_visited:
            return None
        _tss_visited.add(key)

        board = np.array(board_tuple, dtype=int).reshape(BOARD_SIZE, BOARD_SIZE)
        moves = _generate_moves(board)

        # 启发式：只搜索威胁走法
        threat_moves = []
        for r, c in moves:
            if _has_new_threat(board, r, c, player):
                threat_moves.append((r, c))

        if not threat_moves:
            return None

        # 排序：优先尝试能直接五连的走法
        threat_moves.sort(key=lambda m: _quick_eval_move(board, m[0], m[1], player), reverse=True)

        for r, c in threat_moves:
            if _tss_time.time() - _tss_start > _tss_limit:
                return None

            board[r][c] = player
            if _check_win_fast(board, player):
                board[r][c] = 0
                return [(r, c)]

            defense_moves = _tss_endpoints(board, r, c, player)
            if not defense_moves:
                board[r][c] = 0
                continue

            for def_r, def_c in defense_moves:
                if board[def_r][def_c] != 0:
                    continue
                board[def_r][def_c] = opp
                new_tuple = tuple(board.flatten())
                result = _tt(new_tuple, player, depth - 1)
                board[def_r][def_c] = 0
                if result is not None:
                    board[r][c] = 0
                    return [(r, c)] + result

            board[r][c] = 0
        return None

    board_tuple = tuple(board.flatten())
    return _tt(board_tuple, player, max_depth)


def _check_immediate_threat(board, player):
    """
    紧急威胁检测（修复S1: 与旧版对齐，只处理真正imminent的威胁）：
    
    旧版只有4项检测，新版之前有7项(含深层TSS)，TSS抢先触发导致AI走低效的长路径。
    
    现在的策略：
    1. 自己能五连 → 直接赢（总是正确）
    2. 对手能五连 → 必须堵（总是正确）
    3. 自己能形成活四（必胜局面，2步内赢）→ 走这里
    4. 对手能形成活四 → 必须堵（必须防守）
    
    注意：TSS/VCF/双活三等深层策略交给主搜索(alpha_beta)处理，
         不在威胁检测阶段抢先返回，避免走低效长路径。
    """
    opp = 1 if player == 2 else 2

    # 1. 自己能否直接五连
    my_win = _find_winning_moves(board, player)
    if my_win:
        return my_win[0]

    # 2. 对手能否直接五连（必须堵）
    opp_win = _find_winning_moves(board, opp)
    if opp_win:
        return opp_win[0]

    # 3. 自己能否形成活四（必胜局面，下一步成五）
    my_live4 = _find_live_four_moves(board, player)
    if my_live4:
        return my_live4[0]

    # 4. 对手能否形成活四（必须提前堵，否则对手下一步成五）
    opp_live4 = _find_live_four_moves(board, opp)
    if opp_live4:
        return opp_live4[0]

    # ★★ 修复速亡：检测2-3步内的必胜/必败棋型 ★★
    # 这些不是深层TSS（不会返回长路径），而是真正的近端威胁
    moves = _generate_moves(board)

    # 5. 自己能否形成双活三或冲四+活三（2步内必胜）→ 进攻
    for r, c in moves:
        board[r][c] = player
        live3_dirs = []
        rush4_dirs = []
        for dr, dc in [(1, 0), (0, 1), (1, 1), (1, -1)]:
            count, open_ends, _ = _analyze_line(board, r, c, dr, dc, player)
            if count == 3 and open_ends >= 2:
                live3_dirs.append((dr, dc))
            elif count >= 4:
                rush4_dirs.append((dr, dc))
        board[r][c] = 0

        # 双活三 / 双冲四 = 绝对必胜，直接走
        if len(live3_dirs) >= 2 or len(rush4_dirs) >= 2:
            return (r, c)
        # 冲四+活三 = 也几乎必胜
        if len(rush4_dirs) >= 1 and len(live3_dirs) >= 1:
            return (r, c)

    # 6. 对手能否形成双活三/冲四+活三 → 必须立即防守！
    best_defense = None
    best_def_score = float('-inf')
    
    for r, c in moves:
        board[r][c] = opp
        o_live3 = []
        o_rush4 = []
        for dr, dc in [(1, 0), (0, 1), (1, 1), (1, -1)]:
            count, open_ends, _ = _analyze_line(board, r, c, dr, dc, opp)
            if count == 3 and open_ends >= 2:
                o_live3.append((dr, dc))
            elif count >= 4:
                o_rush4.append((dr, dc))
        board[r][c] = 0
        
        threat_level = 0
        if len(o_live3) >= 2:
            threat_level = 100      # 双活三：最高危
        elif len(o_rush4) >= 2:
            threat_level = 90       # 双冲四：高危
        elif len(o_rush4) >= 1 and len(o_live3) >= 1:
            threat_level = 80       # 冲四+活三：高危

        if threat_level > 0:
            def_score = threat_level * 10000 + _quick_eval_move(board, r, c, player)
            if def_score > best_def_score:
                best_def_score = def_score
                best_defense = (r, c)

    if best_defense and best_def_score >= 800000:
        return best_defense

    # ★★ D3修复: 多线威胁检测（防"双杀"战术）★★
    # 人类常用的VCT战术：同时在两条线上发展，AI堵一条另一条就五连了
    # 检测对手是否有>=2条独立的发展中线路（活三/眠四/活二+开放端）
    opp_threat_lines = []  # list of (threat_score, blocking_positions)
    for r, c in moves:
        board[r][c] = opp
        line_info = []
        for dr, dc in [(1, 0), (0, 1), (1, 1), (1, -1)]:
            count, open_ends, _ = _analyze_line(board, r, c, dr, dc, opp)
            if count >= 4 and open_ends >= 1:
                line_info.append(('rush4', 90, dr, dc))
            elif count == 3 and open_ends >= 2:
                line_info.append(('live3', 70, dr, dc))
            elif count == 3 and open_ends == 1:
                line_info.append(('sleep3', 40, dr, dc))
            elif count == 2 and open_ends >= 2:
                line_info.append(('live2', 15, dr, dc))
        board[r][c] = 0
        if line_info:
            max_t = max(li[1] for li in line_info)
            opp_threat_lines.append((max_t, r, c, line_info))

    # 如果对手有多条高威胁线（双杀前兆），必须拦截最高威胁的
    if len(opp_threat_lines) >= 2:
        high_threats = [t for t in opp_threat_lines if t[0] >= 40]
        if len(high_threats) >= 2:
            # 对手有>=2条眠三以上的线 → 危险！选最紧急的堵
            opp_threat_lines.sort(key=lambda x: -x[0])
            best_block = opp_threat_lines[0]
            return (best_block[1], best_block[2])

    return None


# ==================== 开局库 (Opening Book) ====================
_OPENING_BOOK = {
    # 开局局面 fen (简化) → (r, c)
    # 空棋盘 → 天元
    "empty": (9, 9),
    # 天元黑子 → 斜三（常见的平衡开局）
    "c9,9_b1": (9, 8),   # 黑天元，白走旁边
    # 更多开局可在实战中收集
}

def _board_to_fen(board):
    """将棋盘转为简化FEN用于开局库查询"""
    stones = []
    for r in range(BOARD_SIZE):
        for c in range(BOARD_SIZE):
            if board[r][c] != 0:
                stones.append(f"{chr(97+c)}{r+1}_{board[r][c]}")
    if not stones:
        return "empty"
    return "c" + ",".join(sorted(stones))


def ai_move(board, ai_player, depth):
    """
    AI 主入口（v4 修复版 + 日志诊断）:
    1. 开局库命中 -> 直接返回
    2. 增强TSS 威胁检测（含防守TSS）
    3. 时间控制 + 迭代加深（确保搜完目标深度）
    4. Zobrist/置换表 + LMR + PVS 搜索

    返回: (r, c, info_dict) — info_dict 包含决策原因/评分/深度等诊断信息
    """

    _t0 = time.time()
    info = {'reason': 'unknown', 'depth': 0, 'actual_depth': 0,
            'best_val': 0, 'time_ms': 0}

    # === 开局库 ===
    stone_count = np.count_nonzero(board)
    if stone_count <= 6:
        fen = _board_to_fen(board)
        if fen in _OPENING_BOOK:
            r, c = _OPENING_BOOK[fen]
            info.update({'reason': '开局库', 'threat_detail': f'fen={fen}'})
            return (r, c, info)

    # === TSS 立即威胁检测 ===
    # ★★ E2修复：分层威胁响应 ★★
    # ★★ F2修复：多重威胁检测（解决25步速亡的多线牵制问题）★★
    #
    # 问题1（E2已修）: 旧版对所有威胁都early return，PVS整局不执行
    # 问题2（F2新增）: 即使Level-1只拦截五连/活四，对手仍可制造多个同时威胁
    #   日志证据(25步速亡): 步22堵K9(堵五连)→漏M12; 步24堵H8(堵五连)→漏N13
    #   根因: 每次Level-1拦截只堵一个位置，对手用多线牵制让AI顾此失彼
    #
    # 新策略:
    #   Level-1硬拦截（立即返回）：五连、活四 — 但先检查是否存在多重威胁
    #     如果对手有>=2个五连或>=2个活四 → 常规防守已无效，转为"以攻对攻"
    #   Level-2软建议（不返回！）：双活三/冲四+活三 — 交给PVS搜索综合判断
    threat = _check_immediate_threat(board, ai_player)
    threat_hint = None  # E2: Level-2威胁作为建议传给PVS，不跳过搜索
    if threat:
        r, c = threat
        my_win = _find_winning_moves(board, ai_player)
        opp_win = _find_winning_moves(board, 1 if ai_player == 2 else 2)
        my_live4 = _find_live_four_moves(board, ai_player)
        opp_live4 = _find_live_four_moves(board, 1 if ai_player == 2 else 2)

        # ★ F2: 多重威胁检测 — 在Level-1拦截前检查是否已被多线牵制
        # 对手同时有多个必杀位置时，常规防守必然失败
        multi_threat_crisis = len(opp_win) >= 2 or len(opp_live4) >= 2

        if multi_threat_crisis and my_win:
            # ★★ 多重威胁危机 + AI能赢 → 先赢为敬！★★
            info.update({'reason': '威胁检测', 'threat_detail': '多重危机-五连(赢)'})
            return (my_win[0][0], my_win[0][1], info)
        if multi_threat_crisis and my_live4:
            # ★★ 多重威胁危机 + AI有活四 → 走活四必胜路径！★★
            info.update({'reason': '威胁检测', 'threat_detail': '多重危机-活四(必胜)'})
            return (my_live4[0][0], my_live4[0][1], info)
        if multi_threat_crisis:
            # ★★ 多重威胁危机但AI无必杀 → 放弃防守，转为拼命进攻模式 ★★
            # 不再early return！让PVS搜索找最佳进攻路线
            threat_hint = (r, c, '多重危机-放弃防守转进攻')
            # 不要return！继续走PVS搜索路径

        # Level-1：真正的终局威胁 → 必须立即响应（非多重危机时）
        elif my_win and (r, c) in my_win:
            info.update({'reason': '威胁检测', 'threat_detail': '五连(赢)'})
            return (r, c, info)
        elif opp_win and (r, c) in opp_win:
            info.update({'reason': '威胁检测', 'threat_detail': '堵五连'})
            return (r, c, info)
        elif my_live4 and (r, c) in my_live4:
            info.update({'reason': '威胁检测', 'threat_detail': '活四(必胜)'})
            return (r, c, info)
        elif opp_live4 and (r, c) in opp_live4:
            info.update({'reason': '威胁检测', 'threat_detail': '堵活四'})
            return (r, c, info)

        # Level-2：双活三/冲四+活三等 → 不early return！作为建议给PVS
        if not threat_hint:
            threat_hint = (r, c, '双活三/冲四+活三')

    moves = _generate_moves(board)
    if not moves:
        info['reason'] = '无走法'
        return (9, 9, info)

    # === 开局前两手优化 ===
    if stone_count <= 1:
        if board[9][9] == 0:
            # 天元空闲 → 抢占中心（AI先手场景）
            info.update({'reason': '第一步-天元'})
            return (9, 9, info)
        else:
            # 天元已被人类占据 → 必须紧贴人类棋子阻挡
            # 只评估人类棋子四周8邻位，不跳到远处
            human_stone = None
            for rr in range(BOARD_SIZE):
                for cc in range(BOARD_SIZE):
                    if board[rr][cc] != 0:
                        human_stone = (rr, cc)
                        break
                if human_stone:
                    break
            if human_stone:
                hr, hc = human_stone
                nearby = []
                for dr in range(-1, 2):
                    for dc in range(-1, 2):
                        if dr == 0 and dc == 0:
                            continue
                        nr, nc = hr + dr, hc + dc
                        if 0 <= nr < BOARD_SIZE and 0 <= nc < BOARD_SIZE and board[nr][nc] == 0:
                            s = _quick_eval_move(board, nr, nc, ai_player)
                            nearby.append((s, nr, nc))
                if nearby:
                    nearby.sort(reverse=True)
                    best_s, r, c = nearby[0]
                    top3 = [(s, GameLogger.coord_to_sgf(rr, cc)) for s, rr, cc in nearby[:3]]
                    info.update({'reason': '紧贴人类第一步', 'best_val': best_s,
                                 'top_moves': top3})
                    return (r, c, info)
            # 兜底：评估全部候选走法
            best = None
            best_s = float('-inf')
            for r, c in moves:
                s = _quick_eval_move(board, r, c, ai_player)
                if s > best_s:
                    best_s = s
                    best = (r, c)
            if best:
                info.update({'reason': '响应第一步-最优', 'best_val': best_s})
            return best

    # 第二步优化：双方各1子后，优先在人类棋子周围而非远处
    if stone_count == 2:
        human_player = 1 if ai_player == 2 else 2
        # 找出人类棋子的8邻位空位
        human_nearby = set()
        for r in range(BOARD_SIZE):
            for c in range(BOARD_SIZE):
                if board[r][c] == human_player:
                    for dr in range(-1, 2):
                        for dc in range(-1, 2):
                            nr, nc = r + dr, c + dc
                            if 0 <= nr < BOARD_SIZE and 0 <= nc < BOARD_SIZE and board[nr][nc] == 0:
                                human_nearby.add((nr, nc))
        if human_nearby:
            # 在这些紧邻位置中评估最优
            best_s = float('-inf')
            best = None
            for r, c in human_nearby:
                s = _quick_eval_move(board, r, c, ai_player)
                if s > best_s:
                    best_s = s
                    best = (r, c)
            if best and best_s > 0:
                info.update({'reason': '紧贴人类-第二步', 'best_val': best_s})
                return (best[0], best[1], info)
        # 否则正常走搜索流程（fall through）

    # === 时间控制 & 搜索深度（修复S2: 加深搜索让AI看到更远的杀棋组合） ===
    _time_start = time.time()
    # S2修复：增加搜索深度，利用迭代加深+PVS+置换表优势
    # 目标：depth=2时搜4层，能看到"活三→活四→五连"的完整威胁链
    if depth == 1:
        _time_max = 1.5      # 简单: 1.5秒
        target_depth = 2     # 搜2层
    elif depth == 2:
        _time_max = 3.5      # 中级: 3.5秒（S2加深：原2层→4层）
        target_depth = 4     # 搜4层（能看到活三→冲四→五连的威胁链）
    else:
        _time_max = 8.0      # 高级: 8秒（S2加深：原4层→6层）
        target_depth = 6     # 搜6层
    _TIME_RESERVE = 0.25      # 缓冲时间

    # === 走法排序（E2+E3+E4修复: 智能攻防排序） ===
    human_player = 1 if ai_player == 2 else 2

    # D4: 扫描对手的发展中线路数量，动态调整防守权重
    opp_developing = 0
    # ★ E3: 记录对手的"发展中长线"（慢建杀线检测）
    # 问题：J列竖线每颗子单独不触发威胁检测，但整体是杀着
    # 方法：找出对手所有已有>=3子的方向线，标记其空位延伸点为"高优先拦截点"
    opp_long_lines = []  # list of (line_strength, set_of_blocking_positions)
    for r in range(BOARD_SIZE):
        for c in range(BOARD_SIZE):
            if board[r][c] == human_player:
                for dr, dc in [(1, 0), (0, 1), (1, 1), (1, -1)]:
                    count, open_ends, _ = _analyze_line(board, r, c, dr, dc, human_player)
                    if count >= 3 and open_ends >= 1:
                        opp_developing += (count * open_ends)
                        # E3: 记录这条线的延伸端点（潜在拦截位置）
                        endpoints = set()
                        # 向正方向找开放端
                        nr, nc = r + dr * count, c + dc * count
                        while 0 <= nr < BOARD_SIZE and 0 <= nc < BOARD_SIZE and board[nr][nc] == 0:
                            endpoints.add((nr, nc))
                            nr, nc = nr + dr, nc + dc
                            if not (0 <= nr < BOARD_SIZE and 0 <= nc < BOARD_SIZE) or board[nr][nc] != 0:
                                break
                        # 向反方向找开放端
                        nr, nc = r - dr * count, c - dc * count
                        while 0 <= nr < BOARD_SIZE and 0 <= nc < BOARD_SIZE and board[nr][nc] == 0:
                            endpoints.add((nr, nc))
                            nr, nc = nr - dr, nc - dc
                            if not (0 <= nr < BOARD_SIZE and 0 <= nc < BOARD_SIZE) or board[nr][nc] != 0:
                                break
                        line_str = count * 10 + open_ends  # 3子双开=32, 4子单开=41, etc.
                        opp_long_lines.append((line_str, endpoints))
                    elif count == 2 and open_ends >= 2:
                        opp_developing += 2

    # 基础进攻权重3x，当对手发展线多时提高防守权重
    # ★ E4修正：即使强防守模式也保留一定进攻能力（atk_wt最低=2）
    if opp_developing > 25:
        atk_wt, def_wt = 2, 4   # E4: 超强防守模式（对手多条高威胁线）
    elif opp_developing > 20:
        atk_wt, def_wt = 2, 3   # 强防守模式
    elif opp_developing > 12:
        atk_wt, def_wt = 2, 2   # 平衡模式
    else:
        atk_wt, def_wt = 3, 1   # 正常进攻模式

    # ★ E4: 反攻候选扫描 — 当对手发展线多时，寻找AI自己的进攻机会
    # 策略："最好的防守是进攻" — 如果AI能形成活三/冲四，迫使对手防守
    counter_attack_moves = set()
    if opp_developing > 15:  # 对手有明显多线发展时才启用反攻
        for r, c in moves:
            board[r][c] = ai_player
            for dr, dc in [(1, 0), (0, 1), (1, 1), (1, -1)]:
                count, open_ends, _ = _analyze_line(board, r, c, dr, dc, ai_player)
                # AI落子后能形成活三或更好 → 反攻候选
                if count >= 3 and open_ends >= 2:
                    counter_attack_moves.add((r, c))
                    break
                elif count >= 4 and open_ends >= 1:
                    counter_attack_moves.add((r, c))
                    break
            board[r][c] = 0

    # 构建E3的高优先拦截点集合
    high_priority_blocks = set()
    for _, endpoints in opp_long_lines:
        high_priority_blocks.update(endpoints)

    move_scores = []
    for r, c in moves:
        attack = _quick_eval_move(board, r, c, ai_player)
        defense = _quick_eval_move(board, r, c, human_player)
        score = attack * atk_wt + defense * def_wt

        # ★ E2: threat_hint 加成 — Level-2威胁建议位获得额外加权
        if threat_hint and (r, c) == (threat_hint[0], threat_hint[1]):
            score += 500000  # 显著提升但不垄断

        # ★ E3: 发展中长线拦截加成 — 在对手长线延伸点上加分
        if (r, c) in high_priority_blocks:
            score += 200000  # 中等加成，防止慢建杀线

        # ★ E4: 反攻候选加成 — 当被牵制时，AI自己的进攻点额外加分
        if (r, c) in counter_attack_moves:
            score += 300000  # 高于普通防御但低于必堵

        move_scores.append((score, r, c))
    move_scores.sort(reverse=True)
    max_branch_top = 15 if depth >= 2 else 12
    if len(move_scores) > max_branch_top:
        move_scores = move_scores[:max_branch_top]

    # === Zobrist 基础哈希 ===
    base_hash = zobrist_hash(board)

    # === 迭代加深 + 时间控制 ===
    best_move = move_scores[0][1], move_scores[0][2]
    prev_best_val = float('-inf')

    for cur_depth in range(1, target_depth + 1):
        # 时间检查：剩余时间不足时提前停止加深（但至少完成第1层）
        elapsed = time.time() - _time_start
        if elapsed > _time_max - _TIME_RESERVE and cur_depth > 1:
            break

        local_best_move = best_move
        best_val = float('-inf')

        # 上次最优放第一位（迭代加深最佳实践）
        iter_moves = []
        for score, r, c in move_scores:
            if (r, c) == best_move:
                iter_moves.insert(0, (score, r, c))
            else:
                iter_moves.append((score, r, c))

        for _, r, c in iter_moves:
            # 每走完一个顶层节点也检查时间
            if time.time() - _time_start > _time_max - _TIME_RESERVE:
                break

            board[r][c] = ai_player
            if _check_win_fast(board, ai_player):
                board[r][c] = 0
                info.update({'reason': '搜索-发现必胜', 'actual_depth': cur_depth,
                            'best_val': 9999999, 'time_ms': (time.time() - _t0) * 1000})
                return (r, c, info)

            new_hash = base_hash ^ _zobrist_table[ai_player - 1][r][c]
            val = alpha_beta(board, cur_depth - 1, float('-inf'), float('inf'), False, ai_player, new_hash, ply=1)
            board[r][c] = 0

            if val > best_val:
                best_val = val
                local_best_move = (r, c)

        best_move = local_best_move
        info['actual_depth'] = cur_depth
        info['best_val'] = best_val

        # 启发式提前停止：仅在较深搜索且值稳定时触发（修复：提高阈值和深度要求）
        if prev_best_val != float('-inf') and abs(best_val - prev_best_val) < 1000 and cur_depth >= 5:
            if time.time() - _time_start > _time_max * 0.7:
                break

        prev_best_val = best_val

        # 找到必胜路线（五连级=100M），提前结束
        # ★ 修复：阈值从9M提高到95M，防止活四(50M)误触发停止
        # 只有真正看到五连(100M)或接近五连时才提停，且至少搜2层
        if best_val > 95000000 and cur_depth >= 2:
            break

    # 收集前3候选走法用于日志
    top_moves = [(s, GameLogger.coord_to_sgf(r, c))
                 for s, r, c in move_scores[:3]]

    elapsed_ms = (time.time() - _t0) * 1000

    # ★ G2+G3: 拼命模式（PVS返回极低分时切换以攻对攻+紧急防守策略）
    # 日志证据(25步速亡): 步18 PVS返回val=-10000000，AI已知道自己要输了
    # 但之后步20/22/24仍走被动防守，最终被多线牵制致死
    #
    # 日志证据(31步速亡): 步30拼命模式选了M12(纯进攻)，但G7才是人类杀位！
    # 根因：旧版拼命模式只扫描AI自己的活三/冲四，完全不看对手威胁
    #
    # 修复：混合攻防 — 在进攻的同时，必须拦截对手的准杀位
    DESPERATION_THRESHOLD = -5000000
    if best_val <= DESPERATION_THRESHOLD:
        my_win = _find_winning_moves(board, ai_player)
        my_live4 = _find_live_four_moves(board, ai_player)
        human = 1 if ai_player == 2 else 2

        if my_win:
            info.update({'reason': '威胁检测', 'threat_detail': '拼命-五连(赢)',
                        'depth': target_depth, 'time_ms': elapsed_ms, 'top_moves': top_moves})
            return (my_win[0][0], my_win[0][1], info)
        if my_live4:
            info.update({'reason': '威胁检测', 'threat_detail': '拼命-活四(必胜)',
                        'depth': target_depth, 'time_ms': elapsed_ms, 'top_moves': top_moves})
            return (my_live4[0][0], my_live4[0][1], info)

        # ★ G3: 拼命模式中的紧急防守 — 扫描对手的所有准杀位
        # 准杀位定义：
        #   1. 对手落子后能形成活四的位置（opp_live4 moves）
        #   2. 对手落子后能形成五连的位置（opp_win moves）— 最优先！
        #   3. 对手已有的"发展中长线"(>=3子)的延伸空位
        opp_win = _find_winning_moves(board, human)
        opp_live4 = _find_live_four_moves(board, human)

        # 收集对手的准杀位（高优拦截点）
        critical_blocks = set()
        if opp_win:
            critical_blocks.update(opp_win)  # 五连位 — 最高优先级
        if opp_live4:
            critical_blocks.update(opp_live4)  # 活四位 — 极高优先级

        # 扫描对手的发展中长线延伸点（同E3逻辑）
        for r in range(BOARD_SIZE):
            for c in range(BOARD_SIZE):
                if board[r][c] == human:
                    for dr, dc in [(1, 0), (0, 1), (1, 1), (1, -1)]:
                        cnt, opens, _ = _analyze_line(board, r, c, dr, dc, human)
                        if cnt >= 3 and opens >= 1:
                            nr, nc = r + dr * (cnt + 1), c + dc * (cnt + 1)
                            if 0 <= nr < BOARD_SIZE and 0 <= nc < BOARD_SIZE and board[nr][nc] == 0:
                                critical_blocks.add((nr, nc))
                            nr2, nc2 = r - dr, c - dc
                            if 0 <= nr2 < BOARD_SIZE and 0 <= nc2 < BOARD_SIZE and board[nr2][nc2] == 0:
                                critical_blocks.add((nr2, nc2))

        # ★ G2: 混合攻防评分 — 进攻+防守综合评估
        best_desp_val = float('-inf')
        best_desp_move = best_move

        for r, c in [m[1:] for m in move_scores]:
            atk_score = _quick_eval_move(board, r, c, ai_player)
            def_score = _quick_eval_move(board, r, c, human)

            # 进攻检查：AI落子后能否形成强攻击
            board[r][c] = ai_player
            has_strong_threat = False
            for dr, dc in [(1, 0), (0, 1), (1, 1), (1, -1)]:
                cnt, opens, _ = _analyze_line(board, r, c, dr, dc, ai_player)
                if (cnt >= 3 and opens >= 2) or (cnt >= 4 and opens >= 1):
                    has_strong_threat = True
                    break
            board[r][c] = 0

            final_score = atk_score * 2 + def_score * 2  # 攻防并重

            # ★ G3核心：准杀位拦截加成 — 远超普通攻击
            if (r, c) in critical_blocks:
                if opp_win and (r, c) in opp_win:
                    final_score += 100000000  # 堵五连 > 一切
                elif opp_live4 and (r, c) in opp_live4:
                    final_score += 80000000  # 堵活四 > 一切
                else:
                    final_score += 30000000  # 长线延伸拦截（高优但非绝对）

            if has_strong_threat:
                final_score += 500000  # 自身攻击加成

            if final_score > best_desp_val:
                best_desp_val = final_score
                best_desp_move = (r, c)

        info.update({
            'reason': 'PVS搜索',
            'threat_detail': f'拼命模式-攻防混合(PVS={best_val}, blocks={len(critical_blocks)})',
            'depth': target_depth,
            'best_val': best_val,
            'time_ms': elapsed_ms,
            'top_moves': top_moves,
        })
        return (best_desp_move[0], best_desp_move[1], info)

    info.update({
        'reason': 'PVS搜索',
        'depth': target_depth,
        'time_ms': elapsed_ms,
        'top_moves': top_moves,
    })
    return (best_move[0], best_move[1], info)



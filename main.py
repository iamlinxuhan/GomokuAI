"""
五子棋游戏 - PyQt5 版本
AI: PVS+LMR + 固定数组置换表 + 专业权重棋型库 + 增强TSS(攻防) + GPU加速(可选) + 时间控制 + 开局库
"""
import sys
import random
import time
import math
import numpy as np
# PyTorch 可选依赖：尝试导入，不可用时创建占位模块
try:
    import torch
    import torch.nn.functional as F
except (ImportError, Exception):
    import types
    torch = types.ModuleType('torch')
    def _dummy_no_grad(fn=None):
        """无 torch 时 @torch.no_grad() 只是透传"""
        if fn is not None:
            return fn
        class _Ctx:
            def __enter__(self): pass
            def __exit__(self, *a): pass
        return _Ctx()
    torch.no_grad = _dummy_no_grad
    F = types.ModuleType('torch.nn.functional')
    _device = 'cpu'
    _torch_available_final = False
else:
    _torch_available_final = torch.cuda.is_available()
    _device = torch.device('cuda' if _torch_available_final else 'cpu')
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QPushButton, QLabel,
    QVBoxLayout, QHBoxLayout, QGridLayout, QStackedWidget,
    QProgressBar, QFrame, QGraphicsDropShadowEffect, QSizePolicy
)
from PyQt5.QtCore import (
    Qt, QTimer, QThread, pyqtSignal, QPropertyAnimation,
    QEasingCurve, QRect, QPoint, pyqtProperty
)
from PyQt5.QtGui import (
    QPainter, QPen, QBrush, QColor, QFont, QFontDatabase,
    QLinearGradient, QRadialGradient, QPixmap, QPainterPath,
    QMouseEvent, QFontMetrics
)

# ==================== PyTorch 设备 ====================
_device = None  # 由 try/except 设置
_torch_available_final = False


def _ensure_torch():
    """检查 PyTorch/GPU 是否真正可用（torch 已导入，此处仅返回状态）"""
    return _torch_available_final

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

# 颜色方案
COLOR_BG = QColor("#2c1810")
COLOR_BOARD = QColor("#dcb35c")
COLOR_LINE = QColor("#5a3a1a")
COLOR_BLACK = QColor("#1a1a1a")
COLOR_WHITE = QColor("#f0f0f0")
COLOR_HIGHLIGHT = QColor("#ff6b6b")
COLOR_PANEL_BG = QColor("#1e1e2e")
COLOR_ACCENT = QColor("#89b4fa")
COLOR_GREEN = QColor("#a6e3a1")
COLOR_RED = QColor("#f38ba8")
COLOR_TEXT = QColor("#cdd6f4")
COLOR_SUBTEXT = QColor("#a6adc8")

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

@torch.no_grad()
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

    return _score_channel(p_chan), _score_channel(o_chan)


@torch.no_grad()
def _batch_eval_moves(board, moves, player):
    """GPU 批量评估所有候选落子（方向核 × 2色 × N候选），专注有效棋型。
    无GPU时自动回退到CPU排序。""" 
    # 无torch/GPU时立即回退到纯CPU排序
    if not _ensure_torch() or _device.type != 'cuda':
        result = [(_quick_eval_move(board, r, c, player), r, c) for r, c in moves]
        result.sort(reverse=True)
        return result
    
    opp = 1 if player == 2 else 2
    n = len(moves)
    if n == 0:
        return []

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

    # ★★★ 修复F1: 恢复旧版的活四必胜检测（返回50M，让搜索优先走这条路）★★★
    if _find_live_four_moves(board, ai_player):
        return 50000000

    ai_score = _eval_player_composite(board, ai_player)
    human_score = _eval_player_composite(board, human_player)
    return ai_score - human_score * 0.95


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
            score += 5000000   # 活四（必胜），极高权重
        elif count == 4 and open_ends == 1:
            score += 500000    # 冲四
        elif count == 3 and open_ends >= 2:
            score += 50000     # 活三（下一步就活四），高权重
        elif count == 3 and open_ends == 1:
            score += 5000      # 眠三
        elif count == 2 and open_ends >= 2:
            score += 1000      # 活二
        elif count == 2 and open_ends == 1:
            score += 200       # 眠二
        elif count == 1 and open_ends >= 2:
            score += 30        # 活一

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
    use_gpu_batch = (_ensure_torch() and _device.type == 'cuda'
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
    增强威胁检测（含防守TSS）：
    1. 自己能五连 → 直接赢
    2. 对手能五连 → 必须堵
    3. TSS 强制获胜序列（深度6）
    4. 防守TSS：对手是否有VCF，找出防守点
    5. 自己能形成活四（必胜）→ 走这里
    6. 双活三检测
    7. 对手活四 → 必须提前堵
    """
    opp = 1 if player == 2 else 2
    stone_count = np.count_nonzero(board)

    # 1. 自己能否直接五连
    my_win = _find_winning_moves(board, player)
    if my_win:
        return my_win[0]

    # 2. 对手能否直接五连（必须堵）
    opp_win = _find_winning_moves(board, opp)
    if opp_win:
        return opp_win[0]

    # 3. TSS 强制获胜序列（VCF/VCT），深度 6
    if stone_count >= 6:
        forced_seq = _find_forced_win(board, player, max_depth=6)
        if forced_seq:
            return forced_seq[0]

    # 4. 防守TSS：检查对手是否有VCF强制获胜
    if stone_count >= 8:
        opp_forced = _find_forced_win(board, opp, max_depth=5)
        if opp_forced:
            # 对手有VCF，我们需要在他们的第一步行前抢先
            # 尝试每个防守点
            moves = _generate_moves(board)
            best_defense = None
            best_def_score = float('-inf')
            for r, c in moves:
                board[r][c] = player
                # 检查落子后是否破坏了对手的VCF
                h = zobrist_hash(board)
                # 简单策略：下在对手VCF第一步附近
                board[r][c] = 0
                # 比较防守走法 vs 对手VCF威胁：选评分最高的防守点
                def_score = _quick_eval_move(board, r, c, player)
                if def_score > best_def_score:
                    best_def_score = def_score
                    best_defense = (r, c)
            if best_defense:
                return best_defense
            # 否则直接堵对手VCF第一步
            return opp_forced[0]

    # 5. 自己能否形成活四（必胜局面）
    my_live4 = _find_live_four_moves(board, player)
    if my_live4:
        return my_live4[0]

    # 6. 双活三检测（也是必胜）
    moves = _generate_moves(board)
    for r, c in moves:
        has_live4, has_rush4, has_live3, _ = _get_pattern_types(board, r, c, player)
        if has_live3 or has_rush4:
            board[r][c] = player
            live3_dirs = []
            for dr, dc in [(1, 0), (0, 1), (1, 1), (1, -1)]:
                count, open_ends, _ = _analyze_line(board, r, c, dr, dc, player)
                if count == 3 and open_ends >= 2:
                    live3_dirs.append((dr, dc))
                elif count >= 4:
                    live3_dirs.append((dr, dc))  # 冲四也算
            board[r][c] = 0
            if len(live3_dirs) >= 2:
                return (r, c)
            # 冲四 + 活三 = 必胜
            if has_rush4 and len(live3_dirs) >= 1:
                return (r, c)

    # 7. 对手能否形成活四（必须提前堵）
    opp_live4 = _find_live_four_moves(board, opp)
    if opp_live4:
        return opp_live4[0]

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
    AI 主入口（v4 修复版）:
    1. 开局库命中 → 直接返回
    2. 增强TSS 威胁检测（含防守TSS）
    3. 时间控制 + 迭代加深（确保搜完目标深度）
    4. Zobrist/置换表 + LMR + PVS 搜索
    """

    # === 开局库 ===
    stone_count = np.count_nonzero(board)
    if stone_count <= 6:
        fen = _board_to_fen(board)
        if fen in _OPENING_BOOK:
            return _OPENING_BOOK[fen]

    # === TSS 立即威胁检测 ===
    threat = _check_immediate_threat(board, ai_player)
    if threat:
        return threat

    moves = _generate_moves(board)
    if not moves:
        return (9, 9)

    # 第一步优化
    if stone_count <= 1:
        if board[9][9] == 0:
            return (9, 9)
        return random.choice(moves)

    # === 时间控制 & 搜索深度（修复F3: 与旧版对齐，避免过深搜索导致慢） ===
    _time_start = time.time()
    # 修复：与旧版Alpha-Beta Engine保持一致的深度映射
    # 旧版: depth=1→搜1层, depth=2→搜2层, depth=3→搜3层（无迭代加深）
    # 新版用迭代加深但目标深度保持一致，时间预算大幅缩减
    if depth == 1:
        _time_max = 1.0      # 简单: 1秒
        target_depth = 1     # 搜1层（旧版同）
    elif depth == 2:
        _time_max = 2.0      # 中级: 2秒（旧版无时间限制，但只搜2层很快完成）
        target_depth = 2     # 搜2层（旧版search_depth=2, alpha_beta(depth-1=1)）
    else:
        _time_max = 5.0      # 高级: 5秒
        target_depth = 4     # 搜4层（比旧版的3层稍深，利用PVS+置换表优势）
    _TIME_RESERVE = 0.2      # 缓冲时间

    # === 走法排序（修复F2: 恢复旧版 attack*2 + defense 双方综合排序策略） ===
    human_player = 1 if ai_player == 2 else 2
    move_scores = []
    for r, c in moves:
        attack = _quick_eval_move(board, r, c, ai_player)
        defense = _quick_eval_move(board, r, c, human_player)
        # ★ 旧版关键策略：进攻权重 2x > 防守权重 1x
        # 确保"我能赢"的走法排在"堵对手"前面
        move_scores.append((attack * 2 + defense, r, c))
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
                return (r, c)

            new_hash = base_hash ^ _zobrist_table[ai_player - 1][r][c]
            val = alpha_beta(board, cur_depth - 1, float('-inf'), float('inf'), False, ai_player, new_hash, ply=1)
            board[r][c] = 0

            if val > best_val:
                best_val = val
                local_best_move = (r, c)

        best_move = local_best_move

        # 启发式提前停止：仅在较深搜索且值稳定时触发（修复：提高阈值和深度要求）
        if prev_best_val != float('-inf') and abs(best_val - prev_best_val) < 1000 and cur_depth >= 5:
            if time.time() - _time_start > _time_max * 0.7:
                break

        prev_best_val = best_val

        # 找到必胜路线（活四以上），提前结束
        # 修复：阈值与评分表匹配（活四=10M, 此处9M即可判定必胜）
        if best_val > 9000000:
            break

    return best_move


# ==================== AI Worker 线程 ====================
class AIWorker(QThread):
    """AI计算线程，避免阻塞UI"""
    finished = pyqtSignal(int, int)

    def __init__(self, board, ai_player, depth):
        super().__init__()
        self.board = board.copy()
        self.ai_player = ai_player
        self.depth = depth

    def run(self):
        r, c = ai_move(self.board, self.ai_player, self.depth)
        self.finished.emit(r, c)


# ==================== 棋子动画 ====================
class StoneAnimation(QPropertyAnimation):
    """棋子落下动画"""
    pass


# ==================== 加载界面 ====================
class LoadingScreen(QWidget):
    """模拟加载界面"""

    def __init__(self, on_finished):
        super().__init__()
        self.on_finished = on_finished
        self.progress = 0
        self.dot_count = 0
        self.tip_index = 0
        self.tips = [
            "正在初始化游戏引擎...",
            "正在加载开局库...",
            "正在构建Zobrist哈希表...",
            "正在构建棋型评估权重...",
            "正在优化GPU计算图...",
            "正在准备棋盘渲染管线...",
            "正在校准威胁搜索参数...",
            "游戏准备完成！"
        ]
        self.setup_ui()

        # 进度定时器
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_loading)
        self.timer.start(30)

        # 动画定时器
        self.dot_timer = QTimer(self)
        self.dot_timer.timeout.connect(self.update_dots)
        self.dot_timer.start(500)

        self.start_time = time.time()

    def setup_ui(self):
        self.setStyleSheet("background: transparent;")

        layout = QVBoxLayout()
        layout.setAlignment(Qt.AlignCenter)

        # 标题
        self.title = QLabel("五 子 棋")
        self.title.setAlignment(Qt.AlignCenter)
        self.title.setStyleSheet("""
            QLabel {
                color: #cdd6f4;
                font-size: 48px;
                font-weight: bold;
                font-family: 'Microsoft YaHei', 'SimHei', sans-serif;
                letter-spacing: 20px;
            }
        """)

        # 副标题
        subtitle = QLabel("Gomoku AI · PyTorch Engine")
        subtitle.setAlignment(Qt.AlignCenter)
        subtitle.setStyleSheet("""
            QLabel {
                color: #a6adc8;
                font-size: 14px;
                font-family: 'Consolas', 'Microsoft YaHei', monospace;
            }
        """)

        # Loading文字
        self.loading_label = QLabel("Loading")
        self.loading_label.setAlignment(Qt.AlignCenter)
        self.loading_label.setStyleSheet("""
            QLabel {
                color: #89b4fa;
                font-size: 18px;
                font-family: 'Consolas', monospace;
            }
        """)

        # 进度条
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(6)
        self.progress_bar.setFixedWidth(400)
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                background-color: #313244;
                border-radius: 3px;
                border: none;
            }
            QProgressBar::chunk {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #89b4fa, stop:0.5 #a6e3a1, stop:1 #89b4fa);
                border-radius: 3px;
            }
        """)

        # 百分比
        self.percent_label = QLabel("0%")
        self.percent_label.setAlignment(Qt.AlignCenter)
        self.percent_label.setStyleSheet("color: #cdd6f4; font-size: 24px; font-weight: bold;")

        # 提示文字
        self.tip_label = QLabel(self.tips[0])
        self.tip_label.setAlignment(Qt.AlignCenter)
        self.tip_label.setStyleSheet("color: #6c7086; font-size: 13px; font-family: 'Microsoft YaHei';")

        # 版权
        copyright_label = QLabel("基于 PVS+LMR · 专业权重 · TSS攻防搜索 · GPU加速")
        copyright_label.setAlignment(Qt.AlignCenter)
        copyright_label.setStyleSheet("color: #45475a; font-size: 11px; font-family: 'Consolas', monospace;")

        layout.addStretch(2)
        layout.addWidget(self.title)
        layout.addSpacing(10)
        layout.addWidget(subtitle)
        layout.addSpacing(40)
        layout.addWidget(self.loading_label, alignment=Qt.AlignCenter)
        layout.addSpacing(15)
        layout.addWidget(self.progress_bar, alignment=Qt.AlignCenter)
        layout.addSpacing(10)
        layout.addWidget(self.percent_label)
        layout.addSpacing(10)
        layout.addWidget(self.tip_label)
        layout.addStretch(3)
        layout.addWidget(copyright_label)
        layout.addSpacing(30)

        self.setLayout(layout)

    def update_loading(self):
        elapsed = time.time() - self.start_time
        duration = 5.0  # 加载总时长
        self.progress = min(elapsed / duration, 1.0)
        pct = int(self.progress * 100)
        self.progress_bar.setValue(pct)
        self.percent_label.setText(f"{pct}%")

        # 更新提示
        new_tip = min(int(self.progress * (len(self.tips) - 1)), len(self.tips) - 1)
        if new_tip != self.tip_index:
            self.tip_index = new_tip
            self.tip_label.setText(self.tips[self.tip_index])

        if self.progress >= 1.0:
            self.timer.stop()
            self.dot_timer.stop()
            self.loading_label.setText("Ready!")
            self.tip_label.setText(self.tips[-1])
            # 延迟跳转
            QTimer.singleShot(800, self.on_finished)

    def update_dots(self):
        self.dot_count = (self.dot_count + 1) % 4
        if self.progress < 1.0:
            self.loading_label.setText("Loading" + "." * self.dot_count)


# ==================== 游戏棋盘组件 ====================
class BoardWidget(QWidget):
    """棋盘绘制组件"""

    def __init__(self):
        super().__init__()
        self.board = np.zeros((BOARD_SIZE, BOARD_SIZE), dtype=int)
        self.last_move = None  # (r, c, player)
        self.hover_pos = None
        self.setFixedSize(BOARD_PX + MARGIN * 2, BOARD_PX + MARGIN * 2)
        self.setMouseTracking(True)
        self.setStyleSheet("background: transparent;")

    def set_board(self, board):
        self.board = board.copy()
        self.update()

    def set_last_move(self, r, c, player):
        if r is None or c is None:
            self.last_move = None
        else:
            self.last_move = (r, c, player)
        self.update()

    def mouseMoveEvent(self, event: QMouseEvent):
        x, y = event.x(), event.y()
        if MARGIN <= x <= MARGIN + (BOARD_SIZE - 1) * CELL_SIZE and \
           MARGIN <= y <= MARGIN + (BOARD_SIZE - 1) * CELL_SIZE:
            c = round((x - MARGIN) / CELL_SIZE)
            r = round((y - MARGIN) / CELL_SIZE)
            if 0 <= r < BOARD_SIZE and 0 <= c < BOARD_SIZE:
                self.hover_pos = (r, c)
                self.update()
                return
        self.hover_pos = None
        self.update()

    def leaveEvent(self, event):
        self.hover_pos = None
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        # 背景
        bg_grad = QRadialGradient(self.width() / 2, self.height() / 2,
                                   max(self.width(), self.height()))
        bg_grad.setColorAt(0, QColor("#e8c97a"))
        bg_grad.setColorAt(1, QColor("#c4943a"))
        painter.setBrush(QBrush(bg_grad))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(0, 0, self.width(), self.height(), 10, 10)

        # 棋盘木纹背景
        board_rect = QRect(MARGIN - 15, MARGIN - 15,
                           (BOARD_SIZE - 1) * CELL_SIZE + 30,
                           (BOARD_SIZE - 1) * CELL_SIZE + 30)
        painter.setBrush(QBrush(QColor("#dcb35c")))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(board_rect, 8, 8)

        # 网格线
        pen = QPen(QColor("#5a3a1a"), 1.5)
        painter.setPen(pen)
        for i in range(BOARD_SIZE):
            y = MARGIN + i * CELL_SIZE
            painter.drawLine(MARGIN, y, MARGIN + (BOARD_SIZE - 1) * CELL_SIZE, y)
        for i in range(BOARD_SIZE):
            x = MARGIN + i * CELL_SIZE
            painter.drawLine(x, MARGIN, x, MARGIN + (BOARD_SIZE - 1) * CELL_SIZE)

        # 星位
        star_points = [
            (3, 3), (3, 9), (3, 15),
            (9, 3), (9, 9), (9, 15),
            (15, 3), (15, 9), (15, 15)
        ]
        painter.setBrush(QBrush(QColor("#3a1a0a")))
        painter.setPen(Qt.NoPen)
        for r, c in star_points:
            x = MARGIN + c * CELL_SIZE
            y = MARGIN + r * CELL_SIZE
            painter.drawEllipse(QPoint(x, y), 4, 4)

        # 坐标标注
        coord_font = QFont("Consolas", 9)
        painter.setFont(coord_font)
        painter.setPen(QColor("#5a3a1a"))
        for i in range(BOARD_SIZE):
            x = MARGIN + i * CELL_SIZE
            painter.drawText(QRect(x - 10, MARGIN - 25, 20, 20),
                             Qt.AlignCenter, chr(65 + i) if i < 26 else str(i))
            y = MARGIN + i * CELL_SIZE
            painter.drawText(QRect(MARGIN - 35, y - 10, 30, 20),
                             Qt.AlignCenter, str(i + 1))

        # 绘制棋子
        for r in range(BOARD_SIZE):
            for c in range(BOARD_SIZE):
                if self.board[r][c] != 0:
                    self._draw_stone(painter, r, c, self.board[r][c])

        # 最后一手高亮
        if self.last_move:
            r, c, player = self.last_move
            x = MARGIN + c * CELL_SIZE
            y = MARGIN + r * CELL_SIZE
            painter.setPen(QPen(QColor("#ff6b6b"), 2))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(QPoint(x, y), CELL_SIZE // 2 - 2, CELL_SIZE // 2 - 2)

        # 悬停预览
        if self.hover_pos and self.board[self.hover_pos[0]][self.hover_pos[1]] == 0:
            r, c = self.hover_pos
            x = MARGIN + c * CELL_SIZE
            y = MARGIN + r * CELL_SIZE
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(QColor(128, 128, 128, 80)))
            painter.drawEllipse(QPoint(x, y), CELL_SIZE // 2 - 2, CELL_SIZE // 2 - 2)

        painter.end()

    def _draw_stone(self, painter, r, c, player):
        x = MARGIN + c * CELL_SIZE
        y = MARGIN + r * CELL_SIZE
        radius = CELL_SIZE // 2 - 3

        if player == 1:  # 黑棋
            grad = QRadialGradient(x - radius * 0.3, y - radius * 0.3, radius * 1.2)
            grad.setColorAt(0, QColor("#555555"))
            grad.setColorAt(0.7, QColor("#1a1a1a"))
            grad.setColorAt(1, QColor("#000000"))
            painter.setBrush(QBrush(grad))
            painter.setPen(QPen(QColor("#333333"), 1))
        else:  # 白棋
            grad = QRadialGradient(x - radius * 0.3, y - radius * 0.3, radius * 1.2)
            grad.setColorAt(0, QColor("#ffffff"))
            grad.setColorAt(0.6, QColor("#e8e8e8"))
            grad.setColorAt(1, QColor("#c0c0c0"))
            painter.setBrush(QBrush(grad))
            painter.setPen(QPen(QColor("#999999"), 1))

        painter.drawEllipse(QPoint(x, y), radius, radius)

    def get_grid_pos(self, screen_x, screen_y):
        """屏幕坐标转棋盘坐标"""
        if MARGIN <= screen_x <= MARGIN + (BOARD_SIZE - 1) * CELL_SIZE and \
           MARGIN <= screen_y <= MARGIN + (BOARD_SIZE - 1) * CELL_SIZE:
            c = round((screen_x - MARGIN) / CELL_SIZE)
            r = round((screen_y - MARGIN) / CELL_SIZE)
            if 0 <= r < BOARD_SIZE and 0 <= c < BOARD_SIZE:
                return r, c
        return None


# ==================== 游戏面板（右侧） ====================
class GamePanel(QWidget):
    """右侧信息面板"""

    def __init__(self):
        super().__init__()
        self.setFixedWidth(260)
        self.setStyleSheet(f"background-color: {COLOR_PANEL_BG.name()}; border-radius: 0 12px 12px 0;")
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout()
        layout.setContentsMargins(20, 30, 20, 30)
        layout.setSpacing(15)

        # 标题
        title = QLabel("五子棋 AI")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("""
            QLabel {
                color: #cdd6f4;
                font-size: 22px;
                font-weight: bold;
                font-family: 'Microsoft YaHei', 'SimHei';
            }
        """)

        # 分隔线
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("background-color: #45475a; max-height: 1px;")

        # 当前回合
        self.turn_label = QLabel("当前回合：黑棋 ●")
        self.turn_label.setStyleSheet("color: #a6adc8; font-size: 14px; font-family: 'Microsoft YaHei';")

        # AI难度
        self.difficulty_label = QLabel("AI 难度：-")
        self.difficulty_label.setStyleSheet("color: #a6adc8; font-size: 14px; font-family: 'Microsoft YaHei';")

        # 游戏状态
        self.status_label = QLabel("游戏状态：进行中")
        self.status_label.setStyleSheet("color: #a6e3a1; font-size: 14px; font-family: 'Microsoft YaHei';")

        # AI思考指示器
        self.thinking_label = QLabel("")
        self.thinking_label.setAlignment(Qt.AlignCenter)
        self.thinking_label.setStyleSheet("color: #89b4fa; font-size: 13px; font-family: 'Consolas';")
        self.thinking_label.hide()

        # 剩余悔棋次数
        self.undo_label = QLabel("悔棋次数：3")
        self.undo_label.setStyleSheet("color: #a6adc8; font-size: 14px; font-family: 'Microsoft YaHei';")

        # 分隔线2
        sep2 = QFrame()
        sep2.setFrameShape(QFrame.HLine)
        sep2.setStyleSheet("background-color: #45475a; max-height: 1px;")

        # 统计
        self.stats_label = QLabel("步数：0")
        self.stats_label.setStyleSheet("color: #6c7086; font-size: 12px; font-family: 'Consolas';")

        # 按钮区域
        btn_layout = QVBoxLayout()
        btn_layout.setSpacing(10)

        self.undo_btn = self._make_button("↩ 悔棋", COLOR_ACCENT, "#74c7ec")
        self.restart_btn = self._make_button("🔄 重新开始", COLOR_GREEN, "#94e2d5")
        self.quit_btn = self._make_button("✕ 退出游戏", COLOR_RED, "#eba0ac")

        btn_layout.addWidget(self.undo_btn)
        btn_layout.addWidget(self.restart_btn)
        btn_layout.addWidget(self.quit_btn)

        layout.addWidget(title)
        layout.addWidget(sep)
        layout.addWidget(self.turn_label)
        layout.addWidget(self.difficulty_label)
        layout.addWidget(self.status_label)
        layout.addWidget(self.undo_label)
        layout.addWidget(self.thinking_label)
        layout.addWidget(sep2)
        layout.addWidget(self.stats_label)
        layout.addStretch()
        layout.addLayout(btn_layout)

        self.setLayout(layout)

    def _make_button(self, text, color, hover_color):
        btn = QPushButton(text)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setFixedHeight(40)
        btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {color.name()};
                color: #1e1e2e;
                border: none;
                border-radius: 8px;
                font-size: 14px;
                font-weight: bold;
                font-family: 'Microsoft YaHei';
            }}
            QPushButton:hover {{
                background-color: {hover_color};
            }}
            QPushButton:pressed {{
                background-color: {color.darker(120).name()};
            }}
        """)
        return btn

    def update_info(self, turn, difficulty, status, undo_count, move_count):
        players = {1: "黑棋 ●", 2: "白棋 ○"}
        turn_text = f"当前回合：{players.get(turn, '-')}"
        self.turn_label.setText(turn_text)
        self.difficulty_label.setText(f"AI 难度：{difficulty} 级")
        self.status_label.setText(f"游戏状态：{status}")
        self.undo_label.setText(f"悔棋次数：{undo_count}")
        self.stats_label.setText(f"步数：{move_count}")

        if status == "你赢了！":
            self.status_label.setStyleSheet("color: #a6e3a1; font-size: 14px; font-weight: bold;")
        elif status == "你输了！":
            self.status_label.setStyleSheet("color: #f38ba8; font-size: 14px; font-weight: bold;")
        else:
            self.status_label.setStyleSheet("color: #a6adc8; font-size: 14px;")

    def show_thinking(self, show=True):
        if show:
            self.thinking_label.setText("AI 思考中...")
            self.thinking_label.show()
        else:
            self.thinking_label.hide()


# ==================== 选择界面 ====================
class SelectionScreen(QWidget):
    """执棋颜色 / AI难度选择"""

    color_selected = pyqtSignal(int)  # 0=黑先, 1=白后
    difficulty_selected = pyqtSignal(int)  # 1-3

    def __init__(self, mode="color"):
        super().__init__()
        self.mode = mode
        self.setup_ui()

    def setup_ui(self):
        self.setStyleSheet("background: transparent;")

        layout = QVBoxLayout()
        layout.setAlignment(Qt.AlignCenter)

        if self.mode == "color":
            title = QLabel("选择执棋颜色")
            title.setStyleSheet("color: #cdd6f4; font-size: 28px; font-weight: bold; font-family: 'Microsoft YaHei';")
            title.setAlignment(Qt.AlignCenter)

            hint = QLabel("黑棋为先手，白棋为后手")
            hint.setStyleSheet("color: #6c7086; font-size: 14px; font-family: 'Microsoft YaHei';")
            hint.setAlignment(Qt.AlignCenter)

            btn_layout = QHBoxLayout()
            btn_layout.setSpacing(30)

            black_btn = self._make_card_btn("⚫\n黑棋（先手）", QColor("#1a1a1a"), QColor("#333333"))
            white_btn = self._make_card_btn("⚪\n白棋（后手）", QColor("#f0f0f0"), QColor("#ffffff"),
                                             text_color=QColor("#1e1e2e"))

            black_btn.clicked.connect(lambda: self.color_selected.emit(0))
            white_btn.clicked.connect(lambda: self.color_selected.emit(1))

            btn_layout.addWidget(black_btn)
            btn_layout.addWidget(white_btn)

            layout.addStretch(2)
            layout.addWidget(title)
            layout.addSpacing(10)
            layout.addWidget(hint)
            layout.addSpacing(40)
            layout.addLayout(btn_layout)
            layout.addStretch(3)

        else:  # difficulty
            title = QLabel("选择 AI 难度")
            title.setStyleSheet("color: #cdd6f4; font-size: 28px; font-weight: bold; font-family: 'Microsoft YaHei';")
            title.setAlignment(Qt.AlignCenter)

            hint = QLabel("难度越高，AI思考越深入")
            hint.setStyleSheet("color: #6c7086; font-size: 14px; font-family: 'Microsoft YaHei';")
            hint.setAlignment(Qt.AlignCenter)

            btn_layout = QHBoxLayout()
            btn_layout.setSpacing(25)

            colors = [COLOR_GREEN, COLOR_ACCENT, COLOR_RED]
            hovers = ["#94e2d5", "#74c7ec", "#eba0ac"]
            labels = ["初级\n搜索深度 1", "中级\n搜索深度 2", "高级\n搜索深度 3"]

            for i in range(3):
                btn = self._make_card_btn(labels[i], colors[i], hovers[i], text_color=QColor("#1e1e2e"))
                level = i + 1
                btn.clicked.connect(lambda checked, l=level: self.difficulty_selected.emit(l))
                btn_layout.addWidget(btn)

            layout.addStretch(2)
            layout.addWidget(title)
            layout.addSpacing(10)
            layout.addWidget(hint)
            layout.addSpacing(40)
            layout.addLayout(btn_layout)
            layout.addStretch(3)

        self.setLayout(layout)

    def _make_card_btn(self, text, color, hover_color, text_color=QColor("#cdd6f4")):
        btn = QPushButton(text)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setFixedSize(160, 160)
        btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {color.name()};
                color: {text_color.name()};
                border: 3px solid transparent;
                border-radius: 16px;
                font-size: 18px;
                font-weight: bold;
                font-family: 'Microsoft YaHei';
            }}
            QPushButton:hover {{
                background-color: {hover_color};
                border: 3px solid #cdd6f4;
            }}
        """)
        return btn


# ==================== 游戏结束覆盖层 ====================
class GameOverOverlay(QWidget):
    """游戏结束遮罩"""

    restart_clicked = pyqtSignal()
    quit_clicked = pyqtSignal()

    def __init__(self, result_text, is_win):
        super().__init__()
        self.result_text = result_text
        self.is_win = is_win
        self.setStyleSheet("background: rgba(0, 0, 0, 160); border-radius: 12px;")
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout()
        layout.setAlignment(Qt.AlignCenter)

        # 结果文字
        result_label = QLabel(self.result_text)
        result_label.setAlignment(Qt.AlignCenter)
        color = "#a6e3a1" if self.is_win else "#f38ba8"
        result_label.setStyleSheet(f"""
            QLabel {{
                color: {color};
                font-size: 42px;
                font-weight: bold;
                font-family: 'Microsoft YaHei';
            }}
        """)

        # 按钮
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(20)

        restart_btn = QPushButton("🔄 再来一局")
        restart_btn.setCursor(Qt.PointingHandCursor)
        restart_btn.setFixedSize(150, 45)
        restart_btn.setStyleSheet("""
            QPushButton {
                background-color: #a6e3a1;
                color: #1e1e2e;
                border: none;
                border-radius: 10px;
                font-size: 16px;
                font-weight: bold;
                font-family: 'Microsoft YaHei';
            }
            QPushButton:hover { background-color: #94e2d5; }
        """)
        restart_btn.clicked.connect(self.restart_clicked.emit)

        quit_btn = QPushButton("✕ 退出游戏")
        quit_btn.setCursor(Qt.PointingHandCursor)
        quit_btn.setFixedSize(150, 45)
        quit_btn.setStyleSheet("""
            QPushButton {
                background-color: #f38ba8;
                color: #1e1e2e;
                border: none;
                border-radius: 10px;
                font-size: 16px;
                font-weight: bold;
                font-family: 'Microsoft YaHei';
            }
            QPushButton:hover { background-color: #eba0ac; }
        """)
        quit_btn.clicked.connect(self.quit_clicked.emit)

        btn_layout.addWidget(restart_btn)
        btn_layout.addWidget(quit_btn)

        layout.addStretch(2)
        layout.addWidget(result_label)
        layout.addSpacing(30)
        layout.addLayout(btn_layout)
        layout.addStretch(2)

        self.setLayout(layout)


# ==================== 主窗口 ====================
class GomokuGame(QMainWindow):
    """主游戏窗口"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("五子棋 AI · PyTorch Engine")
        self.setFixedSize(WINDOW_W, WINDOW_H)
        self.setStyleSheet(f"background-color: {COLOR_BG.name()};")

        # 游戏状态变量
        self.board = np.zeros((BOARD_SIZE, BOARD_SIZE), dtype=int)
        self.move_history = []  # 每步落子后保存棋盘快照
        self.gamemode = 0  # 0=先手(黑), 1=后手(白)
        self.gameplayer = 1
        self.gamekunnan = 1
        self.gamerule = 3  # 1=输, 2=赢, 3=进行中
        self.output = 3  # 悔棋次数
        self.move_count = 0
        self.game_over = False
        self.ai_thinking = False
        self.ai_first_move_done = False
        self.last_move = None

        # AI Worker
        self.ai_worker = None

        # 中央容器
        self.central = QStackedWidget()
        self.setCentralWidget(self.central)

        # 各页面
        self.loading_screen = None
        self.selection_color = None
        self.selection_difficulty = None
        self.game_widget = None
        self.board_widget = None
        self.game_panel = None
        self.game_over_overlay = None

        self._init_loading()

    def _init_loading(self):
        """初始化加载界面"""
        self.loading_screen = LoadingScreen(on_finished=self._on_loading_finished)
        self.central.addWidget(self.loading_screen)
        self.central.setCurrentWidget(self.loading_screen)

    def _on_loading_finished(self):
        """加载完成，进入主菜单"""
        self._show_color_selection()

    def _show_color_selection(self):
        """显示执棋颜色选择"""
        self.selection_color = SelectionScreen(mode="color")
        self.selection_color.color_selected.connect(self._on_color_selected)
        self.central.addWidget(self.selection_color)
        self.central.setCurrentWidget(self.selection_color)

    def _on_color_selected(self, mode):
        """选择了执棋颜色"""
        self.gamemode = mode
        self._show_difficulty_selection()

    def _show_difficulty_selection(self):
        """显示难度选择"""
        self.selection_difficulty = SelectionScreen(mode="difficulty")
        self.selection_difficulty.difficulty_selected.connect(self._on_difficulty_selected)
        self.central.addWidget(self.selection_difficulty)
        self.central.setCurrentWidget(self.selection_difficulty)

    def _on_difficulty_selected(self, level):
        """选择了难度，开始游戏"""
        self.gamekunnan = level
        self._start_game()

    def _start_game(self):
        """初始化游戏"""
        self.board = np.zeros((BOARD_SIZE, BOARD_SIZE), dtype=int)
        self.move_history = []  # 每步落子后保存棋盘快照
        self.gameplayer = 1
        self.gamerule = 3
        self.output = 3
        self.move_count = 0
        self.game_over = False
        self.ai_thinking = False
        self.ai_first_move_done = False
        self.last_move = None

        # 清空全局状态
        global _killer_moves, _history_table, _transposition_table, _eval_cache, _tt_age
        _killer_moves = [[None, None] for _ in range(MAX_DEPTH)]
        _history_table = np.zeros((2, BOARD_SIZE, BOARD_SIZE), dtype=np.int32)
        _transposition_table = [None] * TT_SIZE
        _eval_cache = {}
        _tt_age = 0

        # 构建游戏界面
        self._build_game_ui()

    def _build_game_ui(self):
        """构建游戏主界面"""
        game_container = QWidget()
        game_container.setStyleSheet("background: transparent;")
        h_layout = QHBoxLayout()
        h_layout.setContentsMargins(0, 0, 0, 0)
        h_layout.setSpacing(0)

        # 棋盘
        self.board_widget = BoardWidget()
        self.board_widget.set_board(self.board)
        self.board_widget.mousePressEvent = self._on_board_click

        # 包装棋盘（左侧圆角）
        board_wrapper = QWidget()
        board_wrapper.setStyleSheet("""
            background-color: #dcb35c;
            border-radius: 12px 0 0 12px;
        """)
        board_layout = QVBoxLayout()
        board_layout.setContentsMargins(0, 0, 0, 0)
        board_layout.addWidget(self.board_widget)
        board_wrapper.setLayout(board_layout)

        # 右侧面板
        self.game_panel = GamePanel()
        self.game_panel.undo_btn.clicked.connect(self._on_undo)
        self.game_panel.restart_btn.clicked.connect(self._on_restart)
        self.game_panel.quit_btn.clicked.connect(self._on_quit)

        h_layout.addWidget(board_wrapper)
        h_layout.addWidget(self.game_panel)

        game_container.setLayout(h_layout)

        # 游戏结束覆盖层（初始隐藏）
        self.game_over_overlay = None

        self.game_widget = QWidget()
        overlay_layout = QVBoxLayout()
        overlay_layout.setContentsMargins(0, 0, 0, 0)
        overlay_layout.addWidget(game_container)
        self.game_widget.setLayout(overlay_layout)

        self.central.addWidget(self.game_widget)
        self.central.setCurrentWidget(self.game_widget)

        self._update_panel()

        # AI先手
        if self.gamemode == 1:  # 玩家后手，AI先手
            self._ai_first_move()

    def _ai_first_move(self):
        """AI第一步：下天元"""
        if not self.ai_first_move_done:
            self.ai_first_move_done = True
            self.board[9][9] = 1
            self.last_move = (9, 9, 1)
            self.board_widget.set_board(self.board)
            self.board_widget.set_last_move(9, 9, 1)
            self.move_count += 1
            self.move_history.append(self.board.copy())
            self._update_panel()

    def _on_board_click(self, event: QMouseEvent):
        """处理棋盘点击"""
        if self.game_over or self.ai_thinking:
            return

        pos = self.board_widget.get_grid_pos(event.x(), event.y())
        if pos is None:
            return
        r, c = pos
        if self.board[r][c] != 0:
            return

        # 玩家落子
        if self.gamemode == 0:
            self.board[r][c] = 1  # 玩家执黑
            player_stone = 1
            ai_stone = 2
        else:
            self.board[r][c] = 2  # 玩家执白
            player_stone = 2
            ai_stone = 1

        self.last_move = (r, c, player_stone)
        self.board_widget.set_board(self.board)
        self.board_widget.set_last_move(r, c, player_stone)
        self.move_count += 1
        self.move_history.append(self.board.copy())
        self._update_panel()

        # 检查玩家是否获胜
        if check_win(self.board, player_stone):
            self.gamerule = 2
            self.game_over = True
            self._show_game_over()
            return

        # 检查平局
        if self.move_count >= BOARD_SIZE * BOARD_SIZE:
            self.gamerule = 0
            self.game_over = True
            self._show_game_over()
            return

        # AI回合
        self._ai_turn(ai_stone)

    def _ai_turn(self, ai_stone):
        """AI回合"""
        self.ai_thinking = True
        self.game_panel.show_thinking(True)
        self.game_panel.undo_btn.setEnabled(False)

        self.ai_worker = AIWorker(self.board, ai_stone, self.gamekunnan)
        self.ai_worker.finished.connect(self._on_ai_finished)
        self.ai_worker.start()

    def _on_ai_finished(self, r, c):
        """AI落子完成"""
        self.ai_thinking = False
        self.game_panel.show_thinking(False)
        self.game_panel.undo_btn.setEnabled(True)

        if self.gamemode == 0:
            ai_stone = 2
        else:
            ai_stone = 1

        self.board[r][c] = ai_stone
        self.last_move = (r, c, ai_stone)
        self.board_widget.set_board(self.board)
        self.board_widget.set_last_move(r, c, ai_stone)
        self.move_count += 1
        self.move_history.append(self.board.copy())
        self._update_panel()

        # 检查AI是否获胜
        if check_win(self.board, ai_stone):
            self.gamerule = 1
            self.game_over = True
            self._show_game_over()
            return

        # 检查平局
        if self.move_count >= BOARD_SIZE * BOARD_SIZE:
            self.gamerule = 0
            self.game_over = True
            self._show_game_over()

    def _on_undo(self):
        """悔棋：撤回玩家最后一步及其后的AI回应（共2步）"""
        if self.game_over or self.ai_thinking:
            return
        if self.output <= 0:
            return
        if self.move_count == 0:
            return  # 棋局尚未开始，无法悔棋

        self.output -= 1

        if self.move_count >= 2:
            # 弹出最后两步（玩家 + AI）
            self.move_history.pop()  # AI的那步
            self.move_history.pop()  # 玩家的那步
            self.board = self.move_history[-1].copy() if self.move_history else np.zeros((BOARD_SIZE, BOARD_SIZE), dtype=int)
            self.move_count -= 2
        elif self.move_count == 1 and self.gamemode == 1:
            # AI先手的情况，撤回AI第一步，重下天元
            self.move_history.pop()
            self.board = np.zeros((BOARD_SIZE, BOARD_SIZE), dtype=int)
            self.move_count = 0
            self.ai_first_move_done = False
            self._ai_first_move()
            self.board_widget.set_board(self.board)
            self._update_panel()
            return
        elif self.move_count == 1:
            self.move_history.pop()
            self.board = np.zeros((BOARD_SIZE, BOARD_SIZE), dtype=int)
            self.move_count = 0

        self.last_move = None
        self.board_widget.set_board(self.board)
        self.board_widget.set_last_move(None, None, None)
        self._update_panel()

    def _on_restart(self):
        """重新开始"""
        if self.ai_worker and self.ai_worker.isRunning():
            self.ai_worker.terminate()
            self.ai_worker.wait()
        self._show_color_selection()

    def _on_quit(self):
        """退出"""
        self.close()

    def _update_panel(self):
        """更新右侧面板"""
        if self.gamemode == 0:
            turn = 1 if self.move_count % 2 == 0 else 2
        else:
            turn = 2 if self.move_count % 2 == 0 else 1

        if self.gamerule == 1:
            status = "你输了！"
        elif self.gamerule == 2:
            status = "你赢了！"
        elif self.gamerule == 0:
            status = "平局！"
        else:
            status = "进行中"

        self.game_panel.update_info(turn, self.gamekunnan, status, self.output, self.move_count)

    def _show_game_over(self):
        """显示游戏结束覆盖层"""
        self._update_panel()

        is_win = (self.gamerule == 2)
        if self.gamerule == 2:
            text = "🎉 你赢了！"
        elif self.gamerule == 1:
            text = "😞 你输了！"
        else:
            text = "🤝 平局！"

        overlay = GameOverOverlay(text, is_win)
        overlay.restart_clicked.connect(self._on_restart)
        overlay.quit_clicked.connect(self._on_quit)

        # 将覆盖层添加到game_widget上
        self.game_over_overlay = overlay
        self.game_widget.layout().addWidget(overlay)
        overlay.setGeometry(self.game_widget.rect())
        overlay.show()
        overlay.raise_()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.game_over_overlay:
            self.game_over_overlay.setGeometry(self.game_widget.rect())


# ==================== 入口 ====================
def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    # 全局字体
    font = QFont("Microsoft YaHei", 10)
    app.setFont(font)

    window = GomokuGame()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()

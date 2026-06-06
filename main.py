"""
五子棋游戏 - PyQt5 版本
AI: Alpha-Beta剪枝 + 杀手/历史启发 + PyTorch GPU加速评估 + TSS威胁空间搜索
"""
import sys
import random
import time
import math
import numpy as np
import torch
import torch.nn.functional as F
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
_device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

# ==================== PyTorch 模式检测卷积核 ====================
# 4个方向 (水平, 垂直, 对角线, 反对角线) 的模式内核
# 用于 GPU 批量检测棋盘上的棋型
_pattern_kernels_cached = None

def _get_pattern_kernels():
    """创建/获取模式检测卷积核（延迟创建，避免首次导入开销）"""
    global _pattern_kernels_cached
    if _pattern_kernels_cached is not None:
        return _pattern_kernels_cached

    # 每个方向一组 5x1 卷积核用于检测连续棋子
    # 水平方向: kernel shape (1,1,1,5) — (out_ch, in_ch, h, w)
    k_h = torch.tensor([[[[1, 1, 1, 1, 1]]]], dtype=torch.float32)  # 五连
    k_h4 = torch.tensor([[[[0, 1, 1, 1, 1]]]], dtype=torch.float32)  # 四连
    k_h3 = torch.tensor([[[[0, 0, 1, 1, 1]]]], dtype=torch.float32)  # 三连
    k_h2 = torch.tensor([[[[0, 0, 0, 1, 1]]]], dtype=torch.float32)  # 二连

    k_v = k_h.permute(0, 1, 3, 2)   # 垂直方向
    k_v4 = k_h4.permute(0, 1, 3, 2)
    k_v3 = k_h3.permute(0, 1, 3, 2)
    k_v2 = k_h2.permute(0, 1, 3, 2)

    # 对角线用 5x5 对角核
    k_d = torch.zeros(1, 1, 5, 5, dtype=torch.float32)
    for i in range(5):
        k_d[0, 0, i, i] = 1

    k_ad = torch.zeros(1, 1, 5, 5, dtype=torch.float32)  # 反对角线
    for i in range(5):
        k_ad[0, 0, i, 4 - i] = 1

    _pattern_kernels_cached = {
        'h5': k_h.to(_device), 'h4': k_h4.to(_device), 'h3': k_h3.to(_device), 'h2': k_h2.to(_device),
        'v5': k_v.to(_device), 'v4': k_v4.to(_device), 'v3': k_v3.to(_device), 'v2': k_v2.to(_device),
        'd5': k_d.to(_device), 'ad5': k_ad.to(_device),
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
    用 GPU 张量运算评估棋盘。
    board_tensor: (1, 2, 19, 19), channel0=player, channel1=opponent.
    返回 (player_score, opponent_score) 标量。
    """
    kernels = _get_pattern_kernels()
    p_chan = board_tensor[:, 0:1, :, :]  # (1, 1, 19, 19)
    o_chan = board_tensor[:, 1:2, :, :]

    def _score_channel(ch):
        score = 0.0
        # 水平五连
        h5 = (F.conv2d(ch, kernels['h5']).squeeze() >= 4.9).float().sum().item()
        score += h5 * 100000000
        # 垂直五连
        v5 = (F.conv2d(ch, kernels['v5']).squeeze() >= 4.9).float().sum().item()
        score += v5 * 100000000
        # 对角线五连
        d5 = (F.conv2d(ch, kernels['d5']).squeeze() >= 4.9).float().sum().item()
        score += d5 * 100000000
        # 反对角五连
        ad5 = (F.conv2d(ch, kernels['ad5']).squeeze() >= 4.9).float().sum().item()
        score += ad5 * 100000000

        # 水平四连
        h4 = (F.conv2d(ch, kernels['h4']).squeeze() >= 3.9).float().sum().item()
        score += h4 * 500000
        # 垂直四连
        v4 = (F.conv2d(ch, kernels['v4']).squeeze() >= 3.9).float().sum().item()
        score += v4 * 500000

        # 水平三连
        h3 = (F.conv2d(ch, kernels['h3']).squeeze() >= 2.9).float().sum().item()
        score += h3 * 10000
        # 垂直三连
        v3 = (F.conv2d(ch, kernels['v3']).squeeze() >= 2.9).float().sum().item()
        score += v3 * 10000

        # 水平二连
        h2 = (F.conv2d(ch, kernels['h2']).squeeze() >= 1.9).float().sum().item()
        score += h2 * 200
        # 垂直二连
        v2 = (F.conv2d(ch, kernels['v2']).squeeze() >= 1.9).float().sum().item()
        score += v2 * 200

        return score

    return _score_channel(p_chan), _score_channel(o_chan)

@torch.no_grad()
def _batch_eval_moves(board, moves, player):
    """GPU 批量评估所有候选落子的价值，返回 [(score, r, c), ...] 排序"""
    opp = 1 if player == 2 else 2
    n = len(moves)
    if n == 0:
        return []

    # 构建 N 个棋盘快照
    boards_p = np.tile((board == player).astype(np.float32), (n, 1, 1))
    boards_o = np.tile((board == opp).astype(np.float32), (n, 1, 1))
    for i, (r, c) in enumerate(moves):
        # 在 player 通道放棋子
        boards_p[i, r, c] = 1.0
        boards_o[i, r, c] = 0.0  # 确保该位置被 player 占据

    tensor = _batch_to_torch(boards_p, boards_o)

    # 用 conv2d 批处理
    kernels = _get_pattern_kernels()
    scores = torch.zeros(n, dtype=torch.float32, device=_device)

    def _add_batch_conv(ch, k, weight):
        conv = F.conv2d(ch, k)  # (N, 1, H_out, W_out)
        # 检查每个 batch item 是否有任意位置满足 5 连
        max_val = conv.amax(dim=[1, 2, 3])  # (N,)
        hits = (max_val >= k.numel() - 0.2).float()  # (N,)
        scores.add_(hits * weight)

    p_ch = tensor[:, 0:1, :, :]
    o_ch = tensor[:, 1:2, :, :]

    for ch, mult in [(p_ch, 1.0), (o_ch, 0.95)]:
        # 五连 (水平和垂直)
        _add_batch_conv(ch, kernels['h5'], 100000000 * mult)
        _add_batch_conv(ch, kernels['v5'], 100000000 * mult)
        _add_batch_conv(ch, kernels['d5'], 100000000 * mult)
        _add_batch_conv(ch, kernels['ad5'], 100000000 * mult)
        # 四连
        _add_batch_conv(ch, kernels['h4'], 500000 * mult)
        _add_batch_conv(ch, kernels['v4'], 500000 * mult)
        # 三连
        _add_batch_conv(ch, kernels['h3'], 10000 * mult)
        _add_batch_conv(ch, kernels['v3'], 10000 * mult)
        # 二连
        _add_batch_conv(ch, kernels['h2'], 200 * mult)
        _add_batch_conv(ch, kernels['v2'], 200 * mult)

    # 中心加分
    for i, (r, c) in enumerate(moves):
        dist = abs(r - 9) + abs(c - 9)
        scores[i] += max(0, 18 - dist) * 5

    # 结合 CPU 复合棋型评估（捕获 GPU 卷积检测不到的活三/冲四等）
    result = []
    for i in range(n):
        r, c = moves[i]
        quick_score = _quick_eval_move(board, r, c, player)
        combined = scores[i].item() + quick_score
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
    """评估棋盘局面，正值对AI有利。使用复合棋型快速评估。"""
    human_player = 1 if ai_player == 2 else 2

    # 快速检查五连
    if _check_win_fast(board, ai_player):
        return 100000000
    if _check_win_fast(board, human_player):
        return -100000000

    ai_score = _eval_player_composite(board, ai_player)
    human_score = _eval_player_composite(board, human_player)
    return ai_score - human_score * 0.95


_SCORE_TABLE = {
    # (count, live, has_jump)
    (5, True, False): 100000000, (5, False, False): 100000000,
    (4, True, False): 1000000, (4, False, False): 10000,
    (3, True, False): 10000, (3, False, False): 500,
    (2, True, False): 200, (2, False, False): 50,
    (1, True, False): 10, (1, False, False): 1,
}


def _eval_player_composite(board, player):
    """用复合棋型评估单方局面（底层扫描）"""
    score = 0
    directions = [(1, 0), (0, 1), (1, 1), (1, -1)]
    counted = np.zeros((BOARD_SIZE, BOARD_SIZE), dtype=bool)

    for r in range(BOARD_SIZE):
        for c in range(BOARD_SIZE):
            if board[r][c] == player and not counted[r][c]:
                for dr, dc in directions:
                    count, open_ends, has_jump = _analyze_line(board, r, c, dr, dc, player)
                    if count >= 5:
                        return 100000000
                    if count >= 1:
                        is_live = (open_ends >= 2)
                        key = (min(count, 5), is_live, False)
                        s = _SCORE_TABLE.get(key, 0)
                        center_dist = abs(r - 9) + abs(c - 9)
                        center_bonus = max(0, 18 - center_dist) * 0.05
                        score += int(s * (1 + center_bonus))
                        for k in range(count):
                            nr, nc = r + dr * k, c + dc * k
                            if 0 <= nr < BOARD_SIZE and 0 <= nc < BOARD_SIZE:
                                counted[nr][nc] = True
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
    复合棋型评估：检测落子后在所有方向上的棋型组合。
    返回 (五连数, 活四数, 冲四数, 活三数, 眠三数, 活二数) 以及综合评分。
    
    关键：一子同时形成双活三 → 极高分数（对手防不住）。
    """
    directions = [(1, 0), (0, 1), (1, 1), (1, -1)]
    live4_cnt = 0
    rush4_cnt = 0
    live3_cnt = 0
    sleep3_cnt = 0
    live2_cnt = 0
    win = False

    for dr, dc in directions:
        count, open_ends, has_jump = _analyze_line(board, r, c, dr, dc, player)
        if count >= 5:
            win = True
            break
        if count == 4 and open_ends >= 2:
            live4_cnt += 1
        elif count == 4 and open_ends == 1:
            rush4_cnt += 1
        elif count == 3 and open_ends >= 2:
            live3_cnt += 1
        elif count == 3 and open_ends == 1:
            sleep3_cnt += 1
        elif count == 2 and open_ends >= 2:
            live2_cnt += 1

    if win:
        return 100000000

    # 复合棋型评分
    score = 0

    # 活四
    if live4_cnt >= 1:
        score += 5000000
    if live4_cnt >= 2:
        score += 50000000  # 双活四必胜

    # 冲四
    if rush4_cnt >= 1:
        score += 500000
    if rush4_cnt >= 2:
        score += 10000000  # 双冲四必胜

    # 活三
    if live3_cnt >= 1:
        score += 50000
    if live3_cnt >= 2:
        score += 5000000   # 双活三必胜！对手防不住

    # 冲四 + 活三组合（也是必胜）
    if rush4_cnt >= 1 and live3_cnt >= 1:
        score += 3000000

    # 眠三
    if sleep3_cnt >= 1:
        score += 5000
    if sleep3_cnt >= 2:
        score += 100000

    # 活二
    if live2_cnt >= 1:
        score += 1000
    if live2_cnt >= 2:
        score += 8000

    return score


def _quick_eval_move(board, r, c, player):
    """快速评估单个落子的价值（用于启发式排序），含复合棋型"""
    board[r][c] = player

    # 进攻评估
    attack = _composite_eval(board, r, c, player)

    # 防守评估
    opp = 1 if player == 2 else 2
    defense = _composite_eval(board, r, c, opp)
    # 防守分折算
    defense = defense // 2

    board[r][c] = 0

    # 中心位置加分
    center_dist = abs(r - 9) + abs(c - 9)
    center_bonus = max(0, 18 - center_dist) * 5

    return attack + defense + center_bonus


def _order_moves(move_list, board, current_player, depth):
    """
    排序候选落子：杀手 → 历史启发 → 复合棋型评估。
    （无 Zobrist / 置换表）
    """
    scored = []
    for r, c in move_list:
        # 1. 杀手走法最高优先
        if _is_killer(depth, r, c):
            priority = 5000000000
        else:
            # 2. 历史启发 + 复合棋型评估
            hist = _get_history(current_player, r, c)
            eval_score = _quick_eval_move(board, r, c, current_player)
            priority = hist + eval_score
        scored.append((priority, r, c))
    scored.sort(reverse=True)
    return scored


def alpha_beta(board, depth, alpha, beta, maximizing, ai_player, ply=0):
    """
    标准 Alpha-Beta 剪枝 + 杀手启发 + 历史启发。
    （无 PVS / Zobrist / 置换表）
    """
    human_player = 1 if ai_player == 2 else 2

    # 终局判断
    if _check_win_fast(board, ai_player):
        return 10000000 + depth
    if _check_win_fast(board, human_player):
        return -10000000 - depth
    if depth == 0:
        return evaluate_board(board, ai_player)

    all_moves = _generate_moves(board)
    if not all_moves:
        return 0

    current_player = ai_player if maximizing else human_player
    move_scores = _order_moves(all_moves, board, current_player, depth)

    # 分支限制
    max_branch = 30 if depth <= 1 else 25
    if len(move_scores) > max_branch:
        move_scores = move_scores[:max_branch]

    if maximizing:
        best_val = float('-inf')
        for _, r, c in move_scores:
            board[r][c] = ai_player
            val = alpha_beta(board, depth - 1, alpha, beta, False, ai_player, ply + 1)
            board[r][c] = 0

            if val > best_val:
                best_val = val
            alpha = max(alpha, val)
            if beta <= alpha:
                _record_killer(depth, r, c)
                _record_history(ai_player, r, c, depth)
                break
        return best_val
    else:
        best_val = float('inf')
        for _, r, c in move_scores:
            board[r][c] = human_player
            val = alpha_beta(board, depth - 1, alpha, beta, True, ai_player, ply + 1)
            board[r][c] = 0

            if val < best_val:
                best_val = val
            beta = min(beta, val)
            if beta <= alpha:
                _record_killer(depth, r, c)
                _record_history(human_player, r, c, depth)
                break
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


def _find_forced_win(board, player, max_depth=4):
    """
    Threat-Space Search: 搜索强制获胜序列（VCF/VCT）。
    
    只搜索 player 的进攻路线（冲四/活四），枚举对手必须堵的位置。
    加入 visited 集合和时间上限避免死循环和性能爆炸。
    """
    import time as _tss_time

    opp = 1 if player == 2 else 2
    _tss_start = _tss_time.time()
    _tss_limit = 1.5  # TSS 最多跑 1.5 秒
    _tss_visited = set()

    def _tss_endpoints(board, r, c, p):
        """找到(r,c)处p棋子形成的4+连子的所有空位端点（对手必堵位置）"""
        ends = set()
        for dr, dc in [(1, 0), (0, 1), (1, 1), (1, -1)]:
            # 向正反方向扫描连续p棋子
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
                # 正方向端点
                nr1, nc1 = r + dr * pos_cnt, c + dc * pos_cnt
                if 0 <= nr1 < BOARD_SIZE and 0 <= nc1 < BOARD_SIZE and board[nr1][nc1] == 0:
                    ends.add((nr1, nc1))
                # 反方向端点
                nr2, nc2 = r - dr * neg_cnt, c - dc * neg_cnt
                if 0 <= nr2 < BOARD_SIZE and 0 <= nc2 < BOARD_SIZE and board[nr2][nc2] == 0:
                    ends.add((nr2, nc2))
        return ends

    def _tt(board_tuple, player, depth):
        nonlocal _tss_start, _tss_limit, _tss_visited

        if depth == 0:
            return None
        # 超时保护
        if _tss_time.time() - _tss_start > _tss_limit:
            return None
        # visited 防重复搜索
        key = (board_tuple, player, depth)
        if key in _tss_visited:
            return None
        _tss_visited.add(key)

        board = np.array(board_tuple, dtype=int).reshape(BOARD_SIZE, BOARD_SIZE)
        moves = _generate_moves(board)

        for r, c in moves:
            if _tss_time.time() - _tss_start > _tss_limit:
                return None

            # 只关注能形成冲四/活四的走法
            has_live4, has_rush4, _, _ = _get_pattern_types(board, r, c, player)
            if not has_live4 and not has_rush4:
                continue

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
    TSS 增强版威胁检测：
    1. 自己能五连 → 直接赢
    2. 对手能五连 → 必须堵
    3. TSS 强制获胜序列 → 走第一步
    4. 自己能形成活四（必胜） → 走这里
    5. 对手能形成活四 → 必须堵
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

    # 3. TSS 强制获胜序列（VCF/VCT）
    # 仅在棋局中期尝试（棋子数 > 4），避免初期浪费计算
    stone_count = np.count_nonzero(board)
    if stone_count >= 6:
        forced_seq = _find_forced_win(board, player, max_depth=4)
        if forced_seq:
            return forced_seq[0]

    # 4. 自己能否形成活四（必胜局面）
    my_live4 = _find_live_four_moves(board, player)
    if my_live4:
        return my_live4[0]

    # 5. 双活三检测（也是必胜）
    moves = _generate_moves(board)
    for r, c in moves:
        has_live4, has_rush4, has_live3, _ = _get_pattern_types(board, r, c, player)
        if has_live3:
            # 检查是否同时形成两个活三
            board[r][c] = player
            live3_dirs = []
            for dr, dc in [(1, 0), (0, 1), (1, 1), (1, -1)]:
                count, open_ends, _ = _analyze_line(board, r, c, dr, dc, player)
                if count == 3 and open_ends >= 2:
                    live3_dirs.append((dr, dc))
            board[r][c] = 0
            if len(live3_dirs) >= 2:
                return (r, c)

    # 6. 对手能否形成活四（必须提前堵）
    opp_live4 = _find_live_four_moves(board, opp)
    if opp_live4:
        return opp_live4[0]

    return None


def ai_move(board, ai_player, depth):
    """
    AI 主入口：TSS 威胁检测 + PyTorch GPU batch 根评估 + 迭代加深 Alpha-Beta。
    
    流程：
    1. TSS 立即威胁检测（冲四/活四/VCF）
    2. PyTorch GPU 批量评估所有候选落子
    3. 迭代加深标准 Alpha-Beta 搜索
    """

    # 立即威胁检测（含 TSS）
    threat = _check_immediate_threat(board, ai_player)
    if threat:
        return threat

    moves = _generate_moves(board)
    if not moves:
        return (9, 9)

    # 第一步优化
    stone_count = np.count_nonzero(board)
    if stone_count <= 1:
        if board[9][9] == 0:
            return (9, 9)
        return random.choice(moves)

    # 根据难度设定目标深度
    if depth == 1:
        target_depth = 1
    elif depth == 2:
        target_depth = 2
    else:
        target_depth = 4

    # === PyTorch GPU 批量评估：对所有候选落子打分排序 ===
    move_scores = _batch_eval_moves(board, moves, ai_player)

    # 限制顶层分支数
    max_branch_top = 15 if target_depth >= 2 else 10
    if len(move_scores) > max_branch_top:
        move_scores = move_scores[:max_branch_top]

    # === 迭代加深搜索 ===
    best_move = move_scores[0][1], move_scores[0][2]

    for cur_depth in range(1, target_depth + 1):
        local_best_move = best_move
        best_val = float('-inf')

        # 上次最优放第一位
        iter_moves = []
        for score, r, c in move_scores:
            if (r, c) == best_move:
                iter_moves.insert(0, (score, r, c))
            else:
                iter_moves.append((score, r, c))

        for _, r, c in iter_moves:
            board[r][c] = ai_player
            if _check_win_fast(board, ai_player):
                board[r][c] = 0
                return (r, c)

            val = alpha_beta(board, cur_depth - 1, float('-inf'), float('inf'), False, ai_player, ply=1)
            board[r][c] = 0

            if val > best_val:
                best_val = val
                local_best_move = (r, c)

        best_move = local_best_move

        # 找到必胜路线，提前结束
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
            "正在加载AI神经网络权重...",
            "正在构建PyTorch评估张量...",
            "正在优化GPU计算图...",
            "正在准备棋盘渲染管线...",
            "正在校准评估函数...",
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
        copyright_label = QLabel("基于 Alpha-Beta 剪枝 · PyTorch GPU 加速 · 迭代加深")
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
        global _killer_moves, _history_table
        _killer_moves = [[None, None] for _ in range(MAX_DEPTH)]
        _history_table = np.zeros((2, BOARD_SIZE, BOARD_SIZE), dtype=np.int32)

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

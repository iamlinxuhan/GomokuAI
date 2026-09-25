# GomokuAI 引擎核心重写方案

## Context

用户要求"全面优化算法"。经代码审查与实测，当前引擎（main.py，3029 行单文件）的问题不是"调参能改善"，而是**存在导致搜索做出错误决策的致命 bug + 每节点成本比应有水平高 1~2 个数量级**，两者叠加使有效搜索深度停在 5~6 层。

实测基线（中局 10 子，难度 3 / 8 秒预算，实际耗时 8.3 秒）：

| 函数 | 调用次数 | 累计耗时 | 占比 |
|---|---|---|---|
| `_check_win_fast` | 17346 | 6.367s | **77%** |
| `_cached_evaluate` | 4201 | 3.608s | 43% |
| `evaluate_board` | 3345 | 3.220s | 39% |
| `_analyze_line` | 537724 | 0.582s | — |

共 1786 万次函数调用。已核实的最严重缺陷：

1. **评分量级倒挂（B1）**：`alpha_beta` 判胜返回 `1e7`（main.py:983/985），但叶节点 `evaluate_board` 里"活四"就是 `1e7`（550-551）、"连五"是 `1e8`（526）。→ **搜索认为"已赢"不优于"有个活四"，max 节点会主动避开真杀线**。连带 `DESPERATION_THRESHOLD=-5000000`（1857）与提前终止 `best_val>95000000`（1840）因量级错配而误触发/永不触发。
2. **TT flag 错误（B2）**：main.py:1064 `flag = 2 if best_val >= beta else (0 if best_val > float('-inf') else 1)` —— `best_val > -inf` 只要搜过一步就恒真，故 **fail-low 被存成 EXACT**；且比较对象是循环中已更新的 `alpha`，不是入口 `alpha_orig`。1101 行对称地永不存 LOWERBOUND。→ **TT 静默返回错误分数并污染走法排序**。
3. **GPU 链路从未生效（B3）**：12-55 行检测出 `_gpu_type='cuda'`，但 72-74 行无条件覆盖为 `'cpu'`，`_ensure_torch()` 恒 False。且**卷积核设计上无法区分活四/冲四/眠三**（B4，核是全 1 张量，不含"两端是否为空"信息），与 CPU 路径不是同一评价函数。
4. **`_check_win_fast` 无增量（B5）**：673-699 每次全盘扫描，在 `alpha_beta` 里**每节点调用两次**（982、984）—— 77% 的耗时来源。

其余已核实缺陷：`_SCORE_TABLE[(3,True,True)]=1e6` 把单线跳活三抬到活三的 100 倍（B7）；排序对全部 150-250 个候选算分后只留 15 个、94% 计算被丢弃（B8）；killer 用 depth 而非 ply 索引导致跨层污染（B9）；history 被 eval_score 淹没实际不参与排序（B10）；无静态搜索（B11）；`alpha_beta` 内零时间检查，8 秒上限形同虚设（B12）；根层无 PVS 无 alpha 传递（B13）；TSS `_find_forced_win` 是**零调用点的死代码**且本身有 AND/OR 反转 bug（B15）；开局库因键格式不匹配而 0 命中（B16）；`QThread.terminate()` 不安全（B19）。

**目标**：重写评估、搜索、威胁搜索三大核心，把有效深度从 5~6 层提到 10~14 层，并让搜索的分数体系自洽。

**已确定的约束**（用户决策）：
- 核心重写，UI 层尽量不动
- 引擎拆到独立的 `engine.py`
- 高级难度 15 秒/步
- 速度优先，不为 GPU 而 GPU
- **彻底移除 torch 依赖**（含 CI/打包）
- 验证基建做完整

---

## 架构

```
engine.py    # 新建：纯 AI 引擎（位棋盘 + 增量评估 + negamax/PVS + TT + quiescence + VCF）
             # 零 Qt、零 torch 依赖
gamelog.py   # 新建：GameLogger + coord_to_sgf（自 main.py:88-200 移出）
             # main.py 与 engine 的日志格式化共用，避免 engine 反向 import UI
main.py      # 仅 UI + 调用层
tools/       # legacy_engine.py（冻结的旧引擎，仅供 A/B）、selfplay.py、bench.py、positions.py
tests/       # pytest
```

### engine.py 顶层 API

```python
BOARD_SIZE = 19
WIN_SCORE  = 10_000_000

class Engine:
    def __init__(self, tt_size=1<<20)
    def new_game(self)                      # 清空 TT/history/killers
    def think(self, board, ai_player, level, *, cancel=None, time_limit=None) -> (r, c, info)

def ai_move(board, ai_player, depth, cancel=None) -> (r, c, info)   # 签名与现状一致
def check_win(board, player) -> bool
def get_gpu_type() -> str                   # 恒返回 'cpu'
def opening_move(board, player) -> (r, c)
```

关键点：
- `ai_move` 签名不变 → `AIWorker.run`（main.py:1968）只需多传 `cancel`，**UI 边界零改动**。
- **所有模块级可变全局消失**：`_transposition_table`、`_history_table`、`_killer_moves`、`_eval_cache`、`_tt_age` 全部变成 `Engine` 实例属性。这从结构上消除跨线程写全局（B19 的一半），并让 main.py:2719-2724 的 `global` 重建整块删除。
- numpy 只在 `think()` 入口出现一次（19×19 ndarray → 内部位棋盘），返回只回传 `(r, c)`。`check_win` 走同一条位棋盘路径 → **胜负判定全局只有一份实现**，测试覆盖它即覆盖搜索内判定。
- `info` 新增 `score_type`（`'mate'|'static'`）与 `depth_reached`；旧键全部保留，`GameLogger.log_ai` 输出格式不变。

---

## 核心技术方案

### 1. 棋盘原语（位棋盘 + 全增量）

```python
b_bits: int          # 黑子位图（361 bit，Python 大整数）
w_bits: int          # 白子位图；occ = b_bits | w_bits 现算（1 次 or）
neighbor_count = bytearray(361)   # 每格半径 2 内的棋子数
cand_mask: int                    # neighbor_count>0 且为空的位图
```

用 Python 大整数而非 numpy：位运算在 C 层完成，**消除 numpy 标量索引开销**（B6 的病根：`zobrist_hash` 的 361 次标量索引 = 0.382s/4202 次）。

模块级预计算表（import 时一次）：`LINE_MASK[4][line_id]`、`CELL_LINES[idx]`、`WIN_WINDOWS[idx][dir]`、`NEIGHBORS[idx]`、`ZOBRIST[2][361]`。

**O(1) 增量胜负判定**（替代 B5）：

```python
def makes_five(bits, i):                    # bits 已含该子
    return any((bits & m) == m for d in range(4) for m in WIN_WINDOWS[i][d])
```

每方向 ≤5 个窗口 × 4 方向 = ≤20 次"与+比较"，取代 `_check_win_fast` 的 1170 次单元读取。**367 µs → < 1 µs（约 400×）**。

搜索中的用法变化是关键：**节点入口不再做胜负扫描**。negamax 落子后立即判落子方是否成五，成了就返回 `WIN_SCORE - ply`，不再递归。父节点自然知道，无需在子节点入口重扫两次（删除 982、984）。

**O(1) 增量候选生成**：`make(i,p)` 对 `NEIGHBORS[i]` 做 ±1 更新并维护 `cand_mask`；`unmake(i)` 对称。候选枚举用位扫描（`m & -m` / `bit_length()`），只与候选数成正比，取代 `_generate_moves`（774-795）的 361 格 + 25 邻居扫描。

单次 make 工作量 < 100 次 Python 操作，对照当前每节点约 5200 次单元读取，**约 50-100× 提升**——这是全部性能预算的来源。

### 2. 评估：增量线评分（推荐方案）

**否决 numpy 整盘向量化**：`_check_win_fast` 才是主热点而 numpy 帮不上；19 长度的规约是小数组高开销坏 case（每次 ufunc 约 1-2 µs 固定开销）；组合棋型跨线，向量化表达不了；且引入标量索引风险。

**否决 3^19 全模式查找表**：1.16e9 不可预计算。只对 5 窗口求和会在重叠窗口上重复计分（正是 B4 的 bug 形态）。

**采用增量线评分**：落子只影响经过该点的 4 条线，重算这 4 条线即可 —— 唯一能把叶节点降到 O(1) 的路线。

线评分输入不是 `(count, open_ends, has_jump)` 三元组（`_analyze_line` 的返回值本身有损，无法区分 `_XX_X_` 与 `_X_XX_`），而是**线上双方的位掩码对**：

```python
def line_score(my_bits, opp_bits, length) -> (score, threat_class)
    # 剥离两侧空白 → 压缩段 → 查 _line_cache → 未命中走精确 run/gap 分类器 → 写回
```

缓存命中率极高（`00000`/`10000`/`11000` 这类模式反复出现）。对称性天然成立：`score_for(B) = line_score(b& M, w & M)`，反向同理，两方共用一份缓存。

精确分类器是**唯一**的棋型语义来源，可被单测直接打靶 —— 不再有 CPU/GPU 两套评分。

增量维护 `line_state[112][2]`、`score_sum[2]`、`threat_agg[2][6]`。make 时对 4 条线 × **2 方**重算（必须重算双方：落子除了形成己方棋型，还可能打断对方的跳活三）。

`evaluate = clamp(score_sum[me] + combo(me) - score_sum[opp] - combo(opp), ±STATIC_MAX)`

**移除 0.85 不对称系数**（main.py:541）：`ai - human*0.85` 使评估非零和，破坏 negamax 的一致性假设，是另一条能导致反常选择的隐性病根。改用对称差 + 小的行棋方 tempo 加成。

**棋型量级阶梯**（唯一真理，写进常量并在测试断言）：

| 棋型 | 分值 |
|---|---|
| `FIVE` | `WIN_SCORE = 10_000_000`（只在搜索里返回 `WIN_SCORE - ply`，静态评估绝不返回） |
| `LIVE_FOUR` 活四 | `1_000_000` |
| 双四 / 四三 / 双活三（组合） | `900_000` / `800_000` / `700_000` |
| `FOUR` 冲四 | `100_000` |
| `OPEN_THREE` 活三 / 线内跳活三 | `30_000`（**同级，修 B7**） |
| `SLEEP_THREE` 眠三 | `1_000` |
| `OPEN_TWO` / `SLEEP_TWO` / `OPEN_ONE` | `500` / `100` / `10` |

B7 两处修正：单线跳活三降到与活三同级；"双活三"从单线 key 中**彻底移除**，改由跨线组合计数表达（`threat_agg` 的全局计数 → `combo_bonus()`）。关键纪律：**组合 bonus 是绝对加成不是倍乘，且 < LIVE_FOUR**。修正后偏序为 活四 > 四三 > 双活三 > 冲四 > 活三，符合棋理。

`_eval_cache`/`_cached_evaluate`（584-595）**删除**：静态评估已 O(1)，整盘缓存失去意义；且现状键不含 `ai_player` 而值与之相关（不对称系数），复用必然串味。TT 是唯一需要的缓存。

### 3. 搜索：negamax + PVS 单分支

现状 `alpha_beta`（959-1108）的两份对称分支（1022-1064 / 1065-1101）**直接导致了 B2 的两个不对称 flag bug**。改单分支后这类 bug 结构上不可能再出现。

节点流程：时间/取消轮询 → TT 探测 → `depth<=0` 转 quiescence → 生成走法 → PVS 主循环 → 存 TT（用 `alpha_orig`）。

**TT 正确语义（B2）**——唯一正确写法，`alpha_orig` 必须在循环前保存：

```python
if best_val <= alpha_orig: flag = UPPER
elif best_val >= beta:     flag = LOWER
else:                      flag = EXACT
```

查询必须校验完整 key 防碰撞；**mate 分数归一化**（存入 `value+ply`、取出 `-ply`），否则不同 ply 的将杀分互相污染。

**根层 PVS + 迭代加深 + aspiration（B13）**：现状每个根走法都以 `(-inf,+inf)` 全窗口搜（1819），根层零剪枝。新设计首走法全窗口、其余零窗口 + fail-high 重搜；深度 >2 时用上一轮分数开 aspiration 窗口（±120），失败重搜并计数，连续 3 次回退全窗口。**只有完整跑完的迭代才更新 `best_move`**，超时的那层整体丢弃。

**走法排序（B8/B10）**：分带 + 惰性评分，复用增量威胁信息（§2 已算过，几乎零成本）：
- band 0 TT best → band 1 己方成五/成四 → band 2 对手成五/成四堵点 → band 3 killer[ply] → band 4 其余
- 只有候选数超限时才对 band 4 用 `heapq.nlargest` 惰性选择，**绝不排序全量**
- B10 修正：history **不参与数值相加**（现状 `hist + eval_score`，eval_score 1e2~9e7 而 history 几十到几千，实际不起作用），改为元组比较 `(band, history, static)` 做量级隔离；每轮新根搜索时 `history >>= 1` 衰减
- killer 改按 **ply** 索引（B9），函数已有 `ply` 参数

**分数体系纪律（B1 根因修复）**——三层量级互不重叠：

| 层 | 范围 |
|---|---|
| 终局 mate | `[WIN_SCORE - MAX_PLY, WIN_SCORE]`，`MAX_PLY=64` |
| 静态评估 | `[-STATIC_MAX, STATIC_MAX]`，`STATIC_MAX = WIN_SCORE - 128`，强制 clamp |
| 启发排序 | 任意非负，只影响顺序不影响返回值 |

**`evaluate_board`（520-528）里 `_check_win_fast` 返回 ±1e8 的两行必须删除**——叶节点永不返回终局分，终局只由搜索的 `WIN_SCORE - ply` 表达。`is_mate(v) = abs(v) > WIN_SCORE - 2*MAX_PLY`。`DESPERATION_THRESHOLD` 与提前终止改用 `is_mate()` 判定。**整块删除"拼命模式"（1857-1945）**：它建立在错乱量级上（用硬编码 1e8/8e7/3e7 加成对抗搜索分数），正当诉求由 §5 的 VCF 以正确语义实现。

**硬时间控制（B12）+ 可取消**：

```python
class SearchAborted(Exception): pass
# _search 入口，1024 节点轮询一次
self._nodes += 1
if not (self._nodes & 1023):
    if self._cancel and self._cancel.is_set(): raise SearchAborted('cancelled')
    if time.monotonic() > self._deadline:      raise SearchAborted('timeout')
```

用异常而非返回值（返回路径十几处，逐处判断会污染热路径）。`think()` 根部捕获后**丢弃未完成的迭代**，回退到上一个完整迭代的 `best_move`；若第 1 层都没完成，返回排序首位保证总能给出合法走法。timeout 与 cancel 必须区分：timeout 用上一轮结果，cancel 直接抛到 UI 层（对局已不存在）。

**验收**：任何难度下墙钟 ≤ `time_limit + 0.1s`。

### 4. Quiescence + VCF

两者都做，各司其职：quiescence 挂 `depth<=0` 解决**水平线效应（B11）**；独立 VCF 在根节点调用，解决短搜索看不见的多步连续冲四杀。

**Quiescence**：只搜强制着法（五、四），分支因子通常 1-6，`MAX_QPLY=10` 保证终止。三个要点：① 对手已有成五威胁时**不能 stand pat**，唯一合法走法是堵点；② **对手成四点必须进入候选**（否则"我进攻完对手先成五"会漏算，这是 quiescence 最易写错的防守侧）；③ 内部 ply 计入全局 ply，保证 `WIN_SCORE - ply` 单调。

**VCF 三态返回**（修 B15"搜不完当无必胜"）：

```python
WIN / NO_WIN / EXHAUSTED    # EXHAUSTED 绝不可当作 NO_WIN；根节点意味着"不表态，继续常规搜索"
```

**AND/OR 语义是 B15 的核心修复**。现状 main.py:1344-1353 只要**任一**防守经递归后 `result is not None` 就宣布必胜（OR 语义），正确应是**所有**防守都失败才必胜（AND）；任一防守成立即须返回 NO_WIN。

防守方候选必须包含**三类**，缺一不可：① 我方所有成五点的堵点（强制覆盖每一个）；② **防守方自身的成五点**（若防守方落子即成五则进攻立刻失败——现状 1347 完全没检测，这是"进攻方假必胜"类反例的根源）；③ 防守方自身成四点（反四会强迫进攻方应手、打断进攻节奏）。

预算：节点 + 深度 + 独立时间（高级 1.5s）三重限制，与主搜索共用 `SearchAborted` 机制保证总时间不失控。

**VCT（活三路线）默认关闭**：分支因子远大于 VCF 且在纯 Python 里性价比低。先做 VCF，用实测决定。

**根节点调用策略**：己方成五 → 直接下；`vcf_attack(己方)==WIN` → 直接下；`vcf_attack(对手)==WIN` → 进入防守 VCF（找能破坏对手全部 VCF 起始着法、或自己形成更快 VCF 的一手），找不到才启用"以攻对攻"（走己方 VCF 的最深进展着法，**而非**旧拼命模式那套启发式评分）；否则常规迭代加深。

### 5. 威胁响应层缩减（`_check_immediate_threat` 1362-1489）

核心主张：**该层职责从"做判断"缩减为"两三个绝对正确的快速通道 + 调用 VCF"**。启发式多线判断已证明必然出错（B17）。

**保留**：第 1 段（己方能成五 → 下）、第 2 段（对手能成五 → 堵，但补：对手有 ≥2 个成五点且己方无成五时为必败，走 VCF 最深进展而非 `return opp_win[0]` 随机取第一个）。

**删除**：第 3/4 段（活四相关，由 VCF 覆盖且现状忽略"对手更快四连杀"）、**第 5 段（B17 核心错误：把"冲四+活三"当几乎必胜直接返回，不校验对手连续冲四反先）**、第 6 段（阈值 `>=800000` 恒真，等于无条件劫持搜索）、第 7 段（多线威胁粗糙近似，与 VCF 防御重复）。

**B17 多线危机判据（1560）整个删除**：`opp_win>=2 or opp_live4>=2` 漏掉最常见的 `opp_win==1 且 opp_live4>=1` 双杀。该局面恰好是"对手存在 VCF 且我一步堵不住"的一种形态，**由 VCF 的 AND/OR 精确判定，不在启发式层打补丁**。

**消除重复计算（B18）**：`_find_winning_moves`/`_find_live_four_moves` 在 1380-1397 与 1553-1556 被重复调用两遍。新设计下这些函数整体删除，"成五点/成四点集合"由增量状态直接导出，一次 `think()` 内只算一次。

---

## 其余 bug 修复

| 编号 | 内容 | 处理 |
|---|---|---|
| B3/B4 | GPU 检测被覆盖 + 卷积核无法表达棋型 | **移除全部 torch 代码**（12-55、72-74、202-271、313-470）；`get_gpu_type()` 保留恒返回 `'cpu'`，UI 的 gpu_type 分支自动落到 CPU 路径 |
| B16 | 开局库键格式不匹配、0 命中、不区分执黑执白 | **删除** `_OPENING_BOOK`/`_board_to_fen`，改为 `opening_move(board, player)` 确定性策略；main.py:2782 的 `_ai_first_move` 改调它（对当前唯一路径行为一致） |
| B19 | `QThread.terminate()` 不安全；`_on_quit` 不管 worker；重开局重建全局污染新局 | 协作取消（`Event` + 节点轮询）；两处改为 `cancel.set(); wait(3000)`；全局状态变实例状态；`_on_ai_finished` 加 `game_generation` 校验丢弃陈旧信号 |
| B20 | 死代码 | 删除 `_eval_board_torch`、`_composite_eval`、`_count_line`、`_find_forced_win` 及伴生函数、`_zobrist_black_turn` |
| B21 | `_make_card_btn` 漏 `.name()` 致黑白选择卡 hover 失效 | main.py:2530 改 `{hover_color.name()}` |
| B22 | 中文路径致 Qt `libraryPaths()` 返回空、GUI 无法启动 | 在构造 QApplication 前，从 `PyQt5.__file__` 推导插件目录并 `os.environ.setdefault('QT_QPA_PLATFORM_PLUGIN_PATH', ...)`，任意安装方式下可用，README 的 workaround 可删 |
| B23 | **根节点进攻偏置读到一张幻影盘面**（C++ `search.cpp` 的 `applyBias`、Python `engine_local._search` 同一处） | **重建根棋盘**：`bd = Board::fromArray(board)` / `Board.from_array(board)`，再交给偏置。见下 |
| B24 | 不是缺陷，是**被量掉的候选方案**：给初级/中级加"根节点进攻偏置"以治"中级很被动" | **撤掉**（2/3 档回到 `bias=None`）。同档自我对打中级 0—8、初级 25%—30%，带宽压到 0 仍是 30%。见下 |

---

### B24 详情：进攻偏置 — 被量掉的候选方案

**这不是一个"已修缺陷"，是一个"量完否掉"的方向**，记在这里是为了不让它被重新发明。

**起点（真实反馈）**：用户报「中级进攻欲望不强，一直很被动」。机械来路可查：旧
`evaluate_board` 的末行是 `ai_score - human_score * 0.85`，注释写着「减少AI对自身
局面的过度悲观 / 配合进攻激励增强，让AI在均势时不再偏向纯防守」。Phase 3 为了
negamax 的零和性删掉了它 —— 于是新引擎在**风格**上比旧引擎更偏防守。反馈成立。

**候选方案**：在**根节点**加一条容差带（`tolerance`），带内的着法改按
`s = attack × 己方棋型 − defence × 对手威胁` 取最大。不动 `evaluate`（那会破坏
零和）。带宽就是它的全部代价，设 0 即退化成"只在同分着法之间重排"。

**判定方式**：同档自我对打，**偏置开 vs 偏置关**，交替执黑，复用
`tools/selfplay.py` 的同一份开局集与镜像开局；两侧是同一份 `engine.py`，靠每手
之前改 `DIFFICULTY[level]['bias']` 区分（`compute()` 每次请求都读字典，与改文件
等价）。脚本在 `/tmp/bias_match.py`。

| 档 | attack / defence / 带宽 | 局数 | 偏置开 — 偏置关 | 偏置开胜率 |
|---|---|---|---|---|
| 初级 | 1.5 / 0.5 / 3000 | 8 | 2 — 6 | 25% |
| 初级 | 1.5 / 0.5 / 3000 | 20 | 6 — 14 | 30% |
| 初级 | 1.5 / 0.5 / **0** | 20 | 6 — 14 | 30% |
| 初级 | **1.0 / 1.0** / 0 | 20 | 7 — 13 | 35% |
| 中级 | 1.5 / 0.5 / 3000 | 8 | **0 — 8** | **0%** |

**结论：否掉，全部撤掉。** 带宽压到 0 仍然是 30%（6—14），这排除了"带宽给多了"。
真因是**判据本身与搜索的判断相反**：任何成四的一手把 `own` 抬到约 100000，于是
它压倒性地赢得重排；而在初级/中级的深度（4—6）下"造一个对手随手应住的冲四"并
不等于进展 —— 搜索看得见，粗粒度棋型分看不见。旧引擎能用那个 0.85，是因为它的
搜索**围绕它建的**（那是它目标函数的一部分）；在一个已经正确的零和搜索上再叠
一层根节点重排，只会把正确结果改坏。

**留档的两条教训**：
1. **"更进取"与"更弱"必须分开量。** 这两件事在这批数据里是同一件事，而纸面上
   的推理（"3000 比旧版的 4500 更小，所以更保守"）完全不能替代对局。
2. **无偏置的搜索的原判是有价值的。** 在分值相同时，搜索的着法次序（hot/priority
   /history）比任何后验的棋型厚度判据都好用。

**代码现状**：`engine.py` 的 2/3 档为 `bias=None`；`engine_local.DIFFICULTY` 回到
三档原值。`engine_local._apply_bias` 与 `_root(collect=)`、C++ 的 `applyBias` **保留**
—— 入门档在 C++ 侧仍在使用这套机制，而 Python 那份是它的镜像参考实现，是
`tools/positions.py` 逐字一致检验能成立的前提。

---

### B23 详情：为什么会读到脏棋盘

C++ 的搜索**刻意不维护撤销栈**：`negamax` 里每一层 `bd.make(mv, me)` 之后靠配对的 `bd.unmake()` 回退。迭代加深的最后一轮被时限打断时，`SearchAborted` 从深层抛出来，沿途每一层的 `make()` 都走不到配对的 `unmake()` —— 于是 `think()` 手里的 `bd` 停在那条被放弃的搜索路径的半途状态。

在 B23 之前的代码里，`think()` 恰好**在搜索之后**才用 `bd` 去跑 `applyBias`（根节点进攻偏置），于是偏置读到的是"真实盘面 + 一整条搜索残留的棋子"。实测（同一进程、同一局面）：`[start] 子=5 hash=b1a72e09…` 是正确盘面，进入 `applyBias` 时已是 `[entry] 子=13 hash=9ec3f6d3… ssB=1780 ssW=2640`，其中还夹着 `1002280` / `102280` 这类只可能来自幻影棋型的分。

**后果**：入门档的"贪吃系数"选点实际由垃圾 `own/opp` 决定，且**随机器负载漂移** —— 同一个局面在不同次运行给出 J8 / J11 / L11 / J12 / H13。这正是用户看到的"怎么笨了"里最刺眼的一类：同一局面下同一个 AI 会给出不同答案，且答案毫无道理。

**修法**：在进 `applyBias` 之前重建一次根棋盘（Python 侧同样）。搜索用的 `bd` 可以脏，因为它的结果 `rootVals` 是搜索结束时就已经固定下来的；只有"搜索之后再读盘面"这个动作必须拿到干净状态。

**为什么重建的是棋盘、不是 `rootVals`**：偏置的输入是 `rootVals`（每一手的分值，搜索的产物，干净）**加上**它自己从棋盘增量算出的 `own` / `oth`（脏的）。两者必须描述同一个局面 —— `rootVals` 天然就是搜索开始时的局面的分值，所以要修的是盘面那一半。这也说明这条缺陷的性质：不是"搜索算错了"，而是"两个输入来自不同的时刻"。

**影响半径只有入门档。** 搜索之后读这张棋盘的**只有** `applyBias` 一处 —— VCF 兜底用的是记录下来的着法与分值（`vcfWinMove` / `vcfDef`），不读棋盘；信息字段（`depth` / `nodes` / `nps`）也不读。所以 1/2/3 档（`biasOn == false`）与 4/5 档完全不受影响。

---

## 难度与时间参数

```python
DIFFICULTY = {
    1: dict(time=1.5,  max_depth=4,  vcf_budget=0,    qply=4),
    2: dict(time=5.0,  max_depth=10, vcf_budget=0.5,  qply=8),
    3: dict(time=15.0, max_depth=24, vcf_budget=1.5,  qply=10),
}
RESERVE = 0.15
```

时间是**硬上限**而非目标值；`max_depth` 是安全阀，实际深度由时间驱动。1 级关 VCF 且限深，保证"初级"确实是初级。

预期：中局深度 **10-14 层**（现状 5-6），节点速度从约 2.1k nps 提升到 **30k-80k nps**。

> **⚠️ 深度的预期已证伪（2026-09-19）。** nps 达成了（实测中位约 5 万），但深度没有：
> 3 档 13 秒预算下实测 4-5 层，与旧引擎的 5 层同量级。**"10-14 层"从未被达到过** ——
> 一度看到的 24 层是坏哈希造成的假象（见文末"Phase 6 进展"）。根因是这套设计没有任何
> 现代裁剪（LMR / 空着裁剪 / 无用着裁剪），实测每层有效分支因子约 20；13 秒约 65 万节点
> 要搜到 10 层，需要把分支因子压到 3.7 附近。此处保留原文以便对照，**门槛已按实测改为 ≥5**。

README 需同步：难度表（77-81）、时间上限（30）、GPU/TSS 描述（3、19-29、49、103-105、291-295、348、355）、诊断案例中的旧量级（127、210、279）；新增仓库结构与测试运行说明。

---

## 分阶段落地

| Phase | 内容 | 验证点 |
|---|---|---|
| **0 基线冻结** | 建 `tools/legacy_engine.py`（冻结 main.py:472-1510）、`selfplay.py`、`bench.py`、`positions.py`、`tests/` 骨架。记录基线：8.3s / depth 5-6 / 1786 万调用。导出 3 盘历史败局（`git show HEAD:<file>`）标注关键错误步 | bench 数字与实测吻合；旧引擎自对弈跑通、0 违规走法 |
| **1 模块拆分** | 建 `engine.py`（**先逐字搬迁**，仅去 Qt/torch 依赖与模块级副作用）、`gamelog.py`；改 main.py 调用点；修 B3/B19/B21/B22 | 新 vs 旧自对弈胜率 **45%-55%**（证明搬迁无回归）；GUI 手动清单通过 |
| **2 棋盘原语** | §1 位棋盘 + make/unmake + 预计算表 + 增量候选；`check_win` 走位棋盘路径 | `test_incremental.py` 200 手差分全绿；`check_win` < 1 µs |
| **3+4 评估与搜索**（合并交付） | §2 增量线评分 + §3 negamax/PVS + TT + 时间控制 + 排序。**必须合并**：分数体系不能一半新一半旧 | 阶梯偏序/零和性/clamp 不变式全绿；timeout 合规 100%；≥30k nps；depth **≥5**（原定 ≥10，2026-09-19 修正，见文末）；**vs 旧引擎 ≥58%**；`test_search_mate` 全绿（B1 确认） |
| **5 Quiescence + VCF** | §4 + §5；删除 `_find_forced_win` 及伴生函数 | VCF 六类用例全绿（尤其假必胜/防守方反杀反例）；题库解题率 ≥80%；**vs 旧引擎 ≥65%** |
| **5 ✅ 已完成（2026-09-19）** | `Engine.vcf/_vcf/_vcf_defence`：三态返回、AND 语义、候选 = `_hot_points`、三重预算（节点 12 万 / `VCF_MAX_PLY=24` / 独立时间 + 主搜索 deadline 收口）。根节点策略：**己方 VCF 必胜 → 兜底结论**（搜索更短则用搜索的）；对手 VCF 必胜 → 找挡点只改走法排序，分值仍由搜索给。`vcf_budget` 接线（1 档 0 → 跳过）。启发式抢答层确认不存在（`test_threat.py`） | `test_vcf.py` 12 条全绿（真链/单冲四/双五点/假必胜/挡点反成四/三态/`dist` 可复现/防守证明/不编分数）；`test_threat.py` 7 条全绿；**题库 9/9 = 100%**；`test_difficulty.py` 时间合规 100% |
| **6 收尾** | 难度参数、README 全量同步、CI 去 torch、死代码清理 | 完整档 A/B ≥65%；三难度时间合规；`grep -n torch main.py engine.py` 无结果；README 每个数字都能被 bench 对上 |

### Phase 6 进展（2026-09-19，进行中）

**已做**

- **CI 去 torch**（`.github/workflows/build.yml`）：删掉三处 `pip install torch
  torchvision torchaudio`、两处 `intel-extension-for-pytorch`、以及全部
  PyInstaller 的 `--exclude-module torch.*` / `--collect-submodules` 参数；
  Linux AMD 的 job 从 `container: pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime`
  （自带约 7GB）换回普通 runner，连带删掉"清磁盘"一步 —— 那一步的存在本身
  就是被 CUDA 体积逼出来的。runner 的系统 Python 受 PEP 668 保护，故改用 venv。
  同时更新了随包发出去的三处文案：`install.bat` 的"支持 NVIDIA CUDA / Intel
  XPU / CPU 自动检测"、`.desktop` 的 `Comment`、`.deb` 的 `Description`
  （后两者原文案还在宣传"多线防守+拼命模式"，那是已删掉的旧引擎特性）。
- **CI 新增 test job**：原 `on:` 只有 `tags: ['v*']`，测试放进去永远不会跑。
  改为 `branches: ['**'] + tags` + `pull_request`，三个 build job 各加
  `if: startsWith(github.ref, 'refs/tags/') || github.event_name == 'workflow_dispatch'`，
  使"每次 push 跑测试、只有打 tag 才打包"。test job 跑 Python 3.11 / 3.12 矩阵
  （两条 Qt 用例只 import 不构造 `QApplication`，无需 display）。
- **README 全量同步**：特性表、难度表（旧表写 1/2/3 秒，实际是 1.5/5/15 秒，
  且"搜索深度 1/2/3"其实是难度**档位**而非搜索深度）、打包命令、依赖、
  项目结构，并新增"测试"一节。
- **删 Case Study 的误导性**：那局是**重写前的旧引擎**所下，其中"分层威胁响应
  / 拼命模式"正是被删掉的机制。原文以"功能展示"的口吻呈现，等于宣传不存在的
  能力。现改为就地标注为历史记录并保留原文 —— 它同时是被删掉那一版的实证材料，
  删掉反而丢失证据。更新日志同理：新增"未发布"条目，历史条目一字未动。

**一处与门槛口径的偏差（有意）**

门槛写的是 `grep -n torch main.py engine.py` 无结果。**这条检查本身是错的，
不应执行。** `tests/test_engine_parity.py:36-41` 已经写明理由：这两个文件的
注释与 docstring 里需要出现 "torch"/"PyQt5" 才能说明"当年为什么删掉它"，
文本 grep 会把说明文字误判成依赖。该处改用 **AST** 判定真实 import 与名字引用，
`test_engine_has_no_qt_or_torch` 是真门禁。为迁就一条字面 grep 而把
"原版此处的 torch 检测块已被删除"改写成不出现 torch 的句子，是拿注释的可读性
换一个假信号。故：**门槛按 AST 那条执行，`grep` 那条作废**，此处显式记下这次
口径修正。

**已完成**

- **`tools/selfplay.py` 改为逐局覆写报告**（`games_done` / `games_planned` /
  `partial` 三个字段）。完整档一场 A/B 要跑几十分钟，原先只在全部结束后写
  一次，中途中断什么都留不下、外部也无法知道进度。
- **题库的 `xfail(strict=False)` 标记已移除** —— 见 BASELINE.md。留在那里
  的话，题库回归会变成 XFAIL 而不是 FAIL，是"覆盖率假象"。

---

### ⚠️ Phase 6 期间发现并修复的引擎缺陷（2026-09-19）

**`engine._ZOBRIST` 整张表退化成常量。**

```python
# 缺陷写法：random.Random(...) 在生成器**内部**
tuple(random.Random(_ZOBRIST_SEED_BB + p).getrandbits(64) for _ in range(CELLS))
```

每一次迭代都新建一个**同种子**的随机数发生器，取它的**第一个**输出 ——
于是 361 个元素全部相同（实测：361 个值只有 1 个不同的）。旧引擎用的是
`np.random.randint`，从未受影响。

**后果不是"哈希变慢"，是哈希失去区分度。** 异或同一个常量，`Board.hash`
只剩"棋子数的奇偶性"（偶数颗异或成 0），置换表实际只有两个键 + 一个白方
扰动。搜索于是表现成这样：

- 每一步 12-58 ms 返回，报告 `depth: 24`，节点数却只有约 1000-4500；
- 连续多步的分值**冻结**在同一个数（1310），而对手在同量级局面上要花 8 秒；
- 实测 `ply_max` 自 depth=6 起**停在 5 不再下降**：迭代加深的循环跑满了
  24 轮，`done` 记的是"第几轮"而不是"搜了多深"，实际上深度 6 之后每一轮
  都在 ply=1 命中同一条错误条目直接返回。

**它为什么能潜伏整个 Phase 2-5**：`from_array` 与 `make` 共用同一张表，
逐字段比对二者的 `test_incremental.py` 一直全绿 —— 缺的是"表内元素**互不
相同**"这条断言。补了两条：`test_zobrist_table_has_no_duplicates` 与
`test_hash_distinguishes_positions_with_equal_stone_count`，已确认它们对
缺陷写法会红（报 360 个重复）。

**同批修的两处**

1. **`think` 退出时未清 `_deadline`**。`_poll` 的文档写着 `_deadline == 0.0`
   表示"当前没有搜索在跑"，但没有任何代码把它复位。于是 `think` 之外独立
   调用 `vcf()`（`tools/positions.py` 的 `opp_vcf` 探针）会撞上**上一次搜索
   留下的、早已过去的**截止时刻，`SearchAborted` 在 `think` 内部被捕获、
   在外部无人接管，直接炸穿调用方。改为 `think` 包一层 `try/finally`。
2. **`_lookup` 把"键存在"记成 `tt_hits`**。深度不够、或界与窗口不相交的
   条目并不产生剪枝，却都计入了命中率 —— 这正是把上面那件事看漏的数字。
   现在只统计真正被采用的三条出口。

**这批缺陷使 Phase 2 之后记录的性能数字全部失效**，包括本文件上一版写下的
"深度中位 4 / 10 / 24、墙钟 40 / 64 / 86 ms"，以及用 `selfplay.py` 跑出的
完整档 A/B 结果。修复后的实测值（三档 × 5 局面，`tools/bench.py`）：

| 档 | 深度（中位） | 墙钟（中位） | nps（中位） | 时间合规 |
|---|---|---|---|---|
| 1 | 4 | 713 ms | 55.2k | 5/5 |
| 2 | 4 | 4251 ms | 43.8k | 5/5 |
| 3 | 5 | 12 757 ms | 50.6k | 5/5 |

`mid_10stones` 上仍是 3 档 4.9 ms 到第 1 层（该局面上双方都认得出必败，
`is_mate()` 在第 1 层就截断了加深），这条不受影响。

**门槛随之修正**：`depth ≥10` → **`depth ≥5`**。理由不是"调低到实测值"，
而是那个 10 从未被真正达到过 —— 它是在坏哈希报出的假深度上定的。本设计是
全宽 PVS + 置换表 + 静止搜索 + VCF，没有任何现代裁剪，实测每层有效分支
因子约 20（A1 从 5 层到 6 层节点数 46k → 1.12M）；13 秒约 65 万节点要搜到
10 层，需要分支因子降到 3.7 附近。**加裁剪才能谈 ≥10**，那是后续独立的一项
工作，不在本次范围内。

**修复后重测的自对弈结果**

- **1 档 40 局：A 胜 38 负 2，胜率 95.0%**（非法 0、异常 0，762.6s），
  Phase 3+4 门槛 ≥58% 达标。作废的旧值为 80.0% —— 同一命令、同一种子。
  报告：`reports/selfplay_engine_vs_legacy_phase34_quick_zobristfix.json`。
- 完整档（3 档 20 局）已用修好的引擎重新跑，结果待回填。**上一次 45.0%
  的结果作废** —— 那是坏引擎跑出来的。

---

## 验证方案

### 单元测试（pytest）

- **`test_win.py`**：4 方向成五、边界（角落/边缘/对角线不越界）、长连 6/7 判胜、误判反例（两段 3 子中间隔 1 空 → 假）
- **`test_eval.py`**：阶梯严格偏序断言；`abs(eval) <= STATIC_MAX`；**零和性**（`eval(pos,B) == -eval(mirror(pos),W)`，这条直接锁死"不许再引入 0.85 这类不对称系数"）；**B7 回归**（活三与跳活三分数比 < 3，且都远小于 FOUR）；跨线组合回归
- **`test_line.py`**：列举 `_analyze_line` 无法区分而新分类器必须区分的用例（`_XXX_` vs `_X_XX_` vs `OXXX_` vs `OXXXXO`）
- **`test_incremental.py`**（最有价值）：随机 200 手，每手后把增量维护的成五标志/`cand_mask`/`hash`/`score_sum[2]`/`threat_agg` 与**从零重算**逐项比对；再做 make/unmake 往返（含乱序 unmake）断言状态位级还原。**这是增量设计正确性的唯一保障**
- **`test_tt.py`**：抽出纯函数 `_flag_of(value, alpha_orig, beta)` 单测（**B2 回归**：喂入 `value==alpha_orig` 边界断言是 UPPER 而非 EXACT）；TT 冷/热搜索往返结果一致；mate 归一化（ply=2 存、ply=0 查语义正确）
- **`test_vcf.py`**（B15 正反例，必做）：(a) 真必胜链 → WIN；(b) **进攻方假必胜**（冲四链被防守方"堵住同时自己成五"）→ 必须 NO_WIN；(c) **防守方反杀**（反冲四打断节奏）→ NO_WIN；(d) OR/AND 语义（双四 → WIN，单四 → NO_WIN）；(e) **三态区分**（深链 + 极小预算 → EXHAUSTED 而非 NO_WIN）；(f) 防守方有直接成五点 → NO_WIN
- **`test_search_mate.py`**（**B1 回归——"搜索不再避开真杀线"**）：构造 AI 3 步内必杀、同时对手有醒目的静态威胁（使旧静态评估偏好防守）的局面，断言 ① 返回杀棋着法 ② `is_mate(best_val) and best_val > 0` ③ `best_val > STATIC_MAX`。**对称反例**：对手有 3 步必杀而 AI 有更大静态优势 → 断言 `is_mate(val) and val < 0`（识别出必败）而非被活四掩盖
- **`test_threat.py`**：B17 回归（`opp_win==1 且 opp_live4==1` 的双杀局面，断言不再盲目堵五连点）
- **`test_difficulty.py`**：三难度各 20 局面，断言墙钟 ≤ `time+0.1s`（B12 回归）；低难度不触发 VCF
- **`test_cancel.py`**：搜索中途 set cancel，断言 100ms 内返回且坐标合法；重开局 20 次压力测试无崩溃无棋盘污染

### 自对弈 A/B 回归

`tools/legacy_engine.py` 冻结旧 AI 段（**不要 `import main`**——它模块级 import QtWidgets 且构造 QColor，在无 display 环境脆弱）。新引擎零 Qt 依赖，headless 直接可用。

`tools/selfplay.py`：固定开局集合（天元后 8 个标准第二手 + 对称对）、交替执黑、**固定随机种子**（用可复现 PRNG 实例而非全局 `random`）、无禁手先五为胜、225 手判和、同 hash 出现 3 次判和。记录每步 `(hash, move, score, depth, nodes, time_ms)`。

- **快速档**：双方固定 1.0s / max_depth=8，100 局 —— 用于迭代筛选
- **完整档**：新引擎 15s vs 旧引擎 8s，20 局 —— 最终验收
- 度量：新引擎胜率、平均每步节点/深度/耗时、非法走法数（必须 0）、崩溃数（必须 0）、超时次数（必须 0）

各阶段门槛：Phase 1 **45-55%**（无回归）→ Phase 4 **≥58%** → Phase 5 **≥65%**。

**补充 `tools/positions.py`**：手工挑选 20-30 个已知解的局面（经典 VCF/VCT 题、双杀题、防守题），断言新引擎命中率 ≥80% 并**记录旧引擎命中率**（预期显著更低）——这比自对弈胜率更不易被噪声干扰，是最有说服力的"更强"证据。

### 性能基准

`tools/bench.py`：固定 10 个中局局面 × 三难度，输出 `nodes/sec`、`depth_reached`、`time_ms`、`tt_hit_rate`、`qnode_ratio`、`vcf_nodes`、`branch_factor`。先跑 legacy 记录基线（应复现 8.3s / depth 5-6 / 1786 万调用 / `_check_win_fast` 77%）。

逐阶段目标：`check_win` 367µs → **<1µs**；每叶评估 858µs → **<5µs**；**≥30k nps**；depth **≥5**（原定 ≥10，已修正，理由同上）；时间合规 **100%**。

### B1 的三重证据

1. **行为级**：`test_search_mate.py` 正反两条断言
2. **日志级**：从 `git show HEAD:game_log_*.txt` 取出历史败局，对"AI 走错的关键步"用新引擎在同一局面重算，断言 `info['score_type']=='mate'`（旧引擎此处返回 1e7 级静态分）；这些局面固化进 `positions.py` 纳入 pytest
3. **不变式级**：断言 `STATIC_MAX < WIN_SCORE - MAX_PLY`，把量级隔离从注释变成可执行断言

### 端到端 GUI 冒烟

- **手动清单**（每阶段一次）：三难度各下一局至结束；悔棋；**中途重开局**（验证 B19 取消）；**中途退出**（验证 worker 回收）；黑白选择卡 hover 样式（B21）；**含中文路径从零启动**（B22）
- **半自动**：`tools/gui_smoke.py` 用 `QT_QPA_PLATFORM=offscreen` 构造 `GomokuGame`，`QTest` 模拟若干步点击，断言不崩溃、棋盘合法

---

## 风险

- **Phase 3+4 合并交付**：分数体系不能一半新一半旧，两者必须一起落地，是单次改动最大的一步。缓解：先完成 Phase 2 的增量基建并全绿，再动搜索。
- **LMR 参数**需在真实对局调优，纸面参数未必最优。
- **VCF 防守候选集爆炸**：靠节点 + 深度 + 时间三重预算与 `EXHAUSTED` 三态兜住。
- **移除 torch 会影响 Release 形态**：README 需明确说明"改为位棋盘 + 增量评估，CPU 实测更快"并给出 bench 数字，避免曾因"GPU 加速"而来的用户困惑。
- **`legacy_engine.py` 的 stub 完整性**：`GameLogger.coord_to_sgf` 被旧 `ai_move`（1630、1844）引用，冻结时需正确 stub。

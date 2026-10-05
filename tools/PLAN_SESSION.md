# 对局会话化架构方案（Session / Player / Room）

> 状态：设计已确认，按里程碑实施。**M0 只落文档与行为契约测试，不改生产代码。**
> 相关文档：`README.md`「The C++ compute core」「Engine design」、
> `tools/BASELINE.md`（实测基线）、`tools/PLAN_ENGINE.md`（引擎重写方案）。

## 1. 需求与决策

把"玩家"从 UI 里抽象出来：玩家可以是真人（本地 / 远程），也可以是 AI 推理。
整体参照 Minecraft：

| Minecraft | 本项目 |
|---|---|
| 客户端（渲染 + 输入） | PyQt5 界面 |
| 单人模式的内置服务端 | 应用内嵌 `Room`（权威对局状态） |
| 生物 / 实体由服务端驱动 | AI 玩家（`Player` 的一种实现） |
| 对局域网开放 | 房主启动局域网监听，其他玩家客户端加入 |
| 其他玩家客户端 | 同一个可执行文件连接 `ip:port` 进房 |
| 专用服务器 | `--server` 无界面多房间模式 |

**已确认的五个决策**（用户拍板，2026-10-05）：

1. **AI 双形态都要**：进程内座位（`AIPlayer`）与网络客户端（`BotClient`）。
2. **服务端两种形态都要**：应用内置 + 专用 `--server` 无界面模式。
3. **联机默认全功能**（悔棋 / 复盘 / 图表），通过 `RoomConfig` 允许禁用。
4. **房间模型**：专用服务器可多房间；客户端一次只在一个房间。
5. **节奏**：增量迁移，单机行为与现有测试 / `gui_smoke` / `ui_e2e` / 打包全程不降级；
   第一步只做可扩展性重构，不实现联机功能。

## 2. 目标架构

```
     专用服务器（--server，可多房间）                          同一可执行文件（GUI）
┌──────────────────────────────────────┐        ┌─────────────────────────────────────┐
│ Server（监听 + 房间注册表）             │        │ GomokuGame（UI，只做渲染/输入）       │
│  ├─ Room A ── Session ── 席位/事件     │◄──TCP──┤  └─ RoomClient                      │
│  ├─ Room B ── Session ── 席位/事件     │        │       ├─ 内置传输：同进程直连 Room    │
│  └─ ...                              │        │       └─ TCP 传输：连本机/局域网/专服 │
└──────────────────────────────────────┘        └─────────────────────────────────────┘
                        Session = 权威状态机（棋盘/回合/判胜/悔棋/席位，零 Qt、零 socket）
                        Player  = HumanLocal | AIPlayer | RemoteHuman | BotClient
```

四条核心原则：

1. **Session 是唯一的规则真源。** 现在规则散在 `main.py`（`_on_board_click`
   自己判合法/判胜，`_on_undo` 自己改状态）。重构后单机、LAN、专用服务器全部走
   同一个 Session，不再有第二套判定。
2. **UI 永远通过 `RoomClient` 说话。** 单机模式用同进程 transport，联机用 TCP。
   好处是单机与联机走同一条协议路径（MC 的单人=内置服务端）；代价是 M4 的 UI
   改造量 —— 用一次性成本换长期可维护性。
3. **AI 双形态共享同一个选点实现。** `AIPlayer`（进程内座位）与 `BotClient`
   （网络客户端）都调用现有 `engine.ai_move` 门面；C++ 引擎、静默降级、协作式
   取消**一行不改**。
4. **全功能默认开、config 可禁。** `RoomConfig` 随加入应答下发，客户端据此渲染
   （按钮禁用、图表隐藏），默认值精确等于今天的单机行为。

## 3. 模块划分（草案，M1 起逐步落地）

| 模块 | 职责 | 不做什么 |
|---|---|---|
| `session.py` | 权威状态机：棋盘、走子序列、回合、结果、悔棋政策、事件；零 Qt / 零 socket | 不碰 UI、不管线程、不发网络包 |
| `players.py` | `Player` 协议 + `HumanLocal` / `AIPlayer`（包 `engine.ai_move`）| 不持有棋盘（只看快照）|
| `driver.py` | 本地对局调度：轮到谁 → 问谁 → 提交；`AIWorker`、代数作废迁到这里 | 不实现规则 |
| `wire.py` | JSON 行编解码、`hello` 双向版本校验、错误码 | 不含业务规则 |
| `room.py` | 单局房间：Session + 席位 + 成员消息路由 | 不监听端口 |
| `server.py` | 监听、房间注册表、内置 / `--server` 两种启动形态 | 不含规则 |
| `client.py` | `RoomClient` + `LocalTransport` / `TcpTransport` | 不做规则判定 |
| `bot.py` | 无头 Bot 客户端（AI 以网络玩家身份接入）| 不渲染 |

拆分时机：`session.py` / `players.py` 在 M1–M2 落地；网络四件在 M3 落地；
`main.py` 在 M4 才改成 RoomClient 客户端。

## 4. 行为契约清单（M1 必须逐条保持）

以下行为是现有用户可见语义，M1 抽取 Session 时必须逐条保留。括号内为现状位置，
"M0 测试"列指出 `tests/test_game_flow.py` 中对应的钉子。

| # | 契约 | 现状位置 | M0 测试 |
|---|---|---|---|
| C1 | 合法落子：终局或 AI 思考中点击被丢弃；已占格子不生效 | `_on_board_click` 开头 | `test_player_win_...` 末尾 |
| C2 | 落子方：人机由 `gamemode` 决定；本地对战由 `move_count` 奇偶决定（黑先） | `_on_board_click` | `test_pvp_alternates_...` |
| C3 | `human_moves` 仅人机模式记录；`before` 是**落子前**快照；`seq = move_count+1` | `_on_board_click` | `test_human_moves_only_...` |
| C4 | 落子后 `move_count` / `move_history` / `last_move` / `board_widget` 同步 | `_on_board_click` / `_on_ai_finished` | 各测试均有断言 |
| C5 | 判胜：玩家五连 → `gamerule=2, winner=玩家子色`；AI 五连 → `gamerule=1, winner=AI 子色`；均触发 `win_line` 高亮与终局 | 两处 `check_win` 分支 | `test_player_win_and_ai_win_...` |
| C6 | 判和：`move_count >= 361` → `gamerule=0, winner=0`（棋盘放满，无五连） | 两处平局分支 | `tests/test_session.py::test_draw_at_full_board`（M4c 起：UI 不再伪造残局）|
| C7 | AI 调度：`_ai_turn` 置思考态并禁用悔棋，经 `AIWorker` 异步返回 | `_ai_turn` | （`gui_smoke` 覆盖；M4c 起由房间 `turn` 事件驱动）|
| C8 | 代数作废：`_on_ai_finished` 丢弃 `generation != _ai_generation` 的迟到结果 | `_on_ai_finished` | （M4c 起不再适用：调度在房间，无陈旧 worker 结果）|
| C9 | 悔棋：人机一次撤两步；本地对战一次撤一步；上限 3 次（`output`）；`human_moves` 按 `seq <= move_count` 修剪 | `_on_undo` / `_trim_human_moves` | `test_undo_*` 三则（M4c 起经房间政策，语义不变）|
| C10 | AI 先手：对空盘走 `engine.ai_move` 的本地开局路由（天元），被悔棋撤掉后由房间重下 | M4c 起：`room.py` 的 AI 席位 | `test_ai_first_move_*` 两则 |
| C11 | **M4c 修正**：悔棋撤到请求方上一次落子之前；天元被撤后房间重新判断轮次，**AI 重下天元**（不再是"撤两手后白先走在空盘上"）| `room.py` 悔棋政策 + `_recheck` | `test_ai_first_move_pair_undo_replays_tengen` |
| C12 | 复盘入口只在"输给 AI"时出现；`human_moves` 是唯一输入 | `_show_game_over` | （`gui_smoke` 覆盖）|
| C13 | 重开：取消 AI/复盘、关日志、回到**模式选择页** | `_on_restart` | `test_restart_returns_...` |
| C14 | 图表：每次落子记一个点；AI 结果带 depth 用搜索分，否则用静态估值；悔棋按序列长度截断 | `_record_score` / `_rewind` | （本阶段不钉，M2 事件化）|
| C15 | 终局节奏：面板**立刻**停表，遮罩延迟 1s；平局不走延迟 | `_finish_win_or_lose` | （`ui_e2e` 覆盖）|
| C16 | 日志：人/AI 各自格式、每约 5 手与开局 3 手记录棋盘状态、格式是题库来源 | `gamelog` 调用点 | （`test_gamelog_flavor` 覆盖文件策略）|

C11 曾按**现状**逐字保留（M1），**M4c 决定修正**：单机改走房间后，悔棋政策
归 `room.py`（撤到请求方上一次落子之前），被撤掉的 AI 先手由房间主循环重新
计算 —— 撤掉天元就重下天元，而不是把白棋推上先手位。

M4c 对 UI 级覆盖的调整（理由详见 `tests/test_game_flow.py` 模块 docstring）：

* C6 判和不再有 UI 用例 —— 棋盘由房间权威维护，UI 不能也不该伪造残局，
  判和规则由 Session 单测覆盖；
* C8 代数作废用例删除 —— 事件按连接顺序投递、开新局前旧客户端整体关闭，
  "迟到的 worker 结果"这条路径不存在了；
* C11 用例改为断言"AI 重下天元"；
* C3 的 `human_moves` 改由客户端记录（人类回合的 `turn` 记快照，`move` 事件
  落账），`before`/`seq` 语义与旧版一致。

## 5. 协议草案 v1（M3 细化）

沿用 `engine.py` 已验证过的风格：**JSON + `\n` 行协议**，`hello` 双向版本校验
（"连得上" ≠ "是我们的服务端"），服务端权威校验一切意图。

```
客户端 → 服务端
  hello          {proto: 1, name, caps: [...]}
  list_rooms     {}
  join           {room, password?}
  seat           {stone: "black"|"white"} | ready
  move           {r, c}
  undo_request   {}
  undo_response  {accept: bool}
  restart_request / restart_response
  resign         {}
  chat           {text}
  ping           {}

服务端 → 客户端
  hello_ok       {proto: 1, server_name, server_version}
  room_list      {rooms: [{id, name, players, state}]}
  welcome        {room, seat, room_config, state, players}
  state          {board, moves, turn, move_no, result}   # 加入 / 重连全量同步
  move           {r, c, stone, move_no, info?}           # info: AI 的 depth/best_val
  turn           {stone, deadline?}
  undo_proposed  {by}
  undo_applied   {move_no}
  game_over      {winner: "black"|"white"|"draw", reason: "five"|"resign"|"draw", line}
  players        {seats: [...]}
  error          {code, message}
```

要点：

- `RoomConfig` 默认值 = 今天的单机行为：`undo_limit=3`、`allow_undo=true`、
  `allow_review=true`、`allow_restart=true`、`show_ai_scores=true`；
  禁用一个功能时客户端按 `welcome` 里的配置禁用对应入口。
- 协议版本不匹配 → 明确 `error`，绝不"尽量兼容"（复制 `engine.py` 的教训）。
- 坐标用 `(r, c)`；日志仍用现有 SGF 风格（跳过 I），两者在 `gamelog` 里已有换算。
- 断线 / 观战 / 房间码等 M5 议题，协议预留字段但不实现。

## 6. 并发陷阱（M3 必须处理，现有设施无法直接多房间并发）

1. **`engine._ServerClient` 不可并发**：`_read_buf` 与请求-应答是实例级共享状态，
   两个房间同时搜索会串包。现状只有一个 `AIWorker`，所以没暴露。
2. **`engine_local` 是进程级单例**：TT / history / killer 由 `_ENGINE` 持有，
   `new_game()` 也是全局入口；两个房间交替搜索会互相污染。
3. **进程级单例还有** `engine._note` / `engine_label`（面板标签）与
   `GameLogger`（单一日志落点），多房间会串。

**对策**：M3 在进程内做**单一 AI 执行器**（一个工作线程 + 队列，所有房间的搜索
串行化），`new_game()` 与搜索在同一个执行器内成对调用；并行基准仍旧靠多进程
（与今天 `tools/ab_enhance.py` 的做法一致）。专用服务器若要 AI 房间真并行，
按"一房间一进程"部署，而不是在进程内硬并发。

## 7. 里程碑与退出标准

| 阶段 | 内容 | 退出标准 |
|---|---|---|
| **M0** | 本设计文档 + 行为契约测试；不动生产代码 | 新增测试全绿；全量套件不回归 |
| **M1** | 抽取 `Session`（状态/规则/悔棋），`main.py` 委托，兼容垫片（只读 property） | 契约测试、全量、`gui_smoke`、`ui_e2e` 全绿；行为零变化 |
| **M2** | `Player` 抽象 + 本地驱动；人机 / 本地双人 / AI-AI 统一走 Player；无头 AI-AI 批量基准（同 `selfplay` 报告格式）| 三种模式行为与契约一致；基准可复现 |
| **M3** | `wire` / `room` / `server` / `bot`；两个 Bot 过 loopback 对局；专用服务器多房间骨架 | socket 集成测试进 pytest；错版本明确拒绝；端口独立可配 |
| **M4** | UI 改造成 `RoomClient` 客户端；内置服务端；"对局域网开放"/"加入房间"；全功能（悔棋协商、本地复盘、事件化图表）| ✅ `ui_e2e` 单机五档全过；loopback 双客户端 e2e（socket 级 + UI 级）；完整套件 412 passed（见第 13 节）|
| **M5** | 断线重连、观战、房间码、局域网发现、`--server` 打包与文档 | 按需排序，每项独立 |

## 8. 待验证的开放点

1. **M4 的单机路径**：内置 transport 是否真的要过完整协议（dogfooding），还是
   直接调用 Session（少一层但会分叉）。倾向协议，M4 开工前用一局单机原型验证
   线程/取消模型没有新增卡顿。
2. **悔棋同意策略**：本地/同进程沿用现状；远程对手默认是否需要同意
   （`undo_needs_consent` 默认值）在 M4 定。
3. **复盘在联机下的引擎**：客户端本地跑 `engine.analyze`（依赖本机引擎可用），
   还是服务端代算。倾向本地，与"复盘是客户端功能"一致。
4. **观战与房间码**：协议已预留，是否进 M5 首版由使用需求决定。
5. **AI 席位配置的粒度**：难度之外是否暴露增强开关（`enhanced/lmr/extend`）
   给 Bot 客户端做参数对比；倾向通过 `PlayerSpec.options` 透传。

## 9. M2 完成记录（2026-10-05）

已落地（**加法式**，不改既有 UI 行为）：

* `players.py`：`PlayerSpec`（可序列化的席位描述）、`AIPlayer`（包装
  `engine.ai_move`，书/降级/取消全部复用）、`ScriptedPlayer`（测试/复现）。
* `session.py`：新增通用入口 `apply_stone(r, c, stone)`；人机 / AI / 开局的
  既有政策入口全部改成它的薄封装 —— 规则仍然只有一份。
* `match.py`：无头 `Match`（黑先交替、开局前缀、协作取消、非法/异常作废、
  步数上限按平局记），零 Qt，M3 的 Room 会复用它。
* `tools/arena.py`：档位 A-A 基准。每两局交换先后手、开局前缀从
  `selfplay.OPENING_SECOND_MOVES` 轮换（引擎确定性，不换开局 N 局会走出
  同一盘棋）；报告 `tools/reports/arena_*.json`，字段与 `selfplay` 对齐，
  每局覆写、可中断观察。
* `main.py` 的 `AIWorker` 改走 Player 抽象（构造从
  `(board, stone, level)` 变为 `(board, player, stone)`），UI 的 AI 回合与
  无头对局从此共用同一个玩家接口。

验证：CI 口径套件全绿；`tools/arena.py --games 2` 产出报告（19 手终局、
0 非法 0 异常）；`tools/ui_e2e.py --levels 1` 真实点击对局通过。

尚未做（按计划留给 M3/M4）：人类席位在网络/房间里的传输、观战、
`RoomConfig`、Session 席位从 `mode/human_stone` 升级为 `PlayerSpec`。

## 10. M3 完成记录（2026-10-05）

已落地（**专用形态先行**；内置形态留给 M4）：

* `wire.py`：JSON 行线格式 + `WireError`（bad_json / timeout / closed…），
  协议版本 `PROTO_VERSION = 1`，双向 `hello` 校验 —— 与 `engine.py` 同一套
  教训（"连得上"不等于"是同类程序"）。
* `room.py`：单房间。席位 `SeatSpec("ai" | "remote")`：`ai` 由服务端算
  （AIPlayer），`remote` 由接入连接驱动；非法着法回错重问、不终止对局；
  局中掉线终止本局（重连/续弈留 M5）。
* `server.py`：`RoomServer`（监听 / 房间注册表 / 连接生命周期
  hello→join→welcome→move·ping）。默认端口 **48900**，与引擎端口池
  （49001–49093）错开。CLI 支持一房间与 `--port 0`。
* `tools/bot_client.py`：网络 Bot 客户端 —— 本机算棋、只发送着法，两个
  不同档位（甚至不同机器）的 AI 可以坐进同一桌。
* `players.reset_engine()` + `_AI_MUTEX`：第 6 节的约定落地——**一个进程内
  AI 搜索串行化**；arena 改用同一入口。

验证：新增 wire 5 条 + room 7 条 socket 级测试；完整套件全绿；真实冒烟
（服务端 + 两个独立 Bot 进程，黑 2 档 / 白 1 档）过 loopback 下满 25 手，
三方结局面一致（winner=1 / five / 25 手），退出码 0。

尚未做（M4）：内置形态（应用内开房间）、UI 作为 RoomClient 客户端、
加入/开房界面、悔棋/复盘/图表的 `RoomConfig`。

## 11. M4b 完成记录（2026-10-05）

协议与房间补齐"单机改走房间"所必需的能力：

* `session.rewind(n)`：通用回退（棋盘/历史/复盘记录），**不涉及政策与
  预算**；`undo()` 的 UI 政策与 `room.py` 的房间政策都经过它。
* 房间悔棋：`submit_undo_request`。对手是服务端 AI 时**立即同意**，远程
  对手明确回 `undo_needs_consent`（协商留 M4d）。政策 = 撤到请求方上一次
  落子之前（自己刚落子撤 1；对手刚回应撤 2；自己一手未下撤 1、让对手
  重下）。`_recheck` 哨兵让主循环在悔棋后重新判断轮次——AI 先手被撤会
  重下天元。
* `move` 事件携带 `info`（AI 席位有 depth/best_val，远程席位为 null）。
* 房间主循环改为"**锁外等待、锁内落子**"：AI 在棋盘快照上搜索，悔棋与
  落子通过 `_game_lock` 串行，互不读取半路状态。
* `RoomClient.undo_request()`；`undo_response` 明确 `not_supported`。

验证：session / room / client / wire / match 共 62 条相关测试全绿；两个
真实 Bot 进程过 loopback 复验通过（主循环重构无回归）。

## 12. M4c 完成记录（2026-10-05）

把 `main.py` 的单机对局从"UI 直接持有 Session、自己跑 `AIWorker`"改成
**应用内房间（`LocalRoom`）+ `RoomClient` 事件驱动**。单机与联机从此走
同一条协议路径（MC 的单人=内置服务端）。

* `main.py`：
  * `_start_game` 先 `_close_room()` 再建房间：人机一个人类席位（remote）
    + 房间 AI 席位；本地双人两条 `RoomClient`（black/white）从同一进程
    接入。
  * 新增 `_RoomBridge`（`pyqtSignal(object)`）把读取线程的事件排队投递回
    Qt 主线程；`_on_room_event` 分派 `welcome/state/move/turn/game_over/
    undo_applied/error`，UI 只做镜像（board / move_count / winner /
    gamerule / `move_history` / `human_moves`）与渲染。
  * 点击只提交意图（`_move_in_flight` 防连点）；悔棋把人机交给人类客户端、
    本地双人交给最后一手所属客户端。删除 `_ai_first_move` / `_ai_turn` /
    `_on_ai_finished` / `_ai_generation`；`AIWorker` 类保留（
    `tests/test_ai_worker.py` 在用），主流程不再引用。
  * `_stop_animations`：删除页面 / 关窗前停掉在途动画（fade_in 的
    target 与动画同为子对象，快速重开时曾在 Windows 上触发原生崩溃
    0xC0000005，这次一并修掉）。
* `room.py`：
  * AI 席位把房间的 `_stop` 作为**协作取消**传给 `AIPlayer.choose_move` ——
    思考中重开会中断搜索，不再占着进程级 AI 互斥锁拖慢同进程下一局。
  * `_undo_plan` 不再读 `session.last_move`（`rewind` 会清空它，用于 UI
    清最后一手环），改由 move_count 奇偶推最后一手颜色 —— 修复"悔棋后
    立刻再悔一次"被误判成无棋可悔。
* `client.py`：`LocalRoom` 的房间 `auto_consent=True` —— 同进程对手的
  悔棋沿用单机"直接生效"的旧行为（本地双人 C9）；专用服务器默认仍回
  `undo_needs_consent`（协商留 M4d）。
* 测试：`test_game_flow.py` 改为房间驱动（脚本 AI 替身 + 轮询等待）；
  C6/C8 的 UI 级用例按上文说明移除，C11 改期望，`test_anim_smoke.py`
  改为等落子/悔棋事件，`gui_smoke.py` 只做最小等待改动。

验证：`pytest -q -m "not perf"` 全绿（399 passed / 1 skipped）；
`tools/ui_e2e.py --levels 1` 全部通过；`tools/gui_smoke.py` 141 通过 /
0 警告 / 0 失败。

## 13. M4d 完成记录（2026-10-05）——M4 收官

* `room.py`：`RoomConfig`（`allow_undo` / `undo_limit` / `allow_review` /
  `allow_restart` / `show_ai_scores`，**默认全开 = 原单机行为**），随
  `welcome` 下发；服务端权威校验悔棋开关与额度，UI 只做入口级禁用。
* 远程悔棋协商：`undo_proposed` / `undo_response`；**pending 期间冻结对局**
  （`_await_move` 两分支都不落子，远程着法攥住不丢），同意则按申请时存下的
  方案执行，拒绝只广播结果。
* `client.py`：`local_ip()`（UDP connect 探测，不真发包）；`LocalRoom`
  支持 `host="0.0.0.0"` 开房、透传 `config`、`auto_consent` 显式化；
  `connect` 失败立即关 socket。
* `main.py`：模式页第三张卡「局域网联机」→ 创建/加入；开房复用颜色页，
  等待遮罩展示 `本机IP:端口`（对手入座的首个事件收回它）；加入页失败
  **原地**显示中文原因（`seat_taken` / `no_room` / `proto_mismatch`…）可
  重试；单机与 LAN 共用 `_attach_room` 与 `_on_room_event`，没有第二套
  对局逻辑；UI 按配置禁用悔棋/重开/复盘入口与评分曲线。

验证（全部在冻结代码上执行）：

| 闸门 | 结果 |
|---|---|
| `pytest -q -m "not perf"` | **412 passed / 1 skipped / 0 failed** |
| `tools/ui_e2e.py`（五档完整对局） | 全部通过（深度 2/4/6/9/9，追点与开局库检查齐全） |
| `tools/gui_smoke.py` | 141 通过 / 0 警告 / 0 失败 |

已知边界（留给 M5）：两台真机 + 防火墙/多网卡未手工验证（同机双客户端的
LAN 路径已由 socket 级与 UI 级测试覆盖）；加入**专用服务器**（多房间/房间名）
未接 UI；开房界面暂无 `RoomConfig` 配置入口；观战/断线重连未做。

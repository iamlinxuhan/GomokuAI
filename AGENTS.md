# AGENTS.md

PyQt5 五子棋（19×19）。同一套算法有两份实现：`engine_local.py`（Python 参考实现，**算法真源**，零 Qt）与 `cpp/`（C++17，本地 TCP 服务端，性能路径）。`engine.py` 只是门面，不含算法。改引擎前先读 `README.md` 的「The C++ compute core」「Engine design」两节和 `tools/BASELINE.md`（实测基线、已推翻的假设）。

## 常用命令

依赖由 **uv** 管理：`pyproject.toml` 是清单，`uv.lock` 已入库且覆盖各平台 / Python 版本。先 `uv sync`（默认含 dev 组：pytest / pyinstaller）再执行下列命令。

```bash
uv sync                                # 运行 + 开发依赖
uv run python main.py                  # 从源码运行

uv run pytest -q                       # 全量；含真实搜索，较慢
uv run pytest -q -m "not perf"         # CI 口径：跳过机器速度相关门槛
uv run pytest tests/test_vcf.py -q
uv run pytest tests/test_vcf.py::test_name -q
```

- `perf` 标记（定义见 `pytest.ini`）：阈值是 nps / 实际到达深度，慢机器上「回归」与「机器慢」无法区分，CI 不跑。正确性、增量一致性、时间合规、题库、VCF、棋型用例**不带**该标记。
- 依赖只改 `pyproject.toml`，改完 `uv lock` 并提交 `uv.lock`；CI 用 `--frozen`，锁与清单不一致会当场失败。**不要删 `[tool.uv]` 里的两条 `PyQt5-Qt5` 平台约束**：Windows 只有 5.15.2 有 wheel，删了 `uv sync` 在 Windows 上会装不上（原因见 `pyproject.toml` 文件头）。
- 没有 uv 时运行依赖等价于 `pip install numpy PyQt5`；不要再手抄 requirements 文件。

## C++ 计算核心

```bash
cmake -S cpp -B cpp/build -DCMAKE_BUILD_TYPE=Release
cmake --build cpp/build -j
./cpp/build/gomoku_engine --selftest   # 退出码 0 = 通过；构建后会自动跑 --verify-tables
```

- **不要手动启动 `gomoku_engine`。** `engine.py` 会按需自动拉起；端口在 `config.PORT_POOL`（49001–49093）中逐个试，每个都必须通过 `hello` 校验（校验 `board_size/win_score/static_max` 刻度，不符就换下一个）。手工排查用 `GOMOKU_ENGINE=<可执行文件>` 覆盖路径，`GOMOKU_AI_LOGDIR=<目录>` 指定日志目录。
- 难度参数（time/max_depth/vcf_budget/qply/bias）由 Python 随请求下发，C++ 侧没有难度表。C++ 与 Python 的分值刻度必须一致：改 `engine_local.py` 的评分常量时要同步 `cpp/src`。
- **Windows 坑**：MSVC 多配置生成器把产物放在 `cpp/build/Release/`，而 `config.py` 只找 `cpp/build/gomoku_engine[.exe]`；不设 `GOMOKU_ENGINE` 时会**静默**降级到本地 Python 引擎（无报错，只表现为 AI 变慢变弱）。CI 用的就是 `cpp\build\Release\gomoku_engine.exe`。

## 不能改 / 生成物

- `tools/legacy_engine.py`：冻结的旧引擎（A/B 基线），**故意保留全部已知缺陷，禁止「顺手修复」**；`tests/test_positions.py` 断言它在已知局面上必错。
- `opening_book.py`：由 `tools/build_book.py` 生成，禁止手改。开局库只在 Python 侧命中（`engine.py` 的 `ai_move` 在发出 TCP 请求之前查表）；C++ 侧没有镜像，也不要添加。
- `cpp/src/zobrist_table.inc`：由 `cpp/tools/gen_zobrist.py` 生成。
- `theme.py` 是颜色字面量（`#rrggbb`）的唯一定义点；`main.py`、`ui_kit.py`、`board_render.py`、`anim.py`、`charts.py` 中出现 hex 会被 `tests/test_no_literal_colors.py` 拦下（注释与 docstring 里也不行）。
- `engine_local.py` / `cpp/src/*.h` 中标注「为什么不能改」的历史缺陷注释：读完再动手，不要按现代直觉修正。
- `installer/GomokuAI.iss` 是 UTF-8 **带 BOM**（中文向导文本需要），编辑后保持 BOM。

## 测试与测量

- `tests/conftest.py` 自动设置 offscreen Qt 并处理非 ASCII 路径下的插件定位，跑测试不需要显示器。
- 只有 `tests/test_remote_client.py` 需要真实 C++ 二进制：`config.find_binary()` 找不到时自动 skip；CI 的 test job 不建 C++。
- 改算法后按顺序验证：`tools/positions.py` 题库（必须全过）→ `tools/bench.py`（与 `tools/BASELINE.md` 的基线对照）→ `tools/selfplay.py`（对旧引擎 A/B 胜率）。UI 接线用 `tools/gui_smoke.py`，端到端真下一局用 `tools/ui_e2e.py`。
- 测量带时间预算的搜索时机器上不要跑别的负载（`tools/BASELINE.md` 里有一条被推翻的假设正来自这个错误）。
- `tools/ab_enhance.py` 靠**逐字字符串锚点**打补丁复刻 `engine.py`；改动 `engine.py` 的 import 段或 `compute()` 下发增强开关那段后，必须同步该脚本的 `_ANCHOR_*`（锚点匹配不上它会故意退出）。

## CI 与发布

- `.github/workflows/build.yml` 只在 tag `v*`、PR、手动 dispatch 时触发；`test` job（`uv sync --frozen` + `uv run --frozen pytest -m "not perf"`）是 `release` 的 `needs`。
- 发布固定 15 个资产（Linux 四架构 + Windows，各 1 安装包 + run_debug + run_play），release job 会逐个比对文件名与数量。
- Linux 四架构共用 `packaging/build_in_container.sh`，在目标架构的 Debian bookworm 容器里构建（PyInstaller 不能交叉编译），**不读 `uv.lock`**（i386 / armhf 没有 PyPI 的 PyQt5 wheel，用 apt 提供）；Windows 打包走 uv（`uv sync --frozen --python 3.11` + `uv run --frozen pyinstaller`）。
- debug/play 变体由构建期注入的 `_build_flavor.py` 决定，**不要按文件名倒推**；debug 写 `game_log_*.txt`，play 与安装包不写。

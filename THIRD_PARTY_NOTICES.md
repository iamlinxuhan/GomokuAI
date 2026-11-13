# 第三方数据与代码声明

本程序以 GPL-3.0-or-later 发布。以下内容的版权归原作者所有，按其各自的许可
条款使用；它们不改变本程序整体的许可。

## Gomocup 2026 官方开局集

- **内容**：Gomocup 2026 的 24 条官方开局（freestyle15 与 standard 各 12 条），
  每条的着法序列从官方公布的坐标转写。
- **来源**：<https://live.gomocup.org/openings>（2026-11-13 取得）；官方示意图
  在 <https://gomocup.org/static/tournaments/2026/openings/>，两者逐条一致。
- **在本仓库中的位置**：
  - `data/gomocup_2026.json` —— 中立数据源（唯一真源）；
  - `cpp/src/gomocup_book_data.h` —— 由 `tools/gen_gomocup_data.py` 生成；
  - `gomocup_book_data.py` —— 同一生成器的 Python 产物。
- **改动说明**：官方坐标是相对盘心的 `(x, y)`，这里换成 15×15 的绝对坐标
  （`col = x + 7`、`row = y + 7`），着法顺序与官方一致，未做增删。尺寸平移、
  D4 对称展开、冲突决胜都是运行时行为，不属于数据本身。
- **应手不是官方数据**：官方只公布开局着法；每条线终局的应手由本项目自己的
  引擎搜索得出（`tools/build_gomocup_json.py --responses`）。它衡量的是本引擎
  的判断，不是 Gomocup 或任何第三方引擎的结论。

> 早期草稿曾打算采用 Rust crate `figrid-board` 的 `src/book.rs` 作为数据来源。
> 2026-11-13 对照官方示意图与官方坐标后发现，那份转录与 2026 官方开局**一条
> 都对不上**（不是旋转/镜像/平移的差别），因此已整体弃用；本仓库不含该 crate
> 的任何内容。

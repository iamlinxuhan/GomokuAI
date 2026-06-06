# 🎮 五子棋 AI (Gomoku AI)

基于 **PyQt5** 的五子棋人机对弈程序，搭载 **PVS+LMR + 固定数组置换表 + 专业棋型权重 + 分层TSS威胁响应 + 多线防守+拼命模式 + GPU加速 + 时间控制 + 开局库** 的高性能 AI 引擎。支持多种难度级别，UI 精美流畅。

![Python](https://img.shields.io/badge/Python-3.11-blue)
![PyQt5](https://img.shields.io/badge/PyQt5-5.x-green)
![PyTorch](https://img.shields.io/badge/PyTorch-2.9-red)
![NumPy](https://img.shields.io/badge/NumPy-✓-orange)
![Version](https://img.shields.io/badge/version-1.6-brightgreen)

---

## ✨ 功能特性

### 🤖 AI 算法
| 技术 | 说明 |
|------|------|
| **PVS 搜索** | Principal Variation Search，零窗口快速剪枝 |
| **LMR** | Late Move Reduction，靠后走法降深度搜索（节点-40%） |
| **Zobrist 哈希** | 64位随机哈希，增量 O(1) 更新 |
| **置换表** | 固定数组百万条，深度优先 + 年龄淘汰，O(1) 查询 |
| **静态评估缓存** | eval_cache 按 Zobrist 缓存评估值，避免重复全盘扫描 |
| **专业棋型权重** | Gomocup 参考权重：连五 1e8 / 活四 1e6 / 冲四 1e5 / 活三 5000 |
| **组合加成** | 双活三 1e5 / 冲四+活三 8e4 / 双冲四 1.5e5 |
| **杀手/历史启发** | 截断走法优先 + 历史深度加权累积 |
| **分层 TSS 威胁** | Level-1硬拦截(五连/活四) + Level-2软建议(双活三→PVS) |
| **多线防守** | 多重威胁检测(≥2五连/活四) → 放弃被动防守，转为以攻对攻 |
| **拼命模式** | PVS判必败时切换攻防混合：扫描对手准杀位(最高+1亿分) + 自身强攻击 |
| **GPU 加速评估** | 方向线型核 + 跳活核批量 conv2d，GPU 并行 |
| **时间控制** | 5秒/步上限(高级)，迭代加深自适应，时间不足提前停止 |
| **开局库** | 前6步预设平衡开局，命中直接返回 |

### 🎨 UI 设计
- **加载界面**：渐变背景 + 动画进度条 + 动态提示语
- **选择面板**：先手/后手 + 三级难度（初级/中级/高级）
- **游戏棋盘**：19×19 木质风格，径向渐变棋子带高光
- **最后一手**：红色高亮标记，鼠标悬停预览落子
- **右侧面板**：回合/难度/状态/悔棋次数/步数实时统计
- **胜负弹窗**：半透明遮罩 + 再来一局/退出按钮
- **AI 异步计算**：QThread 线程化，UI 零卡顿

---

## 📦 快速开始

### 运行方式一：直接下载 Release
从 [Releases](https://github.com/iamlinxuhan/GomokuAI/releases) 下载 `五子棋AI.exe`，双击即可运行（无需安装 Python）。

### 运行方式二：从源码运行

```bash
# 1. 克隆仓库
git clone https://github.com/iamlinxuhan/GomokuAI.git
cd GomokuAI

# 2. 安装依赖
pip install numpy pyqt5 torch

# 3. 运行
python main.py
```

---

## 🎯 游戏规则

1. 标准五子棋规则，19×19 棋盘
2. 黑棋先手，任意一方在 横/纵/斜 方向 **先连成五子** 者获胜
3. 双方交替落子，不可重复落子

---

## 🎛️ 难度说明

| 难度 | 搜索深度 | 说明 |
|------|----------|------|
| **初级** | 1 | 一步计算，1秒时限 |
| **中级** | 2 | 两步展望，3秒时限 |
| **高级** | 3 | PVS+LMR 3层 + 置换表 + TSS攻防 + GPU + 5秒时间控制 |

---

## 📁 项目结构

```
GomokuAI/
├── main.py          # 主程序（UI + AI 引擎）
├── input.png        # 棋子贴图
├── 五子棋.ico       # 程序图标
├── .gitignore       # Git 忽略规则
└── README.md        # 项目说明
```

---

## 🛠️ 打包为 EXE

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --icon="五子棋.ico" --name "五子棋AI" main.py
```

输出文件位于 `dist/五子棋AI.exe`。

---

## 📝 更新日志

### v1.6 (2026-06-06)
- 🔧 **分层TSS威胁响应**：Level-1硬拦截(五连/活四) + Level-2软建议(双活三交给PVS搜索)
- 🔧 **多线防守**：检测对手≥2个同时威胁 → 放弃被动防守，转为以攻对攻
- 🔧 **拼命模式**：PVS判必败时切换攻防混合评分，扫描对手准杀位(堵五连+1亿/堵活四+8000万) + 自身强攻击
- 🔧 **build.yml**：Release job 仅在 tag 推送时触发，修复 `action-gh-release` 缺少 tag 报错

### v1.5 (初始版本)
- PVS+LMR 搜索 + 固定数组置换表 + 专业棋型权重
- 增强TSS攻防 + GPU加速 + 开局库 + 精美PyQt5 UI



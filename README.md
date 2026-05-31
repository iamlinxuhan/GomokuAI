# 🎮 五子棋 AI (Gomoku AI)

基于 **PyQt5** 的五子棋人机对弈程序，搭载 **Alpha-Beta 剪枝 + Zobrist 哈希 + 置换表** 的高性能 AI 引擎。支持多种难度级别，UI 精美流畅。

![Python](https://img.shields.io/badge/Python-3.11-blue)
![PyQt5](https://img.shields.io/badge/PyQt5-5.x-green)
![NumPy](https://img.shields.io/badge/NumPy-✓-orange)

---

## ✨ 功能特性

### 🤖 AI 算法
| 技术 | 说明 |
|------|------|
| **Alpha-Beta 剪枝** | 大幅减少搜索分支，提升搜索深度 |
| **Zobrist 哈希** | 增量哈希计算，避免重复局面评估 |
| **置换表 (TT)** | LRU 缓存约 100 万条局面记录，避免重复搜索 |
| **迭代加深** | 渐进式搜索深度，保证实时响应 |
| **启发式排序** | 攻防评分排序候选落子，优先高价值分支 |
| **活四必胜检测** | 精确检测活四/冲四/五连，优先进攻 |
| **威胁优先级** | 五连 > 堵五连 > 自己活四 > 堵对手活四 |
| **中心加权** | 越靠近棋盘中心权重越高 |

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
pip install numpy pyqt5

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
| **初级** | 1 | 仅计算一步，适合新手 |
| **中级** | 2 | 展望两步，有一定策略 |
| **高级** | 3 | 展望三步，采用完整 Alpha-Beta + 置换表 |

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

## 📄 License

MIT License — 仅供学习交流使用。

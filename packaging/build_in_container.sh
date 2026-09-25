#!/usr/bin/env bash
# 在**目标架构**的 Debian bookworm 容器里完成一次完整打包。
#
#     FAMILY=AMD BITS=x86_64 EXT=deb DEB_ARCH=amd64 APP_VERSION=3.0.1 \
#       bash packaging/build_in_container.sh
#
# 为什么四个架构全都走容器：**PyInstaller 不能交叉编译**。64 位 runner 上
# 建不出 i386 与 armv7 的二进制，只能靠容器换 userland（armv7 再叠一层
# QEMU）。四个架构共用这一份脚本，架构之间的差异就只剩下面几个环境变量 ——
# 上一版把 .deb 与 tar.gz 的配方各抄在 workflow 里，加一个架构要改三处。
#
# 环境变量（前两个就是产物名里的那两格，见下）：
#   FAMILY       架构家族：AMD | ARM。**这一格是族名，不是型号** —— AMD 那两格
#                卖的是 x86 家族，Intel 机器下的是同一份。
#   BITS         位数，两族各用本名：x86_64 | x86_32 | arm64 | arm32
#   EXT          安装包容器格式，也是它的后缀：deb | tar.gz
#   DEB_ARCH     EXT=deb 时的 Architecture 字段：amd64 / i386
#   APP_VERSION  版本号（CI 从 tag 传入；手动跑给个 0.0.0）
#
# 产物名（写在 /src 下）：
#
#   GomokuAI_Linux_<FAMILY>_<BITS>_run           裸可执行文件（onefile，无后缀）
#   GomokuAI_Linux_<FAMILY>_<BITS>_setup.<EXT>   安装包（由 dist/gomoku-ai 的 onedir 收成）
#
# 早先这套名字是 `GomokuAI_For_Linux_<SUFFIX>`，SUFFIX ∈ AMD / X86 / ARM / ARM32
# —— **一格既当族名又当位数，而位数恰恰是最容易装错的那个信息**：`_AMD` 与
# `_X86` 其实都是 x86 家族（一个 64 位一个 32 位），`_ARM` 与 `_ARM32` 同理，
# 光看名字分不出谁是谁，下错就是一句 `Exec format error`。而且 `_X86` 撞上
# 业界"x86 默认指 32 位"的惯例，`_AMD` 又让 Intel 用户以为与自己无关。现在
# 族名与位数拆成两格，64 位/32 位一眼可见。
set -euo pipefail

: "${FAMILY:?需要一个架构家族名，如 AMD}"
: "${BITS:?需要一个位数列，如 x86_64 / x86_32 / arm64 / arm32}"
: "${EXT:?需要 EXT=deb 或 EXT=tar.gz}"
: "${APP_VERSION:=0.0.0}"
DEB_ARCH="${DEB_ARCH:-amd64}"

if [ "$EXT" != "deb" ] && [ "$EXT" != "tar.gz" ]; then
    echo "EXT 只能是 deb 或 tar.gz，收到 '$EXT'" >&2
    exit 2
fi
if [ "$EXT" = "deb" ] && [ -z "${DEB_ARCH:-}" ]; then
    echo "EXT=deb 时必须给 DEB_ARCH" >&2
    exit 2
fi

BASE="GomokuAI_Linux_${FAMILY}_${BITS}"     # 名字的前两格，两份产物共用
RUN_NAME="${BASE}_run"                      # 免安装裸文件（无后缀）
PKG_NAME="${BASE}_setup"                    # 安装包基名，实际文件还要接 ".${EXT}"
cd /src

echo "=============================================================="
echo " 架构容器内打包：FAMILY=$FAMILY BITS=$BITS EXT=$EXT DEB_ARCH=$DEB_ARCH"
echo " 目标架构：$(dpkg --print-architecture)   Python：$(python3 -V)"
echo "=============================================================="

# ---------------------------------------------------------------------------
# ① 系统依赖
#
# PyQt5 走 apt 而**不是** pip：PyPI 上没有 i386 / armhf 的 PyQt5 wheel，
# pip 会去从源码编译整个 Qt —— 那是几十分钟到几小时，而 apt 那份是现成的。
# 代价是 venv 必须 --system-site-packages（见 ③）。
#
# zlib1g-dev + gcc + python3-dev 是给 PyInstaller 的：它在 i386 / armv7 上
# 可能没有预编译 wheel，只能从源码编 bootloader，缺这些就是硬失败。
# ---------------------------------------------------------------------------
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
    python3 python3-venv python3-dev python3-pyqt5 python3-numpy \
    cmake g++ gcc make binutils zlib1g-dev file \
    libgl1 libglib2.0-0 libxkbcommon-x11-0 libxkbcommon0 \
    libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-randr0 \
    libxcb-render-util0 libxcb-shape0 libxcb-xinerama0 libxcb-xkb1 \
    libegl1 libfontconfig1 libdbus-1-3

# ---------------------------------------------------------------------------
# ② C++ 计算核心
#
# **这一步曾经整个缺失**，后果是 Release 上每一个产物都是纯 Python 引擎版：
# 4/5 档全靠 engine_local 兜底，C++ 那 5 倍速度与多出来的层数一件都到不了
# 用户手里 —— 而 config._candidates() 的第一候选就是 `_MEIPASS/gomoku_engine`，
# 说明设计上本来就要它进包，只是没人把它放进去。
#
# POST_BUILD 会自动跑 `--verify-tables`（表枚举错了是唯一会静默出错的地方），
# 这里再显式跑一次 `--selftest`，让"引擎在目标架构上真的能跑"也成为构建门槛 ——
# 交叉出来的二进制"能编译"和"能跑"是两件事。
# ---------------------------------------------------------------------------
echo "---- 构建 C++ 计算核心 ----"
cmake -S cpp -B cpp/build -DCMAKE_BUILD_TYPE=Release
cmake --build cpp/build -j"$(nproc)"
./cpp/build/gomoku_engine --verify-tables --selftest
file cpp/build/gomoku_engine

# ---------------------------------------------------------------------------
# ③ Python 侧
# ---------------------------------------------------------------------------
echo "---- 准备 venv ----"
python3 -m venv --system-site-packages .venv-build
.venv-build/bin/pip install --upgrade pip
.venv-build/bin/pip install pyinstaller
.venv-build/bin/python -c "from PyQt5.QtWidgets import QApplication; import numpy; print('PyQt5 + numpy OK')"

# ---------------------------------------------------------------------------
# ④ 两份产物
#
# `--add-binary "cpp/build/gomoku_engine:."` 把引擎放到包的根（_MEIPASS 下），
# 正是 _candidates() 第一个去找的位置。两份都要带：onedir 是安装包的原料，
# onefile 是免安装裸文件，少了引擎那一份就是"AI 明显变弱"而没有别的症状。
#
# 两份用不同的 --name，否则共用 build/ 与 dist/ 下的同名中间目录，第二次
# 构建会捡第一次的缓存而漏东西。
# ---------------------------------------------------------------------------
echo "---- PyInstaller: onedir（安装包原料）----"
.venv-build/bin/pyinstaller --onedir --windowed --name "gomoku-ai" \
    --add-binary "cpp/build/gomoku_engine:." main.py

echo "---- PyInstaller: onefile（裸可执行文件）----"
.venv-build/bin/pyinstaller --onefile --windowed --name "${RUN_NAME}" \
    --add-binary "cpp/build/gomoku_engine:." main.py

ls -lh "dist/${RUN_NAME}"
file "dist/${RUN_NAME}"

# 断言引擎确实进包了。**这是最容易静默漏掉的一环**：漏了不会报错，只会让
# 所有档位的 AI 都退回 Python 参考实现。onedir 与 onefile 用的是同一个
# --add-binary，而 --add-binary 指向不存在的文件时 PyInstaller 会硬错，
# 所以验 onedir 这一份就足以证明这条路径是通的。
# `-print -quit`：找到第一个就停。**这不是提速，是消掉同一类 SIGPIPE 隐患**
# —— `grep -q` 一命中就退出并关管道，find 若还要继续写就会被 141 干掉，
# `pipefail` 于是把整条管道判成失败，`if !` 随之翻转为"引擎没进包"并 `exit 1`：
# 一个**假警报**把已经成功的构建判死。（实测这条 find 的输出只有一行、
# 写完即退，所以侥幸没炸过 —— 但"侥幸"不是能留在 CI 里的东西。）
if ! find dist/gomoku-ai -type f -name gomoku_engine -print -quit | grep -q .; then
    echo "!! C++ 引擎没有进包 —— 检查 --add-binary" >&2
    exit 1
fi
echo "引擎已进包：$(find dist/gomoku-ai -type f -name gomoku_engine)"

# ---------------------------------------------------------------------------
# ⑤ 收成安装包
# ---------------------------------------------------------------------------
APP_DIR_NAME="gomoku-ai"          # onedir 目录名，也是装好之后的入口名
OPT_DIR="/opt/gomoku-ai"
BIN_LINK="/usr/local/bin/gomoku-ai"

DESKTOP_ENTRY='[Desktop Entry]
Version=1.0
Name=五子棋AI
Name[zh_CN]=五子棋AI
Comment=Gomoku AI - Negamax/PVS + Transposition Table + Quiescence & VCF (CPU)
Exec=/usr/local/bin/gomoku-ai
Terminal=false
Type=Application
Categories=Game;BoardGame;'

# `Depends:` 里的字体链是必需的：界面全靠中文，而这些基础库一个字体都不带，
# 最小系统上装完全是方框。用 `|` 串出候选链，任意一个 CJK 字体已存在即满足，
# 不必为了一个棋盘拖 60MB 的 Noto CJK 下来。
DEB_DEPENDS="libc6 (>= 2.28), libgl1, libglib2.0-0, libxkbcommon0, fonts-noto-cjk | fonts-wqy-microhei | fonts-wqy-zenhei | fonts-arphic-uming"

DEB_DESC="五子棋AI - 位棋盘增量评估 + Negamax/PVS + 置换表 + 静止搜索/VCF 连续冲四 (纯 CPU)"

if [ "$EXT" = "deb" ]; then
    echo "---- 打包 .deb (${DEB_ARCH}) ----"
    PKG_DIR="deb_build"
    rm -rf "$PKG_DIR"
    mkdir -p "${PKG_DIR}${OPT_DIR}" "${PKG_DIR}/usr/local/bin" \
             "${PKG_DIR}/usr/share/applications" "${PKG_DIR}/DEBIAN"

    cp -r "dist/${APP_DIR_NAME}/." "${PKG_DIR}${OPT_DIR}/"
    chmod -R 755 "${PKG_DIR}${OPT_DIR}"

    printf '%s\n%s\n' '#!/bin/bash' "exec ${OPT_DIR}/${APP_DIR_NAME} \"\$@\"" \
        > "${PKG_DIR}/usr/local/bin/${APP_DIR_NAME}"
    chmod 755 "${PKG_DIR}/usr/local/bin/${APP_DIR_NAME}"

    printf '%s\n' "$DESKTOP_ENTRY" > "${PKG_DIR}/usr/share/applications/gomoku-ai.desktop"

    printf '%s\n%s\n%s\n%s\n%s\n%s\n%s\n%s\n' \
        "Package: gomoku-ai" \
        "Version: ${APP_VERSION}" \
        "Section: games" \
        "Priority: optional" \
        "Architecture: ${DEB_ARCH}" \
        "Maintainer: iamlinxuhan" \
        "Depends: ${DEB_DEPENDS}" \
        "Description: ${DEB_DESC}" \
        > "${PKG_DIR}/DEBIAN/control"

    # gzip 而不是 zstd：兼容性更好（旧 dpkg 也能解），代价只是包大一点。
    dpkg-deb -Zgzip --build "${PKG_DIR}" "${PKG_NAME}.${EXT}"
    ls -lh "${PKG_NAME}.${EXT}"
    # 只看前几行摘要，但**不能用 `head`**：见下面 tar.gz 那一处的说明。
    dpkg-deb --info "${PKG_NAME}.${EXT}" | sed -n '1,12p'

else
    echo "---- 打包 .tar.gz（tar + install.sh）----"
    # 目录名用 BASE（不带 _setup）：解包出来的那个目录是给用户 cd 进去的，
    # "_setup" 只在文件名里有意义。
    rm -rf "$BASE"
    mkdir -p "${BASE}/${APP_DIR_NAME}"
    cp -r "dist/${APP_DIR_NAME}/." "${BASE}/${APP_DIR_NAME}/"
    chmod -R 755 "${BASE}/${APP_DIR_NAME}"

    # 一个可执行位都没有的 tar 包是最常见的踩坑点：解出来 chmod +x 忘了做，
    # 安装脚本自己就 `Permission denied`。这里显式写死 755 再打。
    printf '%s\n' "$DESKTOP_ENTRY" > "${BASE}/gomoku-ai.desktop"

    cat > "${BASE}/install.sh" <<EOF
#!/bin/bash
# 五子棋AI ${FAMILY} ${BITS} 版安装脚本。不需要联网，不需要编译。
set -e

APP_DIR="${OPT_DIR}"
SRC_DIR="\$(cd "\$(dirname "\$0")" && pwd)"

echo "Installing GomokuAI (${FAMILY} ${BITS}) to \${APP_DIR} ..."
sudo mkdir -p "\${APP_DIR}"
sudo cp -r "\${SRC_DIR}/${APP_DIR_NAME}/." "\${APP_DIR}/"
sudo chmod -R 755 "\${APP_DIR}"
sudo ln -sf "\${APP_DIR}/${APP_DIR_NAME}" "${BIN_LINK}"

# 装桌面菜单项（可选，失败不影响使用）
if [ -d /usr/share/applications ]; then
    sudo cp "\${SRC_DIR}/gomoku-ai.desktop" /usr/share/applications/ || true
fi

# 清掉旧版 ARM 的目录名，避免同一个程序留下两份
sudo rm -rf /opt/gomoku-ai-arm

echo "Done! Run: gomoku-ai"
EOF
    chmod 755 "${BASE}/install.sh"
    tar -czf "${PKG_NAME}.${EXT}" "$BASE"
    ls -lh "${PKG_NAME}.${EXT}"
    # 列几行证明包不是空的。**这里绝不能写 `| head -5`** —— 本脚本开头是
    # `set -euo pipefail`，而 `head` 读够 5 行就退出、关掉管道；tar 那边还有
    # 几万行要写（onedir 包 5 万个条目），写进已关闭的管道会收到 SIGPIPE，
    # 于是 tar 以 141（128+13）退出，pipefail 把 141 当成整条管道的状态，
    # `set -e` 随即中止脚本 —— **包已经打好了，却在最后一行显示失败**。
    #
    # 这就是 ARM64 / ARM32 两个 job 红、amd64 / i386 两个绿的全部原因：红的
    # 那两条走的是这个 tar.gz 分支，绿的那两条走 .deb 分支。上面 .deb 分支的
    # `dpkg-deb --info | head -12` 只是**碰巧**躲过去了 —— 它的输出远小于
    # 64 KiB 的管道缓冲区，dpkg-deb 写完全部输出就退出了，head 还没来得及关
    # 管子。加长 control 描述或让文件数变多，同一颗雷就会在 deb 分支上炸。
    #
    # `sed -n '1,5p'` 读满 5 行也**继续读到 EOF**，生产者因此永远写不爆管道。
    tar -tzf "${PKG_NAME}.${EXT}" | sed -n '1,5p'
fi

echo "=============================================================="
echo " 完成："
ls -lh "dist/${RUN_NAME}" "${PKG_NAME}".* 2>/dev/null || true
echo "=============================================================="

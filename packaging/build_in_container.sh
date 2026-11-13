#!/usr/bin/env bash
# 在**目标架构**的 Debian bookworm 容器里完成一次完整打包。
#
#     STEM=amd_x86_64 EXT=deb DEB_ARCH=amd64 APP_VERSION=3.0.5 \
#       bash packaging/build_in_container.sh
#
# 为什么四个架构全都走容器：**PyInstaller 不能交叉编译**。64 位 runner 上
# 建不出 i386 与 armv7 的二进制，只能靠容器换 userland（armv7 再叠一层
# QEMU）。四个架构共用这一份脚本，架构之间的差异就只剩下面几个环境变量 ——
# 上一版把 .deb 与 tar.gz 的配方各抄在 workflow 里，加一个架构要改三处。
#
# 环境变量：
#   STEM         产物名里那一格，同时也是"给谁下"的唯一线索：
#                  amd_x86_64 | amd_x32 | aarch64 | armv7
#                **AMD 那一格是族名不是型号** —— Intel 机器下的也是同一份。
#                ARM 两边**合并成一个 token**：`arm_x86_64` / `arm_x32` 这种
#                组合自相矛盾（ARM 怎么会有 x86 的位数），所以直接叫 aarch64
#                与 armv7（后者是 32 位 ARM 的实际架构名，与 `uname -m` 一致）。
#   EXT          安装包容器格式，也是它的后缀：deb | tar.gz | appimage
#                appimage 只给 amd_x86_64 / aarch64 —— 那两个架构有官方
#                appimagetool，且是自动更新的唯一 Linux 载体（见 updater.py）。
#                32 位与 armv7 继续发 deb / tar.gz，只提示下载页。
#   DEB_ARCH     EXT=deb 时的 Architecture 字段：amd64 / i386 / arm64 / armhf
#                **与 STEM 是两回事**：文件名那格给人看（`amd_x32`），这一格
#                给 dpkg 看（`i386`），两者的词表不是一套。
#   APP_VERSION  版本号（CI 从 tag 传入；手动跑给个 0.0.0）
#
# 产物名（写在 /src 下）：
#
#   GomokuAI_Linux_<STEM>_setup.<EXT> 安装包（由 dist/gomoku-ai 的 onedir 收成）
#   GomokuAI_Linux_<STEM>.AppImage    EXT=appimage 时的单个可执行文件
#
# **v4.4.0 起不再发裸可执行文件**（旧的 `_run_debug` / `_run_play` 共 8 个）。
# Linux 的便携形态改由 AppImage 承担；需要日志时用 `--debug` 或
# `GOMOKUAI_LOG=1`，不必再为"要不要写日志"单独构建一份。
#
# 早先这套名字是 `GomokuAI_For_Linux_<SUFFIX>`，SUFFIX ∈ AMD / X86 / ARM / ARM32
# —— **一格既当族名又当位数，而位数恰恰是最容易装错的那个信息**：`_AMD` 与
# `_X86` 其实都是 x86 家族（一个 64 位一个 32 位），`_ARM` 与 `_ARM32` 同理，
# 光看名字分不出谁是谁，下错就是一句 `Exec format error`。而且 `_X86` 撞上
# 业界"x86 默认指 32 位"的惯例，`_AMD` 又让 Intel 用户以为与自己无关。
set -euo pipefail

: "${STEM:?需要一个架构格，如 amd_x86_64 / amd_x32 / aarch64 / armv7}"
: "${EXT:?需要 EXT=deb / tar.gz / appimage}"
: "${APP_VERSION:=0.0.0}"
DEB_ARCH="${DEB_ARCH:-amd64}"

if [ "$EXT" != "deb" ] && [ "$EXT" != "tar.gz" ] && [ "$EXT" != "appimage" ]; then
    echo "EXT 只能是 deb / tar.gz / appimage，收到 '$EXT'" >&2
    exit 2
fi
if [ "$EXT" = "deb" ] && [ -z "${DEB_ARCH:-}" ]; then
    echo "EXT=deb 时必须给 DEB_ARCH" >&2
    exit 2
fi
if [ "$EXT" = "appimage" ] && [ "$STEM" != "amd_x86_64" ] && [ "$STEM" != "aarch64" ]; then
    # 32 位与 armv7 没有官方 appimagetool 的对应构建，也不在自更新的支持范围
    # 内；硬发一个跑不起来的 AppImage 比不发更糟。
    echo "EXT=appimage 只支持 amd_x86_64 / aarch64，收到 '$STEM'" >&2
    exit 2
fi

BASE="GomokuAI_Linux_${STEM}"               # 产物名的公共前缀
PKG_NAME="${BASE}_setup"                    # 安装包基名，实际文件还要接 ".${EXT}"
cd /src

echo "=============================================================="
echo " 架构容器内打包：STEM=$STEM EXT=$EXT DEB_ARCH=$DEB_ARCH"
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
#
# python3-pil（不是 pip install pillow）同理：它只在构建期用来把 .ico 转成
# 各档 PNG（见 ③.5），而 pip 在 i386 / armhf 上没有可靠 wheel，会退回源码
# 编译并连带要一堆 -dev 头文件；apt 那一份四个架构都有现成的。venv 是
# --system-site-packages，所以 ③.5 里直接 import PIL 就得到。
# ---------------------------------------------------------------------------
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
    python3 python3-venv python3-dev python3-pyqt5 python3-numpy python3-pil \
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

# **八个二进制全都要验**（19/15/13/9 × free/ban）。只验 19 的话，9×9 的端口
# 池里少一份二进制不会有任何构建错误 —— 用户选 9×9 之后 AI 静默退回 Python
# 参考实现，面板还会如实写着"Python (本地)"，正是最难被发现的那类退化。
#
# POST_BUILD 已经对每个 target 跑了 `--verify-tables`（表枚举错了是唯一会静默
# 出错的地方）；这里对 19 再跑完整 `--selftest`（含跨尺寸无关的增量/成五
# 自检），其余尺寸只跑 `--verify-tables` —— 它们的差异全在编译期常量上，
# 自检用例本身与尺寸无关，多跑一遍只是把 CI 时间拉长。
ENGINE_BINS="gomoku_engine gomoku_engine_ban"
for _size in 15 13 9; do
    ENGINE_BINS="${ENGINE_BINS} gomoku_engine_${_size} gomoku_engine_${_size}_ban"
done

./cpp/build/gomoku_engine --verify-tables --selftest
./cpp/build/gomoku_engine_ban --verify-tables --selftest
for _bin in $ENGINE_BINS; do
    [ -x "cpp/build/${_bin}" ] || { echo "!! 缺引擎 ${_bin}" >&2; exit 1; }
    ./cpp/build/${_bin} --verify-tables >/dev/null
    file "cpp/build/${_bin}" | sed 's/,.*//'
done

# ---------------------------------------------------------------------------
# ③ Python 侧
#
# **这里不走 uv / uv.lock。** 开发环境的依赖清单（pyproject.toml + uv.lock）
# 做全平台解析，而 i386 / armhf 上 PyPI 根本没有 PyQt5 wheel（原因见 ① 段），
# `uv sync` 在这两个架构上必然失败。这里沿用 apt 的 python3-pyqt5 /
# python3-numpy，pip 只补 pyinstaller 一件；Python 版本也固定是镜像的 3.11。
# ---------------------------------------------------------------------------
echo "---- 准备 venv ----"
python3 -m venv --system-site-packages .venv-build
.venv-build/bin/pip install --upgrade pip
.venv-build/bin/pip install pyinstaller
.venv-build/bin/python -c "from PyQt5.QtWidgets import QApplication; import numpy; print('PyQt5 + numpy OK')"

# ---------------------------------------------------------------------------
# ③.5 图标（PNG，按 freedesktop 的 hicolor 目录布局）
#
# **"装完图标不对"的两个根因都在这份图源上**：在此之前安装包从头到尾没装过
# 任何图标文件，.desktop 里也没有 `Icon=` —— 菜单那条入口找不到图标就退回
# 通用/空白图标。而就算 `.desktop` 修好了，**已经开着的窗口仍然是 X.Org 的
# 兜底 logo**：窗口图标走的是窗口属性（X11 的 `_NET_WM_ICON`、Wayland 的
# app_id），只有程序自己 `setWindowIcon` 才写得出来，`.desktop` 的 `Icon=`
# 管不到。所以这里要产两份：一份给菜单（hicolor 七档），一份打进包里给窗口。
#
# 图源就是仓库根下那个 .ico（单帧 256×256 RGBA），Windows 侧已经在用它
# （build.yml 的 `--icon=`、installer/GomokuAI.iss 的 SetupIconFile）。
# **不新增第二份图源**：构建期由它生成各档 PNG，仓库里不留生成物 ——
# 与 FLAVOR_DIR 同一个理由，本地跑真工作树时也不该多出需要 gitignore 的文件。
#
# .ico 的文件名是中文，**用 find 取它而不是把名字写死**：容器里未必有
# UTF-8 locale，写死一个非 ASCII 字面量是把编码问题请进来。
#
# 七档都是 256 源图的下采样：16~64 给面板与菜单，128/256 给文件管理器与
# HiDPI 缩放。**不生成比源图更大的档**，那是无中生有地插值。
#
# 这一段必须排在 ④ 之前：④ 要用 `--add-data` 把这份 PNG 打进包里，文件得先
# 存在。它又必须在 ③ 之后，因为要借 venv 里的 Pillow。
# ---------------------------------------------------------------------------
#: 打进 PyInstaller 包里的窗口图标名。必须与 main.py 的 APP_ICON 一致。
APP_ICON_NAME="gomoku-ai.png"
ICON_SRC="$(find . -maxdepth 1 -name '*.ico' -print -quit)"
if [ -z "$ICON_SRC" ]; then
    echo "!! 仓库根下找不到 .ico 图源 —— 安装包会没有图标" >&2
    exit 1
fi
ICON_SIZES=(16 24 32 48 64 128 256)
ICON_OUT="$(mktemp -d)"
.venv-build/bin/python - "$ICON_SRC" "$ICON_OUT" "${ICON_SIZES[@]}" <<'PY'
import os
import shutil
import sys

from PIL import Image

src, out = sys.argv[1], sys.argv[2]
sizes = [int(s) for s in sys.argv[3:]]
name = "gomoku-ai.png"

img = Image.open(src).convert("RGBA")
if img.width != img.height or img.width < max(sizes):
    sys.exit("图源不是边长 >= %d 的正方形：%s" % (max(sizes), img.size))

for s in sizes:
    d = os.path.join(out, "hicolor", "%dx%d" % (s, s), "apps")
    os.makedirs(d, exist_ok=True)
    img.resize((s, s), Image.LANCZOS).save(os.path.join(d, name))

# 打进包里的那一份取最大档；main.py 的 _app_icon() 会把它压到 128 以内再
# 交给 setWindowIcon() —— 256×256 塞不进 X 的单次请求，属性会写空。
shutil.copyfile(
    os.path.join(out, "hicolor", "%dx%d" % (max(sizes), max(sizes)),
                 "apps", name),
    os.path.join(out, name))

print("图标已生成：%s（源 %s，%s）" % (
    ", ".join("%dx%d" % (s, s) for s in sizes), src, "%dx%d" % img.size))
PY

# 断言每档 PNG 都真的落盘且非空。Pillow 静默失败就等于没有图标 ——
# 与"引擎没进包"同一类：构建不报错，只是用户看到空白图标。
for _s in "${ICON_SIZES[@]}"; do
    _p="${ICON_OUT}/hicolor/${_s}x${_s}/apps/gomoku-ai.png"
    [ -s "$_p" ] || { echo "!! 图标缺失或为空：$_p" >&2; exit 1; }
done
[ -s "${ICON_OUT}/${APP_ICON_NAME}" ] \
    || { echo "!! 打进包的窗口图标缺失：${ICON_OUT}/${APP_ICON_NAME}" >&2; exit 1; }

# ---------------------------------------------------------------------------
# ④ 产物：onedir
#
# `--add-binary "cpp/build/gomoku_engine:."` 把引擎放到包的根（_MEIPASS 下），
# 正是 _candidates() 第一个去找的位置。**八个二进制全部都要带**（四个尺寸各
# free/ban）；少任何一个，对应尺寸会静默退回 Python 参考实现 —— AI 明显变弱
# 而且面板照样写着"Python (本地)"，没有别的症状。
#
# **禁手版（`gomoku_engine_ban`）同样要带。** 两个引擎各自独立
# 存在，只带 free 版的话，包里的禁手局会退回 `engine_ban.py` 那份本地 Python
# 实现 —— 后果不只是慢：本机 `test_ban_fork_guard` 钉的是"两条实现同着同分"，
# 而一旦用户装了包，实际跑的是没被那份护栏覆盖过的第三条路（包里的
# `engine_ban.py`），面板还会如实写着"Python (本地·禁手)"。用户看到的是
# "选了禁手之后 AI 突然变笨"，与 v3.0.8 那次 MSVCP140 是同一类静默退化。
#
# `--add-data "${ICON_OUT}/gomoku-ai.png:."` 同理，落点也是包的根，由
# main.py 的 `_icon_candidates()` 第一个去找。漏了它窗口就没有图标（X11 上
# 退回 X.Org 的兜底 logo），而构建与运行都不报错 —— 与漏引擎同一类静默退化，
# 所以下面也有断言。
#
# **注入模块走 `--paths`**：往临时目录写两个一行的模块，让 PyInstaller 把它
# 加进分析期的模块搜索路径。写临时目录是为了不在仓库工作区留下生成物 ——
# 这份脚本在 CI 里跑检出版、在本地跑真工作树，两边都不该多出需要 gitignore
# 的文件。
#
#   _build_flavor.py    FLAVOR = "play"（发布产物只有 play；排障用 `--debug`）
#   _build_version.py   APP_VERSION / BUILD_CHANNEL，见 version_info.py
#
# 安装包装的是 **play**：普通用户那边不该堆 game_log。
# ---------------------------------------------------------------------------
FLAVOR_DIR="$(mktemp -d)"
printf 'FLAVOR = "play"\n' > "${FLAVOR_DIR}/_build_flavor.py"
printf 'APP_VERSION = "%s"\nBUILD_CHANNEL = "github"\n' "$APP_VERSION" \
    > "${FLAVOR_DIR}/_build_version.py"

PYI_ARGS=()
for _bin in $ENGINE_BINS; do
    PYI_ARGS+=(--add-binary "cpp/build/${_bin}:.")
done

echo "---- PyInstaller: onedir（安装包 / AppImage 的唯一原料，play）----"
.venv-build/bin/pyinstaller --onedir --windowed --name "gomoku-ai" \
    --paths "${FLAVOR_DIR}" \
    "${PYI_ARGS[@]}" \
    --add-data "${ICON_OUT}/${APP_ICON_NAME}:." main.py

rm -rf "${FLAVOR_DIR}"

ls -lh "dist/gomoku-ai/gomoku-ai"
file "dist/gomoku-ai/gomoku-ai"

# 断言注入的两个模块真的被收进了包。
#
# **这是这套机制唯一会静默失败的地方**：`--paths` 若没生效，`gamelog.py` 与
# `version_info.py` 里那两个 `try: from _build_... import ...` 会把 ImportError
# 吞掉，于是程序照常启动 —— 只是版本号显示成 `0.0.0-dev`、更新检查永远说
# "用 git pull 升级"。不报错、不崩，查起来要绕一大圈。
#
# 查的是 PyInstaller 自己写下的分析产物（`build/<name>/*.toc` 里列着收进去的
# 模块名），而不是最终二进制：onedir 的 PYZ 同样是压缩过的，直接对二进制
# `grep` 一个字符串既可能假阴也可能假阳。
for _mod in _build_flavor _build_version; do
    if ! grep -rqs --include='*.toc' "$_mod" "build/gomoku-ai"; then
        echo "!! 分析产物里没有 ${_mod} —— --paths 没生效" >&2
        exit 1
    fi
done
echo "注入模块都带上了：_build_flavor / _build_version（${APP_VERSION}）"

# 断言引擎确实进包了。**这是最容易静默漏掉的一环**：漏了不会报错，只会让
# 对应尺寸的 AI 退回 Python 参考实现。`--add-binary` 指向不存在的文件时
# PyInstaller 会硬错，所以这里验的是"落点对不对"与"八个是不是都在"。
# `-print -quit`：找到第一个就停。**这不是提速，是消掉同一类 SIGPIPE 隐患**
# —— `grep -q` 一命中就退出并关管道，find 若还要继续写就会被 141 干掉，
# `pipefail` 于是把整条管道判成失败，`if !` 随之翻转为"引擎没进包"并 `exit 1`：
# 一个**假警报**把已经成功的构建判死。（实测这条 find 的输出只有一行、
# 写完即退，所以侥幸没炸过 —— 但"侥幸"不是能留在 CI 里的东西。）
# **八个名字都验。** `-name gomoku_engine` 是精确匹配，匹配不到
# `gomoku_engine_ban` 也匹配不到 `gomoku_engine_15`：只验 19 的 free 版，
# 其余七个漏进包时这条断言照样绿，用户选中该尺寸之后才发现 AI 退回本地
# Python 实现。
for engine in $ENGINE_BINS; do
    if ! find dist/gomoku-ai -type f -name "${engine}" -print -quit | grep -q .; then
        echo "!! C++ 引擎 ${engine} 没有进包 —— 检查 --add-binary" >&2
        exit 1
    fi
    echo "引擎已进包：$(find dist/gomoku-ai -type f -name "${engine}")"
done

# 断言窗口图标同样进了包 —— 用的还是上面那条 `--add-data`，验 onedir 这份就够。**这是"标题栏与任务栏仍然没有图标"唯一会静默
# 发生的地方**：漏了它桌面环境只会拿 X.Org 的兜底 logo 顶上，不报错、
# 不影响启动，用户只看到"图标不对"。
if ! find dist/gomoku-ai -type f -name "${APP_ICON_NAME}" -print -quit | grep -q .; then
    echo "!! 窗口图标没有进包 —— 检查 --add-data" >&2
    exit 1
fi
echo "窗口图标已进包：$(find dist/gomoku-ai -type f -name "${APP_ICON_NAME}")"

# ---------------------------------------------------------------------------
# ⑤ 收成安装包
# ---------------------------------------------------------------------------
APP_DIR_NAME="gomoku-ai"          # onedir 目录名，也是装好之后的入口名
OPT_DIR="/opt/gomoku-ai"
BIN_LINK="/usr/local/bin/gomoku-ai"

# `Icon=` 用**主题名**而不是路径、不带扩展名：绝对路径的图标不能按 DPI 缩放，
# 也无法被主题替换。这个名字必须与 hicolor 下那个 PNG 的基名（③.5 生成的
# `gomoku-ai.png`）一致 —— 与 .desktop 文件名、可执行名三者同为一个
# `gomoku-ai`，不引入新标识符。
#
# `StartupWMClass=` 让桌面环境把**运行中的窗口**关联到这个 .desktop（Menu 里
# 那条入口靠文件名匹配，窗口靠这个）。
#
# **窗口的图标不靠这一条**。实测过：`.desktop` 装齐之后窗口属性里仍然没有
# `_NET_WM_ICON`，任务栏显示的是 X.Org 的兜底 logo —— 桌面环境读的是窗口
# 自身的图标属性，只有 main.py 里那个 `app.setWindowIcon()` 写得出来。这一条
# 管的是"把窗口归到哪个应用"，与图标是两件事，两个都要有。
DESKTOP_ENTRY='[Desktop Entry]
Version=1.0
Name=五子棋AI
Name[zh_CN]=五子棋AI
Comment=Gomoku AI - Negamax/PVS + Transposition Table + Quiescence & VCF (CPU)
Exec=/usr/local/bin/gomoku-ai
Icon=gomoku-ai
StartupWMClass=gomoku-ai
Terminal=false
Type=Application
Categories=Game;BoardGame;'

# `Depends:` 里的字体链是必需的：界面全靠中文，而这些基础库一个字体都不带，
# 最小系统上装完全是方框。用 `|` 串出候选链，任意一个 CJK 字体已存在即满足，
# 不必为了一个棋盘拖 60MB 的 Noto CJK 下来。
#
# `hicolor-icon-theme` 不能省：它提供 `/usr/share/icons/hicolor/index.theme`，
# 而没有这个文件，GTK 的图标主题引擎根本不把 hicolor 当一个主题来看，`Icon=`
# 也就查不到 —— **图标明明装进去了却显示不出来**，正是最难查的那一类症状。
# 它同时声明了对 `/usr/share/icons/hicolor` 的 dpkg 触发器，负责在我们把 PNG
# 放进去之后刷新图标缓存。最小系统上我们的包可能就是唯一往 hicolor 里放东西
# 的那个，指望别人把它带进来是不成立的。
DEB_DEPENDS="libc6 (>= 2.28), libgl1, libglib2.0-0, libxkbcommon0, hicolor-icon-theme, fonts-noto-cjk | fonts-wqy-microhei | fonts-wqy-zenhei | fonts-arphic-uming"

DEB_DESC="五子棋AI - 位棋盘增量评估 + Negamax/PVS + 置换表 + 静止搜索/VCF 连续冲四 (纯 CPU)"

if [ "$EXT" = "appimage" ]; then
    # -------------------------------------------------------------------
    # AppImage：一个**可执行文件**，也是 Linux 上唯一支持自动更新的形态。
    #
    # 更新时 updater.py 对正在运行的这个文件做 `os.replace` —— 运行中的 ELF
    # 已经映射进内存，替换目录项不会影响当前进程，下次启动就是新版本。这要求
    # 产物是"单个自挂载文件"（AppImage type 2），不能是解包目录，所以这里
    # 从头组装 AppDir 而**不复用 .deb 的暂存树**。
    #
    # PyInstaller 的 onedir 原样放进 `usr/bin/`：`gomoku-ai` 与 `_internal/`
    # 必须相邻（bootloader 按自身路径找 `_internal`），所以整个目录一起搬，
    # 不拆。
    # -------------------------------------------------------------------
    echo "---- 打包 AppImage (${STEM}) ----"

    # mksquashfs 是 appimagetool 的硬依赖；1.9.x 的 AppImage 里虽然自带一份，
    # 但显式装上不贵，也避免"某个发行版镜像里恰好缺了"这种只在 CI 炸的问题。
    apt-get install -y -qq --no-install-recommends squashfs-tools

    APPDIR="AppDir"
    rm -rf "$APPDIR"
    mkdir -p "${APPDIR}/usr/bin" "${APPDIR}/usr/share/applications" \
             "${APPDIR}/usr/share/icons"

    cp -r "dist/${APP_DIR_NAME}/." "${APPDIR}/usr/bin/"
    chmod -R 755 "${APPDIR}/usr/bin"

    # `.desktop` 与 deb/tar.gz 共用同一份内容，只把 Exec 换成 AppImage 内的
    # 相对入口名 —— AppRun 会把 `gomoku-ai` 解析到 `usr/bin/gomoku-ai`。
    printf '%s\n' "$DESKTOP_ENTRY" | sed 's|^Exec=.*|Exec=gomoku-ai|' \
        > "${APPDIR}/gomoku-ai.desktop"
    cp "${APPDIR}/gomoku-ai.desktop" \
       "${APPDIR}/usr/share/applications/gomoku-ai.desktop"

    # 图标：usr/share/icons 下保留七档（装进系统后菜单用），AppDir 根下再放
    # 一份最大的 —— appimagetool 会把根目录的 `gomoku-ai.png` 当作 .DirIcon，
    # 文件管理器与桌面集成靠它显示图标。
    cp -r "${ICON_OUT}/hicolor" "${APPDIR}/usr/share/icons/"
    chmod -R u=rwX,go=rX "${APPDIR}/usr/share/icons"
    cp "${ICON_OUT}/hicolor/256x256/apps/gomoku-ai.png" "${APPDIR}/gomoku-ai.png"
    ln -sf gomoku-ai.png "${APPDIR}/.DirIcon"

    cat > "${APPDIR}/AppRun" <<'EOF'
#!/bin/sh
# AppImage 的入口。readlink -f 解析 $0：从符号链接、PATH 或桌面启动器进来
# 时 $0 不是 AppImage 内的绝对路径，dirname 会指错地方。
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/usr/bin/gomoku-ai" "$@"
EOF
    chmod 755 "${APPDIR}/AppRun"

    # 断言：桌面入口、图标、可执行体三者齐全。漏任何一个都可能"能跑但没图标"，
    # 而 AppImage 的失败是静默的 —— 构建不报错，用户只看到空白图标。
    grep -q '^Icon=gomoku-ai$' "${APPDIR}/gomoku-ai.desktop" \
        || { echo "!! AppDir 的 .desktop 里没有 Icon=gomoku-ai" >&2; exit 1; }
    [ -x "${APPDIR}/usr/bin/${APP_DIR_NAME}" ] \
        || { echo "!! AppDir 里没有可执行入口 ${APP_DIR_NAME}" >&2; exit 1; }
    [ -s "${APPDIR}/gomoku-ai.png" ] \
        || { echo "!! AppDir 根下缺 gomoku-ai.png（.DirIcon）" >&2; exit 1; }
    for _s in "${ICON_SIZES[@]}"; do
        [ -s "${APPDIR}/usr/share/icons/hicolor/${_s}x${_s}/apps/gomoku-ai.png" ] \
            || { echo "!! AppDir 里缺 ${_s}x${_s} 图标" >&2; exit 1; }
    done

    # --- appimagetool：固定 1.9.1，按架构校验 SHA-256 ---------------------
    #
    # **不跟 `continuous`**：那是一个每天变的滚动 tag，哈希无法固定，"校验下载"
    # 就退化成"下载什么信什么"。1.9.1 是 2025-11-18 的正式发布。
    #
    # 哈希从官方 Release 下载后本地算得；换版本必须同时改 URL 与哈希。
    APPIMAGETOOL_VERSION="1.9.1"
    APPIMAGETOOL_SHA256_amd_x86_64="8e5a55a3740f92ebf77f1c88c3aa466ace6ad9739ed173d288023bac61457525"
    APPIMAGETOOL_SHA256_aarch64="5ebab677bb0532e7e342487e5e3a40d648963d7a7faa299001c8ab5b3ca3ba7c"
    case "$STEM" in
        amd_x86_64) TOOL_ARCH="x86_64";  TOOL_SHA="$APPIMAGETOOL_SHA256_amd_x86_64" ;;
        aarch64)    TOOL_ARCH="aarch64"; TOOL_SHA="$APPIMAGETOOL_SHA256_aarch64" ;;
        *) echo "!! 没有 ${STEM} 的 appimagetool" >&2; exit 2 ;;
    esac

    TOOL_DIR="$(mktemp -d)"
    TOOL="${TOOL_DIR}/appimagetool-${TOOL_ARCH}.AppImage"
    URL="https://github.com/AppImage/appimagetool/releases/download/${APPIMAGETOOL_VERSION}/appimagetool-${TOOL_ARCH}.AppImage"
    echo "下载 appimagetool ${APPIMAGETOOL_VERSION} (${TOOL_ARCH}) …"
    # --retry 是给 GitHub releases 的 5xx/断流：CI 上一次偶发失败会让整条
    # 发布流水线红掉，而重试三次基本都过。
    curl -fsSL --retry 3 --retry-delay 2 -o "$TOOL" "$URL"
    echo "${TOOL_SHA}  ${TOOL}" | sha256sum -c -
    chmod 755 "$TOOL"

    # 容器里没有 FUSE，直接执行 AppImage 会 `dlopen libfuse` 失败；
    # `--appimage-extract-and-run` 让运行时先解包到临时目录再执行，不碰 FUSE。
    # `ARCH` 必须显式给：appimagetool 从 `uname -m` 猜，而它本身跑在
    # extract-and-run 的临时目录里。
    ARCH="$TOOL_ARCH" "$TOOL" --appimage-extract-and-run --no-appstream \
        "$APPDIR" "${BASE}.AppImage"

    rm -rf "$TOOL_DIR"
    chmod 755 "${BASE}.AppImage"
    [ -s "${BASE}.AppImage" ] || { echo "!! AppImage 没有产出" >&2; exit 1; }
    ls -lh "${BASE}.AppImage"
    # 文件头必须是 AppImage 的 ELF：若 appimagetool 静默失败留下一个
    # 空壳或纯 squashfs 镜像，这一步能当场抓住。
    file "${BASE}.AppImage" | grep -q ELF \
        || { echo "!! ${BASE}.AppImage 不是可执行文件" >&2; exit 1; }

elif [ "$EXT" = "deb" ]; then
    echo "---- 打包 .deb (${DEB_ARCH}) ----"
    PKG_DIR="deb_build"
    rm -rf "$PKG_DIR"
    mkdir -p "${PKG_DIR}${OPT_DIR}" "${PKG_DIR}/usr/local/bin" \
             "${PKG_DIR}/usr/share/applications" "${PKG_DIR}/usr/share/icons" \
             "${PKG_DIR}/DEBIAN"

    cp -r "dist/${APP_DIR_NAME}/." "${PKG_DIR}${OPT_DIR}/"
    chmod -R 755 "${PKG_DIR}${OPT_DIR}"

    printf '%s\n%s\n' '#!/bin/bash' "exec ${OPT_DIR}/${APP_DIR_NAME} \"\$@\"" \
        > "${PKG_DIR}/usr/local/bin/${APP_DIR_NAME}"
    chmod 755 "${PKG_DIR}/usr/local/bin/${APP_DIR_NAME}"

    printf '%s\n' "$DESKTOP_ENTRY" > "${PKG_DIR}/usr/share/applications/gomoku-ai.desktop"

    # 图标。权限显式写死而不用 `cp -r` 的默认值：源 PNG 是 644，但 umask 会
    # 决定拿到什么，`u=rwX,go=rX` 让目录 755、文件 644 与 umask 无关。
    cp -r "${ICON_OUT}/hicolor" "${PKG_DIR}/usr/share/icons/"
    chmod -R u=rwX,go=rX "${PKG_DIR}/usr/share/icons"

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

    # 断言 .desktop 里确实写了 Icon=，且各档 PNG 都进了包目录。
    # **这是本次修复唯一会静默退化的地方**：Icon= 漏写、或图标没拷进来，
    # 构建与安装都不报错，只有用户看到空白图标。断言打在暂存树上（那正是
    # dpkg-deb 的输入），不去读几千条目的归档流。
    grep -q '^Icon=gomoku-ai$' "${PKG_DIR}/usr/share/applications/gomoku-ai.desktop" \
        || { echo "!! .desktop 里没有 Icon=gomoku-ai —— 菜单会退回空白图标" >&2; exit 1; }
    for _s in "${ICON_SIZES[@]}"; do
        [ -s "${PKG_DIR}/usr/share/icons/hicolor/${_s}x${_s}/apps/gomoku-ai.png" ] \
            || { echo "!! 安装包里缺 ${_s}x${_s} 图标" >&2; exit 1; }
    done

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

    # 图标随包装走，由 install.sh 在安装时铺到 /usr/share/icons/hicolor/。
    # 放在 BASE 下的 icons/ 而不是与 .desktop 平铺：7 个尺寸 × 目录层级，
    # 平铺会把解包目录弄得很难看。
    mkdir -p "${BASE}/icons"
    cp -r "${ICON_OUT}/hicolor" "${BASE}/icons/"
    chmod -R u=rwX,go=rX "${BASE}/icons"

    cat > "${BASE}/install.sh" <<EOF
#!/bin/bash
# 五子棋AI ${STEM} 版安装脚本。不需要联网，不需要编译。
set -e

APP_DIR="${OPT_DIR}"
SRC_DIR="\$(cd "\$(dirname "\$0")" && pwd)"

echo "Installing GomokuAI (${STEM}) to \${APP_DIR} ..."
sudo mkdir -p "\${APP_DIR}"
sudo cp -r "\${SRC_DIR}/${APP_DIR_NAME}/." "\${APP_DIR}/"
sudo chmod -R 755 "\${APP_DIR}"
sudo ln -sf "\${APP_DIR}/${APP_DIR_NAME}" "${BIN_LINK}"

# 装桌面菜单项（可选，失败不影响使用）
if [ -d /usr/share/applications ]; then
    sudo cp "\${SRC_DIR}/gomoku-ai.desktop" /usr/share/applications/ || true
fi

# 装图标（可选，失败不影响使用）。hicolor 是 freedesktop 约定的主题目录，
# 桌面环境按 .desktop 里那句 Icon=gomoku-ai 去 hicolor/<尺寸>/apps/ 下找。
# **tar 包声明不了依赖**：最小系统上可能连 hicolor-icon-theme 都没有，那时
# GTK 不把 hicolor 当主题，图标依旧显示不出来 —— 只能尽力而为。
if [ -d "\${SRC_DIR}/icons/hicolor" ] && [ -d /usr/share/icons ]; then
    sudo cp -r "\${SRC_DIR}/icons/hicolor/." /usr/share/icons/hicolor/ || true
    # 有 gtk-update-icon-cache 就刷缓存；没有就跳过 —— 直接读目录也能用。
    if command -v gtk-update-icon-cache >/dev/null 2>&1; then
        sudo gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor 2>/dev/null || true
    fi
fi
# update-desktop-database 维护的是 MIME 关联缓存，菜单入口的显示不依赖它，
# 装了就顺手刷一下。
if command -v update-desktop-database >/dev/null 2>&1; then
    sudo update-desktop-database -q 2>/dev/null || true
fi

# 清掉旧版 ARM 的目录名，避免同一个程序留下两份
sudo rm -rf /opt/gomoku-ai-arm

echo "Done! Run: gomoku-ai"
EOF
    chmod 755 "${BASE}/install.sh"

    # 与 .deb 分支同一套断言：Icon= 漏写或图标没拷进来，构建与安装都不报错，
    # 只有用户看到空白图标 —— 必须在这里拦住。
    grep -q '^Icon=gomoku-ai$' "${BASE}/gomoku-ai.desktop" \
        || { echo "!! tar 包的 .desktop 里没有 Icon=gomoku-ai" >&2; exit 1; }
    for _s in "${ICON_SIZES[@]}"; do
        [ -s "${BASE}/icons/hicolor/${_s}x${_s}/apps/gomoku-ai.png" ] \
            || { echo "!! tar 包里缺 ${_s}x${_s} 图标" >&2; exit 1; }
    done

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

# 图标暂存目录回收。与 FLAVOR_DIR 一样从 mktemp 来，不留在工作区 ——
# 这份脚本在 CI 里跑检出版、在本地跑真工作树（两个分支都已经把需要的东西
# 复制进各自的暂存树了，这里删掉不再有副作用）。
rm -rf "${ICON_OUT}"

echo "=============================================================="
echo " 完成："
if [ "$EXT" = "appimage" ]; then
    ls -lh "${BASE}.AppImage"
else
    ls -lh "${PKG_NAME}.${EXT}"
fi
echo "=============================================================="

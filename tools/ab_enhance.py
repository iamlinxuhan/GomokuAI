# -*- coding: utf-8 -*-
"""增强搜索（LMR / 强制着法延伸）的 A/B 自对弈台。

用法::

    python tools/ab_enhance.py --level 4 --games 12 --time 1.5
    python tools/ab_enhance.py --level 5 --games 12 --time 1.5 --arm lmr

**为什么需要这个工具，而不是直接跑两次 selfplay。** 自对弈的 A/B 必须是
**配对**的：同一批开局、双方交换先后手各下一遍。那要求两个"引擎"活在**同一个
进程**里，而 `engine.ai_move` 的行为由模块级 `DIFFICULTY` 与 `compute()` 里
下发的三个开关决定 —— 同一个模块不可能同时是开与关。

**为什么是"抄一份源码再打补丁"，不是"重新实现一份 compute"。** 重新实现一遍
请求构造就等于把被测代码复制成两份，两份会漂移：测出来的差异从此可能是补丁与
真身的差异，而不是增强与不增强的差异。所以这里复制 `engine.py` 的源码，只对
**两处**做文本替换（开关的值、时限的值），其余一字不动。

**锚点找不到就立刻退出。** 补丁靠字符串匹配，源码一改就可能匹配不上 —— 那时
两个"引擎"会退化成**逐字相同**的两个副本，A/B 会报出一个漂亮的 50%，看起来
像"增强没用"，实际是**根本没测**。这种静默失败是这类工具最坏的结局，所以
`_patch_source` 匹配不到时直接 SystemExit，绝不放过。

时限默认压到 1.5 s：15 s / 20 s 一局要跑几十分钟，做不了统计。**两边同等降时**
仍然能回答"同一预算下谁更强"，而这正是用户那句"在时限不变的前提下变强"的
问题形式；把 20 s 的结果外推到 1.5 s 是不成立的，但"同一预算下更强"可以。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: `compute()` 里下发增强三字段的那一段。**逐字匹配** —— 改 `engine.py` 的这段
#: 就要同步改这里，否则 `_patch_source` 会报错退出（那是刻意的）。
#: `engine.py` 的 import 段。**逐字匹配**，理由同 `_ANCHOR_FLAGS`。
_ANCHOR_IMPORT = "import config\nimport engine_local as _local\n"

#: `compute()` 里下发增强三字段的那一段。**逐字匹配** —— 改 `engine.py` 的这段
#: 就要同步改这里，否则 `_patch_source` 会报错退出（那是刻意的）。
_ANCHOR_FLAGS = '''        if cfg.get("enhance"):
            req["enhanced"] = 1
            req["lmr"] = 1
            req["extend"] = 1
'''

#: 两个臂各自的服务端端口起点。必须与应用自己的端口池（``config.PORT_POOL``）
#: 分开 —— 否则与正在跑的游戏 / 建库 / bench 撞在一起，或者两个臂互相撞。
AB_PORT = 8901

_ARMS = {
    "off": "增强全关（= 今天的高级/宗师）",
    "lmr": "只开晚着法削减",
    "ext": "只开强制着法延伸",
    "both": "两者都开（= 生产中下发的组合）",
}

_FLAG_VALUES = {
    "off": (0, 0, 0),
    "lmr": (1, 1, 0),
    "ext": (1, 0, 1),
    "both": (1, 1, 1),
}


def _patch_source(src: str, arm: str, time_limit: float, port: int) -> str:
    """把 `engine.py` 的源码改成"这一侧固定用 arm 臂、时限为 time_limit"。

    **还要给这一臂一个独享的服务端端口。** 两个臂都是 `engine.py` 的副本，
    各自持有一个 `_ServerClient`，而它们 `import config` 拿到的是**同一个模块
    对象** —— 于是两边都去 8888 上 spawn。第二个服务端 bind 失败立刻退出，
    而 `_spawn_and_wait` 的轮询会**先连上第一个**（连接成功即返回），所以它
    拿着一个已经死掉的 `_proc` 高高兴兴地用起了别人的服务端。

    后果是 A/B 的两个臂**共用一个进程、一块置换表**：一边写入的条目会把另一边
    的挤出去，而 `enhanced=0` 与 `enhanced=1` 用的还是**两套不同的索引几何**
    （见 `cpp/src/search.cpp` 的 `lookup`），互相读到的多半是垃圾。这个偏差的
    方向是确定的 —— **系统性拉向"打平"**，也就是最容易让人误判成"增强没用"。

    修法只动这一臂自己：把 `config` 在这个模块里换成一份**浅拷贝的视图**并改掉
    `PORT`。`config` 里 `PORT` 是个裸常量，`find_binary()` / `binary_name()`
    都不读它，所以拷贝是安全的；`engine.py` 里所有 `config.PORT` / `config.HOST`
    的引用都在**本模块**的全局命名空间里解析，因此只有这一臂看到新端口。
    """
    if _ANCHOR_IMPORT not in src:
        raise SystemExit(
            "补丁锚点没找到：engine.py 里 `import config` 那一段已经不是原来的"
            "样子了。见 _ANCHOR_IMPORT，理由同下面 _ANCHOR_FLAGS —— "
            "**不要**改成「匹配不到就跳过」。")
    src = src.replace(_ANCHOR_IMPORT, _ANCHOR_IMPORT + (
        '\n'
        '# ab_enhance: 本臂独享的服务端端口（理由见 tools/ab_enhance.py）\n'
        'import types as _ab_types\n'
        'config = _ab_types.SimpleNamespace(**vars(config))\n'
        'config.PORT = %d\n' % port))

    if _ANCHOR_FLAGS not in src:
        raise SystemExit(
            "补丁锚点没找到：engine.py 里下发 enhanced/lmr/extend 的那三行"
            " 已经不是原来的样子了。\n"
            "本工具按逐字匹配打补丁，匹配不上就修 tools/ab_enhance.py 的"
            " _ANCHOR_FLAGS。**不要**改成「匹配不到就跳过」——"
            "那样两个引擎会变成逐字相同的副本，A/B 会给出假结果。")
    e, l, x = _FLAG_VALUES[arm]
    src = src.replace(_ANCHOR_FLAGS, (
        '        if True:      # ab_enhance: 该侧固定使用 %s 臂\n'
        '            req["enhanced"], req["lmr"], req["extend"] = %d, %d, %d\n'
        % (arm, e, l, x)))

    # 时限：只替换难度表里的 `time=` 数值，其余参数一字不动。
    pat = re.compile(r'(\b\d+:\s*dict\(name="[^"]+",\s*time=)([0-9.]+)')

    def sub(m):
        return "%s%s" % (m.group(1), repr(float(time_limit)))
    new, n = pat.subn(sub, src)
    if n == 0:
        raise SystemExit("补丁锚点没找到：难度表里没有 `time=` 项")
    return new


def _make_arm(tmpdir: str, arm: str, time_limit: float, port: int) -> str:
    """生成一个模块文件，返回**模块名**（不是路径）。"""
    name = "ab_arm_%s" % arm
    with open(os.path.join(ROOT, "engine.py"), encoding="utf-8") as fh:
        src = fh.read()
    with open(os.path.join(tmpdir, name + ".py"), "w", encoding="utf-8") as fh:
        fh.write(_patch_source(src, arm, time_limit, port))
    return name


def main():
    ap = argparse.ArgumentParser(description="增强搜索 A/B 自对弈")
    ap.add_argument("--level", type=int, default=4, help="档位（4=高级 5=宗师）")
    ap.add_argument("--games", type=int, default=12)
    ap.add_argument("--time", type=float, default=1.5,
                    help="每手预算（秒）。两边同等降低，默认 1.5")
    ap.add_argument("--arm", default="both", choices=sorted(_ARMS),
                    help="被检验的增强组合，另一侧固定是 off（今天的行为）")
    ap.add_argument("--seed", type=int, default=20260919)
    ap.add_argument("--tag", default="")
    args = ap.parse_args()

    if args.arm == "off":
        raise SystemExit("--arm off 就是两侧同配置，得到的一定是 50%，没有信息量")

    with tempfile.TemporaryDirectory(prefix="ab_enhance_") as tmpdir:
        # 一个臂一个端口，两条臂都**不碰应用的端口池**（`config.PORT` 一设就
        # 只用这一个，见 engine._port_candidates）。**不能两个臂共用一个端口** ——
        # 见 `_patch_source` 里那段：第二个服务端 bind 失败、轮询却连上了第一个，
        # 于是两个臂共用一个进程与一块置换表，A/B 被拉向"打平"。
        enh = _make_arm(tmpdir, args.arm, args.time, AB_PORT)
        base = _make_arm(tmpdir, "off", args.time, AB_PORT + 1)
        env = dict(os.environ)
        env["PYTHONPATH"] = tmpdir + os.pathsep + env.get("PYTHONPATH", "")
        cmd = [sys.executable, os.path.join("tools", "selfplay.py"),
               "--black", enh, "--white", base,
               "--black-level", str(args.level),
               "--white-level", str(args.level),
               "--games", str(args.games), "--seed", str(args.seed),
               "--tag", args.tag or ("ab_enhance_%s_L%d" % (args.arm, args.level))]
        print("增强侧=%s(%s)  基线侧=%s  档位=L%d  每手=%.1fs  局数=%d"
              % (enh, _ARMS[args.arm], base, args.level, args.time, args.games))
        print("A/B 是**配对**的（同开局、换先后手各一遍），所以胜率的分母是"
              "两侧各自的对局数，不是总局数。")
        proc = subprocess.run(cmd, cwd=ROOT, env=env)
        return proc.returncode


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""版本号与构建渠道。**构建期注入，运行时不再读 pyproject.toml。**

打包脚本往 ``--paths`` 的临时目录里写一个一行的 ``_build_version.py``（与
``_build_flavor.py`` 同一个机制）::

    APP_VERSION = "4.4.0"
    BUILD_CHANNEL = "github"      # github / msix / source

源码运行时这个模块不存在，于是落到下面那份兜底值：
``0.0.0-dev`` + ``source`` —— **自动更新在源码运行时只检查、不安装**（见
``updater.install_kind``），所以这两个值只需要"能显示、能比较"，不需要准确。

为什么不读 ``pyproject.toml``：打包后的进程里那份文件不存在；就算存在，
也只有源码运行才有意义，而源码运行的版本号本来就不该冒充发布版。
"""

from __future__ import annotations

try:
    from _build_version import APP_VERSION, BUILD_CHANNEL
except ImportError:       # 源码运行，或打包时漏了注入
    APP_VERSION = "0.0.0-dev"
    BUILD_CHANNEL = "source"

#: 显示用的版本串。**带不带 ``v`` 前缀由界面决定** —— 这里只给数字与后缀。
__all__ = ["APP_VERSION", "BUILD_CHANNEL"]

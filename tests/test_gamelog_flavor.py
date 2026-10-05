# -*- coding: utf-8 -*-
"""打包版的日志落点：debug 写盘、play 不写。

`gamelog._resolve_log_dir()` 是**唯一**决定"这次运行到底留不留文件"的地方，
而它只在冻结运行时才有分支 —— 也就是说，本仓库里所有的测试都跑在源码模式下，
**一条都覆盖不到**那段代码。它此前出过一次性质很接近的事故候选（Linux
`.deb` 装到 `/opt` 后 `open(..., 'w')` 抛 PermissionError），所以这里把冻结
分支逐条钉住。

`sys.frozen` / `sys.executable` 用 monkeypatch 模拟：`gamelog.sys` 就是真正的
`sys` 模块，patch 会在用例结束后自动还原。
"""

from __future__ import annotations

import os
import stat
import sys

import pytest

import gamelog


def _fake_frozen(monkeypatch, exe_path):
    """把 `gamelog` 看到的世界伪装成"从 `exe_path` 冻结束起来的"。"""
    monkeypatch.setattr(gamelog.sys, "frozen", True, raising=False)
    monkeypatch.setattr(gamelog.sys, "executable", str(exe_path))
    monkeypatch.delenv(gamelog.LOG_DIR_ENV, raising=False)


@pytest.fixture
def exe_dir(tmp_path):
    d = tmp_path / "appdir"
    d.mkdir()
    return d


def test_frozen_debug_writes_next_to_exe(monkeypatch, exe_dir):
    """debug 变体：写到可执行文件同目录 —— 用户只需"把 exe 旁边那些 txt 发我"。"""
    _fake_frozen(monkeypatch, exe_dir / "GomokuAI_Linux_amd_x86_64_run_debug")
    monkeypatch.setattr(gamelog, "FLAVOR", "debug")
    assert gamelog._resolve_log_dir() == str(exe_dir)


def test_frozen_play_writes_nothing(monkeypatch, exe_dir):
    """play 变体：一个文件都不留（发行版的既有策略）。"""
    _fake_frozen(monkeypatch, exe_dir / "GomokuAI_Linux_amd_x86_64_run_play")
    monkeypatch.setattr(gamelog, "FLAVOR", "play")
    assert gamelog._resolve_log_dir() is None


def test_frozen_debug_falls_back_when_dir_readonly(monkeypatch, exe_dir):
    """debug 但目录不可写 → 退化成不写，而不是在开一局时崩掉。

    只读目录用 chmod 造；**root 能无视权限位**，那种环境下这条断言不成立，
    所以显式跳过而不是让它变成一个偶发的假红。**Windows 也造不出来**：
    它的目录权限不走 POSIX 权限位，chmod 之后照样写得进去。
    """
    if not hasattr(os, "geteuid"):
        pytest.skip("Windows 没有 POSIX 权限位，造不出只读目录")
    if os.geteuid() == 0:
        pytest.skip("root 无视文件权限位，造不出只读目录")
    _fake_frozen(monkeypatch, exe_dir / "GomokuAI_Linux_amd_x86_64_run_debug")
    monkeypatch.setattr(gamelog, "FLAVOR", "debug")

    exe_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        assert gamelog._resolve_log_dir() is None
    finally:
        exe_dir.chmod(stat.S_IRWXU)


@pytest.mark.parametrize("flavor", ["debug", "play"])
def test_env_override_beats_flavor(monkeypatch, exe_dir, tmp_path, flavor):
    """`GOMOKU_AI_LOGDIR` 是显式逃生口：远程排查现场时，play 版也能被指到某处。"""
    _fake_frozen(monkeypatch, exe_dir / "GomokuAI_Linux_amd_x86_64_run_play")
    monkeypatch.setattr(gamelog, "FLAVOR", flavor)
    target = tmp_path / "elsewhere"
    monkeypatch.setenv(gamelog.LOG_DIR_ENV, str(target))
    assert gamelog._resolve_log_dir() == str(target)


def test_source_run_still_writes_to_module_dir(monkeypatch):
    """源码运行不受 FLAVOR 影响 —— 开发者那边要把日志落在仓库里。"""
    monkeypatch.delenv(gamelog.LOG_DIR_ENV, raising=False)
    monkeypatch.setattr(gamelog, "FLAVOR", "play")
    monkeypatch.delattr(gamelog.sys, "frozen", raising=False)
    assert gamelog._resolve_log_dir() == os.path.dirname(
        os.path.abspath(gamelog.__file__))


def test_flavor_defaults_to_play_when_not_injected():
    """没注入 `_build_flavor` 时（源码运行、或注入漏了）默认 play。

    默认成 debug 会让"打包时漏了注入"变成**静默往用户硬盘上堆文件**；
    默认 play 最多是"debug 版没写出日志"，而那条另有构建期断言兜住。
    """
    assert gamelog.FLAVOR == "play"


def test_sys_is_not_left_patched():
    """自检：上面那些用例没有把 `sys.frozen` 永久留在进程里。"""
    assert not getattr(sys, "frozen", False)

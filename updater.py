# -*- coding: utf-8 -*-
"""检查并安装本程序的新版本。**不依赖 Qt** —— 界面层只调这里的纯函数。

流程刻意分成四步，任何一步失败都不会动到正在运行的程序：

1. 读个人站点的 ``version.json``（失败则读 GitHub ``releases/latest``）；
2. 下载这个 Release 的 ``SHA256SUMS`` 与 ``SHA256SUMS.sig``，**先验签再认哈希**；
3. 把安装包流式写进目标同目录的 ``.new.part``，校验 SHA-256 后改名成 ``.new``；
4. 按安装形态替换并重启。

**签名是信任根。** ``SHA256SUMS`` 与安装包来自同一个 Release，谁都能把两者
一起换掉；只有 Ed25519 分离签名把"这份校验和"绑到发布者离线保管的私钥上。
缺签名、公钥缺失、验签失败，一律拒绝安装 —— 宁可让用户手动下载。

哪些形态能自更新、哪些只能引导到下载页，见 :func:`install_kind`：

======================  ==========================================
``windows-install``     Inno 安装版：静默跑新版 setup.exe，然后退出
``windows-portable``    便携版：写临时 PowerShell 脚本，等本进程退出后换文件
``appimage``            Linux AppImage：原子替换正在运行的文件再重启
``deb`` / ``msix``      包管理器 / 商店管的形态：只提示打开下载页
``source``              源码运行：只检查、不安装
======================  ==========================================
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Mapping

import ed25519
from version_info import APP_VERSION, BUILD_CHANNEL

VERSION = APP_VERSION

#: 发布仓库与站点。资产名与 CI 里生成的名字必须一致，改名要两边一起改。
REPO = "iamlinxuhan/GomokuAI"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
GITHUB_DOWNLOAD = f"https://github.com/{REPO}/releases/download"
SITE_PAGE = "https://www.linxuhan.top/GomokuAI/"
SITE_VERSION = f"{SITE_PAGE}version.json"
MIRROR_BASE = "https://dl.linxuhan.top/GomokuAI"
SUMS_ASSET = "SHA256SUMS"
SIG_ASSET = "SHA256SUMS.sig"

#: 三个能自动更新的资产；``deb`` / ``msix`` / 源码运行没有对应资产。
ASSET_WINDOWS_SETUP = "GomokuAI_Windows_amd_x86_64_setup.exe"
ASSET_WINDOWS_PORTABLE = "GomokuAI_Windows_amd_x86_64_run_play.exe"
ASSET_APPIMAGE = {
    "amd_x86_64": "GomokuAI_Linux_amd_x86_64.AppImage",
    "aarch64": "GomokuAI_Linux_aarch64.AppImage",
}

#: tag / 文件名要能安全地拼进 URL，规则与下载镜像保持一致。
_SAFE_TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SAFE_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")

#: 更新链的信任根：32 字节 Ed25519 公钥的 base64。
#:
#: 私钥只在发布者本机的 ``~/.config/gomoku-ai-release/signing-key.pem``，
#: 既不进仓库也不进 CI（理由见 ``scripts/release_sign.py``）。**轮换公钥只能
#: 随一次新版本一起发出** —— 旧版本仍认旧公钥，所以第一次换钥的那个版本
#: 必须用旧私钥签名。
PUBLIC_KEY_B64 = "0RDxRDyH4QEeJ86kEO0+RU4RO+IXzQR40JP/5X+VmA0="

CHUNK = 1 << 20

#: 更新源偏好的默认值（界面写进配置，这里是兜底）。
SOURCES = ("auto", "site", "github")


class UpdateError(RuntimeError):
    """更新过程中的失败。消息面向用户，不夹带堆栈。"""


# --------------------------------------------------------------------------- #
# 版本与资产名
# --------------------------------------------------------------------------- #

def parse_version(text: str) -> tuple[int, ...]:
    """``"v1.3.0"`` → ``(1, 3, 0)``。

    预发布后缀（``-beta.1``）与构建元数据（``+20261017``）在比较时忽略。
    """
    value = (text or "").strip().lstrip("vV")
    for separator in ("+", "-"):
        value = value.split(separator, 1)[0]
    parts: list[int] = []
    for chunk in value.split("."):
        digits = ""
        for char in chunk:
            if not char.isdigit():
                break
            digits += char
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def is_newer(remote: str | tuple[int, ...], local: str | tuple[int, ...]) -> bool:
    """远端版本是否比本地新。缺位按 0 补齐。"""
    try:
        left = remote if isinstance(remote, tuple) else parse_version(remote)
        right = local if isinstance(local, tuple) else parse_version(local)
    except (TypeError, ValueError):
        return False
    width = max(len(left), len(right))
    return left + (0,) * (width - len(left)) > right + (0,) * (width - len(right))


def machine_stem(machine: str | None = None) -> str:
    """``platform.machine()`` → 资产名里的架构段（``amd_x86_64`` / ``aarch64``）。

    只认 x86_64 与 aarch64：其余架构（x32 / armv7）**不产 AppImage**，走
    ``.deb`` / ``.tar.gz``，也就没有自更新 —— 返回空串让上层去引导下载页。
    """
    value = (machine or platform.machine() or "").lower()
    if value in ("x86_64", "amd64"):
        return "amd_x86_64"
    if value in ("aarch64", "arm64"):
        return "aarch64"
    return ""


def _is_inno_install(exe: Path | None) -> bool:
    """Windows：可执行文件旁边有没有 Inno 的卸载器。

    Inno Setup 在 ``{app}`` 里固定放 ``unins000.exe`` / ``unins000.dat``。
    便携版是 PyInstaller 单文件，旁边只有它自己（和用户放的日志），没有这两个。
    """
    if exe is None:
        return False
    folder = Path(exe).parent
    return any((folder / name).is_file()
               for name in ("unins000.exe", "unins000.dat"))


def _appimage_path() -> str:
    """AppImage runtime 注入的 ``APPIMAGE`` 环境变量（原样返回，可能为空）。"""
    return (os.environ.get("APPIMAGE") or "").strip()


def install_kind(
    *,
    system: str | None = None,
    frozen: bool | None = None,
    appimage: str | None = None,
    executable: str | os.PathLike | None = None,
    channel: str | None = None,
) -> str:
    """本程序是怎么跑起来的，决定"能不能自更新、怎么更新"。

    ``msix``（商店渠道）最先判：那种形态的版本由商店管，连检查都该关掉。
    """
    system = (system or sys.platform).lower()
    channel = (channel if channel is not None else BUILD_CHANNEL).lower()
    if channel == "msix":
        return "msix"
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    if not frozen:
        return "source"

    if system.startswith("linux"):
        path = appimage if appimage is not None else _appimage_path()
        return "appimage" if path and Path(path).is_file() else "deb"
    if system.startswith(("win32", "cygwin", "msys")):
        exe = Path(executable) if executable else Path(sys.executable)
        return "windows-install" if _is_inno_install(exe) else "windows-portable"
    return "unsupported"


def current_executable(kind: str | None = None) -> Path | None:
    """正在运行的这一个可执行文件；源码运行返回 ``None``。"""
    kind = kind or install_kind()
    if kind in ("source", "unsupported", "msix", "deb"):
        return None
    if kind == "appimage":
        value = _appimage_path()
        return Path(value) if value else None
    if getattr(sys, "frozen", False):
        return Path(sys.executable)
    return None


def asset_name(kind: str | None = None,
               machine: str | None = None) -> str:
    """这个安装形态该下载哪个资产；不支持自更新时返回空串。"""
    kind = kind or install_kind()
    if kind == "windows-install":
        return ASSET_WINDOWS_SETUP
    if kind == "windows-portable":
        return ASSET_WINDOWS_PORTABLE
    if kind == "appimage":
        stem = machine_stem(machine)
        return ASSET_APPIMAGE.get(stem, "")
    return ""


def can_auto_install(kind: str | None = None) -> bool:
    """这个形态能不能自己下载并装上新版本。"""
    return bool(asset_name(kind))


#: 文件名里的版本号：``4.4.0`` / ``v4.4.0``。前后不能再贴着数字 —— 免得把
#: ``x86_64`` 或 ``2026.11.13`` 这类东西切一半。
_NAME_VERSION_RE = re.compile(r"(?<![\d.])(v?)(\d+\.\d+(?:\.\d+)*)(?![\d.])")


def versioned_appimage_name(target: Path | str, version: str) -> Path | None:
    """把文件名里的版本号换成 ``version``；文件名没有版本号时返回 ``None``。

    只在**文件名已经带版本号**时改名。像 ``GomokuAI_Linux_amd_x86_64.AppImage``
    这样的固定名字保持不动 —— 版本号不进路径，桌面启动器就永远不会失效。
    """
    target = Path(target)
    version = (version or "").strip().lstrip("vV")
    if not version or target.suffix.lower() != ".appimage":
        return None
    stem = target.name[: -len(target.suffix)]
    match = _NAME_VERSION_RE.search(stem)
    if match is None:
        return None
    prefix = "v" if match.group(1) else ""
    new_stem = stem[: match.start()] + prefix + version + stem[match.end():]
    if new_stem == stem:
        return None
    return target.with_name(new_stem + target.suffix)


def _desktop_roots(old: Path, roots: Iterable[Path] | None = None) -> list[Path]:
    """可能放着启动器条目的目录；去重后返回。"""
    if roots is not None:
        candidates = [Path(item) for item in roots]
    else:
        home = Path.home()
        data_home = Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share")
        config_home = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
        candidates = [
            data_home / "applications",
            config_home / "autostart",
            home / "Desktop",
            home / "桌面",
            Path(old).parent,
        ]
    seen: set[str] = set()
    result: list[Path] = []
    for path in candidates:
        key = str(path)
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def update_desktop_entries(
    old: Path | str,
    new: Path | str,
    *,
    roots: Iterable[Path] | None = None,
) -> tuple[Path, ...]:
    """把指向 ``old`` 的启动器条目改成 ``new``，返回实际改过的文件。

    只动 ``Exec=`` / ``TryExec=`` 两行；写不进去就跳过，绝不让更新失败。
    """
    old_text, new_text = str(Path(old)), str(Path(new))
    if old_text == new_text:
        return ()
    updated: list[Path] = []
    for root in _desktop_roots(Path(old), roots):
        try:
            entries = list(root.glob("*.desktop"))
        except OSError:
            continue
        for path in entries:
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            lines = text.splitlines(keepends=True)
            changed = False
            for index, line in enumerate(lines):
                if not line.lstrip().lower().startswith(("exec=", "tryexec=")):
                    continue
                if old_text not in line:
                    continue
                replaced = line.replace(old_text, new_text)
                if replaced != line:
                    lines[index] = replaced
                    changed = True
            if not changed:
                continue
            temp = path.with_name(path.name + ".gomoku-tmp")
            try:
                temp.write_text("".join(lines), encoding="utf-8")
                os.chmod(temp, path.stat().st_mode & 0o777)
                os.replace(temp, path)
            except OSError:
                try:
                    temp.unlink()
                except OSError:
                    pass
                continue
            updated.append(path)
    return tuple(updated)


def sync_appimage_name(
    version: str | None = None,
    *,
    target: Path | str | None = None,
    roots: Iterable[Path] | None = None,
) -> Path | None:
    """让正在运行的 AppImage 文件名跟当前版本对齐（只在名字本来就带版本号时）。"""
    if target is None:
        if not getattr(sys, "frozen", False):
            return None
        value = _appimage_path()
        if not value:
            return None
        target = value
    path = Path(target)
    if not path.is_file():
        return None
    final = versioned_appimage_name(path, version or VERSION)
    if final is None or final == path:
        return None
    if final.exists():
        return None
    try:
        os.replace(path, final)
    except OSError:
        return None
    if _appimage_path() == str(path):
        os.environ["APPIMAGE"] = str(final)
    update_desktop_entries(path, final, roots=roots)
    return final


# --------------------------------------------------------------------------- #
# Release 与网络
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Asset:
    name: str
    url: str
    size: int = 0

    @property
    def size_mb(self) -> str:
        return f"{self.size / (1024 * 1024):.1f} MB" if self.size else "大小未知"


@dataclass(frozen=True)
class Release:
    tag: str
    name: str = ""
    published_at: str = ""
    notes: str = ""
    page_url: str = ""
    assets: tuple[Asset, ...] = field(default_factory=tuple)

    @property
    def version(self) -> tuple[int, ...]:
        return parse_version(self.tag)

    def asset(self, name: str) -> Asset | None:
        for item in self.assets:
            if item.name == name:
                return item
        return None


def _opener(proxy: str = ""):
    """空串走系统代理，``direct`` 强制直连，其它值当作代理地址。"""
    proxy = (proxy or "").strip()
    if proxy.lower() in ("direct", "none"):
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    if proxy:
        if "://" not in proxy:
            proxy = "http://" + proxy
        if proxy.split("://", 1)[0].lower() not in ("http", "https"):
            raise ValueError("只支持 http / https 代理")
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener()


def _headers() -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"GomokuAI/{VERSION}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _env_proxy() -> str:
    for key in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY",
                "all_proxy", "ALL_PROXY"):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return ""


def _desktop_proxy() -> str:
    """Linux 桌面环境的代理：先 gsettings（GNOME），再 kioslaverc（KDE）。"""
    try:
        mode = subprocess.run(
            ["gsettings", "get", "org.gnome.system.proxy", "mode"],
            capture_output=True, text=True, timeout=2).stdout.strip().strip("'")
        if mode == "manual":
            host = subprocess.run(
                ["gsettings", "get", "org.gnome.system.proxy.http", "host"],
                capture_output=True, text=True,
                timeout=2).stdout.strip().strip("'")
            port = subprocess.run(
                ["gsettings", "get", "org.gnome.system.proxy.http", "port"],
                capture_output=True, text=True, timeout=2).stdout.strip()
            if host:
                return f"http://{host}:{port or 8080}"
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        text = (Path.home() / ".config" / "kioslaverc").read_text(
            encoding="utf-8", errors="replace")
    except OSError:
        return ""
    section = ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            section = line
            continue
        if section != "[Proxy Settings]" or "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if key == "httpProxy" and value:
            return value
    return ""


def system_proxy() -> str:
    """没有显式配置时，这台机器在用的代理。

    **为什么不能只靠 urllib**：它在 Linux 上只读环境变量，而桌面环境把代理
    写在 gsettings / kioslaverc 里。于是同一个网络里浏览器走代理飞快、自动
    更新走直连，用户看到的是"更新比浏览器慢一个数量级"。
    """
    return _env_proxy() or (_desktop_proxy()
                            if sys.platform.startswith("linux") else "")


def _proxy_for(url: str, proxy: str) -> str:
    """这条请求该走哪个代理。

    显式配置最优先；没配置就用系统代理；都没有（或显式写了 ``direct``）就
    直连。**下载镜像与个人站点同样走代理** —— 镜像直连被限速时，更新不能
    因为"官方地址能连上"就慢慢爬。
    """
    proxy = (proxy or "").strip()
    if proxy.lower() in ("direct", "none"):
        return "direct"
    return proxy or system_proxy()


def _open(url: str, request: urllib.request.Request, proxy: str,
          timeout: float):
    """按 ``_proxy_for`` 的结论发起请求；代理连不上就退回直连一次。

    系统代理可能是陈的（换过端口、公司网走了 PAC）。那种情况下直连虽然慢，
    至少能完成；让「代理挂了」直接变成「更新失败」没有必要。
    """
    try:
        opener = _opener(_proxy_for(url, proxy))
    except ValueError as exc:
        raise UpdateError(f"代理设置无效：{exc}") from exc
    try:
        return opener.open(request, timeout=timeout)
    except urllib.error.HTTPError:
        raise
    except (urllib.error.URLError, OSError):
        if _proxy_for(url, proxy) in ("", "direct"):
            raise
        return _opener("direct").open(request, timeout=timeout)


def _read_one(url: str, proxy: str = "", timeout: float = 10.0,
              label: str = "GitHub") -> bytes:
    request = urllib.request.Request(url, headers=_headers())
    try:
        with _open(url, request, proxy, timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            hint = ("稍后再试，或在设置里填一个更新代理。"
                    if label == "GitHub" else "稍后再试。")
            raise UpdateError(f"{label} 拒绝了这次请求（可能是被限流）。{hint}") from exc
        if exc.code == 404:
            raise UpdateError(f"{label} 返回 404（文件或版本不存在）。") from exc
        raise UpdateError(f"{label} 返回 HTTP {exc.code}。") from exc
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise UpdateError(f"连不上 {label}：{reason}") from exc


def _read_any(urls: Iterable[str], proxy: str = "", timeout: float = 10.0,
              label: str = "更新源") -> bytes:
    errors: list[str] = []
    for url in urls:
        try:
            return _read_one(url, proxy, timeout, label)
        except UpdateError as exc:
            errors.append(str(exc))
    if not errors:
        raise UpdateError(f"没有可用的 {label}。")
    raise UpdateError(errors[-1] if len(errors) == 1 else "；".join(errors))


def mirror_url(tag: str, name: str) -> str:
    tag, name = str(tag or ""), str(name or "")
    if not _SAFE_TAG.match(tag) or not _SAFE_FILE.match(name):
        return ""
    return f"{MIRROR_BASE}/{tag}/{name}"


def github_url(tag: str, name: str) -> str:
    tag, name = str(tag or ""), str(name or "")
    if not _SAFE_TAG.match(tag) or not _SAFE_FILE.match(name):
        return ""
    return f"{GITHUB_DOWNLOAD}/{tag}/{name}"


def asset_urls(release: Release, asset: Asset, source: str = "auto") -> tuple[str, ...]:
    """一个资产按偏好顺序排好的下载地址。"""
    if source not in SOURCES:
        source = "auto"
    urls: list[str] = []
    if source in ("auto", "site"):
        urls.append(mirror_url(release.tag, asset.name))
    if source in ("auto", "github"):
        urls.append(asset.url or github_url(release.tag, asset.name))
    return tuple(dict.fromkeys(url for url in urls if url))


def parse_site_version(payload: bytes) -> str:
    try:
        data = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise UpdateError("个人站点返回的版本信息不是合法 JSON。") from exc
    tag = ""
    if isinstance(data, dict):
        tag = str(data.get("tag") or data.get("tag_name") or "").strip()
    if not _SAFE_TAG.match(tag):
        raise UpdateError("个人站点返回的版本号不合法。")
    return tag


def _release_from_site(tag: str) -> Release:
    """站点只提供版本号；资产名是固定的，下载地址由两个来源现拼。"""
    names = (*ASSET_APPIMAGE.values(), ASSET_WINDOWS_SETUP,
             ASSET_WINDOWS_PORTABLE, SUMS_ASSET, SIG_ASSET)
    return Release(tag=tag, page_url=SITE_PAGE,
                   assets=tuple(Asset(name=n, url="") for n in dict.fromkeys(names)))


def fetch_site_release(proxy: str = "", timeout: float = 10.0) -> Release:
    """读站点上的 ``version.json``。带按分钟取整的 cache-buster。"""
    url = f"{SITE_VERSION}?t={int(time.time() // 60)}"
    return _release_from_site(
        parse_site_version(_read_one(url, proxy, timeout, "个人站点")))


def fetch_github_release(proxy: str = "", timeout: float = 10.0) -> Release:
    payload = _read_one(API_LATEST, proxy, timeout)
    try:
        data = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise UpdateError("GitHub 返回的内容不是合法 JSON。") from exc
    if not isinstance(data, dict) or not data.get("tag_name"):
        raise UpdateError("GitHub 返回的 Release 信息不完整。")
    assets = tuple(
        Asset(name=str(item.get("name") or ""),
              url=str(item.get("browser_download_url") or ""),
              size=int(item.get("size") or 0))
        for item in data.get("assets") or ()
        if isinstance(item, dict) and item.get("name")
    )
    return Release(tag=str(data.get("tag_name") or ""),
                   name=str(data.get("name") or ""),
                   published_at=str(data.get("published_at") or ""),
                   notes=str(data.get("body") or ""),
                   page_url=str(data.get("html_url") or RELEASES_PAGE),
                   assets=assets)


def fetch_release(proxy: str = "", timeout: float = 10.0,
                  source: str = "auto") -> Release:
    """读最新版本。

    ``auto`` 先问站点，失败再回退 GitHub；站点只回一个版本号，确认有新版本后
    再从 GitHub 补一次说明与文件大小（补不到就用手上这份，下载仍可继续）。
    """
    if source not in SOURCES:
        source = "auto"
    if source in ("auto", "site"):
        try:
            release = fetch_site_release(proxy, timeout)
        except UpdateError:
            if source == "site":
                raise
        else:
            if source == "auto" and is_newer(release.tag, VERSION):
                try:
                    github = fetch_github_release(proxy, timeout)
                except UpdateError:
                    return release
                if release.version <= github.version:
                    return github
            return release
    return fetch_github_release(proxy, timeout)


def check_for_update(proxy: str = "", source: str = "auto",
                     timeout: float = 10.0) -> Release | None:
    """有新版本返回它，已经是最新返回 ``None``。网络问题抛 :class:`UpdateError`。"""
    release = fetch_release(proxy, timeout, source)
    return release if is_newer(release.tag, VERSION) else None


# --------------------------------------------------------------------------- #
# 校验和与签名
# --------------------------------------------------------------------------- #

def parse_checksums(text: str) -> dict[str, str]:
    """``sha256sum`` 的输出 → ``{文件名: 小写哈希}``。

    认 ``<hash>  <name>`` 与 ``<hash> *<name>`` 两种写法，空行与 ``#`` 注释跳过。
    """
    found: dict[str, str] = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        digest, name = parts[0].lower(), parts[1].strip().lstrip("*")
        if len(digest) == 64 and all(char in "0123456789abcdef" for char in digest):
            found[name] = digest
    return found


def verify_signature(data: bytes, signature: bytes,
                     public_key_b64: str = PUBLIC_KEY_B64) -> None:
    """验证 ``data`` 的 Ed25519 分离签名；任何问题都抛 :class:`UpdateError`。

    验签实现是本仓库自带的 :mod:`ed25519`（RFC 8032，零依赖），理由见那个模块
    的开头：本程序还要打 i386 / armv7，那两个架构上没有 ``cryptography`` 的
    wheel，不能为了验一次签名就让打包失败。
    """
    key_text = (public_key_b64 or "").strip()
    if not key_text or "REPLACE_WITH" in key_text:
        raise UpdateError("程序里没有内置更新公钥，已拒绝自动安装。")
    try:
        raw_key = base64.b64decode(key_text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise UpdateError("内置更新公钥格式不对，已拒绝自动安装。") from exc
    if len(raw_key) != 32:
        raise UpdateError("内置更新公钥长度不对，已拒绝自动安装。")
    if len(signature) != 64:
        raise UpdateError("更新签名的长度不对，已拒绝自动安装。")
    if not ed25519.verify(raw_key, data, signature):
        raise UpdateError(
            "更新签名校验失败，下载内容可能被篡改，已拒绝自动安装。"
            "请到下载页手动安装。")


def fetch_checksums(release: Release, proxy: str = "", timeout: float = 10.0,
                    source: str = "auto") -> dict[str, str]:
    """下载、验签并解析这个 Release 的 ``SHA256SUMS``。

    缺校验和、缺签名、签名不符都抛错 —— **不验签不安装**。
    """
    sums_asset = release.asset(SUMS_ASSET)
    sig_asset = release.asset(SIG_ASSET)
    missing = [name for name, item in ((SUMS_ASSET, sums_asset),
                                       (SIG_ASSET, sig_asset)) if item is None]
    if missing:
        raise UpdateError(
            f"这个 Release 没有 {' / '.join(missing)}，无法验证下载来源，"
            "已拒绝自动安装。请到下载页手动安装。")
    payload = _read_any(asset_urls(release, sums_asset, source), proxy, timeout,
                        "校验文件")
    signature = _read_any(asset_urls(release, sig_asset, source), proxy, timeout,
                          "签名文件")
    verify_signature(payload, signature)
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UpdateError(f"{SUMS_ASSET} 不是 UTF-8 文本。") from exc
    checksums = parse_checksums(text)
    if not checksums:
        raise UpdateError(f"{SUMS_ASSET} 里没有可用的校验和。")
    return checksums


# --------------------------------------------------------------------------- #
# 下载与校验
# --------------------------------------------------------------------------- #

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download_one(url: str, dest: Path,
                  on_progress: Callable[[int, int], None] | None,
                  proxy: str, timeout: float) -> None:
    request = urllib.request.Request(url, headers=_headers())
    total, done = -1, 0
    try:
        with _open(url, request, proxy, timeout) as response:
            total = int(response.headers.get("Content-Length") or -1)
            with dest.open("wb") as handle:
                while True:
                    chunk = response.read(CHUNK)
                    if not chunk:
                        break
                    handle.write(chunk)
                    done += len(chunk)
                    if on_progress is not None:
                        on_progress(done, total)
            if total >= 0 and done != total:
                raise UpdateError(f"下载不完整：应为 {total} 字节，实际 {done} 字节。")
    except urllib.error.HTTPError as exc:
        raise UpdateError(f"下载失败：HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise UpdateError(f"下载失败：{reason}") from exc
    finally:
        if on_progress is not None:
            try:
                on_progress(done, total)
            except Exception:  # noqa: BLE001 - 回调不该影响下载结果
                pass


def download(url: str | Iterable[str], dest: Path,
             on_progress: Callable[[int, int], None] | None = None,
             proxy: str = "", timeout: float = 30.0) -> None:
    """把 ``url``（或一组备用地址）流式写到 ``dest``。

    多个地址按顺序尝试，前一个失败就删掉半截文件再换下一个。
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    urls = [url] if isinstance(url, str) else [item for item in url if item]
    errors: list[str] = []
    for item in urls:
        try:
            _download_one(item, dest, on_progress, proxy, timeout)
            return
        except UpdateError as exc:
            errors.append(str(exc))
            try:
                dest.unlink()
            except OSError:
                pass
    if not urls:
        raise UpdateError("没有可用的下载地址。")
    raise UpdateError(errors[-1] if len(errors) == 1 else "；".join(errors))


def verify(path: Path, expected: str) -> None:
    actual = sha256_file(path)
    if actual.lower() != (expected or "").lower():
        raise UpdateError(
            "下载的文件校验和不匹配，已中止安装。\n"
            f"期望 {expected}\n实际 {actual}")


def prepare_update(release: Release, kind: str | None = None,
                   proxy: str = "", source: str = "auto",
                   on_progress: Callable[[int, int], None] | None = None,
                   timeout: float = 30.0) -> Path:
    """下载并校验新版本，返回**待安装的文件**（目标同目录的 ``.new``）。

    ``.part`` 与 ``.new`` 都放在目标可执行文件旁边：同目录才能保证最后那一步
    ``os.replace`` 是同一文件系统内的原子替换。
    """
    kind = kind or install_kind()
    name = asset_name(kind)
    if not name:
        raise UpdateError("当前安装方式不支持自动更新，请到下载页手动安装。")
    target = current_executable(kind)
    if target is None:
        raise UpdateError("找不到正在运行的可执行文件，无法自动更新。")

    asset = release.asset(name)
    if asset is None:
        # 站点那份 Release 是现拼的，一定有；GitHub 那份缺资产就是真缺。
        raise UpdateError(f"这个 Release 里没有 {name}。")
    checksums = fetch_checksums(release, proxy, timeout, source)
    expected = checksums.get(name)
    if not expected:
        raise UpdateError(
            f"{SUMS_ASSET} 里没有 {name} 的校验和，已拒绝自动安装。")

    part = target.with_name(target.name + ".new.part")
    final = target.with_name(target.name + ".new")
    try:
        download(asset_urls(release, asset, source), part,
                 on_progress, proxy, timeout)
        verify(part, expected)
        os.replace(part, final)
    except BaseException:
        for leftover in (part, final):
            try:
                leftover.unlink()
            except OSError:
                pass
        raise
    return final


# --------------------------------------------------------------------------- #
# 安装与重启
# --------------------------------------------------------------------------- #

def install(new: Path, target: Path, kind: str, *,
            version: str = "",
            desktop_roots: Iterable[Path] | None = None) -> Path:
    """把已经下载并校验过的 ``new`` 装上，返回新版本所在的路径。

    * ``appimage``：``os.replace`` 是原子的，运行中的文件也能换；
    * ``windows-install``：静默启动新版安装包，调用方随后退出；
    * ``windows-portable``：起一个等本进程退出后再移动文件的小脚本；
    * 其余形态直接拒绝。
    """
    new, target = Path(new), Path(target)
    if not new.is_file():
        raise UpdateError(f"找不到已下载的文件：{new}")

    if kind == "windows-install":
        _run_windows_installer(new)
        return target
    if kind == "windows-portable":
        _schedule_windows_replace(new, target)
        return target
    if kind != "appimage":
        raise UpdateError("当前安装方式不支持自动替换，请到下载页手动安装。")

    final = versioned_appimage_name(target, version)
    if final is None:
        final = target
    try:
        os.chmod(new, 0o755)
        # 先原地替换：即使后面的改名失败，旧路径上也已经是新版本，程序还能用
        os.replace(new, target)
        if final != target:
            if final.exists():
                final = target
            else:
                try:
                    os.replace(target, final)
                except OSError:
                    final = target
    except OSError as exc:
        raise UpdateError(f"替换 {target.name} 失败：{exc}") from exc

    if final != target:
        update_desktop_entries(target, final, roots=desktop_roots)
    return final


def _run_windows_installer(setup: Path) -> None:
    """静默启动 Inno 安装包。

    Inno 的安装包自带 UAC manifest，提升由它自己弹；被用户拒绝时它会以
    ``ERROR_CANCELLED (1223)`` 退出。这里稍等一下看它有没有当场退出 ——
    当场退出且非零就是被拒了，报一句"当前版本仍可继续使用"，不重试。
    """
    args = [str(setup), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"]
    flags = 0
    if sys.platform == "win32":  # pragma: no cover - 只在 Windows 生效
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        proc = subprocess.Popen(args, close_fds=True, creationflags=flags)
    except OSError as exc:
        raise UpdateError(f"启动安装程序失败：{exc}") from exc
    try:
        code = proc.wait(timeout=3.0)
    except subprocess.TimeoutExpired:
        return          # 还在跑，说明 UAC 通过了（或安装正在进行）
    if code in (1223, 5):
        raise UpdateError("安装被取消（UAC 被拒绝）。当前版本仍可继续使用。")
    if code != 0:
        raise UpdateError(
            f"安装程序提前退出（代码 {code}）。当前版本仍可继续使用。")


def clean_appimage_env(env: Mapping[str, str]) -> dict[str, str]:
    """去掉 AppImage runtime 注入的变量。"""
    cleaned = dict(env)
    for key in ("APPDIR", "APPIMAGE", "ARGV0", "OWD"):
        cleaned.pop(key, None)
    return cleaned


def restart(kind: str, target: Path) -> None:
    """启动新版本，调用方随后应退出当前进程。

    Windows 的替换脚本自己会 ``Start-Process``，这里什么都不用做。
    """
    target = Path(target)
    if kind in ("windows-install", "windows-portable"):
        return
    if kind != "appimage":
        raise UpdateError("当前安装方式不支持自动重启。")
    env = clean_appimage_env(os.environ)
    # 稍等一会儿再 exec：给当前进程留出退出、释放单实例锁和窗口的时间
    subprocess.Popen(
        ["sh", "-c", 'sleep 0.8; exec "$@"', "sh", str(target)],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _powershell_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def windows_replace_script(new: Path, target: Path, pid: int | None = None) -> str:
    """生成等待当前进程退出、替换文件、再启动新版本的 PowerShell 脚本。"""
    pid = int(pid if pid is not None else os.getpid())
    target_q = _powershell_quote(str(target))
    new_q = _powershell_quote(str(new))
    return f"""$ErrorActionPreference = 'SilentlyContinue'
Wait-Process -Id {pid} -Timeout 60
for ($i = 0; $i -lt 60; $i++) {{
    try {{
        Move-Item -LiteralPath {new_q} -Destination {target_q} -Force -ErrorAction Stop
        break
    }} catch {{
        Start-Sleep -Milliseconds 250
    }}
}}
if (Test-Path -LiteralPath {target_q}) {{
    Start-Process -FilePath {target_q}
}}
Remove-Item -LiteralPath $MyInvocation.MyCommand.Path -Force
"""


def _schedule_windows_replace(new: Path, target: Path) -> None:
    """把替换脚本写到临时目录并脱离当前进程启动。"""
    script = Path(tempfile.gettempdir()) / f"gomoku-update-{os.getpid()}.ps1"
    try:
        script.write_text(
            windows_replace_script(new, target, os.getpid()),
            encoding="utf-8-sig")
    except OSError as exc:
        raise UpdateError(f"写更新脚本失败：{exc}") from exc

    flags = 0
    if sys.platform == "win32":  # pragma: no cover - 只在 Windows 生效
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
            subprocess, "DETACHED_PROCESS", 0)
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle",
             "Hidden", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True)
    except OSError as exc:
        try:
            script.unlink()
        except OSError:
            pass
        raise UpdateError(f"启动更新脚本失败：{exc}") from exc


# --------------------------------------------------------------------------- #
# 自检（不联网、不改任何真实文件）
# --------------------------------------------------------------------------- #

def _selftest() -> int:
    failures: list[str] = []

    def check(name: str, got: object, want: object) -> None:
        if got != want:
            failures.append(f"{name}：期望 {want!r}，实际 {got!r}")

    # ---- 版本
    check("v 前缀", parse_version("v1.3.0"), (1, 3, 0))
    check("不带 v", parse_version("1.3.0"), (1, 3, 0))
    check("预发布后缀忽略", parse_version("1.3.0-beta.2"), (1, 3, 0))
    check("构建元数据忽略", parse_version("1.3.0+20261017"), (1, 3, 0))
    check("空版本", parse_version(""), ())
    check("非法版本", parse_version("nightly"), ())
    check("新版本更大", is_newer("v1.3.0", "1.2.0"), True)
    check("同版本不新", is_newer("1.3.0", "v1.3.0"), False)
    check("缺位按 0 补齐", is_newer("1.3", "1.3.0"), False)
    check("位数少但数值大", is_newer("1.4", "1.3.9"), True)

    # ---- 架构与资产
    check("x86_64", machine_stem("x86_64"), "amd_x86_64")
    check("AMD64", machine_stem("AMD64"), "amd_x86_64")
    check("aarch64", machine_stem("aarch64"), "aarch64")
    check("armv7 不产 AppImage", machine_stem("armv7l"), "")
    check("Windows 安装版资产", asset_name("windows-install"), ASSET_WINDOWS_SETUP)
    check("Windows 便携资产", asset_name("windows-portable"), ASSET_WINDOWS_PORTABLE)
    check("AppImage 资产", asset_name("appimage", "x86_64"), ASSET_APPIMAGE["amd_x86_64"])
    check("deb 不自更新", asset_name("deb"), "")
    check("MSIX 不自更新", asset_name("msix"), "")

    # ---- 安装形态
    check("源码运行", install_kind(system="linux", frozen=False, appimage="",
                                 channel="source"), "source")
    check("MSIX 渠道", install_kind(system="win32", frozen=True, channel="msix"),
          "msix")
    check("Linux 冻结但没有 APPIMAGE（deb）",
          install_kind(system="linux", frozen=True, appimage="", channel="github"),
          "deb")
    with tempfile.TemporaryDirectory(prefix="gomoku-updater-") as tmp:
        appimage = Path(tmp) / "GomokuAI.AppImage"
        appimage.write_bytes(b"x")
        check("AppImage", install_kind(system="linux", frozen=True,
                                       appimage=str(appimage), channel="github"),
              "appimage")
        exe = Path(tmp) / "GomokuAI.exe"
        exe.write_bytes(b"x")
        check("Windows 便携", install_kind(system="win32", frozen=True,
                                           executable=exe, channel="github"),
              "windows-portable")
        (Path(tmp) / "unins000.exe").write_bytes(b"x")
        check("Windows 安装版", install_kind(system="win32", frozen=True,
                                             executable=exe, channel="github"),
              "windows-install")

    # ---- AppImage 文件名里的版本
    check("名称版本替换",
          versioned_appimage_name(Path("/x/GomokuAI-4.3.0-x86_64.AppImage"), "4.4.0"),
          Path("/x/GomokuAI-4.4.0-x86_64.AppImage"))
    check("保留 v 前缀",
          versioned_appimage_name(Path("/x/App-v1.2.AppImage"), "4.4.0"),
          Path("/x/App-v4.4.0.AppImage"))
    check("固定文件名不动",
          versioned_appimage_name(Path("/x/GomokuAI_Linux_amd_x86_64.AppImage"),
                                  "4.4.0"), None)

    with tempfile.TemporaryDirectory() as tmp:
        old = Path(tmp) / "GomokuAI-4.3.0-x86_64.AppImage"
        old.write_bytes(b"new")
        target = Path(tmp) / "GomokuAI-4.4.0-x86_64.AppImage"
        check("改名成功", sync_appimage_name("4.4.0", target=old, roots=[]), target)
        check("旧名不再存在", old.exists(), False)
        check("新名内容不变", target.read_bytes(), b"new")

    with tempfile.TemporaryDirectory() as tmp:
        downloaded = Path(tmp) / "download.AppImage"
        downloaded.write_bytes(b"v440")
        running = Path(tmp) / "GomokuAI-4.3.9-x86_64.AppImage"
        running.write_bytes(b"old")
        want = Path(tmp) / "GomokuAI-4.4.0-x86_64.AppImage"
        check("安装后改名",
              install(downloaded, running, "appimage", version="4.4.0",
                      desktop_roots=[]), want)
        check("新内容在最终文件里", want.read_bytes(), b"v440")

    # ---- 代理
    #: 镜像与 GitHub 走同一条策略：显式配置最优先，其次系统代理。
    check("显式代理覆盖下载镜像",
          _proxy_for("https://dl.linxuhan.top/GomokuAI/v1/x.exe", "127.0.0.1:7897"),
          "127.0.0.1:7897")
    check("显式直连镜像",
          _proxy_for("https://dl.linxuhan.top/GomokuAI/v1/x.exe", "direct"),
          "direct")
    check("显式代理覆盖 GitHub",
          _proxy_for("https://github.com/a/b", "socks5://127.0.0.1:1"),
          "socks5://127.0.0.1:1")

    #: 自动源先镜像、再 GitHub；镜像和 GitHub 共用同一个代理结论。
    probe = Release(tag="v1.2.3",
                    assets=(Asset(name="X.exe", url="https://github.com/a/b/X.exe"),))
    urls = asset_urls(probe, probe.assets[0], "auto")
    check("auto 先镜像", urls[0], f"{MIRROR_BASE}/v1.2.3/X.exe")
    check("auto 有 GitHub 回退", urls[-1], "https://github.com/a/b/X.exe")
    check("github 源只留 GitHub", len(asset_urls(probe, probe.assets[0], "github")), 1)

    # ---- 校验和解析
    text = (
        "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
        "  a.AppImage\n"
        "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"
        " *b.exe\n"
        "# 注释\n\n坏的 行\n"
    )
    sums = parse_checksums(text)
    check("解析两个校验和", len(sums), 2)
    check("二进制模式星号被去掉", "b.exe" in sums, True)

    # ---- Ed25519（现场生成测试密钥，不联网、不碰真实私钥）
    seed = bytes(range(32))
    key = base64.b64encode(ed25519.public_key(seed)).decode("ascii")
    payload = b"0123  file\n"
    signature = ed25519.sign(seed, payload)

    def rejects(name: str, call) -> None:
        try:
            call()
        except UpdateError:
            check(name, True, True)
        else:
            check(name, False, True)

    try:
        verify_signature(payload, signature, key)
    except UpdateError as exc:
        failures.append(f"正确签名应通过：{exc}")
    rejects("篡改内容必须失败",
            lambda: verify_signature(payload + b"x", signature, key))
    flipped = bytes([signature[0] ^ 0xFF]) + signature[1:]
    rejects("篡改签名必须失败",
            lambda: verify_signature(payload, flipped, key))
    other = base64.b64encode(ed25519.public_key(bytes(range(1, 33)))).decode("ascii")
    rejects("换公钥必须失败",
            lambda: verify_signature(payload, signature, other))
    rejects("没有公钥必须失败",
            lambda: verify_signature(payload, signature, ""))

    # ---- Windows 替换脚本内容
    script = windows_replace_script(Path("C:/new.exe"), Path("C:/old.exe"), 4242)
    check("脚本等进程退出", "Wait-Process -Id 4242" in script, True)
    check("脚本会启动新版本", "Start-Process" in script, True)
    check("脚本自删", "Remove-Item" in script, True)

    # ---- 站点 JSON
    check("站点 tag", parse_site_version(b'{"tag": "v4.4.0"}'), "v4.4.0")
    try:
        parse_site_version(b"<html>")
    except UpdateError:
        check("站点非 JSON 被拒", True, True)
    else:
        check("站点非 JSON 被拒", False, True)

    if failures:
        print("更新器自检失败：")
        for line in failures:
            print(f"  ✗ {line}")
        return 1
    print("更新器自检通过（版本 / 资产 / 形态 / 校验和 / 签名 / 替换脚本）")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线给 Release 的 ``SHA256SUMS`` 签名，验签通过后才把草稿转正。

为什么签名不在 CI 里做：CI 的 ``GITHUB_TOKEN`` 由平台签发，任何能改 workflow
的人都能拿到；把签名私钥放进去，等于让「能改仓库」和「能发更新」变成同一个
权限。这里刻意把两者分开：

1. 推 tag，CI 构建各平台产物、算出 ``SHA256SUMS``，创建**草稿** Release；
2. 在本机（离线私钥所在处）运行 ``sign <tag> --publish``：
   下载 ``SHA256SUMS`` → 签名 → 上传 ``SHA256SUMS.sig`` → 用程序内置公钥
   复核 → 草稿转正；
3. 客户端只在签名验证通过时才自动安装。

草稿不会被 ``releases/latest`` 返回，所以签名完成前用户看不到这个版本。

用法::

    # 首次：生成密钥（0600），把打印出的公钥填进 GomokuAI/updater.py
    python scripts/release_sign.py keygen

    # 发版：推 tag、等草稿资产齐全，然后
    python scripts/release_sign.py sign v4.4.0 --publish

    # 独立复核（例如从 Release 页面下载两个文件后）
    python scripts/release_sign.py verify SHA256SUMS SHA256SUMS.sig

**不依赖 cryptography**：签名与验签都走仓库自带的 ``GomokuAI/ed25519.py``。
私钥只用 32 字节种子，按 PKCS#8 PEM 存放（与 openssl / cryptography 互通），
这里手写那段 DER —— 它只有一个固定的前缀，不值得为它引一个装着 Rust 的依赖。
"""

from __future__ import annotations

import argparse
import base64
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import NoReturn

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ed25519  # noqa: E402
import updater  # noqa: E402

#: 默认私钥位置。目录 0700、文件 0600，不跟随仓库、不进 CI。
DEFAULT_KEY = Path.home() / ".config" / "gomoku-ai-release" / "signing-key.pem"

#: Ed25519 的 PKCS#8 DER 前缀（RFC 8410）：``SEQUENCE { INTEGER 0, OID 1.3.101.112,
#: OCTET STRING { OCTET STRING <32 字节种子> } }``。前缀之后就是那 32 字节。
_PKCS8_PREFIX = bytes.fromhex("302e020100300506032b657004220420")


def _fail(message: str) -> NoReturn:
    print(f"错误：{message}", file=sys.stderr)
    raise SystemExit(1)


def _seed_from_pem(data: bytes) -> bytes:
    """读 PKCS#8 PEM，取出 32 字节种子。只认未加密的 Ed25519。"""
    text = data.decode("ascii", "replace")
    if "ENCRYPTED" in text:
        _fail("不支持带口令的私钥；本工具的私钥必须是未加密的 Ed25519")
    lines = [line.strip() for line in text.splitlines()
             if line.strip() and not line.startswith("-----")]
    try:
        der = base64.b64decode("".join(lines), validate=True)
    except Exception as exc:  # noqa: BLE001
        _fail(f"私钥不是合法的 PEM：{exc}")
    if not der.startswith(_PKCS8_PREFIX) or len(der) != len(_PKCS8_PREFIX) + 32:
        _fail("私钥不是未加密的 Ed25519 PKCS#8（OID 1.3.101.112）")
    return der[len(_PKCS8_PREFIX):]


def _pem_from_seed(seed: bytes) -> bytes:
    der = _PKCS8_PREFIX + seed
    body = base64.b64encode(der).decode("ascii")
    lines = [body[index:index + 64] for index in range(0, len(body), 64)]
    return ("-----BEGIN PRIVATE KEY-----\n" + "\n".join(lines)
            + "\n-----END PRIVATE KEY-----\n").encode("ascii")


def _public_b64(seed: bytes) -> str:
    return base64.b64encode(ed25519.public_key(seed)).decode("ascii")


def _warn_loose_permissions(path: Path) -> None:
    if os.name != "posix":
        return
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        return
    if mode & 0o077:
        print(f"警告：{path} 的权限是 {mode:03o}，同组或其他用户可读。"
              f"建议 chmod 600 {path}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #

def cmd_keygen(args: argparse.Namespace) -> int:
    path = Path(args.key).expanduser()
    if path.exists():
        _fail(f"{path} 已存在；换钥请先手工备份并移走它，避免覆盖后旧版本无法更新")

    parent = path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            os.chmod(parent, 0o700)
    except OSError as exc:
        _fail(f"建不了私钥目录 {parent}：{exc}")

    seed = secrets.token_bytes(32)
    # O_EXCL：即使前面刚检查过，也不给「并发/竞态覆盖已有私钥」留窗口
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_pem_from_seed(seed))
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == "posix":
            os.chmod(path, 0o600)
    except FileExistsError:
        _fail(f"{path} 已存在，未覆盖")
    except OSError as exc:
        _fail(f"写不了私钥 {path}：{exc}")

    print(f"私钥已生成：{path}（权限 0600，请离线备份，丢失后无法再为旧版本签名）")
    print("把下面这行填进 GomokuAI/updater.py 的 PUBLIC_KEY_B64：")
    print()
    print(f'PUBLIC_KEY_B64 = "{_public_b64(seed)}"')
    return 0


def _gh(args: list[str], *, capture: bool = False) -> subprocess.CompletedProcess:
    if shutil.which("gh") is None:
        _fail("找不到 gh；请先安装 GitHub CLI 并 gh auth login")
    result = subprocess.run(["gh", *args], check=False, text=True,
                            capture_output=capture)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        _fail(f"gh {' '.join(args)} 失败（退出码 {result.returncode}）"
              + (f"：\n{detail}" if detail else ""))
    return result


def cmd_sign(args: argparse.Namespace) -> int:
    key_path = Path(args.key).expanduser()
    try:
        seed = _seed_from_pem(key_path.read_bytes())
    except OSError as exc:
        _fail(f"读不到私钥 {key_path}：{exc}")
    _warn_loose_permissions(key_path)

    if _public_b64(seed) != (updater.PUBLIC_KEY_B64 or "").strip():
        _fail(
            "本机私钥与 GomokuAI/updater.py 里的 PUBLIC_KEY_B64 不一致。\n"
            "先用 keygen 打印公钥并更新常量、提交新版本，否则签出来的包"
            "客户端一律拒绝安装。")

    tag = args.tag.strip()
    if not tag:
        _fail("tag 不能为空")

    with tempfile.TemporaryDirectory(prefix="gomoku-release-sign-") as tmp:
        work = Path(tmp)
        print(f"下载 {tag} 的 {updater.SUMS_ASSET} …")
        _gh(["release", "download", tag, "--repo", updater.REPO,
             "--pattern", updater.SUMS_ASSET, "--dir", str(work)])
        sums_path = work / updater.SUMS_ASSET
        if not sums_path.is_file():
            _fail(f"下载后没有找到 {sums_path}")

        data = sums_path.read_bytes()
        signature = ed25519.sign(seed, data)
        sig_path = work / updater.SIG_ASSET
        sig_path.write_bytes(signature)

        # 上传前先用**程序内置公钥**复核一遍，避免把错签名传上去
        updater.verify_signature(data, signature)
        print(f"本地验签通过：{updater.SUMS_ASSET}（{len(data)} 字节）")

        print(f"上传 {updater.SIG_ASSET} …")
        _gh(["release", "upload", tag, str(sig_path),
             "--repo", updater.REPO, "--clobber"])

    if args.publish:
        print(f"把 {tag} 从草稿转为正式 Release …")
        _gh(["release", "edit", tag, "--repo", updater.REPO, "--draft=false"])
        print("已发布。releases/latest 现在能看到它。")
    else:
        print("签名已上传，Release 仍是草稿。确认无误后加 --publish 转正。")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    sums_path = Path(args.sums).expanduser()
    sig_path = Path(args.signature).expanduser()
    try:
        data = sums_path.read_bytes()
        signature = sig_path.read_bytes()
    except OSError as exc:
        _fail(f"读文件失败：{exc}")
    try:
        updater.verify_signature(data, signature)
    except updater.UpdateError as exc:
        _fail(str(exc))
    print(f"验签通过：{sums_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="GomokuAI 离线发布签名（私钥只应留在本机）")
    sub = parser.add_subparsers(dest="command", required=True)

    keygen = sub.add_parser("keygen", help="生成 Ed25519 私钥并打印公钥常量")
    keygen.add_argument("--key", default=str(DEFAULT_KEY), help="私钥路径")
    keygen.set_defaults(func=cmd_keygen)

    sign = sub.add_parser("sign", help="给草稿 Release 的 SHA256SUMS 签名并上传")
    sign.add_argument("tag", help="形如 v4.4.0")
    sign.add_argument("--key", default=str(DEFAULT_KEY), help="私钥路径")
    sign.add_argument("--publish", action="store_true",
                      help="验签通过后把草稿 Release 转为正式")
    sign.set_defaults(func=cmd_sign)

    verify = sub.add_parser("verify", help="用程序内置公钥独立复核签名")
    verify.add_argument("sums", help="SHA256SUMS 文件")
    verify.add_argument("signature", help="SHA256SUMS.sig 文件")
    verify.set_defaults(func=cmd_verify)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

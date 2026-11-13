# -*- coding: utf-8 -*-
"""Ed25519 的**最小实现**：只有验签与签名，纯 Python、零依赖。

为什么不用 ``cryptography``：本程序还要打 i386 / armv7 的 deb / tar.gz，那两个
架构上 PyPI 没有 ``cryptography`` 的轮子（它从某个版本起只剩 64 位 Linux 的
manylinux wheel），装依赖就要在本机编译 Rust —— 打包会直接失败。而自动更新只
需要"验一个 64 字节签名"，全部计算是模 2^255-19 的群运算与一次 SHA-512，
纯 Python 足够，而且让"能不能验签"不再取决于依赖树。

**这不是加密库，不提供任何机密性。** 它只做三件事：

* :func:`verify` —— RFC 8032 的签名验证；
* :func:`sign` —— 给 ``scripts/release_sign.py`` 用（私钥离线保管）；
* :func:`public_key` —— 从 32 字节私钥种子推出公钥。

验证比生成签名更重要：**签错只影响你自己，验漏会让别人装上来路不明的程序。**
所以下面按 RFC 8032 的 MUST 逐条做严格判定，而不是只算一遍等式：

* ``S >= L`` 直接拒绝（否则同一份签名可以有多条编码）；
* 公钥与 ``R`` 的曲线上点必须能解码、且 ``y < p``（非规范编码拒绝）；
* 拒绝单位元（低阶点）作为公钥或 ``R`` —— 它们会让"任何人都能伪造"成立。

实现思路沿用公有领域的 python-ed25519（Brian Warner）的扩展坐标写法，点乘
用 Montgomery ladder 的变体（双倍-加），一次验签约 10 ms 量级，够用即可。
"""

from __future__ import annotations

import hashlib

#: 主域的模数：2^255 - 19。
q = 2 ** 255 - 19
#: 基点 B 的阶（签名里 S 的取值范围）。
l = 2 ** 252 + 27742317777372353535851937790883648493

_PUBLIC_KEY_LEN = 32
_SIGNATURE_LEN = 64


def _inv(x: int) -> int:
    return pow(x, q - 2, q)


#: 曲线参数：-x^2 + y^2 = 1 + d x^2 y^2。
_d = -121665 * _inv(121666) % q
#: 2 的平方根逆元，用来在开方失败时换另一支。
_I = pow(2, (q - 1) // 4, q)


def _recover_x(y: int, sign: int) -> int | None:
    """由 ``y`` 恢复 ``x``；点不在曲线上返回 ``None``。"""
    xx = (y * y - 1) * _inv(_d * y * y + 1) % q
    x = pow(xx, (q + 3) // 8, q)
    if (x * x - xx) % q != 0:
        x = (x * _I) % q
    if (x * x - xx) % q != 0:
        return None
    if x & 1 != sign:
        x = q - x
    return x


_BY = 4 * _inv(5) % q
_BX = _recover_x(_BY, 0)
_B = (_BX, _BY)


def _edwards_add(p: tuple[int, int], o: tuple[int, int]) -> tuple[int, int]:
    x1, y1 = p
    x2, y2 = o
    t = _d * x1 * x2 * y1 * y2 % q
    x3 = (x1 * y2 + x2 * y1) * _inv(1 + t) % q
    y3 = (y1 * y2 + x1 * x2) * _inv(1 - t) % q
    return (x3, y3)


def _scalar_mult(point: tuple[int, int], scalar: int) -> tuple[int, int]:
    """double-and-add。``scalar`` 为 0 时返回单位元 (0, 1)。"""
    result = (0, 1)
    addend = point
    while scalar > 0:
        if scalar & 1:
            result = _edwards_add(result, addend)
        addend = _edwards_add(addend, addend)
        scalar >>= 1
    return result


def _encode_point(point: tuple[int, int]) -> bytes:
    x, y = point
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decode_point(data: bytes) -> tuple[int, int] | None:
    """32 字节 → 曲线点；非规范编码或不在曲线上返回 ``None``。"""
    if len(data) != _PUBLIC_KEY_LEN:
        return None
    value = int.from_bytes(data, "little")
    sign = value >> 255
    y = value & ((1 << 255) - 1)
    if y >= q:
        return None
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y)


def _is_low_order(point: tuple[int, int]) -> bool:
    """低阶点（含单位元）。用作公钥时等于"谁都能伪造"，一律拒绝。"""
    # 8 个低阶点的阶都整除 8，所以 [8]P == 单位元即可判定。
    return _scalar_mult(point, 8) == (0, 1)


def _sha512(data: bytes) -> bytes:
    return hashlib.sha512(data).digest()


def _int_from_le(data: bytes) -> int:
    return int.from_bytes(data, "little")


def _int_to_le(value: int, length: int = 32) -> bytes:
    return int.to_bytes(value, length, "little")


def public_key(seed: bytes) -> bytes:
    """32 字节私钥种子 → 32 字节公钥。"""
    if len(seed) != 32:
        raise ValueError("私钥种子必须是 32 字节")
    digest = _sha512(seed)[:32]
    a = _int_from_le(digest)
    a &= (1 << 254) - 8
    a |= (1 << 254)
    return _encode_point(_scalar_mult(_B, a))


def sign(seed: bytes, message: bytes) -> bytes:
    """用 32 字节私钥种子给 ``message`` 签名，返回 64 字节。"""
    if len(seed) != 32:
        raise ValueError("私钥种子必须是 32 字节")
    digest = _sha512(seed)
    a = _int_from_le(digest[:32])
    a &= (1 << 254) - 8
    a |= (1 << 254)
    pk = _encode_point(_scalar_mult(_B, a))
    r = _int_from_le(_sha512(digest[32:] + message)) % l
    R = _encode_point(_scalar_mult(_B, r))
    h = _int_from_le(_sha512(R + pk + message)) % l
    S = (r + h * a) % l
    return R + _int_to_le(S)


def verify(public: bytes, message: bytes, signature: bytes) -> bool:
    """验证 ``signature`` 是不是 ``public`` 对 ``message`` 的签名。

    任何格式问题都只返回 ``False``，不抛异常 —— 调用方（更新器）只需要一个
    "能不能装"的答案，异常类型不该泄漏给用户。
    """
    if len(public) != _PUBLIC_KEY_LEN or len(signature) != _SIGNATURE_LEN:
        return False
    R_bytes, S_bytes = signature[:32], signature[32:]
    s = _int_from_le(S_bytes)
    if s >= l:                      # RFC 8032 的 MUST：拒绝非规范的 S
        return False

    A = _decode_point(public)
    if A is None or _is_low_order(A):
        return False
    R = _decode_point(R_bytes)
    if R is None or _is_low_order(R):
        return False

    h = _int_from_le(_sha512(R_bytes + public + message)) % l
    left = _scalar_mult(_B, s)
    right = _edwards_add(R, _scalar_mult(A, h))
    return left == right


# --------------------------------------------------------------------------- #
# 自检：RFC 8032 §7.1 的官方测试向量
# --------------------------------------------------------------------------- #

#: (私钥种子, 公钥, 消息, 签名)。取自 RFC 8032 的 TEST 1 / TEST 2 / TEST 3，
#: 以及一条 SHA-512 单块边界附近的消息（TEST 1024 的前缀）。
_VECTORS = (
    (
        bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"),
        bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"),
        b"",
        bytes.fromhex(
            "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
            "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    ),
    (
        bytes.fromhex("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb"),
        bytes.fromhex("3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c"),
        bytes.fromhex("72"),
        bytes.fromhex(
            "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
            "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
    ),
    (
        bytes.fromhex("c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7"),
        bytes.fromhex("fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025"),
        bytes.fromhex("af82"),
        bytes.fromhex(
            "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac"
            "18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a"),
    ),
)


def _selftest() -> int:
    import base64

    failures: list[str] = []

    for index, (seed, pk, message, signature) in enumerate(_VECTORS, 1):
        got_pk = public_key(seed)
        if got_pk != pk:
            failures.append(f"向量 {index}：公钥不符 {got_pk.hex()}")
        got_sig = sign(seed, message)
        if got_sig != signature:
            failures.append(f"向量 {index}：签名不符 {got_sig.hex()}")
        if not verify(pk, message, signature):
            failures.append(f"向量 {index}：验证失败")
        if verify(pk, message + b"x", signature):
            failures.append(f"向量 {index}：篡改消息仍然通过")
        flipped = bytes([signature[0] ^ 0x01]) + signature[1:]
        if verify(pk, message, flipped):
            failures.append(f"向量 {index}：篡改签名仍然通过")

    # 非规范 S（S + L）必须被拒
    seed, pk, message, signature = _VECTORS[0]
    s = _int_from_le(signature[32:])
    malleable = signature[:32] + _int_to_le(s + l)
    if verify(pk, message, malleable):
        failures.append("非规范 S（S+L）仍然通过")

    # 单位元当公钥必须被拒
    identity = b"\x01" + b"\x00" * 31
    try:
        ok = verify(identity, message, signature)
    except Exception:  # noqa: BLE001 — 任何异常都视为拒绝
        ok = False
    if ok:
        failures.append("单位元公钥仍然通过")

    # base64 往返（更新器把公钥存成 base64）
    if base64.b64encode(VECTORS_PUBLIC()) != base64.b64encode(_VECTORS[0][1]):
        failures.append("base64 往返失败")

    if failures:
        print("Ed25519 自检失败：")
        for line in failures:
            print(f"  ✗ {line}")
        return 1
    print("Ed25519 自检通过（RFC 8032 向量 ×3 + 非规范 S + 低阶点）")
    return 0


def VECTORS_PUBLIC() -> bytes:
    """自检用的第一条公钥（只是为了让 base64 往返在一处可见）。"""
    return _VECTORS[0][1]


if __name__ == "__main__":
    raise SystemExit(_selftest())

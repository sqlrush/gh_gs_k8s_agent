"""国密 SM3 杂凑(GB/T 32905-2016),纯 Python。

只给 SM2 签名算摘要用:一次请求签一次,几百字节,速度无所谓。
不引第三方包——交付镜像的 Python 依赖是客户白名单管的。
"""
from __future__ import annotations

import struct
from typing import List

_IV = [
    0x7380166F, 0x4914B2B9, 0x172442D7, 0xDA8A0600,
    0xA96F30BC, 0x163138AA, 0xE38DEE4D, 0xB0FB0E4E,
]
_MASK = 0xFFFFFFFF


def _rotl(x: int, n: int) -> int:
    n %= 32
    return ((x << n) | (x >> (32 - n))) & _MASK


def _p0(x: int) -> int:
    return x ^ _rotl(x, 9) ^ _rotl(x, 17)


def _p1(x: int) -> int:
    return x ^ _rotl(x, 15) ^ _rotl(x, 23)


def _t(j: int) -> int:
    return 0x79CC4519 if j < 16 else 0x7A879D8A


def _ff(x: int, y: int, z: int, j: int) -> int:
    return (x ^ y ^ z) if j < 16 else ((x & y) | (x & z) | (y & z))


def _gg(x: int, y: int, z: int, j: int) -> int:
    return (x ^ y ^ z) if j < 16 else ((x & y) | (~x & z & _MASK))


def _compress(v: List[int], block: bytes) -> List[int]:
    w = list(struct.unpack(">16I", block))
    for j in range(16, 68):
        w.append(_p1(w[j - 16] ^ w[j - 9] ^ _rotl(w[j - 3], 15)) ^ _rotl(w[j - 13], 7) ^ w[j - 6])
    w1 = [w[j] ^ w[j + 4] for j in range(64)]
    a, b, c, d, e, f, g, h = v
    for j in range(64):
        ss1 = _rotl((_rotl(a, 12) + e + _rotl(_t(j), j)) & _MASK, 7)
        ss2 = ss1 ^ _rotl(a, 12)
        tt1 = (_ff(a, b, c, j) + d + ss2 + w1[j]) & _MASK
        tt2 = (_gg(e, f, g, j) + h + ss1 + w[j]) & _MASK
        d, c, b, a = c, _rotl(b, 9), a, tt1
        h, g, f, e = g, _rotl(f, 19), e, _p0(tt2)
    return [x ^ y for x, y in zip(v, (a, b, c, d, e, f, g, h))]


def sm3_digest(data: bytes) -> bytes:
    """32 字节摘要。"""
    bit_len = len(data) * 8
    padded = data + b"\x80" + b"\x00" * ((55 - len(data)) % 64) + struct.pack(">Q", bit_len)
    v = list(_IV)
    for i in range(0, len(padded), 64):
        v = _compress(v, padded[i:i + 64])
    return struct.pack(">8I", *v)


def sm3_hex(data: bytes) -> str:
    return sm3_digest(data).hex()

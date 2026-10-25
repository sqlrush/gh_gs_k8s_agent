"""common.grmp.sm3 —— 国密 SM3 杂凑,纯 Python,给 SM2 签名用。

向量来自 GB/T 32905-2016 附录 A;再拿 gmssl(有装才跑)做随机输入的对照。
"""
import os
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common.grmp import sm3  # noqa: E402


def test_sm3_standard_vector_abc():
    assert sm3.sm3_hex(b"abc") == "66c7f0f462eeedd9d1f2d46bdc10e4e24167c4875cf2f7a2297da02b8f4ba8e0"


def test_sm3_standard_vector_two_blocks():
    assert sm3.sm3_hex(b"abcd" * 16) == "debe9ff92275b8a138604889c18e5a4d6fdb70e5387e5765293dcba39c0c5732"


def test_sm3_returns_32_bytes_and_hex_matches():
    digest = sm3.sm3_digest(b"")
    assert len(digest) == 32 and digest.hex() == sm3.sm3_hex(b"")


@pytest.mark.parametrize("size", [0, 1, 55, 56, 63, 64, 65, 119, 120, 1000, 4097])
def test_sm3_matches_gmssl_on_padding_boundaries(size):
    gm = pytest.importorskip("gmssl.sm3")
    from gmssl import func as gmfunc
    data = os.urandom(size)
    assert sm3.sm3_hex(data) == gm.sm3_hash(gmfunc.bytes_to_list(data))

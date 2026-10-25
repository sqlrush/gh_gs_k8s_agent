"""common.grmp.sm2 —— 国密 SM2 数字签名(GB/T 32918.2-2016),纯 Python。

三层对照:①标准附录 A 的固定 k 向量;②与 gmssl 互签互验(有装才跑);
③OpenSSL 3(brew openssl@3)生成的 PKCS#8 私钥与 DER 签名,我们能解析、能验——这是 Java 侧最可能的形态。
"""
import base64
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common.grmp import sm2  # noqa: E402

# GB/T 32918.2-2016 附录 A.2(GM/T 0003.5)的签名示例:消息 "message digest",ID "1234567812345678"
_STD_D = 0x3945208F7B2144B13F36E38AC6D39F95889393692860B51A42FB81EF4DF7C5B8
_STD_PX = 0x09F9DF311E5421A150DD7D161E4BC5C672179FAD1833FC076BB08FF356F35020
_STD_PY = 0xCCEA490CE26775A52DC6EA718CC1AA600AED05FBF35E084A6632F6072DA9AD13
_STD_K = 0x59276E27D506861A16680F3AD9C02DCCEF3CC1FA3CDBE4CE6D54B80DEAC1BC21
_STD_R = 0xF5A03B0648D2C4630EEAC513E1BB81A15944DA3827D5B74143AC7EACEEE720B3
_STD_S = 0xB1B6AA29DF212FD8763182BC0D421CA1BB9038FD1F7F42D4840B69C485BBC1AA
_STD_MSG = b"message digest"
_STD_ID = b"1234567812345678"


def test_public_key_derived_from_standard_private_key():
    assert sm2.public_key(_STD_D) == (_STD_PX, _STD_PY)


def test_standard_vector_with_fixed_k():
    r, s = sm2.sign(_STD_D, _STD_MSG, _STD_ID, k=_STD_K)
    assert (r, s) == (_STD_R, _STD_S)
    assert sm2.verify((_STD_PX, _STD_PY), _STD_MSG, _STD_ID, r, s)


def test_sign_verify_roundtrip_and_tamper_detection():
    d = sm2.generate_private_key()
    pub = sm2.public_key(d)
    r, s = sm2.sign(d, b"/v1/x+1726650000000", _STD_ID)
    assert sm2.verify(pub, b"/v1/x+1726650000000", _STD_ID, r, s)
    assert not sm2.verify(pub, b"/v1/x+1726650000001", _STD_ID, r, s)
    assert not sm2.verify(pub, b"/v1/x+1726650000000", b"other-id", r, s)


def test_raw_and_der_encodings_roundtrip():
    r, s = _STD_R, _STD_S
    raw = sm2.encode_signature(r, s, "raw")
    assert len(raw) == 64 and sm2.decode_signature(raw, "raw") == (r, s)
    der = sm2.encode_signature(r, s, "der")
    assert der[0] == 0x30 and sm2.decode_signature(der, "der") == (r, s)
    # 高位为 1 的整数在 DER 里要补 00 前导,解析回来不能多出来
    r2, s2 = 0x80 << 248, 0x7F << 248
    assert sm2.decode_signature(sm2.encode_signature(r2, s2, "der"), "der") == (r2, s2)


def test_parse_private_key_accepts_hex_and_rejects_garbage():
    assert sm2.parse_private_key("%064X" % _STD_D) == _STD_D
    assert sm2.parse_private_key("0x%064x" % _STD_D) == _STD_D
    with pytest.raises(sm2.Sm2Error):
        sm2.parse_private_key("not a key")
    with pytest.raises(sm2.Sm2Error):
        sm2.parse_private_key("%064X" % 0)


def test_parse_public_key_hex_forms():
    hex_xy = "%064X%064X" % (_STD_PX, _STD_PY)
    assert sm2.parse_public_key(hex_xy) == (_STD_PX, _STD_PY)
    assert sm2.parse_public_key("04" + hex_xy) == (_STD_PX, _STD_PY)


# ---------------------------------------------------------------- 与 gmssl 互签互验

def test_cross_verify_with_gmssl():
    gm = pytest.importorskip("gmssl.sm2")
    d = sm2.generate_private_key()
    px, py = sm2.public_key(d)
    crypt = gm.CryptSM2(private_key="%064x" % d, public_key="%064x%064x" % (px, py), mode=1)
    msg = os.urandom(40)
    # 他签我验:gmssl 的 sign_with_sm3 用默认 ID,输出 r||s 的 hex
    theirs = bytes.fromhex(crypt.sign_with_sm3(msg))
    assert sm2.verify((px, py), msg, _STD_ID, *sm2.decode_signature(theirs, "raw"))
    # 我签他验
    r, s = sm2.sign(d, msg, _STD_ID)
    assert crypt.verify_with_sm3(sm2.encode_signature(r, s, "raw").hex(), msg)


# ---------------------------------------------------------------- 与 OpenSSL 3 对照(PEM + DER)

def _openssl():
    for cand in ("/opt/homebrew/opt/openssl@3/bin/openssl", shutil.which("openssl") or ""):
        if cand and pathlib.Path(cand).exists():
            out = subprocess.run([cand, "version"], capture_output=True, text=True).stdout
            if out.startswith("OpenSSL 3"):
                return cand
    return None


def test_pem_private_key_and_der_signature_from_openssl(tmp_path):
    ssl = _openssl()
    if not ssl:
        pytest.skip("没有 OpenSSL 3")
    key = tmp_path / "sm2.pem"
    subprocess.run([ssl, "genpkey", "-algorithm", "SM2", "-out", str(key)], check=True, capture_output=True)
    pem = key.read_text()
    d = sm2.parse_private_key(pem)                       # PKCS#8 PEM
    text = subprocess.run([ssl, "pkey", "-in", str(key), "-text", "-noout"], check=True,
                          capture_output=True, text=True).stdout
    priv_hex = "".join(line.strip() for line in text.split("priv:")[1].split("pub:")[0].splitlines()).replace(":", "")
    assert d == int(priv_hex, 16)
    pub_pem = subprocess.run([ssl, "pkey", "-in", str(key), "-pubout"], check=True, capture_output=True, text=True).stdout
    assert sm2.parse_public_key(pub_pem) == sm2.public_key(d)   # SubjectPublicKeyInfo PEM
    msg = tmp_path / "m.txt"
    msg.write_bytes(b"/v1/validateByNorth/validate+1726650000000")
    pub = sm2.public_key(d)
    # OpenSSL 3 的 SM2 默认 distid 是**空串**,不是国标默认的 1234567812345678(Java BouncyCastle / hutool 默认是后者)。
    # 两边都得能验——这正是 user_id 必须可配、必须跟客户确认的原因。
    sig = subprocess.run([ssl, "dgst", "-sm3", "-sign", str(key), "-out", str(tmp_path / "sig.der"), str(msg)],
                         capture_output=True)
    if sig.returncode != 0:
        pytest.skip("此 OpenSSL 不支持 SM2 签名: %s" % sig.stderr.decode()[:120])
    r, s = sm2.decode_signature((tmp_path / "sig.der").read_bytes(), "der")
    assert sm2.verify(pub, msg.read_bytes(), b"", r, s)
    assert not sm2.verify(pub, msg.read_bytes(), _STD_ID, r, s)
    subprocess.run([ssl, "pkeyutl", "-sign", "-rawin", "-digest", "sm3", "-pkeyopt", "distid:1234567812345678",
                    "-in", str(msg), "-inkey", str(key), "-out", str(tmp_path / "sig2.der")], check=True, capture_output=True)
    r2, s2 = sm2.decode_signature((tmp_path / "sig2.der").read_bytes(), "der")
    assert sm2.verify(pub, msg.read_bytes(), _STD_ID, r2, s2)


def test_base64_and_hex_text_encodings():
    raw = sm2.encode_signature(_STD_R, _STD_S, "raw")
    assert sm2.to_text(raw, "hex") == raw.hex()
    assert sm2.to_text(raw, "base64") == base64.b64encode(raw).decode()
    with pytest.raises(sm2.Sm2Error):
        sm2.to_text(raw, "octal")


# ---------------------------------------------------------------- 快路径与慢路径必须一致

def test_fixed_base_table_agrees_with_the_general_scalar_mul():
    """签名热路径走固定基点 comb 表,验签的 t·P 走通用标量乘。两条路算 k·G 必须给出同一个点——
    表建错了不会报错,只会签出一个谁也验不过的签名。"""
    import random
    random.seed(20260919)
    for k in [1, 2, sm2.N - 1, _STD_K] + [random.randrange(1, sm2.N) for _ in range(12)]:
        assert sm2._mul_g(k) == sm2._mul(k, (sm2.GX, sm2.GY)), hex(k)


def test_public_key_and_za_are_computed_once_per_key(monkeypatch):
    """ZA 只依赖私钥与 userId,与消息无关。每次签名重算 = 白做一次标量乘(原来就是这么慢的)。"""
    sm2._signer_za.cache_clear()
    calls = []
    real = sm2._mul_g
    monkeypatch.setattr(sm2, "_mul_g", lambda k: (calls.append(k), real(k))[1])
    d = _STD_D
    for i in range(5):
        sm2.sign(d, b"message-%d" % i)
    # 5 次签名 = 5 次 k·G + 1 次 d·G(算公钥,只在第一次)
    assert len(calls) == 6, calls
    sm2._signer_za.cache_clear()


def test_scalar_mul_handles_the_degenerate_scalars():
    assert sm2._mul_g(0) is None and sm2._mul_g(sm2.N) is None
    assert sm2._mul(5, None) is None
    assert sm2._mul_g(1) == (sm2.GX, sm2.GY)

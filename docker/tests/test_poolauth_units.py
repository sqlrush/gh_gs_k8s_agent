"""网关 ↔ 管家的签名(固定池化):冒充者截到签名也用不了,应答也伪造不了。"""
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "docker"))

import poolauth as pa  # noqa: E402

KEY = b"k" * 32


def _req(**kw):
    args = dict(method="POST", path="/v1/slots/2/bind", pod="pool-3", body=b'{"user":"u1"}', now=1000.0)
    args.update(kw)
    return args, pa.sign_request(KEY, args["method"], args["path"], args["pod"], args["body"], now=args["now"])


def test_a_good_request_passes_once():
    args, h = _req()
    v = pa.Verifier(KEY, "pool-3")
    assert v.check("POST", "/v1/slots/2/bind", h, args["body"], now=1001) == h[pa.HDR_NONCE]
    assert v.check("POST", "/v1/slots/2/bind", h, args["body"], now=1002) is None, "重放"


@pytest.mark.parametrize("change", [
    dict(path="/v1/slots/3/bind"), dict(method="GET"), dict(body=b'{"user":"u2"}'), dict(pod="pool-4"),
])
def test_anything_changed_fails(change):
    args, h = _req()
    v = pa.Verifier(KEY, change.get("pod", "pool-3"))
    assert v.check(change.get("method", "POST"), change.get("path", args["path"]), h,
                   change.get("body", args["body"]), now=1001) is None


def test_old_or_future_timestamps_fail():
    args, h = _req()
    v = pa.Verifier(KEY, "pool-3")
    assert v.check("POST", args["path"], h, args["body"], now=1000 + pa.WINDOW_S + 1) is None
    assert v.check("POST", args["path"], h, args["body"], now=1000 - pa.WINDOW_S - 1) is None


def test_wrong_key_or_garbage_headers_fail():
    args, h = _req()
    assert pa.Verifier(b"x" * 32, "pool-3").check("POST", args["path"], h, args["body"], now=1001) is None
    assert pa.Verifier(KEY, "pool-3").check("POST", args["path"], {pa.HDR_TS: "abc"}, b"", now=1001) is None
    with pytest.raises(ValueError):
        pa.Verifier(b"short", "pool-3")


def test_response_signature_binds_status_body_and_nonce():
    sig = pa.sign_response(KEY, "pool-3", "n" * 32, 200, b'{"ok":1}')
    assert pa.check_response(KEY, "pool-3", "n" * 32, 200, b'{"ok":1}', sig)
    assert not pa.check_response(KEY, "pool-3", "m" * 32, 200, b'{"ok":1}', sig)
    assert not pa.check_response(KEY, "pool-3", "n" * 32, 409, b'{"ok":1}', sig)
    assert not pa.check_response(KEY, "pool-3", "n" * 32, 200, b'{"ok":2}', sig)
    assert not pa.check_response(KEY, "pool-3", "n" * 32, 200, b'{"ok":1}', "")


def test_nonce_cache_stays_bounded():
    v = pa.Verifier(KEY, "pool-3", max_nonces=100)
    for i in range(300):
        _a, h = _req(now=1000.0 + i / 1000)
        assert v.check("POST", "/v1/slots/2/bind", h, b'{"user":"u1"}', now=1000.5)
    assert len(v._seen) <= 100


def test_gateway_and_steward_copies_are_identical():
    """两份一字不差:改一份忘了另一份,签名就对不上(表现是所有控制请求 401)。"""
    assert (_ROOT / "docker" / "poolauth.py").read_bytes() == (_ROOT / "gateway" / "poolauth.py").read_bytes()


# -- 秘密字段加密 -----------------------------------------------------------------------

KEY2 = b"k" * 32


def test_seal_round_trips_and_is_randomized():
    a, b = pa.seal(KEY2, "grmp-令牌-123"), pa.seal(KEY2, "grmp-令牌-123")
    assert a != b and a.startswith(pa.SEAL_PREFIX)
    assert pa.unseal(KEY2, a) == pa.unseal(KEY2, b) == "grmp-令牌-123"
    assert "grmp" not in a and pa.unseal(KEY2, pa.seal(KEY2, "")) == ""


def test_unseal_refuses_tampering_wrong_key_and_garbage():
    tok = pa.seal(KEY2, "secret-token")
    raw = bytearray(pa.base64.urlsafe_b64decode(tok[len(pa.SEAL_PREFIX):]))
    raw[20] ^= 1
    flipped = pa.SEAL_PREFIX + pa.base64.urlsafe_b64encode(bytes(raw)).decode()
    for bad in (flipped, tok[:-4], "secret-token", None, 42, pa.SEAL_PREFIX + "!!!", pa.SEAL_PREFIX):
        assert pa.unseal(KEY2, bad) is None, bad
    assert pa.unseal(b"x" * 32, tok) is None


def test_seal_refuses_oversized_input():
    with pytest.raises(ValueError):
        pa.seal(KEY2, "x" * (pa.SEAL_MAX + 1))

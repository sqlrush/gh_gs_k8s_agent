"""grmp-mock 的签名校验(客户 2026-09-18 加固的本地对照):缺头、错 Appkey、过期时间戳、坏签名都拒;
skill 侧 GrmpClient 带 RequestSigner 发出的请求原样能过——两边旋钮一致就通,不一致就在本地先暴露。"""
import json
import pathlib
import sys
import urllib.request

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common.grmp import signing, sm2  # noqa: E402
from common.grmp.client import GrmpClient, GrmpError  # noqa: E402
from common.grmp.settings import Settings  # noqa: E402
from grmp_middleware.grmp_mock import instances as inst, server, signature, store as st  # noqa: E402

LIST_PATH = "/example/grmp/diagnostic/agent/common-operations"
TOKEN = "0123456789abcdef0123456789abcdef"
DATA_IP = "10.0.0.9"
D = sm2.generate_private_key()
PUB = sm2.public_key(D)


def _policy(**kw):
    base = dict(appkey="gaussdb-agent", public_key=PUB)
    base.update(kw)
    return signature.SignaturePolicy(**base)


@pytest.fixture
def app(tmp_path):
    return server.App(store=st.ScriptStore(tmp_path / "sc.db"), instances=inst.InstanceMap({DATA_IP: "og"}),
                      token=TOKEN, settings=Settings(), signature=_policy())


def _signed(path, **kw):
    st_ = signing.SignSettings(appkey=kw.pop("appkey", "gaussdb-agent"), private_key=D, **kw)
    return signing.RequestSigner(st_).headers(path)


def _post(app, headers):
    headers = {"auth": TOKEN, **headers}
    return app.handle("POST", LIST_PATH, headers, json.dumps({"dataIp": DATA_IP}).encode())


def test_signed_request_from_skill_side_passes(app):
    status, body = _post(app, _signed(LIST_PATH))
    assert status == 200 and body.get("code") == "0"


@pytest.mark.parametrize("mutate, reason", [
    (lambda h: {k: v for k, v in h.items() if k != "Signature"}, "缺少 Signature"),
    (lambda h: {**h, "Appkey": "someone-else"}, "不在允许的调用方范围"),
    (lambda h: {**h, "Signature": "00" * 64}, "Signature 校验失败"),
    (lambda h: {**h, "Signature": "zz"}, "不是合法的 hex"),
    (lambda h: {**h, "Timestamp": "abc"}, "不是整数"),
])
def test_tampered_or_missing_headers_are_rejected_with_reason(app, mutate, reason):
    status, body = _post(app, mutate(_signed(LIST_PATH)))
    assert status == 200 and body.get("code") != "0" and reason in body.get("msg", "")


def test_replayed_timestamp_outside_window_is_rejected():
    stale = signing.RequestSigner(signing.SignSettings(appkey="gaussdb-agent", private_key=D),
                                  clock=lambda: 1_000_000.0).headers(LIST_PATH)
    assert "超出" in _policy(window_seconds=60).check(LIST_PATH, stale, now=1_000_100.0)
    assert _policy(window_seconds=60).check(LIST_PATH, stale, now=1_000_030.0) is None


def test_knobs_must_match_on_both_sides():
    headers = _signed(LIST_PATH, signature_format="der", signature_encoding="base64", user_id=b"")
    assert "校验失败" in _policy().check(LIST_PATH, headers) or "不是合法" in _policy().check(LIST_PATH, headers)
    assert _policy(signature_format="der", signature_encoding="base64", user_id=b"").check(LIST_PATH, headers) is None


def test_signature_covers_the_path(app):
    other = _signed("/some/other/path")
    status, body = _post(app, other)
    assert body.get("code") != "0" and "校验失败" in body["msg"]


def test_policy_from_args_reads_pem_public_key_from_file(tmp_path):
    hex_xy = "%064x%064x" % PUB
    pol = signature.policy_from_args("app", "04" + hex_xy, None, "ms", "raw", "hex", "{path}+{timestamp}", 300)
    assert pol.public_key == PUB and pol.user_id == sm2.DEFAULT_USER_ID
    pol2 = signature.policy_from_args("app", "@k.txt", "", "s", "der", "base64", "{timestamp}{path}", 10,
                                      read_file=lambda p: hex_xy)
    assert pol2.public_key == PUB and pol2.user_id == b"" and pol2.window_seconds == 10


def test_grmp_client_end_to_end_through_the_mock(app, monkeypatch):
    """客户端签 → mock 验:走真实的 urllib.request.Request,只把套接字换成 app.handle。"""
    class Resp:
        def __init__(self, status, payload):
            self.status, self._body = status, json.dumps(payload).encode()

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        headers = dict(req.header_items())
        status, payload = app.handle("POST", req.selector, headers, req.data)
        return Resp(status, payload)
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    signer = signing.RequestSigner(signing.SignSettings(appkey="gaussdb-agent", private_key=D))
    assert GrmpClient("http://mock", token=TOKEN, data_ip=DATA_IP, signer=signer).list_operations() == []
    with pytest.raises(GrmpError) as info:
        GrmpClient("http://mock", token=TOKEN, data_ip=DATA_IP).list_operations()      # 没签名 → mock 拒
    assert "缺少 Appkey" in str(info.value)

"""GRMP 请求签名(客户 2026-09-18 的中间件加固):每个请求在 auth 之外再带 Appkey / Timestamp / Signature。

设置来源:环境变量 > config.yaml 的 api_connection;没配 appkey 就不签(兼容还没开校验的中间件)。
私钥:环境变量 GRMP_SM2_PRIVATE_KEY > 凭据文件(默认名 grmp-sm2)。
签名细节(时间戳单位 / 编码 / 格式 / userId / 原文拼法)全部可配,客户那边任何一项对不上都不用改代码。
"""
import json
import pathlib
import re
import sys
import urllib.request
from dataclasses import replace

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common import access, config  # noqa: E402
from common.grmp import signing, sm2  # noqa: E402
from common.grmp.client import GrmpClient  # noqa: E402

D = sm2.generate_private_key()
D_HEX = "%064x" % D
PUB = sm2.public_key(D)


def _settings(**kw) -> signing.SignSettings:
    base = signing.SignSettings(appkey="gaussdb-agent", private_key=D)
    return replace(base, **kw)


def _check(headers, path, settings=None):
    """按设置解出签名并验签;返回时间戳文本。"""
    st = settings or _settings()
    assert set(headers) == {"Appkey", "Timestamp", "Signature"}
    assert headers["Appkey"] == st.appkey
    ts = headers["Timestamp"]
    payload = st.payload.format(path=path, timestamp=ts).encode("utf-8")
    r, s = sm2.decode_signature(sm2.from_text(headers["Signature"], st.signature_encoding), st.signature_format)
    assert sm2.verify(PUB, payload, st.user_id, r, s)
    return ts


# ---------------------------------------------------------------- 签名器

def test_headers_carry_appkey_millisecond_timestamp_and_verifiable_signature():
    signer = signing.RequestSigner(_settings(), clock=lambda: 1726650000.123)
    headers = signer.headers("/example/grmp/diagnostic/agent/common-operations/invoke")
    ts = _check(headers, "/example/grmp/diagnostic/agent/common-operations/invoke")
    assert ts == "1726650000123"


def test_seconds_unit_der_format_base64_encoding_and_custom_payload():
    st = _settings(timestamp_unit="s", signature_format="der", signature_encoding="base64",
                   payload="{timestamp}|{path}", user_id=b"")
    headers = signing.RequestSigner(st, clock=lambda: 1726650000.9).headers("/v1/x")
    assert _check(headers, "/v1/x", st) == "1726650000"
    assert re.fullmatch(r"[A-Za-z0-9+/=]+", headers["Signature"])


def test_every_request_gets_a_fresh_signature():
    signer = signing.RequestSigner(_settings())
    a, b = signer.headers("/p"), signer.headers("/p")
    assert a["Signature"] != b["Signature"]          # SM2 的 k 随机,同一原文两次签名不同


# ---------------------------------------------------------------- 设置解析

def test_no_appkey_means_no_signing():
    assert signing.settings_from({}, None) is None
    ep = config.ApiEndpoint(host="h", port=80)
    assert signing.settings_from({"GRMP_AUTH_TOKEN": "t"}, ep) is None


def test_env_beats_config_and_key_comes_from_env():
    ep = config.ApiEndpoint(host="h", port=80, appkey="from-config", sign={"timestamp": "s"})
    st = signing.settings_from({"GRMP_APPKEY": "from-env", "GRMP_SM2_PRIVATE_KEY": D_HEX,
                                "GRMP_SIGN_ENCODING": "base64"}, ep)
    assert st.appkey == "from-env" and st.private_key == D
    assert st.timestamp_unit == "s" and st.signature_encoding == "base64"      # 各取各的:env 没给的用 config
    assert st.signature_format == "raw" and st.user_id == sm2.DEFAULT_USER_ID   # 都没给的用默认


def test_key_falls_back_to_credential_store_when_env_missing():
    ep = config.ApiEndpoint(host="h", port=80, appkey="app", sign={"credential": "grmp-prod-sm2"})
    asked = []

    def load(name):
        asked.append(name)
        return D_HEX
    st = signing.settings_from({}, ep, load_secret=load)
    assert asked == ["grmp-prod-sm2"] and st.private_key == D


def test_missing_private_key_is_a_config_error_naming_both_sources():
    ep = config.ApiEndpoint(host="h", port=80, appkey="app")

    def load(name):
        raise signing.credential.CredentialError("no such credential")
    with pytest.raises(config.ConfigError) as info:
        signing.settings_from({}, ep, load_secret=load)
    msg = str(info.value)
    assert "GRMP_SM2_PRIVATE_KEY" in msg and "grmp-sm2" in msg and "credential_cli" in msg


@pytest.mark.parametrize("env", [
    {"GRMP_SIGN_TIMESTAMP": "us"}, {"GRMP_SIGN_FORMAT": "p1363"}, {"GRMP_SIGN_ENCODING": "octal"},
    {"GRMP_SIGN_PAYLOAD": "{path}+{nonce}"}, {"GRMP_SM2_PRIVATE_KEY": "not-a-key"},
])
def test_bad_knob_values_are_config_errors(env):
    base = {"GRMP_APPKEY": "app", "GRMP_SM2_PRIVATE_KEY": D_HEX}
    with pytest.raises(config.ConfigError):
        signing.settings_from({**base, **env}, None)


def test_api_endpoint_parses_appkey_and_sign_block(tmp_path, monkeypatch):
    monkeypatch.delenv("GSDB_HOME", raising=False)
    monkeypatch.setenv("GDAA_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "connection_mode: api\napi_connection:\n  - host: grmp.internal\n    port: 80\n"
        "    appkey: gaussdb-agent\n    sign:\n      credential: grmp-sm2\n      format: der\n      user_id: ''\n",
        encoding="utf-8")
    ep = config.api_endpoint()
    assert ep.appkey == "gaussdb-agent" and ep.appkey_env == "GRMP_APPKEY"
    assert ep.sign == {"credential": "grmp-sm2", "format": "der", "user_id": ""}


# ---------------------------------------------------------------- 客户端 / 接入层

class _Resp:
    def __init__(self, body):
        self._body = json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _capture(monkeypatch, sent):
    def fake(request, timeout=None):
        sent.append(request)
        return _Resp({"code": "0", "result": {"list": [], "hasNextPage": False}})
    monkeypatch.setattr(urllib.request, "urlopen", fake)


def test_client_sends_auth_plus_three_signature_headers(monkeypatch):
    sent = []
    _capture(monkeypatch, sent)
    client = GrmpClient("http://grmp.internal:80", token="tok", data_ip="10.0.0.5",
                        signer=signing.RequestSigner(_settings()))
    client.list_operations()
    req = sent[0]
    assert req.get_header("Auth") == "tok"
    got = {k: v for k, v in req.header_items() if k in ("Appkey", "Timestamp", "Signature")}
    _check(got, "/example/grmp/diagnostic/agent/common-operations")


def test_client_without_signer_sends_only_auth(monkeypatch):
    sent = []
    _capture(monkeypatch, sent)
    GrmpClient("http://grmp.internal:80", token="tok", data_ip="10.0.0.5").list_operations()
    names = {k for k, _ in sent[0].header_items()}
    assert "Auth" in names and not names & {"Appkey", "Timestamp", "Signature"}


def test_runner_for_builds_signer_from_environment(monkeypatch, tmp_path):
    monkeypatch.delenv("GSDB_HOME", raising=False)
    monkeypatch.setenv("GDAA_HOME", str(tmp_path))          # 没有 config.yaml:签名设置全靠环境变量
    monkeypatch.setenv("GRMP_AUTH_TOKEN", "tok")
    monkeypatch.setenv("GRMP_API_HOST", "grmp.internal")
    monkeypatch.setenv("GRMP_APPKEY", "gaussdb-agent")
    monkeypatch.setenv("GRMP_SM2_PRIVATE_KEY", D_HEX)
    conn = config.Connection(name="c", type="opengauss", host="x", port=80, database="d", user="u",
                             driver="grmp", data_ip="10.0.0.5")
    runner = access.runner_for(conn)
    assert runner.client.signer is not None and runner.client.signer.settings.appkey == "gaussdb-agent"
    monkeypatch.delenv("GRMP_APPKEY")
    assert access.runner_for(conn).client.signer is None


def test_runner_for_reports_missing_key_as_access_error(monkeypatch, tmp_path):
    monkeypatch.delenv("GSDB_HOME", raising=False)
    monkeypatch.setenv("GDAA_HOME", str(tmp_path))
    monkeypatch.setenv("GRMP_AUTH_TOKEN", "tok")
    monkeypatch.setenv("GRMP_API_HOST", "grmp.internal")
    monkeypatch.setenv("GRMP_APPKEY", "gaussdb-agent")
    monkeypatch.delenv("GRMP_SM2_PRIVATE_KEY", raising=False)
    conn = config.Connection(name="c", type="opengauss", host="x", port=80, database="d", user="u",
                             driver="grmp", data_ip="10.0.0.5")
    with pytest.raises(access.AccessError) as info:
        access.runner_for(conn)
    assert "GRMP_SM2_PRIVATE_KEY" in str(info.value)

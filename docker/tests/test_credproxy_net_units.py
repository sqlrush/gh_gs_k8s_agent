"""凭据代理的 Fuyao 形态(2026-11-17):沙箱中继(root,加沙箱令牌)→ 集群凭据代理服务(按令牌认人)→ 上游。

整条链路真跑:技能侧对着沙箱里的 credproxy.sock、opencode 对着 127.0.0.1 的模型端口,与按需模式完全一样。要点:
- 工号取自沙箱令牌,GRMP 令牌按工号查(Secret grmp-token-<工号>);上游收到本人的令牌与工号,收不到沙箱令牌;
- 模型请求换成真的模型 Key;流式应答逐块透传;
- 没有 / 伪造的令牌 401;导入环境的令牌只放行模型;查不到本人 GRMP 令牌时说清楚。
"""
import http.client
import json
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "docker"))
sys.path.insert(0, str(_ROOT / "agent"))
sys.path.insert(0, str(_ROOT / "docker" / "tests"))

import credproxy  # noqa: E402
import sbxtoken  # noqa: E402
from test_credproxy_units import INVOKE, _env, _post, sockdir, upstream  # noqa: E402,F401

KEY = b"c" * 32
TOKENS = {"u1001": "tok-u1001", "u2001": "tok-u2001"}


@pytest.fixture()
def chain(upstream, sockdir):
    base, seen, gate = upstream
    looked = []

    def lookup(user):
        looked.append(user)
        return TOKENS.get(user, "")
    proxy, local = credproxy.network_proxy(_env(base), log=lambda m: None)
    net = credproxy.serve_network(0, proxy, KEY, lookup, local, host="127.0.0.1")
    net_url = "http://127.0.0.1:%d" % net.server_address[1]
    servers = [net]

    def relay(user="u1001", kind="runtime", key=KEY):
        rp = credproxy.RelayProxy(net_url, sbxtoken.issue(key, "sbx-1", user, kind))
        path = sockdir / ("%s-%s.sock" % (user, kind))
        servers.append(credproxy.serve_unix(path, rp))
        tcp = credproxy.serve_tcp(0, rp)
        servers.append(tcp)
        return path, tcp.server_address[1]
    yield relay, seen, gate, net_url, looked
    for s in servers:
        s.shutdown()
        s.server_close()


def _model(port, path="/model/chat/completions", body=b'{"m": 1}'):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("POST", path, body=body, headers={"Authorization": "Bearer credproxy", "Content-Type": "application/json"})
    return c, c.getresponse()


def test_grmp_through_the_sandbox_relay_carries_the_token_owner_and_never_the_sandbox_token(chain):
    relay, seen, _g, _u, looked = chain
    sock, _port = relay("u1001")
    code, out = _post(sock, INVOKE, {"scriptName": "health.overview", "userId": "u9999"})
    assert code == 200
    _m, path, h, body = seen[-1]
    assert path == INVOKE and h["auth"] == "tok-u1001" and h["x-user-id"] == "u1001"
    assert json.loads(body)["userId"] == "u1001", "工号按令牌里的人强制写入,请求里自带的不算"
    assert "x-gsdb-sandbox-token" not in h, "沙箱令牌只到集群凭据代理为止"
    assert looked == ["u1001"]


def test_model_through_the_relay_gets_the_real_key_and_streams(chain):
    relay, seen, gate, _u, _l = chain
    _sock, port = relay("u1001")
    c, r = _model(port)
    assert r.status == 200
    r.read()
    c.close()
    h = seen[-1][2]
    assert h["authorization"] == "Bearer real-model-key" and "x-gsdb-sandbox-token" not in h
    c, r = _model(port, "/model/stream", b"{}")
    assert r.read1(100) == b"data: one\n\n", "第二块还被上游扣着:第一块已经到了"
    gate.set()
    assert r.read() == b"data: two\n\n"
    c.close()


def test_missing_or_forged_tokens_are_refused_and_import_sandboxes_only_get_the_model(chain):
    relay, seen, _g, net_url, _l = chain
    host, port = net_url.split("//")[1].split(":")
    c = http.client.HTTPConnection(host, int(port), timeout=10)
    c.request("POST", INVOKE, body=b"{}", headers={"Content-Type": "application/json"})
    assert c.getresponse().status == 401
    c.close()
    sock, _p = relay("u1001", key=b"x" * 32)
    assert _post(sock, INVOKE, {})[0] == 401
    sock, mport = relay("u2001", kind="kb-import")
    code, out = _post(sock, INVOKE, {})
    assert code == 403 and "导入环境" in out["msg"]
    c, r = _model(mport)
    assert r.status == 200
    c.close()
    assert not [s for s in seen if s[1] == INVOKE], "被拒的请求一个都没到中间件"


def test_a_user_without_a_grmp_token_secret_is_told_so(chain):
    relay, seen, *_ = chain
    sock, _p = relay("u3003")
    code, out = _post(sock, INVOKE, {})
    assert code == 503 and "grmp-token" in out["msg"] and not seen


def test_secret_lookup_caches_for_its_ttl_and_treats_failures_as_missing():
    calls, now = [], {"t": 0.0}

    def fetch(user):
        calls.append(user)
        if user == "bad":
            raise OSError("api down")
        return "tok-" + user
    lk = credproxy.SecretLookup("ns", ttl_s=60, fetch=fetch, clock=lambda: now["t"])
    assert lk("u1") == "tok-u1" and lk("u1") == "tok-u1" and calls == ["u1"]
    now["t"] = 61
    assert lk("u1") == "tok-u1" and calls == ["u1", "u1"]
    assert lk("bad") == ""


def test_the_relay_only_accepts_a_plain_http_service_address():
    """集群凭据代理服务在集群里,沙箱到它走明文 HTTP(地址写 ClusterIP:沙箱解析不了集群域名)。"""
    for bad in ("https://10.96.0.21:8091", "10.96.0.21:8091", ""):
        with pytest.raises(credproxy.ConfigError):
            credproxy.RelayProxy(bad, "t")

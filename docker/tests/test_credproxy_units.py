"""凭据代理(docker/credproxy.py,2026-10-23)。

令牌、SM2 私钥、模型 Key 挪出模型所在的容器之后,代理就是唯一加凭据的地方。这里核对它加得对、也只加给该加的:
签名能用公钥验过、工号只认绑定的人(覆盖请求里自带的)、只转发两个诊断路径、只往自己配置的地址转发、
没人在用时一律拒绝;模型那一路换上真 Key、流式应答一块一块地过去;槽位的中继原样转字节。
"""
import http.client
import http.server
import json
import os
import pathlib
import socket
import socketserver
import sys
import tempfile
import threading
import time

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "docker"))
sys.path.insert(0, str(_ROOT / "agent"))

import credproxy  # noqa: E402
from common.grmp import signing, sm2  # noqa: E402

D = sm2.generate_private_key()
PUB = sm2.public_key(D)
PREFIX = "/example/grmp/diagnostic/agent"         # 部署时配置的前缀(GRMP_PATH_PREFIX);单测用样例
LIST = PREFIX + "/common-operations"
INVOKE = PREFIX + "/common-operations/invoke"


class _TCP(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


@pytest.fixture()
def upstream():
    """假中间件 / 假模型服务:记下收到的请求;/v1/stream 分两块回,第二块等测试放行。"""
    seen = []
    gate = threading.Event()

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _read(self):
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(n) if n else b""

        def _reply(self, code, obj):
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            body = self._read()
            seen.append((self.command, self.path, {k.lower(): v for k, v in self.headers.items()}, body))
            if self.path.endswith("/stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for i, part in enumerate((b"data: one\n\n", b"data: two\n\n")):
                    if i:
                        gate.wait(10)
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(part), part))
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                return
            if self.path.endswith("/boom"):
                return self._reply(500, {"msg": "upstream says no"})
            self._reply(200, {"code": "0", "path": self.path})

        do_GET = do_POST

    srv = _TCP(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % srv.server_address[1], seen, gate
    srv.shutdown()
    srv.server_close()


def _env(base, **kw):
    host, port = base.split("//")[1].split(":")
    env = {"GRMP_API_HOST": host, "GRMP_API_PORT": port, "GRMP_PATH_PREFIX": PREFIX, "GRMP_APPKEY": "gaussdb-agent",
           "GRMP_SM2_PRIVATE_KEY": "%064x" % D, "GRMP_USER_ID_HEADER": "X-User-Id", "GRMP_USER_ID_PARAM": "userId",
           "MODEL_BASE_URL": base + "/v1", "MODEL_API_KEY": "real-model-key",
           "AGENT_SKILLS_DIR": str(_ROOT / "agent")}
    env.update(kw)
    return env


@pytest.fixture()
def sockdir():
    d = tempfile.mkdtemp(dir="/tmp", prefix="cp")      # macOS 套接字路径上限 104 字节
    yield pathlib.Path(d)


def _serve(env, sockdir, binding=lambda: ("u1234", "tok-u1234"), **kind):
    proxy = credproxy.Proxy(credproxy.Upstreams.from_env(env), binding, log=lambda m: None)
    path = sockdir / (credproxy.MODEL_SOCKET_NAME if kind.get("model") else credproxy.SOCKET_NAME)
    srv = credproxy.serve_unix(path, proxy, **kind)
    return proxy, path, srv


class _Unix(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("evil.example", timeout=10)
        self._p = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(str(self._p))


def _post(path, url, body, headers=None):
    c = _Unix(path)
    try:
        c.request("POST", url, body=json.dumps(body).encode(), headers=dict({"Content-Type": "application/json"},
                                                                            **(headers or {})))
        r = c.getresponse()
        return r.status, json.loads(r.read() or b"{}")
    finally:
        c.close()


# --- 中间件 ------------------------------------------------------------------------------------

def test_grmp_requests_get_token_signature_and_the_bound_user_id(upstream, sockdir):
    base, seen, _g = upstream
    _p, path, srv = _serve(_env(base), sockdir)
    try:
        code, out = _post(path, LIST, {"dataIp": "10.0.0.9", "userId": "someone-else"},
                          {"X-User-Id": "someone-else", "auth": "forged"})
    finally:
        srv.shutdown()
    assert code == 200 and out["path"] == LIST
    _m, url, h, body = seen[0]
    assert url == LIST and h["auth"] == "tok-u1234" and h["appkey"] == "gaussdb-agent"
    assert h["x-user-id"] == "u1234" and json.loads(body) == {"dataIp": "10.0.0.9", "userId": "u1234"}
    r, s = sm2.decode_signature(sm2.from_text(h["signature"], "hex"), "raw")
    assert sm2.verify(PUB, ("%s+%s" % (LIST, h["timestamp"])).encode(), sm2.DEFAULT_USER_ID, r, s)


def test_headers_only_mode_needs_no_private_key(upstream, sockdir):
    base, seen, _g = upstream
    env = _env(base, GRMP_SIGN_MODE=signing.MODE_HEADERS_ONLY)
    del env["GRMP_SM2_PRIVATE_KEY"]
    _p, path, srv = _serve(env, sockdir)
    try:
        assert _post(path, INVOKE, {"id": "7"})[0] == 200
    finally:
        srv.shutdown()
    h = seen[0][2]
    assert h["appkey"] == "gaussdb-agent" and "timestamp" in h and "signature" not in h


def test_only_the_two_diagnostic_paths_are_forwarded(upstream, sockdir):
    base, seen, _g = upstream
    _p, path, srv = _serve(_env(base), sockdir)
    try:
        for url in (PREFIX + "/other", "/anything", PREFIX + "/common-operations/../x",
                    "/another/prefix/common-operations", "/another/prefix/common-operations/invoke"):
            assert _post(path, url, {})[0] == 404, url
    finally:
        srv.shutdown()
    assert seen == []


def test_nobody_bound_means_nothing_is_forwarded(upstream, sockdir):
    base, seen, _g = upstream
    _p, path, srv = _serve(_env(base), sockdir, binding=lambda: None)
    try:
        code, out = _post(path, LIST, {})
    finally:
        srv.shutdown()
    assert code == 403 and "未绑定" in out["msg"] and seen == []


def test_a_bound_user_without_a_token_is_refused(upstream, sockdir):
    base, seen, _g = upstream
    _p, path, srv = _serve(_env(base), sockdir, binding=lambda: ("u1", ""))
    try:
        assert _post(path, LIST, {})[0] == 503
    finally:
        srv.shutdown()
    assert seen == []


def test_upstream_errors_pass_through_and_unreachable_says_502(upstream, sockdir):
    base, _seen, _g = upstream
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    dead = s.getsockname()[1]
    s.close()
    _p, path, srv = _serve(_env("http://127.0.0.1:%d" % dead), sockdir)
    try:
        code, out = _post(path, LIST, {})
    finally:
        srv.shutdown()
    assert code == 502 and "连不上中间件" in out["msg"]


def test_bad_signing_config_only_breaks_the_grmp_side(upstream, sockdir):
    base, seen, _g = upstream
    env = _env(base)
    del env["GRMP_SM2_PRIVATE_KEY"]                       # 配了 Appkey、full 档,却没有私钥
    proxy, path, srv = _serve(env, sockdir)
    try:
        code, out = _post(path, LIST, {})
    finally:
        srv.shutdown()
    assert code == 500 and "签名" in out["msg"] and seen == []
    assert proxy.ups.model_base                           # 模型那一路照常


def test_upstreams_config_errors():
    with pytest.raises(credproxy.ConfigError):
        credproxy.Upstreams.from_env({"GRMP_API_HOST": "h", "GRMP_API_SCHEME": "ftp"})
    with pytest.raises(credproxy.ConfigError):
        credproxy.Upstreams.from_env({"GRMP_API_HOST": "h", "GRMP_API_PORT": "x"})
    with pytest.raises(credproxy.ConfigError):
        credproxy.Upstreams.from_env({"MODEL_BASE_URL": "file:///etc/passwd", "MODEL_API_KEY": "k"})
    with pytest.raises(credproxy.ConfigError) as exc:
        credproxy.Upstreams.from_env({"MODEL_BASE_URL": "http://m/v1"})
    assert "MODEL_API_KEY" in str(exc.value)
    ups = credproxy.Upstreams.from_env({"GRMP_API_HOST": "10.1.1.1", "GRMP_API_SCHEME": "HTTPS", "GRMP_API_PORT": "9443"})
    assert ups.grmp_base == "https://10.1.1.1:9443" and ups.model_base == ""


def test_the_grmp_socket_never_serves_the_model_and_vice_versa(upstream, sockdir):
    """模型套接字会被中继到 localhost 的端口上(同 Pod 的别的槽位连得到),所以两者绝不混在一个套接字上。"""
    base, seen, _g = upstream
    _p, gpath, gsrv = _serve(_env(base), sockdir)
    _p, mpath, msrv = _serve(_env(base), sockdir, grmp=False, model=True)
    try:
        assert _post(gpath, "/model/chat", {})[0] == 404
        assert _post(mpath, LIST, {})[0] == 404
        assert _post(mpath, "/model/chat", {})[0] == 200
    finally:
        gsrv.shutdown()
        msrv.shutdown()
    assert [u for _m, u, _h, _b in seen] == ["/v1/chat"]


def test_socket_is_group_connectable_only(upstream, sockdir):
    base, _seen, _g = upstream
    _p, path, srv = _serve(_env(base), sockdir)
    try:
        assert os.stat(path).st_mode & 0o777 == 0o660
    finally:
        srv.shutdown()


# --- 模型 ------------------------------------------------------------------------------------

def _model_srv(env):
    proxy = credproxy.Proxy(credproxy.Upstreams.from_env(env), lambda: ("u1", "t"), log=lambda m: None)
    srv = credproxy.serve_tcp(0, proxy)
    return srv, srv.server_address[1]


def test_model_requests_get_the_real_key_and_stay_on_the_configured_base(upstream):
    base, seen, _g = upstream
    srv, port = _model_srv(_env(base))
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request("POST", "/model/chat/completions?x=1", body=b'{"m": 1}',
                  headers={"Authorization": "Bearer credproxy", "Host": "evil.example", "Content-Type": "application/json"})
        r = c.getresponse()
        assert r.status == 200 and json.loads(r.read())["path"] == "/v1/chat/completions?x=1"
        c.close()
    finally:
        srv.shutdown()
    _m, url, h, body = seen[0]
    assert h["authorization"] == "Bearer real-model-key" and h["host"].startswith("127.0.0.1") and body == b'{"m": 1}'


def test_model_streaming_is_passed_through_chunk_by_chunk(upstream):
    base, _seen, gate = upstream
    srv, port = _model_srv(_env(base))
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request("POST", "/model/stream", body=b"{}")
        r = c.getresponse()
        assert r.status == 200 and r.getheader("Content-Type") == "text/event-stream"
        first = r.read1(100)                               # 第二块还被上游扣着:第一块必须已经到了
        assert first == b"data: one\n\n"
        gate.set()
        assert r.read() == b"data: two\n\n"
        c.close()
    finally:
        gate.set()
        srv.shutdown()


def test_model_upstream_errors_keep_their_status(upstream):
    base, _seen, _g = upstream
    srv, port = _model_srv(_env(base))
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request("POST", "/model/boom", body=b"{}")
        r = c.getresponse()
        assert r.status == 500 and json.loads(r.read())["msg"] == "upstream says no"
        c.request("PUT", "/model/x", body=b"{}")
        r = c.getresponse()
        assert r.status == 405
        r.read()
        c.close()
    finally:
        srv.shutdown()


def test_the_tcp_port_never_serves_grmp(upstream):
    base, seen, _g = upstream
    srv, port = _model_srv(_env(base))
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request("POST", LIST, body=b"{}")
        assert c.getresponse().status == 404
        c.close()
    finally:
        srv.shutdown()
    assert seen == []


def test_unconfigured_model_answers_in_openai_error_shape(upstream):
    base, _seen, _g = upstream
    env = _env(base)
    del env["MODEL_BASE_URL"]
    srv, port = _model_srv(env)
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request("POST", "/model/chat/completions", body=b"{}")
        r = c.getresponse()
        assert r.status == 503 and "MODEL_BASE_URL" in json.loads(r.read())["error"]["message"]
        c.close()
    finally:
        srv.shutdown()


def test_chunked_request_bodies_are_accepted(upstream):
    base, seen, _g = upstream
    srv, port = _model_srv(_env(base))
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request("POST", "/model/chat", body=iter([b'{"a":', b' 1}']), encode_chunked=True,
                  headers={"Transfer-Encoding": "chunked"})
        assert c.getresponse().status == 200
        c.close()
    finally:
        srv.shutdown()
    assert seen[0][3] == b'{"a": 1}'


# --- 槽位的中继 ----------------------------------------------------------------------------------

def test_relay_pumps_bytes_to_the_unix_socket_untouched(upstream, sockdir):
    base, seen, _g = upstream
    _p, path, srv = _serve(_env(base), sockdir, grmp=False, model=True)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    ls = credproxy.relay(port, str(path), log=lambda m: None)
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request("POST", "/model/chat", body=b"{}", headers={"Authorization": "Bearer credproxy"})
        r = c.getresponse()
        assert r.status == 200 and json.loads(r.read())["path"] == "/v1/chat"
        c.close()
    finally:
        ls.close()
        srv.shutdown()
    assert seen[0][2]["authorization"] == "Bearer real-model-key"


def test_relay_with_no_proxy_behind_it_just_closes(sockdir):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    logs = []
    ls = credproxy.relay(port, str(sockdir / "missing.sock"), log=logs.append)
    try:
        c = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            assert c.recv(100) == b""
        except ConnectionResetError:
            pass
        c.close()
        deadline = time.time() + 5
        while not logs and time.time() < deadline:
            time.sleep(0.02)
        assert logs and "连不上凭据代理" in logs[0]
    finally:
        ls.close()


def test_missing_path_prefix_is_reported_not_guessed(upstream, sockdir):
    """前缀是部署配置:没配时中间件请求一律回 503 说清楚缺哪一项,不拿一个猜的前缀去请求中间件。"""
    base, seen, _g = upstream
    env = _env(base)
    env.pop("GRMP_PATH_PREFIX")
    _p, path, srv = _serve(env, sockdir)
    try:
        code, out = _post(path, LIST, {"dataIp": "10.0.0.9"})
    finally:
        srv.shutdown()
    assert code == 503 and "GRMP_PATH_PREFIX" in out["msg"] and seen == []


def test_path_prefix_is_validated_and_normalized_like_the_skills_side():
    """凭据代理与技能侧(common/grmp/paths.py)各有一份规整函数:两边对同一批输入必须一致。"""
    from common.grmp import paths
    good = ["/a/b", " /a/b/ ", "/x", "/example/grmp/diagnostic/agent/"]
    bad = ["a/b", "/a b", "/a?x=1", "/a#f", "//a", "/a/../b", "http://h/a"]
    for raw in good:
        assert credproxy.normalize_prefix(raw) == paths.normalize(raw), raw
    for raw in bad:
        with pytest.raises(credproxy.ConfigError):
            credproxy.normalize_prefix(raw)
        with pytest.raises(paths.PrefixError):
            paths.normalize(raw)
    assert credproxy.normalize_prefix("") == paths.normalize("") == ""
    with pytest.raises(credproxy.ConfigError):
        credproxy.Upstreams.from_env({"GRMP_API_HOST": "h", "GRMP_PATH_PREFIX": "no-slash"})

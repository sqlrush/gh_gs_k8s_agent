"""模拟 SSO(k8s/tools/sso-sim.py):现场测试时站在 SSO 的位置上给网关加身份头(docs/现场测试-模拟SSO.md)。

它决定「网关认为这是谁」,所以核对的是头:加了该加的、丢了浏览器自带的、头名跟得上网关的配置。
"""
import http.server
import importlib.util
import json
import pathlib
import socket
import threading
import urllib.request

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[3]
_PATH = _ROOT / "k8s" / "tools" / "sso-sim.py"


def _load(monkeypatch, **env):
    for k in ("SSO_USER_HEADER", "SSO_ROLES_HEADER"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    spec = importlib.util.spec_from_file_location("sso_sim_under_test", _PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture()
def upstream():
    """假网关:把收到的请求头原样回给调用方。"""
    seen = {}

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_a):
            pass

        def do_GET(self):
            if self.path.endswith("/cached") or self.path.endswith("/nocontent"):
                # 浏览器再验证静态文件时网关回的 304;204 同理 —— 都没有消息体
                self.send_response(304 if self.path.endswith("/cached") else 204)
                self.send_header("ETag", '"v1"')
                self.end_headers()
                return
            seen["headers"] = {k.lower(): v for k, v in self.headers.items()}
            body = json.dumps(seen["headers"]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()


def _through(mod, upstream_port, roles="", browser_headers=None):
    port = _free_port()
    srv = mod.Server(("127.0.0.1", port), mod.make_handler(upstream_port, "u0001", "s" * 20, roles))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        req = urllib.request.Request("http://127.0.0.1:%d/global/health" % port, headers=browser_headers or {})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())
    finally:
        srv.shutdown()


def test_adds_user_and_trust_and_drops_what_the_browser_claims(monkeypatch, upstream):
    mod = _load(monkeypatch)
    got = _through(mod, upstream, browser_headers={"X-Agent-User": "boss", "X-Agent-Trust": "guess",
                                                   "X-Agent-Roles": "kb-admin"})
    assert got["x-agent-user"] == "u0001" and got["x-agent-trust"] == "s" * 20
    assert "x-agent-roles" not in got, "浏览器自带的角色头必须丢掉:模拟的是 SSO 覆盖身份,不是透传"


def test_roles_are_sent_only_when_given(monkeypatch, upstream):
    mod = _load(monkeypatch)
    assert _through(mod, upstream, roles="kb-admin")["x-agent-roles"] == "kb-admin"


def test_header_names_follow_the_gateway_config(monkeypatch, upstream):
    """贵方改了 USER_HEADER / ROLES_HEADER 时,模拟 SSO 要用同样的名字,否则网关一律 403。"""
    mod = _load(monkeypatch, SSO_USER_HEADER="X-Employee-Id", SSO_ROLES_HEADER="X-Groups")
    got = _through(mod, upstream, roles="kb-admin",
                   browser_headers={"X-Employee-Id": "boss", "X-Groups": "root"})
    assert got["x-employee-id"] == "u0001" and got["x-groups"] == "kb-admin"
    assert "x-agent-user" not in got and got["x-agent-trust"] == "s" * 20, "信任头名网关写死,不跟着改"


def test_usage_and_empty_trust_file_are_refused(monkeypatch, tmp_path, capsys):
    mod = _load(monkeypatch)
    assert mod.main(["1", "2"]) == 2
    empty = tmp_path / "t"
    empty.write_text("", encoding="utf-8")
    assert mod.main(["18090", "18089", "u0001", str(empty)]) == 2
    assert "空的" in capsys.readouterr().err


def test_listens_only_on_loopback():
    src = _PATH.read_text(encoding="utf-8")
    assert 'Server(("127.0.0.1", listen)' in src, "只许监听本机:谁连得上谁就是那个工号"


def test_bodyless_responses_carry_no_body_so_the_connection_stays_usable(monkeypatch, upstream):
    """**2026-10-15 抓到**(网关同一个毛病,一起修的):304 / 204 被加上分块编码和结束块 `0\\r\\n\\r\\n`,
    浏览器把这 5 个字节当成同一连接上下一个响应的开头(ERR_INVALID_HTTP_RESPONSE)→ 大盘第二次打开整页空白。
    裸 socket 在一条连接上连发两个请求,第二个响应必须从状态行开始。"""
    mod = _load(monkeypatch)
    port = _free_port()
    srv = mod.Server(("127.0.0.1", port), mod.make_handler(upstream, "u0001", "s" * 20))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        for path, code in (("/dash/cached", 304), ("/dash/nocontent", 204)):
            s = socket.create_connection(("127.0.0.1", port), timeout=5)
            s.sendall(("GET %s HTTP/1.1\r\nHost: x\r\n\r\n" % path).encode())
            buf = b""
            while b"\r\n\r\n" not in buf:
                buf += s.recv(4096)
            head, rest = buf.split(b"\r\n\r\n", 1)
            assert head.startswith(b"HTTP/1.1 %d" % code), head[:40]
            assert b"transfer-encoding" not in head.lower(), "%d 不该带分块编码" % code
            s.sendall(b"GET /dash/echo HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            nxt = rest
            while True:
                data = s.recv(4096)
                if not data:
                    break
                nxt += data
            s.close()
            assert nxt.startswith(b"HTTP/1.1 200"), "%d 的响应后面多出了字节:%r" % (code, nxt[:16])
    finally:
        srv.shutdown()

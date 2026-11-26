"""对话页注入老浏览器兼容补丁(v1.5.3,gateway/legacy.py)。

现场 Edge 109 / 360 企业安全浏览器(Chromium 108)上对话页的行内代码是黑块(color-mix() 要 111+)。网关转发 opencode 的
单页入口时在 <head> 前插一行同源脚本引用,脚本由网关自己出。要钉住的:
① 只改对话路由上 GET 200 的未压缩 HTML,长度按改过的重写;接口、事件流、压缩过的、报告页一律原样;
② 引用在 opencode 主题预载脚本之前(<head> 前),且是同源外链(opencode 的 CSP 拦内联脚本);
③ 固定池化(含 Fuyao)与按需建两条转发路径都生效;④ GATEWAY_LEGACY_COMPAT=0 能关。
"""
import dataclasses
import gzip
import pathlib
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from gateway import config as gwconfig  # noqa: E402
from gateway import legacy, pods, server  # noqa: E402
from gateway.tests.test_pods_units import FakeKube  # noqa: E402
from gateway.tests.test_server_pool_units import Router  # noqa: E402

SECRET = "s" * 32
# opencode 1.18.27 单页入口的开头(真实形态:html 上有 style,head 里先是 meta,再是主题预载脚本)
INDEX = (b'<!doctype html>\n<html lang="en" style="background-color: var(--v2-background-bg-deep, #fafafa)">\n'
         b'  <head>\n    <meta charset="utf-8" />\n    <title>OpenCode</title>\n'
         b'    <script id="oc-theme-preload-script">;(function(){})()</script>\n'
         b'    <script type="module" crossorigin src="/assets/index-Cfi2YXOE.js"></script>\n'
         b'  </head>\n  <body><div id="root"></div></body>\n</html>\n')
CSP = "default-src 'self'; script-src 'self' 'wasm-unsafe-eval' 'sha256-x'; style-src 'self' 'unsafe-inline'"


class _Opencode(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, body: bytes, ctype: str, extra=(), chunked=False):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        for k, v in extra:
            self.send_header(k, v)
        if chunked:
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for i in range(0, len(body), 40):
                part = body[i:i + 40]
                self.wfile.write(b"%x\r\n" % len(part) + part + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            return
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        p = self.path
        if p == "/gz":
            return self._send(gzip.compress(INDEX), "text/html", [("Content-Encoding", "gzip")])
        if p == "/chunked":
            return self._send(INDEX, "text/html; charset=utf-8", chunked=True)
        if p == "/api":
            return self._send(b'{"html": "<head>"}', "application/json")
        if p == "/event":
            return self._send(b"data: <head>\n\n", "text/event-stream")
        if p == "/big":
            return self._send(b"<html><head></head><body>" + b"x" * (legacy.MAX_HTML + 10) + b"</body></html>",
                              "text/html")
        return self._send(INDEX, "text/html", [("Content-Security-Policy", CSP), ("ETag", '"v1"')])

    do_HEAD = do_GET

    def do_POST(self):
        return self._send(INDEX, "text/html")

    def log_message(self, *a):
        pass


def _serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _gateway(env_extra=None):
    oc = _serve(_Opencode)
    env = {"GATEWAY_NAMESPACE": "ns", "GATEWAY_IMAGE_TAG": "t", "GATEWAY_TRUST_SECRET": SECRET}
    env.update(env_extra or {})
    cfg = dataclasses.replace(gwconfig.load(env), port=0)
    mgr = pods.Manager(FakeKube(), namespace="ns", image_tag="t", pull_policy="IfNotPresent",
                       templates=_ROOT / "k8s" / "templates")
    app = server.App(cfg, mgr, log=lambda m: None, pool=Router(oc.server_address[1], oc.server_address[1]))
    g = server.serve(app)
    threading.Thread(target=g.serve_forever, daemon=True).start()
    return "http://127.0.0.1:%d" % g.socket.getsockname()[1], (g, oc)


@pytest.fixture()
def gw():
    base, servers = _gateway()
    yield base
    for s in servers:
        s.shutdown()
        s.server_close()


def _get(base, path, method="GET", user="u1234", headers=None):
    h = {"X-Agent-User": user, "X-Agent-Trust": SECRET, "Accept": "text/html"}
    h.update(headers or {})
    r = urllib.request.Request(base + path, headers=h, method=method, data=b"{}" if method == "POST" else None)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


# ---------------------------------------------------------------- 纯函数

def test_inject_puts_the_tag_before_head_and_only_once():
    out = legacy.inject(INDEX)
    tag = legacy.TAG
    assert out.count(tag) == 1 and out.index(tag) < out.index(b"<head>") < out.index(b"oc-theme-preload-script")
    assert out.replace(tag + b"\n", b"") == INDEX, "除了插一行,一字不改"
    assert legacy.inject(out) == out, "已经注入过的不重复"


def test_inject_without_head_goes_after_the_doctype_or_first():
    assert legacy.inject(b"<!doctype html><p>x</p>").startswith(b"<!doctype html>" + legacy.TAG)
    assert legacy.inject(b"<p>x</p>").startswith(legacy.TAG)
    assert b"<header>" in legacy.inject(b"<html><header>a</header>"), "<header> 不是 <head>"
    assert legacy.inject(b"<html><header>a</header>").index(legacy.TAG) == 0


@pytest.mark.parametrize("method,status,ctype,enc,want", [
    ("GET", 200, "text/html", None, True),
    ("GET", 200, "text/html; charset=utf-8", "identity", True),
    ("HEAD", 200, "text/html", None, False),
    ("POST", 200, "text/html", None, False),
    ("GET", 304, "text/html", None, False),
    ("GET", 200, "text/html", "gzip", False),
    ("GET", 200, "application/json", None, False),
    ("GET", 200, "text/event-stream", None, False),
    ("GET", 200, None, None, False),
])
def test_applies_only_to_plain_html_pages(method, status, ctype, enc, want):
    assert legacy.applies(method, status, ctype, enc) is want


def test_the_script_ships_with_the_gateway_and_is_the_compat_patch():
    js = legacy.script().decode("utf-8")
    assert "color-mix" in js and "light-dark" in js and "withResolvers" in js and "__legacyCompat" in js
    assert "dsh" not in js.split("*/", 1)[1], "名字已改成通用的(dsh 只在注释里说明来源)"


# ---------------------------------------------------------------- 经网关(固定池化 / Fuyao 走这条)

def test_chat_page_gets_the_reference_with_a_correct_length_and_keeps_its_csp(gw):
    code, headers, body = _get(gw, "/")
    assert code == 200 and body == legacy.inject(INDEX)
    assert int(headers["Content-Length"]) == len(body) and "chunked" not in (headers.get("Transfer-Encoding") or "")
    assert headers["Content-Security-Policy"] == CSP, "CSP 不动:补丁是同源脚本,script-src 'self' 放行"
    assert headers["ETag"] == '"v1"'


def test_chunked_chat_page_is_injected_too(gw):
    code, _h, body = _get(gw, "/chunked")
    assert code == 200 and body == legacy.inject(INDEX)


@pytest.mark.parametrize("path", ["/api", "/event", "/gz"])
def test_apis_event_streams_and_compressed_pages_pass_through_untouched(gw, path):
    _code, _h, body = _get(gw, path)
    assert legacy.TAG not in body
    if path == "/gz":
        assert gzip.decompress(body) == INDEX


def test_head_and_post_pass_through(gw):
    code, _headers, body = _get(gw, "/", method="HEAD")
    assert code == 200 and body == b"", "HEAD 照原样转发(没有消息体,也就无从注入)"
    _c, _h, body = _get(gw, "/", method="POST")
    assert legacy.TAG not in body


def test_oversized_html_is_forwarded_whole_and_unchanged(gw):
    code, _h, body = _get(gw, "/big")
    assert code == 200 and legacy.TAG not in body and len(body) == len(b"<html><head></head><body></body></html>") + legacy.MAX_HTML + 10


def test_report_pages_are_not_touched(gw):
    _c, _h, body = _get(gw, "/reports/x.html")
    assert legacy.TAG not in body and body == INDEX


def test_the_gateway_serves_the_script_itself(gw):
    code, headers, body = _get(gw, legacy.SCRIPT_PATH)
    assert code == 200 and body == legacy.script()
    assert headers["Content-Type"].startswith("text/javascript") and headers["X-Content-Type-Options"] == "nosniff"
    code, _h, body = _get(gw, legacy.SCRIPT_PATH, method="HEAD")
    assert code == 200 and body == b""


def test_the_script_still_needs_a_trusted_request():
    """与其它路径一样先认人:不可信来源拿不到(网关不对外说任何东西)。"""
    base, servers = _gateway()
    try:
        r = urllib.request.Request(base + legacy.SCRIPT_PATH, headers={"X-Agent-User": "u1234"})
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(r, timeout=10)
        assert ei.value.code == 403
    finally:
        for s in servers:
            s.shutdown()
            s.server_close()


def test_it_can_be_switched_off():
    base, servers = _gateway({"GATEWAY_LEGACY_COMPAT": "0"})
    try:
        _c, _h, body = _get(base, "/")
        assert body == INDEX
        _c, _h, body = _get(base, legacy.SCRIPT_PATH)
        assert body == INDEX, "关掉后这个路径不归网关,照常转给 opencode"
    finally:
        for s in servers:
            s.shutdown()
            s.server_close()


def test_config_rejects_a_typo():
    with pytest.raises(gwconfig.ConfigError):
        gwconfig.load({"GATEWAY_NAMESPACE": "ns", "GATEWAY_IMAGE_TAG": "t", "GATEWAY_TRUST_SECRET": SECRET,
                       "GATEWAY_LEGACY_COMPAT": "on"})
    assert gwconfig.load({"GATEWAY_NAMESPACE": "ns", "GATEWAY_IMAGE_TAG": "t",
                          "GATEWAY_TRUST_SECRET": SECRET}).legacy_compat is True


# ---------------------------------------------------------------- 按需建 Pod 的转发路径

def test_on_demand_path_injects_too(monkeypatch):
    oc = _serve(_Opencode)
    cfg = dataclasses.replace(gwconfig.load({"GATEWAY_NAMESPACE": "ns", "GATEWAY_IMAGE_TAG": "t",
                                             "GATEWAY_TRUST_SECRET": SECRET}), port=0)
    mgr = pods.Manager(FakeKube(), namespace="ns", image_tag="t", pull_policy="IfNotPresent",
                       templates=_ROOT / "k8s" / "templates")
    monkeypatch.setattr(pods.Manager, "state", lambda self, uid, kind=pods.KIND_RUNTIME:
                        pods.PodState(uid, kind, exists=True, ready=True))
    port = oc.server_address[1]
    monkeypatch.setattr("gateway.proxy.Upstream.__init__",
                        lambda self, host, port_=4096, connect_timeout=10.0, retries=4:
                        (setattr(self, "host", "127.0.0.1"), setattr(self, "port", port),
                         setattr(self, "connect_timeout", 2.0), setattr(self, "retries", 1)) and None)
    g = server.serve(server.App(cfg, mgr, log=lambda m: None))
    threading.Thread(target=g.serve_forever, daemon=True).start()
    try:
        base = "http://127.0.0.1:%d" % g.socket.getsockname()[1]
        _c, _h, body = _get(base, "/")
        assert body == legacy.inject(INDEX)
        _c, _h, body = _get(base, "/dash/index.html")
        assert legacy.TAG not in body, "大盘是我们自己的页面,不注入"
    finally:
        for s in (g, oc):
            s.shutdown()
            s.server_close()

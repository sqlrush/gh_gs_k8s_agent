"""网关 HTTP 层在固定池化模式下(2026-10-23):起真的网关 + 假 opencode + 假占位监听,路由器用替身。

要点:探活与事件流不分配也不记活动;报告端口带报告令牌、不带对话口令;满员 / 等待回等待页(浏览器)或 503 JSON
(接口);打到占位监听上(槽位刚转保留)换个地方重来一次;导入容器照旧按需建。
"""
import dataclasses
import json
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
from gateway import pods, poolrouter, server  # noqa: E402
from gateway.tests.test_pods_units import FakeKube  # noqa: E402

SECRET = "s" * 32
ADMIN = "a" * 32


class _Opencode(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        body = json.dumps({"auth": self.headers.get("Authorization", ""), "path": self.path,
                           "port": self.server.server_address[1]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_POST = do_GET

    def log_message(self, *a):
        pass


class _Placeholder(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self.send_response(503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(server.PLACEHOLDER_BODY)))
        self.end_headers()
        self.wfile.write(server.PLACEHOLDER_BODY)

    do_POST = do_GET

    def log_message(self, *a):
        pass


class _Cut(BaseHTTPRequestHandler):
    """发了一段就断:模拟槽位转保留时 opencode 被停掉。/sse 是事件流,其余是普通的分块响应。"""
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream" if self.path.startswith("/sse") else "application/json")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        chunk = b"data: hello\n\n"
        self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
        self.wfile.flush()
        self.connection.shutdown(2)         # 不写结束块就断

    def log_message(self, *a):
        pass


class Router:
    """PoolRouter 的替身:按测试的安排给目标或抛异常,记下每次调用。"""

    def __init__(self, oc_port, rep_port):
        self.calls, self.noted, self.forgot = [], [], []
        self.active = True
        self.raise_next = None
        self.targets = []
        self.default = poolrouter.Target("pool-0", 1, "127.0.0.1", oc_port, "pw-1", rep_port, "rt-1")

    def target(self, user, interactive=True):
        self.calls.append((user, interactive))
        if self.raise_next is not None:
            exc, self.raise_next = self.raise_next, None
            raise exc
        if not interactive and not self.active:
            raise poolrouter.NotActive(user)
        return self.targets.pop(0) if self.targets else self.default

    def note_activity(self, user):
        self.noted.append(user)

    def forget(self, user):
        self.forgot.append(user)


def _serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture()
def gw():
    oc, rep, ph = _serve(_Opencode), _serve(_Opencode), _serve(_Placeholder)
    cfg = dataclasses.replace(gwconfig.load({"GATEWAY_NAMESPACE": "ns", "GATEWAY_IMAGE_TAG": "t",
                                             "GATEWAY_TRUST_SECRET": SECRET, "GATEWAY_ADMIN_TOKEN": ADMIN}), port=0)
    kube = FakeKube()
    mgr = pods.Manager(kube, namespace="ns", image_tag="t", pull_policy="IfNotPresent",
                       templates=_ROOT / "k8s" / "templates")
    router = Router(oc.server_address[1], rep.server_address[1])
    app = server.App(cfg, mgr, log=lambda m: None, pool=router)
    g = server.serve(app)
    threading.Thread(target=g.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % g.socket.getsockname()[1]
    yield base, router, app, ph.server_address[1], mgr
    for s in (g, oc, rep, ph):
        s.shutdown()
        s.server_close()


def _req(base, path, headers=None, method="GET", user="u1234"):
    h = {"X-Agent-User": user, "X-Agent-Trust": SECRET}
    h.update(headers or {})
    r = urllib.request.Request(base + path, headers=h, method=method, data=b"{}" if method == "POST" else None)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode("utf-8")


def test_interactive_requests_go_to_the_slot_with_its_password_and_count_as_activity(gw):
    base, router, *_ = gw
    code, _h, body = _req(base, "/session")
    out = json.loads(body)
    assert code == 200 and out["auth"].startswith("Basic ") and out["path"] == "/session"
    assert router.calls == [("u1234", True)] and router.noted == ["u1234"]


@pytest.mark.parametrize("path,headers", [("/api/health", {}), ("/global/health", {}), ("/global/event", {}),
                                          ("/event", {}), ("/x", {"Accept": "text/event-stream"})])
def test_health_probes_and_event_streams_neither_allocate_nor_count(gw, path, headers):
    base, router, *_ = gw
    router.active = False
    code, h, body = _req(base, path, headers)
    assert code == 503 and json.loads(body)["reason"] == "not_active" and h["Retry-After"] == "30"
    assert router.calls == [("u1234", False)] and router.noted == [], "开着的标签页不能把保留的槽位拉回来"


def test_reports_carry_the_reports_token_not_the_password(gw):
    base, router, *_ = gw
    code, _h, body = _req(base, "/reports/health/latest.json")
    out = json.loads(body)
    assert code == 200 and out["auth"] == "Bearer rt-1" and out["path"] == "/health/latest.json"
    assert out["port"] == router.default.reports_port


def test_a_full_pool_shows_a_self_refreshing_page_to_browsers_and_503_to_apis(gw):
    base, router, *_ = gw
    router.raise_next = poolrouter.PoolFull(30)
    code, h, body = _req(base, "/", {"Accept": "text/html,application/xhtml+xml"})
    assert code == 503 and h["Retry-After"] == "30" and 'http-equiv="refresh" content="30"' in body
    assert "当前使用人数已满" in body
    router.raise_next = poolrouter.PoolFull(30)
    code, h, body = _req(base, "/session", {"Accept": "application/json"})
    assert code == 503 and json.loads(body)["reason"] == "waiting" and h["Retry-After"] == "30"


def test_waiting_for_an_eviction_is_a_shorter_retry(gw):
    base, router, *_ = gw
    router.raise_next = poolrouter.PoolWait("evicting", 10)
    code, h, body = _req(base, "/", {"Accept": "text/html"})
    assert code == 503 and h["Retry-After"] == "10" and "正在为您准备环境" in body


def test_hitting_a_placeholder_forgets_the_creds_and_retries_once(gw):
    base, router, _app, ph_port, _m = gw
    stale = dataclasses.replace(router.default, port=ph_port)
    router.targets = [stale]
    code, _h, body = _req(base, "/session")
    assert code == 200 and json.loads(body)["port"] == router.default.port
    assert router.forgot == ["u1234"] and len(router.calls) == 2


def test_a_placeholder_twice_in_a_row_is_passed_through(gw):
    base, router, _app, ph_port, _m = gw
    stale = dataclasses.replace(router.default, port=ph_port)
    router.targets = [stale, stale]
    code, _h, body = _req(base, "/session")
    assert code == 503 and body == server.PLACEHOLDER_BODY.decode()


def test_missing_grmp_token_secret_says_not_provisioned(gw):
    base, router, *_ = gw
    router.raise_next = pods.PrerequisiteMissing("缺 Secret grmp-token-u1234")
    code, _h, body = _req(base, "/session")
    assert code == 503 and "尚未开通" in body


def test_the_import_container_stays_on_demand(gw, monkeypatch):
    base, router, app, _ph, mgr = gw
    monkeypatch.setattr(server.App, "is_kb_admin", lambda self, who: True)
    seen = []
    monkeypatch.setattr(server.App, "ensure_ready", lambda self, uid, kb, kind=pods.KIND_RUNTIME:
                        seen.append(kind) or (_ for _ in ()).throw(pods.PrerequisiteMissing("stop here")))
    code, _h, _b = _req(base, "/session", {"X-Agent-Target": "kb-import"})
    assert seen == [pods.KIND_KB_IMPORT] and router.calls == [], "导入容器不进池子"


def test_admin_pool_needs_pool_mode(gw):
    base, _router, app, *_ = gw
    code, _h, body = _req(base, "/admin/pool", {"X-Admin-Token": ADMIN})
    assert code == 404 and "固定池化" in body

    class Adm:
        def summary(self):
            return {"pods": []}

        def act(self, action, pod, slot, reason):
            return 202, {"action": action, "pod": pod, "slot": slot}
    app.pool_admin = Adm()
    assert _req(base, "/admin/pool", {"X-Admin-Token": ADMIN})[0] == 200
    r = urllib.request.Request(base + "/admin/pool/evict", method="POST", data=json.dumps({"pod": "pool-1", "slot": 2}).encode(),
                               headers={"X-Admin-Token": ADMIN, "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=10) as resp:
        assert resp.status == 202 and json.loads(resp.read()) == {"action": "evict", "pod": "pool-1", "slot": 2}
    r = urllib.request.Request(base + "/admin/pool/evict", method="POST", data=b'{"pod": "x"}',
                               headers={"X-Admin-Token": ADMIN, "Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(r, timeout=10)
    assert exc.value.code == 400


def test_pool_interactive_classifier():
    assert server.pool_interactive("GET", "/api/health?x=1", {}) is False
    assert server.pool_interactive("GET", "/global/event/", {}) is False
    assert server.pool_interactive("GET", "/session/abc/message", {"Accept": "*/*"}) is True
    assert server.pool_interactive("POST", "/session", {}) is True


def test_an_event_stream_cut_by_a_retain_ends_cleanly_but_a_cut_response_stays_truncated(gw):
    base, router, *_ = gw
    cut = _serve(_Cut)
    try:
        router.default = dataclasses.replace(router.default, port=cut.server_address[1])
        code, _h, body = _req(base, "/sse")
        assert code == 200 and "data: hello" in body, "事件流:写上结束块,浏览器干净地结束、自己重连"
        with pytest.raises(Exception):
            _req(base, "/plain")                # 普通响应:不能让截断的内容看起来完整
    finally:
        cut.shutdown()
        cut.server_close()

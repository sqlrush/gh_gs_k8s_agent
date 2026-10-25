"""网关 HTTP 层:起真的服务 + 真的假上游,端到端走一遍。

重点不在路由对不对,在这四条:
① 不可信来源打代理入口 → 403,而且**不说原因**(说了就是告诉试探者哪一步过了);
② 没配控制密钥时 /admin 一律 404 —— 默认不暴露运维接口;
③ SSE **按块转发且不缓冲**:客户端要能在上游还没结束时就收到前面的字节;
④ 上游连不上时回 502 并说人话,不要把 traceback 丢给用户。
"""
import dataclasses
import json
import pathlib
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from gateway import config as gwconfig  # noqa: E402
from gateway import identity as idt     # noqa: E402
from gateway import pods, server        # noqa: E402
from gateway.tests.test_pods_units import FakeKube  # noqa: E402

SECRET = "s" * 32
ADMIN = "a" * 32


class _Upstream(BaseHTTPRequestHandler):
    """假的 runtime Pod:/sse 分块慢慢吐,其余回显收到的头。"""
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        if self.path.endswith("/cached") or self.path.endswith("/nocontent"):
            # 浏览器带 If-None-Match 再验证静态文件时 nginx 回的 304;204 同理 —— 都没有消息体
            self.send_response(304 if self.path.endswith("/cached") else 204)
            self.send_header("ETag", '"v1"')
            self.end_headers()
            return
        if self.path.startswith("/slow"):
            # 同步慢响应:头都要等一会儿才发。模拟 POST /session/{id}/message 这类要跑完模型才回的请求
            time.sleep(3)
            body = b'{"slow": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/sse"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("x-accel-buffering", "no")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for i in range(3):
                chunk = b"data: tick%d\n\n" % i
                self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                self.wfile.flush()
                time.sleep(0.05)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            return
        body = json.dumps({"auth": self.headers.get("Authorization", ""),
                           "fwd_user": self.headers.get("X-Forwarded-User", ""),
                           "trust": self.headers.get("X-Agent-Trust", ""),
                           "target": self.headers.get("X-Agent-Target", ""),
                           "path": self.path}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", "1234")
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture()
def stack(monkeypatch):
    up = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    up_port = up.server_address[1]

    kube = FakeKube()
    # 配置层不接受 port=0(生产上那会变成「随机端口」,是个静默失效),
    # 所以测试里不从环境给 0,而是加载后 replace 成 0 让 OS 挑临时端口。
    cfg = dataclasses.replace(
        gwconfig.load({"GATEWAY_NAMESPACE": "ns", "GATEWAY_IMAGE_TAG": "t",
                       "GATEWAY_TRUST_SECRET": SECRET, "GATEWAY_ADMIN_TOKEN": ADMIN}), port=0)
    mgr = pods.Manager(kube, namespace="ns", image_tag="t", pull_policy="IfNotPresent",
                       templates=_ROOT / "k8s" / "templates")
    # FakeKube 的 apply 不会产生 status,state() 永远 not ready → 直接让它「已就绪」
    monkeypatch.setattr(pods.Manager, "state",
                        lambda self, uid, kind=pods.KIND_RUNTIME:
                        pods.PodState(uid, kind, exists=True, ready=True))
    # 上游指到假 Pod
    # 记下网关本想连的 (host, port),再把它指到假 Pod —— 三路分发的测试靠这个看路由对不对
    asked = []
    monkeypatch.setattr("gateway.proxy.Upstream.__init__",
                        lambda self, host, port=4096, connect_timeout=10.0:
                        (asked.append((host, port)), setattr(self, "host", "127.0.0.1"),
                         setattr(self, "port", up_port), setattr(self, "connect_timeout", 2.0)) and None)
    app = server.App(cfg, mgr, log=lambda _m: None)
    app.asked = asked
    gw = server.serve(app)
    gw.server_address = gw.socket.getsockname()
    threading.Thread(target=gw.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % gw.server_address[1]
    yield base, kube, app
    gw.shutdown(); gw.server_close(); up.shutdown(); up.server_close()


def _req(base, path, headers=None, method="GET", data=None):
    r = urllib.request.Request(base + path, headers=headers or {}, method=method, data=data)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


# --- 安全边界 -----------------------------------------------------------------

def test_healthz_needs_no_auth(stack):
    base, _k, _a = stack
    code, body = _req(base, "/healthz")
    assert code == 200 and json.loads(body)["ok"] is True


def test_proxy_without_trust_is_403_and_says_nothing_useful(stack):
    base, _k, _a = stack
    code, body = _req(base, "/session", {"X-Agent-User": "u1234"})
    assert code == 403
    for leak in ("secret", "cidr", "工号格式", "来源不可信"):
        assert leak not in body, "错误信息泄露了判定细节:%s" % body


def test_admin_is_404_when_no_admin_token(monkeypatch):
    """默认不暴露运维接口 —— 没配密钥就当它不存在,连「需要鉴权」都不说。"""
    kube = FakeKube()
    cfg = dataclasses.replace(
        gwconfig.load({"GATEWAY_NAMESPACE": "ns", "GATEWAY_IMAGE_TAG": "t",
                       "GATEWAY_TRUST_SECRET": SECRET}), port=0)
    mgr = pods.Manager(kube, namespace="ns", image_tag="t", pull_policy="IfNotPresent",
                       templates=_ROOT / "k8s" / "templates")
    gw = server.serve(server.App(cfg, mgr, log=lambda _m: None))
    gw.server_address = gw.socket.getsockname()
    threading.Thread(target=gw.serve_forever, daemon=True).start()
    try:
        code, _ = _req("http://127.0.0.1:%d" % gw.server_address[1], "/admin/pods")
        assert code == 404
    finally:
        gw.shutdown(); gw.server_close()


def test_admin_wrong_token_is_403(stack):
    base, _k, _a = stack
    code, _ = _req(base, "/admin/pods", {"X-Admin-Token": "wrong"})
    assert code == 403


def test_admin_lists_pods(stack):
    base, _k, _a = stack
    code, body = _req(base, "/admin/pods", {"X-Admin-Token": ADMIN})
    assert code == 200 and "pods" in json.loads(body)


def test_admin_rejects_illegal_user_id(stack):
    base, _k, _a = stack
    code, _ = _req(base, "/admin/pods/..%2fetc", {"X-Admin-Token": ADMIN}, method="GET")
    assert code in (400, 404)


# --- 代理 ---------------------------------------------------------------------

def test_proxy_injects_basic_auth_and_strips_trust_header(stack):
    """口令由网关注入;信任头**不能**透给后端(它是网关与上游之间的凭据)。"""
    base, _k, _a = stack
    code, body = _req(base, "/whoami", {idt.HEADER_TRUST: SECRET, "X-Agent-User": "u1234"})
    assert code == 200
    seen = json.loads(body)
    assert seen["auth"].startswith("Basic "), "没注入 Basic 口令"
    assert seen["fwd_user"] == "u1234"
    assert seen["trust"] == "", "信任头透到后端去了"


def test_sse_arrives_in_chunks_not_buffered(stack):
    """**核心**:上游边吐边转,客户端不能等到全部结束才拿到第一块。

    **用裸 socket,不用 urllib。** urllib 会在自己这一层做缓冲读,三块「同时到达」
    分不清是网关缓冲了还是客户端缓冲了 —— 第一版这么写,结果测的是 urllib。
    """
    import socket
    base, _k, _a = stack
    port = int(base.rsplit(":", 1)[1])
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    s.sendall(("GET /sse HTTP/1.1\r\nHost: x\r\n%s: %s\r\nX-Agent-User: u1234\r\n\r\n"
               % (idt.HEADER_TRUST, SECRET)).encode())
    t0 = time.time()
    arrivals, buf, headers_done = [], b"", False
    while time.time() - t0 < 8:
        try:
            data = s.recv(4096)
        except socket.timeout:
            break
        if not data:
            break
        buf += data
        if not headers_done and b"\r\n\r\n" in buf:
            headers_done = True
            head, buf = buf.split(b"\r\n\r\n", 1)
            assert b"x-accel-buffering: no" in head.lower(), "上游的关缓冲头要传下去"
        if headers_done and b"data:" in data:
            arrivals.append(time.time() - t0)
        if b"0\r\n\r\n" in buf:
            break
    s.close()
    assert len(arrivals) >= 3, "只在 %d 次 recv 里看到 data:(上游分 3 次吐)" % len(arrivals)
    assert arrivals[-1] - arrivals[0] > 0.05, \
        "三块在 %.3f 秒内全到 —— 说明被攒起来一次性发了" % (arrivals[-1] - arrivals[0])


def test_activity_is_recorded_while_proxying(stack):
    base, kube, _a = stack
    _req(base, "/whoami", {idt.HEADER_TRUST: SECRET, "X-Agent-User": "u1234"})
    assert any(kind == "deployments" and "last-activity" in json.dumps(p)
               for kind, _n, p in kube.patched), "转发时没记活动时间 —— 会被回收器误判闲置"


def test_upstream_down_is_502_in_plain_language(stack, monkeypatch):
    base, _k, _a = stack
    monkeypatch.setattr("gateway.proxy.Upstream.send",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("connection refused")))
    code, body = _req(base, "/session", {idt.HEADER_TRUST: SECRET, "X-Agent-User": "u1234"})
    assert code == 502
    assert "Traceback" not in body and "OSError" not in body


def test_hot_path_does_not_apply_and_so_keeps_activity_annotation(stack, monkeypatch):
    """**2026-09-21 真跑时踩到。**

    原来每个请求都走 ensure() 做 server-side apply,而模板里没有 last-activity 注解,
    于是 apply 把 touch() 写的活动时间抹掉 —— 回收器看不到活动,退回按创建时间算,
    最终会把**正在用的** Pod 收掉。实测表现是 /admin/pods/<工号> 的 last_activity 恒为 0。

    修法:Pod 已存在且就绪时**不 apply**,只取口令 + touch。
    """
    base, kube, _a = stack
    _req(base, "/whoami", {idt.HEADER_TRUST: SECRET, "X-Agent-User": "u1234"})
    applied_deploys = [n for kind, n, _ in kube.applied if kind == "deployments"]
    assert not applied_deploys, "热路径上 apply 了 Deployment:%s —— 会抹掉活动注解" % applied_deploys
    assert any("last-activity" in json.dumps(p) for _k, _n, p in kube.patched), "活动时间没记上"


def test_missing_prerequisite_is_503_not_504(stack, monkeypatch):
    """缺按人签发的令牌是**配置缺失**,等下去不会好;报 504「正在启动请稍后重试」
    会让用户一直刷,而运维永远看不到真原因。"""
    from gateway import pods as gwpods
    base, _k, _a = stack
    monkeypatch.setattr(gwpods.Manager, "state",
                        lambda self, uid, kind=gwpods.KIND_RUNTIME:
                        gwpods.PodState(uid, kind, exists=False, ready=False))
    monkeypatch.setattr(gwpods.Manager, "require_prerequisites",
                        lambda self, uid, kb: (_ for _ in ()).throw(
                            gwpods.PrerequisiteMissing("缺 Secret grmp-token-%s" % uid)))
    code, body = _req(base, "/session", {idt.HEADER_TRUST: SECRET, "X-Agent-User": "u1234"})
    assert code == 503
    assert "联系管理员" in body
    assert "grmp-token" not in body, "给最终用户的话不该带内部资源名"


# --- 三路分发 -----------------------------------------------------------------

def test_route_for_is_a_pure_function():
    assert server.route_for("/dash") == "dash" and server.route_for("/dash/health?x=1") == "dash"
    assert server.route_for("/reports/health/latest.json") == "reports"
    assert server.route_for("/dashboard") == "opencode" and server.route_for("/reportsx") == "opencode"
    assert server.route_for("/session") == "opencode" and server.route_for("/") == "opencode"


def test_reports_route_goes_to_4097_with_prefix_stripped_and_still_touches(stack):
    base, kube, app = stack
    code, body = _req(base, "/reports/health/latest.json", {idt.HEADER_TRUST: SECRET, "X-Agent-User": "u1234"})
    assert code == 200
    echoed = json.loads(body)
    assert echoed["path"] == "/health/latest.json", "转发给报告端口的路径要去掉 /reports 前缀"
    assert app.asked[-1] == ("runtime-u1234.ns.svc", 4097)
    assert echoed["auth"] == "", "报告端口没有口令,不该注入 Authorization"
    assert any("last-activity" in json.dumps(p) for _k, _n, p in kube.patched), "看大盘也算在用"


def test_dash_route_goes_to_frontend_without_password_or_touch_or_ensure(stack):
    base, kube, app = stack
    code, body = _req(base, "/dash/health", {idt.HEADER_TRUST: SECRET, "X-Agent-User": "u1234"})
    assert code == 200
    echoed = json.loads(body)
    assert app.asked[-1] == ("frontend.ns.svc", 80)
    assert echoed["auth"] == "" and echoed["fwd_user"] == "u1234"
    assert not any("last-activity" in json.dumps(p) for _k, _n, p in kube.patched), "静态页不记活动"
    assert not [n for kind, n, _ in kube.applied if kind == "secrets"], "/dash 不该碰用户的口令 Secret"


def test_dash_route_still_requires_trust(stack):
    base = stack[0]
    code, _ = _req(base, "/dash/health", {"X-Agent-User": "u1234"})
    assert code == 403


def test_slow_synchronous_response_is_not_cut_by_connect_timeout(stack):
    """**2026-09-22 e2e 抓到。** POST /session/{id}/message 是同步的,要等模型跑完才回 ——
    可 HTTPConnection(timeout=connect_timeout) 那个 timeout 同时管读响应,10 秒一到就 timed out → 502,
    而 Pod 里的活其实照跑。connect_timeout 只该管连接;连上之后读响应不设超时(proxy.py 注释写了,代码没做到)。
    夹具里 connect_timeout 是 2 秒,上游 3 秒才回:修好前必 502。"""
    base, _k, _a = stack
    code, body = _req(base, "/slow", {idt.HEADER_TRUST: SECRET, "X-Agent-User": "u1234"})
    assert code == 200, body
    assert json.loads(body)["slow"] is True


def test_bodyless_responses_carry_no_body_so_the_connection_stays_usable(stack):
    """**2026-10-15 抓到。** 网关给所有响应都加分块编码并写结束块 `0\\r\\n\\r\\n` —— 而 304 / 204 / HEAD 按协议没有消息体,
    那 5 个字节被浏览器当成同一连接上下一个响应的开头(ERR_INVALID_HTTP_RESPONSE)。大盘第二次打开时静态文件全是 304,
    其中一个模块坏掉 → 整个模块图加载失败 → 整页空白。用裸 socket 在一条连接上连发两个请求,第二个响应必须完好。"""
    import socket
    base, _k, _a = stack
    port = int(base.rsplit(":", 1)[1])
    auth = "%s: %s\r\nX-Agent-User: u1234\r\n" % (idt.HEADER_TRUST, SECRET)
    for method, path, code in (("GET", "/dash/cached", 304), ("GET", "/dash/nocontent", 204), ("HEAD", "/dash/page", 200)):
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        s.sendall(("%s %s HTTP/1.1\r\nHost: x\r\n%s\r\n" % (method, path, auth)).encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += s.recv(4096)
        head, rest = buf.split(b"\r\n\r\n", 1)
        assert head.startswith(b"HTTP/1.1 %d" % code), head[:40]
        assert b"transfer-encoding" not in head.lower(), "%s %s 不该带分块编码" % (method, code)
        # 第二个请求带 Connection: close,读到对端关闭为止,不留未读数据(否则 close 发 RST,服务端日志一片报错)
        s.sendall(("GET /dash/echo HTTP/1.1\r\nHost: x\r\nConnection: close\r\n%s\r\n" % auth).encode())
        nxt = rest
        while True:
            data = s.recv(4096)
            if not data:
                break
            nxt += data
        s.close()
        assert nxt.startswith(b"HTTP/1.1 200"), "%s %s 的响应后面多出了字节:%r" % (method, code, nxt[:16])


def test_stream_ends_cleanly_when_the_environment_is_deleted_midway(stack, monkeypatch):
    """**2026-10-14 抓到。** 事件流开着时 Deployment 被删:touch 拿到 404。
    以前异常冒到 _route 的兜底,在**已经发出 200 与分块头之后**再写一个 500 —— 分块流被污染,
    客户端读到的是坏响应。现在要:日志记一笔,分块正常收尾,不夹带任何 500 文本。"""
    import http.client
    base, _k, app = stack
    logs = []
    app.log = logs.append

    def gone(_uid, now=None, kind=pods.KIND_RUNTIME):
        raise pods.EnvironmentGone("runtime-u1234 已不存在")
    monkeypatch.setattr(app.mgr, "touch", gone)
    port = int(base.rsplit(":", 1)[1])
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("GET", "/sse", headers={idt.HEADER_TRUST: SECRET, "X-Agent-User": "u1234"})
    r = c.getresponse()
    body = r.read()                 # 分块没正常收尾的话这里会抛 IncompleteRead
    c.close()
    assert r.status == 200
    assert "网关内部错误" not in body.decode("utf-8", "replace") and b"500" not in body
    assert any("已被回收" in x and "u1234" in x for x in logs), logs


def _seen_kb_admin(stack, monkeypatch, headers):
    base, kube, app = stack
    seen = []
    monkeypatch.setattr(app, "ensure_ready", lambda uid, kb_admin, kind=pods.KIND_RUNTIME: (seen.append((uid, kb_admin)), "pw")[1])
    _req(base, "/whoami", {idt.HEADER_TRUST: SECRET, **headers})
    return seen


def test_user_listed_in_agent_roles_is_a_kb_admin_without_any_role_header(stack, monkeypatch):
    """手册写的「或写进 ConfigMap agent-roles」—— 以前网关不读这张表,写进去的人一律当普通用户。"""
    _b, kube, _a = stack
    kube.objects[("configmaps", "agent-roles")] = {"data": {"kb-admins": "u2001\n"}}
    assert _seen_kb_admin(stack, monkeypatch, {"X-Agent-User": "u2001"}) == [("u2001", True)]


def test_role_header_still_makes_a_kb_admin_and_others_are_not(stack, monkeypatch):
    _b, kube, _a = stack
    kube.objects[("configmaps", "agent-roles")] = {"data": {"kb-admins": "u2001\n"}}
    assert _seen_kb_admin(stack, monkeypatch, {"X-Agent-User": "u3001", "X-Agent-Roles": "dba,KB-Admin"}) == [("u3001", True)]
    assert _seen_kb_admin(stack, monkeypatch, {"X-Agent-User": "u3002"}) == [("u3002", False)]



# --- 起不来时说清原因(v1.1.6 待修第 2 项) -----------------------------------------

def _cold(stack, monkeypatch, diag, timeout_s=120):
    """让这个人的 Pod 永远不就绪、诊断结果固定,轮询间隔压到 10ms。"""
    from gateway import diagnose as dg
    base, kube, app = stack
    logs = []
    app.log = logs.append
    app.cfg = dataclasses.replace(app.cfg, ready_timeout_s=timeout_s)
    monkeypatch.setattr(server, "POLL_S", 0.01)
    monkeypatch.setattr(pods.Manager, "state",
                        lambda self, uid, kind=pods.KIND_RUNTIME: pods.PodState(uid, kind, exists=False, ready=False))
    monkeypatch.setattr(pods.Manager, "require_prerequisites", lambda self, uid, kb: None)
    monkeypatch.setattr(pods.Manager, "diagnose", lambda self, uid, kind=pods.KIND_RUNTIME: diag)
    return base, logs, dg


def test_permanent_failure_answers_early_with_the_category_only(stack, monkeypatch):
    """拉不到镜像等下去也不会好:不等满 120 秒,当场告诉用户是哪一类、找管理员;
    镜像仓库地址这类内部信息只进网关日志。"""
    from gateway import diagnose as dg
    d = dg.Diagnosis(dg.IMAGE, 'Pod runtime-u1234-x:ImagePullBackOff:pulling "registry.corp/gaussdb-agent-runtime:t"', True)
    base, logs, _ = _cold(stack, monkeypatch, d)
    t0 = time.time()
    code, body = _req(base, "/session", {idt.HEADER_TRUST: SECRET, "X-Agent-User": "u1234"})
    assert time.time() - t0 < 10, "等下去不会好的失败不该等满超时"
    assert code == 503 and "拉不到镜像" in body and "联系管理员" in body
    assert "registry.corp" not in body
    assert any("registry.corp" in x and "u1234" in x for x in logs), logs
    assert any("判定等下去不会好" in x for x in logs), "提前返回不是「N 秒内未就绪」"


def test_timeout_now_says_which_kind_of_wait_it_was(stack, monkeypatch):
    from gateway import diagnose as dg
    d = dg.Diagnosis(dg.MOUNT, "Pod runtime-u1234-x:存储卷挂不上;FailedMount mount failed: exit status 32", False)
    base, logs, _ = _cold(stack, monkeypatch, d, timeout_s=1)
    code, body = _req(base, "/session", {idt.HEADER_TRUST: SECRET, "X-Agent-User": "u1234"})
    assert code == 504 and "存储(NAS)挂不上" in body and "exit status 32" not in body
    assert any("exit status 32" in x for x in logs), "以前 504 在网关日志里一个字都没有"


def test_timeout_without_a_diagnosis_keeps_the_old_message(stack, monkeypatch):
    """网关没被授予读 Pod 的权限(升级时漏了重新 apply Role)→ 退回原来的说法,不报错。"""
    base, logs, _ = _cold(stack, monkeypatch, None, timeout_s=1)
    code, body = _req(base, "/session", {idt.HEADER_TRUST: SECRET, "X-Agent-User": "u1234"})
    assert code == 504 and "正在启动" in body
    assert logs, "超时本身也要进日志"


def test_admin_api_gets_the_full_reason(stack, monkeypatch):
    from gateway import diagnose as dg
    d = dg.Diagnosis(dg.CONFIG, 'CreateContainerConfigError:secret "model-api" not found', True)
    base, _logs, _ = _cold(stack, monkeypatch, d)
    code, body = _req(base, "/admin/pods/u1234", {"X-Admin-Token": ADMIN}, method="POST", data=b"{}")
    assert code == 503
    j = json.loads(body)
    assert j["category"] == dg.CONFIG and "model-api" in j["error"] and j["permanent"] is True



# --- 管理员经网关进导入容器(v1.1.6 待修第 6 项) ------------------------------------
# 网关会给知识库管理员建 kb-import-<工号>,但以前所有对话请求都只转到 runtime —— 导入容器建出来了却没有入口。
# 路径前缀走不通(opencode 网页用根绝对路径),所以按「目标」选:SSO 可给管理员入口加请求头
# X-Agent-Target: kb-import;浏览器访问 /kb-import/ 设 cookie 切过去,/runtime/ 切回。每个请求都重新校验管理员身份。

def _raw(base, path, headers):
    import http.client
    port = int(base.rsplit(":", 1)[1])
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("GET", path, headers=headers)
    r = c.getresponse()
    out = (r.status, dict(r.getheaders()), r.read().decode("utf-8"))
    c.close()
    return out


ADMIN_H = {idt.HEADER_TRUST: SECRET, "X-Agent-User": "u2001", "X-Agent-Roles": "kb-admin"}


def test_admin_switches_to_the_import_container_and_back(stack):
    base, kube, app = stack
    code, h, _ = _raw(base, "/kb-import/", ADMIN_H)
    assert code == 303 and h["Location"] == server.LANDING
    assert "gsa_target=kb-import" in h["Set-Cookie"] and "HttpOnly" in h["Set-Cookie"] and "Path=/" in h["Set-Cookie"]

    code, body = _req(base, "/session", {**ADMIN_H, "Cookie": "gsa_target=kb-import"})
    assert code == 200 and app.asked[-1] == ("kb-import-u2001.ns.svc", 4096)
    assert json.loads(body)["auth"].startswith("Basic "), "导入容器同样要注入口令"
    assert any(n == "kb-import-u2001" for k, n, _p in kube.patched), "在导入容器里的活动要记在导入容器上,否则它闲置 30 分钟就被缩掉"

    code, h, _ = _raw(base, "/runtime/", ADMIN_H)
    assert code == 303 and "gsa_target=" in h["Set-Cookie"] and "Max-Age=0" in h["Set-Cookie"]


def test_sso_can_route_admins_with_a_header_and_it_is_not_forwarded(stack):
    base, _k, app = stack
    code, body = _req(base, "/session", {**ADMIN_H, "X-Agent-Target": "kb-import"})
    assert code == 200 and app.asked[-1] == ("kb-import-u2001.ns.svc", 4096)
    assert json.loads(body)["target"] == "", "选目标的头是给网关的,不往容器里传"


def test_non_admin_cannot_switch_and_a_forged_cookie_goes_to_runtime(stack):
    base, _k, app = stack
    user = {idt.HEADER_TRUST: SECRET, "X-Agent-User": "u3001"}
    code, _h, body = _raw(base, "/kb-import/", user)
    assert code == 403 and "知识库管理员" in body
    code, _ = _req(base, "/session", {**user, "Cookie": "gsa_target=kb-import", "X-Agent-Target": "kb-import"})
    assert code == 200 and app.asked[-1] == ("runtime-u3001.ns.svc", 4096)


def test_reports_and_dash_ignore_the_target(stack):
    base, _k, app = stack
    _req(base, "/reports/health/latest.json", {**ADMIN_H, "Cookie": "gsa_target=kb-import"})
    assert app.asked[-1] == ("runtime-u2001.ns.svc", 4097), "报告只在诊断容器里"


def test_import_container_scaled_to_zero_is_brought_back_on_the_hot_path(stack, monkeypatch):
    """以前热路径只看 runtime 就绪:导入容器被回收器缩到 0 之后,管理员再也进不去(网关不会再拉起它)。"""
    base, kube, app = stack
    applied = []
    monkeypatch.setattr(pods.Manager, "state",
                        lambda self, uid, kind=pods.KIND_RUNTIME:
                        pods.PodState(uid, kind, exists=True,
                                      ready=(kind == pods.KIND_RUNTIME or bool(applied))))
    monkeypatch.setattr(pods.Manager, "require_prerequisites", lambda self, uid, kb: None)
    real = pods.Manager.ensure
    monkeypatch.setattr(pods.Manager, "ensure",
                        lambda self, uid, kb, now=None: (applied.append((uid, kb)), real(self, uid, kb, now))[1])
    code, _ = _req(base, "/session", {**ADMIN_H, "X-Agent-Target": "kb-import"})
    assert code == 200 and applied == [("u2001", True)]
    assert app.asked[-1] == ("kb-import-u2001.ns.svc", 4096)

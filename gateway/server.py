"""网关的 HTTP 层:两个入口。

**代理入口(所有其它路径)** —— 给「没有容器调度能力」的客户:
    浏览器/CLI → 客户 SSO/WAF(认人,把工号放进头) → 本网关 → runtime-<工号>:4096
    网关负责:认这个头凭不凭得过 → 按工号找/建 Pod → 等就绪 → 反代 → 记活动

**控制 API `/admin/...`** —— 给「已经有一套容器管理框架」的客户:
    他们自己的调度器直接调这几个接口,代理那半可以完全不用。
    用独立的 GATEWAY_ADMIN_TOKEN 鉴权(和代理入口的信任来源分开:
    那一层信的是「上游已经认过人」,这一层信的是「调用方持有运维密钥」)。

**/healthz** 不鉴权 —— 探针要用,而且它不泄露任何东西。
"""
from __future__ import annotations

import http.client
import http.cookies
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional, Tuple

from . import identity as idt
from . import kbclip, pods, poolrouter, proxy
from .diagnose import NotReady

_MAX_BODY = 64 * 1024 * 1024        # 上传走网关的话,单请求上限;超过直接 413
POLL_S = 1.5                        # 等用户 Pod 就绪时的轮询间隔
DIAG_EVERY = 4                      # 每轮询这么多次读一次 Pod 状态:等下去不会好的就提前返回


ROUTE_DASH, ROUTE_REPORTS, ROUTE_OPENCODE = "dash", "reports", "opencode"
REPORTS_PORT = 4097          # 用户 Pod 里 podctl serve-reports 的端口(docker/entrypoint.sh)

# 对话请求转到哪个容器(知识库管理员才有 kb-import)。路径前缀走不通 —— opencode 网页用根绝对路径,
# 所以按「目标」选:SSO 可给管理员入口加请求头;浏览器访问切换地址设 cookie。都在网关校验身份。
TARGET_HEADER = "X-Agent-Target"
TARGET_COOKIE = "gsa_target"
SWITCH_PATHS = {"/kb-import": pods.KIND_KB_IMPORT, "/runtime": pods.KIND_RUNTIME}
# 切换后落到 opencode 的项目页:base64url("/data/state/workspace"),两个容器同一个工作目录(docker/entrypoint.sh)
LANDING = "/L2RhdGEvc3RhdGUvd29ya3NwYWNl"


# 固定池化:这些请求**不算交互、也不为它分配槽位** —— opencode 网页开着就每 10 秒探一次活、事件流一直挂着
# (P0 摸底 S5)。算的话开着的标签页永远不闲置,转保留了也会被它马上拉回来。
_NON_INTERACTIVE = frozenset({"/api/health", "/global/health", "/global/event", "/event"})
PLACEHOLDER_BODY = b'{"reason": "slot_not_running"}'      # 槽位不跑 opencode 时占位监听的回答(docker/slotrun.py)
WAITING_PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="%(retry)d"><title>%(title)s</title>
<style>body{font-family:system-ui,sans-serif;max-width:36rem;margin:18vh auto;padding:0 1.5rem;color:#333}
h1{font-size:1.3rem}p{line-height:1.7;color:#555}</style></head>
<body><h1>%(title)s</h1><p>%(body)s</p><p>本页每 %(retry)d 秒自动重试,不用手动刷新。</p></body></html>"""


def pool_interactive(method: str, path: str, headers) -> bool:
    p = path.split("?", 1)[0].rstrip("/") or "/"
    if p in _NON_INTERACTIVE:
        return False
    return "text/event-stream" not in (headers.get("Accept") or "")


def route_for(path: str) -> str:
    """按路径前缀分三路。纯函数,单独可测。

    /dash/*     大盘前端(全体共用的静态页),不建 Pod、不注口令、不记活动
    /reports/*  用户自己 Pod 的报告只读端口,去掉前缀后转发
    其它        opencode serve(对话),原样
    """
    p = path.split("?", 1)[0]
    if p == "/dash" or p.startswith("/dash/"):
        return ROUTE_DASH
    if p.startswith("/reports/"):
        return ROUTE_REPORTS
    return ROUTE_OPENCODE


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "gaussdb-agent-gateway"
    sys_version = ""

    # -- 工具 ---------------------------------------------------------------

    @property
    def app(self):
        return self.server.app          # type: ignore[attr-defined]

    def _peer(self) -> str:
        return self.client_address[0] if self.client_address else ""

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _text(self, code: int, msg: str) -> None:
        body = msg.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> Optional[bytes]:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return None
        if n > _MAX_BODY:
            self._text(413, "请求体过大")
            return None
        return self.rfile.read(n)

    def log_message(self, fmt: str, *args) -> None:
        # 默认实现把每个请求打到 stderr,SSE 心跳会把日志刷爆。只留非 2xx。
        try:
            code = int(args[1])
        except (IndexError, ValueError):
            code = 0
        if code and code >= 400:
            self.app.log("%s %s → %s" % (self.command, self.path.split("?")[0], code))

    # -- 路由 ---------------------------------------------------------------

    def do_GET(self):
        self._route()

    def do_POST(self):
        self._route()

    def do_PUT(self):
        self._route()

    def do_PATCH(self):
        self._route()

    def do_DELETE(self):
        self._route()

    def do_HEAD(self):
        self._route()

    def _route(self) -> None:
        path = self.path.split("?")[0]
        self._streaming = False          # 响应头发出去之后就不能再回一个完整的错误响应
        try:
            if path == "/healthz":
                return self._json(200, {"ok": True, "version": self.server_version})
            if path.startswith("/admin/"):
                return self._admin(path)
            return self._proxy()
        except (BrokenPipeError, ConnectionResetError):
            pass                     # 客户端先走了,正常
        except Exception as exc:     # noqa: BLE001
            # **必须兜住。** 不兜的话 socketserver 只是打一段 traceback 到 stderr 并**断开连接**,
            # 客户端看到的是「空响应」—— 比一个 500 难查得多(2026-09-21 真部署时就这样:
            # k8s API 连不上,/admin/pods 返回完全空白,要翻 Pod 日志才看见原因)。
            self.app.log("处理 %s %s 时未捕获的异常:%r" % (self.command, path, exc))
            if self._streaming:
                # 200 与分块头已经发出:再写一个 500 会被当成分块长度,整条流就坏了。只能断开。
                self.close_connection = True
                return
            try:
                self._json(500, {"error": "网关内部错误,请看网关日志(不回传细节)"})
            except OSError:
                pass

    # -- 控制 API -----------------------------------------------------------

    def _admin(self, path: str) -> None:
        token = self.app.cfg.admin_token
        if not token:
            return self._json(404, {"error": "控制 API 未启用(没有配 GATEWAY_ADMIN_TOKEN)"})
        import hmac
        got = (self.headers.get("X-Admin-Token") or "")
        if not hmac.compare_digest(got, token):
            return self._json(403, {"error": "控制 API 密钥不对"})

        parts = [p for p in path.split("/") if p]        # ['admin','pods',...]
        if len(parts) >= 2 and parts[1] == "pool":
            return self._admin_pool(parts)
        if len(parts) == 2 and parts[1] == "pods" and self.command == "GET":
            return self._json(200, {"pods": self.app.list_pods()})
        if len(parts) == 3 and parts[1] == "pods":
            uid = idt.normalize_user_id(parts[2])
            if not idt.valid_user_id(uid):
                return self._json(400, {"error": "工号格式不合法"})
            if self.command == "GET":
                st = self.app.mgr.state(uid)
                return self._json(200, {"user_id": uid, "exists": st.exists,
                                        "ready": st.ready, "last_activity": st.last_activity})
            if self.command == "POST":
                body = self._read_body()
                want_admin = False
                if body:
                    try:
                        want_admin = bool(json.loads(body).get("kb_admin"))
                    except ValueError:
                        return self._json(400, {"error": "请求体不是合法 JSON"})
                try:
                    self.app.ensure_ready(uid, want_admin)
                except pods.PrerequisiteMissing as exc:
                    # 控制 API 的调用方是运维系统,**要看到完整原因**(缺哪个 Secret),
                    # 不像代理入口那边面对的是最终用户。
                    return self._json(503, {"error": str(exc)})
                except NotReady as exc:
                    return self._json(503 if exc.permanent else 504,
                                      {"error": exc.detail, "category": exc.category, "permanent": exc.permanent})
                st = self.app.mgr.state(uid)
                return self._json(200, {"user_id": uid, "ready": st.ready,
                                        "note": "口令由网关持有并在代理时注入;控制 API 不返回它"})
            if self.command == "DELETE":
                self.app.mgr.remove(uid)
                return self._json(200, {"user_id": uid, "removed": True,
                                        "note": "NAS 上的会话/历史/上传文件保留,下次登录挂回来即恢复"})
        self._json(404, {"error": "没有这个控制接口"})

    def _admin_pool(self, parts) -> None:
        adm = self.app.pool_admin
        if adm is None:
            return self._json(404, {"error": "网关不是固定池化模式(GATEWAY_RUNTIME_MODE=pool)"})
        if len(parts) == 2 and self.command == "GET":
            return self._json(200, adm.summary())
        if len(parts) == 3 and self.command == "POST":
            raw = self._read_body() or b"{}"
            try:
                req = json.loads(raw)
                pod, slot = str(req["pod"]), req["slot"]
                if isinstance(slot, bool) or not isinstance(slot, int):
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                return self._json(400, {"error": "请求体要是 {\"pod\": Pod 名, \"slot\": 槽位号}"})
            code, body = adm.act(parts[2], pod, slot, str(req.get("reason") or "admin"))
            return self._json(code, body)
        self._json(404, {"error": "没有这个控制接口"})

    # -- 代理 ---------------------------------------------------------------

    def _proxy(self) -> None:
        try:
            who = idt.extract(dict(self.headers.items()), self._peer(),
                              header_name=self.app.cfg.user_header,
                              roles_header=self.app.cfg.roles_header,
                              secret=self.app.cfg.trust_secret,
                              cidrs=self.app.cfg.trust_cidrs)
        except idt.IdentityError:
            # 不回细节:这是安全边界,错误信息会告诉试探者哪一步过了
            return self._text(403, "无法确认身份。请经统一认证入口访问。")

        bare = self.path.split("?", 1)[0]
        if kbclip.handles(bare):
            return kbclip.handle(self, who, bare)        # 网页剪藏的安装页 / 剪藏页 / 上传(只给知识库管理员)
        switch = SWITCH_PATHS.get(bare.rstrip("/"))
        if switch is not None:
            return self._switch(who, switch)

        route = route_for(self.path)
        body = self._read_body()
        if body is None and int(self.headers.get("Content-Length") or 0) > 0:
            return                                  # _read_body 已经回了 413

        if route == ROUTE_DASH:
            # 大盘是静态页;浏览器拿到页面后自己经本网关调 /reports 与 opencode。
            # 这里只转发:不建 Pod、不注口令、不记活动(看静态页不算在用)。
            up = proxy.Upstream("%s.%s.svc" % (self.app.cfg.frontend_service, self.app.cfg.namespace), 80)
            headers = proxy.forward_headers(self.headers.items(), None, who.user_id)
            path = self.path
        else:
            kb_admin = self.app.is_kb_admin(who)
            # 报告只在诊断容器里;对话按目标选
            kind = self._target(who, kb_admin) if route == ROUTE_OPENCODE else pods.KIND_RUNTIME
            if kind == pods.KIND_RUNTIME and self.app.pool is not None:
                return self._proxy_pool(who, route, body)      # 固定池化;导入容器照旧按需建
            try:
                password = self.app.ensure_ready(who.user_id, kb_admin, kind)
            except pods.PrerequisiteMissing as exc:
                # 配置缺失,等下去不会好。**日志里写清缺什么**,回给用户的话只说找谁 ——
                # 报成「正在启动,请稍后重试」会让用户一直刷,而运维永远看不到原因。
                self.app.log("为 %s 准备环境失败(前置条件):%s" % (who.user_id, exc))
                return self._text(503, "您的环境尚未开通,请联系管理员为您开通后再登录。")
            except NotReady as exc:
                # 日志里写完整原因(以前 504 在网关日志里一个字都没有);给用户只说是哪一类、找谁
                self.app.log("为 %s 准备环境未就绪:%s" % (who.user_id, exc.detail))
                if exc.permanent:
                    return self._text(503, "您的环境启动失败(%s),等待不会好转,请联系管理员处理。" % exc.category)
                why = (":%s" % exc.category) if exc.category else ""
                return self._text(504, "您的环境正在启动,请稍后重试。(%d 秒内未就绪%s)" % (exc.waited_s, why))
            except Exception as exc:                    # noqa: BLE001
                self.app.log("为 %s 准备环境失败:%s" % (who.user_id, exc))
                return self._text(502, "环境准备失败,请联系管理员。")
            host = "%s-%s.%s.svc" % (kind, who.user_id, self.app.cfg.namespace)
            if route == ROUTE_REPORTS:
                # 报告端口没有口令(鉴权就是「网关只把本人的请求路由过来」);它以 reports/ 为根
                up = proxy.Upstream(host, REPORTS_PORT)
                headers = proxy.forward_headers(self.headers.items(), None, who.user_id)
                path = self.path[len("/reports"):]
            else:
                up = proxy.Upstream(host)
                headers = proxy.forward_headers(self.headers.items(), password, who.user_id)
                path = self.path

        try:
            resp = up.send(self.command, path, headers, body)
        except OSError as exc:
            self.app.log("转发到 %s(%s)失败:%s" % (who.user_id, route, exc))
            return self._text(502, "大盘服务未部署或不可达。" if route == ROUTE_DASH
                              else "无法连接到您的环境,请稍后重试。")
        # 建连时先记一次,免得长连接期间被判闲置;静态页除外
        touch = (lambda: self.app.mgr.touch(who.user_id, kind=kind)) if route != ROUTE_DASH else (lambda: None)
        self._relay(resp, who, touch, per_bytes=True)

    def _relay(self, resp, who: idt.Identity, touch, per_bytes: bool) -> None:
        self.send_response(resp.status)
        for k, v in proxy.response_headers(resp.getheaders()):
            self.send_header(k, v)
        # 1xx / 204 / 304 与 HEAD 的响应按协议没有消息体:不能加分块编码,更不能写结束块。
        # 写了的话那 5 个字节会被浏览器当成同一连接上下一个响应的开头(ERR_INVALID_HTTP_RESPONSE)——
        # 大盘第二次打开时静态文件全是 304,一个模块坏掉整页就空白(2026-10-15 抓到)。
        if self.command == "HEAD" or resp.status in (204, 304) or 100 <= resp.status < 200:
            self.end_headers()
            resp.close()
            return
        # 我们按块转发,长度不可知 → 分块编码。SSE 也走这条路。
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self._streaming = True
        try:
            touch()
            proxy.pump(resp, self._write_chunk, self.wfile.flush, on_bytes=(lambda _n: touch()) if per_bytes else None)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except pods.EnvironmentGone as exc:
            # 用户的 Deployment 在流还开着时被删了(注销 / 卸载重装):背后的 Pod 正在退出,
            # 继续转发只会断在半路 —— 把分块正常收尾,让客户端干净地结束这条连接。
            self.app.log("%s 的环境已被回收,结束这条连接(%s)" % (who.user_id, exc))
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            self.close_connection = True
        except proxy.UpstreamClosed as exc:
            # 上游中途断了(固定池化下槽位转保留时 opencode 被停掉,事件流就这样结束)。事件流写上结束块,
            # 让浏览器干净地结束、按自己的节奏重连;普通响应不写 —— 写了会让截断的内容看起来是完整的
            self.close_connection = True
            if (resp.getheader("Content-Type") or "").startswith("text/event-stream"):
                try:
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except OSError:
                    pass
            else:
                self.app.log("转发给 %s 的响应在中途断了(上游):%s" % (who.user_id, exc))
        except (BrokenPipeError, ConnectionResetError):
            pass                                     # 用户关了页面,正常
        finally:
            resp.close()

    # -- 固定池化 -------------------------------------------------------------

    def _proxy_pool(self, who: idt.Identity, route: str, body: Optional[bytes]) -> None:
        """槽位由 poolrouter 分配。活动只按交互请求记一次(不按转发字节:事件流的心跳不算在用)。"""
        interactive = pool_interactive(self.command, self.path, self.headers)
        pool = self.app.pool
        for attempt in (0, 1):
            try:
                t = pool.target(who.user_id, interactive)
            except poolrouter.NotActive:
                return self._json_retry(503, {"reason": "not_active", "error": "您的环境当前未在运行,有操作时自动恢复"}, 30)
            except poolrouter.PoolFull as exc:
                return self._waiting("当前使用人数已满", "已为您排队:有人空出来就会自动进入。", exc.retry_after)
            except poolrouter.PoolWait as exc:
                self.app.log("为 %s 分配槽位还在等(%s)" % (who.user_id, exc.reason))
                return self._waiting("正在为您准备环境", "马上就好。", exc.retry_after)
            except pods.PrerequisiteMissing as exc:
                self.app.log("为 %s 准备环境失败(前置条件):%s" % (who.user_id, exc))
                return self._text(503, "您的环境尚未开通,请联系管理员为您开通后再登录。")
            except Exception as exc:                    # noqa: BLE001
                self.app.log("为 %s 分配槽位失败:%s" % (who.user_id, exc))
                return self._text(502, "环境准备失败,请联系管理员。")
            if interactive:
                pool.note_activity(who.user_id)
            if route == ROUTE_REPORTS:
                up = proxy.Upstream(t.host, t.reports_port, retries=1)
                headers = proxy.forward_headers(self.headers.items(), None, who.user_id) + \
                    [("Authorization", "Bearer " + t.reports_token)]
                path = self.path[len("/reports"):]
            else:
                up = proxy.Upstream(t.host, t.port, retries=1)
                headers = proxy.forward_headers(self.headers.items(), t.password, who.user_id)
                path = self.path
            try:
                resp = up.send(self.command, path, headers, body)
            except OSError as exc:
                pool.forget(who.user_id)
                if attempt == 0:
                    continue
                self.app.log("转发到 %s(%s/槽位 %d)失败:%s" % (who.user_id, t.pod, t.slot, exc))
                return self._text(502, "无法连接到您的环境,请稍后重试。")
            if resp.status == 503:
                data = resp.read()
                resp.close()
                if data == PLACEHOLDER_BODY:
                    # 打到占位监听上了:槽位刚转保留 / 被腾挪,视图还没刷新。占位监听什么都没处理,换个地方重来是安全的
                    pool.forget(who.user_id)
                    if attempt == 0:
                        continue
                return self._complete(503, resp.getheaders(), data)
            return self._relay(resp, who, lambda: None, per_bytes=False)

    def _json_retry(self, code: int, payload: dict, retry_after: int) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Retry-After", str(retry_after))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _waiting(self, title: str, text: str, retry_after: int) -> None:
        """浏览器打开页面:一张自动重试的等待页;接口请求(opencode 网页里的 fetch):503 + Retry-After。"""
        if self.command == "GET" and "text/html" in (self.headers.get("Accept") or ""):
            page = WAITING_PAGE % {"title": title, "body": text, "retry": retry_after}
            data = page.encode("utf-8")
            self.send_response(503)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Retry-After", str(retry_after))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        return self._json_retry(503, {"reason": "waiting", "error": "%s,%s" % (title, text)}, retry_after)

    def _complete(self, code: int, headers, data: bytes) -> None:
        self.send_response(code)
        for k, v in proxy.response_headers(headers):
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _target(self, who: idt.Identity, kb_admin: bool) -> str:
        """请求头优先(SSO 给管理员入口加的),其次 cookie(浏览器切换过)。不是管理员一律回诊断容器。"""
        want = (self.headers.get(TARGET_HEADER) or "").strip().lower()
        if not want:
            jar = http.cookies.SimpleCookie()
            try:
                jar.load(self.headers.get("Cookie") or "")
            except http.cookies.CookieError:
                pass
            want = jar[TARGET_COOKIE].value if TARGET_COOKIE in jar else ""
        if want == pods.KIND_KB_IMPORT:
            if kb_admin:
                return pods.KIND_KB_IMPORT
            self.app.log("%s 要进导入容器但不是知识库管理员,转到诊断容器" % who.user_id)
        return pods.KIND_RUNTIME

    def _switch(self, who: idt.Identity, kind: str) -> None:
        """/kb-import/ 切到导入容器,/runtime/ 切回;之后落到对话页。"""
        if kind == pods.KIND_KB_IMPORT and not self.app.is_kb_admin(who):
            return self._text(403, "您不是知识库管理员,没有导入容器。")
        if kind == pods.KIND_KB_IMPORT:
            cookie = "%s=%s; Path=/; HttpOnly; SameSite=Lax" % (TARGET_COOKIE, kind)
        else:
            cookie = "%s=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0" % TARGET_COOKIE
        self.send_response(303)
        self.send_header("Location", LANDING)
        self.send_header("Set-Cookie", cookie)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _write_chunk(self, data: bytes) -> None:
        self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")


class App:
    """把配置、k8s、Pod 管理、回收器串起来。"""

    def __init__(self, cfg, mgr: pods.Manager, log=print, pool=None, pool_admin=None):
        self.cfg = cfg
        self.mgr = mgr
        self.log = log
        self.pool = pool                    # 固定池化时是 poolrouter.PoolRouter;按需模式 None
        self.pool_admin = pool_admin        # 固定池化的运维接口(/admin/pool)
        self._waiters: dict = {}
        self._lock = threading.Lock()

    def is_kb_admin(self, who: idt.Identity) -> bool:
        """角色头里带 KB_ADMIN_ROLES 之一,**或**工号登记在 ConfigMap agent-roles 里(SSO 不传角色时用)。"""
        if any(r in self.cfg.kb_admin_roles for r in who.roles):
            return True
        return who.user_id in self.mgr.kb_admins()

    def ensure_ready(self, user_id: str, kb_admin: bool, kind: str = pods.KIND_RUNTIME) -> str:
        """保证这个人的 Pod(kind 那一个)存在且就绪,返回它的 Basic 口令(两个容器同一个口令)。

        同一个人的并发请求只让第一个去建 —— 否则浏览器一开就是十几个并发请求,
        每个都去 apply 一遍 Deployment。**按 kind 判就绪**:导入容器被回收器缩到 0 后,
        runtime 仍就绪 —— 只看 runtime 的话管理员就再也进不去导入容器了。
        """
        with self._lock:
            lock = self._waiters.setdefault(user_id, threading.Lock())
        with lock:
            st = self.mgr.state(user_id, kind)
            if st.exists and st.ready:
                # **热路径上不做 apply。** 两个原因:
                # ① server-side apply 会按模板重写对象,而模板里没有 last-activity 注解,
                #    于是每个请求都把 touch() 写的活动时间抹掉 —— 回收器因此看不到活动,
                #    退回按创建时间算,最终会把正在用的 Pod 收掉(2026-09-21 实测 last_activity=0)。
                # ② 每个请求少一次 k8s 写,SSE 心跳那种频率下差别很大。
                return self.mgr.ensure_password(user_id)
            self.mgr.require_prerequisites(user_id, kb_admin)
            password = self.mgr.ensure(user_id, kb_admin)
            start = time.time()
            deadline = start + self.cfg.ready_timeout_s
            polls = 0
            while time.time() < deadline:
                if self.mgr.state(user_id, kind).ready:
                    return password
                polls += 1
                if polls % DIAG_EVERY == 0:
                    # 拉不到镜像 / 配置缺失 / 个人目录初始化失败:等满超时也不会好,不让用户白等
                    d = self.mgr.diagnose(user_id, kind)
                    if d is not None and d.permanent:
                        raise NotReady(d, int(time.time() - start))
                time.sleep(POLL_S)
            raise NotReady(self.mgr.diagnose(user_id, kind), self.cfg.ready_timeout_s)

    def list_pods(self) -> list:
        from . import reaper
        items = (self.mgr.kube.list(pods.APPS, "deployments").get("items") or [])
        out = []
        for d in items:
            name = ((d.get("metadata") or {}).get("name") or "")
            uid = reaper.user_of(name)
            if not uid:
                continue
            out.append({"deployment": name, "user_id": uid,
                        "replicas": (d.get("spec") or {}).get("replicas"),
                        "ready": int((d.get("status") or {}).get("availableReplicas") or 0) >= 1,
                        "last_activity": reaper.last_seen(d)})
        return out


def serve(app: App) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer(("0.0.0.0", app.cfg.port), _Handler)
    httpd.daemon_threads = True          # 用户关页面时不要卡住停机
    httpd.app = app                      # type: ignore[attr-defined]
    return httpd

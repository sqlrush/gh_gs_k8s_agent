"""发版台的 HTTP 层:JSON 接口 + 静态页面。只做路由与校验,业务在 app.py。

第一版不做登录(封闭环境、只给运维;默认只监听 127.0.0.1)。写操作两道防线,挡住「别的网页借浏览器发请求」:
必须是 application/json(跨站表单发不了,发就要过 CORS 预检,而这里不回 CORS 头);Origin 有的话必须与 Host 同源。
"""
from __future__ import annotations

import json
import mimetypes
import pathlib
import re
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Optional, Tuple

from .app import ApiError, App

WEB = pathlib.Path(__file__).resolve().parent / "web"
MAX_BODY = 8 << 20                      # 白名单导出文件最大 8 MiB


def _route(method: str, path: str) -> Tuple[Optional[str], Dict[str, str]]:
    table = [
        ("GET", r"^/api/overview$", "overview"), ("GET", r"^/api/config$", "config"), ("PUT", r"^/api/config$", "save_config"),
        ("POST", r"^/api/secrets/(?P<name>[a-z_]+)$", "secret"), ("POST", r"^/api/whitelist/export$", "wl_export"),
        ("GET", r"^/api/scan$", "scan"), ("POST", r"^/api/scan$", "start_scan"), ("GET", r"^/api/plan$", "plan"),
        ("POST", r"^/api/runs$", "start"), ("GET", r"^/api/runs/(?P<rid>\d+)$", "run"),
        ("GET", r"^/api/runs/(?P<rid>\d+)/logs$", "logs"), ("GET", r"^/api/runs/(?P<rid>\d+)/diff$", "diff"),
        ("POST", r"^/api/runs/(?P<rid>\d+)/(?P<act>pause|resume|gate)$", "act"), ("POST", r"^/api/rollback$", "rollback"),
        ("GET", r"^/api/cluster$", "cluster"), ("GET", r"^/api/history$", "history"), ("GET", r"^/api/audit$", "audit"),
    ]
    for m, pat, name in table:
        if m == method:
            mt = re.match(pat, path)
            if mt:
                return name, mt.groupdict()
    return None, {}


def make_handler(app: App) -> type:
    class Handler(BaseHTTPRequestHandler):
        server_version = "gaussdb-release"

        def log_message(self, *_a):
            pass

        def _json(self, code: int, obj: Any) -> None:
            body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _text(self, code: int, text: str, ctype: str = "text/plain; charset=utf-8") -> None:
            body = text.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> Any:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise ApiError(413, "请求太大")
            raw = self.rfile.read(n) if n else b""
            if not raw:
                return {}
            try:
                return json.loads(raw)
            except ValueError:
                raise ApiError(400, "请求体不是 JSON")

        def _same_origin(self) -> bool:
            origin = self.headers.get("Origin")
            if not origin:
                return True
            return urllib.parse.urlsplit(origin).netloc == (self.headers.get("Host") or "")

        def _static(self, path: str) -> None:
            rel = "index.html" if path in ("/", "/index.html") else path[len("/web/"):] if path.startswith("/web/") else ""
            f = (WEB / rel).resolve()
            if not rel or WEB not in f.parents or not f.is_file():
                return self._text(404, "没有这个页面")
            body = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", (mimetypes.guess_type(str(f))[0] or "application/octet-stream")
                             + ("; charset=utf-8" if f.suffix in (".html", ".js", ".css") else ""))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(body)

        def _handle(self, method: str) -> None:
            u = urllib.parse.urlsplit(self.path)
            q = dict(urllib.parse.parse_qsl(u.query))
            if not u.path.startswith("/api/"):
                return self._static(u.path) if method == "GET" else self._text(405, "不支持")
            name, args = _route(method, u.path)
            if name is None:
                return self._json(404, {"error": "没有这个接口"})
            if method != "GET":
                if not self._same_origin():
                    return self._json(403, {"error": "跨站请求被拒绝"})
                if "application/json" not in (self.headers.get("Content-Type") or ""):
                    return self._json(415, {"error": "写操作要用 application/json"})
            try:
                body = self._body() if method != "GET" else {}
                out = getattr(self, "api_" + name)(q=q, body=body, **args)
            except ApiError as exc:
                return self._json(exc.status, {"error": str(exc)})
            except RuntimeError as exc:
                return self._json(409, {"error": str(exc)})
            if isinstance(out, str):
                return self._text(200, out)
            self._json(200, out)

        # -- 接口 -----------------------------------------------------------------------------------

        def api_overview(self, q, body):
            return app.overview()

        def api_config(self, q, body):
            return app.config_view()

        def api_save_config(self, q, body):
            return {"version": app.save_config(body.get("config") or {}, str(body.get("note") or ""))}

        def api_secret(self, q, body, name):
            return app.set_secret(name, str(body.get("value") or ""))

        def api_wl_export(self, q, body):
            text = str(body.get("csv") or "")
            from .whitelist import WhitelistError, from_csv
            try:
                n = len(from_csv(text))
            except WhitelistError as exc:
                raise ApiError(400, str(exc))
            app.store.kv_set("whitelist.export", text)
            app.store.audit("上传白名单导出文件", "%d 行" % n)
            return {"rows": n}

        def api_scan(self, q, body):
            return app.scan_view()

        def api_start_scan(self, q, body):
            app.start_scan(str(body.get("tag") or ""))
            return {"started": True}

        def api_plan(self, q, body):
            p, target = app.preview(q.get("tag", ""))
            return dict(p.as_dict(), legacy=target.legacy)

        def api_start(self, q, body):
            return {"run": app.start(str(body.get("tag") or ""))}

        def api_run(self, q, body, rid):
            return app.run_view(int(rid))

        def api_logs(self, q, body, rid):
            return app.store.logs(int(rid), after=int(q.get("after") or 0), step=q.get("step", ""))

        def api_diff(self, q, body, rid):
            return app.diff_text(int(rid))

        def api_act(self, q, body, rid, act):
            rid_i = int(rid)
            if act == "pause":
                app.engine.pause(rid_i)
            elif act == "resume":
                app.engine.resume(rid_i)
            else:
                choice = str(body.get("choice") or "")
                if choice not in ("ok", "cancel", "recheck", "wait", "force"):
                    raise ApiError(400, "不认识的选择 %s" % choice)
                app.engine.resolve(rid_i, choice)
            return {"ok": True}

        def api_rollback(self, q, body):
            return {"run": app.rollback(str(body.get("tag") or ""))}

        def api_cluster(self, q, body):
            return app.cluster_view()

        def api_history(self, q, body):
            return app.store.runs(100)

        def api_audit(self, q, body):
            return app.store.audits(300)

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def do_PUT(self):
            self._handle("PUT")

    return Handler


def serve(app: App, host: str, port: int, ready: Callable[[str], None] = print) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((host, port), make_handler(app))
    ready("发版台就绪:http://%s:%d/" % (host, srv.server_address[1]))
    return srv

"""Fuyao 运行模式的测试替身(2026-11-17):假 OpenSandbox(真 HTTP)与假 envd(对象)。

假 OpenSandbox 按平台实测行为写(docs/specs/2026-11-17-fuyao-sandbox-design.md §2):
- 建沙箱要 extensions.sandboxGroup、resourceLimits、非空 entrypoint;沙箱组满了回 503「no available capacity」;
- metadata 原样存、可 PATCH(值为 None 删键)、列举按 metadata 过滤(查询串 `k=v&k2=v2`);
- 暂停后恢复会换地址;`stale_after_resume=True` 时 /endpoints 仍回旧地址(平台的已知问题);
- 删除即时生效;
- 单台查询(GET /v1/sandboxes/{id})不带业务 metadata、列举与单台查询都不带 expiresAt,只有建沙箱的回包带(端到端实测)。
"""
import itertools
import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeOpenSandbox:
    def __init__(self, capacity=None):
        self.boxes = {}                  # id → dict(state, metadata, ip, old_ip, created, expires, group, template)
        self.capacity = dict(capacity or {})
        self.create_error = None            # (状态码, 应答) —— 设了就让建沙箱直接回它(模拟平台把满员包成 500 等)
        self.requests = []               # (method, path, body)
        self.stale_after_resume = False
        self.on_create = None            # 钩子:建完之后调用 on_create(self, box_id)
        self._ids = itertools.count(1)
        self._ips = itertools.count(10)
        self._lock = threading.Lock()
        fake = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _send(self, code, obj=None):
                data = json.dumps(obj).encode() if obj is not None else b""
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n) or b"null") if n else None

            def _route(self, method):
                u = urllib.parse.urlsplit(self.path)
                body = self._body()
                with fake._lock:
                    fake.requests.append((method, u.path, body, dict(self.headers)))
                    code, obj = fake.handle(method, u.path, urllib.parse.parse_qs(u.query), body)
                self._send(code, obj)

            def do_GET(self):
                self._route("GET")

            def do_POST(self):
                self._route("POST")

            def do_PATCH(self):
                self._route("PATCH")

            def do_DELETE(self):
                self._route("DELETE")

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()

    # -- 直接摆状态(测试用) -------------------------------------------------------------------

    def add(self, metadata, state="Running", created=None, group="g-run", template="tpl-run", expires=None):
        bid = "sbx-%d" % next(self._ids)
        now = time.time()
        self.boxes[bid] = {"state": state, "metadata": dict(metadata), "ip": "10.244.9.%d" % next(self._ips),
                           "old_ip": None, "created": created if created is not None else now,
                           "expires": expires if expires is not None else now + 3600, "group": group,
                           "template": template}
        return bid

    def ip(self, bid):
        return self.boxes[bid]["ip"]

    # -- 协议 -----------------------------------------------------------------------------------

    def _json(self, bid, view="list"):
        b = self.boxes[bid]
        iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))      # noqa: E731
        out = {"id": bid, "status": {"state": b["state"]}, "createdAt": iso(b["created"]),
               "metadata": b["metadata"] if view != "get" else {"sandbox.flux.io/sandbox-id": bid}}
        if view == "create" and b["expires"]:
            out["expiresAt"] = iso(b["expires"])
        return out

    def handle(self, method, path, query, body):
        parts = [p for p in path.split("/") if p]          # ['v1','sandboxes',id,...]
        if parts[:2] != ["v1", "sandboxes"]:
            return 404, {"detail": "no route"}
        if len(parts) == 2 and method == "POST":
            return self._create(body or {})
        if len(parts) == 2 and method == "GET":
            want = dict(urllib.parse.parse_qsl((query.get("metadata") or [""])[0]))
            items = [self._json(b) for b, v in self.boxes.items()
                     if all(v["metadata"].get(k) == x for k, x in want.items())]
            page, size = int((query.get("page") or ["1"])[0]), int((query.get("pageSize") or ["100"])[0])
            chunk = items[(page - 1) * size: page * size]
            return 200, {"items": chunk, "pagination": {"page": page, "pageSize": size,
                                                         "hasNextPage": page * size < len(items)}}
        bid = parts[2]
        if bid not in self.boxes:
            return 404, {"detail": {"message": "sandbox not found"}}
        b = self.boxes[bid]
        if len(parts) == 3 and method == "GET":
            return 200, self._json(bid, "get")
        if len(parts) == 3 and method == "DELETE":
            del self.boxes[bid]
            return 204, None
        verb = parts[3]
        if verb == "endpoints":
            ip = b["old_ip"] if (self.stale_after_resume and b["old_ip"]) else b["ip"]
            return 200, {"endpoint": "%s:49983" % ip}
        if verb == "pause" and method == "POST":
            if b["state"] != "Running":
                return 409, {"detail": {"message": "not running"}}
            b["state"] = "Paused"
            return 202, None
        if verb == "resume" and method == "POST":
            if b["state"] != "Paused":
                return 409, {"detail": {"message": "not paused"}}
            b["state"], b["old_ip"], b["ip"] = "Running", b["ip"], "10.244.9.%d" % next(self._ips)
            return 202, None
        if verb == "metadata" and method == "PATCH":
            for k, v in (body or {}).items():
                if v is None:
                    b["metadata"].pop(k, None)
                else:
                    b["metadata"][k] = str(v)
            return 200, self._json(bid)
        return 404, {"detail": "no route"}

    def _create(self, body):
        ext = body.get("extensions") or {}
        if not ext.get("sandboxGroup"):
            return 400, {"detail": {"message": "extensions['sandboxGroup'] is required"}}
        if not ext.get("poolRef") and not body.get("resourceLimits"):
            return 422, {"detail": [{"msg": "resourceLimits is required when poolRef is not provided."}]}
        if not body.get("entrypoint"):
            return 400, {"detail": {"message": "entrypoint required"}}
        if self.create_error is not None:
            return self.create_error
        group = ext["sandboxGroup"]
        # 容量只算运行中的:暂停的沙箱不占承载 Pod 的名额(鲲鹏 2026-11-18 实测)
        used = sum(1 for b in self.boxes.values() if b["group"] == group and b["state"] != "Paused")
        if group in self.capacity and used >= self.capacity[group]:
            return 503, {"detail": {"message": "no available capacity in sandbox group %s" % group}}
        bid = self.add(body.get("metadata") or {}, group=group, template=(body.get("image") or {}).get("uri"),
                       expires=time.time() + int(body["timeout"]) if body.get("timeout") else 0)
        self.boxes[bid]["timeout"] = body.get("timeout")
        if self.on_create is not None:
            self.on_create(self, bid)
        return 202, self._json(bid, "create")


class FakeEnvd:
    """envd 替身:记下每次执行;`launch` 成功后这个地址被视为 opencode 就绪(配合 prober)。"""

    def __init__(self):
        from gateway import sandboxexec
        self._mod = sandboxexec
        self.runs = []                   # (host, argv, user, envs)
        self.ready = set()               # opencode 已就绪的地址
        self.down = set()                # envd 连不上的地址
        self.fail_launch = False
        self.fail_stop = False

    def healthy(self, host):
        return host not in self.down

    def run(self, host, argv, user="root", envs=None, cwd=None, timeout_s=120.0):
        if host in self.down:
            raise self._mod.ExecError("连不上 %s" % host)
        self.runs.append((host, list(argv), user, dict(envs or {})))
        if argv[-1] == "fuyao-start":
            if self.fail_launch:
                return self._mod.Result(2, "", "启动器:载入状态失败\n")
            self.ready.add(host)
            return self._mod.Result(0, "ok\n", "")
        if "fuyao-stop" in argv:
            return self._mod.Result(1 if self.fail_stop else 0, "", "收尾失败\n" if self.fail_stop else "")
        return self._mod.Result(0, "", "")

    def prober(self, host, password):
        return host in self.ready and password.startswith("pw-")

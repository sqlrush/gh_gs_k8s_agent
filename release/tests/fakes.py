"""测试用的假件:最小 Registry v2(进程内 HTTP 服务)与按 package-images.sh 实物格式造的 OCI 包。"""
from __future__ import annotations

import base64
import gzip
import hashlib
import http.server
import io
import json
import re
import tarfile
import threading
import urllib.parse
import uuid
from typing import Dict, List, Optional, Tuple


def digest_of(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


# -- OCI 包 --------------------------------------------------------------------------------------

def make_package(path, arch: str, images: Dict[str, List[bytes]], suffix: bool = True, drop_blob: str = "",
                 attestation: bool = True) -> Dict[str, str]:
    """images: 镜像名 → 各层内容(第一层可在多个镜像间共用)。返回 镜像名 → 平台清单 digest。
    结构照 v1.4.3 实物:顶层 index.json 的命名条目是单架构 index(平台清单 + 证明清单)。"""
    blobs: Dict[str, bytes] = {}
    top, out = [], {}

    def put(data: bytes) -> Tuple[str, int]:
        d = digest_of(data)
        blobs[d] = data
        return d, len(data)

    for name, layers in images.items():
        cfg = json.dumps({"architecture": arch, "os": "linux", "name": name}).encode()
        cd, cs = put(cfg)
        lds = [put(layer) for layer in layers]
        man = json.dumps({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
                          "config": {"mediaType": "application/vnd.oci.image.config.v1+json", "digest": cd, "size": cs},
                          "layers": [{"mediaType": "application/vnd.oci.image.layer.v1.tar+gzip", "digest": d, "size": s}
                                     for d, s in lds]}, sort_keys=True).encode()
        md, ms = put(man)
        out[name] = md
        entries = [{"mediaType": "application/vnd.oci.image.manifest.v1+json", "digest": md, "size": ms,
                    "platform": {"architecture": arch, "os": "linux"}}]
        if attestation:
            ad, asz = put(json.dumps({"schemaVersion": 2, "attestation": name}).encode())
            entries.append({"mediaType": "application/vnd.oci.image.manifest.v1+json", "digest": ad, "size": asz,
                            "platform": {"architecture": "unknown", "os": "unknown"},
                            "annotations": {"vnd.docker.reference.type": "attestation-manifest"}})
        idx = json.dumps({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json",
                          "manifests": entries}).encode()
        idd, ids = put(idx)
        tag = "agent-v1.4.4-oc1.18.27" + ("-" + arch if suffix else "")
        top.append({"mediaType": "application/vnd.oci.image.index.v1+json", "digest": idd, "size": ids,
                    "annotations": {"io.containerd.image.name": "docker.io/library/%s:%s" % (name, tag),
                                    "org.opencontainers.image.ref.name": tag}})
    top.append({"mediaType": "application/vnd.oci.image.manifest.v1+json", "digest": "sha256:" + "0" * 64, "size": 1,
                "annotations": {"io.containerd.manifest.subject": "x"}})       # 实物里有的无名附属条目
    with tarfile.open(path, "w:gz") as tf:
        def add(name: str, data: bytes) -> None:
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
        for d, data in blobs.items():
            if d != drop_blob:
                add("blobs/sha256/" + d.split(":")[1], data)
        add("index.json", json.dumps({"schemaVersion": 2, "manifests": top}).encode())
        add("oci-layout", b'{"imageLayoutVersion":"1.0.0"}')
    return out


# -- Registry v2 -----------------------------------------------------------------------------------

class FakeRegistry:
    """auth: "" / "basic" / "bearer"。mount=False 模拟不支持跨仓库挂载的仓库。"""

    def __init__(self, auth: str = "", user: str = "u", password: str = "p", mount: bool = True):
        self.auth, self.user, self.password, self.allow_mount = auth, user, password, mount
        self.blobs: Dict[str, Dict[str, bytes]] = {}           # repo → digest → 内容
        self.manifests: Dict[Tuple[str, str], Tuple[bytes, str]] = {}
        self.uploads: Dict[str, str] = {}
        self.log: List[Tuple[str, str]] = []
        self.tokens = set()
        self.digest_override: Optional[str] = None
        reg = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_a):
                pass

            def _send(self, code: int, headers: Optional[dict] = None, body: bytes = b""):
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _authed(self) -> bool:
                got = self.headers.get("Authorization", "")
                if reg.auth == "":
                    return True
                if reg.auth == "basic":
                    want = "Basic " + base64.b64encode(("%s:%s" % (reg.user, reg.password)).encode()).decode()
                    if got == want:
                        return True
                    self._send(401, {"WWW-Authenticate": 'Basic realm="fake"'})
                    return False
                if got.startswith("Bearer ") and got[7:] in reg.tokens:
                    return True
                realm = "http://%s:%d/token" % reg.server.server_address
                self._send(401, {"WWW-Authenticate": 'Bearer realm="%s",service="fake"' % realm})
                return False

            def _body(self) -> bytes:
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n) if n else b""

            def _route(self):
                reg.log.append((self.command, self.path))
                path, _, query = self.path.partition("?")
                q = dict(urllib.parse.parse_qsl(query))
                if path == "/token":
                    want = "Basic " + base64.b64encode(("%s:%s" % (reg.user, reg.password)).encode()).decode()
                    if self.headers.get("Authorization") != want:
                        return self._send(401)
                    tok = uuid.uuid4().hex
                    reg.tokens.add(tok)
                    return self._send(200, {"Content-Type": "application/json"}, json.dumps({"token": tok}).encode())
                if not self._authed():
                    return
                if path == "/v2/":
                    return self._send(200)
                m = re.match(r"^/v2/(.+)/blobs/uploads/(\w*)$", path)
                if m:
                    repo, uid = m.group(1), m.group(2)
                    if self.command == "POST":
                        if "mount" in q and reg.allow_mount:
                            src, d = q["from"], q["mount"]
                            if d in reg.blobs.get(src, {}):
                                reg.blobs.setdefault(repo, {})[d] = reg.blobs[src][d]
                                return self._send(201)
                        uid = uuid.uuid4().hex
                        reg.uploads[uid] = repo
                        return self._send(202, {"Location": "/v2/%s/blobs/uploads/%s?_state=x" % (repo, uid)})
                    if self.command == "PUT":
                        data = self._body()
                        d = q.get("digest", "")
                        if digest_of(data) != d:
                            return self._send(400, body=b"digest mismatch")
                        reg.blobs.setdefault(reg.uploads.pop(uid), {})[d] = data
                        return self._send(201)
                    if self.command == "DELETE":
                        reg.uploads.pop(uid, None)
                        return self._send(204)
                m = re.match(r"^/v2/(.+)/blobs/(sha256:[0-9a-f]{64})$", path)
                if m:
                    data = reg.blobs.get(m.group(1), {}).get(m.group(2))
                    return self._send(200 if data is not None else 404)
                m = re.match(r"^/v2/(.+)/manifests/(.+)$", path)
                if m:
                    key = (m.group(1), m.group(2))
                    if self.command == "PUT":
                        data = self._body()
                        reg.manifests[key] = (data, self.headers.get("Content-Type", ""))
                        return self._send(201, {"Docker-Content-Digest": reg.digest_override or digest_of(data)})
                    got = reg.manifests.get(key)
                    if not got:
                        return self._send(404)
                    return self._send(200, {"Docker-Content-Digest": digest_of(got[0]), "Content-Type": got[1]})
                self._send(404)

            do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = _route

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def host(self) -> str:
        return "127.0.0.1:%d" % self.server.server_address[1]

    def count(self, method: str, needle: str) -> int:
        return sum(1 for m, p in self.log if m == method and needle in p)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

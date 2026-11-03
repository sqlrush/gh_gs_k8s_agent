"""把离线包里的镜像推进客户的内网仓库:Registry HTTP API v2,只用标准库。

一个镜像:逐个 blob 先 HEAD(在就跳过)→ 别的仓库推过的尝试跨仓库挂载 → 都不行才上传(POST 拿上传地址,PUT 单次传完);
最后 PUT 平台清单,读回 Docker-Content-Digest 必须等于清单的 sha256。认证按 401 的 WWW-Authenticate 走 Basic 或 Bearer。
"""
from __future__ import annotations

import base64
import http.client
import json
import re
import ssl
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable, Dict, IO, List, Optional, Set, Tuple

from . import oci

CHUNK = 1 << 20
TIMEOUT = 600


class RegistryError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Endpoint:
    """内网仓库。prefix 例如 registry.example.internal/gaussdb-agent(主机[:端口]/项目路径)。"""
    prefix: str
    username: str = ""
    password: str = ""
    plain_http: bool = False            # 只给测试环境的本地仓库用
    insecure: bool = False              # 不校验证书(自签且没给 CA 时);扫描会标黄提示
    ca_file: str = ""

    @property
    def host(self) -> str:
        return self.prefix.split("/", 1)[0]

    @property
    def path(self) -> str:
        return self.prefix.split("/", 1)[1] if "/" in self.prefix else ""

    def repo(self, name: str) -> str:
        return "%s/%s" % (self.path, name) if self.path else name


@dataclass
class PushResult:
    repo: str
    tag: str
    digest: str
    uploaded: int = 0
    mounted: int = 0
    existing: int = 0
    bytes_sent: int = 0


@dataclass
class Client:
    ep: Endpoint
    log: Callable[[str], None] = print
    _token: Dict[str, str] = field(default_factory=dict)       # scope → Bearer 令牌
    _basic: bool = False

    # -- 传输 -----------------------------------------------------------------------------------

    def _conn(self) -> http.client.HTTPConnection:
        if self.ep.plain_http:
            return http.client.HTTPConnection(self.ep.host, timeout=TIMEOUT)
        return http.client.HTTPSConnection(self.ep.host, timeout=TIMEOUT, context=self._conn_ctx())

    def _auth_header(self, scope: str) -> Dict[str, str]:
        if scope in self._token:
            return {"Authorization": "Bearer " + self._token[scope]}
        if self._basic and self.ep.username:
            raw = ("%s:%s" % (self.ep.username, self.ep.password)).encode("utf-8")
            return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}
        return {}

    def _login(self, challenge: str, scope: str) -> None:
        scheme, _, rest = challenge.partition(" ")
        if scheme.lower() == "basic":
            if not self.ep.username:
                raise RegistryError("仓库要求账号密码,资源配置里没填", 401)
            self._basic = True
            return
        if scheme.lower() != "bearer":
            raise RegistryError("不认识的认证方式:%s" % scheme, 401)
        params = dict(re.findall(r'(\w+)="([^"]*)"', rest))
        if not params.get("realm"):
            raise RegistryError("Bearer 认证没有给 realm", 401)
        q = {"service": params.get("service", ""), "scope": params.get("scope") or scope}
        url = urllib.parse.urlsplit(params["realm"])
        conn = (http.client.HTTPConnection(url.netloc, timeout=60) if url.scheme == "http"
                else http.client.HTTPSConnection(url.netloc, timeout=60, context=self._conn_ctx()))
        headers = {}
        if self.ep.username:
            raw = ("%s:%s" % (self.ep.username, self.ep.password)).encode("utf-8")
            headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
        conn.request("GET", "%s?%s" % (url.path or "/", urllib.parse.urlencode(q)), headers=headers)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        if resp.status != 200:
            raise RegistryError("取令牌失败(%d):账号密码或项目权限不对" % resp.status, resp.status)
        doc = json.loads(body or b"{}")
        token = doc.get("token") or doc.get("access_token")
        if not token:
            raise RegistryError("令牌服务没有返回令牌", 401)
        self._token[scope] = token

    def _conn_ctx(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context(cafile=self.ep.ca_file or None)
        if self.ep.insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def request(self, method: str, path: str, scope: str, headers: Optional[Dict[str, str]] = None,
                body: Optional[bytes] = None, stream: Optional[Tuple[IO[bytes], int]] = None
                ) -> Tuple[int, Dict[str, str], bytes]:
        """发一次请求;401 时按质询登录后重发一次(带流的请求在登录前先用 HEAD 探过,不会走到这里重发)。"""
        for attempt in (0, 1):
            h = dict(headers or {}, **self._auth_header(scope))
            conn = self._conn()
            try:
                if stream is not None:
                    src, size = stream
                    conn.putrequest(method, path)
                    for k, v in dict(h, **{"Content-Length": str(size)}).items():
                        conn.putheader(k, v)
                    conn.endheaders()
                    while True:
                        chunk = src.read(CHUNK)
                        if not chunk:
                            break
                        conn.send(chunk)
                else:
                    conn.request(method, path, body=body, headers=h)
                resp = conn.getresponse()
                data = resp.read()
                rh = {k.lower(): v for k, v in resp.getheaders()}
            finally:
                conn.close()
            if resp.status == 401 and attempt == 0 and stream is None:
                self._login(rh.get("www-authenticate", ""), scope)
                continue
            return resp.status, rh, data
        return resp.status, rh, data

    # -- 动作 -----------------------------------------------------------------------------------

    def ping(self) -> None:
        """/v2/ 握手 + 认证。扫描用。"""
        status, _h, body = self.request("GET", "/v2/", "")
        if status != 200:
            raise RegistryError("仓库 /v2/ 返回 %d:%s" % (status, body[:200].decode("utf-8", "replace")), status)

    def has_blob(self, repo: str, digest: str) -> bool:
        status, _h, _b = self.request("HEAD", "/v2/%s/blobs/%s" % (repo, digest), _scope(repo))
        if status in (200, 404):
            return status == 200
        raise RegistryError("查 blob %s 返回 %d" % (digest, status), status)

    def mount(self, repo: str, digest: str, source_repo: str) -> bool:
        q = urllib.parse.urlencode({"mount": digest, "from": source_repo})
        status, h, _b = self.request("POST", "/v2/%s/blobs/uploads/?%s" % (repo, q),
                                     _scope(repo) + " " + _scope(source_repo, "pull"))
        if status == 201:
            return True
        if status == 202:               # 不支持挂载:仓库开了一个上传会话,作废它
            self._cancel(repo, h.get("location", ""))
        return False

    def _cancel(self, repo: str, location: str) -> None:
        if location:
            try:
                self.request("DELETE", _path(location), _scope(repo))
            except (OSError, RegistryError):
                pass

    def upload(self, repo: str, digest: str, size: int, src: IO[bytes]) -> None:
        status, h, body = self.request("POST", "/v2/%s/blobs/uploads/" % repo, _scope(repo))
        if status != 202 or not h.get("location"):
            raise RegistryError("开上传会话失败(%d):%s" % (status, body[:200].decode("utf-8", "replace")), status)
        loc = _path(h["location"])
        sep = "&" if "?" in loc else "?"
        status, _h, body = self.request("PUT", "%s%sdigest=%s" % (loc, sep, urllib.parse.quote(digest)), _scope(repo),
                                        headers={"Content-Type": "application/octet-stream"}, stream=(src, size))
        if status != 201:
            raise RegistryError("上传 %s 失败(%d):%s" % (digest, status, body[:200].decode("utf-8", "replace")), status)

    def put_manifest(self, repo: str, tag: str, manifest: bytes, media_type: str) -> str:
        status, h, body = self.request("PUT", "/v2/%s/manifests/%s" % (repo, tag), _scope(repo),
                                       headers={"Content-Type": media_type}, body=manifest)
        if status != 201:
            raise RegistryError("推清单 %s:%s 失败(%d):%s" % (repo, tag, status, body[:300].decode("utf-8", "replace")),
                                status)
        return h.get("docker-content-digest", "")

    def manifest_digest(self, repo: str, tag: str, media_type: str) -> str:
        status, h, _b = self.request("HEAD", "/v2/%s/manifests/%s" % (repo, tag), _scope(repo, "pull"),
                                     headers={"Accept": media_type})
        if status == 404:
            return ""
        if status != 200:
            raise RegistryError("查清单 %s:%s 返回 %d" % (repo, tag, status), status)
        return h.get("docker-content-digest", "")


def _scope(repo: str, actions: str = "pull,push") -> str:
    return "repository:%s:%s" % (repo, actions)


def _path(location: str) -> str:
    """上传地址可能是绝对 URL,也可能是相对路径;统一成路径 + 查询串。"""
    u = urllib.parse.urlsplit(location)
    return u.path + ("?" + u.query if u.query else "")


def push_images(client: Client, path: str, images: List[oci.Image], tag_of: Callable[[oci.Image], str],
                progress: Callable[[str, int, int], None] = lambda *_a: None) -> List[PushResult]:
    """把一个架构包里的若干镜像推进仓库。先把每个镜像缺哪些 blob 查清楚(能挂载的挂载),再读一遍包统一上传缺的,
    最后逐个推清单并核对 digest。progress(镜像名, 已完成 blob 数, 总 blob 数)。"""
    results: Dict[str, PushResult] = {}
    have: Dict[str, str] = {}            # digest → 已经在哪个仓库里(用于挂载)
    need: Dict[str, List[str]] = {}      # digest → 还缺它的仓库
    for img in images:
        repo = client.ep.repo(img.repo)
        r = results.setdefault(img.repo, PushResult(repo=repo, tag=tag_of(img), digest=img.manifest_digest))
        done = 0
        for b in img.blobs:
            if client.has_blob(repo, b.digest):
                r.existing += 1
                have.setdefault(b.digest, repo)
            elif b.digest in have and client.mount(repo, b.digest, have[b.digest]):
                r.mounted += 1
            else:
                need.setdefault(b.digest, []).append(repo)
                continue
            done += 1
            progress(img.repo, done, len(img.blobs))
    sizes = {b.digest: b.size for img in images for b in img.blobs}
    pending: Set[str] = set(need)

    def consume(digest: str, size: int, src: IO[bytes]) -> None:
        first, *rest = need[digest]
        client.upload(first, digest, size, src)
        results_by_repo(first).uploaded += 1
        results_by_repo(first).bytes_sent += size
        for repo in rest:
            if not client.mount(repo, digest, first):
                raise RegistryError("仓库不支持跨仓库挂载 %s,且同一 blob 不能重复读包:请开启挂载或分开推送" % digest)
            results_by_repo(repo).mounted += 1
        pending.discard(digest)
        for img in images:
            if digest in {b.digest for b in img.blobs}:
                left = sum(1 for b in img.blobs if b.digest in pending)
                progress(img.repo, len(img.blobs) - left, len(img.blobs))

    def results_by_repo(repo: str) -> PushResult:
        return next(r for r in results.values() if r.repo == repo)

    if need:
        client.log("要上传 %d 个 blob,共 %.1f MB" % (len(need), sum(sizes[d] for d in need) / 1048576))
        oci.stream_blobs(path, list(need), consume)
    for img in images:
        r = results[img.repo]
        got = client.put_manifest(r.repo, r.tag, img.manifest, img.media_type)
        if got and got != img.manifest_digest:
            raise RegistryError("%s:%s 推完的 digest %s 与包里的 %s 不一致" % (r.repo, r.tag, got, img.manifest_digest))
        back = client.manifest_digest(r.repo, r.tag, img.media_type)
        if back != img.manifest_digest:
            raise RegistryError("%s:%s 回读 digest %s 与包里的 %s 不一致" % (r.repo, r.tag, back or "(没有)", img.manifest_digest))
    return [results[img.repo] for img in images]

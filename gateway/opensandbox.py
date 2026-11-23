"""OpenSandbox REST 客户端(Fuyao 运行模式,2026-11-17)。只用标准库。

按平台实测写(docs/specs/2026-11-17-fuyao-sandbox-design.md §2):
- 建沙箱:`extensions.sandboxGroup` 指定沙箱组;不带 `extensions.poolRef` 时必须带 `resourceLimits`
  (实际规格由模板决定,这里给的只是平台要的字段);`image.uri` 填 E2B 模板名;
- `env` / `entrypoint` / `volumes` 在 E2B 后端不生效 —— 不发 volumes;entrypoint 平台要求必填,给一个占位;
- `/endpoints/{port}` 不论问哪个端口都回「虚拟机地址:49983」,这里只取地址;
- metadata 落成沙箱对象的标签:可 PATCH、可按 metadata 列举(列举结果再在本地按 metadata 过一遍,
  不依赖平台对多个条件的组合语义)。
"""
from __future__ import annotations

import datetime as dt
import http.client
import json
import urllib.parse
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Tuple

DEFAULT_KEY_HEADER = "OPEN-SANDBOX-API-KEY"
ENTRYPOINT_PLACEHOLDER = ["/bin/sh", "-c", "sleep infinity"]      # E2B 后端不执行;平台校验要求非空
RUNNING, PAUSED = "Running", "Paused"
_CAPACITY_WORDS = ("capacity", "insufficient", "no available", "no idle", "exhausted", "quota", "full")


class OpenSandboxError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class CapacityError(OpenSandboxError):
    """沙箱组没有空位。调用方回等待页,不当成故障。"""


@dataclass(frozen=True)
class Sandbox:
    id: str
    state: str
    metadata: Mapping[str, str] = field(default_factory=dict)
    created_at: float = 0.0
    expires_at: float = 0.0

    @property
    def running(self) -> bool:
        return self.state == RUNNING

    @property
    def paused(self) -> bool:
        return self.state == PAUSED


def _ts(text: object) -> float:
    if not isinstance(text, str) or not text:
        return 0.0
    try:
        t = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.timestamp()


def _iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sandbox(d: Mapping) -> Sandbox:
    st = d.get("status") or {}
    md = {str(k): str(v) for k, v in (d.get("metadata") or {}).items()}
    return Sandbox(id=str(d.get("id") or ""), state=str(st.get("state") or ""), metadata=md,
                   created_at=_ts(d.get("createdAt")), expires_at=_ts(d.get("expiresAt")))


def _detail(body: object) -> str:
    if isinstance(body, dict):
        d = body.get("detail", body.get("message", body))
        if isinstance(d, dict):
            return str(d.get("message") or d)
        return str(d)
    return str(body)


class Client:
    def __init__(self, base_url: str, api_key: str = "", key_header: str = DEFAULT_KEY_HEADER,
                 timeout_s: float = 30.0):
        u = urllib.parse.urlsplit(base_url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ValueError("OpenSandbox 地址要写成 http(s)://主机[:端口]")
        self.scheme, self.host = u.scheme, u.hostname
        self.port = u.port or (443 if u.scheme == "https" else 80)
        self.prefix = u.path.rstrip("/")
        self.api_key, self.key_header, self.timeout_s = api_key, key_header, timeout_s

    # -- 底层 -------------------------------------------------------------------------------------

    def _call(self, method: str, path: str, body: Optional[dict] = None,
              query: Optional[List[Tuple[str, str]]] = None) -> Tuple[int, object]:
        url = self.prefix + path + (("?" + urllib.parse.urlencode(query)) if query else "")
        raw = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Accept": "application/json"}
        if raw is not None:
            headers["Content-Type"] = "application/json"
        if self.api_key:
            headers[self.key_header] = self.api_key
        cls = http.client.HTTPSConnection if self.scheme == "https" else http.client.HTTPConnection
        conn = cls(self.host, self.port, timeout=self.timeout_s)
        try:
            conn.request(method, url, body=raw, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            code = resp.status
        except (OSError, http.client.HTTPException) as exc:
            raise OpenSandboxError("连不上 OpenSandbox(%s:%d):%s" % (self.host, self.port, exc)) from exc
        finally:
            conn.close()
        try:
            return code, json.loads(data) if data else None
        except ValueError:
            return code, data[:300].decode("utf-8", "replace")

    def _ok(self, what: str, code: int, body: object, ok=(200, 201, 202, 204)) -> object:
        if code in ok:
            return body
        msg = "OpenSandbox %s 失败(HTTP %d):%s" % (what, code, _detail(body)[:300])
        low = msg.lower()
        if code in (409, 429, 503) and any(w in low for w in _CAPACITY_WORDS):
            raise CapacityError(msg, code)
        raise OpenSandboxError(msg, code)

    # -- 各个接口 ---------------------------------------------------------------------------------

    def create(self, template: str, group: str, metadata: Mapping[str, str], cpu: str, memory: str,
               timeout_s: int) -> Sandbox:
        body = {"image": {"uri": template}, "entrypoint": ENTRYPOINT_PLACEHOLDER,
                "resourceLimits": {"cpu": cpu, "memory": memory}, "metadata": dict(metadata),
                "extensions": {"sandboxGroup": group}}
        if timeout_s > 0:                    # 不带 timeout = 没有到期时间(实测);带了平台到点直接删,不能续期
            body["timeout"] = int(timeout_s)
        code, out = self._call("POST", "/v1/sandboxes", body)
        out = self._ok("建沙箱", code, out)
        if not isinstance(out, dict) or not out.get("id"):
            raise OpenSandboxError("OpenSandbox 建沙箱的应答里没有 id")
        return _sandbox(out)

    def get(self, sandbox_id: str) -> Optional[Sandbox]:
        code, out = self._call("GET", "/v1/sandboxes/%s" % urllib.parse.quote(sandbox_id, safe=""))
        if code == 404:
            return None
        out = self._ok("查沙箱", code, out, ok=(200,))
        return _sandbox(out) if isinstance(out, dict) else None

    def list(self, metadata: Mapping[str, str], page_size: int = 100, max_pages: int = 50) -> List[Sandbox]:
        want = dict(metadata)
        flt = "&".join("%s=%s" % kv for kv in sorted(want.items()))
        out: List[Sandbox] = []
        for page in range(1, max_pages + 1):
            code, body = self._call("GET", "/v1/sandboxes",
                                    query=[("metadata", flt), ("page", str(page)), ("pageSize", str(page_size))])
            body = self._ok("列举沙箱", code, body, ok=(200,))
            items = (body or {}).get("items") or [] if isinstance(body, dict) else []
            for it in items:
                s = _sandbox(it)
                if s.id and all(s.metadata.get(k) == v for k, v in want.items()):
                    out.append(s)
            pg = (body or {}).get("pagination") or {} if isinstance(body, dict) else {}
            if len(items) < page_size or not pg.get("hasNextPage", len(items) >= page_size):
                break
        return out

    def address(self, sandbox_id: str, port: int = 49983) -> str:
        """虚拟机在集群网络里的地址(只取主机部分)。"""
        code, out = self._call("GET", "/v1/sandboxes/%s/endpoints/%d" % (urllib.parse.quote(sandbox_id, safe=""), port))
        out = self._ok("取访问地址", code, out, ok=(200,))
        ep = str((out or {}).get("endpoint") or "") if isinstance(out, dict) else ""
        host = ep.rsplit(":", 1)[0] if ":" in ep else ep
        if "/" in host or not host:
            raise OpenSandboxError("OpenSandbox 回的访问地址看不懂:%r" % ep)
        return host

    def pause(self, sandbox_id: str) -> None:
        code, out = self._call("POST", "/v1/sandboxes/%s/pause" % urllib.parse.quote(sandbox_id, safe=""), {})
        self._ok("暂停沙箱", code, out)

    def resume(self, sandbox_id: str) -> None:
        code, out = self._call("POST", "/v1/sandboxes/%s/resume" % urllib.parse.quote(sandbox_id, safe=""), {})
        self._ok("恢复沙箱", code, out)

    def delete(self, sandbox_id: str) -> bool:
        """删掉返回 True;本来就不在返回 False。"""
        code, out = self._call("DELETE", "/v1/sandboxes/%s" % urllib.parse.quote(sandbox_id, safe=""))
        if code == 404:
            return False
        self._ok("删除沙箱", code, out)
        return True

    def patch_metadata(self, sandbox_id: str, patch: Mapping[str, Optional[str]]) -> None:
        """JSON Merge Patch:值为 None 表示删掉这个键。"""
        code, out = self._call("PATCH", "/v1/sandboxes/%s/metadata" % urllib.parse.quote(sandbox_id, safe=""),
                               dict(patch))
        self._ok("改沙箱 metadata", code, out)

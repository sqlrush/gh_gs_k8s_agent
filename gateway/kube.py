"""k8s API 的薄封装 —— 只用标准库,网关镜像里不装 kubectl。

用 Pod 自己挂载的 ServiceAccount 凭据(/var/run/secrets/kubernetes.io/serviceaccount)。
需要的权限很小,见 k8s/base/gateway.yaml 里的 Role:本命名空间内 deployments / services /
secrets 的增删查改 + configmaps 只读 + pods / events 只读(用户 Pod 起不来时读原因)。

**这里只做传输,不做策略**。要建什么、什么时候回收,都在 pods.py 与 reaper.py 里,
那两层才好写测试。
"""
from __future__ import annotations

import json
import os
import pathlib
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

SA_DIR = pathlib.Path("/var/run/secrets/kubernetes.io/serviceaccount")
DEFAULT_TIMEOUT = 15


class KubeError(Exception):
    """带上 HTTP 状态码,调用方据此区分「不存在」与「真出错」。"""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class Kube:
    def __init__(self, host: str, token: str, namespace: str,
                 ca_file: Optional[str] = None, timeout: int = DEFAULT_TIMEOUT):
        self.host = host.rstrip("/")
        self.namespace = namespace
        self._token = token
        self._timeout = timeout
        self._ctx = ssl.create_default_context(cafile=ca_file) if ca_file else ssl.create_default_context()

    @classmethod
    def in_cluster(cls, namespace: Optional[str] = None, timeout: int = DEFAULT_TIMEOUT) -> "Kube":
        host = os.environ.get("KUBERNETES_SERVICE_HOST", "")
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        if not host:
            raise KubeError("不在集群里(没有 KUBERNETES_SERVICE_HOST)。网关必须以 Pod 方式运行。")
        token = (SA_DIR / "token").read_text(encoding="utf-8").strip()
        ns = namespace or (SA_DIR / "namespace").read_text(encoding="utf-8").strip()
        return cls("https://%s:%s" % (host, port), token, ns, str(SA_DIR / "ca.crt"), timeout)

    # -- 传输 ---------------------------------------------------------------

    def request(self, method: str, path: str, body: Optional[Any] = None,
                content_type: str = "application/json") -> Dict[str, Any]:
        data = None
        if body is not None:
            data = body.encode("utf-8") if isinstance(body, str) else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self.host + path, data=data, method=method,
            headers={"Authorization": "Bearer " + self._token, "Accept": "application/json",
                     **({"Content-Type": content_type} if data else {})})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout, context=self._ctx) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise KubeError("k8s %s %s → %s:%s" % (method, path, exc.code, detail), exc.code) from exc
        except urllib.error.URLError as exc:
            raise KubeError("连不上 k8s API(%s):%s" % (self.host, exc)) from exc
        return json.loads(raw) if raw.strip() else {}

    # -- 资源 ---------------------------------------------------------------

    def _ns(self, api: str, kind: str, name: str = "") -> str:
        base = "%s/namespaces/%s/%s" % (api, self.namespace, kind)
        return base + ("/" + name if name else "")

    def get(self, api: str, kind: str, name: str) -> Optional[Dict[str, Any]]:
        """不存在返回 None —— 「还没建」是正常状态,不该当异常处理。"""
        try:
            return self.request("GET", self._ns(api, kind, name))
        except KubeError as exc:
            if exc.status == 404:
                return None
            raise

    def list(self, api: str, kind: str, label_selector: str = "", field_selector: str = "") -> Dict[str, Any]:
        path = self._ns(api, kind)
        q = [(k, v) for k, v in (("labelSelector", label_selector), ("fieldSelector", field_selector)) if v]
        if q:
            path += "?" + urllib.parse.urlencode(q)
        return self.request("GET", path)

    def apply(self, api: str, kind: str, name: str, manifest: Dict[str, Any]) -> Dict[str, Any]:
        """server-side apply:幂等,不用先查后建,也不会和别人的字段打架。"""
        path = self._ns(api, kind, name) + "?fieldManager=gaussdb-agent-gateway&force=true"
        return self.request("PATCH", path, manifest, "application/apply-patch+yaml")

    def patch(self, api: str, kind: str, name: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        return self.request("PATCH", self._ns(api, kind, name), patch, "application/merge-patch+json")

    def delete(self, api: str, kind: str, name: str) -> bool:
        """删掉返回 True,本来就没有返回 False。"""
        try:
            self.request("DELETE", self._ns(api, kind, name))
            return True
        except KubeError as exc:
            if exc.status == 404:
                return False
            raise

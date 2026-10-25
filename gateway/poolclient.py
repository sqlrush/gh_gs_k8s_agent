"""网关调各 Pod 管家控制接口(4098)的客户端(固定池化,2026-10-23)。

直连 Pod IP(不走 headless Service 的 DNS:某个槽位重启时整个 Pod NotReady,DNS 记录会消失,P0 摸底 S2)。
请求按 poolauth 签名、应答验签 —— 验不过的应答当成连不上:管家重启的空档里同 Pod 的槽位可能抢了 4098 冒充它。
GRMP 令牌在请求体里加密;口令与报告令牌只出现在 bind / resume 的应答里,不记日志。
"""
from __future__ import annotations

import http.client
import json
from typing import List, Optional, Tuple

from . import poolauth

STEWARD_PORT = 4098
Reply = Tuple[int, dict]


class StewardError(Exception):
    """连不上、超时、应答没有管家的签名。调用方把这个 Pod 当成暂时不可用。"""


class StewardClient:
    def __init__(self, key: bytes, port: int = STEWARD_PORT, timeout_s: float = 5.0, op_timeout_s: float = 150.0):
        if len(key) < 16:
            raise ValueError("控制密钥至少 16 字节")
        self.key, self.port, self.timeout_s, self.op_timeout_s = key, port, timeout_s, op_timeout_s

    def call(self, pod: str, ip: str, method: str, path: str, body: Optional[dict] = None,
             timeout: Optional[float] = None) -> Reply:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else b""
        hdrs = poolauth.sign_request(self.key, method, path, pod, raw)
        hdrs["Content-Type"] = "application/json"
        conn = http.client.HTTPConnection(ip, self.port, timeout=timeout or self.timeout_s)
        try:
            conn.request(method, path, body=raw if method == "POST" else None, headers=hdrs)
            resp = conn.getresponse()
            data = resp.read()
            sig = resp.getheader(poolauth.HDR_RESP_SIG) or ""
            code = resp.status
        except (OSError, http.client.HTTPException) as exc:
            raise StewardError("连不上 %s 的管家(%s:%d):%s" % (pod, ip, self.port, exc)) from exc
        finally:
            conn.close()
        if not poolauth.check_response(self.key, pod, hdrs[poolauth.HDR_NONCE], code, data, sig):
            raise StewardError("%s 的应答没有管家的签名(%d)" % (pod, code))
        try:
            out = json.loads(data or b"{}")
        except ValueError as exc:
            raise StewardError("%s 的应答不是 JSON" % pod) from exc
        return code, out if isinstance(out, dict) else {}

    # -- 各个指令 --------------------------------------------------------------------------------

    def status(self, pod: str, ip: str) -> dict:
        code, out = self.call(pod, ip, "GET", "/pool/status")
        if code != 200:
            raise StewardError("%s 的 status 回了 %d" % (pod, code))
        return out

    def _seal(self, grmp_token: str) -> dict:
        return {"grmp_token": poolauth.seal(self.key, grmp_token)} if grmp_token else {}

    def bind(self, pod: str, ip: str, slot: int, user: str, grmp_token: str, expect_in_use: int,
             container_id: str) -> Reply:
        body = dict({"slot": slot, "user_id": user, "expect_in_use": expect_in_use, "container_id": container_id},
                    **self._seal(grmp_token))
        return self.call(pod, ip, "POST", "/pool/bind", body, timeout=self.op_timeout_s)

    def resume(self, pod: str, ip: str, slot: int, user: str, grmp_token: str, expect_in_use: int,
               container_id: str) -> Reply:
        body = dict({"slot": slot, "user_id": user, "expect_in_use": expect_in_use, "container_id": container_id},
                    **self._seal(grmp_token))
        return self.call(pod, ip, "POST", "/pool/resume", body, timeout=self.op_timeout_s)

    def retain(self, pod: str, ip: str, slot: int, user: str) -> Reply:
        return self.call(pod, ip, "POST", "/pool/retain", {"slot": slot, "user_id": user}, timeout=self.op_timeout_s)

    def evict(self, pod: str, ip: str, slot: int, container_id: str) -> Reply:
        return self.call(pod, ip, "POST", "/pool/evict", {"slot": slot, "container_id": container_id})

    def verify(self, pod: str, ip: str, slot: int, container_id: str) -> Reply:
        return self.call(pod, ip, "POST", "/pool/verify", {"slot": slot, "container_id": container_id}, timeout=30)

    def reserve(self, pod: str, ip: str, slot: int, user: str, seconds: float) -> Reply:
        return self.call(pod, ip, "POST", "/pool/reserve", {"slot": slot, "user_id": user, "seconds": seconds})

    def disable(self, pod: str, ip: str, slot: int, reason: str) -> Reply:
        return self.call(pod, ip, "POST", "/pool/disable", {"slot": slot, "reason": reason})

    def touch(self, pod: str, ip: str, items: List[list]) -> Reply:
        return self.call(pod, ip, "POST", "/pool/touch", {"activity": items})

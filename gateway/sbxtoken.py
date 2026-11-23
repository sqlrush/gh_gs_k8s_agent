"""沙箱令牌(Fuyao 运行模式,2026-11-17):网关签发,状态服务与凭据代理服务校验。

令牌自带「沙箱 ID、工号、环境种类」,用控制密钥(Secret sandbox-control,与固定池化同一把)派生的子密钥签名。
两个集群服务各自持有同一把控制密钥,就能独立核对,不需要网关逐个登记、也不需要它们之间同步状态。

为什么不用登记表:网关两个副本、两个集群服务、重启与扩缩都要保持一致,登记表要么放共享存储、要么每处各记一份;
签名令牌只要密钥一致就成立。代价是不能单独吊销某一张 —— 令牌只存在于它那台沙箱里,沙箱删除即随之消失,
被暂停的沙箱恢复后继续有效(本来就该有效)。

与 docker/sbxtoken.py 一字不差(守卫测试核对):runtime 镜像里的两个集群服务用那一份。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Optional

PREFIX = "st1."
MAX_LEN = 1024
_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_USER = re.compile(r"^[a-z0-9]([a-z0-9-]{0,52}[a-z0-9])?$")       # 与 gateway/identity.py 的工号规则一致
KINDS = ("runtime", "kb-import")


@dataclass(frozen=True)
class Claims:
    sandbox_id: str
    user_id: str
    kind: str
    issued_at: int


def _subkey(key: bytes) -> bytes:
    return hmac.new(key, b"sandbox-token\n", hashlib.sha256).digest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode((text + "=" * (-len(text) % 4)).encode("ascii"))


def issue(key: bytes, sandbox_id: str, user_id: str, kind: str, now: Optional[float] = None) -> str:
    if len(key) < 16:
        raise ValueError("控制密钥至少 16 字节")
    if not _ID.match(sandbox_id) or not _USER.match(user_id) or kind not in KINDS:
        raise ValueError("沙箱 ID / 工号 / 种类不合法")
    body = json.dumps({"s": sandbox_id, "u": user_id, "k": kind, "t": int(time.time() if now is None else now)},
                      separators=(",", ":"), sort_keys=True).encode("utf-8")
    payload = _b64(body)
    sig = hmac.new(_subkey(key), payload.encode("ascii"), hashlib.sha256).hexdigest()
    return PREFIX + payload + "." + sig


def verify(key: bytes, token: object) -> Optional[Claims]:
    """签名对、字段合法返回 Claims;否则 None(不说原因)。"""
    if not isinstance(token, str) or not token.startswith(PREFIX) or len(token) > MAX_LEN:
        return None
    rest = token[len(PREFIX):]
    if rest.count(".") != 1:
        return None
    payload, sig = rest.split(".")
    want = hmac.new(_subkey(key), payload.encode("ascii", "replace"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want, sig):
        return None
    try:
        d = json.loads(_unb64(payload))
        c = Claims(sandbox_id=str(d["s"]), user_id=str(d["u"]), kind=str(d["k"]), issued_at=int(d["t"]))
    except (ValueError, KeyError, TypeError):
        return None
    if not _ID.match(c.sandbox_id) or not _USER.match(c.user_id) or c.kind not in KINDS:
        return None
    return c


def sandbox_of(token: object) -> Optional[str]:
    """**不验签**读出令牌声明里的沙箱 ID。只用于比对:沙箱里的启动器没有密钥,用它发现「被叫到了别的沙箱的
    虚拟机里」(2026-11-18)。不能用来授权 —— 授权一律走 verify。"""
    if not isinstance(token, str) or not token.startswith(PREFIX) or len(token) > MAX_LEN:
        return None
    rest = token[len(PREFIX):]
    if rest.count(".") != 1:
        return None
    try:
        sid = str(json.loads(_unb64(rest.split(".")[0]))["s"])
    except (ValueError, KeyError, TypeError):
        return None
    return sid if _ID.match(sid) else None


def reports_token(key: bytes, sandbox_id: str) -> str:
    """报告端口(4097)的令牌:按沙箱派生,网关两个副本算出来一样,不用存。"""
    return hmac.new(_subkey(key), ("reports\n" + sandbox_id).encode("utf-8"), hashlib.sha256).hexdigest()[:48]


def bearer(header: Optional[str]) -> str:
    """从 Authorization 头里取 Bearer 令牌;没有返回空串。"""
    h = (header or "").strip()
    return h[7:].strip() if h[:7].lower() == "bearer " else ""


# -- 网关专用的运维请求签名(2026-11-18,资源大盘) ------------------------------------------------------
# 状态服务的统计接口(请求计数、沙箱台数、NAS 用量)只许网关调,沙箱不能调。沙箱手里只有沙箱令牌,这里用控制密钥
# 派生的**另一把**子密钥签名,签的是「方法 + 路径 + 时刻 + 随机数 + 请求体摘要」:沙箱令牌不管真假都过不了这道校验
# (子密钥不同、格式也不同),截获的签名换个接口或改了请求体就失效,时刻窗口之外也失效;
# 窗口内的重放由校验方记住见过的签名挡掉(OpsReplay)。控制密钥从不进沙箱。

OPS_HEADER = "X-Gsdb-Gateway-Sig"
OPS_PREFIX = "go1."
OPS_WINDOW_S = 120


def _ops_subkey(key: bytes) -> bytes:
    return hmac.new(key, b"gateway-ops\n", hashlib.sha256).digest()


def _ops_mac(key: bytes, method: str, path: str, t: int, nonce: str, body: bytes) -> str:
    msg = "%s\n%s\n%d\n%s\n%s" % (method.upper(), path, t, nonce, hashlib.sha256(body or b"").hexdigest())
    return hmac.new(_ops_subkey(key), msg.encode("utf-8"), hashlib.sha256).hexdigest()


def ops_sign(key: bytes, method: str, path: str, body: bytes = b"", now: Optional[float] = None) -> str:
    """带随机数:两个网关副本同一秒对同一路径签名也各不相同(防重放按整个签名记)。"""
    if len(key) < 16:
        raise ValueError("控制密钥至少 16 字节")
    t = int(time.time() if now is None else now)
    nonce = os.urandom(8).hex()
    return "%s%d.%s.%s" % (OPS_PREFIX, t, nonce, _ops_mac(key, method, path, t, nonce, body))


def ops_verify(key: bytes, header: object, method: str, path: str, body: bytes = b"",
               now: Optional[float] = None, window: int = OPS_WINDOW_S) -> bool:
    """签名对、时刻在窗口内返回 True;否则 False(不说原因)。"""
    if not isinstance(header, str) or not header.startswith(OPS_PREFIX) or len(header) > 200:
        return False
    parts = header[len(OPS_PREFIX):].split(".")
    if len(parts) != 3 or not parts[0].isdigit() or not re.match(r"^[0-9a-f]{16}$", parts[1]) or not parts[2]:
        return False
    t = int(parts[0])
    if abs((time.time() if now is None else now) - t) > window:
        return False
    return hmac.compare_digest(_ops_mac(key, method, path, t, parts[1], body), parts[2])


class OpsReplay:
    """记住窗口内见过的签名:同一个签名只认一次(两个网关副本各签各的,不会撞)。"""

    def __init__(self, window: int = OPS_WINDOW_S):
        self.window = window
        self._seen: dict = {}
        self._lock = threading.Lock()

    def first(self, header: str, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        with self._lock:
            if len(self._seen) > 4096:
                self._seen = {k: v for k, v in self._seen.items() if now - v <= 2 * self.window}
            if header in self._seen and now - self._seen[header] <= 2 * self.window:
                return False
            self._seen[header] = now
            return True

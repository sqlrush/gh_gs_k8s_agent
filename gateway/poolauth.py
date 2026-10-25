"""网关 ↔ 管家控制接口的签名(固定池化,2026-10-23)。

**这个文件有两份,一字不差**:docker/poolauth.py(管家用)与 gateway/poolauth.py(网关用),
测试核对两份相同。改一份就要同步另一份。

**为什么不再直接传控制密钥。** v1.2.0 把密钥放在请求头里。固定池化下同一个 Pod 里有 8 个槽位共用 localhost:
管家重启的空档,某个槽位可以抢先监听 4098 冒充管家(P0 摸底 S3),网关发来的请求连同密钥就落到了它手里。
签名之后,截到的只是对某一个请求的签名:

  · 签名内容 = 方法、路径、目标 Pod 名、时间戳、一次性随机数、请求体摘要 —— 换路径、换 Pod、改请求体都对不上;
  · 时间戳超出窗口(默认 60 秒)拒绝;同一个随机数窗口内只认一次 —— 重放不了;
  · 应答也签名(用请求的随机数),冒充者给不出 —— 网关不会把冒充者的回答当真。

**请求体里的秘密另外加密(seal)。** 签名只防篡改、不防偷看:冒充者收到的 bind / resume 请求里有这个人的
GRMP 令牌。所以令牌字段由网关 seal 成密文,管家 unseal。只用标准库:HMAC-SHA256 当伪随机函数做计数器模式
的密钥流(先加密后认证,加密与认证各用从控制密钥派生出的一把子密钥);每次 seal 一个新的随机数。

密钥本身只在网关与管家的环境变量里(Secret sandbox-control),槽位容器里没有。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from typing import Dict, Mapping, Optional

HDR_TS = "X-Pool-Ts"
HDR_NONCE = "X-Pool-Nonce"
HDR_SIG = "X-Pool-Sig"
HDR_RESP_SIG = "X-Pool-Resp-Sig"
WINDOW_S = 60
MAX_NONCES = 20000
SEAL_PREFIX = "s1."
SEAL_MAX = 4096                 # 明文上限(令牌几百字节)
_SEAL_NONCE = 16
_SEAL_TAG = 16


def _mac(key: bytes, *parts: str) -> str:
    return hmac.new(key, "\n".join(parts).encode("utf-8"), hashlib.sha256).hexdigest()


def _body_hash(body: bytes) -> str:
    return hashlib.sha256(body or b"").hexdigest()


def sign_request(key: bytes, method: str, path: str, pod: str, body: bytes = b"",
                 now: Optional[float] = None, nonce: Optional[str] = None) -> Dict[str, str]:
    """返回要加的请求头。pod 是目标 Pod 名(签进去,换个 Pod 就对不上)。"""
    ts = "%d" % int(time.time() if now is None else now)
    nonce = nonce or secrets.token_hex(16)
    return {HDR_TS: ts, HDR_NONCE: nonce,
            HDR_SIG: _mac(key, "REQ", method.upper(), path, pod, ts, nonce, _body_hash(body))}


def sign_response(key: bytes, pod: str, nonce: str, status: int, body: bytes = b"") -> str:
    return _mac(key, "RESP", pod, nonce, "%d" % int(status), _body_hash(body))


def check_response(key: bytes, pod: str, nonce: str, status: int, body: bytes, sig: str) -> bool:
    return hmac.compare_digest(sign_response(key, pod, nonce, status, body), sig or "")


class Verifier:
    """管家一侧:核对请求签名、时间窗口与随机数是否用过。线程安全由调用方保证(管家用一把锁)。"""

    def __init__(self, key: bytes, pod: str, window_s: int = WINDOW_S, max_nonces: int = MAX_NONCES):
        if len(key) < 16:
            raise ValueError("控制密钥至少 16 字节")
        self.key, self.pod, self.window_s, self.max_nonces = key, pod, window_s, max_nonces
        self._seen: Dict[str, float] = {}

    def check(self, method: str, path: str, headers: Mapping[str, str], body: bytes = b"",
              now: Optional[float] = None) -> Optional[str]:
        """通过返回随机数(给应答签名用);不通过返回 None —— 不说原因,与 v1.2.0 的「401 {}」一致。"""
        now = time.time() if now is None else now
        ts, nonce, sig = headers.get(HDR_TS) or "", headers.get(HDR_NONCE) or "", headers.get(HDR_SIG) or ""
        try:
            t = int(ts)
        except ValueError:
            return None
        if abs(now - t) > self.window_s or not (16 <= len(nonce) <= 128):
            return None
        want = _mac(self.key, "REQ", method.upper(), path, self.pod, ts, nonce, _body_hash(body))
        if not hmac.compare_digest(want, sig):
            return None
        self._forget(now)
        if nonce in self._seen:
            return None
        self._seen[nonce] = now
        return nonce

    def _forget(self, now: float) -> None:
        if len(self._seen) < self.max_nonces // 2:
            cutoff = now - 2 * self.window_s
            for n in [n for n, t in self._seen.items() if t < cutoff]:
                del self._seen[n]
            return
        # 超量(被灌了大量合法签名的请求 —— 只有持密钥的人能做到):按时间留新的一半
        keep = sorted(self._seen.items(), key=lambda kv: kv[1])[-(self.max_nonces // 2):]
        self._seen = dict(keep)


# --- 请求体里的秘密字段 ---------------------------------------------------------------

def _subkey(key: bytes, label: bytes) -> bytes:
    return hmac.new(key, b"pool-seal\n" + label, hashlib.sha256).digest()


def _keystream(key: bytes, nonce: bytes, n: int) -> bytes:
    k = _subkey(key, b"enc")
    out = bytearray()
    ctr = 0
    while len(out) < n:
        out += hmac.new(k, nonce + ctr.to_bytes(8, "big"), hashlib.sha256).digest()
        ctr += 1
    return bytes(out[:n])


def _seal_tag(key: bytes, nonce: bytes, ct: bytes) -> bytes:
    return hmac.new(_subkey(key, b"mac"), nonce + ct, hashlib.sha256).digest()[:_SEAL_TAG]


def seal(key: bytes, plaintext: str) -> str:
    data = plaintext.encode("utf-8")
    if len(data) > SEAL_MAX:
        raise ValueError("要加密的内容超过 %d 字节" % SEAL_MAX)
    nonce = secrets.token_bytes(_SEAL_NONCE)
    ct = bytes(a ^ b for a, b in zip(data, _keystream(key, nonce, len(data))))
    return SEAL_PREFIX + base64.urlsafe_b64encode(nonce + ct + _seal_tag(key, nonce, ct)).decode("ascii")


def unseal(key: bytes, token: object) -> Optional[str]:
    """解不开(格式不对、被改过、密钥不对)返回 None。"""
    if not isinstance(token, str) or not token.startswith(SEAL_PREFIX) or len(token) > 2 * SEAL_MAX:
        return None
    try:
        raw = base64.urlsafe_b64decode(token[len(SEAL_PREFIX):].encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        return None
    if len(raw) < _SEAL_NONCE + _SEAL_TAG:
        return None
    nonce, ct, tag = raw[:_SEAL_NONCE], raw[_SEAL_NONCE:-_SEAL_TAG], raw[-_SEAL_TAG:]
    if not hmac.compare_digest(_seal_tag(key, nonce, ct), tag):
        return None
    try:
        return bytes(a ^ b for a, b in zip(ct, _keystream(key, nonce, len(ct)))).decode("utf-8")
    except UnicodeDecodeError:
        return None

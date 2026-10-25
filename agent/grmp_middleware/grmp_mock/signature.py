"""模拟客户 2026-09-18 起的中间件加固:在 auth 之外校验 Appkey / Timestamp / Signature。

三道门对应客户的说法:调用方范围(Appkey 登记过)、重放(Timestamp 在窗口内)、上下文根签名
(Signature 是该 Appkey 私钥对「路径 + 时间戳」的 SM2 签名)。校验参数与 common/grmp/signing.py
的旋钮一一对应,mock 启动时按客户最终口径配,skill 侧配一样的值,对不上就在本地先暴露。
真实 GRMP 校验失败的响应形态未知(【缺】),这里沿用 mock 的业务错误模型并在 msg 里注明。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Tuple

from common.grmp import sm2

DEFAULT_WINDOW_SECONDS = 300


@dataclass(frozen=True)
class SignaturePolicy:
    appkey: str
    public_key: Optional[Tuple[int, int]]   # require_signature=False 时可以没有
    user_id: bytes = sm2.DEFAULT_USER_ID
    timestamp_unit: str = "ms"
    signature_format: str = "raw"
    signature_encoding: str = "hex"
    payload: str = "{path}+{timestamp}"
    window_seconds: int = DEFAULT_WINDOW_SECONDS
    # 2026-09-20 客户过渡期:密钥审批未下来,中间件先只校验 Appkey 与 Timestamp。
    # 默认仍是 True —— 密钥到位后不改这里就自动是全校验。
    require_signature: bool = True

    def check(self, path: str, headers: Mapping[str, str], now: Optional[float] = None) -> Optional[str]:
        """返回拒绝原因;None = 通过。头名不区分大小写。"""
        lower = {k.lower(): v for k, v in headers.items()}
        required = ("appkey", "timestamp", "signature") if self.require_signature else ("appkey", "timestamp")
        for name in required:
            if not lower.get(name):
                return "缺少 %s 请求头" % name.capitalize()
        if lower["appkey"] != self.appkey:
            return "Appkey %r 不在允许的调用方范围内" % lower["appkey"]
        ts_text = lower["timestamp"]
        if not ts_text.isdigit():
            return "Timestamp %r 不是整数" % ts_text
        ts = int(ts_text) / (1000.0 if self.timestamp_unit == "ms" else 1.0)
        skew = abs((now if now is not None else time.time()) - ts)
        if skew > self.window_seconds:
            return "Timestamp 偏离当前时间 %.0f 秒,超出 ±%d 秒窗口(疑似重放或时钟不同步)" % (skew, self.window_seconds)
        if not self.require_signature:
            return None                 # 过渡期:调用方范围与重放这两道门照旧,只是不验签
        try:
            r, s = sm2.decode_signature(sm2.from_text(lower["signature"], self.signature_encoding), self.signature_format)
        except sm2.Sm2Error as exc:
            return "Signature 不是合法的 %s/%s:%s" % (self.signature_encoding, self.signature_format, exc)
        message = self.payload.format(path=path, timestamp=ts_text).encode("utf-8")
        if not sm2.verify(self.public_key, message, self.user_id, r, s):
            return "Signature 校验失败(上下文根、时间戳、userId 或密钥对不匹配)"
        return None


def policy_from_args(appkey: str, public_key_text: str, user_id: Optional[str], timestamp_unit: str,
                     signature_format: str, signature_encoding: str, payload: str, window: int,
                     read_file: Callable[[str], str] = lambda p: open(p, encoding="utf-8").read(),
                     require_signature: bool = True) -> SignaturePolicy:
    """命令行参数 → 策略。public_key_text 以 @ 开头时读文件(PEM 多行不适合放命令行)。

    require_signature=False 是过渡期档位:不验签,也就不需要公钥。
    """
    public_key = None
    if require_signature or public_key_text:
        text = read_file(public_key_text[1:]) if public_key_text.startswith("@") else public_key_text
        public_key = sm2.parse_public_key(text)
    return SignaturePolicy(
        appkey=appkey, public_key=public_key,
        user_id=(user_id if user_id is not None else sm2.DEFAULT_USER_ID.decode("ascii")).encode("utf-8"),
        timestamp_unit=timestamp_unit, signature_format=signature_format, signature_encoding=signature_encoding,
        payload=payload, window_seconds=window, require_signature=require_signature,
    )

"""GRMP 请求签名(客户 2026-09-18 的中间件加固)。

每个请求在 `auth` 令牌之外再带三个头:
    Appkey     调用方应用名——整个智能体一个,客户约定
    Timestamp  调用时的时间戳
    Signature  用该 Appkey 的 SM2 私钥对「URL 上下文根 + 时间戳」的签名
分工:这三个认应用、防重放、防改路径。`auth` 认的是「调用方」——**令牌按不按人签发,客户
未明说**,现有材料里数据库侧只见到单一「中间件执行账号」。工号由 `common/grmp/userid.py`
按客户给的字段单独发(2026-09-20 确认要),不要把身份寄托在这三个头上。

设置来源:环境变量 > config.yaml 的 api_connection;没配 Appkey 就不签(中间件还没开校验时照常可用)。
私钥:环境变量 GRMP_SM2_PRIVATE_KEY > 凭据文件(默认名 grmp-sm2,`credential_cli set` 存入);不落配置文件。
签名细节——时间戳单位、签名格式、编码、userId、原文拼法——客户那边每一项都可能不同,全部可配,
对不上不用改代码。默认取国标 / Java(BouncyCastle)最常见的形态。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Dict, Mapping, Optional

from .. import credential
from ..config import ApiEndpoint, ConfigError
from . import sm2

HEADER_APPKEY = "Appkey"
HEADER_TIMESTAMP = "Timestamp"
HEADER_SIGNATURE = "Signature"

ENV_APPKEY = "GRMP_APPKEY"
ENV_PRIVATE_KEY = "GRMP_SM2_PRIVATE_KEY"
ENV_USER_ID = "GRMP_SIGN_USER_ID"
ENV_PAYLOAD = "GRMP_SIGN_PAYLOAD"
DEFAULT_CREDENTIAL = "grmp-sm2"
DEFAULT_PAYLOAD = "{path}+{timestamp}"       # 客户给的例子:"/v1/validateByNorth/validate+timestamp"

MODE_FULL = "full"
MODE_HEADERS_ONLY = "headers-only"

# 配置键 → (环境变量, 允许值, 默认值)
_KNOBS = {
    "timestamp": ("GRMP_SIGN_TIMESTAMP", ("ms", "s"), "ms"),
    "format": ("GRMP_SIGN_FORMAT", ("raw", "der"), "raw"),
    "encoding": ("GRMP_SIGN_ENCODING", ("hex", "base64"), "hex"),
    # 2026-09-20 客户过渡期:密钥审批没下来,先只发 Appkey + Timestamp。
    # 必须显式配 —— 不做「拿不到私钥就自动降级」,那样生产上 Secret 挂载失败会悄悄不签名。
    "mode": ("GRMP_SIGN_MODE", (MODE_FULL, MODE_HEADERS_ONLY), MODE_FULL),
}


@dataclass(frozen=True)
class SignSettings:
    appkey: str
    private_key: Optional[int]              # SM2 私钥 d;headers-only 档为 None
    mode: str = MODE_FULL                   # full=三个头;headers-only=只 Appkey + Timestamp
    user_id: bytes = sm2.DEFAULT_USER_ID    # 国标默认;OpenSSL 默认是空串,客户用什么必须问
    timestamp_unit: str = "ms"
    signature_format: str = "raw"           # raw = r‖s 64 字节;der = ASN.1(Java 默认)
    signature_encoding: str = "hex"
    payload: str = DEFAULT_PAYLOAD


class RequestSigner:
    """每个请求现算三个头。clock 只在测试里替换。"""

    def __init__(self, settings: SignSettings, clock: Callable[[], float] = time.time):
        self.settings = settings
        self._clock = clock

    def timestamp(self) -> str:
        now = self._clock()
        return str(int(now * 1000)) if self.settings.timestamp_unit == "ms" else str(int(now))

    def payload_for(self, path: str, timestamp: str) -> str:
        return self.settings.payload.format(path=path, timestamp=timestamp)

    def headers(self, path: str) -> Dict[str, str]:
        st = self.settings
        ts = self.timestamp()
        headers = {HEADER_APPKEY: st.appkey, HEADER_TIMESTAMP: ts}
        if st.mode == MODE_HEADERS_ONLY:
            return headers                  # 过渡期:客户密钥还没批下来
        r, s = sm2.sign(st.private_key, self.payload_for(path, ts).encode("utf-8"), st.user_id)
        signature = sm2.to_text(sm2.encode_signature(r, s, st.signature_format), st.signature_encoding)
        return {**headers, HEADER_SIGNATURE: signature}


def settings_from(
    env: Mapping[str, str],
    endpoint: Optional[ApiEndpoint],
    load_secret: Callable[[str], str] = credential.load_secret,
) -> Optional[SignSettings]:
    """解析签名设置;返回 None 表示没配 Appkey、不签。配置错误一律 ConfigError,报得能看懂。"""
    cfg: Mapping[str, object] = endpoint.sign if endpoint is not None else {}
    appkey_env = endpoint.appkey_env if endpoint is not None else ENV_APPKEY
    appkey = env.get(appkey_env, "") or (endpoint.appkey if endpoint is not None else "")
    if not appkey:
        return None

    def knob(name: str) -> str:
        env_name, allowed, default = _KNOBS[name]
        value = env.get(env_name, "") or str(cfg.get(name) or "") or default
        if value not in allowed:
            raise ConfigError("签名参数 %s=%r 不认识,只能是 %s" % (name, value, " / ".join(allowed)))
        return value

    # 先定档位:headers-only 不读私钥,免得过渡期还要求平台挂一个用不上的 Secret
    mode = knob("mode")
    private_key: Optional[int] = None
    if mode == MODE_FULL:
        key_env = str(cfg.get("key_env") or ENV_PRIVATE_KEY)
        cred = str(cfg.get("credential") or DEFAULT_CREDENTIAL)
        key_text = env.get(key_env, "")
        if not key_text:
            try:
                key_text = load_secret(cred)
            except credential.CredentialError as exc:
                raise ConfigError(
                    "配置了 Appkey %s 但拿不到 SM2 私钥:环境变量 %s 未设置,凭据 %s 也读不到(%s)。"
                    "非容器部署先 python3 -m common.credential_cli set %s 存入;容器部署由平台以 Secret 注入 %s。"
                    "若中间件当前只校验 Appkey 与 Timestamp(密钥尚未签发),请显式设 %s=%s——"
                    "本层不会因为拿不到私钥就自动降级,降级必须是配置里写明的决定。"
                    % (appkey, key_env, cred, exc, cred, key_env,
                       _KNOBS["mode"][0], MODE_HEADERS_ONLY)) from exc
        try:
            private_key = sm2.parse_private_key(key_text)
        except sm2.Sm2Error as exc:
            raise ConfigError("SM2 私钥格式不对:%s" % exc) from exc

    payload = env.get(ENV_PAYLOAD, "") or str(cfg.get("payload") or "") or DEFAULT_PAYLOAD
    try:
        payload.format(path="/p", timestamp="0")
    except (KeyError, IndexError, ValueError) as exc:
        raise ConfigError("签名原文模板 %r 只能用 {path} 与 {timestamp}:%s" % (payload, exc)) from exc

    # userId 允许是空串(OpenSSL 的默认),所以要区分「没配」和「配成空」
    if ENV_USER_ID in env:
        user_id = env[ENV_USER_ID]
    elif "user_id" in cfg:
        user_id = str(cfg.get("user_id") if cfg.get("user_id") is not None else "")
    else:
        user_id = sm2.DEFAULT_USER_ID.decode("ascii")

    return SignSettings(
        appkey=appkey, private_key=private_key, mode=mode, user_id=user_id.encode("utf-8"),
        timestamp_unit=knob("timestamp"), signature_format=knob("format"),
        signature_encoding=knob("encoding"), payload=payload,
    )

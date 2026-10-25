"""过渡期:只带 Appkey + Timestamp,先不带 Signature(客户 2026-09-20)。

客户的要求:密钥审批周期紧,先在调用 api 时请求头增加 Appkey 和 Timestamp 字段;Appkey 用中间件给的值,
Timestamp 用系统时间戳,单位秒,例如 1758431954。

原先只有两档——没配 Appkey 就什么都不发,配了 Appkey 就**必须**有 SM2 私钥,拿不到直接报错。
中间没有档位,而客户要的正好是中间那一档。

**为什么用显式开关而不是「没私钥就自动降级」**:生产上 Secret 挂载失败、私钥文件权限不对,
都会让「全签」悄悄变成「不签」。中间件开了校验就整体拒绝(至少还能看出来),没开的话就是
安全等级被默默降了一级而没人知道。降级必须是配置里写明的决定。
"""
import pathlib
import re
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common.config import ConfigError                       # noqa: E402
from common.grmp import signing, sm2                        # noqa: E402
from grmp_middleware.grmp_mock import signature as mocksig  # noqa: E402

D = sm2.generate_private_key()
D_HEX = "%064x" % D
PUB = sm2.public_key(D)
APPKEY = "EXAMPLE-AGENT"          # 样例;实际值由中间件给


def _boom(name):
    raise AssertionError("不该去读凭据 %r:headers-only 档不需要私钥" % name)


def _settings(env):
    return signing.settings_from(env, None, load_secret=_boom if "GRMP_SM2_PRIVATE_KEY" not in env else None)


# --- 三档 ---------------------------------------------------------------------

def test_no_appkey_still_sends_nothing():
    """第一档:没配 Appkey,四个头一个都不加(中间件还没加固的环境)。"""
    assert signing.settings_from({}, None, load_secret=_boom) is None


def test_default_mode_is_full():
    st = signing.settings_from({"GRMP_APPKEY": APPKEY, "GRMP_SM2_PRIVATE_KEY": D_HEX}, None)
    assert st.mode == "full" and st.private_key == D


def test_headers_only_needs_no_private_key():
    """第二档:过渡期。**不去读私钥**——_boom 会在读凭据时炸掉。"""
    st = signing.settings_from(
        {"GRMP_APPKEY": APPKEY, "GRMP_SIGN_MODE": "headers-only"}, None, load_secret=_boom)
    assert st.mode == "headers-only" and st.private_key is None and st.appkey == APPKEY


def test_headers_only_sends_exactly_two_headers():
    st = signing.settings_from(
        {"GRMP_APPKEY": APPKEY, "GRMP_SIGN_MODE": "headers-only"}, None, load_secret=_boom)
    headers = signing.RequestSigner(st).headers("/v1/validateByNorth/validate")
    assert set(headers) == {"Appkey", "Timestamp"}, "过渡期绝不能带 Signature"
    assert headers["Appkey"] == APPKEY


def test_full_mode_sends_three_headers():
    st = signing.settings_from({"GRMP_APPKEY": APPKEY, "GRMP_SM2_PRIVATE_KEY": D_HEX}, None)
    assert set(signing.RequestSigner(st).headers("/v1/x")) == {"Appkey", "Timestamp", "Signature"}


# --- 客户给的具体形态 ---------------------------------------------------------

def test_customer_shape_seconds_ten_digits():
    """Timestamp 单位秒,形如 1758431954(10 位)。"""
    st = signing.settings_from(
        {"GRMP_APPKEY": APPKEY, "GRMP_SIGN_MODE": "headers-only", "GRMP_SIGN_TIMESTAMP": "s"},
        None, load_secret=_boom)
    ts = signing.RequestSigner(st).headers("/v1/x")["Timestamp"]
    assert re.fullmatch(r"\d{10}", ts), "客户要的是秒级 10 位,拿到 %r" % ts


def test_milliseconds_would_be_thirteen_digits():
    """对照:默认 ms 是 13 位。填错单位中间件会当成时钟不同步而拒,所以这条要钉住。"""
    st = signing.settings_from(
        {"GRMP_APPKEY": APPKEY, "GRMP_SIGN_MODE": "headers-only"}, None, load_secret=_boom)
    assert re.fullmatch(r"\d{13}", signing.RequestSigner(st).headers("/v1/x")["Timestamp"])


# --- 配错了要报得能看懂 -------------------------------------------------------

def test_full_mode_without_key_names_the_transition_option():
    """报错里要指出过渡档,否则客户拿着「必须有私钥」的错误没法自己往下走。"""
    with pytest.raises(ConfigError) as exc:
        signing.settings_from({"GRMP_APPKEY": APPKEY}, None,
                              load_secret=lambda n: (_ for _ in ()).throw(
                                  __import__("common.credential", fromlist=["x"]).CredentialError("无")))
    assert "headers-only" in str(exc.value)


def test_unknown_mode_is_rejected():
    with pytest.raises(ConfigError) as exc:
        signing.settings_from({"GRMP_APPKEY": APPKEY, "GRMP_SIGN_MODE": "no-sign"}, None, load_secret=_boom)
    assert "no-sign" in str(exc.value)


# --- 中间件侧:也要能只校验两个头 ----------------------------------------------

def _policy(**kw):
    base = dict(appkey=APPKEY, public_key=PUB, timestamp_unit="s")
    base.update(kw)
    return mocksig.SignaturePolicy(**base)


def _client_headers(**env):
    e = {"GRMP_APPKEY": APPKEY, "GRMP_SIGN_TIMESTAMP": "s"}
    e.update(env)
    return signing.RequestSigner(signing.settings_from(e, None, load_secret=_boom)).headers("/v1/x")


def test_mock_accepts_two_headers_when_signature_not_required():
    policy = _policy(require_signature=False)
    assert policy.check("/v1/x", _client_headers(GRMP_SIGN_MODE="headers-only")) is None


def test_mock_still_rejects_replay_without_signature():
    """不校验签名 ≠ 不防重放。Timestamp 窗口这道门还在。"""
    policy = _policy(require_signature=False, window_seconds=300)
    headers = _client_headers(GRMP_SIGN_MODE="headers-only")
    reason = policy.check("/v1/x", headers, now=float(headers["Timestamp"]) + 9999)
    assert reason and "Timestamp" in reason


def test_mock_still_rejects_wrong_appkey_without_signature():
    policy = _policy(require_signature=False)
    headers = dict(_client_headers(GRMP_SIGN_MODE="headers-only"), Appkey="别的应用")
    assert "Appkey" in (policy.check("/v1/x", headers) or "")


def test_mock_default_still_demands_signature():
    """默认不变:客户密钥批下来后把 require_signature 打开,缺签名立刻被拒。"""
    policy = _policy()
    assert "Signature" in (policy.check("/v1/x", _client_headers(GRMP_SIGN_MODE="headers-only")) or "")


def test_full_mode_still_verifies_end_to_end():
    """回归:全签这档没被改坏。"""
    policy = _policy()
    assert policy.check("/v1/x", _client_headers(GRMP_SM2_PRIVATE_KEY=D_HEX)) is None

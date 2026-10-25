"""把调用人工号送到 GRMP(客户 2026-09-20 确认要)。

中间件按 dataIp 路由、按 auth 令牌认调用方,报文里本来没有「谁在调用」这一维。
客户要显式收工号,但字段名与位置(请求头 / 报文顶层)还没给,所以两者都做成开关,
默认两个都不配 —— 不配就跟加这个特性之前一模一样。

**这里守的主要是静默失效**:配了字段名却取不到工号时,绝不能悄悄发一个空值。
那样中间件收到的是「有人调用但没有身份」,而我方退出码 0、报告照出,最难查。
"""
import json
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common import audit                      # noqa: E402
from common.config import ConfigError         # noqa: E402
from common.grmp import userid                # noqa: E402
from common.grmp.client import GrmpClient     # noqa: E402


def _env(**kw):
    """只留本特性关心的键,其余一律不存在。"""
    return {k: v for k, v in kw.items() if v is not None}


# --- 设置解析 -----------------------------------------------------------------

def test_not_configured_is_none():
    """中间件没要求时返回 None,调用方据此什么都不加。"""
    assert userid.settings_from(_env(GSDB_USER_ID="u1234")) is None


def test_header_only():
    st = userid.settings_from(_env(GRMP_USER_ID_HEADER="X-User-Id", GSDB_USER_ID="u1234"))
    assert (st.header, st.param, st.user_id) == ("X-User-Id", "", "u1234")


def test_param_only():
    st = userid.settings_from(_env(GRMP_USER_ID_PARAM="userId", GSDB_USER_ID="u1234"))
    assert (st.header, st.param, st.user_id) == ("", "userId", "u1234")


def test_both_positions():
    st = userid.settings_from(
        _env(GRMP_USER_ID_HEADER="X-User-Id", GRMP_USER_ID_PARAM="userId", GSDB_USER_ID="u1234"))
    assert st.header == "X-User-Id" and st.param == "userId"


def test_user_id_is_stripped():
    st = userid.settings_from(_env(GRMP_USER_ID_HEADER="X-User-Id", GSDB_USER_ID="  u1234  "))
    assert st.user_id == "u1234"


@pytest.mark.parametrize("uid", [None, "", "   "])
def test_configured_but_no_user_id_raises(uid):
    """**核心**:配了字段名却没有工号 —— 报错,不发空值。"""
    with pytest.raises(ConfigError) as exc:
        userid.settings_from(_env(GRMP_USER_ID_HEADER="X-User-Id", GSDB_USER_ID=uid))
    assert "GSDB_USER_ID" in str(exc.value)


def test_settings_reads_the_same_user_id_as_reports():
    """工号只有一个来源:审计用哪个,发给中间件的就是哪个。两处不一致比不传更糟。"""
    env = _env(GRMP_USER_ID_HEADER="X-User-Id", GSDB_USER_ID="u4321")
    assert userid.settings_from(env).user_id == "u4321"
    assert userid.ENV_USER_ID == audit.ENV_USER_ID


# --- 真的加进请求里 -----------------------------------------------------------

def _client(**kw):
    return GrmpClient(base_url="http://mw:8779", token="t" * 32, data_ip="10.0.0.9", **kw)


def test_headers_unchanged_when_not_configured():
    """回归:没配时请求头与加这个特性之前完全一致。"""
    assert _client()._headers("/v1/x") == {"auth": "t" * 32, "Content-Type": "application/json"}


def test_header_is_added():
    st = userid.settings_from(_env(GRMP_USER_ID_HEADER="X-User-Id", GSDB_USER_ID="u1234"))
    assert _client(user_id=st)._headers("/v1/x")["X-User-Id"] == "u1234"


def test_param_is_added_to_payload():
    st = userid.settings_from(_env(GRMP_USER_ID_PARAM="userId", GSDB_USER_ID="u1234"))
    got = _client(user_id=st).with_user_id({"dataIp": "10.0.0.9", "id": "527"})
    assert got == {"dataIp": "10.0.0.9", "id": "527", "userId": "u1234"}


def test_param_does_not_mutate_the_original_payload():
    """报文是调用方构造的,加工号不能就地改它。"""
    st = userid.settings_from(_env(GRMP_USER_ID_PARAM="userId", GSDB_USER_ID="u1234"))
    original = {"dataIp": "10.0.0.9"}
    _client(user_id=st).with_user_id(original)
    assert original == {"dataIp": "10.0.0.9"}


def test_param_not_added_when_only_header_configured():
    st = userid.settings_from(_env(GRMP_USER_ID_HEADER="X-User-Id", GSDB_USER_ID="u1234"))
    assert _client(user_id=st).with_user_id({"dataIp": "10.0.0.9"}) == {"dataIp": "10.0.0.9"}


def test_header_name_is_sent_verbatim():
    """客户给什么头名就发什么,不做大小写归一 —— 中间件那边可能是大小写敏感的。"""
    st = userid.settings_from(_env(GRMP_USER_ID_HEADER="staffNo", GSDB_USER_ID="u1234"))
    assert "staffNo" in _client(user_id=st)._headers("/v1/x")


def test_access_layer_fails_at_connect_not_at_first_request(monkeypatch):
    """配错了要在建连接时就暴露。拖到第一次请求,报错看起来像是那个 skill 坏了。"""
    from common import access
    from common.config import Connection

    monkeypatch.setenv(userid.ENV_HEADER, "X-User-Id")
    monkeypatch.delenv(userid.ENV_USER_ID, raising=False)
    monkeypatch.setenv("GRMP_AUTH_TOKEN", "t" * 32)
    monkeypatch.setenv("GRMP_API_HOST", "mw")
    conn = Connection(name="og-grmp", type="gaussdb", host="mw", port=8779,
                      database="postgres", user="grmp", driver="grmp", data_ip="10.0.0.9")
    with pytest.raises(access.AccessError) as exc:
        access.runner_for(conn)
    assert "工号" in str(exc.value) and userid.ENV_USER_ID in str(exc.value)


def test_request_actually_carries_it(monkeypatch):
    """端到端:一次真实的 invoke,头和报文里都要有。"""
    seen = {}

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            return json.dumps(
                {"status": "finished",
                 "result": {"type": "array", "data": [], "columns": []}}).encode()

    def _fake_urlopen(request, timeout=None):
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.data.decode("utf-8"))
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    st = userid.settings_from(
        _env(GRMP_USER_ID_HEADER="X-User-Id", GRMP_USER_ID_PARAM="userId", GSDB_USER_ID="u1234"))
    c = _client(user_id=st)
    c._ids = {"health.overview": "527"}
    c.invoke("health.overview")
    # urllib 会把头名首字母大写,所以按小写比
    assert {k.lower(): v for k, v in seen["headers"].items()}["x-user-id"] == "u1234"
    assert seen["body"]["userId"] == "u1234"

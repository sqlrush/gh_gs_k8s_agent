"""接口一（查询白屏化诊断命令列表）与其周边：实例映射、鉴权、请求校验。

处理逻辑做成不依赖套接字的纯函数（App.handle），便于逐条断言；
真起 HTTP 服务的端到端验证另做（单测绿不等于跑得通）。
"""
import sys
import pathlib
import json

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

from common.grmp import script as sc  # noqa: E402
from grmp_middleware.grmp_mock import envelope, instances as inst, store as st  # noqa: E402
from grmp_middleware.grmp_mock.server import App  # noqa: E402
from common.grmp.settings import Settings  # noqa: E402
from common.grmp.placeholder import ParamDef  # noqa: E402

LIST_PATH = "/example/grmp/diagnostic/agent/common-operations"
INVOKE_PATH = LIST_PATH + "/invoke"

TOKEN = "0123456789abcdef0123456789abcdef"
DATA_IP = "10.0.0.9"          # 本机映射用的占位 IP，不使用客户测试环境的真实 IP
UNKNOWN_IP = "1.2.3.4"


@pytest.fixture
def app(tmp_path):
    store = st.ScriptStore(tmp_path / "sc.db")
    store.register(
        sc.ScriptRecord(
            script_name="health.db_info",
            script_content="select datname from pg_database;",
        )
    )
    store.register(
        sc.ScriptRecord(
            script_name="slowsql.slow_sql",
            script_content="select 1 where a > {{n}};",
            params=(ParamDef(key="n", type="INTEGER", description="阈值"),),
        )
    )
    return App(
        store=store,
        instances=inst.InstanceMap({DATA_IP: "og"}),
        token=TOKEN,
        settings=Settings(),
    )


def _post(app, path=LIST_PATH, body=None, token=TOKEN):
    headers = {"auth": token} if token is not None else {}
    raw = json.dumps(body if body is not None else {"dataIp": DATA_IP}).encode()
    return app.handle("POST", path, headers, raw)


# ===========================================================================
# 1. 实例映射
# ===========================================================================

def test_instance_map_resolves_data_ip_to_connection_name():
    m = inst.InstanceMap({DATA_IP: "og"})
    assert m.resolve(DATA_IP) == "og"


def test_instance_map_returns_none_for_unknown_ip():
    assert inst.InstanceMap({DATA_IP: "og"}).resolve(UNKNOWN_IP) is None


def test_instance_map_rejects_multi_ip_input():
    """【规】dataIp 明确限定「单 IP」，不支持逗号分隔或数组。"""
    m = inst.InstanceMap({DATA_IP: "og"})
    assert m.resolve("%s,%s" % (DATA_IP, UNKNOWN_IP)) is None


def test_missing_instance_file_yields_empty_map(tmp_path):
    """配置缺失时映射为空，于是每个 dataIp 都走「查不到实例」——与客户同形。"""
    m = inst.load(tmp_path / "nope.yaml")
    assert m.resolve(DATA_IP) is None
    assert m.count() == 0


def test_instance_file_is_loaded_from_yaml(tmp_path):
    path = tmp_path / "instances.yaml"
    path.write_text("%s: og\n" % DATA_IP, encoding="utf-8")
    assert inst.load(path).resolve(DATA_IP) == "og"


# ===========================================================================
# 2. 鉴权
# ===========================================================================

def test_valid_token_passes(app):
    status, body = _post(app)
    assert status == 200
    assert body["code"] == "0"


def test_wrong_token_is_rejected_with_business_error_not_http_status(app):
    """【实】业务错误一律 HTTP 200 + code!="0"，从不用 HTTP 状态码表达。

    鉴权失败的响应文档未给（【缺】），本实现按同一模型处理并在 msg 里
    声明这是本实现约定。
    """
    status, body = _post(app, token="deadbeef")
    assert status == 200
    assert body["code"] == "1"


def test_missing_auth_header_is_rejected(app):
    status, body = _post(app, token=None)
    assert status == 200
    assert body["code"] == "1"


def test_auth_header_name_is_lowercase_auth_not_authorization(app):
    """【实】头名是 auth，不是 Authorization，且无 Bearer 前缀。"""
    raw = json.dumps({"dataIp": DATA_IP}).encode()
    status, body = app.handle(
        "POST", LIST_PATH, {"Authorization": "Bearer " + TOKEN}, raw
    )
    assert body["code"] == "1"


def test_auth_header_matching_is_case_insensitive_on_the_name(app):
    """HTTP 头名大小写不敏感，Auth 与 auth 应等价。"""
    raw = json.dumps({"dataIp": DATA_IP}).encode()
    status, body = app.handle("POST", LIST_PATH, {"Auth": TOKEN}, raw)
    assert body["code"] == "0"


# ===========================================================================
# 3. 路由
# ===========================================================================

def test_unknown_path_returns_http_404(app):
    """路由不存在是传输层问题，不是业务错误 —— 这个才该用 HTTP 状态码。"""
    status, _ = _post(app, path="/nope")
    assert status == 404


def test_get_method_is_rejected(app):
    """【规】两个接口都是 POST。"""
    status, _ = app.handle("GET", LIST_PATH, {"auth": TOKEN}, b"")
    assert status == 405


def test_dataip_path_variant_is_accepted_with_body_taking_precedence(app):
    """接口文档的示例 URL 带 /dataip/{dataip} 段，客户实际调用没有。

    以客户实际调用为准，但一并接受带路径段的写法（否则照着文档写的
    客户端会拿到 404，排查方向会被带偏）；冲突时以 body 为准。
    """
    path = (
        "/example/grmp/diagnostic/agent/dataip/%s/common-operations"
        % UNKNOWN_IP
    )
    status, body = app.handle(
        "POST", path, {"auth": TOKEN}, json.dumps({"dataIp": DATA_IP}).encode()
    )
    assert status == 200
    assert body["code"] == "0"


# ===========================================================================
# 4. 请求体校验
# ===========================================================================

def test_unknown_data_ip_returns_the_customer_error_verbatim(app):
    """【实】客户示例第 6 节：传入不存在的 dataIp 的返回，必须逐字一致。"""
    status, body = _post(app, body={"dataIp": UNKNOWN_IP})
    assert status == 200
    assert body == {
        "code": "1",
        "msg": "通过dataIp查询不到对应高斯实例信息",
    }


def test_missing_data_ip_is_rejected(app):
    """【规】dataIp 必选。"""
    _, body = _post(app, body={})
    assert body["code"] == "1"


def test_malformed_json_body_is_rejected(app):
    status, body = app.handle("POST", LIST_PATH, {"auth": TOKEN}, b"{not json")
    assert status == 200
    assert body["code"] == "1"


def test_non_object_json_body_is_rejected(app):
    _, body = app.handle("POST", LIST_PATH, {"auth": TOKEN}, b"[1,2,3]")
    assert body["code"] == "1"


def test_offset_and_limit_default_to_one_and_ten(app):
    """【规】offset 默认 1，limit 默认 10；【实】客户示例中两者确实可省。"""
    _, body = _post(app, body={"dataIp": DATA_IP})
    assert body["result"]["pageNum"] == 1
    assert body["result"]["pageSize"] == 10


@pytest.mark.parametrize("bad", [0, -1, "x", 1.5, None])
def test_out_of_range_offset_is_rejected_not_clamped(app, bad):
    """【规】offset 最小 1。越界行为文档未定义（【缺】）——本实现报错。

    夹取（clamp）会静默改变调用方要的页，翻页逻辑从此对不上而不报错。
    """
    _, body = _post(app, body={"dataIp": DATA_IP, "offset": bad})
    assert body["code"] == "1"


@pytest.mark.parametrize("bad", [0, -1, 1001, "x"])
def test_out_of_range_limit_is_rejected_not_clamped(app, bad):
    """【规】limit 最小 1、最大 1000。"""
    _, body = _post(app, body={"dataIp": DATA_IP, "limit": bad})
    assert body["code"] == "1"


def test_cmd_type_filter_is_not_supported_server_side(app):
    """【实】客户端 --cmd-type 是本地过滤，请求体里没有对应字段。

    传了就报错而不是默默忽略：默默忽略会让调用方以为服务端筛过了。
    """
    _, body = _post(app, body={"dataIp": DATA_IP, "cmd_type": "SQL"})
    assert body["code"] == "1"


# ===========================================================================
# 5. 接口一的响应
# ===========================================================================

def test_list_returns_registered_scripts_in_page_info_envelope(app):
    _, body = _post(app)
    assert body["code"] == "0"
    assert body["msg"] == "success"
    result = body["result"]
    assert result["total"] == 2
    assert len(result["list"]) == 2
    assert len(result) == 18


def test_listed_scripts_use_the_command_detail_shape(app):
    _, body = _post(app)
    detail = body["result"]["list"][0]
    assert set(detail.keys()) == {
        "id", "cmd", "cmd_name", "description", "cmd_type", "param"
    }


def test_parameterless_script_has_empty_param_array(app):
    _, body = _post(app)
    by_name = {d["cmd_name"]: d for d in body["result"]["list"]}
    assert by_name["health.db_info"]["param"] == []


def test_parameterised_script_exposes_operation_param(app):
    _, body = _post(app)
    by_name = {d["cmd_name"]: d for d in body["result"]["list"]}
    param = by_name["slowsql.slow_sql"]["param"][0]
    assert param["param_name"] == "n"
    assert param["data_type"] == "Integer"
    assert param["required"] is True


def test_second_page_is_empty_but_still_well_formed(app):
    _, body = _post(app, body={"dataIp": DATA_IP, "offset": 2, "limit": 10})
    assert body["code"] == "0"
    assert body["result"]["list"] == []
    assert body["result"]["total"] == 2


def test_paging_splits_the_script_list(app):
    _, body = _post(app, body={"dataIp": DATA_IP, "offset": 1, "limit": 1})
    assert body["result"]["size"] == 1
    assert body["result"]["pages"] == 2
    assert body["result"]["hasNextPage"] is True

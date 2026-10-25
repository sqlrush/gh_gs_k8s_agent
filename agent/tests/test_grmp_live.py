"""grmp-mock 连真库的端到端验证。

单测绿不等于跑得通：单测里的连接是假的，真库会带来类型映射、
系统视图差异、驱动行为这些假连接看不见的东西。

未配置 og 连接时自动跳过。运行：
    GSDB_HOME=$HOME/.gdaa python3 -m pytest tests/test_grmp_live.py -v
"""
import sys
import pathlib
import json

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

import common  # noqa: E402
from common.grmp import script as sc  # noqa: E402
from grmp_middleware.grmp_mock import instances as inst, store as st  # noqa: E402
from grmp_middleware.grmp_mock.server import App  # noqa: E402
from common.grmp.settings import Settings  # noqa: E402

pytestmark = pytest.mark.live

CONN = "og"
TOKEN = "0123456789abcdef0123456789abcdef"
DATA_IP = "10.0.0.9"
INVOKE_PATH = "/example/grmp/diagnostic/agent/common-operations/invoke"
LIST_PATH = "/example/grmp/diagnostic/agent/common-operations"

REGISTRY = _ROOT / "scripts" / "registry"


def _available() -> bool:
    try:
        common.find(CONN)
        return True
    except common.ConfigError:
        return False


pytestmark = [pytest.mark.live, pytest.mark.skipif(
    not _available(), reason="连接 %s 未配置" % CONN
)]


@pytest.fixture
def app(tmp_path):
    """用仓库里真实的脚本定义建库 —— 测的是将要交付的那份，不是测试专用副本。"""
    store = st.ScriptStore(tmp_path / "sc.db")
    for path in sorted(REGISTRY.rglob("*.yaml")):
        store.register(sc.load_script(path))
    return App(
        store=store,
        instances=inst.InstanceMap({DATA_IP: CONN}),
        token=TOKEN,
        settings=Settings(),
    )


def _post(app, path, body):
    return app.handle(
        "POST", path, {"auth": TOKEN}, json.dumps(body).encode()
    )


def _id_of(app, cmd_name):
    """按逻辑名解析 ID —— 与 agent 在客户环境必须做的事完全一致。"""
    _, body = _post(app, LIST_PATH, {"dataIp": DATA_IP, "limit": 1000})
    for detail in body["result"]["list"]:
        if detail["cmd_name"] == cmd_name:
            return detail["id"]
    raise AssertionError("未注册脚本 %s" % cmd_name)


def test_parameterless_script_runs_against_the_real_database(app):
    _, body = _post(
        app, INVOKE_PATH, {"dataIp": DATA_IP, "id": _id_of(app, "health.db_info")}
    )
    assert body["status"] == "finished", body.get("msg")
    assert body["result"]["type"] == "array"
    assert len(body["result"]["data"]) > 0


def test_every_value_from_the_real_database_is_a_string(app):
    """【实】全字段字符串化 —— 真库里有 oid/int/bool/text，都要变成字符串。"""
    _, body = _post(
        app, INVOKE_PATH, {"dataIp": DATA_IP, "id": _id_of(app, "health.db_info")}
    )
    for row in body["result"]["data"]:
        for key, value in row.items():
            assert isinstance(value, str), "%s -> %r" % (key, value)


def test_boolean_columns_follow_the_configured_style(app):
    """真库的布尔列经驱动到序列化这一整条链路后的表现。"""
    _, body = _post(
        app, INVOKE_PATH, {"dataIp": DATA_IP, "id": _id_of(app, "health.db_info")}
    )
    values = {row["datistemplate"] for row in body["result"]["data"]}
    assert values <= {"t", "f"}, values


def test_parameterised_script_substitutes_and_runs(app):
    _, body = _post(
        app,
        INVOKE_PATH,
        {
            "dataIp": DATA_IP,
            "id": _id_of(app, "slowsql.slow_sql"),
            "param": [
                {"param_name": "threshold_ms", "param_value": "0"},
                {"param_name": "begin_time", "param_value": "2020-01-01 00:00:00"},
                {"param_name": "limit", "param_value": "2"},
            ],
        },
    )
    assert body["status"] == "finished", body.get("msg")
    assert len(body["result"]["data"]) <= 2


def test_injection_payload_never_reaches_the_real_database(app):
    _, body = _post(
        app,
        INVOKE_PATH,
        {
            "dataIp": DATA_IP,
            "id": _id_of(app, "slowsql.slow_sql"),
            "param": [
                {"param_name": "threshold_ms", "param_value": "0 OR 1=1"},
                {"param_name": "begin_time", "param_value": "2020-01-01 00:00:00"},
                {"param_name": "limit", "param_value": "2"},
            ],
        },
    )
    assert body["status"] == "failed"
    assert "result" not in body


def test_broken_script_fails_loudly_without_a_result_key(app, tmp_path):
    """SQL 执行失败时不能产出 result —— 否则会被读成「查询结果为空」。"""
    store = st.ScriptStore(tmp_path / "broken.db")
    rec = store.register(
        sc.ScriptRecord(
            script_name="x.broken",
            script_content="select * from table_that_does_not_exist_9x7;",
        )
    )
    broken_app = App(
        store=store,
        instances=inst.InstanceMap({DATA_IP: CONN}),
        token=TOKEN,
        settings=Settings(),
    )
    _, body = _post(broken_app, INVOKE_PATH, {"dataIp": DATA_IP, "id": rec.id})
    assert body["status"] == "failed"
    assert "result" not in body
    assert body["msg"]

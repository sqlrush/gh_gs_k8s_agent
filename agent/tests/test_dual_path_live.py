"""双路径一致性：同一条脚本，走中间件和走直连，结果必须逐行相同。

这是整个方案里价值最高的一项测试。任何差异要么是中间件的 bug，
要么是协议的真实限制 —— 两者都必须被看见，而不是等到交付现场才发现。

两条路径共用同一份脚本 YAML 与同一个渲染器，所以比对的确实是
「两条不同的执行链路」，而不是「两条不同的 SQL」。

真起 HTTP 服务、真连 og5。未配置 og 连接时自动跳过。
"""
import sys
import pathlib
import threading

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

import common  # noqa: E402
from common.grmp import script as sc  # noqa: E402
from common.grmp.client import GrmpClient, GrmpRunner, GrmpError  # noqa: E402
from common.grmp.registry import Registry  # noqa: E402
from common.grmp.runner import DirectRunner  # noqa: E402
from common.grmp.settings import Settings  # noqa: E402
from grmp_middleware.grmp_mock import instances as inst, store as st  # noqa: E402
from grmp_middleware.grmp_mock.http_server import serve  # noqa: E402
from grmp_middleware.grmp_mock.server import App  # noqa: E402

CONN = "og"
TOKEN = "0123456789abcdef0123456789abcdef"
DATA_IP = "10.0.0.9"
REGISTRY_DIR = _ROOT / "scripts" / "registry"

SLOW_SQL_PARAMS = {
    "threshold_ms": 0,
    "begin_time": "2020-01-01 00:00:00",
    "limit": 5,
}


def _available() -> bool:
    try:
        common.find(CONN)
        return True
    except common.ConfigError:
        return False


pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not _available(), reason="连接 %s 未配置" % CONN),
]


@pytest.fixture
def registry():
    return Registry(REGISTRY_DIR)


@pytest.fixture
def middleware(tmp_path, registry):
    """在线程里真起一个 grmp-mock，返回 (GrmpRunner, 停止函数)。"""
    store = st.ScriptStore(tmp_path / "sc.db")
    for name in registry.names():
        store.register(registry.find(name))

    app = App(
        store=store,
        instances=inst.InstanceMap({DATA_IP: CONN}),
        token=TOKEN,
        settings=Settings(),
    )
    httpd = serve(app, port=0, quiet=True)     # 0 = 让系统分配端口
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    runner = GrmpRunner(
        GrmpClient(
            base_url="http://127.0.0.1:%d" % port,
            token=TOKEN,
            data_ip=DATA_IP,
        )
    )
    yield runner
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


@pytest.fixture
def direct(registry):
    return DirectRunner(conn_name=CONN, registry=registry, settings=Settings())


# ===========================================================================
# 一致性
# ===========================================================================

def test_parameterless_script_matches_across_both_paths(direct, middleware):
    assert middleware.run("health.db_info") == direct.run("health.db_info")


def test_parameterised_script_matches_across_both_paths(direct, middleware):
    """带参脚本：文本替换在两侧必须产出同一条 SQL，结果才可能一致。"""
    assert (
        middleware.run("slowsql.slow_sql", SLOW_SQL_PARAMS)
        == direct.run("slowsql.slow_sql", SLOW_SQL_PARAMS)
    )


def test_both_paths_return_only_strings(direct, middleware):
    """全字符串化在两侧都成立 —— 直连路径不能因为「本地」就返回原生类型。

    返回原生类型的话，本地写出的解析代码到客户环境会全部失效。
    """
    for rows in (middleware.run("health.db_info"), direct.run("health.db_info")):
        assert rows
        for row in rows:
            for key, value in row.items():
                assert isinstance(value, str), "%s -> %r" % (key, value)


def test_column_names_and_order_match(direct, middleware):
    a = middleware.run("health.db_info")
    b = direct.run("health.db_info")
    assert [list(r.keys()) for r in a] == [list(r.keys()) for r in b]


# ===========================================================================
# 两条路径对错误的反应也要一致（不一致就说明有一侧在悄悄兜底）
# ===========================================================================

def test_injection_payload_is_rejected_on_both_paths(direct, middleware):
    bad = dict(SLOW_SQL_PARAMS, threshold_ms="0 OR 1=1")
    with pytest.raises(Exception):
        direct.run("slowsql.slow_sql", bad)
    with pytest.raises(GrmpError):
        middleware.run("slowsql.slow_sql", bad)


def test_missing_param_is_rejected_on_both_paths(direct, middleware):
    with pytest.raises(Exception):
        direct.run("slowsql.slow_sql", {"limit": 5})
    with pytest.raises(GrmpError):
        middleware.run("slowsql.slow_sql", {"limit": 5})


def test_unknown_script_is_rejected_on_both_paths(direct, middleware):
    with pytest.raises(Exception):
        direct.run("nope.nope")
    with pytest.raises(GrmpError):
        middleware.run("nope.nope")


# ===========================================================================
# 中间件路径必须按逻辑名解析 ID，不能硬编码
# ===========================================================================

def test_middleware_path_resolves_id_by_command_name(middleware):
    """脚本 ID 是环境相关数据：换环境后同一个 ID 会指向另一条脚本，
    执行成功、结果无关、不报错。所以必须先查清单再取 ID。"""
    ids = {
        d["cmd_name"]: d["id"]
        for d in middleware._client.list_operations()
    }
    assert "health.db_info" in ids
    assert middleware._client.resolve_id("health.db_info") == ids["health.db_info"]

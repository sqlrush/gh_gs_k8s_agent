"""grmp-mock 的备机模式:读 statement_history 的脚本按真实 GRMP 形态回 HTTP 400 + unlogged 报错;
health.overview 的 in_recovery 置 t。复现现场 2026-09-01 的 400,让 skill 的降级路径能端到端跑。"""
import json
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common.grmp import script as sc  # noqa: E402
from common.grmp.placeholder import ParamDef  # noqa: E402
from common.grmp.settings import Settings  # noqa: E402
from grmp_middleware.grmp_mock import instances as inst, store as st  # noqa: E402
from grmp_middleware.grmp_mock import server  # noqa: E402

LIST_PATH = "/example/grmp/diagnostic/agent/common-operations"
INVOKE_PATH = LIST_PATH + "/invoke"
TOKEN = "0123456789abcdef0123456789abcdef"
DATA_IP = "10.0.0.9"


@pytest.fixture
def app(tmp_path):
    store = st.ScriptStore(tmp_path / "sc.db")
    store.register(sc.ScriptRecord(
        script_name="sqlfetch.from_history",
        script_content="SELECT schema_name, query FROM dbe_perf.statement_history WHERE unique_query_id = {{sid}};",
        params=(ParamDef(key="sid", type="INTEGER", description="unique_sql_id"),)))
    return server.App(store=store, instances=inst.InstanceMap({DATA_IP: "og"}), token=TOKEN,
                      settings=Settings(), standby=True)


def _post(app, path, body):
    return app.handle("POST", path, {"auth": TOKEN}, json.dumps(body).encode())


def test_standby_mode_returns_http_400_with_unlogged_error(app):
    status, listing = _post(app, LIST_PATH, {"dataIp": DATA_IP})
    sid = next(d["id"] for d in listing["result"]["list"] if d["cmd_name"] == "sqlfetch.from_history")
    status, payload = _post(app, INVOKE_PATH, {"dataIp": DATA_IP, "id": sid,
                                               "param": [{"param_name": "sid", "param_value": "300316117"}]})
    assert status == 400
    assert payload["status"] == "failed" and "result" not in payload
    assert "cannot be accessed on the standby" in payload["msg"]


def test_mark_standby_flips_in_recovery_without_mutating():
    result = {"type": "array", "data": [{"in_recovery": "f", "x": "1"}, {"x": "2"}]}
    out = server.mark_standby(result)
    assert out["data"][0]["in_recovery"] == "t" and out["data"][1] == {"x": "2"}
    assert result["data"][0]["in_recovery"] == "f"          # 原对象不动

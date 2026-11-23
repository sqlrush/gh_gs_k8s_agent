"""grmp-mock 的行方模式(--bank-like):一条脚本里有多条语句回 HTTP 400;执行失败只回「agent诊断脚本执行失败」。

2026-11-08 行方测试报告后加。我们的模拟中间件原先能跑多条语句、出错原样回数据库报错,和行方真实 GRMP 不一样:
explain / sqltune 的「补全表名」退路在我们这里从没被执行过,Bug 1(EXTRACT(HOUR FROM 列) 的列被补成 schema.列)
因此漏到现场。行方模式让这条退路在测试里被真正走到(见 test_explain_plan_under_bank_like_mock_*)。
"""
import json
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common import search_path as sp  # noqa: E402
from common.grmp import script as sc  # noqa: E402
from common.grmp.placeholder import ParamDef  # noqa: E402
from common.grmp.settings import Settings  # noqa: E402
from grmp_middleware.grmp_mock import instances as inst, store as st  # noqa: E402
from grmp_middleware.grmp_mock import server  # noqa: E402

LIST_PATH = "/example/grmp/diagnostic/agent/common-operations"
INVOKE_PATH = LIST_PATH + "/invoke"
TOKEN = "0123456789abcdef0123456789abcdef"
DATA_IP = "10.0.0.9"


class _Db:
    """假库:EXPLAIN 时只认带 schema 的表名(执行账号的 search_path 里没有业务 schema,与行方一致)。"""

    def __init__(self, executed):
        self.executed = executed

    def set_statement_timeout(self, _s):
        pass

    def query(self, sql):
        self.executed.append(sql)
        if "boom" in sql:
            raise RuntimeError('ERROR:  permission denied for database "other"')
        if sql.lstrip().upper().startswith("EXPLAIN") and " app." not in sql:
            raise RuntimeError('ERROR:  relation "t1" does not exist')
        return ["QUERY PLAN"], [("Seq Scan on t1",)]

    def close(self):
        pass


@pytest.fixture
def mock(tmp_path):
    store = st.ScriptStore(tmp_path / "sc.db")
    store.register(sc.ScriptRecord(script_name="explain.multi_stmt_probe",
                                   script_content="SET search_path TO public; SELECT 1 AS ok;"))
    store.register(sc.ScriptRecord(script_name="explain.plan_text", script_content="EXPLAIN {{sql}}",
                                   params=(ParamDef(key="sql", type="STRING", description="SQL"),)))
    store.register(sc.ScriptRecord(script_name="health.storage", script_content="SELECT boom"))
    executed = []
    app = server.App(store=store, instances=inst.InstanceMap({DATA_IP: "og"}), token=TOKEN, settings=Settings(),
                     open_db=lambda name, ro=True: _Db(executed), single_statement=True, opaque_errors=True)
    return app, executed


def _invoke(app, name, values=None):
    _, listing = app.handle("POST", LIST_PATH, {"auth": TOKEN}, json.dumps({"dataIp": DATA_IP}).encode())
    sid = next(d["id"] for d in listing["result"]["list"] if d["cmd_name"] == name)
    body = {"dataIp": DATA_IP, "id": sid,
            "param": [{"param_name": k, "param_value": v} for k, v in (values or {}).items()]}
    return app.handle("POST", INVOKE_PATH, {"auth": TOKEN}, json.dumps(body).encode())


def test_multi_statement_script_is_refused_like_the_bank(mock):
    app, executed = mock
    status, payload = _invoke(app, "explain.multi_stmt_probe")
    assert status == 400 and payload["msg"] == server.BANK_EXEC_FAILED and executed == []


def test_database_error_is_opaque_like_the_bank(mock):
    app, _ = mock
    status, payload = _invoke(app, "health.storage")
    assert status == 400 and payload["msg"] == server.BANK_EXEC_FAILED     # 原始报错不回给调用方


class _MockRunner:
    """把 search_path.explain_plan 接到行方模式的 mock 上:走真实的协议处理与渲染。"""

    def __init__(self, app):
        self.app = app

    def run(self, script, values=None):
        status, payload = _invoke(self.app, script, {k: str(v) for k, v in (values or {}).items()})
        if status != 200 or payload.get("status") != "finished":
            raise RuntimeError("HTTP %d %s" % (status, payload.get("msg")))
        return payload["result"]["data"]


def test_explain_plan_under_bank_like_mock_qualifies_tables_but_not_function_arguments(mock):
    """补全表名后单条执行(行方中间件拦截两段 SQL)。EXTRACT(HOUR FROM 列) 里的列不能被补成 schema.列(行方报告 Bug 1)。"""
    app, executed = mock
    plan, applied, note = sp.explain_plan(_MockRunner(app), "explain.plan_text",
                                          "SELECT EXTRACT(HOUR FROM sample_time) FROM t1", "app")
    assert plan == "Seq Scan on t1" and applied == ""
    sent = executed[-1]
    assert "FROM app.t1" in sent and "HOUR FROM sample_time" in sent and "app.sample_time" not in sent
    assert "补全" in note

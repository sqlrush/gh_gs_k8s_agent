"""切到业务库(dbName)的端到端单测:客户端什么时候带 dbName、模拟中间件怎么切库、登录怎么核实、会话怎么记。

行方方案(2026-11-11《切库问题说明》):脚本 database_type = appbusiness、parameter_config 最前面声明 dbName,
调用接口二时 param 里带 dbName 的值,中间件切到该业务库执行;不带则连 postgres。
我们这边三条同时满足才带:会话里有登录时核实过的目标库;脚本被我们划为业务库类;行方接口一里这条脚本声明了 dbName。
"""
import importlib.util
import json
from dataclasses import replace
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common import session  # noqa: E402
from common.config import Connection  # noqa: E402
from common.grmp import routing, script as sc  # noqa: E402
from common.grmp.client import GrmpClient, GrmpError  # noqa: E402
from common.grmp.placeholder import ParamDef  # noqa: E402
from common.grmp.settings import Settings  # noqa: E402
from grmp_middleware.grmp_mock import instances as inst, store as st  # noqa: E402
from grmp_middleware.grmp_mock.server import App  # noqa: E402

TOKEN = "0123456789abcdef0123456789abcdef"
DATA_IP = "10.0.0.9"
LIST_PATH = "/example/grmp/diagnostic/agent/common-operations"
INVOKE_PATH = "/example/grmp/diagnostic/agent/common-operations/invoke"


# ============================================================ 模拟中间件

class FakeDB:
    def __init__(self, db):
        self.db, self.executed = db, []

    def query(self, sql, params=None):
        self.executed.append(sql)
        return ["db"], [(self.db,)]

    def set_statement_timeout(self, seconds):
        pass

    def close(self):
        pass


@pytest.fixture
def mock(tmp_path):
    store = st.ScriptStore(tmp_path / "sc.db")
    store.register(sc.ScriptRecord(script_name="sqltune.plan_text", database_type=routing.APPBUSINESS,
                                   script_content="EXPLAIN {{sql}}", params=(ParamDef(key="sql", type="STRING"),)))
    store.register(sc.ScriptRecord(script_name="topsql.snap_statement", script_content="select 1 from snapshot.x;"))
    opened = []

    def open_db(name, read_only=True, database=""):
        if database == "nodb":
            raise RuntimeError('FATAL:  database "nodb" does not exist')
        opened.append(database or "postgres")
        return FakeDB(database or "postgres")

    def make(**kw):
        return App(store=store, instances=inst.InstanceMap({DATA_IP: "og"}), token=TOKEN, settings=Settings(),
                   open_db=open_db, path_prefix="/example/grmp/diagnostic/agent", **kw)
    return make, store, opened


def _call(app, path, body):
    code, resp = app.handle("POST", path, {"auth": TOKEN}, json.dumps(body).encode())
    return code, resp


def _ids(app):
    _, resp = _call(app, LIST_PATH, {"dataIp": DATA_IP, "offset": 1, "limit": 100})
    return {d["cmd_name"]: d for d in resp["result"]["list"]}


def test_interface_one_lists_db_name_first_and_optional_for_business_scripts(mock):
    make, _, _ = mock
    ids = _ids(make())
    assert ids["sqltune.plan_text"]["param"][0] == {**routing.API_PARAM}
    assert [p["param_name"] for p in ids["topsql.snap_statement"]["param"]] == []


def test_db_name_switches_a_business_script_to_that_database(mock):
    make, _, opened = mock
    app = make()
    sid = _ids(app)["sqltune.plan_text"]["id"]
    code, resp = _call(app, INVOKE_PATH, {"dataIp": DATA_IP, "id": sid, "param": [
        {"param_name": "dbName", "param_value": "opendb_test"}, {"param_name": "sql", "param_value": "select 1"}]})
    assert resp["status"] == "finished" and opened == ["opendb_test"]
    assert resp["result"]["data"][0]["db"] == "opendb_test"


def test_without_db_name_a_business_script_is_rejected_the_way_the_bank_does(mock):
    """11-11 现场:行方中间件对声明了 dbName 的脚本不带就按空库名去连,HTTP 400,不退回 postgres。"""
    make, _, opened = mock
    app = make()
    sid = _ids(app)["sqltune.plan_text"]["id"]
    body = {"dataIp": DATA_IP, "id": sid, "param": [{"param_name": "sql", "param_value": "select 1"}]}
    code, resp = _call(app, INVOKE_PATH, body)
    assert code == 400 and resp == {"error_msg": "数据库[]在目标高斯实例中不存在, instanceId: mock-og", "error_code": 400}
    assert opened == []
    lenient = make(dbname_missing="postgres")                     # 11-11 之前的假设,留作对照
    assert _call(lenient, INVOKE_PATH, body)[1]["status"] == "finished" and opened == ["postgres"]


def test_a_db_name_that_does_not_exist_is_reported_with_its_name(mock):
    make, _, _ = mock
    app = make()
    sid = _ids(app)["sqltune.plan_text"]["id"]
    code, resp = _call(app, INVOKE_PATH, {"dataIp": DATA_IP, "id": sid, "param": [
        {"param_name": "dbName", "param_value": "nodb"}, {"param_name": "sql", "param_value": "select 1"}]})
    assert code == 400 and resp["error_msg"].startswith("数据库[nodb]在目标高斯实例中不存在")


def test_db_name_on_a_postgres_script_is_ignored_unless_told_to_reject(mock):
    make, _, opened = mock
    app = make()
    sid = _ids(app)["topsql.snap_statement"]["id"]
    body = {"dataIp": DATA_IP, "id": sid, "param": [{"param_name": "dbName", "param_value": "opendb_test"}]}
    assert _call(app, INVOKE_PATH, body)[1]["status"] == "finished" and opened == ["postgres"], "读快照的脚本不能被切走"
    assert "不接受 dbName" in _call(make(dbname_on_postgres="reject"), INVOKE_PATH, body)[1]["msg"]


def test_store_round_trip_keeps_db_name_out_of_the_sql_params(mock):
    _, store, _ = mock
    rec = store.find_by_name("sqltune.plan_text") if hasattr(store, "find_by_name") else \
        next(r for r in store.list_all() if r.script_name == "sqltune.plan_text")
    assert [p.key for p in rec.params] == ["sql"] and rec.database_type == routing.APPBUSINESS
    assert rec.parameter_config.startswith("[" + routing.CONFIG_ENTRY_JSON + ",")


# ============================================================ 客户端:什么时候带 dbName

class _Wire(GrmpClient):
    """把两个接口换成内存里的假响应,记下每次发出的报文。"""

    def __init__(self, declared, target_db=""):
        super().__init__(base_url="http://x", token="t", data_ip=DATA_IP, path_prefix="/p", target_db=target_db)
        self.declared, self.sent = declared, []

    def _post(self, path, payload):
        if not path.endswith("/invoke"):
            return {"code": "0", "result": {"hasNextPage": False, "list": [
                {"id": str(i), "cmd_name": n, "param": [{"param_name": p} for p in ps]}
                for i, (n, ps) in enumerate(self.declared.items())]}}
        self.sent.append(payload)
        return {"status": "finished", "result": {"type": "Array", "data": []}}


BANK = {"sqltune.plan_text": ["dbName", "sql"], "topsql.snap_statement": ["dbName"], "sqltune.indexes": ["schema"]}


def test_db_name_goes_first_only_when_all_three_conditions_hold():
    c = _Wire(BANK, target_db="appdb")
    c.invoke("sqltune.plan_text", {"sql": "select 1"})
    assert c.sent[-1]["param"] == [{"param_name": "dbName", "param_value": "appdb"},
                                   {"param_name": "sql", "param_value": "select 1"}]


def test_scripts_reading_postgres_only_data_get_postgres_when_the_bank_declares_db_name():
    """行方白名单把读快照的脚本登记成了业务库类(增量没导入):不能切到业务库(查不到),也不能不带(中间件报空库名)。"""
    c = _Wire(BANK, target_db="appdb")
    c.invoke("topsql.snap_statement")
    assert c.sent[-1]["param"] == [{"param_name": "dbName", "param_value": "postgres"}]


def test_no_db_name_when_the_bank_has_not_declared_it_yet():
    c = _Wire(BANK, target_db="appdb")
    c.invoke("sqltune.indexes", {"schema": "s"})
    assert [p["param_name"] for p in c.sent[-1]["param"]] == ["schema"], "没配好的脚本多传参数会被行方拒"


def test_without_a_verified_target_declared_scripts_get_postgres():
    c = _Wire(BANK)
    c.invoke("sqltune.plan_text", {"sql": "select 1"})
    assert c.sent[-1]["param"][0] == {"param_name": "dbName", "param_value": "postgres"}


class _Strict(_Wire):
    """接口一没列出 dbName,执行时却按行方的形态报空库名(白名单库里登记了、清单没带出来)。"""

    def _post(self, path, payload):
        if path.endswith("/invoke") and not any(p["param_name"] == "dbName" for p in payload.get("param") or []):
            self.sent.append(payload)
            raise GrmpError('请求 /p/invoke 失败：HTTP 400 ；中间件返回：数据库[]在目标高斯实例中不存在, instanceId: x')
        return super()._post(path, payload)


def test_an_empty_database_error_is_retried_once_on_postgres_and_remembered():
    c = _Strict({"explain.from_history": ["sql_id"]}, target_db="appdb")
    c.invoke("explain.from_history", {"sql_id": 1})
    assert [[p["param_name"] for p in s.get("param", [])] for s in c.sent] == [["sql_id"], ["dbName", "sql_id"]]
    assert c.sent[-1]["param"][0]["param_value"] == "postgres", "读 SQL 历史的脚本只能在 postgres"
    c.invoke("explain.from_history", {"sql_id": 2})
    assert len(c.sent) == 3 and c.sent[-1]["param"][0]["param_value"] == "postgres", "记住了,不再先撞一次"


def test_a_named_database_that_does_not_exist_is_not_retried():
    class _NoDb(_Wire):
        def _post(self, path, payload):
            if path.endswith("/invoke"):
                self.sent.append(payload)
                raise GrmpError("请求失败：HTTP 400；中间件返回：数据库[appdb]在目标高斯实例中不存在, instanceId: x")
            return super()._post(path, payload)
    c = _NoDb(BANK, target_db="appdb")
    with pytest.raises(GrmpError, match="appdb"):
        c.invoke("sqltune.plan_text", {"sql": "select 1"})
    assert len(c.sent) == 1


# ============================================================ 会话

def test_session_keeps_the_routing_database(tmp_path, monkeypatch):
    monkeypatch.setenv("GSDB_HOME", str(tmp_path))
    conn = Connection(name="ip-appdb", type="gaussdb", host="h", port=1, database="appdb", user="grmp",
                      driver="grmp", data_ip=DATA_IP, route_db="appdb")
    handle = session.save(conn)
    assert session._read(session.path_for(handle)).route_db == "appdb"


# v1.4.10 的会话读取只认这些键,见到别的就报错 —— 而且每次取数都先读一遍目录里全部 *.yaml(顺手清理过期)。
# 照抄自 agent-v1.4.10 的 common/session.py,不要跟着本版改。
V1410_KEYS = {"name", "type", "host", "port", "database", "user", "sslmode", "driver", "data_ip", "app", "exec_database"}


def test_a_routed_session_stays_readable_by_v1_4_10(tmp_path, monkeypatch):
    """11-11 测试环境回滚实测:route_db 写进 yaml 后,回滚到 1.4.10 这个用户的所有取数技能都报「无法识别的键」。"""
    import yaml
    monkeypatch.setenv("GSDB_HOME", str(tmp_path))
    conn = Connection(name="ip-appdb", type="gaussdb", host="h", port=1, database="appdb", user="grmp",
                      driver="grmp", data_ip=DATA_IP, route_db="appdb")
    path = session.path_for(session.save(conn))
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert set(raw) <= V1410_KEYS, "会话 yaml 里出现旧版不认识的键:%s" % sorted(set(raw) - V1410_KEYS)
    assert raw["database"] == "appdb" and "exec_database" not in raw, "旧版读到的是「库名 = 业务库、不切库」"
    route = path.with_suffix(session.ROUTE_SUFFIX)
    assert route.read_text(encoding="utf-8").strip() == "appdb" and oct(route.stat().st_mode & 0o777) == "0o600"


def test_route_file_follows_its_session(tmp_path, monkeypatch):
    monkeypatch.setenv("GSDB_HOME", str(tmp_path))
    base = Connection(name="ip-appdb", type="gaussdb", host="h", port=1, database="appdb", user="grmp",
                      driver="grmp", data_ip=DATA_IP)
    h = session.save(replace(base, route_db="appdb"))
    path = session.path_for(h)
    session.save(base, h)                                   # 同一句柄改成不切库:旧的库名文件要清掉
    assert not path.with_suffix(session.ROUTE_SUFFIX).exists() and session._read(path).route_db == ""
    h2 = session.save(replace(base, route_db="appdb"))
    assert session.clear(h2) and not session.path_for(h2).with_suffix(session.ROUTE_SUFFIX).exists()
    h3 = session.save(replace(base, route_db="appdb"))
    session.path_for(h3).unlink()                           # 旧版清过期会话只删 yaml
    session.list_sessions()
    assert not session.path_for(h3).with_suffix(session.ROUTE_SUFFIX).exists(), "留下的孤儿库名文件顺手删"
    bad = session.save(replace(base, route_db="appdb"))
    session.path_for(bad).with_suffix(session.ROUTE_SUFFIX).write_text("app db\n", encoding="utf-8")
    with pytest.raises(session.ConfigError, match="业务库名不合法"):
        session._read(session.path_for(bad))


# ============================================================ 登录核实

_LOGIN = _ROOT / "skills" / "gaussdb-login" / "scripts" / "login.py"


def _load():
    spec = importlib.util.spec_from_file_location("login_dbname_under_test", _LOGIN)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


login = _load()
ROW = [{"db": "postgres", "usr": "grmp_ro", "databases": "appdb, postgres"}]


class _Client:
    def __init__(self, declared):
        self.declared = declared

    def registered(self, n):
        return n in self.declared

    def declares(self, n, p):
        return p in self.declared.get(n, ())


class _Runner:
    def __init__(self, conn, declared, probe_db=None, error=None):
        self.conn, self.client, self.probe_db, self.error = conn, _Client(declared), probe_db, error

    def run(self, script, values=None):
        if script == routing.PROBE:
            if self.error:
                raise RuntimeError(self.error)
            return [{"db": self.probe_db if self.conn.route_db else "postgres", "usr": "grmp_ro"}]
        return ROW


FULL = {n: ("dbName",) for n in routing.BUSINESS_DB_SCRIPTS}


@pytest.fixture
def api_home(tmp_path, monkeypatch):
    monkeypatch.setenv("GSDB_HOME", str(tmp_path))
    monkeypatch.setenv("GRMP_AUTH_TOKEN", "test-token")
    (tmp_path / "config.yaml").write_text("connection_mode: api\napi_connection:\n  - host: 127.0.0.1\n    port: 8769\n    token_env: GRMP_AUTH_TOKEN\n", encoding="utf-8")
    monkeypatch.delenv(session.ENV_HANDLE, raising=False)
    session.use(None)
    monkeypatch.setattr(login, "_verify", lambda conn: (True, "ok"))
    monkeypatch.setattr(login, "_probe_role", lambda conn: "主库（in_recovery=false）")
    monkeypatch.setattr(login, "whitelist", lambda conn: ["health.overview"])


def _login(monkeypatch, declared, probe_db="appdb", error=None):
    monkeypatch.setattr(login.access, "runner_for", lambda c: _Runner(c, declared, probe_db, error))
    return login.main(["--ip", DATA_IP, "--database", "appdb"])


def _session():
    return session._read(sorted(session._dir().glob("*.yaml"))[-1])


def test_verified_switch_records_the_routing_database_and_says_direct(api_home, monkeypatch, capsys):
    assert _login(monkeypatch, FULL) == 0
    out = capsys.readouterr().out
    conn = _session()
    assert conn.route_db == "appdb" and conn.exec_database == "" and conn.database == "appdb"
    assert "已直连" in out and "已切到业务库「appdb」" in out and "未直连" not in out


def test_partial_whitelist_still_switches_but_names_what_is_left(api_home, monkeypatch, capsys):
    declared = dict(FULL)
    declared["sqltune.indexes"] = ()
    assert _login(monkeypatch, declared) == 0
    assert "还有 1 条该切库的脚本没带 dbName" in capsys.readouterr().out and _session().route_db == "appdb"


@pytest.mark.parametrize("declared,probe_db,error,why", [
    ({}, "appdb", None, "还没有探针"),
    ({routing.PROBE: ()}, "appdb", None, "没有声明 dbName"),
    (FULL, "postgres", None, "没有按 dbName 切库"),
    (FULL, "appdb", "agent诊断脚本执行失败", "切库调用失败"),
])
def test_unverified_switch_falls_back_to_today_and_says_why(api_home, monkeypatch, capsys, declared, probe_db, error, why):
    assert _login(monkeypatch, declared, probe_db, error) == 0
    out = capsys.readouterr().out
    conn = _session()
    assert conn.route_db == "" and conn.exec_database == "postgres", "没切过去就照旧:对象类取数在 postgres"
    assert "没有切到业务库" in out and why in out and "未直连" in out


def test_alias_with_a_database_targets_another_database_on_the_same_instance(api_home, monkeypatch, capsys):
    (pathlib.Path(session._dir()).parent / "config.yaml").write_text(
        "connection_mode: api\napi_connection:\n  - host: 127.0.0.1\n    port: 8769\n    token_env: GRMP_AUTH_TOKEN\n"
        "instance_aliases:\n  - {name: og5, ip: 10.0.0.9, database: postgres}\n", encoding="utf-8")
    monkeypatch.setattr(login.access, "runner_for", lambda c: _Runner(c, FULL, "appdb"))
    assert login.main(["--alias", "og5", "--database", "appdb"]) == 0
    assert _session().route_db == "appdb"
    assert login.main(["--alias", "og5", "--ip", DATA_IP]) == 2


def test_login_without_a_business_database_says_instance_wide_data_needs_none(api_home, monkeypatch, capsys):
    """11-11 测试环境:只给实例登录后,模型说「业务在别的库的话负载不在 Top SQL 里」—— 错的,Top SQL 是整个实例的。"""
    (pathlib.Path(session._dir()).parent / "config.yaml").write_text(
        "connection_mode: api\napi_connection:\n  - host: 127.0.0.1\n    port: 8769\n    token_env: GRMP_AUTH_TOKEN\n"
        "instance_aliases:\n  - {name: og5, ip: 10.0.0.9, database: postgres}\n", encoding="utf-8")
    monkeypatch.setattr(login.access, "runner_for", lambda c: _Runner(c, FULL, "appdb"))
    assert login.main(["--alias", "og5"]) == 0
    out = capsys.readouterr().out
    assert "取数在实例的「postgres」库上" in out and "实例上所有库的会话与 SQL 都在里面" in out and "--database <业务库>" in out
    assert _login(monkeypatch, FULL) == 0
    assert "实例上所有库的会话与 SQL 都在里面" not in capsys.readouterr().out, "已直连时用 DIRECT_NOTE,不重复"

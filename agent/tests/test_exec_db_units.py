"""要诊断的库与取数的库不同(common/exec_db.py,explain 与 sqltune 共用)。

2026-11-06 行方:中间件按实例 IP 固定连 postgres,用户要诊断同一实例上的业务库。对业务库的 SQL 现取执行计划,
在 postgres 里报「relation does not exist」—— 原因是库不对。原来 sqltune 附的「让 DBA 给执行账号设
search_path(ALTER ROLE …)」在这里是误导,而且要改数据库;explain 按 id 调用时改看 SQL 历史里的真实计划
(statement_history 只在系统库有数据、覆盖全部库,取数库是 postgres 时正好取得到)。
"""
import importlib.util
import io
import pathlib
import sys
from dataclasses import replace
from types import SimpleNamespace

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common import exec_db  # noqa: E402
from common.config import ConfigError, Connection  # noqa: E402
from common.grmp.errors import QueryError  # noqa: E402

# 多个 skill 下有同名模块,pytest 一个进程跑完全部测试:加载前清掉别人留在 sys.modules 里的那份
_SHARED = ("render", "sqlfetch", "evidence", "model", "rules", "thresholds", "util", "report", "audit", "baseline")


def _load(skill: str, name: str):
    scripts = _ROOT / "skills" / skill / "scripts"
    for m in _SHARED:
        sys.modules.pop(m, None)
    sys.path.insert(0, str(scripts))
    try:
        spec = importlib.util.spec_from_file_location(name, scripts / ("%s.py" % name.split("_for_")[0]))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(scripts))
        for m in _SHARED:
            sys.modules.pop(m, None)
    return mod


explain_mod = _load("gaussdb-explain", "explain_for_exec_db")
sqltune_mod = _load("gaussdb-sqltune", "sqltune_for_exec_db")

ROUTED = Connection(name="10-0-0-9-appdb", type="gaussdb", host="127.0.0.1", port=8769, database="appdb",
                    user="grmp", driver="grmp", data_ip="10.0.0.9", app="api", exec_database="postgres")
MISSING = ('relation "gmag.t_order" does not exist\n补充:这条 SQL 的执行 schema 是 gmag,'
           '可让 DBA 给执行账号设置 search_path(ALTER ROLE <执行账号> IN DATABASE <库> SET search_path = gmag)')


def test_routed_only_when_the_two_databases_differ(monkeypatch):
    monkeypatch.setattr(exec_db, "resolve", lambda name: ROUTED)
    assert exec_db.routed("") is ROUTED
    same = replace(ROUTED, exec_database="")
    monkeypatch.setattr(exec_db, "resolve", lambda name: same)
    assert exec_db.routed("") is None

    def _none(name):
        raise ConfigError("没有会话")
    monkeypatch.setattr(exec_db, "resolve", _none)
    assert exec_db.routed("") is None


def test_misplaced_names_both_databases_and_rules_out_search_path():
    msg = exec_db.misplaced(ROUTED, MISSING)
    assert "目标库「appdb」" in msg and "固定连「postgres」" in msg
    assert 'relation "gmag.t_order" does not exist' in msg
    assert "ALTER ROLE" not in msg and "不是权限或 search_path 的问题" in msg


class _HistRunner:
    def __init__(self, plan="Datanode Name: dn1\nSeq Scan on gmag.t_order  (actual time=0.1..9.9 rows=10 loops=1)"):
        self.plan, self.ran = plan, []

    def run(self, script, values=None):
        self.ran.append(script)
        if script == "explain.plan_from_history":
            return [{"query_plan": self.plan, "start_time": "2026-11-06 15:00:00", "db_time_ms": "12.5",
                     "n_returned_rows": "10", "schema_name": "gmag"}] if self.plan else []
        raise AssertionError("不该再去现取计划:%s" % script)


def test_explain_by_id_falls_back_to_the_recorded_plan(capsys):
    args = SimpleNamespace(sql_id="126", format="markdown", analyze=False)
    fr = SimpleNamespace(sql="SELECT * FROM gmag.t_order")
    rc = explain_mod._emit_misplaced(args, _HistRunner(), fr, ROUTED, MISSING)
    out = capsys.readouterr().out
    assert rc == 0 and "Seq Scan on gmag.t_order" in out and "Datanode Name" not in out
    assert "当时真实执行的计划" in out and "固定连「postgres」" in out


def test_explain_by_id_without_a_recorded_plan_says_why(capsys):
    args = SimpleNamespace(sql_id="126", format="markdown", analyze=False)
    rc = explain_mod._emit_misplaced(args, _HistRunner(plan=""), SimpleNamespace(sql="SELECT 1"), ROUTED, MISSING)
    err = capsys.readouterr().err
    assert rc == 2 and "SQL 历史里也没有这条的执行计划" in err and "ALTER ROLE" not in err


def test_explain_main_routes_a_missing_object_to_the_new_message(monkeypatch, capsys):
    monkeypatch.setattr(explain_mod.access, "for_conn", lambda *a, **k: _HistRunner())
    monkeypatch.setattr(explain_mod.schema_infer, "infer",
                        lambda *a, **k: SimpleNamespace(ambiguous=False, schema="", via_guess=False))

    def _missing(*a, **k):
        raise QueryError(MISSING)
    monkeypatch.setattr(explain_mod, "explain_plan_via_script", _missing)
    monkeypatch.setattr(exec_db, "resolve", lambda name: ROUTED)
    monkeypatch.setattr(sys, "stdin", io.StringIO("SELECT * FROM gmag.t_order"))
    assert explain_mod.main(["--sql-stdin"]) == 2
    err = capsys.readouterr().err
    assert "目标库「appdb」" in err and "--sql-id" in err and "ALTER ROLE" not in err


def test_sqltune_tunes_from_the_recorded_plan_when_the_table_is_in_another_database(monkeypatch, capsys):
    """行方「部分 SQL 无法调优」:现取计划找不到对象时,按 SQL 历史里的真实执行计划出分析,不再只报错。"""
    plan = ("Datanode Name: dn1\nAggregate  (actual time=900.1..900.1 rows=1 loops=1)\n"
            "  ->  Seq Scan on t_order  (actual time=0.1..880.0 rows=2000000 loops=1)\n"
            "        Filter: (to_char(created_at, 'YYYYMMDD') = '20261106')")
    monkeypatch.setattr(sqltune_mod.access, "for_conn", lambda *a, **k: _HistRunner(plan))
    monkeypatch.setattr(sqltune_mod.access, "may_provide_session", lambda *a, **k: False)
    monkeypatch.setattr(sqltune_mod.history_tune, "sql_fetch",
                        lambda r, sid: SimpleNamespace(sql="SELECT count(*) FROM gmag.t_order WHERE to_char(created_at,"
                                                          " 'YYYYMMDD') = '20261106'", schema="gmag"))

    def _missing(*a, **k):
        raise QueryError(MISSING)
    monkeypatch.setattr(sqltune_mod, "tune_by_id", _missing)
    monkeypatch.setattr(exec_db, "resolve", lambda name: ROUTED)
    assert sqltune_mod.main(["126"]) == 0
    out = capsys.readouterr().out
    assert "SQL 调优(按执行历史)— sql_id 126" in out and "要诊断的库「appdb」与取数的库「postgres」不同" in out
    assert "Seq Scan on t_order" in out and "Datanode Name" not in out and "没有经过假设索引验证" in out
    assert "## 计划里看到的问题" in out and "ALTER ROLE" not in out


def test_sqltune_replaces_the_search_path_hint(monkeypatch, capsys):
    monkeypatch.setattr(sqltune_mod.access, "for_conn", lambda *a, **k: _HistRunner())
    monkeypatch.setattr(sqltune_mod.access, "may_provide_session", lambda *a, **k: False)
    monkeypatch.setattr(sqltune_mod.history_tune, "report", lambda *a, **k: "")     # 历史里没有这条的计划

    def _missing(*a, **k):
        raise QueryError(MISSING)
    monkeypatch.setattr(sqltune_mod, "tune_by_id", _missing)
    monkeypatch.setattr(exec_db, "resolve", lambda name: ROUTED)
    assert sqltune_mod.main(["126"]) == 1
    err = capsys.readouterr().err
    assert "目标库「appdb」" in err and "gaussdb-explain --sql-id 126" in err
    assert "ALTER ROLE" not in err and "search_path =" not in err


def test_sqltune_keeps_the_old_hint_when_databases_match(monkeypatch, capsys):
    monkeypatch.setattr(sqltune_mod.access, "for_conn", lambda *a, **k: _HistRunner())
    monkeypatch.setattr(sqltune_mod.access, "may_provide_session", lambda *a, **k: False)

    def _missing(*a, **k):
        raise QueryError(MISSING)
    monkeypatch.setattr(sqltune_mod, "tune_by_id", _missing)
    monkeypatch.setattr(exec_db, "resolve", lambda name: replace(ROUTED, exec_database=""))
    assert sqltune_mod.main(["126"]) == 1
    assert "ALTER ROLE" in capsys.readouterr().err          # 同一个库里找不到对象,search_path 仍是可能的原因

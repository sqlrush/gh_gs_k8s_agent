"""四份 sqlfetch(sqlfetch / sqltune / sqlreview / proctune 各一份同源副本):
statement_history 查不了(备机上是 unlogged 表)要退到 dbe_perf.statement,不是整条命令中断;
原因要明写进结果;两边都不行才报错。
"""
import importlib.util
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common.grmp.errors import QueryError  # noqa: E402

STANDBY = "执行 x.from_history 失败：ERROR: Temporary or unlogged table cannot be accessed on the standby."
MODULES = {
    "sqlfetch": _ROOT / "skills" / "gaussdb-sqlfetch" / "scripts" / "sqlfetch.py",
    "sqltune": _ROOT / "skills" / "gaussdb-sqltune" / "scripts" / "sqlfetch.py",
    "sqlreview": _ROOT / "skills" / "gaussdb-sqlreview" / "scripts" / "sqlfetch.py",
    "proctune": _ROOT / "skills" / "gaussdb-proctune" / "scripts" / "sqlfetch.py",
}


def _load(name: str):
    path = MODULES[name]
    sys.path.insert(0, str(path.parent))            # 各 skill 自带的 render.py
    spec = importlib.util.spec_from_file_location(f"sqlfetch_{name}", path)
    if spec.name in sys.modules:
        return sys.modules[spec.name]
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod          # dataclass 装饰器要按 __module__ 回查 sys.modules,不登记会炸
    spec.loader.exec_module(mod)
    return mod


class FakeRunner:
    def __init__(self, history="raise", statement_rows=None):
        self.history = history
        self.statement_rows = statement_rows if statement_rows is not None else [
            {"query": "select * from t where id = $1"}]
        self.calls = []

    def run(self, script, values=None):
        self.calls.append(script)
        if "history" in script:
            if self.history == "raise":
                raise QueryError(STANDBY)
            return self.history
        if self.statement_rows == "raise":
            raise QueryError("statement 也炸")
        return self.statement_rows


@pytest.mark.parametrize("name", sorted(MODULES))
def test_history_error_degrades_to_statement_and_records_reason(name, capsys):
    mod = _load(name)
    r = mod.sql_fetch(FakeRunner(), "300316117")
    assert r.source == "statement" and r.sql.startswith("select") and r.normalized
    assert "standby" in r.degraded_reason
    assert "降级到 dbe_perf.statement" in capsys.readouterr().err


@pytest.mark.parametrize("name", sorted(MODULES))
def test_history_ok_is_untouched(name):
    mod = _load(name)
    runner = FakeRunner(history=[{"schema_name": "app", "query": "select 1"}])
    r = mod.sql_fetch(runner, "1")
    assert r.source == "statement_history" and r.degraded_reason == "" and runner.calls == [runner.calls[0]]


@pytest.mark.parametrize("name", sorted(MODULES))
def test_both_paths_failing_or_empty_are_reported(name):
    mod = _load(name)
    with pytest.raises(QueryError):
        mod.sql_fetch(FakeRunner(statement_rows="raise"), "1")
    with pytest.raises(ValueError) as ei:
        mod.sql_fetch(FakeRunner(statement_rows=[]), "1")
    assert "statement_history 本身不可用" in str(ei.value) and "standby" in str(ei.value)


def test_sqlfetch_report_shows_degradation():
    mod = _load("sqlfetch")
    r = mod.sql_fetch(FakeRunner(), "300316117")
    out = mod.fetch_report(r)
    assert "已降级到 `dbe_perf.statement`" in out and "主库 IP" in out and "Source: `dbe_perf.statement`" in out


def test_sqltune_statement_fallback_guesses_the_schema_from_user_name():
    """dbe_perf.statement 没有 schema_name,但有 user_name;GaussDB 默认 search_path 是 "$user", public,
    应用的表多半就在同名 schema 下。退到 statement 时拿它当推测值,来源标明是推测,别当成记录值。"""
    mod = _load("sqltune")
    r = mod.sql_fetch(FakeRunner(statement_rows=[{"query": "select 1 from t", "user_name": "app_trade"}]), "7")
    assert r.source == "statement" and r.schema == "app_trade" and r.schema_source == "user_name"
    r2 = mod.sql_fetch(FakeRunner(history=[{"schema_name": "app", "query": "select 1"}]), "7")
    assert r2.schema == "app" and r2.schema_source == "statement_history"
    r3 = mod.sql_fetch(FakeRunner(), "7")                       # 老白名单行没有 user_name 列:不崩,schema 为空
    assert r3.schema == "" and r3.schema_source == ""


def test_sqlfetch_report_points_to_by_id_and_carries_the_schema():
    """现场(客户 09-09 早):模型按 sqlfetch → explain --sql-stdin 贴文本,schema 在这一步丢了 → 400。
    sqlfetch 的收尾提示是模型下一步的直接依据:要指向按 id 的入口,贴文本时必须把 Schema 带成 --schema,不许猜。"""
    mod = _load("sqlfetch")
    r = mod.FetchResult(sql_id="1568852919", sql="select 1 from batch_job_status", schema="gmag",
                        source="statement_history", normalized=False, placeholders=0)
    out = mod.fetch_report(r)
    assert "explain.py -c <conn> --sql-id 1568852919" in out
    assert "sqltune.py -c <conn> 1568852919" in out
    assert "--schema gmag" in out and "不要猜" in out
    r2 = mod.FetchResult(sql_id="7", sql="select 1 from t", schema="",
                         source="statement", normalized=True, placeholders=1)
    out2 = mod.fetch_report(r2)
    assert "--schema " not in out2 and "问用户" in out2

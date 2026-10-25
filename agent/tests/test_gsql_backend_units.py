# tests/test_gsql_backend_units.py
import sys, pathlib, types
_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402
from common.config import Connection  # noqa: E402
from common.backends.base import DBError  # noqa: E402
from common.backends import gsql_backend as gb  # noqa: E402

def _conn(**kw):
    base = dict(name="a", type="opengauss", host="h", port=5432,
                database="d", user="u", driver="gsql")
    base.update(kw)
    return Connection(**base)

class FakeCompleted:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err

def _patch(monkeypatch, *, rc=0, out="", err="", sink=None):
    monkeypatch.setattr(gb.shutil, "which", lambda x: "/usr/bin/gsql")
    def fake_run(argv, **kw):
        if sink is not None:
            sink.append((argv, kw))
        return FakeCompleted(rc=rc, out=out, err=err)
    monkeypatch.setattr(gb.subprocess, "run", fake_run)

def test_open_verifies_with_select_1(monkeypatch):
    calls = []
    _patch(monkeypatch, out="[{\"?column?\":1}]\n", sink=calls)
    b = gb.GsqlBackend.open(_conn(), "secret")
    assert isinstance(b, gb.GsqlBackend)
    # 验活确有一次调用，且发出的就是 SELECT 1
    assert calls
    assert any("SELECT 1" in a for a in calls[0][0])

def test_password_goes_via_env_not_argv(monkeypatch):
    calls = []
    _patch(monkeypatch, out="[{\"?column?\":1}]\n", sink=calls)
    gb.GsqlBackend.open(_conn(), "secretpw")
    argv, kw = calls[0]
    assert "secretpw" not in " ".join(argv)
    assert kw["env"]["PGPASSWORD"] == "secretpw"

def test_missing_binary_raises_dberror(monkeypatch):
    monkeypatch.setattr(gb.shutil, "which", lambda x: None)
    with pytest.raises(DBError):
        gb.GsqlBackend.open(_conn(), "pw")

def test_query_wraps_select_and_parses_json(monkeypatch):
    calls = []
    _patch(monkeypatch, out='[{"a":1,"b":"x"}]\n', sink=calls)
    b = gb.GsqlBackend.open(_conn(), "pw", read_only=False)
    calls.clear()
    cols, rows = b.query("SELECT a, b FROM t")
    assert cols == ["a", "b"] and rows == [(1, "x")]
    sent = calls[-1][0]
    assert any("json_agg(row_to_json(_t))" in a for a in sent)

def test_read_only_prefix_present(monkeypatch):
    calls = []
    _patch(monkeypatch, out="[]\n", sink=calls)
    b = gb.GsqlBackend.open(_conn(), "pw", read_only=True)
    calls.clear()
    b.query("SELECT 1")
    sent = " ".join(calls[-1][0])
    assert "default_transaction_read_only = on" in sent

def test_show_uses_text_bypass(monkeypatch):
    # 桩数据必须是 gsql **真实**的输出格式：-A 不带 -t 时首行是列名、
    # 末行是 (N rows) 页脚。此前这里桩的是 "on\n"（-t 的格式），于是
    # 「文本旁路返回空列名」这个真 bug 被 mock 遮住了 —— runner 拿 cols
    # 判断有没有结果集,空列名会让 explain 在 driver: gsql 下整个不可用。
    calls = []
    _patch(monkeypatch, out="enable_wdr_snapshot\non\n(1 row)\n", sink=calls)
    b = gb.GsqlBackend.open(_conn(), "pw", read_only=False)
    calls.clear()
    cols, rows = b.query("SHOW enable_wdr_snapshot")
    assert cols == ["enable_wdr_snapshot"], "文本旁路必须给出列名"
    assert rows == [("on",)]
    sent = calls[-1][0]
    assert "json_agg" not in " ".join(sent)
    assert "-t" not in sent, "文本旁路要带表头跑，否则拿不到列名"


def test_explain_text_path_returns_query_plan_column(monkeypatch):
    """psycopg2 跑 EXPLAIN 给出列名 QUERY PLAN —— gsql 必须给出同一形状。

    否则 runner 的 `if not cols` 把它判成「未返回结果集」，
    explain / proctune / sqltune 的 plan_text 在 driver: gsql 下全部跑不了。
    """
    _patch(monkeypatch, out="QUERY PLAN\nSeq Scan on t  (cost=0.00..1.00)\n(1 row)\n")
    b = gb.GsqlBackend.open(_conn(), "pw", read_only=False)
    cols, rows = b.query("EXPLAIN SELECT * FROM t")
    assert cols == ["QUERY PLAN"]
    assert rows == [("Seq Scan on t  (cost=0.00..1.00)",)]


def test_password_never_appears_in_argv_and_goes_through_stdin(monkeypatch):
    """openGauss 的 gsql **不认 PGPASSWORD** —— 实测只给它会卡在交互口令
    提示直到超时。口令必须走 -2（stdin），而且绝不能进 argv（ps 能看见）。
    """
    calls = []
    _patch(monkeypatch, out='[{"?column?":1}]\n', sink=calls)
    gb.GsqlBackend.open(_conn(), "s3cretpw")
    argv, kw = calls[0]
    assert "-2" in argv, "没走 -2 的话 openGauss 会去等交互口令，一直卡到超时"
    assert "s3cretpw" not in " ".join(argv)
    assert kw.get("input", "").strip() == "s3cretpw"


def test_empty_result_still_reports_column_names(monkeypatch):
    """**「查到 0 行」和「这条语句没有结果集」是两件事。**

    零行时 json_agg 返回 NULL，列名无从得知；不补的话 runner 会把空结果
    报成「未返回结果集」—— slowsql 阈值调高查不到慢 SQL 就失败，
    memanalyze 探测不存在的视图直接抛 Traceback。
    """
    outs = iter(['[]\n', 'sql_id|elapsed\n(0 rows)\n'])
    calls = []
    monkeypatch.setattr(gb.shutil, "which", lambda x: "/usr/bin/gsql")

    def fake_run(argv, **kw):
        calls.append(argv)
        return FakeCompleted(rc=0, out=next(outs))

    monkeypatch.setattr(gb.subprocess, "run", fake_run)
    b = gb.GsqlBackend(_conn(), "pw", "/usr/bin/gsql", read_only=False)
    cols, rows = b.query("SELECT sql_id, elapsed FROM slow")
    assert rows == []
    assert cols == ["sql_id", "elapsed"], "空结果丢了列名"
    assert "WHERE false" in " ".join(calls[-1]), "列名探测该用 WHERE false"

def test_query_in_rollback_wraps_begin_rollback(monkeypatch):
    calls = []
    _patch(monkeypatch, out="Seq Scan\n", sink=calls)
    b = gb.GsqlBackend.open(_conn(), "pw", read_only=False)
    calls.clear()
    b.query_in_rollback("EXPLAIN ANALYZE INSERT INTO t VALUES (1)")
    sent = " ".join(calls[-1][0])
    assert "BEGIN;" in sent and "ROLLBACK;" in sent

def test_query_in_rollback_with_timeout(monkeypatch):
    calls = []
    _patch(monkeypatch, out="Seq Scan\n", sink=calls)
    b = gb.GsqlBackend.open(_conn(), "pw", read_only=False)
    b.set_statement_timeout(5)
    calls.clear()
    b.query_in_rollback("EXPLAIN ANALYZE INSERT INTO t VALUES (1)")
    sent = " ".join(calls[-1][0])
    assert "BEGIN;" in sent and "ROLLBACK;" in sent
    assert "SET statement_timeout = 5000;" in sent
    assert "INSERT INTO t VALUES (1)" in sent

def test_sql_error_raises_parsed_dberror(monkeypatch):
    _patch(monkeypatch, rc=1, err='gsql: ERROR:  42P01: relation "x" does not exist\n')
    # 直接构造实例（绕过 open 的验活）以测查询错误路径：
    inst = gb.GsqlBackend(_conn(), "pw", "/usr/bin/gsql", read_only=False)
    with pytest.raises(DBError) as ei:
        inst.query("SELECT * FROM x")
    assert "42P01" in str(ei.value)

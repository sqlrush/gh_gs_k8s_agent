"""DB-free unit tests for gaussdb-sqltune's system-SQL policy gate.

策略：系统表/系统视图上的慢 SQL 不做调优——采集证据之前就判定并跳过。
跳过是确定性结论,必须 exit 0：现场 agent 把非零退出当可重试故障,
会对同一条 SQL 反复重跑（客户现场即出现过重试一小时的情况）。
"""
import io
import json
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "skills" / "gaussdb-sqltune" / "scripts"))

import evidence  # noqa: E402
import sqltune  # noqa: E402
import systables  # noqa: E402
import verify  # noqa: E402

from common import access  # noqa: E402


# --- extract_table_refs (schema preserved) -----------------------------------

def test_refs_keep_schema():
    assert evidence.extract_table_refs(
        "SELECT * FROM dbe_perf.statement") == ["dbe_perf.statement"]


def test_refs_dedup_keeps_distinct_schemas():
    got = evidence.extract_table_refs(
        "SELECT * FROM a.t JOIN b.t ON 1=1 JOIN a.t ON 1=1")
    assert got == ["a.t", "b.t"]


def test_extract_tables_behavior_unchanged():
    # 原有去 schema 语义不能被重构破坏(coltypes/证据采集都依赖它)。
    assert evidence.extract_tables("SELECT * FROM public.orders") == ["orders"]
    assert evidence.extract_tables("SELECT * FROM a.t JOIN b.t ON 1=1") == ["t"]


# --- system verdict ----------------------------------------------------------

def test_verdict_dbe_perf_is_system():
    v = systables.system_verdict(
        "SELECT unique_sql_id FROM dbe_perf.statement ORDER BY total_elapse_time")
    assert v.is_system and v.system_objects == ["dbe_perf.statement"]


def test_verdict_unqualified_pg_and_gs_prefixes():
    v = systables.system_verdict(
        "SELECT * FROM pg_class c JOIN gs_session_memory_detail m ON 1=1")
    assert v.is_system and v.system_objects == ["pg_class", "gs_session_memory_detail"]


def test_verdict_user_table_not_system():
    v = systables.system_verdict("SELECT * FROM demo_mem.big_orders WHERE id = 1")
    assert not v.is_system and v.system_objects == []


def test_verdict_mixed_is_not_system():
    # 用户表 join 系统视图 → 是用户 SQL,照常调优(只有全系统对象才拦)。
    v = systables.system_verdict(
        "SELECT * FROM app.orders o JOIN pg_class c ON c.relname = o.tbl")
    assert not v.is_system and v.system_objects == ["pg_class"]


def test_verdict_user_schema_masquerade_not_system():
    # 用户 schema 下恰好叫 pg_* 的表不是系统对象——schema 限定优先于前缀。
    assert not systables.system_verdict("SELECT * FROM app.pg_backup").is_system


def test_verdict_no_tables_not_system():
    assert not systables.system_verdict("SELECT 1").is_system


def test_skip_report_names_objects_and_forbids_retry():
    out = systables.skip_report(["dbe_perf.statement", "pg_class"])
    assert "dbe_perf.statement" in out and "pg_class" in out
    assert "按策略跳过" in out and "重试" in out


def test_skip_json_payload():
    assert systables.skip_json(["pg_class"]) == {
        "skipped": True, "reason": "system-sql", "system_objects": ["pg_class"]}


# --- _tune gate fires before runner/DB work ----------------------------------

class _BoomRunner:
    def run(self, *a, **k):
        raise AssertionError("system SQL must be rejected before calling the runner")


def test_tune_gate_rejects_system_sql_before_runner():
    with pytest.raises(systables.SystemSQLSkipped) as ei:
        sqltune._tune(_BoomRunner(), None,
                      original_sql="SELECT * FROM dbe_perf.statement",
                      binds=[], do_analyze=False)
    assert ei.value.objects == ["dbe_perf.statement"]


# --- CLI exit-code contract --------------------------------------------------

def _run_cli(monkeypatch, capsys, sql: str, argv: list[str]) -> tuple[int, str]:
    monkeypatch.setattr(access, "for_conn", lambda *a, **k: _BoomRunner())
    monkeypatch.setattr(access, "may_provide_session", lambda *a, **k: False)
    monkeypatch.setattr(sys, "stdin", io.StringIO(sql))
    rc = sqltune.main(argv)
    return rc, capsys.readouterr().out


def test_cli_skip_exits_zero_markdown(monkeypatch, capsys):
    rc, out = _run_cli(monkeypatch, capsys, "SELECT * FROM dbe_perf.statement",
                       ["-c", "og", "--sql-stdin"])
    assert rc == 0
    assert "按策略跳过" in out and "dbe_perf.statement" in out


def test_cli_skip_exits_zero_json(monkeypatch, capsys):
    rc, out = _run_cli(monkeypatch, capsys, "SELECT * FROM pg_settings",
                       ["-c", "og", "--sql-stdin", "--format", "json"])
    assert rc == 0
    assert json.loads(out) == {"skipped": True, "reason": "system-sql",
                               "system_objects": ["pg_settings"]}


# --- verify.py guards --------------------------------------------------------

def test_verify_precheck_rejects_system_sql():
    with pytest.raises(ValueError, match="系统"):
        verify._precheck("SELECT * FROM pg_class", "SELECT relname FROM pg_class", "rewrite")


def test_verify_precheck_allows_user_sql():
    verify._precheck("SELECT * FROM app.orders", "SELECT id FROM app.orders", "rewrite")


def test_index_ddl_guard_rejects_parallel_build():
    with pytest.raises(ValueError, match="并行"):
        verify._check_index_ddl(
            ["CREATE INDEX idx_o ON app.orders(id) WITH (parallel_workers=8)"])


def test_index_ddl_guard_allows_plain_ddl():
    verify._check_index_ddl(["CREATE INDEX idx_o ON app.orders(id)"])

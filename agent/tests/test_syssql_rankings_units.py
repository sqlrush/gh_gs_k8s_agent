"""慢 SQL / Top SQL 排行排除内核系统 SQL(2026-10-22 客户要求)—— slowsql 与 topsql 的接线。

钉住:系统 SQL 不上榜、榜不因排除而变短(多取了余量)、报告里写明排除了几条、--include-system 原样保留、
正文截断(80 / 180 字)时按 sql_id 补全文再判。判定本身见 test_syssql_units.py。
"""
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
for _d in ("gaussdb-slowsql", "gaussdb-topsql"):
    sys.path.insert(0, str(_ROOT / "skills" / _d / "scripts"))
for _m in ("slowsql", "topsql", "delta", "span", "render"):
    sys.modules.pop(_m, None)

import pytest  # noqa: E402

import slowsql  # noqa: E402
import delta as delta_mod  # noqa: E402


@pytest.fixture(autouse=True)
def _state_dir(monkeypatch, tmp_path):
    """判定缓存落在状态目录:测试里指到临时目录,不碰开发机上真的 GSDB_HOME。"""
    monkeypatch.setenv("GSDB_HOME", str(tmp_path / "gdaa"))

WDR_THREAD = "select ?, ?, t.* from dbe_perf.summary_statio_all_indexes t"
TOOL_LONG = ("SELECT schema_name, to_char(start_time, ?) AS start_time, round(db_time / ?, ?) AS db_time_ms, "
             "n_returned_rows, query_plan FROM dbe_perf.statement_history WHERE unique_query_id = ? "
             "ORDER BY start_time DESC LIMIT ?")


class _Runner:
    """slowsql.slow_sql 按 limit 截;补全文的脚本按 sql_id 查表。"""

    def __init__(self, rows, full=None):
        self.rows, self.full, self.calls = rows, full or {}, []

    def run(self, script, values=None):
        self.calls.append((script, dict(values or {})))
        if script == "slowsql.slow_sql":
            return self.rows[: values["limit"]]
        if script == "sqlfetch.from_statement":
            q = self.full.get(str(values["sid"]))
            return [{"query": q, "user_name": "gaussdb"}] if q else []
        return []


def _slow(sid, query, total="9.0"):
    return {"unique_sql_id": sid, "query": query, "calls": "3", "avg_ms": "3000", "total_sec": total,
            "cpu_sec": "1.0", "rows": "1"}


def test_slowsql_drops_system_sql_keeps_the_list_full_and_says_so():
    rows = [_slow("1", WDR_THREAD, "40.0"), _slow("2", "UPDATE fl.a SET x = ? WHERE id = ?"),
            _slow("3", TOOL_LONG[:180], "12.5"), _slow("4", "SELECT * FROM fl.b WHERE k = ?"),
            _slow("5", "SELECT * FROM fl.c")]
    r = _Runner(rows, full={"3": TOOL_LONG})
    got, screening = slowsql.slow_sql_screened(r, 1000, 3, "2026-10-01 00:00:00", False)
    assert [x.sql_id for x in got] == ["2", "4", "5"], "排除两条后仍要凑满 3 条"
    assert r.calls[0][1]["limit"] == 3 + slowsql.syssql.SCREEN_MARGIN, "要多取余量"
    assert ("sqlfetch.from_statement", {"sid": 3}) in r.calls, "180 字截断的那条要补全文再判"
    assert screening.excluded_ids == ["1", "3"]
    assert "已排除 2 条内核系统 SQL" in screening.note() and "52.5 秒" in screening.note()


def test_slowsql_include_system_keeps_everything():
    rows = [_slow("1", WDR_THREAD), _slow("2", "UPDATE fl.a SET x = ?")]
    r = _Runner(rows)
    got, screening = slowsql.slow_sql_screened(r, 1000, 5, "2026-10-01 00:00:00", False, include_system=True)
    assert [x.sql_id for x in got] == ["1", "2"] and screening.note() == ""
    assert r.calls[0][1]["limit"] == 5


def test_slowsql_markdown_carries_the_note_and_the_way_back(monkeypatch, capsys):
    rows = [_slow("1", WDR_THREAD), _slow("2", "UPDATE fl.a SET x = ?")]
    monkeypatch.setattr(slowsql.access, "for_conn", lambda conn, timeout=None: _Runner(rows))
    monkeypatch.setattr(slowsql.cli, "apply_session_arg", lambda args: None)
    assert slowsql.main(["-c", "x"]) == 0
    out = capsys.readouterr().out
    assert "已排除 1 条内核系统 SQL" in out and "--include-system" in out
    assert "summary_statio_all_indexes" not in out


def test_slowsql_says_the_ranking_was_all_system_sql_not_that_stats_are_off(monkeypatch, capsys):
    """og5 实测:按平均耗时排的前 110 条全是系统 SQL —— 不能报「No matching statements / 检查 enable_stmt_track」。"""
    monkeypatch.setattr(slowsql.access, "for_conn", lambda conn, timeout=None: _Runner([_slow("1", WDR_THREAD)]))
    monkeypatch.setattr(slowsql.cli, "apply_session_arg", lambda args: None)
    assert slowsql.main(["-c", "x"]) == 0
    out = capsys.readouterr().out
    assert "平均耗时超过 1000 ms 的 1 条全是内核系统 SQL,没有业务语句" in out and "enable_stmt_track" not in out


def _drow(sid, query, total):
    return delta_mod.DeltaRow(sql_id=sid, query=query, calls=1, total_sec=total, avg_ms=total * 1000,
                              rows=1, is_new=False)


def _sample(at, rows):
    return delta_mod.to_sample(at, [delta_mod._Row(sql_id=s, calls=c, total_sec=t, rows=1, query=q)
                                    for s, q, c, t in rows])


def test_topsql_window_screens_before_cutting_the_list():
    """增量榜在截前 limit 条**之前**筛 —— 先截后筛,榜就短了。"""
    import topsql
    before = _sample("20261022T010000Z", [("1", WDR_THREAD, 1, 1.0), ("2", "UPDATE fl.a SET x = ?", 1, 1.0),
                                          ("3", "SELECT * FROM fl.b", 1, 1.0)])
    after = _sample("20261022T011000Z", [("1", WDR_THREAD, 9, 90.0), ("2", "UPDATE fl.a SET x = ?", 5, 50.0),
                                         ("3", "SELECT * FROM fl.b", 3, 30.0)])
    res = delta_mod.diff(before, after, 600, limit=2, screen=topsql._screener(_Runner([]), False))
    assert [r.sql_id for r in res.rows] == ["2", "3"]
    assert res.system_sql["excluded"] == ["1"] and "--include-system" in topsql._screen_line(res.system_sql)
    plain = delta_mod.diff(before, after, 600, limit=2)
    assert [r.sql_id for r in plain.rows] == ["1", "2"] and plain.system_sql is None


def test_topsql_cumulative_json_keeps_its_shape_and_archives_the_exclusion(monkeypatch, capsys, tmp_path):
    import json
    import topsql
    monkeypatch.setenv("GSDB_REPORTS_DIR", str(tmp_path))

    class R:
        def run(self, script, values=None):
            if script == "topsql.top_sql":
                return [{"unique_sql_id": s, "query": q, "calls": "1", "total_sec": t, "avg_ms": "1", "rows": "1"}
                        for s, q, t in [("1", WDR_THREAD, "50"), ("2", "UPDATE fl.a SET x = ?", "40"),
                                        ("3", "SELECT * FROM fl.b", "30")]][: values["limit"]]
            return []

    monkeypatch.setattr(topsql.access, "for_conn", lambda conn, timeout=None: R())
    monkeypatch.setattr(topsql.cli, "apply_session_arg", lambda args: None)
    monkeypatch.setattr(topsql.common.config, "resolved_name", lambda c: "x")
    assert topsql.main(["-c", "x", "--format", "json", "--limit", "2"]) == 0
    cap = capsys.readouterr()
    assert [r["sql_id"] for r in json.loads(cap.out)] == ["2", "3"], "stdout 仍是行列表"
    assert "已排除 1 条内核系统 SQL" in cap.err
    saved = [json.loads(p.read_text(encoding="utf-8")) for p in tmp_path.rglob("*.json")]
    assert any(isinstance(d, dict) and (d.get("system_sql") or {}).get("excluded") == ["1"] for d in saved)

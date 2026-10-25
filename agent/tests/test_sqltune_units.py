"""DB-free unit tests for sqltune's ported pure-logic modules."""
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "skills" / "gaussdb-sqltune" / "scripts"))

import placeholder  # noqa: E402
import evidence  # noqa: E402
import render  # noqa: E402
import sqlfetch  # noqa: E402


# --- placeholder substitution ------------------------------------------------

def test_substitute_no_placeholders():
    r = placeholder.substitute("SELECT 1", [])
    assert r.placeholders == 0
    assert r.sql == "SELECT 1"


def test_substitute_limit_offset():
    r = placeholder.substitute("SELECT * FROM t LIMIT ? OFFSET ?", [])
    assert [s.value for s in r.substitutions] == ["100", "0"]
    assert "LIMIT 100 OFFSET 0" in r.sql


def test_substitute_int_vs_text_vs_date():
    r = placeholder.substitute(
        "SELECT * FROM t WHERE user_id = ? AND name = ? AND created_at >= ?", [])
    assert [s.value for s in r.substitutions] == ["1", "'test'", "'2024-01-01'"]


def test_substitute_typed_date_literal():
    # DATE ?/TIMESTAMP ?/TIME ? must become quoted literals (not a bare number,
    # which yields "syntax error near N").
    assert placeholder.substitute(
        "SELECT * FROM t WHERE d >= DATE ?", []).substitutions[0].value == "'2024-01-01'"
    assert placeholder.substitute(
        "SELECT * FROM t WHERE ts > TIMESTAMP ?", []).substitutions[0].value == "'2024-01-01 00:00:00'"
    # A column literally named order_date must NOT trip the keyword rule; the
    # '=' op rule still gives it a valid quoted date value.
    assert placeholder.substitute(
        "SELECT * FROM t WHERE order_date = ?", []).substitutions[0].value == "'2024-01-01'"


def test_substitute_to_char_followup():
    r = placeholder.substitute("SELECT * FROM t WHERE TO_CHAR(d, ?) = ?", [])
    vals = [s.value for s in r.substitutions]
    assert vals == ["'YYYY-MM-DD'", "'2024-01-15'"]
    assert r.substitutions[1].source == "rule-format-followup"


def test_substitute_bind_override():
    r = placeholder.substitute("SELECT * FROM t WHERE id = ?", ["42"])
    assert r.substitutions[0].value == "42"
    assert r.substitutions[0].source == "bind"


def test_substitute_bind_serializes_timestamp_and_text_values():
    r = placeholder.substitute(
        "SELECT * FROM t WHERE created_at >= TIMESTAMP ? AND name = ?",
        ["2024-01-01 00:00:00", "O'Reilly"],
        [None, "varchar"],
    )
    assert [s.value for s in r.substitutions] == [
        "'2024-01-01 00:00:00'", "'O''Reilly'",
    ]
    assert "TIMESTAMP '2024-01-01 00:00:00'" in r.sql


def test_substitute_bind_preserves_explicit_literals_and_numbers():
    r = placeholder.substitute(
        "SELECT * FROM t WHERE created_at >= TIMESTAMP ? AND level = ?",
        ["'2024-01-01 00:00:00'", "2"],
        ["timestamp without time zone", "smallint"],
    )
    assert [s.value for s in r.substitutions] == ["'2024-01-01 00:00:00'", "2"]


def test_bind_into_a_text_column_is_quoted_even_when_it_looks_numeric():
    """列类型说了算,不能因为值长得像数字就放弃加引号。

    账号/机构号/客户号这类"存在 varchar 列里的纯数字"是银行现场的主流形态;
    原样拼进去 openGauss 报 operator does not exist: character varying = bigint。
    """
    r = placeholder.substitute("SELECT 1 FROM t WHERE acct_no = ?",
                               ["6222021234567"], ["varchar"])
    assert r.substitutions[0].value == "'6222021234567'"
    r2 = placeholder.substitute("SELECT 1 FROM t WHERE org_no = ?",
                                ["001"], ["character"])
    assert r2.substitutions[0].value == "'001'"


def test_bind_of_bare_text_is_quoted_when_the_column_type_is_unknown():
    """类型探测失败时也不能把文本裸拼——那会变成标识符。

    `x = ABC` 报的是 column "abc" does not exist,这个报错完全指不到 bind 上,
    比不加引号本身更难排查。
    """
    r = placeholder.substitute("SELECT 1 FROM t WHERE x = ?", ["ABC"], [None])
    assert r.substitutions[0].value == "'ABC'"


def test_numeric_bind_stays_bare_so_limit_offset_keep_working():
    """LIMIT/OFFSET 的参数必须是裸数字,不能被顺手引起来。"""
    r = placeholder.substitute("SELECT 1 FROM t LIMIT ? OFFSET ?",
                               ["1000", "10"], [None, None])
    assert [s.value for s in r.substitutions] == ["1000", "10"]


# --- 报告里的合成值声明 --------------------------------------------------------

def _report_with(binds, types, sql="SELECT 1 FROM t WHERE a = ? AND b = ?"):
    import sqltune
    sub = placeholder.substitute(sql, binds, types)
    ev = evidence.Evidence(sql=sub.sql, version="og", plan="Seq Scan", analyzed=False)
    return sqltune.sqltune_report(sqltune.TuneResult(
        original_sql=sql, substitution=sub, evidence=ev))


def test_report_does_not_call_real_bind_values_synthetic():
    """全部值都来自 --bind 时,报告不能再自称合成值。

    SKILL.md 要求「基于合成值的倍数」必须附 caveat,报告若无条件声明合成,
    模型就会给一份真实值跑出来的结论硬加免责,把结论说弱。
    """
    out = _report_with(["42", "7"], ["integer", "integer"])
    assert "## Placeholder Substitution" in out          # 小节仍可被 SKILL.md 认出
    assert "synthetic" not in out.lower()
    assert "re-run with `--bind`" not in out


def test_report_still_warns_when_any_value_is_synthetic():
    """只要有一个值是猜的,合成值提醒必须还在——降级要说出口。"""
    out = _report_with(["42"], ["integer", "integer"])
    assert "synthetic" in out.lower()
    assert "--bind" in out


def test_substitute_skips_string_literals():
    # The ? inside the literal must NOT be treated as a placeholder.
    r = placeholder.substitute("SELECT '?' , id FROM t WHERE id = ?", [])
    assert r.placeholders == 1


def test_substitute_dollar_and_colon():
    r = placeholder.substitute("SELECT * FROM t WHERE a = $1 AND b = :2", [])
    assert r.placeholders == 2
    assert ":2" not in r.sql and "$1" not in r.sql


def test_comparison_column_handles_repeated_in_placeholders():
    sql = "SELECT * FROM customer c WHERE c.customer_level IN (?, ?, ?, ?)"
    columns = [placeholder.comparison_column(ctx)
               for ctx in placeholder.placeholder_contexts(sql)]
    assert columns == ["customer_level"] * 4


# --- table extraction --------------------------------------------------------

def test_extract_simple():
    assert evidence.extract_tables("SELECT * FROM orders") == ["orders"]


def test_extract_schema_qualified():
    assert evidence.extract_tables("SELECT * FROM public.orders") == ["orders"]


def test_extract_alias_and_comma():
    assert evidence.extract_tables("SELECT * FROM orders o, items i") == ["orders", "items"]


def test_extract_join_chain():
    got = evidence.extract_tables(
        "SELECT * FROM a JOIN b ON a.id=b.id LEFT JOIN c ON b.x=c.x")
    assert got == ["a", "b", "c"]


def test_extract_dedup():
    assert evidence.extract_tables("SELECT * FROM t JOIN t2 ON 1=1 JOIN t ON 1=1") == ["t", "t2"]


# --- is_dml ------------------------------------------------------------------

def test_is_dml():
    assert evidence.is_dml("UPDATE t SET x=1")
    assert evidence.is_dml("  delete from t")
    assert not evidence.is_dml("SELECT * FROM t")
    assert evidence.is_dml("WITH c AS (SELECT 1) INSERT INTO t SELECT * FROM c")
    assert not evidence.is_dml("WITH c AS (SELECT 1) SELECT * FROM c")


# --- render ------------------------------------------------------------------

def test_render_table_escapes_pipes_and_pads():
    out = render.table(["A", "B"], [["x|y"], ["1", "2"]])
    assert "x\\|y" in out
    lines = out.strip().split("\n")
    assert lines[0] == "| A | B |"
    assert lines[2] == "| x\\|y |  |"  # padded to 2 cols


def test_render_code_block_extends_fence():
    out = render.code_block("", "has ``` inside")
    assert out.startswith("````")  # fence longer than the inner run


def test_truncate():
    assert render.truncate("hello", 10) == "hello"
    assert render.truncate("hello", 3) == "he…"
    assert render.truncate("x", 0) == ""


# --- count_placeholders ------------------------------------------------------

def test_count_placeholders():
    assert sqlfetch.count_placeholders("a = ? and b = $1 and c = :name") == 3
    assert sqlfetch.count_placeholders("a::int") == 0  # cast, not placeholder


# --- 运行态计划(gs_get_explain,GaussDB 私有):按 sql_id 找正在执行的会话,有就附一节 ----------

from common import kernel_funcs as kf  # noqa: E402

_GAUSS = "(GaussDB Kernel V500R002C10 build 1) compiled at 2025"
_OG = "(openGauss-lite 5.0.3 build x) compiled at 2024"


class _RtRunner:
    def __init__(self, version, has_explain=True, session=True, plan="Index Scan rt"):
        self.version, self.has_explain, self.session, self.plan = version, has_explain, session, plan

    def run(self, script, values=None):
        if script == kf.PROBE_SCRIPT:
            rows = [{"item": "version", "detail": self.version}]
            if self.has_explain:
                rows.append({"item": "func:gs_get_explain", "detail": "integer"})
            return rows
        if script == kf.ACTIVE_PID_SCRIPT:
            return [{"pid": "42", "query": "select 1", "query_start": ""}] if self.session else []
        if script in (kf.RUNTIME_PLAN_SCRIPT, kf.RUNTIME_PLAN_INT4_SCRIPT):
            return [{"plan": self.plan}]
        raise AssertionError(script)


def _tr(**kw):
    import sqltune
    sub = placeholder.substitute("SELECT 1", [], [])
    ev = evidence.Evidence(sql="SELECT 1", version="og", plan="Seq Scan", analyzed=False)
    return sqltune.TuneResult(original_sql="SELECT 1", substitution=sub, evidence=ev, **kw)


def test_report_shows_runtime_plan_section_when_present():
    import sqltune
    out = sqltune.sqltune_report(_tr(sql_id="1", runtime_plan="Index Scan rt", runtime_plan_pid=42))
    assert "## Runtime Plan" in out and "gs_get_explain" in out and "42" in out
    assert "Index Scan rt" in out and "运行态" in out
    assert out.index("## Runtime Plan") > out.index("## Execution Plan")     # 紧跟估算计划之后


def test_report_shows_runtime_note_when_plan_unavailable():
    import sqltune
    out = sqltune.sqltune_report(_tr(sql_id="1", runtime_note="该 SQL 当前没有正在执行的会话"))
    assert "## Runtime Plan" in out and "没有正在执行" in out


def test_report_is_silent_without_runtime_info():
    import sqltune
    assert "Runtime Plan" not in sqltune.sqltune_report(_tr(sql_id="1"))


def test_runtime_plan_for_finds_active_session_and_fetches_plan():
    import sqltune
    assert sqltune._runtime_plan_for(_RtRunner(_GAUSS), "825712617") == ("Index Scan rt", 42, "")


def test_runtime_plan_for_notes_when_no_active_session():
    import sqltune
    plan, pid, note = sqltune._runtime_plan_for(_RtRunner(_GAUSS, session=False), "1")
    assert plan == "" and pid == 0 and "正在执行" in note


def test_runtime_plan_for_on_opengauss_is_silent():
    import sqltune
    assert sqltune._runtime_plan_for(_RtRunner(_OG, has_explain=False), "1") == ("", 0, "")


def test_runtime_plan_for_on_gaussdb_missing_function_explains():
    import sqltune
    plan, pid, note = sqltune._runtime_plan_for(_RtRunner(_GAUSS, has_explain=False), "1")
    assert plan == "" and "GaussDB" in note and "pg_proc" in note


def test_runtime_plan_for_empty_plan_reports_preconditions():
    import sqltune
    plan, pid, note = sqltune._runtime_plan_for(_RtRunner(_GAUSS, plan=""), "1")
    assert plan == "" and "plan_collect_thresh" in note


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ok  {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL  {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)


def test_evidence_says_when_analyze_was_requested_but_the_plan_is_an_estimate():
    """现场 sqltune.plan_text_analyze 按客户只读要求把 ANALYZE 固定关闭:要了 --analyze,
    模板回来的仍是估算计划。analyzed 必须按计划文本判(false),报告里要说明,不能写 Analyzed: true。"""
    class _R:
        def run(self, script, values=None):
            if script == evidence.VERSION_SCRIPT:
                return [{"version": "openGauss 5.0.3"}]
            if script.endswith("plan_text_analyze"):
                return [{"QUERY PLAN": "Seq Scan on t  (cost=0.00..1.00 rows=1 width=4)"}]
            return []

    ev = evidence.collect(_R(), None, "SELECT 1", True)
    assert ev.analyzed is False and ev.analyze_requested is True
    rep = evidence.evidence_report(ev)
    assert "Analyzed: false" in rep and "估算" in rep and "ANALYZE" in rep and "只读" in rep


def test_evidence_that_really_analyzed_is_reported_as_such():
    class _R:
        def run(self, script, values=None):
            if script == evidence.VERSION_SCRIPT:
                return [{"version": "openGauss 5.0.3"}]
            if script.endswith("plan_text_analyze"):
                return [{"QUERY PLAN": "Seq Scan on t  (cost=0.00..1.00 rows=1 width=4) "
                                       "(actual time=0.01..0.02 rows=1 loops=1)"}]
            return []

    ev = evidence.collect(_R(), None, "SELECT 1", True)
    assert ev.analyzed is True
    assert "估算" not in evidence.evidence_report(ev)


def test_tune_by_id_adds_the_recorded_schema_when_a_relation_is_missing(monkeypatch):
    """statement_history 里记着这条 SQL 当初执行的 schema_name,sqltune 一直取回来却没用上。
    EXPLAIN 报 relation does not exist 时至少要把它说出来:DBA 才知道该给执行账号设哪个 search_path。"""
    import pytest
    import sqltune
    from types import SimpleNamespace
    from common.grmp.errors import QueryError

    monkeypatch.setattr(sqltune, "sql_fetch", lambda runner, raw_id: SimpleNamespace(
        sql="select * from orders where status = 'NEW'", truncated=False, truncated_reason="",
        sql_id="123", source="statement_history", schema="app_trade"))

    def _boom(*a, **k):
        raise QueryError('执行脚本 sqltune.plan_text 失败：ERROR: relation "orders" does not exist (SQLSTATE 42P01)')
    monkeypatch.setattr(sqltune, "_tune", _boom)

    with pytest.raises(QueryError) as ei:
        sqltune.tune_by_id(object(), None, "123", [], False)
    msg = str(ei.value)
    assert "app_trade" in msg and "search_path" in msg and 'relation "orders" does not exist' in msg


class _SchemaRunner:
    """回答探测脚本与 *_schema 模板的 runner:看 collect 有没有把 schema 递到模板里。"""

    def __init__(self, probe_ok=True):
        self.probe_ok, self.calls = probe_ok, []

    def run(self, script, values=None):
        self.calls.append((script, dict(values or {})))
        if script == evidence.VERSION_SCRIPT:
            return [{"version": "openGauss 5.0.3"}]
        if script == "explain.multi_stmt_probe":
            return [{"ok": "1"}] if self.probe_ok else []
        if script.startswith("sqltune.plan_"):
            return [{"QUERY PLAN": "Seq Scan on orders  (cost=0.00..1.00 rows=1 width=4)"}]
        return []


def test_collect_switches_search_path_through_the_schema_template():
    """现场 400 的修法:知道 schema 就走「SET search_path; EXPLAIN」两语句模板,证据包记下切到了哪个 schema。"""
    from common import search_path as sp
    sp.reset_probe_cache()
    r = _SchemaRunner()
    ev = evidence.collect(r, None, "select * from orders", False, schema="app_trade")
    assert ("sqltune.plan_text_schema", {"sql": "select * from orders", "schema": "app_trade"}) in r.calls
    assert ev.search_path == "app_trade" and ev.search_path_note == ""
    assert "app_trade" in evidence.evidence_report(ev)


def test_collect_falls_back_to_the_plain_template_when_two_statements_are_refused():
    from common import search_path as sp
    sp.reset_probe_cache()
    r = _SchemaRunner(probe_ok=False)
    ev = evidence.collect(r, None, "select * from orders", False, schema="app_trade")
    # 2026-09-09 起:两语句跑不了不再把原文原样发过去等 400,脚本自己把表名按 schema 补全后走单语句模板
    assert ("sqltune.plan_text", {"sql": "select * from app_trade.orders"}) in r.calls
    assert ev.search_path == "" and "app_trade" in ev.search_path_note and "补全" in ev.search_path_note
    assert ev.schema == "app_trade"                       # 要求切到的 schema 留着,后面取 JSON 计划还要用
    assert ev.search_path_note in evidence.evidence_report(ev)


def test_collect_sets_search_path_on_a_raw_session_before_explain():
    """直连有原始会话时不走模板:在同一会话上先 SET search_path,后面的 EXPLAIN 与 hypopg 都受益。"""
    class _Db:
        def __init__(self):
            self.executed, self.queried = [], []

        def execute(self, sql, params=None):
            self.executed.append(sql)

        def query(self, sql, params=None):
            self.queried.append(sql)
            return ["QUERY PLAN"], [["Seq Scan on orders  (cost=0.00..1.00 rows=1 width=4)"]]

    db = _Db()
    ev = evidence.collect(_SchemaRunner(), db, "select * from orders", False, schema="app_trade")
    assert db.executed == ['SET search_path TO "app_trade", public']
    assert db.queried and db.queried[0].startswith("EXPLAIN")
    assert ev.search_path == "app_trade"


def test_explain_json_via_script_uses_the_schema_template_too():
    from common import search_path as sp
    sp.reset_probe_cache()
    r = _SchemaRunner()
    evidence.explain_json_via_script(r, "select 1", schema="app_trade")
    assert r.calls[-1] == ("sqltune.plan_json_schema", {"sql": "select 1", "schema": "app_trade"})


# ---- statement_history.schema_name 是搜索路径串(2026-10-21 现场:`fl, public` 被当成一个 schema 名整条没切)

def test_collect_switches_to_a_search_path_string_through_the_template():
    from common import search_path as sp
    sp.reset_probe_cache()
    r = _SchemaRunner()
    ev = evidence.collect(r, None, "select * from orders", False, schema="fl, public")
    # 末尾的 public 模板自己补:填进去的值与原先单 schema 一样,渲染出 SET search_path TO "fl", public
    assert ("sqltune.plan_text_schema", {"sql": "select * from orders", "schema": "fl"}) in r.calls
    assert ev.search_path == "fl, public" and ev.search_path_note == ""
    assert "不是合法标识符" not in evidence.evidence_report(ev)


def test_collect_sets_a_search_path_string_on_a_raw_session():
    class _Db:
        def __init__(self):
            self.executed = []

        def execute(self, sql, params=None):
            self.executed.append(sql)

        def query(self, sql, params=None):
            return ["QUERY PLAN"], [["Seq Scan on orders  (cost=0.00..1.00 rows=1 width=4)"]]

    db = _Db()
    ev = evidence.collect(_SchemaRunner(), db, "select * from orders", False, schema="fl, app")
    assert db.executed == ['SET search_path TO "fl", "app", public']
    assert ev.search_path == "fl, app"


def test_collect_with_a_bad_segment_switches_nothing_and_says_which():
    from common import search_path as sp
    sp.reset_probe_cache()
    r = _SchemaRunner()
    ev = evidence.collect(r, None, "select * from orders", False, schema="fl, x;drop")
    assert ev.search_path == "" and "x;drop" in ev.search_path_note and "整串不切" in ev.search_path_note
    assert all(not c[0].endswith("_schema") for c in r.calls)


def test_explain_json_via_script_takes_the_applied_search_path_back():
    """代价推演取 JSON 计划时传的是 ev.search_path(`fl, public`),要能原样再走一次模板。"""
    from common import search_path as sp
    sp.reset_probe_cache()
    r = _SchemaRunner()
    evidence.explain_json_via_script(r, "select 1", schema="fl, public")
    assert r.calls[-1] == ("sqltune.plan_json_schema", {"sql": "select 1", "schema": "fl"})


def test_tune_by_id_missing_relation_hint_with_a_search_path_string(monkeypatch):
    import pytest
    import sqltune
    from types import SimpleNamespace
    from common.grmp.errors import QueryError

    monkeypatch.setattr(sqltune, "sql_fetch", lambda runner, raw_id: SimpleNamespace(
        sql="select * from orders", truncated=False, truncated_reason="",
        sql_id="123", source="statement_history", schema="fl, public", schema_source="statement_history"))

    def _boom(*a, **k):
        raise QueryError('ERROR: relation "orders" does not exist')
    monkeypatch.setattr(sqltune, "_tune", _boom)
    with pytest.raises(QueryError) as ei:
        sqltune.tune_by_id(object(), None, "123", [], False)
    msg = str(ei.value)
    assert "SET search_path = fl, public)" in msg and "public, public" not in msg and "fl.<表>" in msg


def test_cli_schema_accepts_a_search_path_string_but_not_a_bad_segment(capsys):
    import sqltune
    assert sqltune.main(["--schema", "fl, x;drop", "--sql-stdin"]) == 2
    assert "x;drop" in capsys.readouterr().err


def test_derivation_report_degrades_to_a_note_when_calibration_hits_missing_stats(monkeypatch):
    """og5 实测:没 ANALYZE 过的表,计划里一个 Index Scan 就让 catalog.column 抛 CatalogError,
    整条 sqltune 命令 Traceback。推演是附加证据,docstring 承诺「任何一步失败都返回一段说明」——要兜到底。"""
    import sqltune
    import catalog as cat_mod
    from types import SimpleNamespace

    monkeypatch.setattr(sqltune.costconst, "from_gucs", lambda gucs: object())
    monkeypatch.setattr(sqltune.catalog, "from_evidence", lambda ev: object())
    monkeypatch.setattr(sqltune, "explain_json_via_script", lambda runner, sql, schema="": "[]")
    monkeypatch.setattr(sqltune.plantree, "parse", lambda raw: object())

    def _boom(root, factory):
        raise cat_mod.CatalogError("列 kf_orders.id 没有统计信息（pg_stats 里没有这一行）")
    monkeypatch.setattr(sqltune.calibrate, "calibrate_best_variant", _boom)

    ev = SimpleNamespace(gucs=[], tables=[], search_path="")
    out = sqltune._derivation_report(object(), None, "select 1", ev)
    assert out.startswith("\n## 代价推演") and "未进行" in out and "kf_orders.id" in out


def test_whitelist_path_refuses_dml_before_calling_the_middleware():
    """客户清单第 2 项第 3 点:中间件只受理 SELECT 的执行计划,写语句要在 skill 层拦,不能靠中间件报 400。
    直连原始会话那条路不受影响(EXPLAIN UPDATE 不执行,直连能出计划)。"""
    import pytest
    import sqltune
    from common.grmp.statement import ExplainNotAllowed

    sqltune._guard_sql("select 1 from t", False)                    # 只读照常放行
    with pytest.raises(ExplainNotAllowed) as ei:
        sqltune._guard_sql("update t set a = 1 where id = 1", False)
    msg = str(ei.value)
    assert "UPDATE" in msg and "只读" in msg and "中间件" in msg


class _TwoSchemaRunner:
    """同名表 orders 在 app 与 other 两个 schema 里各一份,列类型还不一样:看 collect / 列类型推断按 schema 过滤没有。"""

    def __init__(self):
        self.calls = []

    def run(self, script, values=None):
        self.calls.append((script, dict(values or {})))
        if script == evidence.VERSION_SCRIPT:
            return [{"version": "openGauss 5.0.3"}]
        if script == "explain.multi_stmt_probe":
            return [{"ok": "1"}]
        if script.startswith("sqltune.plan_"):
            return [{"QUERY PLAN": "Seq Scan on orders  (cost=0.00..1.00 rows=1 width=4)"}]
        if script == evidence.TABLES_SCRIPT:
            return [{"nspname": s, "relname": "orders", "relpages": "10", "reltuples": "100", "curpages": "10",
                     "relkind": "r", "size_mb": "1.0"} for s in ("app", "other")]
        if script == evidence.INDEXES_SCRIPT:
            return [{"schema_name": s, "table_name": "orders", "index_name": f"idx_{s}", "indisunique": "f",
                     "indisprimary": "f", "index_relpages": "1", "index_reltuples": "100",
                     "index_def": f"CREATE INDEX idx_{s} ON {s}.orders(id)"} for s in ("app", "other")]
        if script == evidence.COLUMN_STATS_SCRIPT:
            return [{"schemaname": s, "tablename": "orders", "attname": "status", "n_distinct": "2",
                     "null_frac": "0", "avg_width": "4", "correlation": "1", "most_common_vals": "",
                     "most_common_freqs": "", "histogram_bounds": ""} for s in ("app", "other")]
        if script == evidence.STATS_FRESHNESS_SCRIPT:
            return [{"schemaname": s, "relname": "orders", "n_live_tup": "100", "n_dead_tup": "0",
                     "last_analyze": "never", "last_autoanalyze": "never", "analyze_count": "0",
                     "autoanalyze_count": "0"} for s in ("app", "other")]
        if script == "sqltune.column_types":
            return [{"schema_name": "app", "table_name": "orders", "attname": "status", "type_name": "text"},
                    {"schema_name": "other", "table_name": "orders", "attname": "status", "type_name": "integer"}]
        return []


def test_collect_keeps_only_the_resolved_schema_rows():
    """现场同名表跨 schema:证据包不能混进别的 schema 的表、索引、列统计。"""
    from common import search_path as sp
    sp.reset_probe_cache()
    ev = evidence.collect(_TwoSchemaRunner(), None, "select * from orders where status = 'NEW'", False, schema="app")
    assert [t.schema for t in ev.tables] == ["app"]
    assert [i.name for i in ev.indexes] == ["idx_app"]
    assert len(ev.columns) == 1 and len(ev.freshness) == 1 and ev.freshness[0].schema == "app"


def test_collect_without_a_schema_keeps_everything_as_before():
    from common import search_path as sp
    sp.reset_probe_cache()
    ev = evidence.collect(_TwoSchemaRunner(), None, "select * from orders", False)
    assert sorted(t.schema for t in ev.tables) == ["app", "other"] and len(ev.indexes) == 2


def test_explicit_schema_in_sql_beats_the_resolved_one():
    from common import search_path as sp
    sp.reset_probe_cache()
    ev = evidence.collect(_TwoSchemaRunner(), None, "select * from other.orders", False, schema="app")
    assert [t.schema for t in ev.tables] == ["other"] and [i.name for i in ev.indexes] == ["idx_other"]


def test_column_type_inference_uses_the_resolved_schema():
    """两个 schema 的同名列类型冲突时原先保守放弃(退回启发式,合成 'test' 塞进数值列就是 400);
    知道 schema 就按它取,类型明确。"""
    import coltypes
    r = _TwoSchemaRunner()
    types = coltypes.infer_types(r, "select * from orders where status = ?", schema="app")
    assert types == ["text"]
    assert coltypes.infer_types(_TwoSchemaRunner(), "select * from orders where status = ?") == [None]   # 不知道 schema:冲突就放弃,老行为


# ---------------------------------------------------------------- 贴文本没带 --schema / 按 id 只有推测值 → 目录推断

class _CatalogRunner:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def run(self, script, values=None):
        self.calls.append((script, dict(values or {})))
        assert script == evidence.TABLES_SCRIPT
        return [{"nspname": s, "relname": t} for s, t in self.rows]


def test_text_without_schema_is_resolved_from_the_catalog():
    import sqltune
    r = _CatalogRunner([("gmag", "batch_job_status")])
    assert sqltune.resolve_schema(r, "select count(1) from batch_job_status", "", "") == ("gmag", "catalog")
    assert r.calls and r.calls[0][1] == {"names": "'batch_job_status'"}


def test_explicit_schema_is_never_second_guessed():
    import sqltune
    r = _CatalogRunner([("other", "t")])
    assert sqltune.resolve_schema(r, "select * from t", "gmag", "--schema") == ("gmag", "--schema")
    assert r.calls == []


def test_user_name_guess_yields_to_a_unique_catalog_match():
    import sqltune
    r = _CatalogRunner([("gmag", "t")])
    assert sqltune.resolve_schema(r, "select * from t", "kf_app", "user_name") == ("gmag", "catalog")


def test_ambiguous_tables_raise_with_candidates_instead_of_guessing():
    import pytest
    import sqltune
    r = _CatalogRunner([("gbatch", "t"), ("dsc_ora_public", "t")])
    with pytest.raises(ValueError) as ei:
        sqltune.resolve_schema(r, "select * from t", "", "")
    assert "gbatch" in str(ei.value) and "dsc_ora_public" in str(ei.value) and "--schema" in str(ei.value)
    assert sqltune.resolve_schema(r, "select * from t", "gbatch", "user_name") == ("gbatch", "user_name")   # 推测值在候选里就留


def test_nothing_in_catalog_keeps_the_old_behaviour():
    import sqltune
    r = _CatalogRunner([])
    assert sqltune.resolve_schema(r, "select * from t", "", "") == ("", "")
    assert sqltune.resolve_schema(r, "select * from t", "kf_app", "user_name") == ("kf_app", "user_name")
    assert "catalog" in sqltune._SCHEMA_SOURCE_LABEL

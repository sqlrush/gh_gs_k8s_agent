"""DB-free unit tests for proctune's procanalyze port."""
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "skills" / "gaussdb-proctune" / "scripts"))

import procanalyze as pa  # noqa: E402


# --- cursor extraction -------------------------------------------------------

def test_declare_is_eligible():
    body = "DECLARE\n  CURSOR c IS SELECT a FROM t WHERE x = v;\nBEGIN\n  NULL;\nEND;"
    curs = pa.extract_cursors(body)
    c = [x for x in curs if x.name == "c"][0]
    assert c.kind == pa.CURSOR_DECLARE_IS
    assert c.eligible
    assert c.select_sql == "SELECT a FROM t WHERE x = v"


def test_for_update_skipped():
    body = "DECLARE CURSOR c IS SELECT a FROM t FOR UPDATE; BEGIN NULL; END;"
    c = pa.extract_cursors(body)[0]
    assert not c.eligible and "FOR UPDATE" in c.skip_reason


def test_parameterized_skipped():
    body = "DECLARE CURSOR c(p int) IS SELECT a FROM t WHERE x = p; BEGIN NULL; END;"
    c = pa.extract_cursors(body)[0]
    assert not c.eligible and "参数化" in c.skip_reason


def test_open_for_execute_skipped():
    body = "BEGIN OPEN c FOR EXECUTE 'select 1'; END;"
    c = [x for x in pa.extract_cursors(body) if x.name == "c"][0]
    assert not c.eligible and "动态游标" in c.skip_reason


def test_for_loop_select():
    body = "BEGIN FOR rec IN SELECT id FROM t WHERE v = 1 LOOP NULL; END LOOP; END;"
    c = [x for x in pa.extract_cursors(body) if x.kind == pa.CURSOR_FOR_LOOP][0]
    assert c.eligible and c.select_sql == "SELECT id FROM t WHERE v = 1"


def test_for_loop_numeric_range_ignored():
    body = "BEGIN FOR i IN 1..10 LOOP NULL; END LOOP; END;"
    assert not [x for x in pa.extract_cursors(body) if x.kind == pa.CURSOR_FOR_LOOP]


# --- structural findings -----------------------------------------------------

def test_scan_dynamic_sql():
    body = "BEGIN EXECUTE 'select 1'; END;"
    kinds = {f.kind for f in pa.scan_structure(body)}
    assert "dynamic_sql" in kinds


def test_scan_per_row_dml_in_loop():
    body = "BEGIN LOOP INSERT INTO t VALUES (1); END LOOP; END;"
    kinds = {f.kind for f in pa.scan_structure(body)}
    assert "per_row_dml" in kinds


def test_scan_dml_outside_loop_not_flagged():
    body = "BEGIN INSERT INTO t VALUES (1); END;"
    kinds = {f.kind for f in pa.scan_structure(body)}
    assert "per_row_dml" not in kinds


def test_for_update_not_treated_as_dml():
    body = "BEGIN LOOP SELECT 1 FROM t FOR UPDATE; END LOOP; END;"
    kinds = {f.kind for f in pa.scan_structure(body)}
    assert "per_row_dml" not in kinds  # "FOR UPDATE" must not match the update DML rule


# --- args + vars + substitution ----------------------------------------------

def test_parse_args():
    args = pa.parse_args("p_id integer, p_status character varying DEFAULT 'x'::text")
    assert args == [pa.Arg("p_id", "integer"), pa.Arg("p_status", "character varying")]


def test_extract_vars_from_declare():
    body = "DECLARE\n  v_total numeric := 0;\n  CURSOR c IS SELECT 1;\nBEGIN\n NULL; END;"
    vars_ = pa.extract_vars(body, [pa.Arg("p_id", "integer")])
    assert vars_["p_id"] == "integer"
    assert vars_["v_total"] == "numeric"
    assert "c" not in vars_  # cursor decl skipped


def test_substitute_vars_by_type():
    vars_ = {"p_id": "integer", "p_name": "varchar", "p_day": "date"}
    r = pa.substitute_vars(
        "SELECT * FROM t WHERE id = p_id AND name = p_name AND d = p_day", vars_, {})
    assert "id = 1" in r.sql
    assert "name = 'test'" in r.sql
    assert "d = '2024-01-15'" in r.sql
    assert {s.var for s in r.subs} == {"p_id", "p_name", "p_day"}


def test_substitute_vars_bind_override():
    r = pa.substitute_vars("SELECT * FROM t WHERE id = p_id", {"p_id": "integer"}, {"p_id": "42"})
    assert "id = 42" in r.sql
    assert r.subs[0].source == "bind"


def test_substitute_vars_qualified_untouched():
    # o.p_id is a column reference, not the variable p_id.
    r = pa.substitute_vars("SELECT o.p_id FROM t o", {"p_id": "integer"}, {})
    assert "o.p_id" in r.sql
    assert not r.subs


def test_substitute_vars_string_literal_untouched():
    r = pa.substitute_vars("SELECT 'p_id' FROM t", {"p_id": "integer"}, {})
    assert "'p_id'" in r.sql
    assert not r.subs


def test_rollback_safe():
    assert pa.detect_rollback_safe("BEGIN SELECT 1; END;")
    assert not pa.detect_rollback_safe("BEGIN COMMIT; END;")


def test_record_var_names_and_reference():
    vars_ = {"rec_0": "record", "p_cust": "integer", "r_row": "orders%rowtype"}
    assert set(pa.record_var_names(vars_)) == {"rec_0", "r_row"}
    rv = ["rec_0", "r_row"]
    # Nested cursor referencing an outer record var -> detected.
    assert pa.references_record_var(
        "SELECT a FROM t WHERE order_id = rec_0.k", rv) == "rec_0"
    # A column whose name merely contains a record-var name -> NOT a match.
    assert pa.references_record_var(
        "SELECT a FROM t WHERE my_rec_0.x = 1", rv) == ""
    # No record reference -> "".
    assert pa.references_record_var(
        "SELECT a FROM t WHERE customer_id = p_cust", rv) == ""


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


def _load_proctune_evidence():
    """sqltune 也有同名 evidence / render 模块,按路径单独加载 proctune 这份,不碰 sys.modules 里的名字。"""
    import importlib.util
    scripts = _ROOT / "skills" / "gaussdb-proctune" / "scripts"
    sys.modules.pop("render", None)
    sys.path.insert(0, str(scripts))
    spec = importlib.util.spec_from_file_location("proctune_evidence_under_test", scripts / "evidence.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod   # dataclass 解析字符串注解时要按 __module__ 找到模块,否则 NoneType.__dict__
    spec.loader.exec_module(mod)
    return mod


def test_evidence_says_when_analyze_was_requested_but_the_plan_is_an_estimate():
    """现场 proctune.plan_text_analyze 按客户只读要求把 ANALYZE 固定关闭:要了 analyze,
    模板回来的仍是估算计划。analyzed 按计划文本判(false),报告里要说明。"""
    evidence = _load_proctune_evidence()

    class _R:
        def run(self, script, values=None):
            if script == evidence.DB_VERSION_SCRIPT:
                return [{"version": "openGauss 5.0.3"}]
            if script.endswith("plan_text_analyze"):
                return [{"QUERY PLAN": "Seq Scan on t  (cost=0.00..1.00 rows=1 width=4)"}]
            return []

    ev = evidence.collect(_R(), None, "SELECT 1", True)
    assert ev.analyzed is False and ev.analyze_requested is True
    rep = evidence.evidence_report(ev)
    assert "Analyzed: false" in rep and "估算" in rep and "ANALYZE" in rep


def test_cursor_evidence_switches_search_path_to_the_procedure_schema():
    """存储过程里的游标 SELECT 表名多半不带 schema——它们本来就在过程所在的 schema 下解析。"""
    from common import search_path as sp
    sp.reset_probe_cache()
    evidence = _load_proctune_evidence()

    class _R:
        def __init__(self):
            self.calls = []

        def run(self, script, values=None):
            self.calls.append((script, dict(values or {})))
            if script == evidence.DB_VERSION_SCRIPT:
                return [{"version": "openGauss 5.0.3"}]
            if script == "explain.multi_stmt_probe":
                return [{"ok": "1"}]
            if script.startswith("proctune.plan_"):
                return [{"QUERY PLAN": "Seq Scan on t  (cost=0.00..1.00 rows=1 width=4)"}]
            return []

    r = _R()
    ev = evidence.collect(r, None, "select 1 from t", False, schema="billing")
    assert ("proctune.plan_text_schema", {"sql": "select 1 from t", "schema": "billing"}) in r.calls
    assert ev.search_path == "billing"


def test_proctune_fetch_proc_def_handles_package_names():
    """proctune 与 procinfo 同一套定位逻辑:三段名定位包内过程,同名多个时拒绝。"""
    import pytest
    import importlib.util
    scripts = _ROOT / "skills" / "gaussdb-proctune" / "scripts"
    sys.modules.pop("render", None)
    sys.path.insert(0, str(scripts))
    spec = importlib.util.spec_from_file_location("proctune_main_under_test", scripts / "proctune.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    class _R:
        def run(self, script, values=None):
            v = dict(values or {})
            rows = [{"nspname": "gmag", "proname": "proc_x", "lanname": "plpgsql",
                     "prosrc": "DECLARE CURSOR c1 IS SELECT 1 FROM t; BEGIN OPEN c1; CLOSE c1; END",
                     "args": "", "package": p} for p in ("pkg_a", "pkg_b")]
            return [r for r in rows if r["proname"] == v["name"]
                    and (not v["schema"] or r["nspname"] == v["schema"])
                    and (not v["package"] or r["package"] == v["package"])]

    d = mod.fetch_proc_def(_R(), "gmag.pkg_b.proc_x")
    assert d.package == "pkg_b" and d.schema == "gmag"
    with pytest.raises(ValueError) as ei:
        mod.fetch_proc_def(_R(), "gmag.proc_x")
    assert "pkg_a" in str(ei.value) and "pkg_b" in str(ei.value)


def test_proctune_collect_filters_catalog_rows_by_procedure_schema():
    """游标 SELECT 的表按过程所在 schema 解析——目录证据也只留那个 schema 的行。"""
    from common import search_path as sp
    sp.reset_probe_cache()
    evidence = _load_proctune_evidence()

    class _R:
        def run(self, script, values=None):
            if script == evidence.DB_VERSION_SCRIPT:
                return [{"version": "openGauss 5.0.3"}]
            if script == "explain.multi_stmt_probe":
                return [{"ok": "1"}]
            if script.startswith("proctune.plan_"):
                return [{"QUERY PLAN": "Seq Scan on t  (cost=0.00..1.00 rows=1 width=4)"}]
            if script == evidence.TABLES_SCRIPT:
                return [{"nspname": s, "relname": "t", "relpages": "1", "reltuples": "1", "curpages": "1",
                         "relkind": "r", "size_mb": "0.1"} for s in ("billing", "other")]
            if script == evidence.INDEXES_SCRIPT:
                return [{"schema_name": s, "table_name": "t", "index_name": f"i_{s}", "indisunique": "f",
                         "indisprimary": "f", "index_def": f"CREATE INDEX i_{s} ON {s}.t(id)"} for s in ("billing", "other")]
            if script == evidence.COLUMN_STATS_SCRIPT:
                return [{"schemaname": s, "tablename": "t", "attname": "id", "n_distinct": "1", "null_frac": "0",
                         "avg_width": "4", "correlation": "1", "most_common_vals": "", "most_common_freqs": "",
                         "histogram_bounds": ""} for s in ("billing", "other")]
            return []

    ev = evidence.collect(_R(), None, "select 1 from t", False, schema="billing")
    assert [t.schema for t in ev.tables] == ["billing"] and [i.name for i in ev.indexes] == ["i_billing"]
    assert len(ev.columns) == 1

"""DB-free unit tests for the sqlreview skill (lexer / rules / checks / objects / report)."""
import importlib.util
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "skills" / "gaussdb-sqlreview" / "scripts"


def _load(mod: str):
    """Load under the plain module name: the skill's own modules do `from model
    import ...`, so they must resolve to the very object this test holds — a
    `sqlreview_model` alias would give them a second, unequal RuleError class."""
    path = _SCRIPTS / f"{mod}.py"
    sys.path.insert(0, str(path.parent))
    sys.path.insert(0, str(_ROOT))
    spec = importlib.util.spec_from_file_location(mod, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[mod] = m  # dataclasses resolve annotations via sys.modules
    spec.loader.exec_module(m)
    return m


# sibling imports inside the skill (e.g. `import model`) need the scripts dir first
for _m in ("model", "lexer", "rules", "checks", "objects", "report", "render"):
    sys.modules.pop(_m, None)
sys.path.insert(0, str(_SCRIPTS))

model = _load("model")
lexer = _load("lexer")
rules = _load("rules")
checks = _load("checks")
objects = _load("objects")
report = _load("report")


# --------------------------------------------------------------------------
# lexer
# --------------------------------------------------------------------------
def test_mask_preserves_length_and_newlines():
    sql = "SELECT 1; -- kill\nSELECT 2;"
    masked = lexer.mask(sql)
    assert len(masked) == len(sql)
    assert masked.count("\n") == sql.count("\n")
    assert "kill" not in masked


def test_comments_do_not_trigger_statements():
    sql = "-- DELETE FROM orders\nSELECT id FROM orders;"
    stmts = lexer.split(sql)
    assert len(stmts) == 1
    assert stmts[0].kind == "dql"
    assert "DELETE" not in stmts[0].normalized.upper()


def test_block_comment_stripped_but_line_numbers_survive():
    sql = "/* multi\n   line */\nDELETE FROM t WHERE id = 1;"
    stmts = lexer.split(sql)
    assert len(stmts) == 1
    assert stmts[0].verb == "delete"
    assert stmts[0].line == 3  # the DELETE really starts on line 3


def test_semicolon_inside_string_does_not_split():
    sql = "SELECT 'a;b' AS x FROM t;"
    stmts = lexer.split(sql)
    assert len(stmts) == 1


def test_semicolon_inside_dollar_body_does_not_split():
    sql = (
        "CREATE OR REPLACE FUNCTION f() RETURNS int AS $$\n"
        "BEGIN\n  DELETE FROM t;\n  RETURN 1;\nEND;\n$$ LANGUAGE plpgsql;"
    )
    stmts = lexer.split(sql)
    assert len(stmts) == 1
    assert stmts[0].kind == "ddl"


def test_string_literals_become_placeholders_and_are_recoverable():
    sql = "SELECT * FROM t WHERE name LIKE '%abc';"
    st = lexer.split(sql)[0]
    assert "'%abc'" not in st.normalized
    assert ":s1" in st.normalized
    assert st.literals[":s1"] == "%abc"


def test_classify_kinds():
    cases = {
        "CREATE TABLE t (id int);": ("ddl", "create_table"),
        "CREATE INDEX idx_t_a ON t (a);": ("ddl", "create_index"),
        "ALTER TABLE t ADD COLUMN b int;": ("ddl", "alter_table"),
        "INSERT INTO t VALUES (1);": ("dml", "insert"),
        "UPDATE t SET a = 1 WHERE id = 2;": ("dml", "update"),
        "DELETE FROM t WHERE id = 2;": ("dml", "delete"),
        "SELECT a FROM t;": ("dql", "select"),
        "WITH x AS (SELECT 1) SELECT * FROM x;": ("dql", "select"),
    }
    for sql, (kind, verb) in cases.items():
        st = lexer.split(sql)[0]
        assert (st.kind, st.verb) == (kind, verb), sql


def test_extract_table_and_index_names():
    st = lexer.split("CREATE TABLE public.Orders (id int);")[0]
    assert st.table == "orders"  # normalized to lowercase, schema stripped

    st = lexer.split("CREATE UNIQUE INDEX uk_o_no ON orders (order_no, tenant_id);")[0]
    assert st.index_name == "uk_o_no"
    assert st.table == "orders"
    assert st.index_cols == ("order_no", "tenant_id")


def test_quoted_identifier_is_not_a_string_literal():
    st = lexer.split('SELECT "weird;col" FROM t;')[0]
    assert st.kind == "dql"
    assert not st.literals  # double quotes are identifiers, not literals


# --------------------------------------------------------------------------
# rules
# --------------------------------------------------------------------------
_MIN = {
    "version": 1,
    "rules": [
        {
            "id": "DML001",
            "name": "禁止物理删除",
            "severity": "error",
            "applies_to": ["dml"],
            "check": "stmt_forbidden",
            "kind": "delete",
            "message": "禁止 DELETE",
        }
    ],
}


def test_parse_rules_minimal():
    rs = rules.parse_rules(_MIN, "test")
    assert len(rs) == 1
    assert rs[0].id == "DML001"
    assert rs[0].severity == model.Severity.ERROR
    assert rs[0].applies_to == ("dml",)
    assert rs[0].params["kind"] == "delete"
    assert rs[0].enabled is True


def test_unknown_check_name_is_rejected_with_rule_id():
    bad = {"version": 1, "rules": [dict(_MIN["rules"][0], check="teleport")]}
    with pytest.raises(model.RuleError) as exc:
        rules.parse_rules(bad, "test")
    assert "DML001" in str(exc.value)
    assert "teleport" in str(exc.value)


def test_invalid_severity_is_rejected():
    bad = {"version": 1, "rules": [dict(_MIN["rules"][0], severity="apocalyptic")]}
    with pytest.raises(model.RuleError):
        rules.parse_rules(bad, "test")


def test_uncompilable_pattern_is_rejected():
    bad = {
        "version": 1,
        "rules": [
            {
                "id": "X1",
                "name": "bad regex",
                "severity": "warn",
                "applies_to": ["dql"],
                "check": "regex",
                "pattern": "([unclosed",
                "message": "m",
            }
        ],
    }
    with pytest.raises(model.RuleError):
        rules.parse_rules(bad, "test")


def test_missing_required_param_is_rejected():
    bad = {
        "version": 1,
        "rules": [
            {
                "id": "X2",
                "name": "naming without pattern",
                "severity": "warn",
                "applies_to": ["ddl"],
                "check": "naming_pattern",
                "target": "table",
                "message": "m",
            }
        ],
    }
    with pytest.raises(model.RuleError):
        rules.parse_rules(bad, "test")


def test_duplicate_rule_id_is_rejected():
    dup = {"version": 1, "rules": [_MIN["rules"][0], dict(_MIN["rules"][0])]}
    with pytest.raises(model.RuleError):
        rules.parse_rules(dup, "test")


def test_disabled_rule_is_dropped():
    off = {"version": 1, "rules": [dict(_MIN["rules"][0], enabled=False)]}
    assert rules.parse_rules(off, "test") == ()


def test_shipped_rules_yaml_loads():
    """The rules.yaml we ship must itself be valid."""
    rs = rules.load_rules(rules.DEFAULT_RULES_PATH)
    assert len(rs) >= 10
    ids = [r.id for r in rs]
    assert len(ids) == len(set(ids))
    for want in ("TBL001", "TBL002", "TBL003", "IDX001", "DML001", "DQL001"):
        assert want in ids


# --------------------------------------------------------------------------
# checks — statements
# --------------------------------------------------------------------------
def _rule(**kw):
    base = {
        "id": "R1",
        "name": "r",
        "severity": model.Severity.ERROR,
        "applies_to": ("ddl", "dml", "dql"),
        "check": "regex",
        "message": "hit",
        "params": {},
    }
    base.update(kw)
    return model.Rule(**base)


def _check(sql: str, rule) -> list:
    return list(checks.check_statements(lexer.split(sql), (rule,)))


def test_stmt_forbidden_delete_hits():
    r = _rule(check="stmt_forbidden", applies_to=("dml",), params={"kind": "delete"})
    assert len(_check("DELETE FROM t WHERE id = 1;", r)) == 1
    assert _check("UPDATE t SET is_deleted = 1 WHERE id = 1;", r) == []


def test_delete_in_comment_is_not_reported():
    r = _rule(check="stmt_forbidden", applies_to=("dml",), params={"kind": "delete"})
    assert _check("-- DELETE FROM t\nSELECT 1;", r) == []


def test_leading_wildcard_like_hits_only_leading():
    r = _rule(check="leading_wildcard_like", applies_to=("dql",))
    assert len(_check("SELECT a FROM t WHERE n LIKE '%x';", r)) == 1
    assert _check("SELECT a FROM t WHERE n LIKE 'x%';", r) == []


def test_leading_wildcard_survives_literal_placeholding():
    """Regression: literals are masked, so this must be a structured check, not a regex."""
    st = lexer.split("SELECT a FROM t WHERE n LIKE '%x';")[0]
    assert "'%x'" not in st.normalized  # literal really is masked
    r = _rule(check="leading_wildcard_like", applies_to=("dql",))
    assert len(checks.check_statements((st,), (r,))) == 1


def test_table_no_primary_key():
    r = _rule(check="table_no_primary_key", applies_to=("ddl",))
    assert len(_check("CREATE TABLE t (id int, name text);", r)) == 1
    assert _check("CREATE TABLE t (id int PRIMARY KEY, name text);", r) == []
    assert _check("CREATE TABLE t (id int, PRIMARY KEY (id));", r) == []


def test_table_has_foreign_key():
    r = _rule(check="table_has_foreign_key", applies_to=("ddl",))
    assert len(_check(
        "CREATE TABLE t (id int PRIMARY KEY, uid int REFERENCES users(id));", r)) == 1
    assert len(_check(
        "ALTER TABLE t ADD CONSTRAINT fk_u FOREIGN KEY (uid) REFERENCES users(id);", r)) == 1
    assert _check("CREATE TABLE t (id int PRIMARY KEY);", r) == []


def test_naming_pattern_table():
    r = _rule(check="naming_pattern", applies_to=("ddl",),
              params={"target": "table", "pattern": r"^[a-z][a-z0-9_]{2,62}$"})
    assert len(_check("CREATE TABLE OrderItems (id int);", r)) == 1
    assert _check("CREATE TABLE order_items (id int);", r) == []


def test_naming_pattern_index():
    r = _rule(check="naming_pattern", applies_to=("ddl",),
              params={"target": "index", "pattern": r"^(idx|uk)_[a-z0-9_]+$"})
    assert len(_check("CREATE INDEX orders_a ON orders (a);", r)) == 1
    assert _check("CREATE INDEX idx_orders_a ON orders (a);", r) == []


def test_index_column_count():
    r = _rule(check="index_column_count", applies_to=("ddl",), params={"max": 3})
    assert len(_check("CREATE INDEX idx_t ON t (a, b, c, d);", r)) == 1
    assert _check("CREATE INDEX idx_t ON t (a, b, c);", r) == []


def test_dml_without_where():
    r = _rule(check="dml_without_where", applies_to=("dml",))
    assert len(_check("UPDATE t SET a = 1;", r)) == 1
    assert _check("UPDATE t SET a = 1 WHERE id = 2;", r) == []


def test_select_star():
    r = _rule(check="select_star", applies_to=("dql",))
    assert len(_check("SELECT * FROM t;", r)) == 1
    assert _check("SELECT a, b FROM t;", r) == []
    assert _check("SELECT count(*) FROM t;", r) == []  # count(*) is not SELECT *


def test_regex_rule_runs_on_normalized_by_default():
    r = _rule(check="regex", applies_to=("dql",), params={"pattern": r"(?i)\bnolock\b"})
    assert _check("SELECT a FROM t WITH (NOLOCK);", r)
    assert _check("SELECT 'nolock' FROM t;", r) == []  # literal is masked → no false hit


def test_regex_rule_on_raw_sees_literals():
    r = _rule(check="regex", applies_to=("dql",),
              params={"pattern": r"(?i)nolock", "on": "raw"})
    assert _check("SELECT 'nolock' FROM t;", r)


def test_applies_to_filters_by_kind():
    r = _rule(check="select_star", applies_to=("dql",))
    assert _check("SELECT * FROM t;", r)
    # same check, but the rule only targets dql — an INSERT..SELECT * is dml, skipped
    assert _check("INSERT INTO x SELECT * FROM t;", r) == []


def test_advisory_rule_produces_advisory_finding_not_violation():
    r = _rule(check="advisory", applies_to=("ddl",), params={"criteria": "judge me"})
    out = _check("CREATE TABLE t (id int);", r)
    assert len(out) == 1
    assert out[0].advisory is True
    assert "judge me" in out[0].rationale


# --------------------------------------------------------------------------
# checks — objects
# --------------------------------------------------------------------------
def _facts(**kw):
    base = {"tables": (), "indexes": (), "notes": ()}
    base.update(kw)
    return model.ObjectFacts(**base)


def test_object_table_no_primary_key():
    r = _rule(check="table_no_primary_key", applies_to=("object",),
              message="表 {table} 未定义主键")
    f = _facts(tables=(model.TableFact("public", "orders", False, (), ("id",)),))
    out = list(checks.check_objects(f, (r,)))
    assert len(out) == 1
    assert "orders" in out[0].message      # {table} really got substituted
    assert out[0].location == "public.orders"

    ok = _facts(tables=(model.TableFact("public", "orders", True, (), ("id",)),))
    assert list(checks.check_objects(ok, (r,))) == []


def test_object_table_has_foreign_key():
    r = _rule(check="table_has_foreign_key", applies_to=("object",))
    f = _facts(tables=(model.TableFact("public", "t", True, ("fk_t_u",), ("id",)),))
    assert len(list(checks.check_objects(f, (r,)))) == 1


def test_object_index_redundant():
    r = _rule(check="index_redundant", applies_to=("object",),
              message="索引 {index} 被 {covered_by} 的前缀覆盖")
    f = _facts(indexes=(
        model.IndexFact("public", "t", "idx_a", ("a",), False, False, 0),
        model.IndexFact("public", "t", "idx_a_b", ("a", "b"), False, False, 0),
    ))
    out = list(checks.check_objects(f, (r,)))
    assert len(out) == 1
    # the shorter index is the redundant one, and it is named as such
    assert out[0].message == "索引 idx_a 被 idx_a_b 的前缀覆盖"


def test_object_index_not_redundant_when_prefix_differs():
    r = _rule(check="index_redundant", applies_to=("object",))
    f = _facts(indexes=(
        model.IndexFact("public", "t", "idx_a", ("a",), False, False, 0),
        model.IndexFact("public", "t", "idx_b_a", ("b", "a"), False, False, 0),
    ))
    assert list(checks.check_objects(f, (r,))) == []


# --------------------------------------------------------------------------
# objects (I/O layer, FakeDB)
# --------------------------------------------------------------------------
class _FakeRunner:
    """按脚本逻辑名返回预置行；`fail` 里列到的脚本抛 QueryError。

    行是**全字符串的列名字典** —— 与统一入口两条真实路径同形。这一点是刻意的：
    若在这里喂原生 bool/list，objects.py 里那两个「字符串化」的坑就测不到。
    """

    def __init__(self, fail=()):
        self.fail = fail
        self.calls = []

    def run(self, script_name, values=None):
        from common import access
        self.calls.append((script_name, values))
        if script_name in self.fail:
            raise access.QueryError(f"permission denied for {script_name}")
        if script_name == objects.TABLES_SCRIPT:
            return [{"schema": "public", "table": "orders", "has_pk": "t",
                     "fks": "fk_orders_user", "columns": "id,uid"}]
        if script_name == objects.INDEXES_SCRIPT:
            return [{"schema": "public", "table": "orders",
                     "name": "idx_orders_status", "columns": "status",
                     "is_unique": "f", "is_primary": "f", "scans": "0"}]
        return []


def test_collect_facts_builds_immutable_facts():
    facts = objects.collect_facts(_FakeRunner(), "public")
    assert facts.tables[0].table == "orders"
    assert facts.tables[0].has_pk is True
    assert facts.tables[0].fks == ("fk_orders_user",)
    assert facts.tables[0].columns == ("id", "uid")
    assert facts.indexes[0].name == "idx_orders_status"
    assert facts.indexes[0].columns == ("status",)


def test_collect_facts_calls_the_registered_logical_names():
    """按逻辑名调用，不硬编码脚本 ID —— ID 是环境相关数据。"""
    runner = _FakeRunner()
    objects.collect_facts(runner, "public")
    assert [c[0] for c in runner.calls] == ["sqlreview.tables", "sqlreview.indexes"]
    assert all(c[1] == {"schema": "public"} for c in runner.calls)


def test_boolean_columns_are_parsed_not_truth_tested():
    """协议把布尔渲染成 't'/'f'，而 bool("f") 是 True。

    直接 bool() 的话每张表都会被判成「有主键」、每个索引都成 UNIQUE ——
    不报错，只是结论反了。这条用例守的就是这个。
    """
    facts = objects.collect_facts(_FakeRunner(), "public")
    assert facts.indexes[0].is_unique is False
    assert facts.indexes[0].is_primary is False


def test_collect_facts_degrades_on_query_failure():
    facts = objects.collect_facts(_FakeRunner(fail=(objects.INDEXES_SCRIPT,)), "public")
    assert facts.tables  # table dimension still collected
    assert facts.indexes == ()
    assert any("permission denied" in n for n in facts.notes)


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------
def _finding(**kw):
    base = {
        "rule_id": "DML001",
        "rule_name": "禁止物理删除",
        "severity": model.Severity.ERROR,
        "message": "禁止 DELETE",
        "location": "stmt#1 line 1",
    }
    base.update(kw)
    return model.Finding(**base)


def test_report_clean_run():
    res = model.ReviewResult("file:a.sql", (), 3, 0, ())
    out = report.render_markdown(res)
    assert "未发现违规" in out


def test_report_sorts_by_severity_and_splits_advisory():
    res = model.ReviewResult(
        "file:a.sql",
        (
            _finding(rule_id="W1", severity=model.Severity.WARN, message="warn me"),
            _finding(rule_id="E1", severity=model.Severity.ERROR, message="error me"),
            _finding(rule_id="A1", severity=model.Severity.WARN, message="advise me",
                     advisory=True),
        ),
        1, 0, (),
    )
    out = report.render_markdown(res)
    assert "## Deterministic Findings" in out
    assert "## Advisory" in out
    # error sorts above warn within the deterministic block
    assert out.index("error me") < out.index("warn me")
    # advisory findings live in the advisory block, below the deterministic one
    assert out.index("warn me") < out.index("advise me")


def test_report_json_is_machine_readable():
    import json
    res = model.ReviewResult("file:a.sql", (_finding(),), 1, 0, ())
    payload = json.loads(report.render_json(res))
    assert payload["summary"]["error"] == 1
    assert payload["findings"][0]["rule_id"] == "DML001"
    assert payload["findings"][0]["severity"] == "error"


def test_two_sessions_without_a_handle_is_a_clean_error_not_a_traceback(tmp_path, monkeypatch, capsys):
    """场景矩阵抓到的:两个会话并存、不带句柄时 sqlreview --top 直接 Traceback——
    别的 skill 的 for_conn 都在 try 里,只有它在 try 外面直接问 session.current()。要的是一句 error 加候选清单。"""
    import importlib.util
    import os
    from common import session
    from common.config import Connection

    monkeypatch.setenv("GSDB_HOME", str(tmp_path))
    monkeypatch.delenv(session.ENV_HANDLE, raising=False)
    session.use(None)

    def conn(ip, db):
        return Connection(name="%s-%s" % (ip.replace(".", "-"), db), type="gaussdb", host="127.0.0.1",
                          port=8769, database=db, user="grmp", driver="grmp", data_ip=ip, app="api")
    session.save(conn("10.0.0.9", "core"))
    session.save(conn("10.0.0.20", "report"))

    spec = importlib.util.spec_from_file_location("sqlreview_two_sessions_under_test", _SCRIPTS / "sqlreview.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    try:
        rc = mod.main(["--top", "3"])
    finally:
        os.environ.pop(session.ENV_HANDLE, None)
        session.use(None)
    err = capsys.readouterr().err
    # 2026-09-11 起不列候选:只说「本对话未登录」,别人的库名、句柄一个字不透
    assert rc == 1 and "本对话未登录" in err and "Traceback" not in err
    assert "report" not in err and "core" not in err and "2 个" not in err

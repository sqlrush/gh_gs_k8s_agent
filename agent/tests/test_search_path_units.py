"""common.search_path —— EXPLAIN 前把 search_path 切到 SQL 原本的 schema。

现场 sqltune 400 的成因:业务 SQL 的表名不带 schema,靠应用账号的 search_path 解析;中间件执行账号的
search_path 是 "$user", public,解析不到 → relation does not exist → HTTP 400。修法是注册一条
「SET search_path TO "<schema>", public; EXPLAIN …」的两语句模板。中间件能不能跑两条语句不确定,
所以先用一条探测脚本验一次(每个 runner 只验一次),不行就退回单语句模板并说明——不让它变成新的 400。
schema 名同时进 SET 语句的引号里,形状不是合法标识符一律不用。
"""
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common import search_path as sp  # noqa: E402
from common.grmp.errors import QueryError  # noqa: E402


class FakeRunner:
    def __init__(self, probe_ok=True, probe_exc=None):
        self.probe_ok, self.probe_exc, self.calls = probe_ok, probe_exc, []

    def run(self, script, values=None):
        self.calls.append((script, dict(values or {})))
        if script == sp.PROBE_SCRIPT:
            if self.probe_exc:
                raise QueryError(self.probe_exc)
            return [{"ok": "1"}] if self.probe_ok else []
        return [{"QUERY PLAN": "Seq Scan on orders"}]


@pytest.fixture(autouse=True)
def _fresh_cache():
    sp.reset_probe_cache()
    yield
    sp.reset_probe_cache()


def test_valid_schema_is_a_plain_identifier():
    assert sp.valid_schema("app_trade") and sp.valid_schema("S1") and sp.valid_schema("a$b")
    for bad in ("", "app;drop", "a b", '"x"', "app.trade", "1abc", "x" * 70):
        assert not sp.valid_schema(bad), bad


def test_no_schema_runs_the_plain_template():
    r = FakeRunner()
    plan, applied, note = sp.explain_plan(r, "sqltune.plan_text", "select 1", "")
    assert plan == "Seq Scan on orders" and applied == "" and note == ""
    assert r.calls == [("sqltune.plan_text", {"sql": "select 1"})]


def test_schema_uses_the_schema_template_when_the_middleware_runs_two_statements():
    r = FakeRunner()
    plan, applied, note = sp.explain_plan(r, "sqltune.plan_text", "select 1", "app_trade")
    assert plan == "Seq Scan on orders" and applied == "app_trade" and note == ""
    assert r.calls[0][0] == sp.PROBE_SCRIPT
    assert r.calls[1] == ("sqltune.plan_text_schema", {"sql": "select 1", "schema": "app_trade"})


def test_probe_failure_falls_back_to_the_plain_template_with_a_note():
    """探测脚本没注册(白名单没灌)/ 中间件拒绝两条语句:退回单语句模板,把原因和**可照做的配置命令**写进 note——
    用户看到就知道该让 DBA 做什么,不用再来问。"""
    r = FakeRunner(probe_exc="脚本 explain.multi_stmt_probe 不存在")
    plan, applied, note = sp.explain_plan(r, "sqltune.plan_text", "select 1", "app_trade")
    assert plan == "Seq Scan on orders" and applied == ""
    assert r.calls[1] == ("sqltune.plan_text", {"sql": "select 1"})
    assert "search_path" in note and "app_trade" in note and "不存在" in note
    assert "白名单" in note and "explain.multi_stmt_probe" in note          # 原因一:探测脚本没灌进白名单
    assert "ALTER ROLE" in note and "SET search_path = app_trade, public" in note   # 可照做的 DBA 命令
    assert "app_trade.<表>" in note                                         # 备选:表名写全


def test_probe_returning_nothing_counts_as_unsupported():
    r = FakeRunner(probe_ok=False)
    _, applied, note = sp.explain_plan(r, "explain.plan_text", "select 1", "app_trade")
    assert applied == "" and note and r.calls[1][0] == "explain.plan_text"


def test_probe_runs_once_per_runner():
    r = FakeRunner()
    sp.explain_plan(r, "sqltune.plan_text", "select 1", "a1")
    sp.explain_plan(r, "sqltune.plan_json", "select 2", "a1")
    assert [c[0] for c in r.calls].count(sp.PROBE_SCRIPT) == 1
    assert r.calls[-1][0] == "sqltune.plan_json_schema"


def test_invalid_schema_never_reaches_a_template():
    r = FakeRunner()
    _, applied, note = sp.explain_plan(r, "x.plan_text", "select 1", "app;drop")
    assert applied == "" and "不是合法" in note
    assert all(c[0] != "x.plan_text_schema" for c in r.calls)


def test_set_search_path_on_a_raw_session_quotes_the_identifier():
    class Db:
        def __init__(self):
            self.executed = []

        def execute(self, sql, params=None):
            self.executed.append(sql)

    db = Db()
    assert sp.set_search_path(db, "app_trade") == "app_trade"
    assert db.executed == ['SET search_path TO "app_trade", public']
    assert sp.set_search_path(db, "") == "" and len(db.executed) == 1
    with pytest.raises(ValueError):
        sp.set_search_path(db, 'app"; drop schema x')


def test_executor_refusing_two_statements_gets_the_same_dba_command():
    """原因二:探测脚本在、但中间件不支持一条脚本跑两条语句——说明要写明这是执行器的限制,并给同一条 DBA 命令。"""
    r = FakeRunner(probe_ok=False)
    _, applied, note = sp.explain_plan(r, "explain.plan_text", "select 1", "gbatch")
    assert applied == "" and "两条语句" in note and "白名单" not in note
    assert "ALTER ROLE" in note and "SET search_path = gbatch, public" in note


def test_fallback_explain_failure_carries_the_note_in_the_error():
    """探测没通过、退回单语句模板后 EXPLAIN 又因为表不在 search_path 里失败:这时没有报告可以放说明,
    说明必须跟在报错后面——用户看到的那一段就得写清是白名单没灌还是执行器不支持,以及 DBA 该跑的命令。"""
    class _R(FakeRunner):
        def run(self, script, values=None):
            if script == "sqltune.plan_text":
                raise QueryError('执行脚本 sqltune.plan_text 失败：ERROR: relation "orders" does not exist')
            return super().run(script, values)

    r = _R(probe_exc="脚本 explain.multi_stmt_probe 不存在")
    with pytest.raises(QueryError) as ei:
        sp.explain_plan(r, "sqltune.plan_text", "select 1", "app_trade")
    msg = str(ei.value)
    assert 'relation "orders" does not exist' in msg and "补充" in msg
    assert "白名单" in msg and "ALTER ROLE" in msg and "SET search_path = app_trade, public" in msg


# ---------------------------------------------------------------- 切不成 search_path → 脚本自己补全表名(客户 09-09 早截图)

class _QualRunner(FakeRunner):
    """记下单语句模板收到的 SQL;可指定「原文失败、补全后成功」。"""

    def __init__(self, fail_unqualified=False, **kw):
        super().__init__(**kw)
        self.fail_unqualified = fail_unqualified

    def run(self, script, values=None):
        if script != sp.PROBE_SCRIPT and self.fail_unqualified and "gmag." not in (values or {}).get("sql", ""):
            self.calls.append((script, dict(values or {})))
            raise QueryError('执行脚本失败：ERROR: relation "batch_job_status" does not exist')
        return super().run(script, values)


def test_unsupported_two_statements_qualifies_table_names_instead():
    """执行器跑不了两条语句:不再把原 SQL 原样发过去等 400,先把不带 schema 的表名补全再走单语句模板。"""
    r = _QualRunner(probe_ok=False)
    plan, applied, note = sp.explain_plan(r, "sqltune.plan_text", "SELECT COUNT(1) FROM batch_job_status WHERE x = 1", "gmag")
    assert plan == "Seq Scan on orders"
    assert r.calls[1] == ("sqltune.plan_text", {"sql": "SELECT COUNT(1) FROM gmag.batch_job_status WHERE x = 1"})
    assert applied == ""                                   # search_path 确实没切,不假装
    assert "补全" in note and "gmag.batch_job_status" in note and "两条语句" in note
    assert "ALTER ROLE" in note and "SET search_path = gmag, public" in note


def test_missing_probe_script_also_qualifies_and_says_which_cause():
    r = _QualRunner(probe_exc="脚本 explain.multi_stmt_probe 不存在")
    _, applied, note = sp.explain_plan(r, "explain.plan_text", "select * from t1 join t2 on t1.id = t2.id", "app")
    assert applied == "" and r.calls[1][1]["sql"] == "select * from app.t1 join app.t2 on t1.id = t2.id"
    assert "补全" in note and "白名单" in note and "explain.multi_stmt_probe" in note


def test_nothing_to_qualify_keeps_the_old_fallback():
    """SQL 里的表名本来就带 schema(或没有表):没什么可补,行为与之前一样——原文单语句模板 + 原因说明。"""
    r = _QualRunner(probe_ok=False)
    _, applied, note = sp.explain_plan(r, "explain.plan_text", "select * from other.t", "gmag")
    assert applied == "" and r.calls[1][1]["sql"] == "select * from other.t"
    assert "补全" not in note and "ALTER ROLE" in note


def test_qualified_explain_failure_still_carries_the_note():
    class _R(_QualRunner):
        def run(self, script, values=None):
            if script != sp.PROBE_SCRIPT:
                raise QueryError('ERROR: relation "gmag.batch_job_status" does not exist')
            return super().run(script, values)

    with pytest.raises(QueryError) as ei:
        sp.explain_plan(_R(probe_ok=False), "explain.plan_text", "select 1 from batch_job_status", "gmag")
    msg = str(ei.value)
    assert "补充" in msg and "补全" in msg and "ALTER ROLE" in msg


def test_two_statement_path_still_preferred_when_it_works():
    r = _QualRunner(probe_ok=True)
    _, applied, note = sp.explain_plan(r, "explain.plan_text", "select * from t", "gmag")
    assert applied == "gmag" and note == ""
    assert r.calls[1] == ("explain.plan_text_schema", {"sql": "select * from t", "schema": "gmag"})


# ---------------------------------------------------------------- schema_name 是搜索路径串(2026-10-21 现场:`fl, public`)

class _Db:
    def __init__(self):
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append(sql)


def _render_registry_template(name, values):
    """按真实注册模板 + 共用渲染器渲染出中间件最终执行的文本:验证拼出来的就是想要的 SET 语句。"""
    from common.grmp import placeholder
    from common.grmp.script import load_script
    from common.grmp.settings import Settings
    domain, _, base = name.partition(".")
    rec = load_script(_ROOT / "scripts" / "registry" / domain / (base + ".yaml"))
    return placeholder.render(rec.script_content, rec.params, values, Settings())


def test_search_path_string_is_split_and_every_segment_checked():
    assert sp.parse_search_path("fl, public") == (["fl", "public"], None)
    assert sp.parse_search_path(" fl ,public ") == (["fl", "public"], None)
    assert sp.parse_search_path("app_trade") == (["app_trade"], None)
    assert sp.valid_search_path("fl, public") and sp.valid_search_path("a$b,S1")
    # 任何一段不合法整串判死,并点出是哪一段;拿不到「部分合法」的段
    assert sp.parse_search_path('"$user", public') == ([], '"$user"')
    assert sp.parse_search_path("fl, x;drop") == ([], "x;drop")
    assert sp.parse_search_path("fl,,public") == ([], "")
    assert sp.parse_search_path("fl, ") == ([], "")
    for bad in ("", "fl public", 'fl", "public', "fl; drop schema x, public", "fl, app.trade", "fl, 1abc"):
        assert not sp.valid_search_path(bad), bad


def test_primary_and_dba_paths():
    assert sp.primary_schema("fl, public") == "fl" and sp.primary_schema("app") == "app"
    assert sp.dba_search_path("app") == "app, public"
    assert sp.dba_search_path("fl, public") == "fl, public"           # 不叠第二个 public
    assert sp.dba_search_path("a, b") == "a, b, public"


def test_search_path_string_goes_through_the_schema_template():
    """`fl, public` 不再被当成一个 schema 名拒掉:整串切过去,末尾的 public 由模板自己补。"""
    r = FakeRunner()
    plan, applied, note = sp.explain_plan(r, "sqltune.plan_text", "select 1", "fl, public")
    assert plan == "Seq Scan on orders" and applied == "fl, public" and note == ""
    assert r.calls[1] == ("sqltune.plan_text_schema", {"sql": "select 1", "schema": "fl"})


def test_rendered_template_sets_every_segment_quoted():
    """真实模板渲染后的文本:`fl, public` 与原先单 schema 的那条逐字相同;多段各自加引号、常量连接。"""
    sql = "select * from orders"
    out = _render_registry_template("sqltune.plan_text_schema", {"sql": sql, "schema": sp.template_value(["fl", "public"])})
    assert out.startswith('SET search_path TO "fl", public; EXPLAIN')
    out = _render_registry_template("sqltune.plan_text_schema", {"sql": sql, "schema": sp.template_value(["a", "b"])})
    assert out.startswith('SET search_path TO "a", "b", public; EXPLAIN')
    out = _render_registry_template("sqltune.plan_text_schema", {"sql": sql, "schema": sp.template_value(["public"])})
    assert out.startswith('SET search_path TO "public", public; EXPLAIN')
    assert out.count(";") == 1                                       # 只有模板自己那一个分号


def test_template_value_refuses_unchecked_segments():
    """防御:绕过 parse_search_path 直接调用也不放行——模板是文本替换,引号 / 分号进去就是注入。"""
    for bad in (['fl", public; drop schema x; --'], ["fl", "x;drop"], ["fl", ""]):
        with pytest.raises(ValueError):
            sp.template_value(bad)


def test_one_bad_segment_switches_nothing_and_names_it():
    r = FakeRunner()
    _, applied, note = sp.explain_plan(r, "sqltune.plan_text", "select 1", 'fl, public"; drop schema fl; --')
    assert applied == "" and "不是合法" in note and "整串不切" in note and "未切换 search_path" in note
    assert all(not c[0].endswith(sp.SCHEMA_SUFFIX) for c in r.calls)
    assert r.calls == [("sqltune.plan_text", {"sql": "select 1"})]      # 连探测都不做,原 SQL 走单语句模板


def test_set_search_path_on_a_raw_session_takes_a_path():
    db = _Db()
    assert sp.set_search_path(db, "fl, public") == "fl, public"
    assert sp.set_search_path(db, "a,b") == "a, b"
    assert db.executed == ['SET search_path TO "fl", public', 'SET search_path TO "a", "b", public']
    with pytest.raises(ValueError) as ei:
        sp.set_search_path(db, '"$user", public')
    assert "$user" in str(ei.value) and "拒绝" in str(ei.value) and len(db.executed) == 2


def test_unsupported_two_statements_qualifies_with_the_first_segment():
    """执行器跑不了两条语句时按路径第一段补全表名;说明如实写「表在第一段下才等价」,DBA 命令不叠 public。"""
    r = FakeRunner(probe_ok=False)
    _, applied, note = sp.explain_plan(r, "sqltune.plan_text", "select * from orders", "fl, public")
    assert applied == "" and r.calls[1] == ("sqltune.plan_text", {"sql": "select * from fl.orders"})
    assert "fl.orders" in note and "第一个 schema fl" in note and "fl, public" in note
    assert "SET search_path = fl, public;" in note and "public, public" not in note


def test_fallback_note_for_a_path_names_the_first_segment_for_qualified_names():
    note = sp.fallback_note("fl, public", "探测脚本没有返回 1")
    assert "SET search_path = fl, public;" in note and "fl.<表>" in note and "public, public" not in note

"""common.search_path —— 业务 SQL 的表名不带 schema 时怎么取执行计划。

现场 sqltune 400 的成因:业务 SQL 的表名不带 schema,靠应用账号的 search_path 解析;中间件执行账号的
search_path 是 "$user", public,解析不到 → relation does not exist → HTTP 400。
原先注册「SET search_path …; EXPLAIN …」两语句模板、先探测中间件能不能跑两条语句;行方中间件拦截一条脚本里的
两段 SQL(2026-11-12),v1.4.13 起一律把不带 schema 的表名补全后走单语句模板,不再探测、不再交付两语句脚本。
schema 名进 SQL 前照旧要过合法标识符检查。
"""
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common import search_path as sp  # noqa: E402
from common.grmp.errors import QueryError  # noqa: E402


class FakeRunner:
    def __init__(self):
        self.calls = []

    def run(self, script, values=None):
        self.calls.append((script, dict(values or {})))
        return [{"QUERY PLAN": "Seq Scan on orders"}]


def _no_two_statement_scripts(calls):
    """不再有探测、不再有 *_schema 两语句模板。"""
    return all("multi_stmt_probe" not in c[0] and not c[0].endswith("_schema") for c in calls)


def test_valid_schema_is_a_plain_identifier():
    assert sp.valid_schema("app_trade") and sp.valid_schema("S1") and sp.valid_schema("a$b")
    for bad in ("", "app;drop", "a b", '"x"', "app.trade", "1abc", "x" * 70):
        assert not sp.valid_schema(bad), bad


def test_no_schema_runs_the_plain_template():
    r = FakeRunner()
    plan, applied, note = sp.explain_plan(r, "sqltune.plan_text", "select 1", "")
    assert plan == "Seq Scan on orders" and applied == "" and note == ""
    assert r.calls == [("sqltune.plan_text", {"sql": "select 1"})]


def test_schema_qualifies_tables_and_sends_one_statement():
    """有 schema:把不带 schema 的表名补全,走单语句模板;不探测、不发两段 SQL(行方中间件会拦截)。"""
    r = FakeRunner()
    plan, applied, note = sp.explain_plan(r, "sqltune.plan_text", "SELECT COUNT(1) FROM batch_job_status WHERE x = 1", "gmag")
    assert plan == "Seq Scan on orders" and applied == ""
    assert r.calls == [("sqltune.plan_text", {"sql": "SELECT COUNT(1) FROM gmag.batch_job_status WHERE x = 1"})]
    assert "补全" in note and "gmag.batch_job_status" in note and "等价" in note
    assert "ALTER ROLE" not in note and "DBA" not in note, "生产环境权限与配置固定(11-12 行方):不把改执行账号当办法"


def test_joined_tables_are_all_qualified():
    r = FakeRunner()
    _, applied, note = sp.explain_plan(r, "explain.plan_text", "select * from t1 join t2 on t1.id = t2.id", "app")
    assert applied == "" and r.calls == [("explain.plan_text", {"sql": "select * from app.t1 join app.t2 on t1.id = t2.id"})]
    assert _no_two_statement_scripts(r.calls)


def test_invalid_schema_never_reaches_the_sql():
    r = FakeRunner()
    _, applied, note = sp.explain_plan(r, "x.plan_text", "select 1", "app;drop")
    assert applied == "" and "不是合法" in note
    assert r.calls == [("x.plan_text", {"sql": "select 1"})]


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


def test_nothing_to_qualify_runs_the_original_sql_with_a_note():
    """SQL 里的表名本来就带 schema(或没有表):没什么可补,原 SQL 走单语句模板,说明里只给「表名写全」这一条能调的路。"""
    r = FakeRunner()
    _, applied, note = sp.explain_plan(r, "explain.plan_text", "select * from other.t", "gmag")
    assert applied == "" and r.calls == [("explain.plan_text", {"sql": "select * from other.t"})]
    assert "补全" in note and "gmag.<表>" in note and "不影响其余部分" in note and "ALTER ROLE" not in note


class _FailUnqualified(FakeRunner):
    """原文失败、补全后成功 / 补全后失败、原文成功 —— 两种都要照样出计划。"""

    def __init__(self, fail_marker):
        super().__init__()
        self.fail_marker = fail_marker

    def run(self, script, values=None):
        self.calls.append((script, dict(values or {})))
        if self.fail_marker((values or {}).get("sql", "")):
            raise QueryError('执行脚本失败：ERROR: relation "batch_job_status" does not exist')
        return [{"QUERY PLAN": "Seq Scan on orders"}]


def test_qualified_sql_failing_falls_back_to_the_original_sql():
    """补全规则漏了某种语法时(2026-11-08 行方报告 Bug 1 那类),原 SQL 往往本来就能跑:用原 SQL 再试一次。"""
    r = _FailUnqualified(lambda sql: "gmag." in sql)
    plan, applied, note = sp.explain_plan(r, "explain.plan_text", "select 1 from batch_job_status", "gmag")
    assert plan == "Seq Scan on orders" and [c[1]["sql"] for c in r.calls] == [
        "select 1 from gmag.batch_job_status", "select 1 from batch_job_status"]
    assert "已改用原 SQL 取计划" in note


def test_both_failing_carries_the_note_in_the_error():
    """补全后与原文都失败:没有报告可以放说明,说明跟在报错后面——能调的只有 SQL,写清这一条的计划取不到、不影响其余。"""
    r = _FailUnqualified(lambda sql: True)
    with pytest.raises(QueryError) as ei:
        sp.explain_plan(r, "explain.plan_text", "select 1 from batch_job_status", "gmag")
    msg = str(ei.value)
    assert 'relation "batch_job_status" does not exist' in msg and "补充" in msg and "补全" in msg
    assert "ALTER ROLE" not in msg and _no_two_statement_scripts(r.calls)


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


def test_search_path_string_qualifies_with_the_first_segment():
    """`fl, public` 不再被当成一个 schema 名拒掉:按第一段补全,说明如实写「表在第一段下才等价」。"""
    r = FakeRunner()
    plan, applied, note = sp.explain_plan(r, "sqltune.plan_text", "select * from orders", "fl, public")
    assert plan == "Seq Scan on orders" and applied == ""
    assert r.calls == [("sqltune.plan_text", {"sql": "select * from fl.orders"})]
    assert "fl.orders" in note and "第一个 schema fl" in note and "ALTER ROLE" not in note


def test_no_registered_script_has_two_statements():
    """守卫(2026-11-12 行方):中间件拦截一条脚本里的两段 SQL —— 白名单里每条脚本都只能是一条语句。"""
    from common.grmp.registry import Registry
    from common.grmp.statement import split_statements
    reg = Registry()
    bad = [n for n in sorted(reg.names()) if len(split_statements(reg.find(n).script_content)) != 1]
    assert not bad, "这些脚本含多段 SQL,行方中间件会拦截:%s" % bad


def test_template_value_refuses_unchecked_segments():
    """防御:绕过 parse_search_path 直接调用也不放行——模板是文本替换,引号 / 分号进去就是注入。"""
    for bad in (['fl", public; drop schema x; --'], ["fl", "x;drop"], ["fl", ""]):
        with pytest.raises(ValueError):
            sp.template_value(bad)


def test_one_bad_segment_switches_nothing_and_names_it():
    r = FakeRunner()
    _, applied, note = sp.explain_plan(r, "sqltune.plan_text", "select 1", 'fl, public"; drop schema fl; --')
    assert applied == "" and "不是合法" in note and "整串不切" in note and "未切换 search_path" in note
    assert r.calls == [("sqltune.plan_text", {"sql": "select 1"})]      # 原 SQL 走单语句模板


def test_set_search_path_on_a_raw_session_takes_a_path():
    db = _Db()
    assert sp.set_search_path(db, "fl, public") == "fl, public"
    assert sp.set_search_path(db, "a,b") == "a, b"
    assert db.executed == ['SET search_path TO "fl", public', 'SET search_path TO "a", "b", public']
    with pytest.raises(ValueError) as ei:
        sp.set_search_path(db, '"$user", public')
    assert "$user" in str(ei.value) and "拒绝" in str(ei.value) and len(db.executed) == 2


def test_fallback_note_for_a_path_names_the_first_segment_for_qualified_names():
    note = sp.fallback_note("fl, public")
    assert "fl.<表>" in note and "不影响其余部分" in note and "ALTER ROLE" not in note


def test_business_path_drops_the_default_user_segment():
    """statement_history 记的默认路径 `"$user",public`:原先整串判不合法、一段都不切,业务表找不到(2026-11-06)。
    现在 `"$user"` 去掉、合法名外的引号去掉;只剩 public 时返回空串,由调用方按表名在目录里找 schema。"""
    assert sp.business_path('"$user",public') == ""
    assert sp.business_path('"$user", public') == ""
    assert sp.business_path('"$user", gmag, public') == "gmag, public"
    assert sp.business_path('"Gmag", public') == "Gmag, public"
    assert sp.business_path("fl, public") == "fl, public"
    assert sp.business_path("") == "" and sp.business_path(None) == ""
    # 注入防线不变:不合法的段原样留着,照旧由 parse_search_path 判掉
    assert not sp.valid_search_path(sp.business_path('fl, x;drop'))

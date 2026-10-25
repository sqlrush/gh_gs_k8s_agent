"""sqltune 占位符:算术表达式里的推断 + 推断错了的降级(纯函数 + 假 runner,不连库)。

对应 2026-10-24 faultlab 在 og5 上的失败:按 sql_id 调 S3(3509710879),归一化文本
`... NOT IN (SELECT c.code FROM fl.customers c WHERE c.grade + ? = r.zone)` 里的 `?`
被猜成 'test' —— EXPLAIN 报 invalid input syntax for integer,整个调优 rc=1。
两个毛病叠在一起:
  ① `"in (" in 左上下文` 是子串判断,80 字窗口里只要出现过 `NOT IN (` 就当成 IN 列表,
     列又不像整数列,于是填 'test';
  ② comparison_column 碰到 `+` 就放弃,catalog 里 grade 明明是 integer 也用不上。
推断不出时还要能降级:数据库按报错拒了一个合成值,换一个按报错里的目标类型能通过的值重取计划,
不能让整条调优失败。
"""
import pathlib
import sys
from types import SimpleNamespace

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "skills" / "gaussdb-sqltune" / "scripts"))
sys.path.insert(0, str(_ROOT))

import coltypes  # noqa: E402
import placeholder  # noqa: E402

# og5 dbe_perf.statement 里 S3 的归一化文本(原样)
S3_TEXT = (
    "SELECT count(*) AS kept, sum(o.amount) AS amt\n"
    "  FROM (SELECT * FROM fl.orders WHERE id <= ?) o\n"
    "  JOIN fl.regions   r ON r.id = o.region_id\n"
    "  JOIN fl.channels ch ON ch.id = o.channel_id\n"
    "  JOIN fl.promos   pr ON pr.id = o.promo_id\n"
    " WHERE o.cust_code NOT IN (SELECT c.code FROM fl.customers c WHERE c.grade + ? = r.zone)")


class FakeRunner:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def run(self, script, params=None):
        self.calls.append((script, params))
        return self.rows


# ---------------------------------------------------------------- 纯启发式

def test_arithmetic_placeholder_inside_not_in_subquery_is_not_a_string():
    """没有 catalog 信息时也不能填 'test':`+ ?` 只能是数(或日期加天数),'test' 必炸。"""
    r = placeholder.substitute(S3_TEXT, [])
    assert r.substitutions[1].value == "1"
    assert "'test'" not in r.sql


def test_in_list_rule_only_applies_when_the_placeholder_is_inside_the_list():
    """`NOT IN (SELECT …` 早就过去了,这个占位符不在 IN 列表里。"""
    ctx_far = "SELECT 1 FROM o WHERE o.code NOT IN (SELECT c.code FROM t c WHERE c.n = abs("
    assert placeholder.substitute(ctx_far + "?))", []).substitutions[0].value == "1"
    # 真在 IN 列表里的老行为不变:第一项、后续项都按列名猜
    r = placeholder.substitute("SELECT * FROM t WHERE name IN (?, ?) AND user_id IN (?)", [])
    assert [s.value for s in r.substitutions] == ["'test'", "'test'", "1"]


@pytest.mark.parametrize("ctx,col", [("c.grade +", "grade"), ("c.grade+", "grade"),
                                     ("o.amount *", "amount"), ("(o.id %", "id"),
                                     ("x.n -", "n"), ("qty /", "qty")])
def test_comparison_column_sees_through_an_arithmetic_operator(ctx, col):
    assert placeholder.comparison_column("SELECT 1 FROM t WHERE " + ctx) == col
    assert placeholder.is_arithmetic_context("SELECT 1 FROM t WHERE " + ctx)


def test_arithmetic_on_an_expression_has_no_column_but_is_still_numeric():
    ctx = "JOIN w ON w.id = (c.id % ?) +"
    assert placeholder.comparison_column(ctx) is None
    assert placeholder.is_arithmetic_context(ctx)


def test_concatenation_and_comparison_are_not_arithmetic():
    assert not placeholder.is_arithmetic_context("WHERE c.code LIKE l3.k ||")
    assert not placeholder.is_arithmetic_context("WHERE c.grade =")
    assert not placeholder.is_arithmetic_context("WHERE c.grade >=")


# ---------------------------------------------------------------- catalog 推断

def test_catalog_type_reaches_the_arithmetic_placeholder():
    runner = FakeRunner([
        {"schema_name": "fl", "table_name": "orders", "attname": "id", "type_name": "integer"},
        {"schema_name": "fl", "table_name": "customers", "attname": "grade", "type_name": "integer"},
    ])
    types = coltypes.infer_types(runner, S3_TEXT, schema="fl")
    assert types == ["integer", "integer"]
    _, params = runner.calls[0]
    assert "'grade'" in params["columns"]
    r = placeholder.substitute(S3_TEXT, [], types=types)
    assert [s.value for s in r.substitutions] == ["1", "1"]
    assert [s.source for s in r.substitutions] == ["type", "type"]


@pytest.mark.parametrize("col_type,expect", [
    ("date", "integer"),                          # date + 1 = 明天;'2024-01-01' 塞进去是 date + date,不存在
    ("timestamp without time zone", "interval"),  # timestamp + interval
    ("numeric", "numeric"),
    ("character varying", None),                  # 字符串列上的算术不是我们能猜的,交回启发式
])
def test_arithmetic_operand_type_follows_the_other_side(col_type, expect):
    runner = FakeRunner([{"attname": "d", "type_name": col_type}])
    assert coltypes.infer_types(runner, "SELECT * FROM t WHERE t.d + ? > now()") == [expect]


def test_date_arithmetic_gets_a_day_count_not_a_date_literal():
    r = placeholder.substitute("SELECT * FROM t WHERE t.d + ? > now()", [], types=["integer"])
    assert r.substitutions[0].value == "1"
    r = placeholder.substitute("SELECT * FROM t WHERE t.ts + ? > now()", [], types=["interval"])
    assert r.substitutions[0].value == "'1 day'"


# ---------------------------------------------------------------- 降级:按报错换值

def test_invalid_input_for_a_synthetic_value_yields_a_fallback_of_the_target_type():
    r = placeholder.substitute("SELECT * FROM t WHERE a_id = ? AND grp = ?", [])
    assert r.substitutions[1].value == "'test'"
    msg = 'ERROR: invalid input syntax for integer: "test" (SQLSTATE 22P02)'
    fb = coltypes.fallback_for_error(msg, r.substitutions)
    assert fb == {1: "'1'"}


@pytest.mark.parametrize("msg,expect", [
    ('invalid input syntax for type numeric: "test"', "'1'"),
    ('invalid input syntax for type bigint: "test"', "'1'"),
    ('invalid input syntax for type date: "test"', "'2024-01-01'"),
    ('invalid input syntax for type timestamp: "test"', "'2024-01-01 00:00:00'"),
    ('invalid input syntax for type boolean: "test"', "'true'"),
])
def test_fallback_is_a_quoted_literal_the_target_type_accepts(msg, expect):
    """原值是个带引号的字面量、数据库按目标类型解析失败 —— 那个位置接受「未定类型的字面量」,
    换成目标类型解析得了的带引号字面量,这一处就一定能过;'1' 放进字符串列也照样合法。"""
    r = placeholder.substitute("SELECT * FROM t WHERE grp = ?", [])
    assert coltypes.fallback_for_error(msg, r.substitutions) == {0: expect}


def test_operator_mismatch_on_a_guessed_number_falls_back_to_a_quoted_number():
    """`acct_no = ?` 按列名猜成整数,而列其实是 varchar:operator does not exist。
    带引号的 '1' 对整数列、字符串列都合法。"""
    r = placeholder.substitute("SELECT * FROM t WHERE acct_no = ? LIMIT ?", [])
    assert [s.value for s in r.substitutions] == ["1", "100"]
    msg = "ERROR: operator does not exist: character varying = integer"
    assert coltypes.fallback_for_error(msg, r.substitutions) == {0: "'1'"}


def test_no_fallback_for_bind_values_or_unrelated_errors():
    r = placeholder.substitute("SELECT * FROM t WHERE grp = ?", ["abc"], types=["integer"])
    assert coltypes.fallback_for_error('invalid input syntax for integer: "abc"', r.substitutions) is None
    r = placeholder.substitute("SELECT * FROM t WHERE grp = ?", [])
    assert coltypes.fallback_for_error('relation "t" does not exist', r.substitutions) is None


def test_with_values_rebuilds_sql_and_marks_the_fallback():
    r = placeholder.substitute("SELECT * FROM t WHERE a_id = ? AND grp = ?", [])
    r2 = placeholder.with_values(r, "SELECT * FROM t WHERE a_id = ? AND grp = ?", {1: "'1'"},
                                 note="#2 'test' 被拒")
    assert r2.sql == "SELECT * FROM t WHERE a_id = 1 AND grp = '1'"
    assert [s.source for s in r2.substitutions] == ["rule", "fallback"]
    assert r2.notes == ("#2 'test' 被拒",)
    assert r.sql.endswith("grp = 'test'"), "原结果不可变"


# ---------------------------------------------------------------- _tune 重试

def _fake_tune_env(monkeypatch, fail_times, message):
    import sqltune
    seen = []

    def collect(runner, db, sql, analyze, schema=""):
        seen.append(sql)
        if len(seen) <= fail_times:
            raise sqltune.access.QueryError(message)
        return SimpleNamespace(findings=[], tables=[], indexes=[], columns=[], plan="", sql=sql)

    monkeypatch.setattr(sqltune, "collect", collect)
    monkeypatch.setattr(sqltune.coltypes, "infer_types", lambda *a, **k: [])
    monkeypatch.setattr(sqltune, "_guard_sql", lambda *a, **k: None)
    monkeypatch.setattr(sqltune, "_derivation_report", lambda *a, **k: "")
    monkeypatch.setattr(sqltune.planrules, "root_causes", lambda *a, **k: [])
    return sqltune, seen


def test_tune_retries_with_the_fallback_instead_of_failing(monkeypatch):
    sqltune, seen = _fake_tune_env(monkeypatch, 1, 'ERROR: invalid input syntax for integer: "test"')
    tr = sqltune._tune(object(), None, original_sql="SELECT * FROM t WHERE grp = ?", binds=[],
                       do_analyze=False)
    assert seen == ["SELECT * FROM t WHERE grp = 'test'", "SELECT * FROM t WHERE grp = '1'"]
    assert tr.substitution.substitutions[0].source == "fallback"
    assert "invalid input syntax" in tr.substitution.notes[0]


def test_tune_still_fails_loudly_when_the_fallback_is_rejected_too(monkeypatch):
    sqltune, seen = _fake_tune_env(monkeypatch, 99, 'ERROR: invalid input syntax for integer: "test"')
    with pytest.raises(sqltune.access.QueryError) as ei:
        sqltune._tune(object(), None, original_sql="SELECT * FROM t WHERE grp = ?", binds=[],
                      do_analyze=False)
    assert len(seen) == 2, "只重试一轮:换过的值再被拒就如实报错,不无限试"
    assert "#1" in str(ei.value)


def test_tune_does_not_second_guess_a_bind_value(monkeypatch):
    sqltune, seen = _fake_tune_env(monkeypatch, 99, 'ERROR: invalid input syntax for integer: "abc"')
    with pytest.raises(sqltune.access.QueryError) as ei:
        sqltune._tune(object(), None, original_sql="SELECT * FROM t WHERE grp = ?", binds=["abc"],
                      do_analyze=False)
    assert len(seen) == 1 and "错位" in str(ei.value)


def test_report_says_which_synthetic_value_was_replaced():
    import sqltune
    sub = placeholder.with_values(placeholder.substitute("SELECT * FROM t WHERE grp = ?", []),
                                  "SELECT * FROM t WHERE grp = ?", {0: "'1'"},
                                  note="占位符 #1 的合成值 'test' 被数据库拒绝")
    out = sqltune._substitution_section(sub)
    assert "## Placeholder Substitution (synthetic values)" in out
    assert "fallback" in out and "被数据库拒绝" in out

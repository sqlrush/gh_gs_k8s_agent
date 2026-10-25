import sys, pathlib
from decimal import Decimal
_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402
from common.backends.base import DBError  # noqa: E402
from common.backends import gsql_protocol as gp  # noqa: E402

def test_string_param_uses_quoted_var():
    sql, vars_ = gp.rewrite_params("WHERE n = %s", ["public"])
    assert sql == "WHERE n = :'p0'"
    assert vars_ == {"p0": "public"}

def test_numeric_param_uses_raw_var():
    sql, vars_ = gp.rewrite_params("LIMIT %s", [100])
    assert sql == "LIMIT :p0"
    assert vars_ == {"p0": "100"}

def test_decimal_param_preserved_as_text():
    sql, vars_ = gp.rewrite_params("x > %s", [Decimal("1.5")])
    assert sql == "x > :p0"
    assert vars_ == {"p0": "1.5"}

def test_bool_and_none_inlined():
    sql, vars_ = gp.rewrite_params("a=%s AND b=%s", [True, None])
    assert sql == "a=TRUE AND b=NULL"
    assert vars_ == {}

def test_mixed_string_and_numeric():
    sql, vars_ = gp.rewrite_params(
        "p=%s AND (%s='' OR n=%s) LIMIT %s", ["proc", "", "public", 1]
    )
    assert sql == "p=:'p0' AND (:'p1'='' OR n=:'p2') LIMIT :p3"
    assert vars_ == {"p0": "proc", "p1": "", "p2": "public", "p3": "1"}

def test_percent_literal_escaped_when_params_present():
    """有参数时 %% 才是转义 —— 与 psycopg2 的 format 参数风格一致。"""
    sql, vars_ = gp.rewrite_params("x LIKE 'a%%b' AND n = %s", ["v"])
    assert sql == "x LIKE 'a%b' AND n = :'p0'"
    assert vars_ == {"p0": "v"}


@pytest.mark.parametrize("sql", [
    "x LIKE 'a%%b'",
    "x LIKE '%s%'",          # 用户想找含 s 的名字，不是占位符
    "x LIKE '%status%'",
    "SELECT a % 7 FROM t",
])
def test_no_params_means_sql_is_untouched(sql):
    """**没参数就没有占位符要填，此时一个字符都不该动。**

    原先无条件扫一遍。实测（og5）三种走样，直连驱动与中间件都没有：

        LIKE 'x%%y'  → 静默改写成 'x%y'，计划是改写后那条的，无人告知
        LIKE 'x%sy'  → error: more %s placeholders than params

    而 DirectRunner.run() 调的正是 db.query(sql)，一个参数都不传 ——
    对它来说这趟扫描纯属白做工，却把 explain/proctune/sqltune 里用户给的
    LIKE 模式弄坏了。
    """
    out, vars_ = gp.rewrite_params(sql, [])
    assert out == sql
    assert vars_ == {}

def test_count_mismatch_raises():
    with pytest.raises(DBError):
        gp.rewrite_params("a=%s AND b=%s", ["only-one"])

def test_unsupported_type_raises():
    with pytest.raises(DBError):
        gp.rewrite_params("x=%s", [object()])

def test_is_wrappable_true_for_select():
    assert gp.is_wrappable_select("SELECT 1")
    assert gp.is_wrappable_select("  select * from t")
    assert gp.is_wrappable_select("WITH x AS (SELECT 1) SELECT * FROM x")

def test_is_wrappable_strips_leading_comment():
    assert gp.is_wrappable_select("-- c\nSELECT 1")
    assert gp.is_wrappable_select("/* c */ SELECT 1")

def test_is_wrappable_false_for_non_select():
    assert not gp.is_wrappable_select("SHOW enable_wdr_snapshot")
    assert not gp.is_wrappable_select("EXPLAIN ANALYZE SELECT 1")
    assert not gp.is_wrappable_select("SET statement_timeout = 1000")

def test_wrap_select_json_strips_trailing_semicolon():
    assert (
        gp.wrap_select_json("SELECT a FROM t;")
        == "SELECT json_agg(row_to_json(_t)) FROM (SELECT a FROM t) _t"
    )

def test_parse_json_recovers_types_and_null():
    out = '[{"a": 1, "b": "x", "c": null}]\n'
    cols, rows = gp.parse_json_result(out)
    assert cols == ["a", "b", "c"]
    assert rows == [(1, "x", None)]

def test_parse_json_float_is_decimal():
    cols, rows = gp.parse_json_result('[{"v": 1.5}]')
    assert rows[0][0] == Decimal("1.5")
    assert isinstance(rows[0][0], Decimal)

def test_parse_json_empty_set():
    assert gp.parse_json_result("\n") == ([], [])
    assert gp.parse_json_result("") == ([], [])

def test_parse_text_lines():
    cols, rows = gp.parse_text_result("on\n")
    assert cols == []
    assert rows == [("on",)]
    _, rows2 = gp.parse_text_result("line1\nline2\n")
    assert rows2 == [("line1",), ("line2",)]

def test_parse_text_empty():
    assert gp.parse_text_result("") == ([], [])

def test_parse_text_no_trailing_newline():
    assert gp.parse_text_result("on") == ([], [("on",)])

def test_parse_error_with_sqlstate():
    err = "gsql: ERROR:  42P01: relation \"foo\" does not exist\n"
    assert gp.parse_gsql_error(err) == 'ERROR: relation "foo" does not exist (SQLSTATE 42P01)'

def test_parse_error_without_sqlstate():
    assert gp.parse_gsql_error("gsql: ERROR:  boom\n") == "ERROR: boom"

def test_parse_error_fallback():
    assert gp.parse_gsql_error("could not connect to server") == "could not connect to server"

def test_parse_error_lowercase_token_not_sqlstate():
    assert gp.parse_gsql_error("gsql: ERROR:  error: boom") == "ERROR: error: boom"

# openGauss 的 VERBOSITY=verbose 把消息挪到下一行(GAUSS-NNNNN 前缀),ERROR: 那行
# 是空的。只读首行会得到光秃秃的 "ERROR: " —— 现场 agent 看不到原因,无从自纠。
_OG_VERBOSE = (
    "ERROR:  \n"
    "GAUSS-08964: column \"nosuchcol\" does not exist\n"
    "SQLSTATE: 42703\n"
    "LINE 1: SELECT nosuchcol FROM pg_class\n"
    "               ^\n"
    "CONTEXT:  referenced column: nosuchcol\n"
)

def test_parse_error_opengauss_verbose_next_line():
    assert gp.parse_gsql_error(_OG_VERBOSE) == (
        'ERROR: column "nosuchcol" does not exist (SQLSTATE 42703)')

def test_parse_error_opengauss_verbose_without_sqlstate():
    err = "ERROR:  \nGAUSS-01234: something broke\n"
    assert gp.parse_gsql_error(err) == "ERROR: something broke"

def test_parse_error_empty_error_line_falls_back_to_raw():
    # 既没有内联消息也没有 GAUSS- 行时,宁可回退原文,也不要返回空的 "ERROR: "。
    err = "gsql: ERROR:  \nDETAIL:  只有细节没有主消息\n"
    out = gp.parse_gsql_error(err)
    assert out.strip() != "ERROR:"
    assert "只有细节没有主消息" in out


# --- openGauss 的 JSON 数字表示（实测钉住，别当成 bug 去"修"）------------

def test_sub_one_numbers_arrive_as_quoted_strings():
    """**这是实测事实，不是待修的缺陷。**

    openGauss 把绝对值小于 1 的数写成无前导零的 .5,那不是合法 JSON,
    于是它加引号。numeric / float4 / float8 一视同仁:

        SELECT 0.5::numeric n, 0.5::float8 f, '.5'::text txt
        → {"n":".5","f":".5","txt":".5"}

    钉在这里是为了防止后人写一个"长得像数字就转回去"的正则:
    txt 那一列证明了**转回去必然会把文本列悄悄变成数字**,
    那是拿一个静默错误换另一个。
    """
    cols, rows = gp.parse_json_result('[{"n":".5","f":".5","txt":".5"}]\n')
    assert cols == ["n", "f", "txt"]
    assert rows == [(".5", ".5", ".5")], "三者在 JSON 里本就无法区分"


def test_numbers_at_or_above_one_stay_numeric():
    cols, rows = gp.parse_json_result('[{"a":1.5,"b":123.456,"c":7,"d":1e-07}]\n')
    assert rows[0][0] == Decimal("1.5")
    assert rows[0][2] == 7
    assert rows[0][3] == Decimal("1e-07"), "科学计数法是合法 JSON,不受影响"

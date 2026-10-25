"""导出客户格式的 INSERT DML。

客户没有脚本管理 API（原文：「由于安全原因，目前脚本仅能通过版本 dml 带出」），
新增诊断能力必须随版本发布上线。所以这份 DML 就是交付物本身 ——
列顺序、引号风格、NULL 写法都要与客户样例一致，客户才能直接拿去执行。
"""
import sys
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common.grmp import script as sc  # noqa: E402
from grmp_middleware.grmp_mock import dml  # noqa: E402
from common.grmp.placeholder import ParamDef  # noqa: E402


def _rec(**kw):
    base = dict(
        id="527",
        script_name="slowsql.slow_sql",
        script_content="select 1 where runtime > {{threshold_seconds}}",
        params=(ParamDef(key="threshold_seconds", type="INTEGER"),),
        create_user="123456789",
        create_time="2026-07-03 19:36:58.978",
        last_modify_user="123456789",
    )
    base.update(kw)
    return sc.ScriptRecord(**base)


def test_targets_the_three_part_table_name():
    """【实】表名为三段式 grmp.grmp.script_config（库.模式.表）。"""
    assert "grmp.grmp.script_config" in dml.insert_statement(_rec())


def test_column_list_follows_the_customer_column_order():
    stmt = dml.insert_statement(_rec())
    cols = stmt.split("(", 1)[1].split(")", 1)[0]
    names = [c.strip().strip('"') for c in cols.split(",")]
    assert names == list(sc.SCRIPT_CONFIG_COLUMNS)


def test_extend_column_is_quoted_in_the_column_list():
    """【实】样例 DML 里只有 "extend" 带引号 —— 保留字或大小写敏感。"""
    assert '"extend"' in dml.insert_statement(_rec())


def test_text_values_are_single_quoted():
    stmt = dml.insert_statement(_rec())
    assert "'slowsql.slow_sql'" in stmt
    assert "'SQL'" in stmt
    assert "'AGENT'" in stmt


def test_integer_columns_are_written_bare():
    """【实】样例中 is_valid/is_asyn/refered_appbusiness 是裸整数，不带引号。"""
    stmt = dml.insert_statement(_rec())
    values = stmt.rsplit("VALUES", 1)[1]
    assert ", 1, " in values or values.strip().startswith("('527', 'SQL'")
    assert "'1'" not in values.replace("'1663'", "")


def test_null_columns_are_written_as_null_keyword():
    """【实】样例中 region/deployment_form/last_modify_time 为 NULL 而非空串。

    写成 '' 会让「未设置作用域」变成「作用域是空字符串」，
    过滤规则一旦按 NULL 判断就会静默失配。
    """
    stmt = dml.insert_statement(_rec())
    assert "NULL" in stmt
    assert "''," not in stmt


def test_single_quotes_inside_sql_are_doubled():
    """脚本正文里带引号是常态（如 datname not in ('template1')），必须转义。"""
    rec = _rec(
        script_content="select 1 where datname not in ('template1')",
        params=(),
    )
    stmt = dml.insert_statement(rec)
    assert "(''template1'')" in stmt


def test_parameter_config_json_is_embedded_as_a_quoted_string():
    stmt = dml.insert_statement(_rec())
    assert (
        '\'[{"key":"threshold_seconds","value":"","type":"INTEGER",'
        '"autoAcquire":false}]\'' in stmt
    )


def test_statement_ends_with_semicolon():
    assert dml.insert_statement(_rec()).rstrip().endswith(";")


def test_script_file_bundles_statements_with_a_provenance_header():
    """交付物要能自证来源：客户拿到的是一段 SQL，得知道它是什么、谁生成的。"""
    text = dml.script_file([_rec(), _rec(id="528", script_name="health.db_info")])
    assert text.count("INSERT INTO") == 2
    assert text.lstrip().startswith("--")

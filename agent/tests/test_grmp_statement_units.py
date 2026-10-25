"""语句形态判定 —— 只读还是写。

注册期的硬拦截与运行期的会话模式都靠它，所以放在 common/grmp/：
tools/ 里的风险标注也复用同一份判定，两处不能各判各的。
"""
import sys
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

from common.grmp.statement import is_read_only, leading_keyword  # noqa: E402


@pytest.mark.parametrize(
    "sql",
    [
        "select 1",
        "  select 1",
        "SELECT 1",
        "-- 注释\nselect 1",
        "/* 块注释 */ select 1",
        "with x as (select 1) select * from x",
        "explain select 1",
        "show all",
        "values (1)",
    ],
)
def test_read_only_statements(sql):
    assert is_read_only(sql) is True


@pytest.mark.parametrize(
    "sql",
    [
        "delete from t",
        "update t set a = 1",
        "insert into t values (1)",
        "create index i on t(a)",
        "drop table t",
        "truncate table t",
        "alter table t add column b int",
        "vacuum t",
        "analyze t",
        "set work_mem='64MB'",
        "prepare p as select 1",
    ],
)
def test_write_statements(sql):
    assert is_read_only(sql) is False


def test_leading_keyword_skips_comments_and_whitespace():
    assert leading_keyword("  -- x\n /* y */ SELECT 1") == "select"
    assert leading_keyword("") == ""


def test_multi_statement_is_judged_by_every_statement():
    """多语句要逐条判，不能只看第一条。

    实测中间件允许一次调用里发多条语句（PREPARE + EXPLAIN EXECUTE 就靠这个）。
    只看首个关键字的话，`select 1; drop table t;` 会被判成只读。
    """
    assert is_read_only("select 1; select 2;") is True
    assert is_read_only("select 1; drop table t;") is False
    assert is_read_only("select 1;\n-- 注释\ndelete from t;") is False


def test_semicolon_inside_string_literal_does_not_split():
    """字符串里的分号不是语句分隔符，否则会把一条语句错判成两条。"""
    assert is_read_only("select 'a;b' as x") is True
    assert is_read_only("select 'drop table t;' as x") is True

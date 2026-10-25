"""语句形态判定的回归测试 —— 三条实测出来的绕过/误判。

全部来自一次 explain 的双模式全量矩阵（29 用例 × 中间件/直连），
每一条都在 og5 上真复现过，不是推演出来的。
"""
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common.grmp.statement import (  # noqa: E402
    is_dml,
    is_read_only,
    leading_keyword,
    split_statements,
    strip_noise,
)


# ===========================================================================
# 注释里的分号不是语句分隔
# ===========================================================================

@pytest.mark.parametrize("sql", [
    "SELECT 1 AS x -- a;b;c",
    "SELECT 1 /* ; */ AS x",
    "-- 开头注释;里有分号\nSELECT 1",
    "SELECT 1 AS x /* 多行\n;注释; */",
])
def test_semicolon_inside_a_comment_is_not_a_separator(sql):
    """**这条在中间件模式下是硬失败。**

    `SELECT 1 AS x -- a;b;c` 原先被判成 3 条语句，explain 报
    「SQL 含多条语句（3 条）」—— 数字和结论都是错的。直连模式靠回落到
    原始会话侥幸能跑，客户环境只有中间件，就是拒之门外。
    """
    assert len(split_statements(sql)) == 1


def test_trailing_comment_only_fragment_is_dropped():
    """`SELECT 1; -- 收尾注释` 是一条语句，不是两条。"""
    assert len(split_statements("SELECT 1; -- 收尾注释")) == 1


@pytest.mark.parametrize("sql", ["-- 只有注释", "/* 只有注释 */", "  \n  ", ";"])
def test_comment_or_blank_only_yields_no_statement(sql):
    assert split_statements(sql) == []


def test_semicolon_inside_a_literal_still_is_not_a_separator():
    """原本就对的行为，别在修注释时把它弄丢。"""
    assert len(split_statements("SELECT 'a;b' AS x")) == 1
    assert len(split_statements("SELECT 'it''s; fine' AS x")) == 1


def test_real_separators_still_split():
    assert len(split_statements("SELECT 1; SELECT 2")) == 2
    assert len(split_statements("SELECT 1; DROP TABLE t; --")) == 2


# ===========================================================================
# 前导注释不能让 DML 蒙混过关
# ===========================================================================

@pytest.mark.parametrize("sql", [
    "UPDATE t SET a = 1",
    "/* c */ UPDATE t SET a = 1",
    "-- c\nUPDATE t SET a = 1",
    "/* c */ DELETE FROM t",
    "\n\n  /* 多行\n注释 */\n  INSERT INTO t VALUES (1)",
    "-- c\nMERGE INTO t USING s ON (1=1)",
])
def test_leading_comment_does_not_hide_dml(sql):
    """**这条实测写过库。**

    原先三个 skill 各抄一份 `^\\s*(insert|update|delete|merge)\\b`，
    `^\\s*` 跳空白但不跳注释。og5 上 gsql 与直连驱动两条各复现一次：
    explain `--analyze` 下 `/* c */ UPDATE ...` 退出 0、报告一切正常，
    而表真被改了；`/* c */ DELETE FROM t` 把表清空了。

    同一个函数还决定要不要包回滚事务 —— 判错一次是双重失效：既没拒绝，
    也没回滚。
    """
    assert is_dml(sql) is True


@pytest.mark.parametrize("sql", [
    "SELECT 1",
    "SELECT 'delete' AS x",                      # 字面量里的关键字不算
    "SELECT 'update t set a=1' AS sql_text",
    'SELECT "delete" FROM t',                    # 引号标识符也不算
    "WITH c AS (SELECT 1) SELECT * FROM c",
    "SELECT comment FROM (SELECT 1 AS comment) t",
])
def test_read_only_queries_are_not_dml(sql):
    assert is_dml(sql) is False


def test_dml_hidden_in_a_cte_is_caught():
    assert is_dml("WITH x AS (DELETE FROM t RETURNING 1) SELECT * FROM x")
    assert is_dml("/* c */ WITH x AS (UPDATE t SET a=1 RETURNING 1) SELECT * FROM x")


def test_cte_with_dml_word_only_in_a_literal_is_not_dml():
    """`WITH x AS (SELECT 'delete') ...` 不是 DML —— 扫关键字前要抹掉字面量。"""
    assert is_dml("WITH x AS (SELECT 'delete' AS a) SELECT * FROM x") is False


def test_dml_in_any_statement_counts():
    """多语句时任何一条是 DML 就算 —— 只看首条会漏。"""
    assert is_dml("SELECT 1; UPDATE t SET a = 1") is True


# ===========================================================================
# strip_noise
# ===========================================================================

def test_strip_noise_blanks_comments_without_glueing_tokens():
    """删注释会把 `select 1--c\\nfrom t` 粘成 `1from`，所以抹成等长空格。"""
    out = strip_noise("select 1--c\nfrom t")
    assert "1from" not in out
    assert out.split() == ["select", "1", "from", "t"]


def test_strip_noise_keeps_literals_by_default():
    assert "'a;b'" in strip_noise("SELECT 'a;b' /* c */")


def test_strip_noise_can_blank_literals_too():
    out = strip_noise("SELECT 'delete' AS a", literals=True)
    assert "delete" not in out
    assert "SELECT" in out and "AS a" in out


# ===========================================================================
# is_read_only 不能被上面的改动带歪
# ===========================================================================

def test_is_read_only_unaffected_by_comment_handling():
    assert is_read_only("select 1;\n-- 注释\ndelete from t;") is False
    assert is_read_only("select 1; select 2;") is True
    assert is_read_only("select 'drop table t;' as x") is True
    assert is_read_only("/* c */ delete from t") is False


def test_leading_keyword_still_skips_noise():
    assert leading_keyword("/* c */ -- d\n  SELECT 1") == "select"
    assert leading_keyword("-- 只有注释") == ""

"""common.sqlqualify —— search_path 切不成时,把 SQL 里不带 schema 的表名补全成 schema.表。

现场(客户 09-09 早截图):两语句模板在客户的执行器上没走通,退回提示里的备选②「把表名写全」是模型手工做的。
这一步该由脚本做:确定性、可测、报告里注明。只动表位置(FROM / JOIN / UPDATE / INSERT INTO / DELETE FROM 及 FROM 列表里
逗号后的项),不动列名、别名、函数、CTE 名、已带 schema 的名字、字符串和注释里的东西。
"""
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common import sqlqualify as q  # noqa: E402


def _q(sql, schema="gmag"):
    return q.qualify_tables(sql, schema)


def test_simple_from_gets_the_schema():
    sql, changed = _q("SELECT COUNT(1) FROM batch_job_status WHERE batch_time = '2026-09-08'")
    assert sql == "SELECT COUNT(1) FROM gmag.batch_job_status WHERE batch_time = '2026-09-08'"
    assert changed == ["batch_job_status"]


def test_joins_and_aliases():
    sql, changed = _q("SELECT a.x FROM t1 a LEFT JOIN t2 AS b ON a.id = b.id INNER JOIN t3 ON t3.k = a.k")
    assert sql == "SELECT a.x FROM gmag.t1 a LEFT JOIN gmag.t2 AS b ON a.id = b.id INNER JOIN gmag.t3 ON t3.k = a.k"
    assert changed == ["t1", "t2", "t3"]


def test_comma_list_in_from():
    sql, changed = _q("SELECT * FROM t1, t2 b, t3 WHERE t1.id = b.id")
    assert sql == "SELECT * FROM gmag.t1, gmag.t2 b, gmag.t3 WHERE t1.id = b.id"
    assert changed == ["t1", "t2", "t3"]


def test_column_lists_are_not_tables():
    sql, changed = _q("SELECT id, name, amount FROM orders o, items i WHERE o.id = i.oid ORDER BY id, name")
    assert sql == "SELECT id, name, amount FROM gmag.orders o, gmag.items i WHERE o.id = i.oid ORDER BY id, name"
    assert changed == ["orders", "items"]


def test_subqueries_are_walked():
    sql, changed = _q("SELECT * FROM (SELECT id FROM inner_t) s WHERE s.id IN (SELECT id FROM other_t)")
    assert sql == "SELECT * FROM (SELECT id FROM gmag.inner_t) s WHERE s.id IN (SELECT id FROM gmag.other_t)"
    assert changed == ["inner_t", "other_t"]


def test_cte_names_are_left_alone():
    sql, changed = _q("WITH c AS (SELECT id FROM base_t), d AS (SELECT * FROM c) SELECT * FROM d JOIN real_t r ON r.id = d.id")
    assert sql == "WITH c AS (SELECT id FROM gmag.base_t), d AS (SELECT * FROM c) SELECT * FROM d JOIN gmag.real_t r ON r.id = d.id"
    assert changed == ["base_t", "real_t"]


def test_already_qualified_and_functions_are_skipped():
    sql, changed = _q("SELECT * FROM other.t1 JOIN generate_series(1, 3) g ON true JOIN t2 ON t2.id = g")
    assert sql == "SELECT * FROM other.t1 JOIN generate_series(1, 3) g ON true JOIN gmag.t2 ON t2.id = g"
    assert changed == ["t2"]


def test_quoted_identifiers_keep_their_quotes():
    sql, changed = _q('SELECT * FROM "MixedCase" m JOIN plain p ON p.id = m.id')
    assert sql == 'SELECT * FROM gmag."MixedCase" m JOIN gmag.plain p ON p.id = m.id'
    assert changed == ['"MixedCase"', "plain"]


def test_schema_with_uppercase_is_quoted():
    sql, _ = _q("SELECT * FROM t", schema="Gmag")
    assert sql == 'SELECT * FROM "Gmag".t'


def test_dml_targets():
    assert _q("UPDATE t SET a = 1 WHERE id = 2")[0] == "UPDATE gmag.t SET a = 1 WHERE id = 2"
    assert _q("DELETE FROM t WHERE id = 2")[0] == "DELETE FROM gmag.t WHERE id = 2"
    assert _q("INSERT INTO t (a) SELECT b FROM s")[0] == "INSERT INTO gmag.t (a) SELECT b FROM gmag.s"


def test_only_and_lateral_keywords():
    sql, changed = _q("SELECT * FROM ONLY parent p JOIN LATERAL (SELECT 1 FROM child c WHERE c.pid = p.id) x ON true")
    assert sql == "SELECT * FROM ONLY gmag.parent p JOIN LATERAL (SELECT 1 FROM gmag.child c WHERE c.pid = p.id) x ON true"
    assert changed == ["parent", "child"]


def test_strings_and_comments_are_untouched():
    sql, changed = _q("SELECT 'FROM fake' AS s, t.x FROM t -- FROM comment_t\n/* FROM block_t */ WHERE t.y = 'JOIN z'")
    assert sql == "SELECT 'FROM fake' AS s, t.x FROM gmag.t -- FROM comment_t\n/* FROM block_t */ WHERE t.y = 'JOIN z'"
    assert changed == ["t"]


def test_keywords_are_case_insensitive_and_names_are_not_dedup_lost():
    sql, changed = _q("select * from T1 join t1 on true")
    assert sql == "select * from gmag.T1 join gmag.t1 on true"
    assert changed == ["T1", "t1"]


def test_nothing_to_change_returns_the_same_text():
    sql = "SELECT * FROM s.t WHERE x IN (SELECT 1)"
    assert _q(sql) == (sql, [])


def test_placeholders_and_whitespace_are_preserved():
    sql, _ = _q("SELECT *\n  FROM   t\n WHERE id = {{id}} AND x = $1")
    assert sql == "SELECT *\n  FROM   gmag.t\n WHERE id = {{id}} AND x = $1"


def test_invalid_schema_is_rejected():
    with pytest.raises(ValueError):
        _q("SELECT * FROM t", schema="x; drop")


def test_unqualified_tables_lists_only_bare_table_positions():
    assert q.unqualified_tables("SELECT * FROM a x, b.c y JOIN d ON true WHERE id IN (SELECT id FROM e)") == ["a", "d", "e"]

"""共享的对抗性 SQL 语料:所有解析 SQL 的地方跑同一套(2026-11-08 行方测试报告后加)。

行方的测试是按语法边界逐条构造的,我们原先的单测只覆盖写代码的人想到的写法,4 个高危 bug 都落在语法边界上:
函数里的 FROM(Bug 1)、带分号的引号标识符(Bug 2)、行尾注释吞分号(Bug 3)、DELETE … USING(M3)、
CREATE INDEX … USING / 函数索引(M1 / M2)。以后新增的边界写法加进下面的语料,所有解析器自动一起跑。

同一件事有两套实现的(分句:中间件侧 common/grmp/statement.split_statements 与 sqlreview 的 lexer.split),
逐条比对结果必须一致 —— Bug 2 就是两套里只有一套处理了引号。
"""
import importlib.util
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common import schema_infer, sqlqualify  # noqa: E402
from common.grmp.statement import split_statements  # noqa: E402


def _load_lexer():
    scripts = _ROOT / "skills" / "gaussdb-sqlreview" / "scripts"
    for m in ("model", "lexer"):
        sys.modules.pop(m, None)
    sys.path.insert(0, str(scripts))
    try:
        spec = importlib.util.spec_from_file_location("lexer_for_corpus", scripts / "lexer.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(str(scripts))


lexer = _load_lexer()

# (SQL, 补全到 schema app 之后的样子, 表位置上不带 schema 的名字)
QUALIFY = [
    ("SELECT EXTRACT(HOUR FROM sample_time) FROM t1",
     "SELECT EXTRACT(HOUR FROM sample_time) FROM app.t1", ["t1"]),
    ("SELECT TRIM(BOTH 'x' FROM col) FROM t",
     "SELECT TRIM(BOTH 'x' FROM col) FROM app.t", ["t"]),
    ("SELECT SUBSTRING(name FROM 1 FOR 3), OVERLAY(a PLACING 'x' FROM 2) FROM t",
     "SELECT SUBSTRING(name FROM 1 FOR 3), OVERLAY(a PLACING 'x' FROM 2) FROM app.t", ["t"]),
    ("SELECT extract(epoch from now() - t.d) FROM t WHERE x IN (SELECT y FROM u)",
     "SELECT extract(epoch from now() - t.d) FROM app.t WHERE x IN (SELECT y FROM app.u)", ["t", "u"]),
    ("SELECT coalesce((SELECT max(id) FROM t2), 0) FROM t1 JOIN t3 USING (id)",
     "SELECT coalesce((SELECT max(id) FROM app.t2), 0) FROM app.t1 JOIN app.t3 USING (id)", ["t2", "t1", "t3"]),
    ("DELETE FROM t USING other o, o2 WHERE t.id = o.id",
     "DELETE FROM app.t USING app.other o, app.o2 WHERE t.id = o.id", ["t", "other", "o2"]),
    ("MERGE INTO tgt USING src ON tgt.id = src.id WHEN MATCHED THEN UPDATE SET v = src.v",
     "MERGE INTO app.tgt USING app.src ON tgt.id = src.id WHEN MATCHED THEN UPDATE SET v = src.v", ["tgt", "src"]),
    ("SELECT count(*) OVER (PARTITION BY a ORDER BY b) FROM t",
     "SELECT count(*) OVER (PARTITION BY a ORDER BY b) FROM app.t", ["t"]),
    ("SELECT 'FROM x' AS s, \"FROM\" FROM t -- FROM y",
     "SELECT 'FROM x' AS s, \"FROM\" FROM app.t -- FROM y", ["t"]),
    ("WITH c AS (SELECT * FROM base) SELECT * FROM c JOIN other ON true",
     "WITH c AS (SELECT * FROM app.base) SELECT * FROM c JOIN app.other ON true", ["base", "other"]),
    ("INSERT INTO t (a, b) SELECT x, y FROM s",
     "INSERT INTO app.t (a, b) SELECT x, y FROM app.s", ["t", "s"]),
    ("SELECT position('a' IN name), cast(x AS int) FROM t",
     "SELECT position('a' IN name), cast(x AS int) FROM app.t", ["t"]),
]

# (SQL 文本, 语句条数) —— 中间件侧与 sqlreview 侧必须切得一样
SPLIT = [
    ('SELECT "a;b" FROM t', 1),
    ("SELECT 'a;b' FROM t; SELECT 2", 2),
    ("SELECT 1 -- trailing comment;\nSELECT 2;", 1),          # 注释里的分号不算:这本来就是一条
    ("SELECT 1 -- trailing comment\n;\nSELECT 2\n;", 2),      # --top 拼接后的样子
    ("SELECT 1 /* a;b */ FROM t", 1),
    ('SELECT "x"";y" FROM t', 1),                              # 引号里转义的引号
]

# (DDL, 索引列)
INDEX = [
    ("CREATE INDEX idx_a ON t USING btree (a)", ("a",)),
    ("CREATE INDEX idx_g ON public.t USING gin (doc jsonb_path_ops)", ("doc",)),
    ("CREATE INDEX idx_f ON t (lower(name))", ("lower(name)",)),
    ('CREATE UNIQUE INDEX uk_p ON t (a DESC NULLS LAST, "B", lower(t.x))', ("a", "b", "lower(t.x)")),
    ("CREATE INDEX idx_w ON t USING btree (a, b, c, d, e, f) LOCAL", ("a", "b", "c", "d", "e", "f")),
]


@pytest.mark.parametrize("sql,qualified,unqualified", QUALIFY)
def test_qualify(sql, qualified, unqualified):
    assert sqlqualify.qualify_tables(sql, "app")[0] == qualified
    assert sqlqualify.unqualified_tables(sql) == unqualified
    assert schema_infer.sqlqualify.unqualified_tables(sql) == unqualified      # 按 schema 推断用的是同一个


@pytest.mark.parametrize("sql,n", SPLIT)
def test_both_splitters_agree(sql, n):
    assert len(split_statements(sql)) == n, "中间件侧"
    assert len(lexer.split(sql)) == n, "sqlreview 侧"


# 已知且有意的差异:中间件侧分句用来判「只读 / 单条」,刻意保守 —— `$$…$$` 不认、宁可多切一刀把合法语句拒掉,
# 也不漏切把写语句放过去(common/grmp/statement._scan 的说明)。这里只要求它**不少切**。
@pytest.mark.parametrize("sql,n", [("SELECT $$a;b$$; SELECT 2", 2)])
def test_grmp_side_may_over_split_but_never_under_split(sql, n):
    assert len(lexer.split(sql)) == n
    assert len(split_statements(sql)) >= n


@pytest.mark.parametrize("ddl,cols", INDEX)
def test_create_index_columns(ddl, cols):
    assert lexer._names("create_index", ddl)["index_cols"] == cols

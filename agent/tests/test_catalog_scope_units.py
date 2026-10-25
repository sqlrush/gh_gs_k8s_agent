"""common.catalog_scope —— 目录行(表 / 索引 / 列统计 / 统计新鲜度 / 列类型)按 schema 过滤。

目录脚本按裸表名全库匹配:Oracle 迁移库里同名表跨 schema 很常见(现场日志里 gbatch 与 dsc_ora_public 都有),
不过滤的话证据包混进别的 schema 的行,列类型推断撞上冲突就放弃,占位符退回启发式,合成值类型不对又是一个 400。
规则:SQL 里显式写了 schema.表 的按显式的;否则按本次解析出的 schema;都没有就不过滤(老行为)。
已知 schema 下一行都没有时退到 public(search_path 的兜底);还没有就全留,不把证据清空。
老白名单行没有 schema 列时不过滤(兼容客户还没重灌的情况)。
"""
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common import catalog_scope as cs  # noqa: E402


def _rows():
    return [
        {"schema_name": "app", "table_name": "orders", "v": "app-orders"},
        {"schema_name": "other", "table_name": "orders", "v": "other-orders"},
        {"schema_name": "public", "table_name": "cfg", "v": "public-cfg"},
        {"schema_name": "app", "table_name": "cfg", "v": "app-cfg"},
    ]


def test_explicit_schemas_come_from_qualified_refs():
    assert cs.explicit_schemas(["other.orders", "cfg", "APP.Items"]) == {"orders": "other", "items": "app"}


def test_unknown_schema_keeps_everything():
    scope = cs.Scope(explicit={}, default="")
    assert [r["v"] for r in cs.keep_rows(_rows(), scope, table_key="table_name", schema_key="schema_name")] \
        == ["app-orders", "other-orders", "public-cfg", "app-cfg"]


def test_default_schema_filters_each_table():
    scope = cs.Scope(explicit={}, default="app")
    assert [r["v"] for r in cs.keep_rows(_rows(), scope, table_key="table_name", schema_key="schema_name")] \
        == ["app-orders", "app-cfg"]


def test_explicit_schema_wins_over_default():
    scope = cs.Scope(explicit={"orders": "other"}, default="app")
    assert [r["v"] for r in cs.keep_rows(_rows(), scope, table_key="table_name", schema_key="schema_name")] \
        == ["other-orders", "app-cfg"]


def test_falls_back_to_public_then_to_everything():
    rows = [{"schema_name": "public", "table_name": "t", "v": "pub"},
            {"schema_name": "other", "table_name": "t", "v": "oth"}]
    scope = cs.Scope(explicit={}, default="app")
    assert [r["v"] for r in cs.keep_rows(rows, scope, table_key="table_name", schema_key="schema_name")] == ["pub"]
    rows2 = [{"schema_name": "x", "table_name": "t", "v": "x"}, {"schema_name": "y", "table_name": "t", "v": "y"}]
    assert [r["v"] for r in cs.keep_rows(rows2, scope, table_key="table_name", schema_key="schema_name")] == ["x", "y"]


def test_rows_without_a_schema_column_are_left_alone():
    """客户白名单里还是老的 SQL 正文(没有 schema 列)时不能把证据过滤空。"""
    rows = [{"table_name": "orders", "v": 1}, {"table_name": "orders", "v": 2}]
    scope = cs.Scope(explicit={}, default="app")
    assert cs.keep_rows(rows, scope, table_key="table_name", schema_key="schema_name") == rows

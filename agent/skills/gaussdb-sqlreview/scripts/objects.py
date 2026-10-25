"""Catalog collection (I/O only — this module never judges anything).

Each dimension degrades independently: if the index query is denied, the table
findings still come out and the reason lands in `notes`, mirroring
skills/health/scripts/collectors.py.
"""
from __future__ import annotations

import common
from model import IndexFact, ObjectFacts, TableFact

import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve()

for parent in _HERE.parents:
    if (parent / "common" / "sql.py").exists():
        sys.path.insert(0, str(parent))
        break

# SQL 已迁到 scripts/registry/sqlreview/ —— 两条路径共用同一份定义
# 取数失败只认这一个类型。换一种数据库访问方式时，改的是访问模块，
# 不是这里 —— 详见 common/grmp/errors.py。
from common import access  # noqa: E402


TABLES_SCRIPT = "sqlreview.tables"
INDEXES_SCRIPT = "sqlreview.indexes"

# 布尔列的真值形态。协议把所有列值都渲染成字符串，而布尔的渲染形式接口文档
# 自相矛盾（§3.1 给 true/false，§3.2 给 t/f），所以两套都认。
# 直接 bool(值) 是不行的：bool("f") 是 True —— 每张表都会被判成「有主键」，
# 不报错，只是结论反了。
_TRUE_TEXTS = frozenset({"t", "true", "y", "yes", "1"})


def _as_bool(val) -> bool:
    return str(val).strip().lower() in _TRUE_TEXTS


def _as_tuple(val) -> tuple[str, ...]:
    """列名/约束名数组。脚本里已用 array_to_string 投影成逗号分隔文本。

    数组的字符串化形式协议没有定义（见 registry/sqlreview/tables.yaml），
    所以在 SQL 里就转成文本，这里只按逗号切。空串表示空数组。
    """
    if not val:
        return ()
    return tuple(v for v in str(val).split(",") if v)


def _collect_tables(runner, schema: str) -> tuple[tuple[TableFact, ...], list[str]]:
    try:
        rows = runner.run(TABLES_SCRIPT, {"schema": schema})
    except access.QueryError as exc:
        return (), [f"表信息采集失败（已降级）：{exc}"]
    return tuple(
        TableFact(schema=str(r["schema"]), table=str(r["table"]),
                  has_pk=_as_bool(r["has_pk"]),
                  fks=_as_tuple(r["fks"]), columns=_as_tuple(r["columns"]))
        for r in rows
    ), []


def _collect_indexes(runner, schema: str) -> tuple[tuple[IndexFact, ...], list[str]]:
    try:
        rows = runner.run(INDEXES_SCRIPT, {"schema": schema})
    except access.QueryError as exc:
        return (), [f"索引信息采集失败（已降级）：{exc}"]
    return tuple(
        IndexFact(schema=str(r["schema"]), table=str(r["table"]), name=str(r["name"]),
                  columns=_as_tuple(r["columns"]),
                  is_unique=_as_bool(r["is_unique"]),
                  is_primary=_as_bool(r["is_primary"]),
                  scans=int(r["scans"] or 0))
        for r in rows
    ), []


def collect_facts(runner, schema: str) -> ObjectFacts:
    """Snapshot one schema's tables and indexes. Never raises on query failure."""
    tables, t_notes = _collect_tables(runner, schema)
    indexes, i_notes = _collect_indexes(runner, schema)
    return ObjectFacts(tables=tables, indexes=indexes, notes=tuple(t_notes + i_notes))

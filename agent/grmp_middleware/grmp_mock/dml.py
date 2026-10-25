"""把 ScriptRecord 导出成客户格式的 INSERT DML。

这份 DML 是交付物本身：客户没有脚本管理 API，新增诊断能力必须随版本
发布上线（「由于安全原因，目前脚本仅能通过版本 dml 带出」）。
所以列顺序、引号风格、NULL 写法都按客户样例来 —— 差一点客户就要手工改，
手工改就会改错。
"""
from __future__ import annotations

from typing import Any, Iterable, List, Sequence

from common.grmp.script import SCRIPT_CONFIG_COLUMNS, ScriptRecord, quoted_column

TABLE = "grmp.grmp.script_config"

# 按客户样例，这几列是裸整数
_INTEGER_COLUMNS = frozenset({"refered_appbusiness", "is_valid", "is_asyn"})


def _literal(column: str, value: Any) -> str:
    """渲染一个列值。

    NULL 必须写成 NULL 关键字而不是 ''：作用域列一旦按 NULL 判断，
    空串会让「不限作用域」静默变成「作用域等于空串」。
    """
    if value is None:
        return "NULL"
    if column in _INTEGER_COLUMNS:
        return str(int(value))
    return "'%s'" % str(value).replace("'", "''")


def insert_statement(record: ScriptRecord) -> str:
    """单条 INSERT。列顺序即客户样例的列顺序。"""
    row = record.as_row()
    cols = ", ".join(quoted_column(c) for c in SCRIPT_CONFIG_COLUMNS)
    vals = ", ".join(_literal(c, row[c]) for c in SCRIPT_CONFIG_COLUMNS)
    return "INSERT INTO %s (%s) VALUES (%s);" % (TABLE, cols, vals)


def script_file(
    records: Sequence[ScriptRecord],
    header_note: str = "",
) -> str:
    """把多条 INSERT 拼成一个可交付的 .sql 文件。

    带来源说明：客户拿到的是一段要在生产库执行的 SQL，必须能自证
    它是什么、由什么生成、包含哪些脚本。
    """
    lines: List[str] = [
        "-- GRMP 诊断脚本注册 DML",
        "-- 由 grmp_middleware/grmp_register.py 从 scripts/registry/ 生成，请勿手工编辑",
        "-- 共 %d 条脚本：" % len(records),
    ]
    for rec in records:
        lines.append("--   %s -> id=%s" % (rec.script_name, rec.id))
    if header_note:
        lines.extend("-- %s" % line for line in header_note.splitlines())
    lines.append("")
    for rec in records:
        lines.append(insert_statement(rec))
    lines.append("")
    return "\n".join(lines)

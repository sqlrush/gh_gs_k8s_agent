"""目录行(表 / 索引 / 列统计 / 统计新鲜度 / 列类型)按 schema 过滤。

目录脚本按裸表名全库匹配(`WHERE c.relname IN (...)`):Oracle 迁移库里同名表跨 schema 很常见
(现场日志里 gbatch 与 dsc_ora_public 都有),不过滤的话证据包混进别的 schema 的表、索引、列统计,
列类型推断撞上冲突就放弃,占位符退回启发式,合成值类型不对又是一个 400。

规则:
  · SQL 里显式写了 schema.表 的,按显式的;
  · 否则按本次解析出的 schema(statement_history.schema_name / --schema / user_name 推测);
  · 都没有就不过滤(老行为);
  · 已知 schema 下一行都没有时退到 public(search_path 的兜底),再没有就全留——不把证据清空;
  · 行里没有 schema 列(客户白名单还是老 SQL 正文)时不过滤,兼容未重灌的现场。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List


@dataclass(frozen=True)
class Scope:
    explicit: Dict[str, str] = field(default_factory=dict)   # 表名(小写) → SQL 里显式写的 schema
    default: str = ""                                         # 本次解析出的 schema;空 = 未知

    def schema_for(self, table: str) -> str:
        return self.explicit.get((table or "").lower(), "") or (self.default or "").lower()


def explicit_schemas(refs: Iterable[str]) -> Dict[str, str]:
    """extract_table_refs 的输出('s.t' / 't')→ {表: schema},只收带 schema 的。"""
    out: Dict[str, str] = {}
    for ref in refs or []:
        if "." in ref:
            schema, _, table = ref.rpartition(".")
            if schema and table:
                out[table.lower()] = schema.lower()
    return out


def keep_rows(rows: List[Dict[str, Any]], scope: Scope, *, table_key: str, schema_key: str) -> List[Dict[str, Any]]:
    """按 scope 过滤目录行,保持原顺序。"""
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    if not rows or (not scope.explicit and not scope.default):
        return rows
    if not all(schema_key in r for r in rows):
        return rows                                   # 老白名单行没有 schema 列:不过滤
    by_table: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        by_table.setdefault(str(r.get(table_key, "")).lower(), []).append(r)
    chosen: List[Dict[str, Any]] = []
    for table, group in by_table.items():
        want = scope.schema_for(table)
        if not want:
            chosen.extend(group)
            continue
        hit = [r for r in group if str(r.get(schema_key, "")).lower() == want]
        if not hit:
            hit = [r for r in group if str(r.get(schema_key, "")).lower() == "public"]
        chosen.extend(hit or group)
    keep = {id(r) for r in chosen}
    return [r for r in rows if id(r) in keep]


__all__ = ["Scope", "explicit_schemas", "keep_rows"]

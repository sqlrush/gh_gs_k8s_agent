"""贴文本没带 --schema 时,按表名在目录里找 schema:唯一就用,多个候选就问,没有就照旧。

现场(客户 2026-09-09 早):模型贴文本漏了 --schema → 400。命令是模型拼的,SKILL.md 只能约束不能保证;
脚本层自己去 pg_class 查一遍,把「模型记不记得带参数」从关键路径上拿掉。
纪律不变:同名表跨 schema 时不猜(Oracle 迁移库里 gbatch / dsc_ora_public 同名表很常见),列出候选交用户;
各表分散在不同 schema(多 schema 的 search_path)同样不猜——只有 DBA 给执行账号设完整 search_path 能根治。
按执行账号名推测出来的 schema(备机退回路)只用来在多个候选之间打破平局,不能压过目录里的唯一匹配。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Set, Tuple

from common import sqlqualify

_SYSTEM_PREFIXES = ("pg_", "dbe_")
_SYSTEM_NAMES = {"information_schema", "pkg_service", "pkg_util", "sqladvisor", "db4ai", "snapshot",
                 "blockchain", "cstore", "pmk", "coverage"}


@dataclass(frozen=True)
class Inference:
    schema: str = ""                                   # 推断出的 schema;空 = 推不出
    tables: Tuple[str, ...] = ()                       # 参与推断的不带 schema 的表名(查目录用的形态)
    candidates: Dict[str, Tuple[str, ...]] = field(default_factory=dict)   # 表 → 它所在的 schema 们
    reason: str = ""                                   # 人话
    ambiguous: bool = False                            # 有候选但定不下来——要问用户
    via_guess: bool = False                            # 是靠推测值在候选里打破平局才定下来的


def _s(v: Any) -> str:
    return "" if v is None else str(v).strip()


def _is_system(schema: str) -> bool:
    return schema.startswith(_SYSTEM_PREFIXES) or schema in _SYSTEM_NAMES


def _lookup_key(name: str) -> str:
    """查目录用的 relname:加引号的原样(去引号、还原双引号),不加引号的小写。"""
    if name.startswith('"') and name.endswith('"') and len(name) >= 2:
        return name[1:-1].replace('""', '"')
    return name.lower()


def quoted_list(keys) -> str:
    return ",".join("'%s'" % k.replace("'", "''") for k in keys)


def infer(runner, script: str, sql: str, guess: str = "") -> Inference:
    names = sqlqualify.unqualified_tables(sql)
    keys = list(dict.fromkeys(_lookup_key(n) for n in names))
    if not keys:
        return Inference(reason="SQL 里没有不带 schema 的表名")
    try:
        rows = runner.run(script, {"names": quoted_list(keys)})
    except Exception as exc:                        # noqa: BLE001 —— 推断失败只是「推不出」,不能变成新的报错
        text = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        return Inference(tables=tuple(keys), reason="目录查询失败:%s" % text.split("。", 1)[0][:120])
    found: Dict[str, Set[str]] = {k: set() for k in keys}
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        schema, table = _s(r.get("nspname")), _s(r.get("relname"))
        if table in found and schema and not _is_system(schema):
            found[table].add(schema)
    cands = {k: tuple(sorted(v)) for k, v in found.items()}
    missing = [k for k, v in cands.items() if not v]
    if missing:
        return Inference(tables=tuple(keys), candidates=cands,
                         reason="目录里没有名为 %s 的表/视图(对执行账号可见的范围内)" % "、".join(missing))
    common = set.intersection(*(set(v) for v in cands.values()))
    detail = ";".join("%s 在 %s" % (k, "、".join(v)) for k, v in cands.items())
    if len(common) == 1:
        return Inference(schema=next(iter(common)), tables=tuple(keys), candidates=cands,
                         reason="按表名在目录里唯一匹配到 schema %s" % next(iter(common)))
    if not common:
        return Inference(tables=tuple(keys), candidates=cands, ambiguous=True,
                         reason="各表不在同一个 schema 下(%s)——这条 SQL 依赖多 schema 的 search_path" % detail)
    if guess and guess in common:
        return Inference(schema=guess, tables=tuple(keys), candidates=cands, via_guess=True,
                         reason="多个 schema 都有这些表(%s),按推测值 %s 取" % (detail, guess))
    return Inference(tables=tuple(keys), candidates=cands, ambiguous=True,
                     reason="同名表在多个 schema 下(%s)" % detail)


def describe(inf: Inference) -> str:
    """歧义时给用户看的话:说清候选,要用户定,不猜。"""
    cands = sorted({s for v in inf.candidates.values() for s in v})
    return ("无法确定这条 SQL 的 schema:%s。不猜——请确认这条 SQL 平时在哪个 schema 下跑,加 --schema <schema> 重跑"
            "(候选:%s);各表分属不同 schema 时需要 DBA 给执行账号设完整的 search_path,或把表名写全。"
            % (inf.reason, "、".join(cands) or "无"))


__all__ = ["Inference", "infer", "describe", "quoted_list"]

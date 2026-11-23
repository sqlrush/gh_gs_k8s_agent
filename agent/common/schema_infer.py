"""贴文本没带 --schema 时,按表名在目录里找 schema:唯一就用,多个候选按规则选一个并写明,没有就照旧。

现场(2026-09-09 早):模型贴文本漏了 --schema → 400。命令是模型拼的,SKILL.md 只能约束不能保证;
脚本层自己去 pg_class 查一遍,把「模型记不记得带参数」从关键路径上拿掉。
不再因为跨 schema 停下(2026-11-06 行方:技能不要做跨 schema 的限制):同名表在多个 schema 下时按推测值、
否则按名字顺序取第一个;各表分散在不同 schema 时把这些 schema 组成 search_path(`a, b`)一起用。两种都在 reason
里写明取了哪个、候选有哪些(picked=True),调用方把它写进报告 —— 取错了用户加 --schema 重跑即可,不再先挡下来问。
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
    picked: bool = False                               # 有多个候选,按规则选了一个(reason 里写明)——报告要说
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
        path = ", ".join(sorted({s for v in cands.values() for s in v}))
        return Inference(schema=path, tables=tuple(keys), candidates=cands, picked=True,
                         reason="各表分属不同 schema(%s),按 search_path %s 一起取;不对就加 --schema 指定" % (detail, path))
    if guess and guess in common:
        return Inference(schema=guess, tables=tuple(keys), candidates=cands, via_guess=True,
                         reason="多个 schema 都有这些表(%s),按推测值 %s 取" % (detail, guess))
    first = sorted(common)[0]
    return Inference(schema=first, tables=tuple(keys), candidates=cands, picked=True,
                     reason="同名表在多个 schema 下(%s),按 %s 取;不对就加 --schema 指定" % (detail, first))


__all__ = ["Inference", "infer", "quoted_list"]

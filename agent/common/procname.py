"""存储过程名的解析与定位 —— procinfo / proctune 共用。

支持三段名 schema.package.proc。现场(客户 2026-09-08 截图):模型按慢 SQL 里的写法传了
gmag.pckg_xxx.proc_xxx,旧逻辑按最后一个点切,把 gmag.pckg_xxx 当 schema 找不到,报 not found,
模型于是脑补「GaussDB 包子程序不注册在 pg_proc」。实测(og5 A 兼容库)包内过程就在 pg_proc 里:
proname 是过程名、prosrc 是过程体、propackageid 指向 gs_package;GaussDB 505 同源。

两条纪律:
  · 两段名 a.b 既可能是 schema.proc 也可能是 package.proc,先按前者查、再按后者查;
  · 同名过程不止一个(Oracle 迁移库里不同包同名很常见)而没指定包 → 拒绝并列出候选。
    旧脚本 LIMIT 1 静默取第一个,分析的可能是另一个包里的同名过程,输出看起来完全正常。
名字各段只放行未加引号的合法标识符——它们进的是白名单模板的 String 参数位。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,62}$")


@dataclass(frozen=True)
class ProcRef:
    schema: str
    package: str
    name: str


class NotFound(ValueError):
    """按名字查不到过程。带上 ref 与 tried(按什么查过),skill 用它们代为排查(common/proc_locate.py):
    连的库对不对、执行账号有没有权限、过程本身在不在——而不是只给用户一句「找不到」。"""

    def __init__(self, ref: ProcRef, tried: Tuple[str, ...], message: str):
        super().__init__(message)
        self.ref = ref
        self.tried = tuple(tried)


def _ident(part: str, what: str) -> str:
    if not IDENT_RE.match(part or ""):
        raise ValueError(
            "%s %r 不是合法标识符(未加引号的名字只含字母、数字、下划线、$,且以字母或下划线开头)" % (what, part))
    return part.lower()


def split_qualified(q: str) -> ProcRef:
    """'proc' / 'schema.proc' / 'schema.package.proc'。未加引号的标识符按小写存,这里统一小写。"""
    parts = (q or "").strip().split(".")
    if len(parts) == 1:
        return ProcRef("", "", _ident(parts[0], "过程名"))
    if len(parts) == 2:
        return ProcRef(_ident(parts[0], "schema 或包名"), "", _ident(parts[1], "过程名"))
    if len(parts) == 3:
        return ProcRef(_ident(parts[0], "schema"), _ident(parts[1], "包名"), _ident(parts[2], "过程名"))
    raise ValueError("过程名 %r 最多三段:schema.package.proc" % (q,))


def qualified(schema: str, package: str, name: str) -> str:
    return ".".join(x for x in (schema or "", package or "", name) if x)


def lookup(runner, script: str, qualified_name: str) -> Dict[str, Any]:
    """按名字取过程定义那一行(nspname / proname / lanname / prosrc / args / package)。

    找不到 → ValueError 带「按什么查过」和三段名写法;同名多个 → ValueError 列出候选,不猜。
    """
    ref = split_qualified(qualified_name)
    attempts = [(ref.schema, ref.package)]
    if ref.schema and not ref.package:
        attempts.append(("", ref.schema))          # 两段名也可能是 package.proc
    rows: List[Dict[str, Any]] = []
    for schema, package in attempts:
        rows = [r for r in (runner.run(script, {"name": ref.name, "schema": schema, "package": package}) or [])
                if isinstance(r, dict)]
        if rows:
            break
    if not rows:
        tried = tuple(qualified(s, p, ref.name) for s, p in attempts)
        raise NotFound(ref, tried, (
            "过程 %r 在当前库的 pg_proc 里找不到(按 %s 查过)。包内过程请写全 schema.package.proc;"
            "也请核对所连的库与 schema——包内过程同样登记在 pg_proc 里,不是「不支持」。"
            % (qualified_name, " / ".join(tried))))
    distinct = sorted({(str(r.get("nspname", "")), str(r.get("package", "") or "")) for r in rows})
    if len(distinct) > 1:
        cands = "、".join(qualified(s, p, ref.name) for s, p in distinct)
        raise ValueError(
            "同名过程不止一个:%s。请用 schema.package.proc 指明是哪一个——不猜,"
            "猜错就是在分析另一个包里的同名过程,而输出看起来完全正常。" % cands)
    return rows[0]


__all__ = ["IDENT_RE", "ProcRef", "NotFound", "split_qualified", "qualified", "lookup"]

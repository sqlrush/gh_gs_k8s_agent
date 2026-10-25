"""适用性:条目声明的产品系列 / 版本范围 / 系统,与本实例的环境画像比对。

结果只有三种:适用 / 不适用 / 未知。**拿不准一律「未知」**——版本写法千奇百怪,解析不了的不猜;
不适用的条目只降权、写明理由,不丢弃(模型要能说出「这条是 openGauss 的行为,不适用本环境」)。

版本范围写法(逗号分隔,任一子句命中即算):
    505.2        精确到给出的位数(505 匹配 505.x.x)
    505.*        同上
    >= 503 / ≥503 / < 505.2 / <= 505.1 / > 2.0
    505.1–505.2  闭区间(– — - ~ 至 到 均可)
    * / 全部 / 所有
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence, Tuple

APPLICABLE, NOT_APPLICABLE, UNKNOWN = "适用", "不适用", "未知"
_ANY = {"*", "全部", "所有", "all", "any"}
_RANGE_RE = re.compile(r"^\s*([vV]?\d+(?:\.\d+)*)\s*(?:–|—|-|~|至|到)\s*([vV]?\d+(?:\.\d+)*)\s*$")
_CMP_RE = re.compile(r"^\s*(>=|≥|<=|≤|>|<|=)?\s*([vV]?\d+(?:\.\d+)*)(\.\*)?\s*$")
_FAMILY_ALIASES = (
    ("GaussDB 分布式", ("gaussdb分布式", "分布式", "gaussdb(dws)", "dws", "distributed")),
    ("GaussDB 集中式", ("gaussdb集中式", "集中式", "主备版", "centralized")),
    ("openGauss", ("opengauss",)),
    ("PostgreSQL", ("postgresql", "postgres", "pg")),
    ("通用", ("通用", "全部", "*")),
)


@dataclass(frozen=True)
class Verdict:
    state: str          # 适用 / 不适用 / 未知
    reason: str = ""

    @property
    def label(self) -> str:
        if self.state == APPLICABLE:
            return ""
        return f"{self.state}本环境:{self.reason}" if self.reason else f"{self.state}本环境"


def parse_version(text: Any) -> Optional[Tuple[int, ...]]:
    m = re.search(r"\d+(?:\.\d+)*", str(text or ""))
    return tuple(int(x) for x in m.group(0).split(".")) if m else None


def _prefix_eq(v: Tuple[int, ...], spec: Tuple[int, ...]) -> bool:
    return v[:len(spec)] == spec


def _cmp(v: Tuple[int, ...], spec: Tuple[int, ...]) -> int:
    n = max(len(v), len(spec))
    a = v + (0,) * (n - len(v))
    b = spec + (0,) * (n - len(spec))
    return (a > b) - (a < b)


def version_matches(spec: Any, version: Any) -> Optional[bool]:
    """True / False / None(写法解析不了,或本环境版本未知)。"""
    v = parse_version(version)
    clauses = [c for c in re.split(r"[,,;;、]", str(spec or "")) if c.strip()]
    if not clauses:
        return None
    if any(c.strip().lower() in _ANY for c in clauses):
        return True
    if v is None:
        return None
    understood = False
    for c in clauses:
        m = _RANGE_RE.match(c)
        if m:
            lo, hi = parse_version(m.group(1)), parse_version(m.group(2))
            understood = True
            if lo and hi and _cmp(v, lo) >= 0 and (_cmp(v, hi) <= 0 or _prefix_eq(v, hi)):
                return True
            continue
        m = _CMP_RE.match(c)
        if not m:
            continue
        understood = True
        op, ref = m.group(1) or "=", parse_version(m.group(2))
        if ref is None:
            continue
        c_ = _cmp(v, ref)
        hit = {"=": _prefix_eq(v, ref), ">=": c_ >= 0, "≥": c_ >= 0, "<=": c_ <= 0 or _prefix_eq(v, ref),
               "≤": c_ <= 0 or _prefix_eq(v, ref), ">": c_ > 0 and not _prefix_eq(v, ref), "<": c_ < 0}[op]
        if hit:
            return True
    return False if understood else None


def family(text: Any) -> str:
    """产品系列归一;认不出返回空串。"""
    t = re.sub(r"\s+", "", str(text or "")).lower()
    if not t:
        return ""
    for name, keys in _FAMILY_ALIASES:
        if any(k in t for k in keys):
            return name
    if "gaussdb" in t:
        return "GaussDB 集中式"
    return ""


def _families(value: Any) -> Tuple[str, ...]:
    vals = value if isinstance(value, (list, tuple)) else [value]
    return tuple(f for f in (family(v) for v in vals) if f)


def judge(item_meta: Dict[str, Any], profile_meta: Optional[Dict[str, Any]], *,
          versions_key: str = "versions") -> Verdict:
    """条目(frontmatter)对本实例画像(frontmatter)的适用性。

    条目的声明取 applies.{product,versions,systems},产品文档 / 缺陷的顶层 product / versions /
    affected_versions 也认(versions_key 指定看哪个版本字段)。"""
    applies = item_meta.get("applies") if isinstance(item_meta.get("applies"), dict) else {}
    prod = applies.get("product") or item_meta.get("product")
    vers = applies.get("versions") or item_meta.get(versions_key)
    systems = applies.get("systems")
    if not (prod or vers or systems):
        return Verdict(APPLICABLE)                 # 没声明边界的条目不打标签,免得满屏「未知」
    if not profile_meta:
        return Verdict(UNKNOWN, "未登记本实例的环境画像")
    if systems and not (isinstance(systems, str) and systems.strip() in _ANY):
        wanted = systems if isinstance(systems, (list, tuple)) else [systems]
        wanted = [str(s).strip() for s in wanted if str(s).strip()]
        if wanted and "*" not in wanted and str(profile_meta.get("system") or "") not in wanted:
            return Verdict(NOT_APPLICABLE, f"只适用于系统 {'/'.join(wanted)}")
    fams = [f for f in _families(prod) if f != "通用"] if prod else []
    here = family(profile_meta.get("product"))
    if fams:
        if not here:
            return Verdict(UNKNOWN, "画像没写产品系列")
        if here not in fams:
            return Verdict(NOT_APPLICABLE, f"条目是 {'/'.join(fams)},本环境是 {here}")
    if vers:
        ok = version_matches(vers, profile_meta.get("version"))
        if ok is None:
            why = "画像没写版本" if not profile_meta.get("version") else f"版本范围「{vers}」解析不了"
            return Verdict(UNKNOWN, why)
        if not ok:
            return Verdict(NOT_APPLICABLE, f"条目版本 {vers},本环境 {profile_meta.get('version')}")
    return Verdict(APPLICABLE)


def defect_verdict(item_meta: Dict[str, Any], profile_meta: Optional[Dict[str, Any]]) -> str:
    """已知缺陷:本环境版本受不受影响(一句话,直接写进引用行)。"""
    if not profile_meta or not profile_meta.get("version"):
        return "本环境是否受影响未知(没有画像版本)"
    v = profile_meta.get("version")
    fixed = item_meta.get("fixed_in")
    if fixed and version_matches(f">= {fixed}", v):
        return f"本环境 {v} 已含修复({fixed})"
    hit = version_matches(item_meta.get("affected_versions"), v)
    if hit is None:
        return f"本环境 {v} 是否受影响未知(受影响版本写法解析不了)"
    return f"本环境 {v} {'受影响' if hit else '不受影响'}"


def first_match(profiles: Sequence[Dict[str, Any]], alias: str) -> Optional[Dict[str, Any]]:
    """连接别名 → 画像(画像的 aliases 列表里有它)。"""
    a = (alias or "").strip().lower()
    if not a:
        return None
    for p in profiles:
        names = p.get("aliases") or []
        names = names if isinstance(names, (list, tuple)) else re.split(r"[,,、\s]+", str(names))
        if any(str(n).strip().lower() == a for n in names):
            return p
    return None

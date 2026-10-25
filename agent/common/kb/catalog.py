"""全库条目的统一视图:规范条款、修正、案例、旧指南、六类类型化条目 → Entry。

L0 / L1 的生成、`kb.py show / evidence` 都只认 Entry,不再各自去读五种存放形态。
文件是真相:每次现读现算(几千条也是毫秒级),诊断环境的知识库目录只读,也不依赖 index 产物新不新。
"""
from __future__ import annotations

import pathlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import yaml

from . import cases as kbcases
from . import topics as kbtopics
from . import types as kbtypes

_FIRST_SENTENCE_RE = re.compile(r"^(.+?[。;;!?!?])")


@dataclass(frozen=True)
class Entry:
    id: str
    type: str                     # norm / errata / case / guide / runbook / defect / baseline / product / wiki / profile
    title: str
    summary: str
    file: str                     # 相对 <kb> 的路径
    topics: Tuple[str, ...] = ()
    topics_inferred: bool = False  # 没写 topics,按同义词推断的
    verification: str = ""        # 案例的结论强度 / 条目的验证状态 / 缺陷的厂商结论
    source: str = ""
    as_of: str = ""
    evidence: Tuple[Any, ...] = ()
    aliases: Tuple[str, ...] = ()
    meta: Dict[str, Any] = field(default_factory=dict)
    archived: bool = False

    @property
    def short(self) -> str:
        return kbtypes.TYPE_NAMES.get(self.type, (self.type, self.type))[1]

    @property
    def cite(self) -> str:
        """引用时的写法:〔手册 RB-LCK-001〕"""
        return f"〔{self.short} {self.id}〕"


@dataclass(frozen=True)
class Catalog:
    entries: Tuple[Entry, ...]
    topics: Tuple[kbtopics.Topic, ...]
    warnings: Tuple[str, ...] = ()

    def by_id(self) -> Dict[str, Entry]:
        out: Dict[str, Entry] = {}
        for e in self.entries:
            out.setdefault(e.id, e)
        for e in self.entries:              # 改过类型 / 迁移前的旧 ID 也要找得到(报告里引用过)
            for a in e.aliases:
                out.setdefault(a, e)
        return out

    def find(self, item_id: str) -> Optional[Entry]:
        key = item_id.strip()
        hit = self.by_id().get(key)
        if hit is not None:
            return hit
        prefixed = [e for e in self.entries if e.id.startswith(key)] if len(key) >= 8 else []
        return prefixed[0] if len(prefixed) == 1 else None

    def of_type(self, *types: str) -> List[Entry]:
        return [e for e in self.entries if e.type in types and not e.archived]

    def in_topic(self, slug: str) -> List[Entry]:
        live = [e for e in self.entries if not e.archived and e.type != "profile"]
        if slug == kbtopics.OTHER:
            return [e for e in live if not e.topics]
        return [e for e in live if slug in e.topics]

    def profiles(self) -> List[Entry]:
        return self.of_type("profile")

    def customer(self) -> List[Entry]:
        """客户知识库自己的条目(不含官方文档集)。"""
        return [e for e in self.entries if not e.meta.get("docset")]

    def from_docset(self, code: str = "") -> List[Entry]:
        return [e for e in self.entries if e.meta.get("docset") and (not code or e.meta.get("docset") == code)]


def first_sentence(text: str, limit: int = 120) -> str:
    flat = " ".join(str(text or "").split())
    m = _FIRST_SENTENCE_RE.match(flat)
    s = m.group(1) if m and len(m.group(1)) <= limit else flat
    return s if len(s) <= limit else s[:limit - 1] + "…"


# ---------------------------------------------------------------- loaders (各存放形态 → Entry)

def _rule_entries(kb: pathlib.Path, sub: str, topics: Sequence[kbtopics.Topic]) -> List[Entry]:
    out: List[Entry] = []
    root = kb / sub
    if not root.is_dir():
        return out
    for path in sorted(root.rglob("*.y*ml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            continue
        if not isinstance(data, list):
            continue
        for e in data:
            if not isinstance(e, dict) or not e.get("id"):
                continue
            if sub == "rules" and str(e.get("status") or "active").lower() != "active":
                continue
            rule = str(e.get("rule") or "").strip()
            kws = e.get("keywords") or []
            declared = kbtypes.as_list(e.get("topics"))
            text = " ".join([rule, str(e.get("rationale") or ""), " ".join(map(str, kws)) if isinstance(kws, list) else str(kws)])
            tops = declared or kbtopics.infer_topics(text, topics)
            out.append(Entry(
                id=str(e["id"]), type=kbtypes.NORM, title=first_sentence(rule, 60) or str(e["id"]),
                summary=first_sentence(rule, 160), file=path.relative_to(kb).as_posix(), topics=tops,
                topics_inferred=not declared, verification=str(e.get("severity") or ""),
                source=str(e.get("source") or ""), evidence=tuple(kbtypes.as_list(e.get("evidence"))),
                meta=dict(e), archived=(sub == "archive")))
    return out


def _md_meta(path: pathlib.Path) -> Tuple[Dict[str, Any], str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}, ""
    meta, body, err = kbcases.split_frontmatter(text)
    return (meta or {}) if not err else {}, body if not err else text


def _errata_and_guides(kb: pathlib.Path, topics: Sequence[kbtopics.Topic]) -> List[Entry]:
    out: List[Entry] = []
    for sub, typ in (("errata", kbtypes.ERRATA), ("guides", kbtypes.GUIDE)):
        root = kb / sub
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.md")):
            meta, body = _md_meta(path)
            first = next((ln.strip().lstrip("#").strip() for ln in body.splitlines() if ln.strip()), path.stem)
            desc = str(meta.get("description") or "").strip()
            declared = kbtypes.as_list(meta.get("topics"))
            tops = declared or kbtopics.infer_topics(f"{desc} {body[:2000]}", topics)
            out.append(Entry(
                id=str(meta.get("id") or path.stem), type=typ, title=first_sentence(desc or first, 60),
                summary=first_sentence(desc or first, 160), file=path.relative_to(kb).as_posix(),
                topics=tops, topics_inferred=not declared, source=str(meta.get("source") or ""),
                aliases=kbtypes.as_list(meta.get("aliases")), meta=dict(meta)))
    return out


def case_summary(c: kbcases.Case) -> str:
    judge = c.primary_factor or first_sentence(c.section("判断"), 80)
    act = first_sentence(c.section("处置"), 80)
    return "".join(s for s in (f"根因:{judge}" if judge else "", f";处置:{act}" if act else ""))[:200]


def _case_entries(kb: pathlib.Path, topics: Sequence[kbtopics.Topic]) -> List[Entry]:
    out: List[Entry] = []
    cases, _ = kbcases.load_cases(kb)
    for c in cases:
        declared = c.topics
        text = " ".join([c.title, " ".join(c.signals), c.section("现场"), c.section("复发标志"), c.primary_factor])
        tops = declared or kbtopics.infer_topics(text, topics)
        out.append(Entry(
            id=c.id, type=kbtypes.CASE, title=c.title, summary=case_summary(c),
            file=c.path.relative_to(kb).as_posix(), topics=tops, topics_inferred=not declared,
            verification=c.conclusion, source=c.source, as_of=c.occurred_at, evidence=c.evidence,
            meta={"system": c.system, "signals": list(c.signals), "occurred_at": c.occurred_at,
                  "sections": dict(c.sections)}))
    return out


def _typed_entries(kb: pathlib.Path) -> List[Entry]:
    items, _ = kbtypes.load_items(kb)
    return [Entry(
        id=i.id, type=i.type, title=i.title, summary=first_sentence(i.summary, 200),
        file=i.path.relative_to(kb).as_posix(), topics=i.topics, verification=i.verification,
        source=i.source, as_of=i.as_of, evidence=i.evidence, aliases=i.aliases,
        meta={**i.meta, "sections": dict(i.sections)}) for i in items]


def _docset_entries(kb: pathlib.Path) -> List[Entry]:
    """官方文档集:只读各套的 catalog.json(几千条不逐个解析文件);条目的产品系列与版本取自该套的 manifest。"""
    from . import docset as kbdocset
    out: List[Entry] = []
    for ds in kbdocset.installed(kb):
        m = ds.manifest
        base = {"docset": ds.code, "product": m.get("product", ""), "versions": str(m.get("versions", "")),
                "deployment": m.get("deployment", "")}
        for c in ds.catalog():
            out.append(Entry(
                id=str(c.get("id")), type=kbtypes.TYPED["product"].code, title=str(c.get("title", "")),
                summary=str(c.get("summary", "")), file=f"{kbdocset.DOCSETS_DIR}/{ds.code}/items/{c.get('id')}.md",
                topics=tuple(c.get("topics") or ()), source=f"{m.get('source_file') or ds.title}",
                as_of=str(m.get("as_of") or ""),
                meta={**base, "kind": c.get("kind", ""), "doc_path": list(c.get("path") or []),
                      "doc_key": c.get("doc_key", ""), "summary_by": c.get("summary_by", "script"),
                      "section_names": list(c.get("sections") or []), "chars": c.get("chars", 0)}))
    return out


def load(kb: pathlib.Path, with_docsets: bool = True) -> Catalog:
    topics, warns = kbtopics.load_taxonomy(kb)
    entries = (_rule_entries(kb, "rules", topics) + _rule_entries(kb, "archive", topics)
               + _errata_and_guides(kb, topics) + _case_entries(kb, topics) + _typed_entries(kb)
               + (_docset_entries(kb) if with_docsets else []))
    order = {t: i for i, t in enumerate(kbtypes.DISPLAY_ORDER)}
    entries.sort(key=lambda e: (order.get(e.type, 99), e.archived, e.id))
    return Catalog(entries=tuple(entries), topics=tuple(topics), warnings=tuple(warns))


def profile_for_alias(cat: Catalog, alias: str) -> Optional[Entry]:
    from . import versions as kbversions
    metas = [dict(e.meta, _id=e.id) for e in cat.profiles()]
    hit = kbversions.first_match(metas, alias)
    return next((e for e in cat.profiles() if hit and e.id == hit["_id"]), None)


def type_counts(cat: Catalog) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for e in cat.entries:
        if not e.archived:
            out[e.type] = out.get(e.type, 0) + 1
    return out


def entries_text(entries: Iterable[Entry]) -> str:
    return " ".join(f"{e.id} {e.title} {e.summary}" for e in entries)


# ---------------------------------------------------------------- 覆盖度(kb.py health 与大盘)

# 八类知识的计数口径与建议的导入顺序(2026-10-24):最小环境画像和现行规范 → 高频故障案例及排查步骤 → 对应产品章节 → Wiki
COVERAGE_ROWS = (("norm", "规范"), ("case", "案例"), ("profile", "画像"), ("baseline", "基线"), ("defect", "缺陷"),
                 ("runbook", "手册"), ("product", "产品文档"), ("wiki", "Wiki"), ("guide", "指南(待归类)"))


def coverage(kb: pathlib.Path) -> Dict[str, int]:
    """八类各登记了多少(修正并入规范);读不了就空,大盘与 health 照常。"""
    try:
        c = type_counts(load(kb))
    except Exception:                                     # noqa: BLE001
        return {}
    c["norm"] = c.get("norm", 0) + c.pop("errata", 0)
    return {k: c.get(k, 0) for k, _ in COVERAGE_ROWS}


def next_step(types: Dict[str, int]) -> str:
    """按建议的导入顺序,说下一步最该补什么。"""
    if not types:
        return ""
    if not types.get("profile"):
        return "先登记环境画像(kb.py profile 起草):没有画像,条目对本环境适不适用无法判断"
    if not types.get("norm"):
        return "导入现行规范(公司内部规范)"
    if types.get("case", 0) < 5:
        return "导入高频故障案例(带排查思路:候选原因、取证顺序、排除理由、区分证据)"
    if not types.get("runbook"):
        return "补处置与恢复手册(执行前提、影响、验证、回退)"
    if not types.get("product"):
        return "补对应的产品文档章节(写清产品系列与版本)"
    if types.get("guide"):
        return "旧指南还没归类型:kb.py migrate"
    if not types.get("wiki"):
        return "存量 Wiki 经验持续补齐(注明验证状态)"
    return "覆盖齐了:按缺口清单补材料"

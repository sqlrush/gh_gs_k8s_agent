"""知识库大盘「目录」页签的数据:按渐进式披露 L0 → L1 → L2 → L3 一层层取,不一次端出几千条。

报告端口 4097 上四条路(现读现算;骨架按文件修改时间缓存):
  /_kb/outline.json                       L0 骨架 + L1 专题(客户条目、本行确认过的候选原因、官方文档计数与代表条目)
                                          + 官方文档集的原书目录(到第 5 级,带条目数;GUC 参数能下到参数类)
  /_kb/list.json?docset=&path=&topic=&offset=&limit=
                                          L1 下钻:某个目录 / 某个专题下的官方文档条目(分页,一页最多 200 条)
  /_kb/item/<ID>.json                     L2 一条条目的全文(小节)
  /_kb/evidence/<ID>.json                 L3 原始证据:官方文档给原页;**客户条目只给出处位置,不给原件内容**
                                          (原件在 sources/,大盘不端客户原件 —— 与目录页签同一条规矩)
ID 只在目录里查,不拿请求里的字符串拼路径;收件目录 inbox/、原件 sources/、索引 index/ 一律不读。
"""
from __future__ import annotations

import os
import pathlib
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

for _p in (os.environ.get("GSDB_SKILLS_DIR", ""), "/opt/agent/config/opencode/skills",
           str(pathlib.Path(__file__).resolve().parents[1] / "agent")):
    if _p and (pathlib.Path(_p) / "common" / "kb" / "__init__.py").is_file():
        sys.path.insert(0, _p)
        break

import yaml  # noqa: E402

from common.kb import catalog as kbcat  # noqa: E402
from common.kb import docindex  # noqa: E402
from common.kb import docnav  # noqa: E402
from common.kb import docset as kbdocset  # noqa: E402
from common.kb import graphfiles as gf  # noqa: E402
from common.kb import layers as kblayers  # noqa: E402
from common.kb import topics as kbtopics  # noqa: E402
from common.kb import types as kbtypes  # noqa: E402

LIST_MAX = 200
EVIDENCE_MAX = 30000
SECTION_MAX = 20000
TOC_DEPTH = 5
_READ_DIRS = ("cases", "rules", "graph", "errata", "guides", "topics") + kbtypes.TYPED_DIRS


def _key(kb: pathlib.Path) -> Tuple[float, ...]:
    """缓存键:客户知识各目录的最新修改时间 + 各官方文档集 manifest / catalog 的修改时间(不扫几千个条目文件)。"""
    latest = 0.0
    for sub in _READ_DIRS:
        root = kb / sub
        if root.is_dir():
            for p in root.rglob("*"):
                try:
                    latest = max(latest, p.stat().st_mtime)
                except OSError:
                    pass
    sets = []
    for ds in kbdocset.installed(kb):
        for name in ("manifest.json", "catalog.json"):
            try:
                sets.append((ds.root / name).stat().st_mtime)
            except OSError:
                sets.append(0.0)
    return (latest, *sets)


def _brief(e: kbcat.Entry) -> Dict[str, Any]:
    return {"id": e.id, "type": e.type, "title": e.title, "summary": kbcat.first_sentence(e.summary, 160),
            "verification": e.verification}


def _toc(entries: List[kbcat.Entry]) -> List[Dict[str, Any]]:
    """官方文档的原书目录(到第 TOC_DEPTH 级):[{name, path, n, children}]。"""
    root: Dict[str, Any] = {"children": {}}
    for e in entries:
        full = list(e.meta.get("doc_path") or [])
        path = full if e.meta.get("kind") == "guc" else (full[:-1] or full[:1])   # GUC 参数的上级就是它所在的参数类页
        node = root
        for i, seg in enumerate(path[:TOC_DEPTH]):
            node = node["children"].setdefault(seg, {"name": seg, "path": path[:i + 1], "n": 0, "children": {}})
            node["n"] += 1

    def flat(node: Dict[str, Any]) -> List[Dict[str, Any]]:
        return [{"name": c["name"], "path": c["path"], "n": c["n"], "children": flat(c)} for c in node["children"].values()]
    return flat(root)


def build(kb: pathlib.Path) -> Dict[str, Any]:
    kb = pathlib.Path(kb)
    if not kb.is_dir():
        return {"attached": False, "reason": "知识库目录不存在"}
    cat = kbcat.load(kb)
    triples, _ = gf.load_triples(kb, case_ids=[e.id for e in cat.of_type(kbtypes.CASE)])
    counts = kbcat.type_counts(cat)
    official = cat.from_docset()
    sets = kbdocset.installed(kb)
    try:
        from common.kb import config as kbconfig
        test_switch = kbconfig.load(kb).docset_applicability_unknown
    except Exception:                                    # noqa: BLE001  kb.yaml 坏了不影响目录
        test_switch = False
    topics = []
    for t in list(cat.topics) + [kbtopics.Topic(kbtopics.OTHER, kbtopics.OTHER_NAME)]:
        view = kblayers.topic_view(cat, triples, t.slug)
        own = [e for e in view.entries if not e.meta.get("docset")]
        off = [e for e in view.entries if e.meta.get("docset")]
        if not own and not off:
            continue
        cats: Dict[str, int] = {}
        for e in off:
            cats[docnav.category(e)] = cats.get(docnav.category(e), 0) + 1
        topics.append({
            "slug": t.slug, "name": t.name, "synonyms": list(view.synonyms)[:12],
            "causes": [{"symptom": c.symptom, "rootcause": c.rootcause, "cases": list(c.cases),
                        "discriminator": c.discriminator, "from": c.disc_from} for c in view.causes[:12]],
            "lookalikes": list(view.lookalikes),
            "entries": [_brief(e) for e in kblayers._sorted_entries(own)],
            "official": len(off), "official_categories": cats,
            "official_top": [dict(_brief(e), category=docnav.category(e))
                             for e in docnav.representatives(off, t.slug, t)],
        })
    profiles = [{"id": p.id, "title": p.title, "system": p.meta.get("system", ""), "product": p.meta.get("product", ""),
                 "version": str(p.meta.get("version", "")), "aliases": list(kbtypes.as_list(p.meta.get("aliases")))}
                for p in cat.profiles()]
    docsets = []
    for ds in sets:
        m, idx = ds.manifest, ds.manifest.get("index") or {}
        ents = [e for e in official if e.meta.get("docset") == ds.code]
        docsets.append({"code": ds.code, "title": m.get("title", ""), "product": m.get("product", ""),
                        "versions": str(m.get("versions", "")), "items": len(ents),
                        "doc_version": m.get("doc_version", ""), "kernel_version": m.get("kernel_version", ""),
                        "index_complete": bool(idx.get("complete")), "model_summaries": m.get("model_summaries", 0),
                        "kinds": m.get("kinds", {}), "toc": _toc(ents)})
    return {"attached": True, "version": _version(kb), "types": counts, "official": len(official),
            "test_switch": bool(test_switch and official), "profiles": profiles, "topics": topics, "docsets": docsets}


def _version(kb: pathlib.Path) -> str:
    try:
        return (kb / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


class Outline:
    """骨架按修改时间缓存;条目全文与原页每次现读(一次只读一条)。"""

    def __init__(self, kb: pathlib.Path):
        self.kb = pathlib.Path(kb)
        self._key: Optional[Tuple[float, ...]] = None
        self._value: Optional[Dict[str, Any]] = None
        self._cat: Optional[kbcat.Catalog] = None

    def outline(self) -> Dict[str, Any]:
        key = _key(self.kb)
        if self._value is None or key != self._key:
            value = build(self.kb)
            value["built_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self._key, self._value, self._cat = key, value, kbcat.load(self.kb) if value.get("attached") else None
        return self._value

    def catalog(self) -> Optional[kbcat.Catalog]:
        self.outline()
        return self._cat

    # --- L1 下钻 -------------------------------------------------------------
    def list(self, docset: str = "", path: str = "", topic: str = "", offset: int = 0, limit: int = 100) -> Dict[str, Any]:
        cat = self.catalog()
        if cat is None:
            return {"total": 0, "items": []}
        ents = cat.from_docset(docset) if docset else cat.from_docset()
        if path:
            want = [p for p in path.split(kbdocset.SEP) if p]
            ents = [e for e in ents if list(e.meta.get("doc_path") or [])[:len(want)] == want]
        if topic:
            ents = [e for e in ents if (topic == kbtopics.OTHER and not e.topics) or topic in e.topics]
            # 专题下按「标题带专题关键词 → GUC 参数 → 视图 → 说明页 → SQL 语法 → 错误码」排,与 L1 代表条目同一口径
            ents = docnav.representatives(ents, topic, kbtopics.by_slug(cat.topics).get(topic), n=len(ents))
        offset = max(0, int(offset))
        limit = max(1, min(LIST_MAX, int(limit)))
        page = ents[offset:offset + limit]
        return {"total": len(ents), "offset": offset, "limit": limit,
                "items": [dict(_brief(e), category=docnav.category(e), path=list(e.meta.get("doc_path") or []))
                          for e in page]}

    # --- L2 / L3 -------------------------------------------------------------
    def _find(self, item_id: str) -> Optional[kbcat.Entry]:
        cat = self.catalog()
        return cat.by_id().get(item_id) if cat is not None else None

    def item(self, item_id: str) -> Optional[Dict[str, Any]]:
        e = self._find(item_id)
        if e is None:
            return None
        out: Dict[str, Any] = {"id": e.id, "type": e.type, "title": e.title, "topics": list(e.topics),
                               "summary": e.summary, "verification": e.verification, "source": e.source,
                               "as_of": e.as_of, "file": e.file, "docset": e.meta.get("docset", "")}
        if e.meta.get("docset"):
            meta, secs = docindex.parse_item((self.kb / e.file).read_text(encoding="utf-8"))
            out.update({"product": meta.get("product", ""), "versions": str(meta.get("versions", "")),
                        "deployment": meta.get("deployment", ""), "doc_path": list(meta.get("doc_path") or []),
                        "summary_by": meta.get("summary_by", "script"), "category": docnav.category(e),
                        "sections": [{"name": k, "text": v[:SECTION_MAX]} for k, v in secs.items() if k != "摘要"]})
        elif e.type == kbtypes.NORM:
            m = e.meta
            out["sections"] = [{"name": n, "text": str(m.get(k))} for k, n in
                               (("rule", "条文"), ("rationale", "为什么"), ("criteria", "怎么判定违规"),
                                ("approval", "审批要求"), ("effective", "生效"), ("keywords", "关键词"))
                               if m.get(k) not in (None, "", [])]
        else:
            secs = e.meta.get("sections") or {}
            if not secs:
                try:
                    _m, secs = docindex.parse_item((self.kb / e.file).read_text(encoding="utf-8"))
                except OSError:
                    secs = {}
            out["sections"] = [{"name": k, "text": str(v)[:SECTION_MAX]} for k, v in secs.items() if k != "摘要" and v]
            facts = {k: e.meta.get(k) for k in ("product", "versions", "system", "affected_versions", "fixed_in",
                                                  "vendor_status", "metric", "unit", "occurred_at", "conclusion", "severity")
                     if e.meta.get(k) not in (None, "", [])}
            out["facts"] = {k: (v if isinstance(v, (str, int, float)) else yaml.safe_dump(v, allow_unicode=True).strip())
                            for k, v in facts.items()}
        out["evidence_count"] = len(e.evidence) + (1 if e.source else 0)
        return out

    def evidence(self, item_id: str) -> Optional[Dict[str, Any]]:
        e = self._find(item_id)
        if e is None:
            return None
        if e.meta.get("docset"):
            meta, _s = docindex.parse_item((self.kb / e.file).read_text(encoding="utf-8"))
            page = self.kb / kbdocset.DOCSETS_DIR / str(e.meta.get("docset")) / str(meta.get("page") or "")
            text = page.read_text(encoding="utf-8") if page.is_file() else ""
            name = str(meta.get("doc_key") or "").partition("#")[2]
            if name and f"### {name}" in text:                  # GUC 参数:只给本参数那一段
                i = text.index(f"### {name}")
                nxt = text.find("\n### ", i + 4)
                text = text[i:nxt if nxt > 0 else len(text)]
            return {"id": e.id, "kind": "page", "source": meta.get("source", ""), "text": text[:EVIDENCE_MAX],
                    "truncated": len(text) > EVIDENCE_MAX}
        refs = [str(r) if not isinstance(r, dict) else f"{r.get('file', '')}#{r.get('at', '')}"
                for r in list(e.evidence) + ([e.source] if e.source and e.source not in e.evidence else [])]
        return {"id": e.id, "kind": "refs", "refs": refs,
                "note": "客户原件(sources/、evidence/)不在大盘展示;要核对原文请在会话里让智能体执行 kb.py evidence " + e.id}


__all__ = ["Outline", "build", "LIST_MAX"]

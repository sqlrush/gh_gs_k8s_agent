"""索引器:<kb>/ 文件 → SQLite 索引 <kb>/index/store.sqlite(切块 + 词法 + 向量 + 节点向量)+ Neo4j(节点 + 边)。

文件是真相,两个库是派生索引(2026-10-23 起向量库是 SQLite + sqlite-vec,原高斯 / PG 向量库下线):
  · 文档按 content_hash 增量——没变的不重写、不重算向量;文件删了库里也删;
  · 图每次整体重建(MERGE 幂等,规模小),避免"改了一条边、旧边残留"的静默漂移;
  · 向量逐块 embed、失败隔离;`fill_missing` 只补缺的;覆盖率 < 100% 由调用方按退出码 2 处理;
  · 一切统计写回 IndexReport,状态行靠它,不许有"看起来成功"的默默降级。

切块规则:条款一条一块;指南按标题切、超长滑动;案例 = 摘要块 + 各小节一块(块首带 标题 › 小节),
复发标志与 signals 作为加权的信号 token 附在摘要/现场/复发标志块上;原始工单整篇按滑动切;
六类类型化条目(手册 / 缺陷 / 基线 / 产品文档 / Wiki / 画像)= 摘要块 + 各小节一块,kind 即类型代码。
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Dict, Iterable, List, Optional, Sequence, Tuple

import yaml

from . import cases as kbcases
from . import graphfiles as gf
from . import store_graph as sg
from . import store as spg
from . import text as kbtext
from .embed import Embedder

if TYPE_CHECKING:                       # 只为类型标注;运行时不引入,免得与 store_sqlite 互相 import
    from .store_sqlite import SqliteStore

CHUNK_LEN = 800
CHUNK_OVERLAP = 100
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.M)
VECTOR_NODE_KINDS = ("symptom", "rootcause", "action")
# 案例自带的边(involves 对象 / references 条款)取自案例 frontmatter:案例文件是用户在选择列表里确认后才落盘的,
# 所以记 accepted;能不能进确定性推理仍看置信度(结论强度 推测 0.6 / 待验证 0.3 的照样进不去)。
_CASE_EDGE_STATUS = sg.PATH_STATUS


@dataclass(frozen=True)
class IndexReport:
    docs_indexed: int = 0
    docs_unchanged: int = 0
    docs_removed: int = 0
    chunks_written: int = 0
    embedded: int = 0
    embed_cached: int = 0
    embed_failed: int = 0
    nodes: int = 0
    edges: int = 0
    edges_confirmed: int = 0
    chunk_total: int = 0
    chunk_embedded: int = 0
    vector_engine: str = "none"
    graph: str = "none"          # ok | unavailable | none
    embedding_configured: bool = False
    warnings: Tuple[str, ...] = ()

    @property
    def coverage_ok(self) -> bool:
        """只有**配了** embedding 却没覆盖满才算失败;故意不开向量是正常态(状态行会如实写「未启用」)。"""
        return not self.embedding_configured or self.chunk_total == self.chunk_embedded


# ---------------------------------------------------------------- chunking (pure)

def sliding(text: str, size: int = CHUNK_LEN, overlap: int = CHUNK_OVERLAP) -> List[str]:
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []
    out, start = [], 0
    while start < len(text):
        out.append(text[start:start + size])
        if start + size >= len(text):
            break
        start += size - overlap
    return out


def split_by_headings(body: str) -> List[Tuple[str, str]]:
    """[(标题面包屑, 正文)]:按 # 标题切;标题前的引言归到空标题。"""
    marks = list(_HEADING_RE.finditer(body))
    if not marks:
        return [("", body.strip())] if body.strip() else []
    out: List[Tuple[str, str]] = []
    lead = body[:marks[0].start()].strip()
    if lead:
        out.append(("", lead))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(body)
        section = body[m.end():end].strip()
        if section:
            out.append((m.group(2).strip(), section))
    return out


def _chunk(doc_id: str, seq: int, section: str, content: str,
           signal_tokens: Sequence[str] = (), embedding=None) -> spg.ChunkRow:
    return spg.ChunkRow(
        id=f"{doc_id}#{seq}", doc_id=doc_id, seq=seq, section=section, content=content,
        content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest()[:16],
        tokens=kbtext.tokenize(content), signal_tokens=tuple(signal_tokens), embedding=embedding)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def rule_docs(kb: pathlib.Path) -> List[Tuple[spg.DocRow, List[spg.ChunkRow]]]:
    """rules/*.yaml 现行条款,一条一文档一块;archive/ 不进索引(已废止不得用于判定)。"""
    out = []
    root = kb / "rules"
    if not root.is_dir():
        return out
    for path in sorted(root.rglob("*.y*ml")):
        try:
            entries = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        except (OSError, yaml.YAMLError, UnicodeDecodeError):
            continue
        if not isinstance(entries, list):
            continue
        for e in entries:
            if not isinstance(e, dict) or not e.get("id"):
                continue
            if str(e.get("status") or "active").lower() != "active":
                continue
            rid = str(e["id"])
            keywords = e.get("keywords") or []
            content = "\n".join(s for s in (
                f"{rid} {e.get('rule', '')}".strip(),
                str(e.get("rationale") or ""), str(e.get("criteria") or ""),
                " ".join(str(k) for k in keywords) if isinstance(keywords, list) else str(keywords),
            ) if s)
            doc = spg.DocRow(id=f"rule:{rid}", kind="rule", title=str(e.get("rule") or rid),
                             source=str(e.get("source") or str(path.relative_to(kb))), version="",
                             meta={"severity": e.get("severity"), "check": e.get("check"),
                                   "file": str(path.relative_to(kb)), "keywords": keywords},
                             content_hash=_hash(content))
            out.append((doc, [_chunk(doc.id, 0, "条款", content,
                                     signal_tokens=kbtext.tokenize(" ".join(map(str, keywords))))]))
    return out


def md_docs(kb: pathlib.Path, sub: str, kind: str) -> List[Tuple[spg.DocRow, List[spg.ChunkRow]]]:
    """guides/ errata/:按标题切,超长滑动。"""
    out = []
    root = kb / sub
    if not root.is_dir():
        return out
    for path in sorted(root.rglob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        meta, body, err = kbcases.split_frontmatter(text)
        if err or meta is None:
            continue
        rel = str(path.relative_to(kb))
        title = str((meta or {}).get("description") or (meta or {}).get("id") or path.stem)
        doc_id = f"{kind}:{(meta or {}).get('id') or path.stem}"
        chunks: List[spg.ChunkRow] = []
        for heading, section in split_by_headings(body):
            crumb = f"{title} › {heading}" if heading else title
            for piece in sliding(section):
                chunks.append(_chunk(doc_id, len(chunks), heading or "正文", f"{crumb}\n{piece}"))
        if not chunks:
            continue
        doc = spg.DocRow(id=doc_id, kind=kind, title=title, source=str((meta or {}).get("source") or rel),
                         version="", meta={"file": rel, "scope": (meta or {}).get("scope")},
                         content_hash=_hash(text))
        out.append((doc, chunks))
    return out


def case_docs(cases: Sequence[kbcases.Case], kb: pathlib.Path) -> List[Tuple[spg.DocRow, List[spg.ChunkRow]]]:
    out = []
    for c in cases:
        signals = list(c.signals) + ([c.section("复发标志")] if c.section("复发标志") else [])
        signal_tokens = kbtext.tokenize(" ".join(signals))
        summary = "\n".join(s for s in (
            c.title, f"系统:{c.system} 发生:{c.occurred_at} 结论强度:{c.conclusion}",
            f"主要因素:{c.primary_factor}" if c.primary_factor else "",
            f"对象:{' '.join(c.objects)}" if c.objects else "",
            f"复发标志:{' ;'.join(signals)}" if signals else "") if s)
        doc_id = f"case:{c.id}"
        chunks = [_chunk(doc_id, 0, "摘要", summary, signal_tokens=signal_tokens)]
        for name in kbcases.SECTIONS:
            body = c.section(name)
            if not body:
                continue
            sig = signal_tokens if name in ("现场", "复发标志") else ()
            for piece in sliding(body):
                chunks.append(_chunk(doc_id, len(chunks), name, f"{c.title} › {name}\n{piece}", signal_tokens=sig))
        doc = spg.DocRow(id=doc_id, kind="case", title=c.title, source=c.source, version="",
                         meta={"system": c.system, "occurred_at": c.occurred_at, "conclusion": c.conclusion,
                               "confidence": c.confidence, "severity": c.severity, "engine": c.engine,
                               "objects": list(c.objects), "signals": list(c.signals), "rules": list(c.rules),
                               "primary_factor": c.primary_factor, "file": str(c.path.relative_to(kb)),
                               "topics": list(c.topics)},
                         content_hash=c.content_hash)
        out.append((doc, chunks))
    return out


# 类型化条目进索引时带上的 frontmatter 字段:检索结果的引用行、适用性判断要用,不必再回读文件
_TYPED_META_KEYS = ("product", "versions", "deployment", "affected_versions", "fixed_in", "vendor_status",
                    "system", "metric", "unit", "metric_source", "window", "value_kind", "version", "aliases")


def typed_docs(kb: pathlib.Path) -> List[Tuple[spg.DocRow, List[spg.ChunkRow]]]:
    """六类类型化条目(wiki/ product/ runbooks/ defects/ baselines/ profiles/):摘要一块(同义词作信号),
    其余小节各一块(块首带 标题 › 小节,超长滑动)。kind = 类型代码。"""
    from . import types as kbtypes
    out = []
    items, _ = kbtypes.load_items(kb)
    for it in items:
        doc_id = f"{it.type}:{it.id}"
        signal_tokens = kbtext.tokenize(" ".join(it.synonyms))
        chunks = [_chunk(doc_id, 0, kbtypes.SUMMARY_SECTION, f"{it.title} › 摘要\n{it.summary}",
                         signal_tokens=signal_tokens)]
        for name, body in it.sections.items():
            if name == kbtypes.SUMMARY_SECTION or not body:
                continue
            for piece in sliding(body):
                chunks.append(_chunk(doc_id, len(chunks), name, f"{it.title} › {name}\n{piece}"))
        meta = {k: it.meta[k] for k in _TYPED_META_KEYS if it.meta.get(k) not in (None, "", [])}
        meta.update({"type": it.type, "topics": list(it.topics), "applies": it.applies,
                     "not_applies": it.not_applies, "verification": it.verification, "summary": it.summary,
                     "file": it.path.relative_to(kb).as_posix(),
                     "sections": {k: v for k, v in it.sections.items() if k in ("正常表现", "规避办法", "执行前提", "回退步骤")}})
        out.append((spg.DocRow(id=doc_id, kind=it.type, title=it.title, source=it.source or meta["file"],
                               version="", meta=meta, content_hash=it.content_hash), chunks))
    return out


_GENERIC_HEADING_RE = re.compile(r"^#{2,6}\s+.*$", re.M)


def raw_body(text: str) -> str:
    """原始工单进索引前:去 frontmatter、去二级以下标题(「问题描述」「处理过程」这类泛词会让
    任何带「处理」「问题」的提问都命中)、去「- 字段: 值」里的字段名只留值。"""
    _, body, err = kbcases.split_frontmatter(text)
    if err:
        body = text
    body = _GENERIC_HEADING_RE.sub("", body)
    body = re.sub(r"^- [^:\n]{1,12}: ", "", body, flags=re.M)
    return re.sub(r"\n{3,}", "\n\n", body).strip()


def raw_docs(kb: pathlib.Path) -> List[Tuple[spg.DocRow, List[spg.ChunkRow]]]:
    """inbox/<slug>/items/*.md|txt —— 已导入、未结构化的原始工单,整篇滑动切,当天可检索。"""
    out = []
    root = kb / "inbox"
    if not root.is_dir():
        return out
    for path in sorted(root.glob("*/items/*")):
        if path.suffix.lower() not in (".md", ".txt"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if not text.strip():
            continue
        slug = path.parent.parent.name
        doc_id = f"raw:{slug}/{path.stem}"
        body = raw_body(text)
        if not body:
            continue
        first = next((ln.strip().lstrip("#").strip() for ln in body.splitlines() if ln.strip()), path.stem)[:80]
        chunks = [_chunk(doc_id, i, "原文", piece) for i, piece in enumerate(sliding(body))]
        doc = spg.DocRow(id=doc_id, kind="raw", title=first, source=str(path.relative_to(kb)), version="",
                         meta={"slug": slug, "file": str(path.relative_to(kb))}, content_hash=_hash(text))
        out.append((doc, chunks))
    return out


def build_docs(kb: pathlib.Path, cases: Sequence[kbcases.Case]) -> List[Tuple[spg.DocRow, List[spg.ChunkRow]]]:
    return (rule_docs(kb) + md_docs(kb, "guides", "guide") + md_docs(kb, "errata", "errata")
            + case_docs(cases, kb) + raw_docs(kb) + typed_docs(kb))


# ---------------------------------------------------------------- graph assembly (pure)

def build_graph(cases: Sequence[kbcases.Case], triples: Sequence[gf.Triple], kb_version: str
                ) -> Tuple[List[sg.NodeRow], List[sg.EdgeRow], List[spg.NodeVectorRow]]:
    """三元组(accepted + candidate)+ 案例自带的确定性边(案例节点、involves 对象)。rejected 不入。

    每条边都带上 status:路径查询要靠它把候选边挡在外面(只看置信度的话,手工改成 1.0 的候选边会混进去)。
    """
    nodes: Dict[str, sg.NodeRow] = {}
    edges: List[sg.EdgeRow] = []
    signals_by_symptom: Dict[str, List[str]] = {}
    by_id = {c.id: c for c in cases}

    for c in cases:
        cid = f"case:{c.id}"
        nodes[cid] = sg.NodeRow(id=cid, kind="case", name=c.title,
                                attrs={"system": c.system, "occurred_at": c.occurred_at, "conclusion": c.conclusion})
        for obj in c.objects:
            kind = "guc" if re.match(r"^[a-z_]+$", obj) and not "." in obj and obj.endswith(("_threshold", "_factor", "_limit", "_timeout", "_size", "_commit", "_workers", "_naptime", "_delay", "_cost")) else "object"
            oid = gf.canonical_id(kind, obj, {})
            nodes.setdefault(oid, sg.NodeRow(id=oid, kind=kind, name=obj))
            edges.append(sg.EdgeRow(cid, "involves", oid, c.confidence, f"{c.path.name}#frontmatter", c.id,
                                    status=_CASE_EDGE_STATUS))
        for rid in c.rules:
            kid = f"clause:{rid}"
            nodes.setdefault(kid, sg.NodeRow(id=kid, kind="clause", name=rid))
            edges.append(sg.EdgeRow(cid, "references", kid, c.confidence, f"{c.path.name}#frontmatter", c.id,
                                    status=_CASE_EDGE_STATUS))

    for t in triples:
        if t.status == "rejected":
            continue
        for ref in (t.src, t.dst):
            nodes.setdefault(ref.id, sg.NodeRow(id=ref.id, kind=ref.kind, name=ref.name, canonical=ref.id))
        edges.append(sg.EdgeRow(t.src.id, t.rel, t.dst.id, t.confidence, t.source, t.case_id,
                                t.valid_from, t.valid_to, status=t.status))
        if t.rel == "exhibits" and t.dst.kind == "symptom" and t.case_id in by_id:
            c = by_id[t.case_id]
            sigs = signals_by_symptom.setdefault(t.dst.id, [])
            sigs += list(c.signals)
            if c.section("复发标志"):
                sigs.append(c.section("复发标志"))

    vectors = [spg.NodeVectorRow(node_id=n.id, kind=n.kind, name=n.name,
                                 tokens=kbtext.tokenize(n.name),
                                 signal_tokens=kbtext.tokenize(" ".join(signals_by_symptom.get(n.id, []))),
                                 signals=" ;".join(dict.fromkeys(signals_by_symptom.get(n.id, []))))
               for n in nodes.values() if n.kind in VECTOR_NODE_KINDS]
    return list(nodes.values()), edges, vectors


# ---------------------------------------------------------------- run

def _embed_texts(embedder: Optional[Embedder], texts: List[str]) -> Tuple[List[Optional[List[float]]], int, int, int]:
    if embedder is None or not texts:
        return [None] * len(texts), 0, 0, 0
    vecs = embedder.embed(texts)
    s = embedder.stats
    return vecs, s.embedded, s.cached, s.failed


def _endpoint_ok(embedder: Embedder, sample: str) -> bool:
    """端点这次通不通:先试一条。不通就不逐批硬补 —— 几万块 × 每批 30 秒超时会把一次 index 拖成几小时。
    试的这条进了缓存,接下来补的时候不重复算。"""
    try:
        return embedder.embed_one(sample) is not None
    except Exception:                                   # noqa: BLE001  端点的任何异常都只是「这次不补」
        return False


def run_index(kb: pathlib.Path, pg: "SqliteStore", graph: Optional[sg.GraphStore],
              embedder: Optional[Embedder], kb_version: str = "",
              rebuild: bool = False, fill_missing: bool = False) -> IndexReport:
    warnings: List[str] = []
    caps = pg.rebuild() if rebuild else pg.setup()
    if rebuild:
        caps = pg.capabilities()
    # 换了 embedding 模型而不重建 → 拒绝。同维度换模型是静默失效:库里混着两套向量空间,
    # 相似度失去意义,而覆盖率仍显示 100%。放在取数之前,失败得早一点。
    if caps.vector and embedder is not None:
        pg.check_embed_model(embedder.model, rebuild)
    cases, case_findings = kbcases.load_cases(kb)
    warnings += [m for lvl, m in case_findings if lvl == "error"]
    triples, tri_findings = gf.load_triples(kb, case_ids=[c.id for c in cases])
    warnings += [m for lvl, m in tri_findings if lvl == "error"]

    # --- 文档:增量 ---
    wanted = build_docs(kb, cases)
    existing = pg.doc_hashes()
    embedded = cached = failed = 0
    indexed = unchanged = 0
    chunks_written = 0
    for doc, chunks in wanted:
        if not rebuild and existing.get(doc.id) == doc.content_hash:
            unchanged += 1
            continue
        if caps.vector and embedder is not None:
            vecs, e, c, f = _embed_texts(embedder, [ch.content for ch in chunks])
            embedded += e; cached += c; failed += f
            chunks = [spg.ChunkRow(**{**ch.__dict__, "embedding": v}) for ch, v in zip(chunks, vecs)]
        pg.upsert_doc(doc, chunks)
        indexed += 1
        chunks_written += len(chunks)
    removed = 0
    wanted_ids = {doc.id for doc, _ in wanted}
    for doc_id in existing:
        if doc_id not in wanted_ids:
            pg.delete_doc(doc_id)
            removed += 1

    # 没有向量的块(之前没配 embedding、或上次端点没通)每次都补,不用记得加 --fill-missing。
    # 2026-10-24 测试环境实测:已有知识库新配上 embedding 后跑 index,文档没变就不重新切块,覆盖率停在 0/10。
    missing = pg.missing_embeddings() if caps.vector and embedder is not None else []
    if missing and (fill_missing or _endpoint_ok(embedder, missing[0][1])):
        vecs, e, c, f = _embed_texts(embedder, [m[1] for m in missing])
        embedded += e; cached += c; failed += f
        for (chunk_id, _), v in zip(missing, vecs):
            if v is not None:
                pg.set_chunk_embedding(chunk_id, v)
    elif missing:
        warnings.append(f"{len(missing)} 个块没有向量,embedding 端点这次没通,先不补(下次 index 再补,"
                        f"或 --fill-missing 强制逐批重试)")

    # --- 图:整体重建 ---
    nodes, edges, vectors = build_graph(cases, triples, kb_version)
    graph_state = "none"
    n_edges = n_conf = 0
    if graph is not None:
        try:
            graph.rebuild()
            graph.upsert_nodes(nodes, kb_version)
            n_edges = graph.upsert_edges(edges)
            n_conf = sum(1 for e in edges if sg.confirmed(e.status, e.confidence))
            graph_state = "ok"
        except sg.GraphStoreError as exc:
            graph_state = "unavailable"
            warnings.append(f"图库写入失败:{exc}")

    # --- 节点向量(放 SQLite 索引)---
    if vectors:
        if caps.vector and embedder is not None:
            vecs, e, c, f = _embed_texts(embedder, [v.name for v in vectors])
            embedded += e; cached += c; failed += f
            vectors = [spg.NodeVectorRow(**{**v.__dict__, "embedding": emb}) for v, emb in zip(vectors, vecs)]
        pg.delete_node_vectors(pg.node_ids())
        pg.upsert_node_vectors(vectors)
        if fill_missing and caps.vector and embedder is not None:    # 节点每次整批重算,这里只是逐个重试失败的
            for node_id, name in pg.missing_node_embeddings():
                v = embedder.embed_one(name)
                if v is not None:
                    pg.set_node_embedding(node_id, v)

    total, done = pg.coverage()
    vector_engine = pg.meta_get("vector_engine") or "none"
    if caps.vector and embedder is None:
        warnings.append("sqlite-vec 可用但没配 embedding(kb.yaml embeddings.source)——向量列全空,检索只走词法 + 图")
    report = IndexReport(
        docs_indexed=indexed, docs_unchanged=unchanged, docs_removed=removed, chunks_written=chunks_written,
        embedded=embedded, embed_cached=cached, embed_failed=failed,
        nodes=len(nodes), edges=n_edges, edges_confirmed=n_conf,
        chunk_total=total, chunk_embedded=done,
        vector_engine=vector_engine if embedder is not None or not caps.vector else f"{vector_engine}(未配 embedding)",
        graph=graph_state, embedding_configured=embedder is not None and caps.vector,
        warnings=tuple(warnings))
    _write_state(kb, report, kb_version)
    return report


def _write_state(kb: pathlib.Path, report: IndexReport, kb_version: str) -> None:
    state_dir = kb / "index"
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        payload = {"indexed_at": time.strftime("%Y-%m-%d %H:%M:%S"), "kb_version": kb_version,
                   **{k: v for k, v in report.__dict__.items() if k != "warnings"},
                   "warnings": list(report.warnings)}
        (state_dir / "state.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def read_state(kb: pathlib.Path) -> Optional[dict]:
    path = kb / "index" / "state.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

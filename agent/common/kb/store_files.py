"""文件模式存储 —— 还没建索引(<kb>/index/store.sqlite)或打不开时,直接在 <kb>/ 的文件上检索。

文件本来就是真相,SQLite 索引与 Neo4j 只是派生索引;这里把「派生」这一步放在内存里做:
  · 文档与切块用 indexer 同一套规则(rule_docs / md_docs / case_docs / raw_docs),同一条案例在三种模式下
    切出来的块、带的信号 token 完全一样;
  · 词法打分是 BM25(信号 token 加权),归一到 0..1,与 SQLite 索引同一套算式(bm25.py),
    query.py 的绝对/相对门槛照常生效;
  · 图用 indexer.build_graph 组出的节点与边(案例自带的 involves/references + graph/*.yaml 三元组),
    路径推理与 GraphStore.paths 的 Cypher 逐条对应:只走 accepted、confidence ≥ min 且在有效期内的边。
没有向量:capabilities().vector=False,向量检索恒空,状态行会写「向量:未启用(文件模式)」。
对外只暴露 SqliteStore / GraphStore 在检索侧用到的那一小截接口,KbSession 不感知差别。
"""
from __future__ import annotations

import pathlib
from typing import Dict, List, Optional, Sequence, Tuple

from . import cases as kbcases
from . import graphfiles as gf
from . import indexer
from . import store_graph as sg
from . import bm25
from . import store as spg

ENGINE = "files"
SIGNAL_WEIGHT = bm25.SIGNAL_WEIGHT


# ---------------------------------------------------------------- BM25

class Lexicon:
    """一组文本的 BM25 倒排。文本 = (tokens, signal_tokens);打分在 bm25.py,与 SQLite 索引同一套算式。"""

    def __init__(self, items: Sequence[Tuple[Sequence[str], Sequence[str]]]):
        self._docs: List[Tuple[int, float, Dict[str, float]]] = []
        df: Dict[str, int] = {}
        for key, (tokens, signals) in enumerate(items):
            tf, length = bm25.term_freqs(list(tokens), list(signals))
            for t in tf:
                df[t] = df.get(t, 0) + 1
            self._docs.append((key, length, tf))
        self._df = df
        self._n = len(self._docs)
        self._avg = (sum(length for _k, length, _tf in self._docs) / self._n) if self._n else 1.0

    def scores(self, query_tokens: Sequence[str]) -> List[Tuple[int, float]]:
        """[(key, 0..1 分)],只含命中的;分 = BM25 / 「每个查询 token 都饱和命中」的理想分。"""
        return bm25.scores(query_tokens, self._df, self._n, self._avg, self._docs)


# ---------------------------------------------------------------- graph

def _edge_valid(e: sg.EdgeRow, today: str, min_confidence: float) -> bool:
    # status 与置信度同一口径(sg.confirmed):手工把候选边的 confidence 改成 1.0,它也不进路径
    return (sg.confirmed(e.status, e.confidence, min_confidence)
            and (not e.valid_to or e.valid_to > today)
            and (not e.valid_from or e.valid_from <= today))


class FileGraph:
    """graph/*.yaml + 案例自带边 组成的内存图;接口对齐 GraphStore 的检索侧。"""

    def __init__(self, nodes: Sequence[sg.NodeRow], edges: Sequence[sg.EdgeRow]):
        self._nodes: Dict[str, sg.NodeRow] = {n.id: n for n in nodes}
        self._edges: Tuple[sg.EdgeRow, ...] = tuple(edges)
        by_src: Dict[str, List[sg.EdgeRow]] = {}
        for e in edges:
            by_src.setdefault(e.src, []).append(e)
        self._by_src = by_src

    def __repr__(self) -> str:
        return f"FileGraph(nodes={len(self._nodes)}, edges={len(self._edges)})"

    def ping(self) -> str:
        return f"graph/*.yaml · 节点 {len(self._nodes)} · 边 {len(self._edges)}"

    def node(self, node_id: str) -> Optional[Dict[str, object]]:
        n = self._nodes.get(node_id)
        if n is None:
            return None
        return {"id": n.id, "kind": n.kind, "name": n.name, "canonical": n.canonical or n.id,
                "aliases": list(n.aliases)}

    def paths(self, symptom_ids: Sequence[str], today: str, min_confidence: float = 1.0,
              limit: int = 20) -> List[sg.PathHit]:
        """现象 → 根因 → 处置 的完整链,按三元组聚合:来源合并、案例去重;与 GraphStore.paths 同序。"""
        agg: Dict[Tuple[str, str, str], dict] = {}
        for sid in symptom_ids:
            s = self._nodes.get(sid)
            if s is None or s.kind != "symptom":
                continue
            for e1 in self._by_src.get(sid, ()):
                if e1.rel != "caused_by" or not _edge_valid(e1, today, min_confidence):
                    continue
                r = self._nodes.get(e1.dst)
                if r is None or r.kind != "rootcause":
                    continue
                for e2 in self._by_src.get(r.id, ()):
                    if e2.rel != "handled_by" or not _edge_valid(e2, today, min_confidence):
                        continue
                    a = self._nodes.get(e2.dst)
                    if a is None or a.kind != "action":
                        continue
                    key = (s.id, r.id, a.id)
                    cur = agg.get(key) or {"s": s, "r": r, "a": a, "s1": [], "s2": [], "cases": [], "minc": 1.0}
                    cur = {**cur,
                           "s1": list(dict.fromkeys(cur["s1"] + [e1.source])),
                           "s2": list(dict.fromkeys(cur["s2"] + [e2.source])),
                           "cases": list(dict.fromkeys(cur["cases"] + [c for c in (e1.case_id, e2.case_id) if c])),
                           "minc": min(cur["minc"], e1.confidence, e2.confidence)}
                    agg[key] = cur
        ranked = sorted(agg.items(), key=lambda kv: (-len(kv[1]["cases"]), kv[0][0], kv[0][1], kv[0][2]))
        return [sg.PathHit(symptom_id=v["s"].id, symptom=v["s"].name, rootcause_id=v["r"].id, rootcause=v["r"].name,
                           action_id=v["a"].id, action=v["a"].name, cases=tuple(v["cases"]),
                           sources=tuple(v["s1"] + v["s2"]), min_confidence=float(v["minc"]))
                for _k, v in ranked[:int(limit)]]

    def clauses_for(self, node_ids: Sequence[str], min_confidence: float = 1.0) -> Dict[str, List[str]]:
        """对象/GUC → 约束它的条款 id(CONSTRAINS)。"""
        wanted = set(node_ids)
        out: Dict[str, List[str]] = {}
        for e in self._edges:
            if e.rel != "constrains" or e.dst not in wanted or not sg.confirmed(e.status, e.confidence, min_confidence):
                continue
            k = self._nodes.get(e.src)
            if k is None or k.kind != "clause":
                continue
            if e.src not in out.get(e.dst, []):
                out[e.dst] = out.get(e.dst, []) + [e.src]
        return out

    def counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for n in self._nodes.values():
            out[f"nodes.{n.kind}"] = out.get(f"nodes.{n.kind}", 0) + 1
        for e in self._edges:
            out[f"edges.{e.rel}"] = out.get(f"edges.{e.rel}", 0) + 1
            out.setdefault(f"edges.{e.rel}.confirmed", 0)
            if sg.confirmed(e.status, e.confidence):
                out[f"edges.{e.rel}.confirmed"] += 1
        out["edges"] = len(self._edges)
        out["edges.confirmed"] = sum(1 for e in self._edges if sg.confirmed(e.status, e.confidence))
        return out


# ---------------------------------------------------------------- loading

def _load_tree(kb: pathlib.Path):
    """cases + triples + 发现(只留 error 级,作为 warnings 报出来)。"""
    cases, case_findings = kbcases.load_cases(kb)
    triples, tri_findings = gf.load_triples(kb, case_ids=[c.id for c in cases])
    warnings = tuple(m for lvl, m in list(case_findings) + list(tri_findings) if lvl == "error")
    return cases, triples, warnings


def _version(kb: pathlib.Path) -> str:
    try:
        return (kb / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def load_graph(kb: pathlib.Path) -> FileGraph:
    """只要图(向量库在、Neo4j 不在时用)。"""
    cases, triples, _ = _load_tree(kb)
    nodes, edges, _vectors = indexer.build_graph(cases, triples, _version(kb))
    return FileGraph(nodes, edges)


class FileStore:
    """接口对齐 SqliteStore 的检索侧;`graph` 属性是同一棵文件树组出的 FileGraph。"""

    def __init__(self, docs: Sequence[Tuple[spg.DocRow, List[spg.ChunkRow]]],
                 vectors: Sequence[spg.NodeVectorRow], graph: FileGraph, warnings: Sequence[str] = ()):
        self._docs: Dict[str, spg.DocRow] = {d.id: d for d, _ in docs}
        self._chunks: List[Tuple[spg.ChunkRow, spg.DocRow]] = [(ch, d) for d, chunks in docs for ch in chunks]
        self._chunk_lex = Lexicon([(ch.tokens, ch.signal_tokens) for ch, _ in self._chunks])
        self._vectors: List[spg.NodeVectorRow] = list(vectors)
        self._node_lex = Lexicon([(v.tokens, v.signal_tokens) for v in self._vectors])
        self.graph = graph
        self.warnings: Tuple[str, ...] = tuple(warnings)

    @classmethod
    def load(cls, kb: pathlib.Path) -> "FileStore":
        kb = pathlib.Path(kb)
        cases, triples, warnings = _load_tree(kb)
        docs = indexer.build_docs(kb, cases)
        nodes, edges, vectors = indexer.build_graph(cases, triples, _version(kb))
        return cls(docs, vectors, FileGraph(nodes, edges), warnings)

    def __repr__(self) -> str:
        return f"FileStore(docs={len(self._docs)}, chunks={len(self._chunks)})"

    # --- 形状 -----------------------------------------------------------------

    def close(self) -> None:
        return None

    def has_index(self) -> bool:
        return bool(self._docs)

    def capabilities(self) -> spg.Capabilities:
        return spg.Capabilities(engine=ENGINE, version="", vector=False, hnsw=False, dims=0)

    def meta_get(self, key: str) -> Optional[str]:
        return None

    def coverage(self) -> Tuple[int, int]:
        return len(self._chunks), 0

    def counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for d in self._docs.values():
            out[f"docs.{d.kind}"] = out.get(f"docs.{d.kind}", 0) + 1
        out["chunks"] = len(self._chunks)
        out["nodes"] = len(self._vectors)
        return out

    def doc_hashes(self) -> Dict[str, str]:
        return {d.id: d.content_hash for d in self._docs.values()}

    # --- 检索 -----------------------------------------------------------------

    def search_chunks_lexical(self, query_tokens: Sequence[str], k: int = 10,
                              kinds: Optional[Sequence[str]] = None) -> List[spg.Hit]:
        allowed = set(kinds) if kinds else None
        scored = [(key, s) for key, s in self._chunk_lex.scores(query_tokens)
                  if allowed is None or self._chunks[key][1].kind in allowed]
        scored.sort(key=lambda p: (-p[1], self._chunks[p[0]][0].doc_id, self._chunks[p[0]][0].seq))
        return [self._chunk_hit(key, s) for key, s in scored[:int(k)]]

    def search_chunks_vector(self, embedding: Sequence[float], k: int = 10,
                             kinds: Optional[Sequence[str]] = None) -> List[spg.Hit]:
        return []

    def search_nodes_lexical(self, query_tokens: Sequence[str], k: int = 10,
                             kinds: Optional[Sequence[str]] = None) -> List[spg.Hit]:
        allowed = set(kinds) if kinds else None
        scored = [(key, s) for key, s in self._node_lex.scores(query_tokens)
                  if allowed is None or self._vectors[key].kind in allowed]
        scored.sort(key=lambda p: (-p[1], self._vectors[p[0]].node_id))
        return [spg.Hit(id=v.node_id, kind=v.kind, title=v.name, score=float(s), content=v.signals or "")
                for v, s in ((self._vectors[key], s) for key, s in scored[:int(k)])]

    def search_nodes_vector(self, embedding: Sequence[float], k: int = 10,
                            kinds: Optional[Sequence[str]] = None) -> List[spg.Hit]:
        return []

    def _chunk_hit(self, key: int, score: float) -> spg.Hit:
        ch, d = self._chunks[key]
        return spg.Hit(id=ch.id, doc_id=ch.doc_id, seq=int(ch.seq), section=ch.section or "",
                       content=ch.content or "", kind=d.kind, title=d.title, source=d.source,
                       meta=dict(d.meta), score=float(score))

    # --- 文档读取 ---------------------------------------------------------------

    def docs_by_ids(self, ids: Sequence[str]) -> Dict[str, spg.DocRow]:
        return {i: self._docs[i] for i in ids if i in self._docs}

    def doc_sections(self, doc_id: str) -> Dict[str, str]:
        """section → 正文(去掉面包屑首行);与 SqliteStore.doc_sections 同规则。"""
        out: Dict[str, str] = {}
        for ch, _d in self._chunks:
            if ch.doc_id != doc_id:
                continue
            body = ch.content or ""
            if "\n" in body and " › " in body.split("\n", 1)[0]:
                body = body.split("\n", 1)[1]
            out.setdefault(ch.section or "", body)
        return out

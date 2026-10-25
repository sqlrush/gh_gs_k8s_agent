"""common.kb.indexer —— 切块/组图纯函数单测 + 端到端索引(SQLite 索引不用服务;配了 KB_TEST_NEO4J 再连真图库)。"""
import os
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common.kb import cases as kbcases, graphfiles as gf, indexer, store_graph as sg, store_sqlite, text as kbtext  # noqa: E402

CASE_ID = "S1-20250224-CBST-偶现单条update慢"
CASE = f"""---
id: {CASE_ID}
title: 偶现单条 update 走索引耗时 3s
system: CBST
occurred_at: 2025-02-24
engine: gaussdb
severity: S1
primary_factor: autovacuum 尾部空页回收持 8 级锁与 DML 互相 cancel
conclusion: 已确认
source: sources/report.v1.docx#前言
objects: [cbst.cosp_asyn_task_dtl, autovacuum_vacuum_threshold]
signals: [单条 update 偶发秒级, autovacuum 频繁触发]
rules: [GS-VAC-002]
---
## 现场
业务偶现单条 update 走索引执行耗时 3s。
## 判断
autovacuum 检测到表尾部空页时触发 page 回收,持 8 级锁,与 DML 互相 cancel。
## 处置
针对 cbst.cosp_asyn_task_dtl 小表调大 autovacuum_vacuum_threshold。
## 复发标志
单条 update 偶发 3s 且该表 autovacuum 次数异常高。
"""
TRIPLES = f"""
- src: {{kind: case, name: 偶现单条update慢, canonical: "case:{CASE_ID}"}}
  rel: exhibits
  dst: {{kind: symptom, name: 单条 update 偶发秒级}}
  confidence: 1.0
  source: cases/{CASE_ID}.md#现场
  case: {CASE_ID}
- src: {{kind: symptom, name: 单条 update 偶发秒级}}
  rel: caused_by
  dst: {{kind: rootcause, name: autovacuum 尾部回收持 8 级锁}}
  confidence: 1.0
  source: cases/{CASE_ID}.md#判断
  case: {CASE_ID}
- src: {{kind: rootcause, name: autovacuum 尾部回收持 8 级锁}}
  rel: handled_by
  dst: {{kind: action, name: 表级调大 autovacuum_vacuum_threshold}}
  confidence: 1.0
  source: cases/{CASE_ID}.md#处置
  case: {CASE_ID}
"""
RULES = """# vacuum 规范
- id: GS-VAC-002
  severity: warn
  check: advisory
  rule: 行数小于 1 万的热表按表级调大 autovacuum_vacuum_threshold
  criteria: 看 pg_stat_user_tables 的 autovacuum_count 与 n_live_tup
  keywords: [小表, autovacuum 阈值, autovacuum_vacuum_threshold]
  source: 《运维规范》v5 §6.2
- id: GS-VAC-009
  severity: info
  check: advisory
  rule: 已废止的条款
  status: deprecated
"""
GUIDE = """---
id: guide-vacuum
description: vacuum 调优指南
---
## 何时调大阈值
小表频繁更新时调大阈值。

## 何时不该动
大表按默认。
"""


def _fixture_kb(tmp_path: pathlib.Path) -> pathlib.Path:
    for sub in ("cases", "graph", "rules", "guides", "inbox/tickets-q1/items"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    (tmp_path / "cases" / f"{CASE_ID}.md").write_text(CASE, encoding="utf-8")
    (tmp_path / "graph" / "cbst.yaml").write_text(TRIPLES, encoding="utf-8")
    (tmp_path / "rules" / "vacuum.yaml").write_text(RULES, encoding="utf-8")
    (tmp_path / "guides" / "vacuum.md").write_text(GUIDE, encoding="utf-8")
    (tmp_path / "inbox" / "tickets-q1" / "items" / "T-100.md").write_text(
        "工单 T-100:分区表 exchange 后统计信息未更新导致计划跳变。\n" * 3, encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------- pure

def test_sliding_window():
    assert indexer.sliding("", 10, 2) == []
    assert indexer.sliding("abc", 10, 2) == ["abc"]
    pieces = indexer.sliding("a" * 25, 10, 2)
    assert pieces[0] == "a" * 10 and len(pieces) == 3
    assert all(len(p) <= 10 for p in pieces)


def test_split_by_headings_keeps_lead_and_sections():
    parts = indexer.split_by_headings("引言\n## A\n正文A\n### B\n正文B\n")
    assert parts == [("", "引言"), ("A", "正文A"), ("B", "正文B")]


def test_build_docs_covers_every_kind(tmp_path):
    kb = _fixture_kb(tmp_path)
    cases, _ = kbcases.load_cases(kb)
    docs = indexer.build_docs(kb, cases)
    kinds = {d.kind for d, _ in docs}
    assert kinds == {"rule", "guide", "case", "raw"}
    ids = {d.id for d, _ in docs}
    assert "rule:GS-VAC-002" in ids and "rule:GS-VAC-009" not in ids     # 废止条款不进索引
    case_chunks = next(ch for d, ch in docs if d.id == f"case:{CASE_ID}")
    assert [c.section for c in case_chunks] == ["摘要", "现场", "判断", "处置", "复发标志"]
    assert case_chunks[1].content.startswith("偶现单条 update 走索引耗时 3s › 现场")
    assert case_chunks[0].signal_tokens and case_chunks[2].signal_tokens == ()   # 判断块不带信号
    guide_chunks = next(ch for d, ch in docs if d.id == "guide:guide-vacuum")
    assert len(guide_chunks) == 2 and "何时调大阈值" in guide_chunks[0].content
    rule_chunk = next(ch for d, ch in docs if d.id == "rule:GS-VAC-002")[0]
    assert "autovacuum_vacuum_threshold" in rule_chunk.signal_tokens


def test_build_graph_adds_case_nodes_and_involves_edges(tmp_path):
    kb = _fixture_kb(tmp_path)
    cases, _ = kbcases.load_cases(kb)
    triples, _ = gf.load_triples(kb, [c.id for c in cases])
    nodes, edges, vectors = indexer.build_graph(cases, triples, "2026.09")
    by_id = {n.id: n for n in nodes}
    assert f"case:{CASE_ID}" in by_id and by_id[f"case:{CASE_ID}"].kind == "case"
    assert "object:cbst.cosp_asyn_task_dtl" in by_id and by_id["object:cbst.cosp_asyn_task_dtl"].kind == "object"
    assert "guc:autovacuum_vacuum_threshold" in by_id
    assert "clause:GS-VAC-002" in by_id
    rels = {(e.src, e.rel, e.dst) for e in edges}
    assert (f"case:{CASE_ID}", "involves", "object:cbst.cosp_asyn_task_dtl") in rels
    assert (f"case:{CASE_ID}", "references", "clause:GS-VAC-002") in rels
    assert any(e.rel == "handled_by" for e in edges)
    sym = next(v for v in vectors if v.kind == "symptom")
    assert "autovacuum" in sym.signal_tokens                    # 复发标志进了现象节点的信号
    assert {v.kind for v in vectors} <= set(indexer.VECTOR_NODE_KINDS)


def test_build_graph_drops_rejected(tmp_path):
    kb = _fixture_kb(tmp_path)
    (kb / "graph" / "cbst.yaml").write_text(TRIPLES.replace("confidence: 1.0\n  source: cases/" + CASE_ID + ".md#处置",
                                                            "confidence: 1.0\n  status: rejected\n  source: cases/" + CASE_ID + ".md#处置"),
                                            encoding="utf-8")
    cases, _ = kbcases.load_cases(kb)
    triples, _ = gf.load_triples(kb, [c.id for c in cases])
    _, edges, _ = indexer.build_graph(cases, triples, "")
    assert not any(e.rel == "handled_by" for e in edges)


def test_build_graph_carries_status_onto_edges(tmp_path):
    """status 必须随边进图:路径查询靠它挡候选边。手工把候选边写成 1.0 时,置信度已经区分不出来了。"""
    kb = _fixture_kb(tmp_path)
    (kb / "graph" / "cbst.yaml").write_text(TRIPLES.replace("confidence: 1.0\n  source: cases/" + CASE_ID + ".md#处置",
                                                            "confidence: 1.0\n  status: candidate\n  source: cases/" + CASE_ID + ".md#处置"),
                                            encoding="utf-8")
    cases, _ = kbcases.load_cases(kb)
    triples, _ = gf.load_triples(kb, [c.id for c in cases])
    _, edges, _ = indexer.build_graph(cases, triples, "")
    by_rel = {e.rel: e for e in edges}
    assert by_rel["handled_by"].status == "candidate" and by_rel["handled_by"].confidence == 1.0
    assert by_rel["caused_by"].status == "accepted"                  # 没写 status 的三元组按 accepted(文件格式约定)
    assert by_rel["involves"].status == "accepted" and by_rel["references"].status == "accepted"   # 案例自带的边
    assert not sg.confirmed(by_rel["handled_by"].status, by_rel["handled_by"].confidence)


# ---------------------------------------------------------------- live

class _FakeEmbedder:
    """确定性向量:按文本哈希;'现场' 相关的文本靠近 [1,0,0,0]。"""
    model = "fake-4d"

    def __init__(self):
        from common.kb.embed import EmbedStats
        self.stats = EmbedStats()

    def embed(self, texts, timeout_s=None):
        from common.kb.embed import EmbedStats
        out = []
        for t in texts:
            out.append([1.0, 0.0, 0.0, 0.0] if "update" in t else [0.0, 1.0, 0.0, 0.0])
        self.stats = EmbedStats(requested=len(texts), embedded=len(texts))
        return out

    def embed_one(self, text, timeout_s=None):
        return self.embed([text])[0]


def test_run_index_end_to_end(tmp_path):
    """SQLite 索引端到端(2026-10-23 起取代高斯 / PG 向量库,不需要服务);配了 KB_TEST_NEO4J 再连真图库验路径。"""
    pytest.importorskip("sqlite_vec", reason="没装 sqlite-vec,向量覆盖率断言没法做")
    gspec = os.environ.get("KB_TEST_NEO4J")
    graph = None
    if gspec:
        url, guser, gpw = gspec.split("|", 2)
        graph = sg.GraphStore(url, guser, gpw)
    pg = store_sqlite.SqliteStore.open_writer(tmp_path / "store.sqlite", dims=4)
    kb = _fixture_kb(tmp_path)
    try:
        rep = indexer.run_index(kb, pg, graph, _FakeEmbedder(), kb_version="2026.09", rebuild=True)
        assert rep.docs_indexed == 4 and rep.docs_removed == 0
        if graph is not None:
            assert rep.graph == "ok" and rep.edges >= 5 and rep.edges_confirmed == rep.edges
        else:
            assert rep.graph == "none"
        assert rep.coverage_ok and rep.chunk_total == rep.chunk_embedded > 0
        assert rep.vector_engine == "sqlite-vec"
        assert not rep.warnings, rep.warnings
        assert indexer.read_state(kb)["docs_indexed"] == 4

        # 词法:按发现里的对象名命中案例
        hits = pg.search_chunks_lexical(kbtext.query_tokens("cbst.cosp_asyn_task_dtl"), kinds=["case"])
        assert hits and hits[0].doc_id == f"case:{CASE_ID}"
        # 向量:靠近 [1,0,0,0] 的是含 update 的块
        vhits = pg.search_chunks_vector([1, 0, 0, 0], k=3)
        assert all("update" in h.content for h in vhits)
        # 节点向量 + 图路径
        nodes = pg.search_nodes_lexical(kbtext.query_tokens("autovacuum 频繁触发"), kinds=["symptom"])
        assert nodes and nodes[0].id.startswith("symptom:")
        assert pg.search_nodes_vector([1, 0, 0, 0], k=1), "节点向量也要写进去"
        if graph is not None:
            paths = graph.paths([nodes[0].id], today="2026-09-02")
            assert len(paths) == 1 and paths[0].action.startswith("表级调大") and paths[0].cases

        # 增量:什么都没改 → 全部 unchanged
        rep2 = indexer.run_index(kb, pg, graph, _FakeEmbedder(), kb_version="2026.09")
        assert rep2.docs_indexed == 0 and rep2.docs_unchanged == 4
        # 删一个文件 → 库里也删
        (kb / "inbox" / "tickets-q1" / "items" / "T-100.md").unlink()
        rep3 = indexer.run_index(kb, pg, graph, _FakeEmbedder(), kb_version="2026.09")
        assert rep3.docs_removed == 1
        assert "docs.raw" not in pg.counts()
        # 没配 embedding → 覆盖率为 0、有告警,但这是"故意不开向量",不算失败(状态行会写未启用)
        rep4 = indexer.run_index(kb, pg, graph, None, kb_version="2026.09", rebuild=True)
        assert rep4.chunk_embedded == 0 and rep4.coverage_ok and not rep4.embedding_configured
        assert any("embedding" in w for w in rep4.warnings)

        # 配了 embedding 却全失败 → 才是 coverage 失败
        class _Broken(_FakeEmbedder):
            def embed(self, texts, timeout_s=None):
                from common.kb.embed import EmbedStats
                self.stats = EmbedStats(requested=len(texts), failed=len(texts), last_error="boom")
                return [None] * len(texts)
        rep5 = indexer.run_index(kb, pg, graph, _Broken(), kb_version="2026.09", rebuild=True)
        assert rep5.embedding_configured and not rep5.coverage_ok and rep5.embed_failed > 0
    finally:
        pg.close()
        if graph is not None:
            graph.rebuild()

"""common.kb.store_files —— 文件模式存储:没配 / 连不上高斯·PG 与 Neo4j 时,直接在 <kb>/ 文件上做
词法检索与 现象→根因→处置 路径推理。接口与 SqliteStore / GraphStore 检索侧一致,KbSession 不感知差别。"""
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common.kb import store as spg, store_files as sf, text as kbtext  # noqa: E402

CASE_ID = "S1-20250224-CBST-偶现单条update慢"
CASE2_ID = "S2-20250420-CBMS-ProcArrayLock等待"
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
CASE2 = f"""---
id: {CASE2_ID}
title: 高并发下 ProcArrayLock 等待
system: CBMS
occurred_at: 2025-04-20
conclusion: 推测
source: sources/t2.md#1
objects: [cbms.acct_bal]
signals: [ProcArrayLock 等待占比高]
---
## 现场
高峰期 ProcArrayLock 等待占 DB time 40%。
## 判断
连接数过高。
## 处置
降低并发连接数。
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
# 第二份材料佐证同一条链:sources 合并、案例去重
- src: {{kind: symptom, name: 单条 update 偶发秒级}}
  rel: caused_by
  dst: {{kind: rootcause, name: autovacuum 尾部回收持 8 级锁}}
  confidence: 1.0
  source: sources/report.v1.docx#结论
  case: {CASE_ID}
# 候选边:不进路径
- src: {{kind: symptom, name: 单条 update 偶发秒级}}
  rel: caused_by
  dst: {{kind: rootcause, name: 网络抖动}}
  confidence: 0.6
  status: candidate
  source: cases/{CASE_ID}.md#判断
  case: {CASE_ID}
- src: {{kind: rootcause, name: 网络抖动}}
  rel: handled_by
  dst: {{kind: action, name: 换网线}}
  confidence: 1.0
  source: cases/{CASE_ID}.md#处置
  case: {CASE_ID}
# 已过期的边:valid_to 早于今天,不进路径
- src: {{kind: symptom, name: 单条 update 偶发秒级}}
  rel: caused_by
  dst: {{kind: rootcause, name: 旧版本 bug}}
  confidence: 1.0
  valid_to: 2025-12-31
  source: cases/{CASE_ID}.md#判断
  case: {CASE_ID}
- src: {{kind: rootcause, name: 旧版本 bug}}
  rel: handled_by
  dst: {{kind: action, name: 升级内核}}
  confidence: 1.0
  source: cases/{CASE_ID}.md#处置
  case: {CASE_ID}
# 条款约束对象
- src: {{kind: clause, name: GS-VAC-002, canonical: "clause:GS-VAC-002"}}
  rel: constrains
  dst: {{kind: object, name: cbst.cosp_asyn_task_dtl}}
  confidence: 1.0
  source: rules/vacuum.yaml
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
  rule: 已废止的条款 autovacuum
  status: deprecated
"""
GUIDE = """---
id: guide-vacuum
description: vacuum 调优指南
---
## 何时调大阈值
小表频繁更新时调大 autovacuum 阈值。
"""


def _kb(tmp_path: pathlib.Path) -> pathlib.Path:
    for sub in ("cases", "graph", "rules", "guides", "inbox/tickets-q1/items"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    (tmp_path / "VERSION").write_text("2026.09\n", encoding="utf-8")
    (tmp_path / "cases" / f"{CASE_ID}.md").write_text(CASE, encoding="utf-8")
    (tmp_path / "cases" / f"{CASE2_ID}.md").write_text(CASE2, encoding="utf-8")
    (tmp_path / "graph" / "cbst.yaml").write_text(TRIPLES, encoding="utf-8")
    (tmp_path / "rules" / "vacuum.yaml").write_text(RULES, encoding="utf-8")
    (tmp_path / "guides" / "vacuum.md").write_text(GUIDE, encoding="utf-8")
    (tmp_path / "inbox" / "tickets-q1" / "items" / "T-100.md").write_text(
        "工单 T-100:分区表 exchange 后统计信息未更新导致计划跳变。\n", encoding="utf-8")
    return tmp_path


def _q(text: str):
    return kbtext.query_tokens(text)


# ---------------------------------------------------------------- FileStore:形状

def test_file_store_has_pg_store_shape(tmp_path):
    fs = sf.FileStore.load(_kb(tmp_path))
    assert fs.has_index()
    caps = fs.capabilities()
    assert isinstance(caps, spg.Capabilities) and caps.engine == "files" and caps.vector is False
    assert fs.meta_get("vector_engine") is None
    assert fs.coverage()[1] == 0 and fs.coverage()[0] > 0
    c = fs.counts()
    assert c["docs.case"] == 2 and c["docs.rule"] == 1 and c["docs.guide"] == 1 and c["docs.raw"] == 1
    assert c["chunks"] == fs.coverage()[0] and c["nodes"] > 0
    fs.close()                                             # 无库可关,但接口要在


def test_empty_kb_has_no_index(tmp_path):
    (tmp_path / "rules").mkdir()
    fs = sf.FileStore.load(tmp_path)
    assert not fs.has_index() and fs.counts() == {"chunks": 0, "nodes": 0}
    assert fs.search_chunks_lexical(_q("autovacuum")) == []


# ---------------------------------------------------------------- FileStore:词法检索

def test_lexical_search_hits_case_by_strong_token(tmp_path):
    fs = sf.FileStore.load(_kb(tmp_path))
    hits = fs.search_chunks_lexical(_q("cbst.cosp_asyn_task_dtl autovacuum 次数异常高"), k=30)
    assert hits and hits[0].doc_id == f"case:{CASE_ID}"
    h = hits[0]
    assert isinstance(h, spg.Hit) and h.kind == "case" and h.id.startswith(h.doc_id + "#")
    assert 0.0 < h.score <= 1.0
    assert h.meta.get("conclusion") == "已确认" and h.meta.get("occurred_at") == "2025-02-24"
    assert h.title == "偶现单条 update 走索引耗时 3s" and h.source == "sources/report.v1.docx#前言"
    # 同一查询同时命中条款(第二类):文件模式不是"只搜案例"
    assert any(x.doc_id == "rule:GS-VAC-002" for x in hits)
    assert not any(x.doc_id == "rule:GS-VAC-009" for x in hits)       # 已废止条款不进检索


def test_lexical_search_orders_by_relevance_and_is_deterministic(tmp_path):
    fs = sf.FileStore.load(_kb(tmp_path))
    a = fs.search_chunks_lexical(_q("ProcArrayLock 等待占比高"), k=5)
    b = fs.search_chunks_lexical(_q("ProcArrayLock 等待占比高"), k=5)
    assert a == b
    assert a and a[0].doc_id == f"case:{CASE2_ID}"
    assert all(a[i].score >= a[i + 1].score for i in range(len(a) - 1))


def test_lexical_search_respects_k_and_kinds(tmp_path):
    fs = sf.FileStore.load(_kb(tmp_path))
    only_rules = fs.search_chunks_lexical(_q("autovacuum 阈值"), k=10, kinds=["rule"])
    assert only_rules and {h.kind for h in only_rules} == {"rule"}
    assert len(fs.search_chunks_lexical(_q("autovacuum"), k=1)) == 1
    assert fs.search_chunks_lexical([], k=10) == []
    assert fs.search_chunks_lexical(_q("完全无关的词汇 zzz"), k=10) == []


def test_signal_tokens_boost_the_chunk_that_carries_them(tmp_path):
    """复发标志(signals)是权重 A 的信号:查询词落在信号里的块要排在只在正文里出现的块前面。"""
    fs = sf.FileStore.load(_kb(tmp_path))
    hits = fs.search_chunks_lexical(_q("autovacuum 频繁触发"), k=10, kinds=["case"])
    assert hits and hits[0].section in ("摘要", "现场", "复发标志")


def test_vector_search_is_empty_in_file_mode(tmp_path):
    fs = sf.FileStore.load(_kb(tmp_path))
    assert fs.search_chunks_vector([1, 0, 0, 0], k=5) == []
    assert fs.search_nodes_vector([1, 0, 0, 0], k=5) == []


# ---------------------------------------------------------------- FileStore:文档读取

def test_doc_sections_strip_breadcrumb(tmp_path):
    fs = sf.FileStore.load(_kb(tmp_path))
    secs = fs.doc_sections(f"case:{CASE_ID}")
    assert secs["处置"].startswith("针对 cbst.cosp_asyn_task_dtl")
    assert "›" not in secs["处置"] and "复发标志" in secs
    assert fs.doc_sections("case:不存在") == {}


def test_docs_by_ids_returns_doc_rows(tmp_path):
    fs = sf.FileStore.load(_kb(tmp_path))
    docs = fs.docs_by_ids(["rule:GS-VAC-002", "rule:nope"])
    assert list(docs) == ["rule:GS-VAC-002"]
    d = docs["rule:GS-VAC-002"]
    assert isinstance(d, spg.DocRow) and d.kind == "rule" and d.source == "《运维规范》v5 §6.2"
    assert d.meta["severity"] == "warn"
    assert fs.docs_by_ids([]) == {}


# ---------------------------------------------------------------- FileStore:节点

def test_node_search_finds_symptom_by_name_or_signals(tmp_path):
    fs = sf.FileStore.load(_kb(tmp_path))
    by_name = fs.search_nodes_lexical(_q("单条 update 偶发秒级"), k=10, kinds=["symptom"])
    assert by_name and by_name[0].id == "symptom:单条_update_偶发秒级" and by_name[0].kind == "symptom"
    assert by_name[0].title == "单条 update 偶发秒级"
    by_signal = fs.search_nodes_lexical(_q("autovacuum 次数异常高"), k=10, kinds=["symptom"])
    assert by_signal and by_signal[0].id == by_name[0].id
    assert "autovacuum" in by_signal[0].content                        # content = 复发标志原文
    assert fs.search_nodes_lexical(_q("单条 update 偶发秒级"), k=10, kinds=["action"]) == []


# ---------------------------------------------------------------- FileGraph

def test_file_graph_paths_walk_confirmed_edges_only(tmp_path):
    g = sf.FileStore.load(_kb(tmp_path)).graph
    assert g.ping()
    paths = g.paths(["symptom:单条_update_偶发秒级"], today="2026-09-05")
    assert len(paths) == 1                                           # 候选边、过期边都不走
    p = paths[0]
    assert p.symptom == "单条 update 偶发秒级" and p.rootcause.startswith("autovacuum") and p.action.startswith("表级调大")
    assert p.cases == (CASE_ID,)                                     # 两条佐证边同一案例:去重(边上存裸 ID,与 Neo4j 一致)
    assert set(p.sources) == {f"cases/{CASE_ID}.md#判断", "sources/report.v1.docx#结论", f"cases/{CASE_ID}.md#处置"}
    assert p.min_confidence == 1.0
    assert g.paths([], today="2026-09-05") == []
    assert g.paths(["symptom:不存在"], today="2026-09-05") == []


def test_file_graph_paths_honor_validity_window(tmp_path):
    g = sf.FileStore.load(_kb(tmp_path)).graph
    paths = g.paths(["symptom:单条_update_偶发秒级"], today="2025-06-01")   # 旧版本 bug 那条边当时还生效
    assert {p.rootcause for p in paths} == {"autovacuum 尾部回收持 8 级锁", "旧版本 bug"}
    assert paths[0].rootcause.startswith("autovacuum")               # 案例支持数相同时按 id 稳定排序


def test_file_graph_paths_lower_min_confidence_still_excludes_candidates(tmp_path):
    """调低置信度门槛只放宽 accepted 边,候选边无论置信度多少都不进路径。"""
    g = sf.FileStore.load(_kb(tmp_path)).graph
    paths = g.paths(["symptom:单条_update_偶发秒级"], today="2026-09-05", min_confidence=0.5)
    assert paths and all(p.rootcause != "网络抖动" for p in paths)


def _set_candidate_edge(kb: pathlib.Path, confidence: str, status: str) -> None:
    """把夹具里那条 现象 -caused_by-> 网络抖动 的候选边改成给定的置信度与状态(模拟手工改 graph/*.yaml)。"""
    path = kb / "graph" / "cbst.yaml"
    text = path.read_text(encoding="utf-8")
    old = "  confidence: 0.6\n  status: candidate\n"
    assert text.count(old) == 1
    path.write_text(text.replace(old, f"  confidence: {confidence}\n  status: {status}\n"), encoding="utf-8")


def test_hand_edited_candidate_at_full_confidence_stays_out_of_paths(tmp_path):
    """手工把候选边的 confidence 写成 1.0:status 仍是 candidate,不得混进「本行处置路径」,
    也不算进状态行的「已确认边」;同一现象下 accepted 且 1.0 的链照常出现。"""
    kb = _kb(tmp_path)
    _set_candidate_edge(kb, "1.0", "candidate")
    g = sf.FileStore.load(kb).graph
    paths = g.paths(["symptom:单条_update_偶发秒级"], today="2026-09-05")
    assert [p.rootcause for p in paths] == ["autovacuum 尾部回收持 8 级锁"]
    assert all(p.rootcause != "网络抖动" for p in g.paths(["symptom:单条_update_偶发秒级"], today="2026-09-05",
                                                         min_confidence=0.0))
    assert g.counts()["edges.caused_by.confirmed"] == 3              # 4 条 caused_by,候选那条不算


def test_accepted_edge_at_full_confidence_enters_paths(tmp_path):
    """对照:同一条边改成 accepted + 1.0(用户在选择列表里接受了),就进路径。"""
    kb = _kb(tmp_path)
    _set_candidate_edge(kb, "1.0", "accepted")
    g = sf.FileStore.load(kb).graph
    paths = g.paths(["symptom:单条_update_偶发秒级"], today="2026-09-05")
    assert {p.rootcause for p in paths} == {"autovacuum 尾部回收持 8 级锁", "网络抖动"}
    assert next(p for p in paths if p.rootcause == "网络抖动").action == "换网线"
    assert g.counts()["edges.caused_by.confirmed"] == 4


def test_candidate_constrains_edge_does_not_add_a_clause(tmp_path):
    """对象 → 条款同样是确定性引用:候选的 constrains 边即使 1.0 也不出条款。"""
    kb = _kb(tmp_path)
    (kb / "graph" / "extra.yaml").write_text(
        "- src: {kind: clause, name: GS-VAC-009, canonical: \"clause:GS-VAC-009\"}\n"
        "  rel: constrains\n"
        "  dst: {kind: object, name: cbst.other_tbl}\n"
        "  confidence: 1.0\n"
        "  status: candidate\n"
        "  source: rules/vacuum.yaml\n", encoding="utf-8")
    g = sf.FileStore.load(kb).graph
    assert g.clauses_for(["object:cbst.other_tbl"]) == {}
    assert g.clauses_for(["object:cbst.cosp_asyn_task_dtl"]) == {
        "object:cbst.cosp_asyn_task_dtl": ["clause:GS-VAC-002"]}


def test_file_graph_clauses_for_and_counts(tmp_path):
    g = sf.FileStore.load(_kb(tmp_path)).graph
    assert g.clauses_for(["object:cbst.cosp_asyn_task_dtl", "object:nope"]) == {
        "object:cbst.cosp_asyn_task_dtl": ["clause:GS-VAC-002"]}
    assert g.clauses_for([]) == {}
    c = g.counts()
    # confidence < 1 的只有两条:那条候选边(0.6)和「推测」案例自带的 involves 边(0.6)
    assert c["edges"] >= 9 and c["edges.confirmed"] == c["edges"] - 2
    assert c["edges.caused_by"] == 4 and c["edges.caused_by.confirmed"] == 3
    assert c["nodes.symptom"] == 1 and c["nodes.case"] == 2


def test_file_store_without_graph_dir_has_empty_graph(tmp_path):
    kb = _kb(tmp_path)
    (kb / "graph" / "cbst.yaml").unlink()
    fs = sf.FileStore.load(kb)
    assert fs.graph.paths(["symptom:单条_update_偶发秒级"], today="2026-09-05") == []
    assert fs.graph.counts()["edges.confirmed"] >= 0                   # 案例自带的 involves 边仍在
    assert fs.search_chunks_lexical(_q("autovacuum"), k=3)             # 文档检索不受影响


def test_load_tolerates_broken_files(tmp_path):
    kb = _kb(tmp_path)
    (kb / "cases" / "broken.md").write_text("---\nid: [\n---\n", encoding="utf-8")
    (kb / "graph" / "bad.yaml").write_text("- src: 1\n", encoding="utf-8")
    fs = sf.FileStore.load(kb)
    assert fs.counts()["docs.case"] == 2
    assert any("broken" in w or "bad.yaml" in w for w in fs.warnings)

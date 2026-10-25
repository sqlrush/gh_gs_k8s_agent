"""common.kb.query / render —— 假存储钉住融合、阈值、降级与渲染纪律(无库)。"""
import pathlib
import sys
from dataclasses import dataclass

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common.kb import config as kbconfig, query, render, store as spg, store_graph as sg  # noqa: E402

try:                                              # 部署仓有 common.finding;主干还没有,用同形的本地替身
    from common.finding import Finding, Severity  # noqa: E402
except ImportError:                               # pragma: no cover
    from enum import IntEnum

    class Severity(IntEnum):
        OK = 0; NOTICE = 1; WARN = 2; CRITICAL = 3

        def label(self):
            return {3: "🔴严重", 2: "🟠告警", 1: "🟡关注"}.get(int(self), "🟢健康")

    @dataclass(frozen=True)
    class Finding:
        dimension: str; code: str; severity: Severity; metric: str; value: str
        threshold: str; evidence: str; sql_id: str = ""; skill: str = ""


def _hit(id, doc_id, kind, score, content="内容", title="标题", section="现场", meta=None, source="src"):
    return spg.Hit(id=id, doc_id=doc_id, kind=kind, score=score, content=content, title=title,
                   section=section, meta=meta or {}, source=source, seq=0)


class FakePg:
    def __init__(self, lex=(), vec=(), nodes_lex=(), nodes_vec=(), sections=None, docs=None, vector=True):
        self.lex, self.vec, self.nodes_lex, self.nodes_vec = list(lex), list(vec), list(nodes_lex), list(nodes_vec)
        self._sections = sections or {}
        self._docs = docs or {}
        self._vector = vector
        self.closed = False

    # 只对含 autovacuum 的查询命中,别的查询一无所获——这样才能同时测"有命中"和"明写无"。
    @staticmethod
    def _relevant(tokens): return any("autovacuum" in t for t in tokens)
    def has_index(self): return True
    def capabilities(self): return spg.Capabilities("sqlite", "3.40", self._vector, False, 4)
    def search_chunks_lexical(self, tokens, k=10, kinds=None): return self.lex[:k] if self._relevant(tokens) else []
    def search_chunks_vector(self, emb, k=10, kinds=None): return self.vec[:k]
    def search_nodes_lexical(self, tokens, k=10, kinds=None): return self.nodes_lex[:k] if self._relevant(tokens) else []
    def search_nodes_vector(self, emb, k=10, kinds=None): return self.nodes_vec[:k]
    def doc_sections(self, doc_id): return self._sections.get(doc_id, {})
    def docs_by_ids(self, ids): return {i: self._docs[i] for i in ids if i in self._docs}
    def coverage(self): return (10, 10)
    def counts(self): return {"docs.rule": 2, "docs.case": 3, "docs.raw": 1, "chunks": 10, "nodes": 4}
    def meta_get(self, k): return "pgvector" if k == "vector_engine" else None
    def close(self): self.closed = True


class FakeGraph:
    def __init__(self, paths=(), clauses=None, fail=False):
        self._paths, self._clauses, self.fail = list(paths), clauses or {}, fail
        self.asked = []

    def ping(self): return "Neo4j 5"
    def paths(self, ids, today, min_confidence=1.0, limit=20):
        self.asked.append(list(ids))
        if self.fail:
            raise sg.GraphStoreError("Neo4j 不可达(测试)")
        return self._paths if ids else []
    def clauses_for(self, ids, min_confidence=1.0): return {i: self._clauses[i] for i in ids if i in self._clauses}
    def counts(self): return {"edges": 7, "edges.confirmed": 6}


class FakeEmbedder:
    def __init__(self, fail=False): self.fail = fail
    def embed_one(self, text, timeout_s=None): return None if self.fail else [1, 0, 0, 0]


def _session(tmp_path, pg, graph=None, embedder=None, thresholds=None):
    cfg = kbconfig.KbConfig(kb_dir=tmp_path, store=kbconfig.StoreConfig(), embeddings=kbconfig.EmbeddingConfig(),
                            thresholds=thresholds or kbconfig.Thresholds(), defaults={})
    (tmp_path / "VERSION").write_text("2026.09\n", encoding="utf-8")
    return query.KbSession(cfg, pg, graph, embedder)


CASE_ID = "S1-20250224-CBST-偶现单条update慢"

# 文件模式用的最小知识库:一个案例、一条条款、三条已确认边(现象 → 根因 → 处置)。
_FILE_CASE = f"""---
id: {CASE_ID}
title: 偶现单条 update 走索引耗时 3s
system: CBST
occurred_at: 2025-02-24
conclusion: 已确认
source: sources/report.v1.docx#前言
objects: [cbst.cosp_asyn_task_dtl]
signals: [autovacuum 频繁触发]
rules: [GS-VAC-002]
---
## 现场
业务偶现单条 update 耗时 3s,cbst.cosp_asyn_task_dtl 的 autovacuum 次数异常高。
## 判断
autovacuum 持 8 级锁。
## 处置
针对小表调大 autovacuum_vacuum_threshold。
## 复发标志
单条 update 偶发 3s 且该表 autovacuum 次数异常高。
"""
_FILE_TRIPLES = f"""
- src: {{kind: symptom, name: 单条 update 偶发秒级}}
  rel: caused_by
  dst: {{kind: rootcause, name: autovacuum 持 8 级锁}}
  confidence: 1.0
  source: cases/{CASE_ID}.md#判断
  case: {CASE_ID}
- src: {{kind: rootcause, name: autovacuum 持 8 级锁}}
  rel: handled_by
  dst: {{kind: action, name: 表级调大 autovacuum_vacuum_threshold}}
  confidence: 1.0
  source: cases/{CASE_ID}.md#处置
  case: {CASE_ID}
- src: {{kind: case, name: x, canonical: "case:{CASE_ID}"}}
  rel: exhibits
  dst: {{kind: symptom, name: 单条 update 偶发秒级}}
  confidence: 1.0
  source: cases/{CASE_ID}.md#现场
  case: {CASE_ID}
"""
_FILE_RULES = """- id: GS-VAC-002
  severity: warn
  check: advisory
  rule: 小表 autovacuum 阈值按表级调大 autovacuum_vacuum_threshold
  keywords: [autovacuum 阈值]
  source: 《运维规范》v5 §6.2
"""


def _file_kb(tmp_path, kb_yaml="embeddings: {source: none}\n"):
    for sub in ("cases", "graph", "rules"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    (tmp_path / "VERSION").write_text("2026.09\n", encoding="utf-8")
    (tmp_path / "cases" / f"{CASE_ID}.md").write_text(_FILE_CASE, encoding="utf-8")
    (tmp_path / "graph" / "cbst.yaml").write_text(_FILE_TRIPLES, encoding="utf-8")
    (tmp_path / "rules" / "vacuum.yaml").write_text(_FILE_RULES, encoding="utf-8")
    (tmp_path / "kb.yaml").write_text(kb_yaml, encoding="utf-8")
    return tmp_path


def _rich_pg():
    # 假命中的正文必须真含查询里的强 token(autovacuum / cbst.cosp_asyn_task_dtl):相关度门槛会核对。
    return FakePg(
        lex=[_hit("case:%s#1" % CASE_ID, "case:" + CASE_ID, "case", 0.30,
                  "偶现 update › 现场\n业务偶现单条update耗时3s,autovacuum 频繁触发 cbst.cosp_asyn_task_dtl",
                  "偶现单条 update 慢", meta={"conclusion": "已确认", "occurred_at": "2025-02-24"}),
             _hit("rule:GS-VAC-002#0", "rule:GS-VAC-002", "rule", 0.20, "GS-VAC-002 小表 autovacuum 次数异常高时按表级调大阈值",
                  "小表 autovacuum 阈值", meta={"severity": "warn"}, source="《运维规范》v5 §6.2"),
             _hit("raw:q1/T-100#0", "raw:q1/T-100", "raw", 0.05, "工单 T-100 原文 autovacuum 次数异常高", "工单 T-100")],
        vec=[_hit("case:%s#2" % CASE_ID, "case:" + CASE_ID, "case", 0.80, "偶现 update › 判断\n持8级锁",
                  "偶现单条 update 慢", meta={"conclusion": "已确认", "occurred_at": "2025-02-24"})],
        nodes_lex=[spg.Hit(id="symptom:update_slow", kind="symptom", title="单条 update 偶发秒级", score=0.4,
                           content="autovacuum 次数异常高 频繁触发")],
        sections={"case:" + CASE_ID: {"处置": "针对小表调大 autovacuum_vacuum_threshold", "现场": "x"}},
        docs={"rule:GS-VAC-002": spg.DocRow(id="rule:GS-VAC-002", kind="rule", title="小表 autovacuum 阈值",
                                             source="《运维规范》v5 §6.2", version="", meta={"severity": "warn"})})


def _path():
    return sg.PathHit("symptom:update_slow", "单条 update 偶发秒级", "rootcause:x", "autovacuum 持 8 级锁",
                      "action:y", "表级调大 autovacuum_vacuum_threshold", ("case:" + CASE_ID,), ("s1", "s2"), 1.0)


# ---------------------------------------------------------------- rrf (pure)

def test_rrf_normalizes_to_one_for_top_of_every_list():
    a = _hit("x", "d", "case", 0.9)
    fused = query.rrf([[a], [a]])
    assert fused["x"][0] == pytest.approx(1.0)
    only_first = query.rrf([[a], [_hit("y", "e", "case", 0.9)]])
    assert only_first["x"][0] == pytest.approx(0.5)
    single = query.rrf([[a]])
    assert single["x"][0] == pytest.approx(1.0)


def test_rrf_keeps_raw_scores_per_source():
    a_lex = _hit("x", "d", "case", 0.1)
    a_vec = _hit("x", "d", "case", 0.9)
    fused = query.rrf([[a_lex], [a_vec]])
    assert fused["x"][2] == 0.1 and fused["x"][3] == 0.9


def test_identifiers_in_picks_objects_and_gucs():
    ids = query.identifiers_in("XACT_LONG 长事务 cbst.cosp_asyn_task_dtl autovacuum_vacuum_threshold LWLock:WALWriteLock 3s")
    assert "cbst.cosp_asyn_task_dtl" in ids and "autovacuum_vacuum_threshold" in ids and "lwlock:walwritelock" in ids
    assert "3s" not in ids


# ---------------------------------------------------------------- search

def test_search_returns_clause_case_and_path(tmp_path):
    sess = _session(tmp_path, _rich_pg(), FakeGraph([_path()]), FakeEmbedder())
    refs = sess.search("VAC_FREQ", "🟠 VAC_FREQ", "VAC_FREQ autovacuum 次数异常高 cbst.cosp_asyn_task_dtl")
    assert [c.short_id for c in refs.clauses] == ["GS-VAC-002"]
    assert [c.short_id for c in refs.cases] == [CASE_ID]
    assert refs.cases[0].sections["处置"].startswith("针对小表")
    assert len(refs.paths) == 1 and refs.paths[0].cases == (CASE_ID,) and refs.paths[0].support == 1
    assert refs.raws and refs.raws[0].short_id == "q1/T-100"
    assert not refs.notes


def test_search_drops_weak_hits_and_logs_miss(tmp_path):
    """词法分低于下限、又没向量佐证的命中不许凑数;整条查不到要记进 misses.log。"""
    weak = FakePg(lex=[_hit("case:a#0", "case:a", "case", 0.001, "autovacuum")])
    sess = _session(tmp_path, weak, FakeGraph(), None)
    refs = sess.search("IDX_UNUSED", "🟡 IDX_UNUSED", "IDX_UNUSED 未使用索引 autovacuum")
    assert refs.empty
    log = (tmp_path / "index" / "misses.log").read_text(encoding="utf-8")
    assert "IDX_UNUSED" in log


def test_search_drops_hits_that_share_no_strong_token(tmp_path):
    """词法分不低,但只靠「等待」这种泛二元组命中——没有一个强 token,不许上榜(真跑抓到的串台)。"""
    generic = FakePg(lex=[_hit("case:lock#0", "case:lock", "case", 0.3, "锁等待超过 30 秒 autovacuum 频繁触发")],
                     nodes_lex=[spg.Hit(id="symptom:lockwait", kind="symptom", title="锁等待超过 30 秒", score=0.3)])
    sess = _session(tmp_path, generic, FakeGraph([_path()]), None)
    refs = sess.search("WAIT_LWLOCK_HEAVY", "🟠 WAIT_LWLOCK_HEAVY",
                       "WAIT_LWLOCK_HEAVY LWLOCK_EVENT 等待占 DB_TIME 20% autovacuum 频繁触发")
    assert refs.cases and refs.paths == ()          # 案例含强 token autovacuum 留下;现象节点只靠「等待」→ 不走路径


def test_relevance_counts_strong_tokens_and_coverage():
    r = query.relevance(["wait_lwlock_heavy", "等待", "占", "autovacuum"], "锁等待超过 30 秒 autovacuum 频繁")
    assert r.strong == 1 and r.matched == 2 and r.coverage == pytest.approx(0.5)
    assert query.is_strong_token("wait_lwlock_heavy") and query.is_strong_token("cbst.cosp_asyn_task_dtl")
    assert query.is_strong_token("autovacuum")
    for weak in ("等待", "30", "stat", "user", "tables", "database", "time"):
        assert not query.is_strong_token(weak), weak


def test_relevance_counts_a_matched_cjk_phrase_as_strong():
    """纯中文提问没有标识符:连续 ≥3 个二元组首尾相接、都落在正文里(= 一个 ≥4 字的中文短语)算 1 个强 token。
    散落的「等待」「会话」连不成短语,照旧不算——门槛防的就是它们;3 字短语只有 2 个二元组,也不够。"""
    from common.kb import text as kbtext
    r = query.relevance(kbtext.query_tokens("空闲事务持锁阻塞会话"),
                        "S2 空闲事务持锁阻塞60余会话 idle in transaction 40 分钟")
    assert r.strong == 1 and r.matched >= 6
    scattered = query.relevance(["等待", "锁阻", "会话"], "锁等待超过 30 秒,阻塞 12 个会话")
    assert scattered.strong == 0 and scattered.matched == 2
    assert query.relevance(["序列", "列取"], "序列取号").strong == 0


def test_search_without_graph_has_no_paths_but_still_cases(tmp_path):
    sess = _session(tmp_path, _rich_pg(), None, None)
    refs = sess.search("k", "l", "autovacuum 次数异常高")
    assert refs.cases and refs.paths == ()


def test_vector_timeout_degrades_with_note(tmp_path):
    sess = _session(tmp_path, _rich_pg(), FakeGraph([_path()]), FakeEmbedder(fail=True))
    refs = sess.search("k", "l", "autovacuum 次数异常高")
    assert refs.cases                                   # 词法仍然命中
    assert any("超时" in n for n in refs.notes)
    assert "本次超时" in sess.status().vector


def test_graph_failure_degrades_not_raises(tmp_path):
    sess = _session(tmp_path, _rich_pg(), FakeGraph(fail=True), None)
    refs = sess.search("k", "l", "autovacuum 次数异常高")
    assert refs.cases and refs.paths == ()
    assert any("降级" in n for n in sess.notes)


def test_graph_clauses_for_objects_added_even_without_lexical_hit(tmp_path):
    pg = _rich_pg(); pg.lex = []; pg.vec = []
    graph = FakeGraph(clauses={"object:cbst.cosp_asyn_task_dtl": ["clause:GS-VAC-002"]})
    sess = _session(tmp_path, pg, graph, None)
    refs = sess.search("k", "l", "VAC_FREQ cbst.cosp_asyn_task_dtl")
    assert [c.short_id for c in refs.clauses] == ["GS-VAC-002"]


# ---------------------------------------------------------------- entry points

def _finding(code="VAC_FREQ", sev=Severity.WARN):
    if code == "IDX_UNUSED":
        return Finding(dimension="index", code=code, severity=sev, metric="未使用索引", value="3",
                       threshold="0", evidence="idx_order_ts 自上次统计以来 idx_scan = 0")
    return Finding(dimension="vacuum", code=code, severity=sev, metric="autovacuum 次数/h", value="37",
                   threshold="20", evidence="cbst.cosp_asyn_task_dtl autovacuum 次数异常高")


def test_from_findings_uses_session_and_labels(tmp_path):
    sess = _session(tmp_path, _rich_pg(), FakeGraph([_path()]), None)
    res = query.from_findings([_finding(), _finding("IDX_UNUSED", Severity.NOTICE)], session=sess)
    assert res.status.attached and res.status.counts["docs.case"] == 3
    assert [it.key for it in res.items] == ["VAC_FREQ", "IDX_UNUSED"]
    assert res.items[0].label.startswith("🟠告警 VAC_FREQ(autovacuum 次数/h = 37)")
    assert not sess.pg.closed                            # 外部会话由调用方关


def test_from_findings_never_raises_when_unattached(tmp_path):
    res = query.from_findings([_finding()], kb_dir=tmp_path / "nope")
    assert res.status.attached is False and "不存在" in res.status.reason and res.items == ()


# ---------------------------------------------------------------- 三种模式的感知与回退

def test_open_without_store_falls_back_to_file_mode(tmp_path):
    """没配向量库/图库不是「未接入」:直接在 <kb>/ 文件上检索,小节格式一样,状态行写明模式与原因。"""
    sess = query.KbSession.open(_file_kb(tmp_path))
    try:
        assert sess.attached and sess.mode == "文件"
        st = sess.status()
        assert st.attached and st.mode == "文件" and "还没建索引" in st.reason
        assert st.counts["docs.case"] == 1 and st.counts["docs.rule"] == 1
        assert st.vector.startswith("未启用") and st.graph.startswith("图文件") and "条已确认边" in st.graph
        refs = sess.search("VAC_FREQ", "🟠 VAC_FREQ", "VAC_FREQ autovacuum 次数异常高 cbst.cosp_asyn_task_dtl")
        assert [c.short_id for c in refs.cases] == [CASE_ID]
        assert [c.short_id for c in refs.clauses] == ["GS-VAC-002"]
        assert refs.cases[0].sections["处置"].startswith("针对小表")
        assert len(refs.paths) == 1 and refs.paths[0].action.startswith("表级调大") and refs.paths[0].cases == (CASE_ID,)
    finally:
        sess.close()


def test_file_mode_finds_case_by_pure_chinese_question(tmp_path):
    """用户用现象语言问「有没有偶现单条更新慢的案例」,一个标识符都没有。文件模式没有向量兜底,
    以前这种提问过不了强 token 门槛,永远「无相似案例」,模型只能自己 grep cases/(2026-09-18 在 Pod 里真跑抓到的)。"""
    sess = query.KbSession.open(_file_kb(tmp_path))
    try:
        refs = sess.search("k", "l", "有没有偶现单条更新慢的案例")
        assert [c.short_id for c in refs.cases] == [CASE_ID]
    finally:
        sess.close()


def test_open_falls_back_to_files_when_the_index_file_is_broken(tmp_path):
    """索引文件坏了 / 不是 SQLite:不是「未接入」,退到文件模式,原因写进状态行。"""
    kb = _file_kb(tmp_path)
    (kb / "index").mkdir(exist_ok=True)
    (kb / "index" / "store.sqlite").write_bytes(b"not a sqlite file" * 100)
    sess = query.KbSession.open(kb)
    try:
        assert sess.attached and sess.mode == "文件" and "打不开索引文件" in sess.status().reason
        assert sess.search("k", "l", "autovacuum 次数异常高 cbst.cosp_asyn_task_dtl").cases
    finally:
        sess.close()


def test_legacy_store_pg_does_not_change_the_mode(tmp_path):
    """kb.yaml 里还留着已下线的 store.pg:不连、不报错,照常走文件模式 / SQLite 索引。"""
    kb = _file_kb(tmp_path, "store:\n  pg: {host: 127.0.0.1, port: 1, database: d, user: u, credential: kb}\n")
    sess = query.KbSession.open(kb, password_lookup=lambda n: (_ for _ in ()).throw(AssertionError("不该取口令")))
    try:
        assert sess.attached and sess.mode == "文件" and "还没建索引" in sess.status().reason
    finally:
        sess.close()


def test_open_graph_uses_graph_files_when_neo4j_unreachable(tmp_path):
    kb = _file_kb(tmp_path)
    cfg = kbconfig.KbConfig(kb_dir=kb, embeddings=kbconfig.EmbeddingConfig(), thresholds=kbconfig.Thresholds(),
                            defaults={}, store=kbconfig.StoreConfig(
                                graph=kbconfig.GraphStore(url="http://127.0.0.1:1", user="neo4j", credential="g")))
    graph, note = query.open_graph(cfg, lambda n: "pw")
    assert graph is not None and graph.ping() and "Neo4j 不可用" in note
    assert graph.paths(["symptom:单条_update_偶发秒级"], today="2026-09-05")
    none_cfg = kbconfig.KbConfig(kb_dir=kb, embeddings=kbconfig.EmbeddingConfig(), thresholds=kbconfig.Thresholds(),
                                 defaults={}, store=kbconfig.StoreConfig())
    graph2, note2 = query.open_graph(none_cfg, lambda n: "pw")
    assert graph2 is not None and "store.graph" in note2


def test_mode_reflects_which_backends_are_live(tmp_path):
    from common.kb import store_files as sf
    kb = _file_kb(tmp_path)
    assert _session(tmp_path, _rich_pg(), FakeGraph()).mode == "向量库+图库"
    assert _session(tmp_path, _rich_pg(), sf.load_graph(kb)).mode == "向量库+图文件"
    assert _session(tmp_path, _rich_pg(), None).mode == "向量库"
    assert _session(tmp_path, sf.FileStore.load(kb), sf.load_graph(kb)).mode == "文件"


def test_open_reports_missing_dir_and_bad_yaml_as_unattached(tmp_path):
    """文件模式也要有目录和合法的 kb.yaml:这两种才是真正的「未接入」。"""
    assert not query.KbSession.open(tmp_path / "nope").attached
    (tmp_path / "kb.yaml").write_text("store: [\n", encoding="utf-8")
    sess = query.KbSession.open(tmp_path)
    assert not sess.attached and "kb.yaml" in sess.reason


def test_empty_kb_stays_unattached_instead_of_a_section_full_of_none(tmp_path):
    """装完还没导入任何材料时,不要给每条发现挂一串「无对应条款 / 无相似案例」——
    那是噪音,且与契约里「客户尚未导入就别提知识库」相抵触。空库 = 未接入(说明是空的)。"""
    (tmp_path / "kb.yaml").write_text("embeddings: {source: none}\n", encoding="utf-8")
    (tmp_path / "rules").mkdir()
    sess = query.KbSession.open(tmp_path)
    assert not sess.attached and "空" in sess.reason
    assert render.render_section(query.from_findings([_finding()], kb_dir=tmp_path)).count("\n") <= 3


def test_file_mode_reports_a_graph_that_could_not_be_loaded(tmp_path):
    """图文件加载不出来时状态行要说原因,不能显示成「未配置」——那是两回事。"""
    sess = query.KbSession(None, None, None, None)
    st = query.KbSession(_session(tmp_path, _rich_pg(), None).cfg, _rich_pg(), None, None,
                         graph_reason="Neo4j 不可用:连不上").status()
    assert "Neo4j 不可用" in st.graph


def test_graph_label_explains_legacy_edges_without_status(tmp_path):
    """旧版索引写进 Neo4j 的边不带 status,一律不进路径;状态行要说出来——否则路径全是「无」却看不出为什么。"""
    class LegacyGraph(FakeGraph):
        def counts(self): return {"edges": 7, "edges.confirmed": 0, "edges.no_status": 7}

    st = _session(tmp_path, _rich_pg(), LegacyGraph()).status()
    assert st.graph.startswith("Neo4j 0 条已确认边") and "7 条边是旧版索引写的" in st.graph
    assert "kb.py" not in st.graph                      # 状态行进诊断报告,不带导入命令
    assert _session(tmp_path, _rich_pg(), FakeGraph()).status().graph == "Neo4j 6 条已确认边"


# ---------------------------------------------------------------- render

def test_render_unattached_is_title_plus_reason_only(tmp_path):
    res = query.from_findings([_finding()], kb_dir=tmp_path / "nope")
    out = render.render_section(res)
    assert out.startswith("## 客户知识库参照\n> 知识库未接入(")
    assert "贵行规范" not in out


def test_render_item_has_all_four_lines_and_explicit_none(tmp_path):
    sess = _session(tmp_path, _rich_pg(), FakeGraph([_path()]), None)
    res = query.from_findings([_finding(), _finding("IDX_UNUSED", Severity.NOTICE)], session=sess)
    out = render.render_section(res)
    assert "> 知识库 v2026.09 · 模式:向量库+图库 · 条款 2 · 案例 3 · 原始工单 1 · 向量:" in out
    assert "### 对 🟠告警 VAC_FREQ" in out
    assert "- **贵行规范** GS-VAC-002《小表 autovacuum 阈值》(warn) ——《运维规范》v5 §6.2" in out
    assert f"- **历史相似** {CASE_ID}(结论强度:已确认,2025-02-24):处置 = 针对小表调大 autovacuum_vacuum_threshold" in out
    assert "- **本行历史路径** 单条 update 偶发秒级 → autovacuum 持 8 级锁 → 表级调大 autovacuum_vacuum_threshold (1 案例支持:" in out
    assert "- **原始工单** q1/T-100(未结构化)" in out
    # 第二条发现没命中:必须明写「无」,不能省略
    assert "### 对 🟡关注 IDX_UNUSED" in out
    assert "- 贵行规范:无对应条款 · 历史相似:无相似案例 · 路径:无" in out
    assert "违规汇总" not in out


def test_render_file_mode_status_line_names_mode_and_reason(tmp_path):
    sess = query.KbSession.open(_file_kb(tmp_path))
    try:
        res = query.from_findings([_finding()], session=sess)
    finally:
        sess.close()
    out = render.render_section(res)
    assert out.startswith("## 客户知识库参照\n> 知识库 v2026.09 · 模式:文件(还没建索引")
    assert "kb.py" not in out.split("\n")[1], "状态行会进诊断报告,诊断环境里没有导入命令,不能出现命令"
    assert "· 向量:未启用" in out and "· 图:图文件" in out
    assert f"- **历史相似** {CASE_ID}(结论强度:已确认,2025-02-24):处置 = 针对小表调大" in out
    assert "- **本行历史路径** 单条 update 偶发秒级 → autovacuum 持 8 级锁 → 表级调大 autovacuum_vacuum_threshold (1 案例支持:" in out


def test_render_partial_hits_state_missing_kinds(tmp_path):
    sess = _session(tmp_path, _rich_pg(), None, None)
    res = query.from_findings([_finding()], session=sess)
    out = render.render_section(res)
    assert "- 本行历史路径:无(没有已确认的 现象→根因→处置 链)" in out
    assert "- **历史相似**" in out

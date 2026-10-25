"""common.kb.store_graph —— 打真 Neo4j(HTTP Query API)。

    KB_TEST_NEO4J = url|user|password      (用 | 分隔:url 里有冒号)
没设就跳过并说明。每个用例从空图开始(rebuild)。
"""
import os
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common.kb import store_graph as sg  # noqa: E402

pytestmark = pytest.mark.live
OK = sg.PATH_STATUS          # 用户接受过的边;EdgeRow 缺省是 candidate(不进路径)


@pytest.fixture
def graph():
    spec = os.environ.get("KB_TEST_NEO4J")
    if not spec:
        pytest.skip("未配置 KB_TEST_NEO4J(Neo4j 不可达/未配置,不是通过)")
    url, user, pw = spec.split("|", 2)
    g = sg.GraphStore(url, user, pw)
    g.rebuild()
    yield g


def _seed(g: sg.GraphStore):
    g.upsert_nodes([
        sg.NodeRow(id="case:S1-A", kind="case", name="偶现单条update慢"),
        sg.NodeRow(id="case:S1-B", kind="case", name="同类第二例"),
        sg.NodeRow(id="symptom:update_slow", kind="symptom", name="单条 update 偶发秒级",
                   aliases=("update 偶现慢",)),
        sg.NodeRow(id="rootcause:autovac_lock", kind="rootcause", name="autovacuum 尾部回收持 8 级锁"),
        sg.NodeRow(id="action:raise_threshold", kind="action", name="表级调大 autovacuum_vacuum_threshold"),
        sg.NodeRow(id="object:cbst.cosp_asyn_task_dtl", kind="object", name="cbst.cosp_asyn_task_dtl"),
        sg.NodeRow(id="clause:GS-VAC-002", kind="clause", name="小表 autovacuum 阈值"),
    ], kb_version="2026.09")
    g.upsert_edges([
        sg.EdgeRow("case:S1-A", "exhibits", "symptom:update_slow", 1.0, "cases/S1-A.md#现场", "S1-A", status=OK),
        sg.EdgeRow("case:S1-B", "exhibits", "symptom:update_slow", 1.0, "cases/S1-B.md#现场", "S1-B", status=OK),
        sg.EdgeRow("symptom:update_slow", "caused_by", "rootcause:autovac_lock", 1.0, "cases/S1-A.md#判断", "S1-A",
                   status=OK),
        sg.EdgeRow("symptom:update_slow", "caused_by", "rootcause:autovac_lock", 1.0, "cases/S1-B.md#判断", "S1-B",
                   status=OK),
        sg.EdgeRow("rootcause:autovac_lock", "handled_by", "action:raise_threshold", 1.0, "cases/S1-A.md#处置", "S1-A",
                   status=OK),
        sg.EdgeRow("case:S1-A", "involves", "object:cbst.cosp_asyn_task_dtl", 1.0, "cases/S1-A.md#判断", "S1-A",
                   status=OK),
        sg.EdgeRow("clause:GS-VAC-002", "constrains", "object:cbst.cosp_asyn_task_dtl", 1.0, "rules/vacuum.yaml", "",
                   status=OK),
    ])


def test_ping_and_setup(graph):
    assert graph.ping()
    graph.setup()
    graph.setup()                     # 幂等


def test_repr_hides_password(graph):
    assert "password" not in repr(graph) and os.environ["KB_TEST_NEO4J"].split("|")[2] not in repr(graph)


def test_upsert_is_idempotent_and_counts(graph):
    _seed(graph)
    _seed(graph)
    c = graph.counts()
    assert c["nodes.case"] == 2 and c["nodes.symptom"] == 1
    assert c["edges.caused_by"] == 2          # 两份材料各一条边
    assert c["edges"] == 7 and c["edges.confirmed"] == 7


def test_edge_without_endpoint_is_an_error_not_silent(graph):
    _seed(graph)
    with pytest.raises(sg.GraphStoreError, match="端点"):
        graph.upsert_edges([sg.EdgeRow("symptom:update_slow", "caused_by", "rootcause:ghost", 1.0, "x", "")])


def test_illegal_kind_or_rel_rejected(graph):
    with pytest.raises(sg.GraphStoreError):
        graph.upsert_nodes([sg.NodeRow(id="x", kind="co_occurs_thing", name="x")])
    with pytest.raises(sg.GraphStoreError):
        graph.upsert_edges([sg.EdgeRow("a", "co_occurs", "b", 1.0, "s", "")])


def test_paths_full_chain_with_case_support(graph):
    _seed(graph)
    hits = graph.paths(["symptom:update_slow"], today="2026-09-02")
    assert len(hits) == 1
    p = hits[0]
    assert p.rootcause_id == "rootcause:autovac_lock" and p.action_id == "action:raise_threshold"
    assert set(p.cases) == {"S1-A", "S1-B"} or set(p.cases) == {"case:S1-A", "case:S1-B"}
    assert p.min_confidence == 1.0


def test_paths_exclude_unconfirmed_edges(graph):
    """confidence 0.7 的边不得进入路径——这是"图不掺假"的守卫。"""
    _seed(graph)
    graph.upsert_edges([sg.EdgeRow("rootcause:autovac_lock", "handled_by", "action:raise_threshold",
                                   0.7, "cases/S1-C.md#处置", "S1-C", status=OK)])
    hits = graph.paths(["symptom:update_slow"], today="2026-09-02")
    assert all("S1-C" not in s for h in hits for s in h.sources)
    graph.upsert_nodes([sg.NodeRow(id="action:guess", kind="action", name="猜的处置")])
    graph.upsert_edges([sg.EdgeRow("rootcause:autovac_lock", "handled_by", "action:guess", 0.9, "cases/S1-D.md#处置", "S1-D",
                                   status=OK)])
    assert all(h.action_id != "action:guess" for h in graph.paths(["symptom:update_slow"], today="2026-09-02"))


def test_paths_exclude_candidate_edges_even_at_full_confidence(graph):
    """手工把候选边的 confidence 写成 1.0:status 仍是 candidate,不得进路径,也不算已确认。"""
    _seed(graph)
    graph.upsert_nodes([sg.NodeRow(id="action:unconfirmed", kind="action", name="未确认的处置")])
    graph.upsert_edges([sg.EdgeRow("rootcause:autovac_lock", "handled_by", "action:unconfirmed", 1.0,
                                   "cases/S1-E.md#处置", "S1-E", status="candidate")])
    hits = graph.paths(["symptom:update_slow"], today="2026-09-02")
    assert [h.action_id for h in hits] == ["action:raise_threshold"]
    assert graph.counts()["edges.confirmed"] == 7


def test_legacy_edges_without_status_stay_out_of_paths(graph):
    """旧版索引写的边没有 status 属性:拿不准就不进路径,counts 另报 edges.no_status。"""
    _seed(graph)
    graph.run("MATCH ()-[e:HANDLED_BY]->() REMOVE e.status")
    assert graph.paths(["symptom:update_slow"], today="2026-09-02") == []
    assert graph.counts()["edges.no_status"] == 1


def test_paths_respect_validity_window(graph):
    _seed(graph)
    graph.upsert_edges([sg.EdgeRow("rootcause:autovac_lock", "handled_by", "action:raise_threshold",
                                   1.0, "cases/S1-A.md#处置", "S1-A", valid_from="2020-01-01", valid_to="2021-01-01",
                                   status=OK)])
    assert graph.paths(["symptom:update_slow"], today="2026-09-02") == []
    assert graph.paths(["symptom:update_slow"], today="2020-06-01")


def test_neighbors_bounded_hops(graph):
    _seed(graph)
    one = {n.id for n in graph.neighbors(["symptom:update_slow"], hops=1)}
    assert "rootcause:autovac_lock" in one and "action:raise_threshold" not in one
    two = {n.id for n in graph.neighbors(["symptom:update_slow"], hops=2)}
    assert "action:raise_threshold" in two


def test_cases_for_and_clauses_for(graph):
    _seed(graph)
    assert set(graph.cases_for(["symptom:update_slow"])["symptom:update_slow"]) == {"case:S1-A", "case:S1-B"}
    assert graph.clauses_for(["object:cbst.cosp_asyn_task_dtl"]) == {"object:cbst.cosp_asyn_task_dtl": ["clause:GS-VAC-002"]}


def test_delete_by_source_prefix_and_orphans(graph):
    _seed(graph)
    n = graph.delete_edges_by_source_prefix("cases/S1-B.md")
    assert n == 2
    assert graph.counts()["edges.caused_by"] == 1
    assert graph.delete_orphans() == 1        # case:S1-B 没边了
    assert graph.node("case:S1-B") is None


def test_rebuild_empties(graph):
    _seed(graph)
    graph.rebuild()
    assert graph.counts() == {"edges": 0, "edges.confirmed": 0}


def test_bad_cypher_is_a_graph_error(graph):
    with pytest.raises(sg.GraphStoreError, match="Cypher|HTTP"):
        graph.run("THIS IS NOT CYPHER")


def test_unreachable_url_fails_fast():
    g = sg.GraphStore("http://127.0.0.1:1", "neo4j", "x", timeout_s=1.0)
    with pytest.raises(sg.GraphStoreError, match="不可达"):
        g.ping()

"""store_graph 的纯函数部分(不连库,CI 常跑)。"""
import pathlib
import sys


_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common.kb import store_graph as sg  # noqa: E402


def test_labels_are_camel_case_and_bijective():
    assert sg.LABELS["wait_event"] == "WaitEvent" and sg.LABELS["rootcause"] == "RootCause"
    for kind, label in sg.LABELS.items():
        assert sg.kind_of_label(label) == kind


def test_no_co_occurrence_relation_exists():
    """设计红线:图里不许有共现边。"""
    assert "co_occurs" not in sg.REL_TYPES


def test_graph_store_repr_has_no_password():
    g = sg.GraphStore("http://h:7474", "neo4j", "secret-pw")
    assert "secret-pw" not in repr(g)


# ---------------------------------------------------------------- status 进边、进路径条件(不连库:记下发出的 Cypher)

class _Recorder(sg.GraphStore):
    """不发 HTTP,只记 (cypher, params);写边时按批大小回 n,让端点校验通过。"""

    def __init__(self, edge_rows=()):
        super().__init__("http://h:7474", "neo4j", "pw")
        self.calls = []
        self._edge_rows = list(edge_rows)

    def run(self, cypher, params=None):
        params = params or {}
        self.calls.append((cypher, params))
        if cypher.startswith("UNWIND $rows"):
            return [{"n": len(params["rows"])}]
        if "type(e) AS t" in cypher:
            return self._edge_rows
        return []


def test_confirmed_needs_accepted_status_and_full_confidence():
    assert sg.confirmed("accepted", 1.0)
    assert not sg.confirmed("candidate", 1.0)        # 手工改成 1.0 的候选边
    assert not sg.confirmed("accepted", 0.9)
    assert not sg.confirmed("", 1.0) and not sg.confirmed(None, 1.0)
    assert sg.confirmed("accepted", 0.6, min_confidence=0.5)


def test_edge_row_without_status_defaults_to_candidate():
    """拿不准就不进路径:忘了传 status 的边按候选处理。"""
    e = sg.EdgeRow("symptom:a", "caused_by", "rootcause:b", 1.0, "x")
    assert e.status == "candidate" and not sg.confirmed(e.status, e.confidence)


def test_upsert_edges_writes_status_onto_the_edge():
    g = _Recorder()
    g.upsert_edges([sg.EdgeRow("symptom:a", "caused_by", "rootcause:b", 1.0, "s1", status="accepted"),
                    sg.EdgeRow("symptom:a", "caused_by", "rootcause:c", 1.0, "s2", status="candidate")])
    cypher, params = g.calls[0]
    assert "e.status = r.status" in cypher
    assert [r["status"] for r in params["rows"]] == ["accepted", "candidate"]


def test_every_confirmed_only_query_filters_on_status():
    """路径与其它「只走已确认边」的查询:status = accepted 与置信度两个条件都要在。"""
    g = _Recorder()
    g.paths(["symptom:a"], today="2026-09-02")
    g.neighbors(["symptom:a"])
    g.cases_for(["symptom:a"])
    g.clauses_for(["object:x"])
    assert len(g.calls) == 4
    for cypher, params in g.calls:
        assert params["accepted"] == "accepted" and params["min"] == 1.0
        assert "confidence >= $min" in cypher
    paths_cypher = g.calls[0][0]
    assert "e1.status = $accepted" in paths_cypher and "e2.status = $accepted" in paths_cypher
    assert all("e.status = $accepted" in c for c, _ in g.calls[1:])


def test_counts_reports_legacy_edges_without_status():
    """旧版索引写的边不带 status:不算已确认,另报 edges.no_status 让状态行说明原因。"""
    g = _Recorder(edge_rows=[{"t": "CAUSED_BY", "n": 3, "confirmed": 1, "legacy": 2},
                             {"t": "HANDLED_BY", "n": 1, "confirmed": 1, "legacy": 0}])
    c = g.counts()
    assert c["edges"] == 4 and c["edges.confirmed"] == 2 and c["edges.no_status"] == 2
    edge_cypher, params = g.calls[1]
    assert "e.status = $accepted AND e.confidence >= 1.0" in edge_cypher and params["accepted"] == "accepted"
    fresh = _Recorder(edge_rows=[{"t": "CAUSED_BY", "n": 1, "confirmed": 1, "legacy": 0}]).counts()
    assert "edges.no_status" not in fresh                # 新索引不多出这个键(状态行不提)

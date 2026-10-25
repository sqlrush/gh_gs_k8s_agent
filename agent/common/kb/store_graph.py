"""Neo4j —— 经 HTTP Query API(POST /db/<db>/query/v2)跑 Cypher,urllib 实现,零新依赖。

图里只有客户专属、强类型、带出处的关系:节点标签 = kind,关系类型 = rel,
关系属性 confidence / status / source / case_id / valid_from / valid_to。**没有共现边。**
进「本行处置路径」的边必须 status = accepted 且 confidence ≥ 1.0(见 confirmed())。
边的身份是 (src, rel, dst, source):同一条因果由两份材料各自佐证时是两条边,
"N 案例支持"数的就是它。

真相在 <kb>/graph/*.yaml;这里是可重建的派生索引。不可达时抛 GraphStoreError,
由 query.py 降级(状态行标"图:不可用"),不阻塞 skill。
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

NODE_KINDS = ("object", "symptom", "rootcause", "action", "clause", "case",
              "error", "wait_event", "guc", "component")
REL_TYPES = ("exhibits", "caused_by", "handled_by", "involves", "constrains",
             "references", "depends_on")

# kind → 标签;rel → 关系类型。Cypher 的标签/类型不能参数化,只能白名单拼接。
LABELS = {k: "".join(p.capitalize() for p in k.split("_")) for k in NODE_KINDS}
LABELS["rootcause"] = "RootCause"
LABELS["wait_event"] = "WaitEvent"
REL_LABELS = {r: r.upper() for r in REL_TYPES}

# 只有用户在选择列表里接受过的边能进确定性推理(路径、对象 → 条款)。
PATH_STATUS = "accepted"


def confirmed(status: str, confidence: float, min_confidence: float = 1.0) -> bool:
    """边能否进确定性推理:status = accepted **且** confidence ≥ min,两个条件缺一不可。

    只看置信度不够:正常导入流程里只有用户接受的边记 1.0,但有人手工改 graph/*.yaml、把候选边的
    confidence 写成 1.0 时,它就混进了「本行处置路径」,而读者以为那是客户确认过的因果链。
    graphfiles.Triple.in_path、FileGraph、Neo4j 的 Cypher 都按这一个口径。
    """
    return status == PATH_STATUS and float(confidence) >= float(min_confidence)


# Cypher 里的同一口径(confirmed() 的 Cypher 版);{e} 换成边变量名,参数 $accepted / $min 由调用方给。
_CONFIRMED = "{e}.status = $accepted AND {e}.confidence >= $min"


class GraphStoreError(Exception):
    """图库不可达 / Cypher 报错 / 非法 kind|rel。"""


# 知识库服务在本机/内网,绝不走 HTTP 代理:macOS 的 urllib 会读系统代理设置,
# 连 127.0.0.1 都会被代理接管(实测返回 503,像是 Neo4j 没起来)。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@dataclass(frozen=True)
class NodeRow:
    id: str
    kind: str
    name: str
    canonical: str = ""
    aliases: Sequence[str] = ()
    attrs: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EdgeRow:
    src: str
    rel: str
    dst: str
    confidence: float
    source: str
    case_id: str = ""
    valid_from: str = ""          # ISO 日期字符串;字符串比较即日期比较
    valid_to: str = ""
    # 缺省按候选:拿不准就不进路径。indexer 组图时总是从三元组显式带上 status,
    # 这个缺省只兜「忘了传」的调用方 —— 宁可路径里少一条,也不能让未确认的边冒充确认过的。
    status: str = "candidate"


@dataclass(frozen=True)
class PathHit:
    symptom_id: str
    symptom: str
    rootcause_id: str
    rootcause: str
    action_id: str
    action: str
    cases: Tuple[str, ...]
    sources: Tuple[str, ...]
    min_confidence: float


@dataclass(frozen=True)
class Neighbor:
    id: str
    kind: str
    name: str
    hops: int
    rel: str
    confidence: float


def _label(kind: str) -> str:
    try:
        return LABELS[kind]
    except KeyError:
        raise GraphStoreError(f"非法节点 kind {kind!r},只能是 {'/'.join(NODE_KINDS)}")


def _rel(rel: str) -> str:
    try:
        return REL_LABELS[rel]
    except KeyError:
        raise GraphStoreError(f"非法关系 {rel!r},只能是 {'/'.join(REL_TYPES)}")


def kind_of_label(label: str) -> str:
    for k, v in LABELS.items():
        if v == label:
            return k
    return label.lower()


class GraphStore:
    def __init__(self, url: str, user: str, password: str, database: str = "neo4j",
                 timeout_s: float = 10.0):
        self.url = url.rstrip("/")
        self.database = database
        self.timeout_s = float(timeout_s)
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        self._auth = f"Basic {token}"

    def __repr__(self) -> str:          # 不带口令
        return f"GraphStore(url={self.url!r}, database={self.database!r})"

    # --- HTTP --------------------------------------------------------------

    def run(self, cypher: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """执行一条 Cypher,返回 [{列名: 值}]。"""
        body = json.dumps({"statement": cypher, "parameters": params or {}}).encode()
        req = urllib.request.Request(
            f"{self.url}/db/{self.database}/query/v2", data=body, method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json",
                     "Authorization": self._auth})
        try:
            with _OPENER.open(req, timeout=self.timeout_s) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:500]
            except Exception:
                pass
            raise GraphStoreError(f"Neo4j HTTP {exc.code}:{detail or exc.reason}") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise GraphStoreError(f"Neo4j 不可达({self.url}):{exc}") from exc
        errors = payload.get("errors") or []
        if errors:
            first = errors[0] if isinstance(errors[0], dict) else {"message": str(errors[0])}
            raise GraphStoreError(f"Cypher 失败:{first.get('code', '')} {first.get('message', '')}")
        data = payload.get("data") or {}
        fields = data.get("fields") or []
        return [dict(zip(fields, row)) for row in (data.get("values") or [])]

    def ping(self) -> str:
        rows = self.run("CALL dbms.components() YIELD name, versions RETURN name, versions[0] AS v")
        return "; ".join(f"{r['name']} {r['v']}" for r in rows) or "ok"

    # --- schema ------------------------------------------------------------

    def setup(self) -> None:
        for label in LABELS.values():
            self.run(f"CREATE CONSTRAINT kb_{label.lower()}_id IF NOT EXISTS "
                     f"FOR (n:{label}) REQUIRE n.id IS UNIQUE")

    def rebuild(self) -> None:
        """清空图(分批 DETACH DELETE,大图不撑爆事务)。"""
        while True:
            rows = self.run("MATCH (n) WITH n LIMIT 5000 DETACH DELETE n RETURN count(*) AS n")
            if not rows or int(rows[0]["n"]) == 0:
                break
        self.setup()

    # --- 写 ------------------------------------------------------------------

    def upsert_nodes(self, rows: Sequence[NodeRow], kb_version: str = "") -> None:
        by_kind: Dict[str, List[Dict[str, Any]]] = {}
        for r in rows:
            by_kind.setdefault(r.kind, []).append({
                "id": r.id, "name": r.name, "canonical": r.canonical or r.id,
                "aliases": list(r.aliases), "attrs": json.dumps(r.attrs, ensure_ascii=False),
                "kb_version": kb_version})
        for kind, batch in by_kind.items():
            self.run(
                f"UNWIND $rows AS r MERGE (n:{_label(kind)} {{id: r.id}}) "
                "SET n.name = r.name, n.canonical = r.canonical, n.aliases = r.aliases, "
                "    n.attrs = r.attrs, n.kb_version = r.kb_version",
                {"rows": batch})

    def upsert_edges(self, rows: Sequence[EdgeRow]) -> int:
        """按关系类型分批 MERGE;返回实际匹配到两端的边数(端点缺失的边不会静默丢——报出来)。"""
        by_rel: Dict[str, List[Dict[str, Any]]] = {}
        for r in rows:
            by_rel.setdefault(r.rel, []).append({
                "src": r.src, "dst": r.dst, "confidence": float(r.confidence),
                "source": r.source, "case_id": r.case_id, "status": r.status,
                "valid_from": r.valid_from or None, "valid_to": r.valid_to or None})
        written = 0
        for rel, batch in by_rel.items():
            rows_out = self.run(
                "UNWIND $rows AS r MATCH (a {id: r.src}), (b {id: r.dst}) "
                f"MERGE (a)-[e:{_rel(rel)} {{source: r.source}}]->(b) "
                "SET e.confidence = r.confidence, e.case_id = r.case_id, e.status = r.status, "
                "    e.valid_from = r.valid_from, e.valid_to = r.valid_to "
                "RETURN count(e) AS n",
                {"rows": batch})
            n = int(rows_out[0]["n"]) if rows_out else 0
            if n != len(batch):
                raise GraphStoreError(
                    f"关系 {rel} 有 {len(batch) - n} 条找不到端点节点(先 upsert_nodes 再写边)")
            written += n
        return written

    def delete_edges_by_source_prefix(self, prefix: str) -> int:
        rows = self.run("MATCH ()-[e]->() WHERE e.source STARTS WITH $p DELETE e RETURN count(*) AS n",
                        {"p": prefix})
        return int(rows[0]["n"]) if rows else 0

    def delete_orphans(self) -> int:
        rows = self.run("MATCH (n) WHERE NOT (n)--() DELETE n RETURN count(*) AS n")
        return int(rows[0]["n"]) if rows else 0

    # --- 读 ------------------------------------------------------------------

    def node(self, node_id: str) -> Optional[Dict[str, Any]]:
        rows = self.run("MATCH (n {id: $id}) RETURN n.id AS id, labels(n)[0] AS label, n.name AS name, "
                        "n.canonical AS canonical, n.aliases AS aliases", {"id": node_id})
        if not rows:
            return None
        r = rows[0]
        return {"id": r["id"], "kind": kind_of_label(r["label"]), "name": r["name"],
                "canonical": r["canonical"], "aliases": list(r["aliases"] or [])}

    def paths(self, symptom_ids: Sequence[str], today: str, min_confidence: float = 1.0,
              limit: int = 20) -> List[PathHit]:
        """现象 → 根因 → 处置 的完整链,只走 accepted、confidence ≥ min 且生效的边。"""
        if not symptom_ids:
            return []
        # 同一条 现象→根因→处置 被多份材料各自佐证时是多条边;这里按三元组聚合,
        # 来源合并、案例去重计数——「N 案例支持」数的就是这个。
        # status 用等号比:旧版索引写进来的边没有 status 属性(null),比较结果为 null,
        # 在 WHERE 里就是不通过 —— 拿不准就不进路径,重跑 kb.py index(整图重建)即恢复。
        valid = (_CONFIRMED + " AND ({e}.valid_to IS NULL OR {e}.valid_to > $today) "
                 "AND ({e}.valid_from IS NULL OR {e}.valid_from <= $today)")
        # 「N 案例支持」= 佐证**这条链**的边来自哪些案例(边上的 case_id),不是所有出现过该现象的案例——
        # 同一现象下两条不同根因的路径,支持数必须不一样。
        rows = self.run(
            "MATCH (s:Symptom)-[e1:CAUSED_BY]->(r:RootCause)-[e2:HANDLED_BY]->(a:Action) "
            "WHERE s.id IN $ids AND " + valid.format(e="e1") + " AND " + valid.format(e="e2") + " "
            "WITH s, r, a, collect(DISTINCT e1.source) AS s1, collect(DISTINCT e2.source) AS s2, "
            "     collect(DISTINCT e1.case_id) + collect(DISTINCT e2.case_id) AS cs, "
            "     min(CASE WHEN e1.confidence < e2.confidence THEN e1.confidence ELSE e2.confidence END) AS minc "
            "RETURN s.id AS sid, s.name AS sname, r.id AS rid, r.name AS rname, "
            "       a.id AS aid, a.name AS aname, "
            "       [c IN cs WHERE c IS NOT NULL AND c <> ''] AS cases, s1 + s2 AS sources, minc "
            "ORDER BY size(cases) DESC, sid, rid, aid LIMIT $limit",
            {"ids": list(symptom_ids), "min": float(min_confidence), "today": today, "limit": int(limit),
             "accepted": PATH_STATUS})
        return [PathHit(symptom_id=r["sid"], symptom=r["sname"], rootcause_id=r["rid"],
                        rootcause=r["rname"], action_id=r["aid"], action=r["aname"],
                        cases=tuple(dict.fromkeys(c for c in (r["cases"] or []) if c)),
                        sources=tuple(r["sources"] or []), min_confidence=float(r["minc"]))
                for r in rows]

    def neighbors(self, node_ids: Sequence[str], hops: int = 2, min_confidence: float = 1.0,
                  limit: int = 50) -> List[Neighbor]:
        """任意方向有界扩展(1..hops),每个邻居只保留最近一跳;边按 status + 置信度过滤。"""
        if not node_ids:
            return []
        hops = max(1, min(int(hops), 3))
        rows = self.run(
            f"MATCH p = (n)-[*1..{hops}]-(m) WHERE n.id IN $ids AND m.id <> n.id "
            "  AND ALL(e IN relationships(p) WHERE " + _CONFIRMED.format(e="e") + ") "
            "WITH m, min(length(p)) AS hops, "
            "     head([e IN relationships(p) | type(e)]) AS rel, "
            "     min(reduce(c = 1.0, e IN relationships(p) | CASE WHEN e.confidence < c THEN e.confidence ELSE c END)) AS conf "
            "RETURN m.id AS id, labels(m)[0] AS label, m.name AS name, hops, rel, conf "
            "ORDER BY hops, id LIMIT $limit",
            {"ids": list(node_ids), "min": float(min_confidence), "limit": int(limit), "accepted": PATH_STATUS})
        return [Neighbor(id=r["id"], kind=kind_of_label(r["label"]), name=r["name"],
                         hops=int(r["hops"]), rel=str(r["rel"]).lower(), confidence=float(r["conf"]))
                for r in rows]

    def cases_for(self, node_ids: Sequence[str], min_confidence: float = 1.0) -> Dict[str, List[str]]:
        """节点 → 直接关联(EXHIBITS/INVOLVES/REFERENCES)的案例 id。"""
        if not node_ids:
            return {}
        rows = self.run(
            "MATCH (c:Case)-[e:EXHIBITS|INVOLVES|REFERENCES]->(n) WHERE n.id IN $ids AND "
            + _CONFIRMED.format(e="e") + " RETURN n.id AS id, collect(DISTINCT c.id) AS cases",
            {"ids": list(node_ids), "min": float(min_confidence), "accepted": PATH_STATUS})
        return {r["id"]: list(r["cases"] or []) for r in rows}

    def clauses_for(self, node_ids: Sequence[str], min_confidence: float = 1.0) -> Dict[str, List[str]]:
        """对象/GUC → 约束它的条款 id(CONSTRAINS)。"""
        if not node_ids:
            return {}
        rows = self.run(
            "MATCH (k:Clause)-[e:CONSTRAINS]->(n) WHERE n.id IN $ids AND "
            + _CONFIRMED.format(e="e") + " RETURN n.id AS id, collect(DISTINCT k.id) AS clauses",
            {"ids": list(node_ids), "min": float(min_confidence), "accepted": PATH_STATUS})
        return {r["id"]: list(r["clauses"] or []) for r in rows}

    def counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for r in self.run("MATCH (n) RETURN labels(n)[0] AS label, count(*) AS n"):
            out[f"nodes.{kind_of_label(r['label'])}"] = int(r["n"])
        legacy = 0
        for r in self.run("MATCH ()-[e]->() RETURN type(e) AS t, count(*) AS n, "
                          "sum(CASE WHEN e.status = $accepted AND e.confidence >= 1.0 THEN 1 ELSE 0 END) AS confirmed, "
                          "sum(CASE WHEN e.status IS NULL THEN 1 ELSE 0 END) AS legacy",
                          {"accepted": PATH_STATUS}):
            out[f"edges.{str(r['t']).lower()}"] = int(r["n"])
            out[f"edges.{str(r['t']).lower()}.confirmed"] = int(r["confirmed"])
            legacy += int(r.get("legacy") or 0)
        out["edges"] = sum(v for k, v in out.items() if k.startswith("edges.") and not k.endswith(".confirmed"))
        out["edges.confirmed"] = sum(v for k, v in out.items() if k.endswith(".confirmed"))
        if legacy:
            # 旧版索引写的边不带 status,一律不进路径;状态行据此提示「重建索引后恢复」,
            # 否则路径小节突然全是「无」,看起来像客户没有先例。
            out["edges.no_status"] = legacy
        return out

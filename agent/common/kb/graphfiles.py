"""<kb>/graph/*.yaml(强类型三元组)与 graph/canonical.yaml(实体别名表)—— 解析、归一、校验。

三元组是图的真相;Neo4j 里的节点/边由它重建。每条边:
    src {kind, name[, canonical]} —rel→ dst {kind, name[, canonical]}
    confidence(0..1)· source(出处,必填)· case(案例 ID)· valid_from / valid_to · status
status:accepted(用户在选择列表里接受)| candidate(模型提出,未确认)| rejected。
只有 accepted 且 confidence=1.0 的边进「本行历史路径」;candidate 只作"未确认"参照;rejected 不入库。

节点 id = canonical:先查别名表,查不到就按 kind + 归一化名字生成。同一个对象的不同叫法
(核心账户表 / core_acct / CORE_ACCT)必须落到同一个 id——否则图里就是三个孤岛。
"""
from __future__ import annotations

import pathlib
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import yaml

from . import store_graph as sg
from . import text as kbtext

STATUSES = ("accepted", "candidate", "rejected")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_IDENT_RE = re.compile(r"^[a-z0-9_][a-z0-9_.:]*$")


@dataclass(frozen=True)
class NodeRef:
    kind: str
    name: str
    canonical: str          # 节点 id

    @property
    def id(self) -> str:
        return self.canonical


@dataclass(frozen=True)
class Triple:
    src: NodeRef
    rel: str
    dst: NodeRef
    confidence: float
    source: str
    case_id: str = ""
    valid_from: str = ""
    valid_to: str = ""
    status: str = "accepted"
    file: str = ""

    @property
    def in_path(self) -> bool:
        """能否进入确定性推理(路径小节)。组图后边上带着 status,路径查询按同一口径(sg.confirmed)。"""
        return sg.confirmed(self.status, self.confidence)


# ---------------------------------------------------------------- canonical

def slug(name: str) -> str:
    """归一化名字:全角→半角、小写、空白折叠为 _;中文保留。"""
    s = kbtext.normalize(name)
    s = re.sub(r"[\s]+", "_", s)
    s = re.sub(r"[^\w.:\-一-鿿]+", "", s)
    return s.strip("_") or "unnamed"


def canonical_id(kind: str, name: str, aliases: Dict[str, str]) -> str:
    """别名表命中 → 表里的 id;否则 kind:slug(name)。标识符类(对象/GUC/等待事件)直接用小写原名。"""
    key = kbtext.normalize(name)
    if key in aliases:
        return aliases[key]
    if kind in ("object", "guc", "wait_event", "error", "component") and _IDENT_RE.match(key):
        return f"{kind}:{key}"
    return f"{kind}:{slug(name)}"


def load_canonical(kb: pathlib.Path) -> Tuple[Dict[str, str], List[Tuple[str, str]]]:
    """canonical.yaml:`<id>: [别名, …]` → {normalize(别名): id}。"""
    path = kb / "graph" / "canonical.yaml"
    findings: List[Tuple[str, str]] = []
    if not path.is_file():
        return {}, findings
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        return {}, [("error", f"graph/canonical.yaml 解析失败:{exc}")]
    if not isinstance(data, dict):
        return {}, [("error", "graph/canonical.yaml 顶层必须是 `<节点id>: [别名…]` 映射")]
    aliases: Dict[str, str] = {}
    for cid, names in data.items():
        cid = str(cid)
        if ":" not in cid or cid.split(":", 1)[0] not in sg.NODE_KINDS:
            findings.append(("error", f"graph/canonical.yaml: 节点 id `{cid}` 必须形如 <kind>:<名>,kind ∈ {'/'.join(sg.NODE_KINDS)}"))
            continue
        for n in (names or []):
            key = kbtext.normalize(str(n))
            if key in aliases and aliases[key] != cid:
                findings.append(("error", f"graph/canonical.yaml: 别名「{n}」同时指向 {aliases[key]} 与 {cid}"))
                continue
            aliases[key] = cid
        aliases.setdefault(kbtext.normalize(cid.split(":", 1)[1]), cid)
    return aliases, findings


# ---------------------------------------------------------------- triples

def _node_ref(raw, aliases: Dict[str, str], where: str, errors: List[str]) -> Optional[NodeRef]:
    if not isinstance(raw, dict):
        errors.append(f"{where}: 节点必须是 {{kind, name}} 映射")
        return None
    kind = str(raw.get("kind") or "").strip()
    name = str(raw.get("name") or "").strip()
    if kind not in sg.NODE_KINDS:
        errors.append(f"{where}: kind `{kind}` 非法,只能是 {'/'.join(sg.NODE_KINDS)}")
        return None
    if not name:
        errors.append(f"{where}: 节点缺 name")
        return None
    canonical = str(raw.get("canonical") or "").strip() or canonical_id(kind, name, aliases)
    if not canonical.startswith(kind + ":"):
        errors.append(f"{where}: canonical `{canonical}` 的前缀必须是 `{kind}:`")
        return None
    return NodeRef(kind=kind, name=name, canonical=canonical)


def parse_triples(data, aliases: Dict[str, str], file: str) -> Tuple[List[Triple], List[str]]:
    errors: List[str] = []
    out: List[Triple] = []
    if data is None:
        return out, errors
    if not isinstance(data, list):
        return out, [f"{file}: 顶层必须是三元组列表"]
    for i, raw in enumerate(data):
        where = f"{file}[{i}]"
        if not isinstance(raw, dict):
            errors.append(f"{where}: 三元组必须是键值映射")
            continue
        src = _node_ref(raw.get("src"), aliases, where + ".src", errors)
        dst = _node_ref(raw.get("dst"), aliases, where + ".dst", errors)
        rel = str(raw.get("rel") or "").strip()
        if rel not in sg.REL_TYPES:
            errors.append(f"{where}: rel `{rel}` 非法,只能是 {'/'.join(sg.REL_TYPES)}(没有共现边)")
        try:
            confidence = float(raw.get("confidence"))
        except (TypeError, ValueError):
            errors.append(f"{where}: confidence 缺失或不是数字")
            confidence = -1.0
        if not 0.0 <= confidence <= 1.0:
            errors.append(f"{where}: confidence {confidence} 必须在 0..1")
        source = str(raw.get("source") or "").strip()
        if not source:
            errors.append(f"{where}: 缺 source(出处必填,没出处的边不入图)")
        # 没写 status 按 accepted:这是三元组文件一直以来的格式约定(Triple 的缺省也是它)——
        # kb.py apply 落盘时三种状态都显式写,省略 status 的只有用户自己手写进来的边,视同他确认过。
        # 派生索引(Neo4j)里没有 status 的边则相反,按候选处理,见 store_graph.EdgeRow。
        status = str(raw.get("status") or "accepted").strip()
        if status not in STATUSES:
            errors.append(f"{where}: status `{status}` 只能是 {'/'.join(STATUSES)}")
        vf = str(raw.get("valid_from") or "").strip()
        vt = str(raw.get("valid_to") or "").strip()
        for label, v in (("valid_from", vf), ("valid_to", vt)):
            if v and not _DATE_RE.match(v):
                errors.append(f"{where}: {label} `{v}` 不是 YYYY-MM-DD")
        if src is None or dst is None or rel not in sg.REL_TYPES or confidence < 0 or not source or status not in STATUSES:
            continue
        out.append(Triple(src=src, rel=rel, dst=dst, confidence=confidence, source=source,
                          case_id=str(raw.get("case") or "").strip(), valid_from=vf, valid_to=vt,
                          status=status, file=file))
    return out, errors


def iter_graph_files(kb: pathlib.Path) -> List[pathlib.Path]:
    root = kb / "graph"
    if not root.is_dir():
        return []
    return sorted(p for p in root.rglob("*.yaml") if p.is_file() and p.name != "canonical.yaml")


def load_triples(kb: pathlib.Path, case_ids: Optional[Sequence[str]] = None
                 ) -> Tuple[List[Triple], List[Tuple[str, str]]]:
    """全部三元组 + 发现。case_ids 给了就校验 case 引用存在。"""
    aliases, findings = load_canonical(kb)
    triples: List[Triple] = []
    seen: Dict[Tuple[str, str, str, str], str] = {}
    known = set(case_ids or [])
    for path in iter_graph_files(kb):
        rel = str(path.relative_to(kb))
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except UnicodeDecodeError:
            findings.append(("error", f"{rel}: 不是 UTF-8 编码"))
            continue
        except (OSError, yaml.YAMLError) as exc:
            findings.append(("error", f"{rel}: 解析失败:{exc}"))
            continue
        parsed, errors = parse_triples(data, aliases, rel)
        findings += [("error", e) for e in errors]
        for t in parsed:
            key = (t.src.id, t.rel, t.dst.id, t.source)
            if key in seen:
                findings.append(("error", f"{rel}: 边 {t.src.id} -{t.rel}-> {t.dst.id}(出处 {t.source})与 {seen[key]} 重复"))
                continue
            seen[key] = rel
            if case_ids is not None and t.case_id and t.case_id not in known:
                findings.append(("error", f"{rel}: 边引用的案例 `{t.case_id}` 不存在"))
                continue
            if t.status == "candidate":
                findings.append(("warn", f"{rel}: 候选边 {t.src.name} -{t.rel}-> {t.dst.name} 尚未确认(不入路径)"))
            triples.append(t)
    return triples, findings


def nodes_of(triples: Sequence[Triple]) -> Dict[str, NodeRef]:
    """去重后的节点表(id → NodeRef);同一 id 多个 name 时取第一个出现的。"""
    out: Dict[str, NodeRef] = {}
    for t in triples:
        for ref in (t.src, t.dst):
            out.setdefault(ref.id, ref)
    return out

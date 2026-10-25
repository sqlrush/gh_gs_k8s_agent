"""混合检索编排 —— 按"发现"查库:词法 ∥ 向量 ∥ 图扩展 → RRF 融合 → 分类型 top-k → 阈值。

给两类调用方:
  · 诊断 skill 的脚本:`from_findings(findings)`——确定性入口,每条 finding 一组引用;
  · 会话里的模型:`from_text(q)`——纯问答路径。
两者都**永不抛异常**:知识库不可达 / 没配 / 没索引,都变成 KbStatus.attached=False + 原因,
由 render.py 写成「知识库未接入(原因)」——skill 本身照常。

检索层的两条纪律:
  · 向量给入口(像什么),图给链路(现象→根因→处置,只走 accepted、confidence=1 且生效的边);
  · 分数阈值——低于门槛整类返回空,由渲染层明写「无」,不凑数。
"""
from __future__ import annotations

import datetime
import os
import pathlib
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from . import config as kbconfig
from . import store_files as sf
from . import store_graph as sg
from . import store as spg
from . import store_sqlite as ssq
from . import text as kbtext
from .embed import Embedder, EmbedError
from .indexer import read_state

# 三种模式,按环境里实际连得上什么自动感知(状态行「模式:」):
# 「向量库」= <kb>/index/store.sqlite(SQLite + sqlite-vec,2026-10-23 起;原高斯 / PG 向量库下线)
MODE_FULL = "向量库+图库"          # SQLite 索引 + Neo4j 都在
MODE_PG_FILEGRAPH = "向量库+图文件"  # SQLite 索引在,Neo4j 没配或不可达 → 路径改走 graph/*.yaml
MODE_PG_ONLY = "向量库"            # SQLite 索引在,没有任何图(只在测试替身里出现)
MODE_FILES = "文件"                # 还没建索引 / 打不开 → 整个检索走 <kb>/ 文件(词法 + 图文件)

RRF_K = 60
TIME_BUDGET_S = 3.0
_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*(?:[.:][a-z0-9_]+)*$")


@dataclass(frozen=True)
class Ref:
    id: str                 # 文档 id,如 case:S1-… / rule:GS-…
    kind: str
    title: str
    score: float
    snippet: str = ""
    source: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)
    sections: Dict[str, str] = field(default_factory=dict)

    @property
    def short_id(self) -> str:
        return self.id.split(":", 1)[1] if ":" in self.id else self.id


@dataclass(frozen=True)
class PathRef:
    symptom: str
    rootcause: str
    action: str
    cases: Tuple[str, ...]
    sources: Tuple[str, ...]

    @property
    def support(self) -> int:
        return len(self.cases)


@dataclass(frozen=True)
class FindingRefs:
    key: str                # finding code 或 "q"
    label: str              # 渲染标题,如 "🟠 VAC_FREQ(…)"
    query: str
    clauses: Tuple[Ref, ...] = ()
    cases: Tuple[Ref, ...] = ()
    paths: Tuple[PathRef, ...] = ()
    raws: Tuple[Ref, ...] = ()
    guides: Tuple[Ref, ...] = ()
    notes: Tuple[str, ...] = ()
    typed: Tuple[Ref, ...] = ()        # 手册 / 缺陷 / 基线 / 产品文档 / Wiki / 画像(meta["_applies"] 是适用性标签)
    topics: Tuple[str, ...] = ()       # 这条发现 / 这个问题落在哪些专题(L1 的入口)
    hints: Tuple[str, ...] = ()        # 专题入口 + 本行确认过的候选原因(layers.hint_lines)

    @property
    def empty(self) -> bool:
        return not (self.clauses or self.cases or self.paths or self.raws or self.guides or self.typed)


@dataclass(frozen=True)
class KbStatus:
    attached: bool
    reason: str = ""            # 未接入的原因;文件模式下是「为什么退到了文件」
    version: str = ""
    counts: Dict[str, int] = field(default_factory=dict)
    vector: str = "未启用"
    graph: str = "未配置"
    mode: str = ""              # MODE_* 之一;未接入为空
    l0: Tuple[str, ...] = ()    # L0 摘要:本实例画像、八类知识各多少(render 写在状态行下面)


@dataclass(frozen=True)
class QueryResult:
    status: KbStatus
    items: Tuple[FindingRefs, ...] = ()
    elapsed_ms: int = 0


# ---------------------------------------------------------------- fusion (pure)

def rrf(lists: Sequence[Sequence[spg.Hit]], k: int = RRF_K) -> Dict[str, Tuple[float, spg.Hit, float, float]]:
    """id → (归一融合分, 代表 hit, 词法原始分, 向量原始分)。

    归一:除以"在每个列表都排第一"的分,两列表下单列表第一 = 0.5,单列表模式第一 = 1.0。
    """
    active = [lst for lst in lists if lst is not None]
    if not active:
        return {}
    fused: Dict[str, float] = {}
    best: Dict[str, spg.Hit] = {}
    raw = {}
    for li, lst in enumerate(active):
        for rank, hit in enumerate(lst, 1):
            fused[hit.id] = fused.get(hit.id, 0.0) + 1.0 / (k + rank)
            if hit.id not in best or hit.score > best[hit.id].score:
                best[hit.id] = hit
            lex, vec = raw.get(hit.id, (0.0, 0.0))
            if li == 0:
                lex = max(lex, hit.score)
            else:
                vec = max(vec, hit.score)
            raw[hit.id] = (lex, vec)
    top = len(active) / (k + 1)
    return {i: (s / top, best[i], raw[i][0], raw[i][1]) for i, s in fused.items()}


# 英文泛词:出现在几乎每条证据/案例里,命中它们不能证明相关(pg_stat_* 拆出来的碎片尤其多)
_GENERIC_WORDS = frozenset({
    "database", "databases", "table", "tables", "index", "indexes", "event", "events", "stat", "stats",
    "user", "users", "global", "snap", "snapshot", "query", "queries", "count", "total", "time", "local",
    "value", "values", "name", "null", "select", "update", "insert", "delete", "where", "from", "size",
    "read", "write", "hit", "high", "heavy", "class", "wait", "lock", "scan", "plan", "cpu", "io", "db",
})


def is_strong_token(tok: str) -> bool:
    """能单独证明相关性的 token:标识符/代码(带 _ . :)或 ≥6 字符的词,且不是英文泛词;
    二元组、数字、单位、pg_stat 拆出来的 stat/user 之类都不算。中文二元组单个不算,
    但连成 ≥4 字的短语算——那在 cjk_phrase_hits 里数。"""
    if tok.isdigit() or tok in _GENERIC_WORDS:
        return False
    return any(ch in tok for ch in "_.:") or len(tok) >= 6


_PHRASE_MIN_BIGRAMS = 3      # 首尾相接的 3 个二元组 = 一个 ≥4 字的中文短语


def _is_cjk_bigram(tok: str) -> bool:
    return len(tok) == 2 and tok[0] >= "㐀"


def cjk_phrase_hits(query_tokens: Sequence[str], have: Set[str]) -> int:
    """查询里首尾相接(前一个的尾字 = 后一个的首字)、且都落在正文里的二元组串,每串 ≥3 个算一个短语命中。
    没有标识符的中文提问(「有没有空闲事务持锁阻塞会话的案例」)只有二元组,单个永远够不上强 token,
    文件模式又没有向量兜底,以前这种提问对任何案例都是「无相似案例」;连成 ≥4 字的短语足以证明相关,
    而散落的「等待」「占」连不成串,照旧挡在门外。"""
    hits, run, prev = 0, 0, ""
    for tok in query_tokens:
        matched = _is_cjk_bigram(tok) and tok in have
        if matched and (run == 0 or prev[1] == tok[0]):
            run += 1
        else:
            if run >= _PHRASE_MIN_BIGRAMS:
                hits += 1
            run = 1 if matched else 0
        prev = tok
    return hits + (1 if run >= _PHRASE_MIN_BIGRAMS else 0)


@dataclass(frozen=True)
class Relevance:
    strong: int          # 命中的强 token 数
    matched: int         # 命中的查询 token 数
    coverage: float      # matched / 查询 token 数


def relevance(query_tokens: Sequence[str], text: str) -> Relevance:
    """纯函数,可解释:查询 token 里有多少落在正文里、其中多少是强 token。"""
    q = [t for t in dict.fromkeys(query_tokens) if not t.isdigit()]
    if not q:
        return Relevance(0, 0, 0.0)
    have = set(kbtext.tokenize(text))
    matched = [t for t in q if t in have]
    strong = sum(1 for t in matched if is_strong_token(t)) + cjk_phrase_hits(q, have)
    return Relevance(strong, len(matched), len(matched) / len(q))


SHORT_QUERY_TOKENS = 3


def _short_query(tokens: Sequence[str]) -> bool:
    """很短的提问:去掉停用词后不超过 3 个词,且至少一个不是单字 / 纯数字。"""
    q = [t for t in dict.fromkeys(tokens) if not t.isdigit()]
    return 0 < len(q) <= SHORT_QUERY_TOKENS and any(len(t) >= 2 for t in q)


def identifiers_in(text: str) -> List[str]:
    """finding 证据里像对象名 / GUC / 等待事件的整词,用来直接查图里的约束条款。"""
    out = []
    for tok in kbtext.tokenize(text):
        if _IDENT_RE.match(tok) and ("." in tok or "_" in tok or ":" in tok) and tok not in out:
            out.append(tok)
    return out


# ---------------------------------------------------------------- session

def open_store(cfg: kbconfig.KbConfig) -> Tuple[Optional[ssq.SqliteStore], str]:
    """(SQLite 索引, 打不开的原因)。还没建 / 打不开 / 格式旧了 都不抛,原因带回去,调用方退到文件模式。"""
    try:
        store = ssq.SqliteStore.open_readonly(ssq.store_path(cfg.kb_dir), dims=cfg.embeddings.dims)
    except spg.StoreError as exc:
        return None, str(exc)
    try:
        if not store.has_index():
            store.close()
            return None, "索引是旧格式或不完整(要重建索引)"
    except spg.StoreError as exc:
        store.close()
        return None, str(exc)
    return store, ""


def _graph_files(kb: pathlib.Path) -> Optional[sf.FileGraph]:
    try:
        return sf.load_graph(kb)
    except Exception:            # 文件树本身坏到组不出图:没有图,状态行会写出来
        return None


def open_graph(cfg: kbconfig.KbConfig, lookup: Callable[[str], str]) -> Tuple[Optional[object], str]:
    """(图, 退到图文件的原因)。Neo4j 通就用 Neo4j(原因为空);没配或不可达就退到 graph/*.yaml 组的内存图。"""
    if cfg.store.graph is None:
        return _graph_files(cfg.kb_dir), "kb.yaml 未配置 store.graph"
    try:
        gpw = lookup(cfg.store.graph.credential)
        graph = sg.GraphStore(cfg.store.graph.url, cfg.store.graph.user, gpw,
                              database=cfg.store.graph.database, timeout_s=5.0)
        graph.ping()
        return graph, ""
    except Exception as exc:
        return _graph_files(cfg.kb_dir), f"Neo4j 不可用:{exc}"


class KbSession:
    """一次调用内共用的连接。open() 永不抛:连不上就退到文件模式;目录不存在 / kb.yaml 无效才是 attached=False。"""

    def __init__(self, cfg: Optional[kbconfig.KbConfig], pg: Optional["ssq.SqliteStore"],
                 graph: Optional[sg.GraphStore], embedder: Optional[Embedder],
                 reason: str = "", notes: Sequence[str] = (), graph_reason: str = ""):
        self.cfg = cfg
        self.pg = pg            # 检索用的存储:SqliteStore,或文件模式的 FileStore(名字沿用旧的 pg)
        self.graph = graph
        self.embedder = embedder
        self.reason = reason
        self.graph_reason = graph_reason
        self.notes: List[str] = list(notes)
        self._vector_failed = False
        self.alias = ""                 # 当前连接别名(诊断 skill 的会话),用来匹配环境画像
        self._catalog = None
        self._triples = None
        # 官方文档集:各自的索引、单独的候选池(几万块官方文档不许把贵行的规范与案例挤出候选)
        self.docsets: List[Tuple[Any, "ssq.SqliteStore", bool]] = []

    # --- 八类知识 / 分层(条目元数据从文件现读;拿不到就当没有,检索照常)---------------

    def catalog(self):
        if self._catalog is None and self.cfg is not None:
            from . import catalog as kbcat
            try:
                self._catalog = kbcat.load(self.cfg.kb_dir)
            except Exception as exc:                      # noqa: BLE001  坏文件不许炸掉 skill
                self.notes.append(f"条目目录读取失败:{exc}")
                self._catalog = kbcat.Catalog(entries=(), topics=())
        return self._catalog

    def triples(self):
        if self._triples is None and self.cfg is not None:
            from . import graphfiles as gf
            try:
                cases = [e.id for e in self.catalog().entries if e.type == "case"]
                self._triples, _ = gf.load_triples(self.cfg.kb_dir, case_ids=cases)
            except Exception:                             # noqa: BLE001
                self._triples = []
        return self._triples or []

    def profile(self):
        """当前连接别名对应的画像条目(没有别名 / 没登记就是 None)。"""
        if not self.alias or self.catalog() is None:
            return None
        from . import catalog as kbcat
        return kbcat.profile_for_alias(self.catalog(), self.alias)

    def topics_for_finding(self, f: Any) -> Tuple[str, ...]:
        from . import topics as kbtopics
        cat = self.catalog()
        if cat is None:
            return ()
        return kbtopics.finding_topics(str(getattr(f, "code", "") or ""), str(getattr(f, "dimension", "") or ""),
                                       finding_query_text(f), cat.topics)

    def topics_for_text(self, q: str) -> Tuple[str, ...]:
        from . import topics as kbtopics
        cat = self.catalog()
        return kbtopics.infer_topics(q, cat.topics) if cat is not None else ()

    def l0_lines(self) -> Tuple[str, ...]:
        """L0 摘要两行:本实例画像(或没登记)、八类知识各多少。为 0 的类型如实说未登记。"""
        from . import catalog as kbcat
        cat = self.catalog()
        if cat is None:
            return ()
        lines: List[str] = []
        prof = self.profile()
        if prof is not None:
            m = prof.meta
            lines.append(f"> 本实例({self.alias}):{prof.id}《{prof.title}》{m.get('product', '')} {m.get('version', '')}"
                         f" · {m.get('deployment', '部署形态未写')}")
        elif self.alias:
            lines.append(f"> 本实例({self.alias}):未登记环境画像——条目的产品系列 / 版本适用性按「未知」处理")
        c = kbcat.type_counts(cat)
        official = len(cat.from_docset())
        parts = [("规范", c.get("norm", 0) + c.get("errata", 0)), ("案例", c.get("case", 0)), ("画像", c.get("profile", 0)),
                 ("基线", c.get("baseline", 0)), ("缺陷", c.get("defect", 0)), ("手册", c.get("runbook", 0)),
                 ("产品文档", c.get("product", 0)), ("Wiki", c.get("wiki", 0))]
        lines.append("> 已登记:" + " · ".join(f"{k} {v}" + (f"(含官方文档集 {official})" if k == "产品文档" and official else "")
                                             for k, v in parts)
                     + "(为 0 的如实说「本行未登记」)· 总索引 `kb.py overview`")
        if official and self.cfg is not None and self.cfg.docset_applicability_unknown:
            lines.append("> ⚠ 测试开关开着:官方文档集对所有实例都按「适用性未知」处理(只许在测试环境)")
        return tuple(lines)

    def _open_docsets(self) -> None:
        """装好的官方文档集逐套只读打开;向量只在该套是用同一个嵌入模型算的时候用(否则只走关键词,说明原因)。"""
        if self.cfg is None:
            return
        from . import docset as kbdocset
        for ds in kbdocset.installed(self.cfg.kb_dir):
            if not ds.index_path.is_file():
                self.notes.append(f"官方文档集 {ds.code}:还没建索引,检索不到")
                continue
            try:
                store = ssq.SqliteStore.open_readonly(ds.index_path, dims=self.cfg.embeddings.dims)
            except spg.StoreError as exc:
                self.notes.append(f"官方文档集 {ds.code}:{exc}")
                continue
            model = store.meta_get("embed_model") or ""
            vec_ok = bool(self.embedder is not None and store.capabilities().vector and model
                          and model == self.embedder.model)
            if self.embedder is not None and model and model != self.embedder.model:
                self.notes.append(f"官方文档集 {ds.code}:向量是 {model} 算的,本库配的是 {self.embedder.model},"
                                  "这套文档本次只走关键词(导入环境跑 `kb.py index` 会按本库模型重算)")
            self.docsets.append((ds, store, vec_ok))

    @classmethod
    def open(cls, kb_dir: Optional[pathlib.Path] = None,
             password_lookup: Optional[Callable[[str], str]] = None) -> "KbSession":
        kb = pathlib.Path(kb_dir) if kb_dir else kbconfig.resolve_kb_dir(None)
        if not kb.is_dir():
            return cls(None, None, None, None, reason=f"知识库目录不存在:{kb}")
        try:
            cfg = kbconfig.load(kb)
        except kbconfig.KbConfigError as exc:
            return cls(None, None, None, None, reason=f"kb.yaml 无效:{exc}")
        lookup = password_lookup or _default_password_lookup
        pg, why = open_store(cfg)
        if pg is None:
            return cls._open_files(cfg, why)

        graph, graph_reason = open_graph(cfg, lookup)
        notes: List[str] = []
        embedder: Optional[Embedder] = None
        caps = pg.capabilities()
        if caps.vector:
            try:
                embedder = Embedder.from_config(cfg)
                if embedder is None:
                    notes.append("向量:未启用(kb.yaml 未配 embeddings)")
            except (EmbedError, kbconfig.KbConfigError) as exc:
                notes.append(f"向量:未启用({exc})")
        else:
            notes.append("向量:未启用(%s)" % (pg.vector_note or "sqlite-vec 不可用"))
        session = cls(cfg, pg, graph, embedder, notes=notes, graph_reason=graph_reason)
        session._open_docsets()
        return session

    @classmethod
    def _open_files(cls, cfg: kbconfig.KbConfig, reason: str) -> "KbSession":
        """文件模式:<kb>/ 文件上做词法检索,路径走 graph/*.yaml;原因写进状态行,不藏。"""
        try:
            fs = sf.FileStore.load(cfg.kb_dir)
        except Exception as exc:
            return cls(cfg, None, None, None, reason=f"{reason};文件模式加载失败:{exc}")
        from . import docset as kbdocset
        has_docsets = bool(kbdocset.installed(cfg.kb_dir))
        if not fs.has_index() and not has_docsets:
            # 装完还没导入任何材料:整节写「未接入(空库)」就够了,不要给每条发现挂一串「无」。
            return cls(cfg, None, None, None, reason=f"知识库是空的(还没导入任何规范或工单;{reason})")
        # 客户库还没索引、但标准知识库(官方文档集)装好了:官方文档照样能查,向量按配置用(客户池走文件模式)
        embedder: Optional[Embedder] = None
        if has_docsets:
            try:
                embedder = Embedder.from_config(cfg)
            except (EmbedError, kbconfig.KbConfigError):
                embedder = None
        session = cls(cfg, fs, fs.graph, embedder, reason=reason, notes=["向量:未启用(文件模式无向量)"])
        session._open_docsets()
        return session

    def close(self) -> None:
        if self.pg is not None:
            self.pg.close()
        for _ds, store, _v in self.docsets:
            store.close()
        self.docsets = []

    @property
    def attached(self) -> bool:
        return self.pg is not None

    @property
    def mode(self) -> str:
        if self.pg is None:
            return ""
        if isinstance(self.pg, sf.FileStore):
            return MODE_FILES
        if self.graph is None:
            return MODE_PG_ONLY
        return MODE_PG_FILEGRAPH if isinstance(self.graph, sf.FileGraph) else MODE_FULL

    def status(self) -> KbStatus:
        if not self.attached or self.cfg is None or self.pg is None:
            return KbStatus(attached=False, reason=self.reason or "未接入")
        counts = self.pg.counts()
        state = read_state(self.cfg.kb_dir) or {}
        version = ""
        try:
            version = (self.cfg.kb_dir / "VERSION").read_text(encoding="utf-8").strip()
        except OSError:
            pass
        caps = self.pg.capabilities()
        if caps.vector and self.embedder is not None:
            total, done = self.pg.coverage()
            engine = self.pg.meta_get("vector_engine") or "vector"
            pct = f"{done * 100 // total}%" if total else "0%"
            vector = f"{engine}(覆盖 {pct})" + ("·本次超时未用" if self._vector_failed else "")
        else:
            vector = next((n.split(":", 1)[1] for n in self.notes if n.startswith("向量:")), "未启用")
        mode = self.mode
        return KbStatus(attached=True, reason=self.reason if mode == MODE_FILES else "",
                        version=version or str(state.get("kb_version", "")),
                        counts=counts, vector=vector, graph=self._graph_label(), mode=mode, l0=self.l0_lines())

    def _graph_label(self) -> str:
        if self.graph is None:
            # 「加载不出来」和「没配」是两回事,别显示成同一句
            if self.graph_reason:
                return self.graph_reason
            return next((n.split(":", 1)[1] for n in self.notes if n.startswith("图:")), "未配置")
        if isinstance(self.graph, sf.FileGraph):
            n = self.graph.counts().get("edges.confirmed", 0)
            return f"图文件 {n} 条已确认边" + (f"({self.graph_reason})" if self.graph_reason else "")
        try:
            c = self.graph.counts()
        except sg.GraphStoreError as exc:
            return f"不可用({exc})"
        label = f"Neo4j {c.get('edges.confirmed', 0)} 条已确认边"
        if c.get("edges.no_status"):
            # 旧版索引写的边不带 status,不进路径(拿不准就不进);不点明的话,路径小节全是「无」却看不出原因。
            # 措辞不带命令:状态行会进诊断报告,而诊断环境里没有导入命令。
            label += f"({c['edges.no_status']} 条边是旧版索引写的、不带 status,不进路径;重建索引后恢复)"
        return label

    # --- 检索 -------------------------------------------------------------

    def search(self, key: str, label: str, q_text: str, objects: Sequence[str] = (),
               free_text: bool = False, topics: Sequence[str] = ()) -> FindingRefs:
        """free_text:用户自己问的话(kb.py query --q)。很短的问题(「死锁」「连接数」)没有强 token,
        按诊断发现那套绝对门槛一条都过不了;这时词全部命中就算相关(见 _relevant)。"""
        if not self.attached or self.cfg is None or self.pg is None:
            return FindingRefs(key=key, label=label, query=q_text)
        th = self.cfg.thresholds
        started = time.monotonic()
        notes: List[str] = []
        idents = identifiers_in(q_text + " " + " ".join(objects))
        tokens = kbtext.query_tokens(q_text + " " + " ".join(objects))

        emb: Optional[List[float]] = None
        if self.embedder is not None:
            try:
                emb = self.embedder.embed_one(q_text, timeout_s=self.cfg.embeddings.query_timeout_s)
            except Exception:
                emb = None
            if emb is None:
                self._vector_failed = True
                notes.append("向量本次超时/失败,只用词法 + 图")

        lex = self._safe(lambda: self.pg.search_chunks_lexical(tokens, k=30)) or []
        vec = self._safe(lambda: self.pg.search_chunks_vector(emb, k=30)) if emb is not None else None
        fused = rrf([lex, vec] if vec is not None else [lex])
        by_kind: Dict[str, List[Tuple[float, spg.Hit]]] = {}
        seen_docs: Dict[str, float] = {}
        # 两遍:先绝对门槛(强 token + 覆盖),再在幸存者里取同类最高分做相对门槛——
        # 参照分若来自一个高分但不相关的命中,真正相关的反而会被相对门槛砍掉。
        survivors = [(score, hit, lex_s, vec_s) for (score, hit, lex_s, vec_s) in fused.values()
                     if self._relevant(tokens, hit, lex_s, vec_s, free_text)]
        best_lex: Dict[str, float] = {}
        for _score, hit, lex_s, _vec_s in survivors:
            best_lex[hit.kind] = max(best_lex.get(hit.kind, 0.0), lex_s)
        for score, hit, lex_s, vec_s in survivors:
            if vec_s < th.vector_min and lex_s < best_lex.get(hit.kind, 0.0) * th.relative_floor:
                continue
            if score > seen_docs.get(hit.doc_id, -1.0):
                seen_docs[hit.doc_id] = score
                by_kind.setdefault(hit.kind, [])
                by_kind[hit.kind] = [(s, h) for s, h in by_kind[hit.kind] if h.doc_id != hit.doc_id] + [(score, hit)]
        for score, hit in self._docset_hits(tokens, emb, free_text, th):
            by_kind.setdefault("product", []).append((score, hit))
        for k in by_kind:
            by_kind[k].sort(key=lambda p: (-p[0], p[1].doc_id))

        clauses = self._refs(by_kind.get("rule", []), th.clause, th.top_clause)
        cases = self._refs(by_kind.get("case", []), th.case, th.top_case, with_sections=True)
        raws = self._refs(by_kind.get("raw", []), th.chunk, th.top_raw)
        # 修正(errata)排在指南前面:库内优先级 修正 > 条款 > 指南,检索结果的顺序要和契约一致
        errata_first = sorted(by_kind.get("guide", []) + by_kind.get("errata", []),
                              key=lambda p: (p[1].kind != "errata", -p[0], p[1].doc_id))
        guides = self._refs(errata_first, th.chunk, th.top_guide)
        typed = self._typed_refs(by_kind, th, free_text)

        # 图:现象节点 → 路径;对象 → 条款
        paths: List[PathRef] = []
        if self.graph is not None and time.monotonic() - started < TIME_BUDGET_S:
            nl = self._safe(lambda: self.pg.search_nodes_lexical(tokens, k=10, kinds=["symptom"])) or []
            nv = self._safe(lambda: self.pg.search_nodes_vector(emb, k=10, kinds=["symptom"])) if emb is not None else None
            nfused = rrf([nl, nv] if nv is not None else [nl])
            node_ok = [(i, s, h, l, v) for i, (s, h, l, v) in nfused.items()
                       if self._relevant(tokens, h, l, v, free_text)]
            best_node = max((l for (_i, _s, _h, l, _v) in node_ok), default=0.0)
            symptom_ids = [i for i, s, h, l, v in sorted(node_ok, key=lambda p: -p[1])
                           if s >= th.symptom and (v >= th.vector_min or l >= best_node * th.node_relative_floor)][:3]
            today = datetime.date.today().isoformat()
            hits = self._safe(lambda: self.graph.paths(symptom_ids, today=today)) or []
            # 按现象的匹配名次排,同一现象内再按案例支持数——不能让弱匹配的现象靠案例多挤到前面
            rank = {sid: i for i, sid in enumerate(symptom_ids)}
            hits = sorted(hits, key=lambda p: (rank.get(p.symptom_id, 99), -len(p.cases)))
            for p in hits[:th.top_path]:
                paths.append(PathRef(symptom=p.symptom, rootcause=p.rootcause, action=p.action,
                                     cases=tuple(c.split(":", 1)[1] if c.startswith("case:") else c for c in p.cases),
                                     sources=p.sources))
            if idents:
                node_ids = [f"{kind}:{i}" for i in idents for kind in ("object", "guc", "wait_event", "error")]
                clause_map = self._safe(lambda: self.graph.clauses_for(node_ids)) or {}
                extra = [cid for ids in clause_map.values() for cid in ids]
                have = {c.id for c in clauses}
                extra_docs = self._safe(lambda: self.pg.docs_by_ids(
                    [f"rule:{c.split(':', 1)[1]}" for c in extra if f"rule:{c.split(':', 1)[1]}" not in have])) or {}
                for doc in extra_docs.values():
                    clauses = clauses + (Ref(id=doc.id, kind="rule", title=doc.title, score=1.0,
                                             source=doc.source, meta=doc.meta),)
        elif self.graph is None:
            pass

        hints: Tuple[str, ...] = ()
        if topics and self.catalog() is not None:
            from . import layers as kblayers
            try:
                hints = tuple(kblayers.hint_lines(self.catalog(), self.triples(), list(topics)))
            except Exception:                             # noqa: BLE001  提示是附加的,失败不影响引用
                hints = ()
        refs = FindingRefs(key=key, label=label, query=q_text, clauses=tuple(clauses), cases=tuple(cases),
                           paths=tuple(paths), raws=tuple(raws), guides=tuple(guides), notes=tuple(notes),
                           typed=typed, topics=tuple(topics), hints=hints)
        if not refs.clauses and not refs.cases and not refs.paths:
            self._log_miss(key, q_text)
        return refs

    def _docset_hits(self, tokens: Sequence[str], emb: Optional[List[float]], free_text: bool, th: Any
                     ) -> List[Tuple[float, spg.Hit]]:
        """官方文档集的候选池:每套单独取「关键词前 12 + 向量前 12」,门槛与客户池同一套,相对门槛只在本池里比。"""
        out: List[Tuple[float, spg.Hit]] = []
        for ds, store, vec_ok in self.docsets:
            lex = self._safe(lambda: store.search_chunks_lexical(tokens, k=12)) or []
            vec = self._safe(lambda: store.search_chunks_vector(emb, k=12)) if emb is not None and vec_ok else None
            fused = rrf([lex, vec] if vec is not None else [lex])
            survivors = [(s, h, ls, vs) for (s, h, ls, vs) in fused.values() if self._relevant(tokens, h, ls, vs, free_text)]
            best = max((ls for _s, _h, ls, _vs in survivors), default=0.0)
            seen: Dict[str, Tuple[float, spg.Hit]] = {}
            for s, h, ls, vs in survivors:
                if vs < th.vector_min and ls < best * th.relative_floor:
                    continue
                if s > seen.get(h.doc_id, (-1.0, h))[0]:
                    seen[h.doc_id] = (s, h)
            out += list(seen.values())
        return out

    def _typed_refs(self, by_kind: Dict[str, List[Tuple[float, spg.Hit]]], th: Any, free_text: bool) -> Tuple[Ref, ...]:
        """六类类型化条目:每类最多 top_typed 条;按本实例画像标适用性——不适用的降到同类末尾、写明理由,不丢。
        别的系统的基线与本实例无关,直接不出;画像只在纯问答里出(诊断时 L0 已经写了本实例的画像)。"""
        from . import versions as kbversions
        prof = self.profile()
        pmeta = dict(prof.meta) if prof is not None else None
        out: List[Ref] = []
        for kind in spg.TYPED_KINDS:
            if kind == "profile" and not free_text:
                continue
            refs = list(self._refs(by_kind.get(kind, []), th.chunk, th.top_typed + 2))
            labeled = []
            for r in refs:
                meta = dict(r.meta)
                if kind == "baseline" and pmeta and meta.get("system") and meta.get("system") != pmeta.get("system"):
                    continue
                if kind == "defect":
                    meta["_applies"] = kbversions.defect_verdict(meta, pmeta)
                    verdict = kbversions.Verdict(kbversions.APPLICABLE)
                elif meta.get("docset") and self.cfg is not None and self.cfg.docset_applicability_unknown:
                    verdict = kbversions.Verdict(kbversions.UNKNOWN, "测试开关:官方文档适用性按未知处理")
                    meta["_applies"] = verdict.label
                else:
                    verdict = kbversions.judge(meta, pmeta)
                    meta["_applies"] = verdict.label
                labeled.append((verdict.state == kbversions.NOT_APPLICABLE, Ref(**{**r.__dict__, "meta": meta})))
            labeled.sort(key=lambda p: p[0])
            out += [r for _bad, r in labeled[:th.top_typed]]
        return tuple(out)

    def _relevant(self, tokens: Sequence[str], hit: spg.Hit, lex_s: float, vec_s: float,
                  free_text: bool = False) -> bool:
        """绝对门槛:向量够像直接过;否则词法原始分要过下限,且命中至少一个强 token 并命中 ≥2 个
        查询 token(或覆盖率达标)。相对门槛(同类最高分的比例)由调用方在幸存者里做。

        用户自己问的短问题(≤ SHORT_QUERY_TOKENS 个词、至少一个中文二元组或整词)词全部命中也算:
        「死锁」「连接数」「主从切换」没有强 token,不开向量时(客户现场的默认)以前一条都查不到。
        诊断发现生成的查询是长证据,不走这一条 —— 泛词误召回的防线照旧。"""
        th = self.cfg.thresholds if self.cfg else kbconfig.Thresholds()
        if vec_s >= th.vector_min:
            return True
        if lex_s < th.lexical_min:
            return False
        r = relevance(tokens, f"{hit.title} {hit.content}")
        if free_text and _short_query(tokens) and r.coverage >= 1.0:
            return True
        return r.strong >= 1 and (r.matched >= 2 or r.coverage >= th.min_coverage)

    def _refs(self, ranked: Sequence[Tuple[float, spg.Hit]], floor: float, top: int,
              with_sections: bool = False) -> Tuple[Ref, ...]:
        out: List[Ref] = []
        for score, hit in ranked:
            if score < floor or len(out) >= top:
                break
            sections = self._safe(lambda: self.pg.doc_sections(hit.doc_id)) if with_sections else None
            snippet = hit.content.split("\n", 1)[1] if " › " in hit.content.split("\n", 1)[0] else hit.content
            out.append(Ref(id=hit.doc_id, kind=hit.kind, title=hit.title, score=round(score, 3),
                           snippet=snippet[:160], source=hit.source, meta=hit.meta, sections=sections or {}))
        return tuple(out)

    def _safe(self, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except (spg.StoreError, sg.GraphStoreError, OSError) as exc:
            self.notes.append(f"检索降级:{exc}")
            return None

    def _log_miss(self, key: str, q_text: str) -> None:
        if self.cfg is None:
            return
        try:
            path = self.cfg.kb_dir / "index" / "misses.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(f"{datetime.date.today().isoformat()}\t{key}\t{q_text[:80]}\n")
        except OSError:
            pass


def _default_password_lookup(name: str) -> str:
    from ..credential import load_secret
    return load_secret(name)


def result_to_dict(result: QueryResult) -> Dict[str, Any]:
    """JSON 形态(skill 的 --json 输出与 kb.py query --json 共用),随报告落盘可事后核对。"""
    return {
        "status": dict(result.status.__dict__),
        "elapsed_ms": result.elapsed_ms,
        "items": [{
            "key": it.key, "label": it.label,
            "clauses": [{"id": r.id, "title": r.title, "score": r.score, "source": r.source} for r in it.clauses],
            "cases": [{"id": r.id, "title": r.title, "score": r.score, "source": r.source,
                       "conclusion": r.meta.get("conclusion"), "action": r.sections.get("处置", "")} for r in it.cases],
            "paths": [dict(p.__dict__) for p in it.paths],
            "raws": [{"id": r.id, "title": r.title, "score": r.score} for r in it.raws],
            "typed": [{"id": r.id, "kind": r.kind, "title": r.title, "score": r.score,
                       "applies": r.meta.get("_applies", "")} for r in it.typed],
            "topics": list(it.topics),
            "notes": list(it.notes),
        } for it in result.items],
    }


@dataclass(frozen=True)
class SimpleFinding:
    """给没有 common.finding.Finding 的 skill(如 sqltune)组装检索项用的最小形状。"""
    code: str
    dimension: str
    metric: str
    value: str
    evidence: str
    severity: str = ""
    threshold: str = ""


def section_for(findings: Sequence[Any], kb_dir: Optional[pathlib.Path] = None,
                alias: str = "") -> Tuple[str, Dict[str, Any]]:
    """skill 脚本一行接入:返回 (markdown 小节, JSON 字典)。永不抛——知识库出任何问题都是「未接入(原因)」。
    alias:当前连接别名(不传就从本进程的登录会话里取),用来匹配环境画像。"""
    from . import render as kbrender
    try:
        result = from_findings(findings, kb_dir=kb_dir, alias=alias or current_alias())
    except Exception as exc:                           # 兜底,理论上 from_findings 已不抛
        result = QueryResult(status=KbStatus(attached=False, reason=f"知识库检索异常:{exc}"))
    return kbrender.render_section(result), result_to_dict(result)


# ---------------------------------------------------------------- entry points

def finding_query_text(f: Any) -> str:
    """Finding → 检索文本:code + 指标 + 证据 + 维度(共用 common.finding.Finding 的字段名)。"""
    parts = [str(getattr(f, "code", "") or ""), str(getattr(f, "dimension", "") or ""),
             str(getattr(f, "metric", "") or ""), str(getattr(f, "evidence", "") or "")]
    return " ".join(p for p in parts if p)


def finding_label(f: Any) -> str:
    sev = getattr(f, "severity", None)
    label = sev.label() if hasattr(sev, "label") else str(sev or "")
    metric = str(getattr(f, "metric", "") or "")
    value = str(getattr(f, "value", "") or "")
    detail = f"{metric} = {value}" if metric and value else metric or value
    return f"{label} {getattr(f, 'code', '')}" + (f"({detail})" if detail else "")


def current_alias() -> str:
    """本进程登录会话的连接别名(skill 脚本里 --session 选中的那个);取不到就空串,永不抛。"""
    try:
        from .. import session as gsession
        conn = gsession.current()
        if conn is None:
            return ""
        # api(中间件)模式下会话里的连接名是按实例地址生成的(10-0-0-9-postgres),用户、画像认的是登录时的
        # 别名(og5)——按部署时登记的别名表反查回去(2026-10-24 测试环境:画像登记了 og5,诊断时没匹配上)
        if getattr(conn, "data_ip", ""):
            from .. import config as gconfig
            for a in gconfig.instance_aliases():
                if a.ip == conn.data_ip and (not conn.database or a.database == conn.database):
                    return a.name
        return str(getattr(conn, "name", "") or "")
    except Exception:                                  # noqa: BLE001  没登录、旧安装都不影响知识库
        return ""


def from_findings(findings: Sequence[Any], kb_dir: Optional[pathlib.Path] = None,
                  session: Optional[KbSession] = None, alias: str = "") -> QueryResult:
    """诊断 skill 的确定性入口:每条 finding 一组引用;永不抛。"""
    started = time.monotonic()
    own = session is None
    sess = session or KbSession.open(kb_dir)
    sess.alias = alias or sess.alias
    try:
        items = tuple(sess.search(str(getattr(f, "code", "") or f"#{i}"), finding_label(f),
                                  finding_query_text(f), objects=(), topics=sess.topics_for_finding(f))
                      for i, f in enumerate(findings)) if sess.attached else ()
        status = sess.status()
    except Exception as exc:                      # 兜底:知识库任何异常都不许炸掉 skill
        status = KbStatus(attached=False, reason=f"知识库检索异常:{exc}")
        items = ()
    finally:
        if own:
            sess.close()
    return QueryResult(status=status, items=items, elapsed_ms=int((time.monotonic() - started) * 1000))


def from_text(q: str, kb_dir: Optional[pathlib.Path] = None,
              session: Optional[KbSession] = None, alias: str = "") -> QueryResult:
    """纯问答入口(kb.py query --q [--alias og5])。"""
    started = time.monotonic()
    own = session is None
    sess = session or KbSession.open(kb_dir)
    sess.alias = alias or sess.alias
    try:
        items = (sess.search("q:" + q[:24], q[:60], q, free_text=True, topics=sess.topics_for_text(q)),) \
            if sess.attached else ()
        status = sess.status()
    except Exception as exc:
        status = KbStatus(attached=False, reason=f"知识库检索异常:{exc}")
        items = ()
    finally:
        if own:
            sess.close()
    return QueryResult(status=status, items=items, elapsed_ms=int((time.monotonic() - started) * 1000))

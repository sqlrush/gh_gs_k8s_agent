"""存储层共用的数据形状 —— 索引器写、检索读、两种存储(SQLite 索引 / 文件模式)都用这一套。

原先放在 store_pg.py 里;2026-10-23 高斯 DataVec / PG 向量库下线,改成知识库目录里的一个 SQLite 文件
(store_sqlite.py),形状本身不变,挪到这个中立的模块。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Sequence

DOC_KINDS = ("rule", "guide", "errata", "case", "raw")
# 2026-10-24 八类知识:六类类型化条目各自一个 kind(= types.TYPED 的类型代码)
TYPED_KINDS = ("runbook", "defect", "baseline", "product", "wiki", "profile")
ALL_KINDS = DOC_KINDS + TYPED_KINDS


class StoreError(Exception):
    """存储层的用户可读错误(打不开、建表失败、SQL 报错)。"""


@dataclass(frozen=True)
class Capabilities:
    engine: str            # sqlite | files
    version: str
    vector: bool           # 能做向量检索(sqlite-vec 加载成功)
    hnsw: bool             # 有近似索引(SQLite 版恒 False:精确检索,我方规模够用)
    dims: int


@dataclass(frozen=True)
class DocRow:
    id: str
    kind: str              # rule | guide | errata | case | raw | 六类类型化条目(TYPED_KINDS)
    title: str
    source: str
    version: str
    meta: Dict[str, Any] = field(default_factory=dict)
    content_hash: str = ""


@dataclass(frozen=True)
class ChunkRow:
    id: str
    doc_id: str
    seq: int
    section: str
    content: str
    content_hash: str
    tokens: Sequence[str]
    signal_tokens: Sequence[str] = ()       # 复发标志等,BM25 里按 SIGNAL_WEIGHT 加权
    embedding: Optional[Sequence[float]] = None
    novec: bool = False                     # 只建关键词、不算向量(官方文档的错误码块:按码号 / 报错原文精确命中)


@dataclass(frozen=True)
class NodeVectorRow:
    node_id: str
    kind: str
    name: str
    tokens: Sequence[str]
    signal_tokens: Sequence[str] = ()
    signals: str = ""                       # 复发标志原文,查询时做相关度门槛用
    embedding: Optional[Sequence[float]] = None


@dataclass(frozen=True)
class Hit:
    id: str
    score: float           # 0..1,越大越相关(向量 = 1 - 余弦距离;词法 = BM25 / 理想分)
    kind: str
    doc_id: str = ""
    seq: int = 0
    section: str = ""
    content: str = ""
    title: str = ""
    source: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)

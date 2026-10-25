"""专题(L1 的划分):定义、文字 → 专题、诊断发现 → 专题。

专题按「现场怎么描述问题」划分,不按 skill 划分;每个专题带:
  · synonyms —— 现场的叫法,同时是 L1 的「症状与同义词」与文字归类的依据;
  · codes    —— 诊断发现 code 的正则(LOCK_* / CONN_* …),发现按 code 直接落到专题;
  · dimensions —— 发现的维度名片段(health / wdr 的 DIM_*),code 没对上时按维度。
默认 12 个写在这里;客户在 <kb>/topics/_taxonomy.yaml 里增删改(同 slug 覆盖,新 slug 追加,
`disabled: true` 去掉一个默认专题)。taxonomy 文件坏了不炸,退回默认并带一条 warning。
"""
from __future__ import annotations

import pathlib
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Sequence, Tuple

import yaml

from . import text as kbtext

TAXONOMY_FILE = "_taxonomy.yaml"
OTHER = "other"                    # 没归进任何专题的条目;L0 里单列一行,不让它们「消失」
OTHER_NAME = "未归类"


@dataclass(frozen=True)
class Topic:
    slug: str
    name: str
    synonyms: Tuple[str, ...] = ()
    codes: Tuple[str, ...] = ()
    dimensions: Tuple[str, ...] = ()


DEFAULT_TOPICS: Tuple[Topic, ...] = (
    Topic("conn", "连接与会话", ("连接数", "连接打满", "连不上", "会话堆积", "活跃会话", "会话数", "max_connections",
                              "连接池", "too many clients"),
          (r"^CONN", r"ACTIVE_HIGH", r"SESSION"), ("Connections", "L3 会话")),
    Topic("lock", "锁与阻塞", ("锁等待", "阻塞", "死锁", "卡住", "持锁", "行锁", "表锁", "阻塞链", "根会话",
                              "idle in transaction", "空闲事务", "lockwait", "deadlock"),
          (r"LOCK", r"DEADLOCK", r"BLOCK", r"IDLE_XACT", r"LONG_XACT", r"XACT"),
          ("Transaction Locks", "Blocking", "Long & Idle Transactions", "Transactions / Concurrency")),
    Topic("sql", "慢 SQL 与执行计划", ("慢 SQL", "慢sql", "变慢", "跑不完", "执行计划", "计划变了", "全表扫描",
                                    "索引失效", "seq scan", "nestloop", "Top SQL"),
          (r"SLOWSQL", r"TOPSQL", r"PLAN", r"SEQ_?SCAN", r"INDEX_UNUSED", r"INDEX_INVALID"),
          ("Slow SQL", "Top SQL", "Window Top SQL", "L4 SQL", "plan", "sqltune", "sqlreview")),
    Topic("proc", "存储过程", ("存储过程", "存过", "过程超时", "批量过程", "procedure", "函数慢"),
          (r"^PROC",), ("proctune",)),
    Topic("wait", "等待事件", ("等待事件", "IO 等待", "LWLock", "轻量锁", "等待占比", "DB Time", "wait event"),
          (r"^WAIT", r"LWLOCK", r"DBTIME", r"WAIT_CLASS"),
          ("Wait Events", "Window Waits", "Lightweight Locks", "DB Time")),
    Topic("wal", "WAL 与日志", ("WAL", "日志暴涨", "刷盘慢", "归档堆积", "xlog", "checkpoint", "检查点", "redo",
                               "WALWriteLock", "WALFlushWait"),
          (r"WAL", r"CKPT", r"ARCH", r"XLOG", r"REDO", r"CHECKPOINT"),
          ("Checkpoint / WAL / Archiving", "Checkpoint / BgWriter / Redo")),
    Topic("mem", "内存", ("内存高", "内存泄漏", "OOM", "下盘", "临时文件", "work_mem", "缓存命中率", "dynamic_used_memory"),
          (r"^MEM", r"CACHE", r"SPILL", r"TEMP", r"BUFFER_HIT"),
          ("Cache / Memory", "L1 实例内存", "L2 内存上下文", "L5 算子")),
    Topic("vacuum", "膨胀与回收", ("膨胀", "回收不掉", "死元组", "vacuum", "autovacuum", "统计过期", "统计信息",
                                 "analyze", "freeze", "xid"),
          (r"VAC", r"BLOAT", r"DEAD", r"STALE_STAT", r"FREEZE", r"XID"), ("Dead Tuples & Bloat",)),
    Topic("repl", "主备与复制", ("备机延迟", "主备", "主从", "复制延迟", "切换", "standby", "replay"),
          (r"REPL", r"STANDBY"), ("Replication / Standby",)),
    Topic("param", "参数配置", ("参数调整", "调参", "参数默认值", "GUC", "配置项"),
          (r"PARAM", r"GUC", r"CONFIG"), ("L6 配置面",)),
    Topic("space", "空间与存储", ("磁盘满", "表空间", "空间不足", "存储空间", "数据文件"),
          (r"SPACE", r"DISK", r"STORAGE", r"TBS"), ("Storage Usage", "File IO")),
    Topic("batch", "批量与作业", ("跑批", "批量", "日终", "作业超时", "批量窗口", "定时任务", "job"), (), ()),
)


def _as_tuple(v: Any) -> Tuple[str, ...]:
    if v in (None, ""):
        return ()
    if isinstance(v, (list, tuple)):
        return tuple(str(x).strip() for x in v if str(x).strip())
    return (str(v).strip(),)


def load_taxonomy(kb: pathlib.Path) -> Tuple[List[Topic], List[str]]:
    """(专题列表, warnings)。<kb>/topics/_taxonomy.yaml 覆盖默认;文件坏了退回默认。"""
    topics: Dict[str, Topic] = {t.slug: t for t in DEFAULT_TOPICS}
    path = kb / "topics" / TAXONOMY_FILE
    if not path.is_file():
        return list(topics.values()), []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        return list(topics.values()), [f"topics/{TAXONOMY_FILE} 读不了,用默认专题:{exc}"]
    if not isinstance(data, list):
        return list(topics.values()), [f"topics/{TAXONOMY_FILE} 顶层必须是列表,用默认专题"]
    warns: List[str] = []
    for i, raw in enumerate(data):
        slug = str((raw or {}).get("slug") or "").strip() if isinstance(raw, dict) else ""
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,23}", slug) or slug == OTHER:
            warns.append(f"topics/{TAXONOMY_FILE}[{i}]: slug 要求小写字母开头的 2–24 位字母数字下划线,且不能是 {OTHER}")
            continue
        if raw.get("disabled"):
            topics.pop(slug, None)
            continue
        base = topics.get(slug, Topic(slug, slug))
        topics[slug] = Topic(
            slug=slug, name=str(raw.get("name") or base.name),
            synonyms=_as_tuple(raw["synonyms"]) if "synonyms" in raw else base.synonyms,
            codes=_as_tuple(raw["codes"]) if "codes" in raw else base.codes,
            dimensions=_as_tuple(raw["dimensions"]) if "dimensions" in raw else base.dimensions)
    return list(topics.values()), warns


def taxonomy_yaml(topics: Sequence[Topic]) -> str:
    """`kb.py topic --init` 写出的可编辑文件(带默认内容)。"""
    rows = [{"slug": t.slug, "name": t.name, "synonyms": list(t.synonyms), "codes": list(t.codes),
             "dimensions": list(t.dimensions)} for t in topics]
    head = ("# 专题定义(L1 的划分)。同 slug 覆盖默认,新 slug 追加,disabled: true 去掉一个默认专题。\n"
            "# synonyms:现场的叫法(也用于把条目、提问归到专题);codes:诊断发现 code 的正则;dimensions:发现维度名片段。\n")
    return head + yaml.safe_dump(rows, allow_unicode=True, sort_keys=False)


def _norm(s: str) -> str:
    return kbtext.normalize(s).replace(" ", "")


def infer_topics(text: str, topics: Sequence[Topic], limit: int = 3) -> Tuple[str, ...]:
    """文字 → 专题:同义词(去空格、忽略大小写)在文字里出现的次数排序,取前 limit 个。"""
    hay = _norm(text)
    if not hay:
        return ()
    scored = []
    for t in topics:
        n = sum(1 for s in (t.name,) + t.synonyms if s and _norm(s) in hay)
        if n:
            scored.append((-n, t.slug))
    return tuple(slug for _n, slug in sorted(scored)[:limit])


def finding_topics(code: str, dimension: str, text: str, topics: Sequence[Topic]) -> Tuple[str, ...]:
    """诊断发现 → 专题。code 规则优先,其次维度名,最后按证据文字的同义词。"""
    out: List[str] = []
    up = (code or "").upper()
    for t in topics:
        if up and any(re.search(rx, up) for rx in t.codes):
            out.append(t.slug)
    if not out and dimension:
        for t in topics:
            if any(d and d.lower() in dimension.lower() for d in t.dimensions):
                out.append(t.slug)
    if not out:
        out = list(infer_topics(text, topics, limit=2))
    return tuple(dict.fromkeys(out))


def by_slug(topics: Iterable[Topic]) -> Dict[str, Topic]:
    return {t.slug: t for t in topics}


def name_of(slug: str, topics: Iterable[Topic]) -> str:
    if slug == OTHER:
        return OTHER_NAME
    return by_slug(topics).get(slug, Topic(slug, slug)).name

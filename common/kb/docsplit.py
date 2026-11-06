"""官方文档的切分:目录节点 + 页面 → 产品文档条目(纯函数,不碰知识库)。

切法(2026-10-25 与用户确认):
  · 一页一条,标题 = 页面标题,带完整目录路径;只有目录、没有正文的导航页不成条(只进目录导航)。
  · GUC 参数页(配置运行参数 › GUC参数说明 › <类>)一参一条,按「参数说明 / 参数类型 / … / 设置不当的风险与影响」分小节。
  · 错误码页(GAUSS-xxxxx 区间)按页成条;检索块按错误码切、只建关键词(见 indexer)。
  · 页内 h4 小标题成为小节;摘要 ≤ 200 字,结论在前(参数取「参数说明」首句 + 默认值,视图取首句,其余跳过小标题取首句)。
专题:先按目录名(锁管理 → lock …),系统表 / 视图按名字(PG_LOCKS → lock),再按标题与正文的同义词推断;
计划里的人工覆盖(topic-map.yaml 的 override)优先于以上全部。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from . import topics as kbtopics

SUMMARY_MAX = 200
KIND_GUC, KIND_PAGE, KIND_ERRCODE = "guc", "page", "errcode"
GUC_FIELDS = ("参数说明", "参数类型", "参数单位", "取值范围", "默认值", "设置方式", "设置建议", "设置不当的风险与影响")
ERRCODE_RE = re.compile(r"^(GAUSS-\d{5})\s*[:：]", re.M)
NAV_MIN_CHARS = 80

# 目录名 → 专题(整段相等才算;从路径末端往前找,第一个命中的为准)
PATH_TOPICS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("锁管理", ("lock",)), ("Lock", ("lock",)), ("事务", ("lock",)), ("Transaction", ("lock",)),
    ("连接和认证", ("conn",)), ("连接池参数", ("conn",)), ("线程池", ("conn",)), ("客户端连接缺省设置", ("conn",)),
    ("Session/Thread", ("conn",)),
    ("资源消耗", ("mem",)), ("Memory", ("mem",)), ("负载管理", ("mem",)), ("Global SysCache参数", ("mem",)),
    ("Global PLsql Cache特性参数", ("mem", "proc")), ("Workload Manager", ("mem",)),
    ("预写式日志", ("wal",)), ("RTO & RPO", ("repl",)), ("双机复制", ("repl",)), ("双数据库实例复制参数", ("repl",)),
    ("DCF参数设置", ("repl",)), ("备机数据修复", ("repl",)), ("逻辑解码", ("repl",)),
    ("查询规划", ("sql",)), ("SPM计划管理", ("sql",)), ("SQL调优指南", ("sql",)), ("Operator", ("sql",)),
    ("Global Plancache", ("sql",)), ("DBE_SQL_UTIL Schema", ("sql",)), ("Query", ("sql",)),
    ("自动清理", ("vacuum",)), ("VACUUM", ("vacuum",)),
    ("等待事件", ("wait",)), ("Wait Events", ("wait",)), ("运行时统计", ("wait",)), ("系统性能快照", ("wait",)),
    ("WDR Snapshot Schema", ("wait",)), ("黑匣子相关参数", ("wait",)), ("Cache/IO", ("wait",)),
    ("存储过程", ("proc",)), ("自治事务", ("proc",)), ("用户自定义函数", ("proc",)),
    ("DBE_PLDEBUGGER Schema", ("proc",)), ("DBE_PLDEVELOPER", ("proc",)),
    ("Undo", ("space",)), ("闪回相关参数", ("space",)), ("回滚相关参数", ("space",)), ("段页式存储", ("space",)),
    ("File", ("space",)), ("OLTP表压缩", ("space",)), ("数据生命周期管理-OLTP表压缩", ("space",)),
    ("数据导入导出", ("batch",)), ("定时任务", ("batch",)),
    ("在线创建索引", ("change",)), ("在线DDL", ("change",)), ("升级参数", ("change",)), ("高危操作", ("change",)),
)
# 系统表 / 视图 / 函数按名字归(大写匹配);华为把 PG_STAT_ACTIVITY 放在「VACUUM」小节下,按目录会归错
TITLE_RULES: Tuple[Tuple[str, str], ...] = (
    (r"LOCK|DEADLOCK", "lock"), (r"WAIT", "wait"),
    (r"STAT_ACTIVITY|SESSION|CONNECT|THREAD|BACKEND|POOLER", "conn"),
    (r"MEMORY|MEMCTX|_MEM|SHARED_MEM|CACHE", "mem"), (r"XLOG|WAL|CHECKPOINT|CKPT|REDO|ARCHIV", "wal"),
    (r"REPL|STANDBY|SLOT|DCF|SYNC_STATE|RTO|RPO", "repl"), (r"STATEMENT|SQL|PLAN|QUERY|OPERATOR|HINT|EXPLAIN", "sql"),
    (r"VACUUM|DEAD|BLOAT|ANALYZE|STAT_ALL_TABLES|STAT_USER_TABLES|AUTOVAC", "vacuum"),
    (r"TABLESPACE|SPACE|FILE|SEGMENT|UNDO|DISK|TOAST", "space"), (r"PROC|FUNCTION|PLSQL|PLDEBUG|PACKAGE", "proc"),
    (r"JOB|SCHEDULER|COPY|LOAD", "batch"), (r"GUC|SETTING|CONFIG|PARAM", "param"),
)
_CATALOG_CHAPTERS = ("系统表和系统视图", "Schema")
_GUC_PARENT = ("配置运行参数", "GUC参数说明")
_GUC_SKIP_PAGES = ("GUC参数说明", "GUC使用说明")
_HEADINGISH = re.compile(r"^[一-鿿A-Za-z0-9 /、()（）-]{1,12}$")


@dataclass(frozen=True)
class DocItem:
    doc_key: str                            # 页面文件名;GUC 参数再加 #参数名。重导靠它对上同一条
    page: str
    path: Tuple[str, ...]                   # 页面在目录里的完整路径(GUC 参数是参数所在页的路径)
    title: str
    kind: str                               # guc / page / errcode
    summary: str
    sections: Tuple[Tuple[str, str], ...]   # (小节名, markdown 正文)
    topics: Tuple[str, ...] = ()
    summary_by: str = "script"              # script / model

    @property
    def chars(self) -> int:
        return sum(len(b) for _n, b in self.sections)

    def text(self) -> str:
        return "\n".join(b for _n, b in self.sections)


# ---------------------------------------------------------------- 摘要

def _clean(text: str) -> str:
    text = re.sub(r"`{3}.*?`{3}", " ", text, flags=re.S)
    text = re.sub(r"^\|.*$", " ", text, flags=re.M)               # 表格行不进摘要
    text = re.sub(r"^#+\s.*$", " ", text, flags=re.M)             # 小标题不进摘要
    text = re.sub(r"[*`>]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def first_sentences(text: str, limit: int = SUMMARY_MAX, enough: int = 60) -> str:
    """跳过「问题描述」「概述」这种小标题行,取正文前一两句,够 enough 字就停。"""
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln and not _HEADINGISH.match(ln.lstrip("- ")) and not ln.startswith(("|", "#", "```"))]
    body = _clean("\n".join(lines))
    out = ""
    for sent in re.split(r"(?<=[。；！？!?])", body):
        sent = sent.strip()
        if not sent:
            continue
        if out and len(out) + len(sent) > limit:
            break
        out += sent
        if len(out) >= enough:
            break
    return (out or body)[:limit].strip()


def guc_fields(text: str) -> Dict[str, str]:
    """「参数说明:…参数类型:…默认值:…」→ 字段表。没有的字段不出。"""
    flat = re.sub(r"\s+", " ", text)
    out: Dict[str, str] = {}
    for i, name in enumerate(GUC_FIELDS):
        nxt = "|".join(GUC_FIELDS[i + 1:]) or r"$^"
        m = re.search(rf"{name}[：:]\s*(.+?)(?=\s*(?:{nxt})[：:]|$)", flat)
        if m:
            out[name] = m.group(1).strip()
    return out


def guc_summary(name: str, fields: Dict[str, str], fallback: str) -> str:
    desc = fields.get("参数说明", "")
    first = re.split(r"(?<=。)", desc, maxsplit=1)[0] if desc else first_sentences(fallback, 120)
    dflt = fields.get("默认值", "")
    dflt = re.split(r"(?<=。)", dflt, maxsplit=1)[0].rstrip("。") if dflt else ""
    s = f"{name}:{first}" + (f"默认值 {dflt[:60]}。" if dflt else "")
    return s[:SUMMARY_MAX]


# ---------------------------------------------------------------- 专题

def topics_for(path: Sequence[str], title: str, text: str, kind: str,
               taxonomy: Sequence[kbtopics.Topic], limit: int = 3) -> Tuple[str, ...]:
    found: List[str] = []
    known = {t.slug for t in taxonomy}
    if any(c in path for c in _CATALOG_CHAPTERS):
        up = title.upper()
        found = list(dict.fromkeys(slug for pat, slug in TITLE_RULES if re.search(pat, up)))[:2]
    if not found:
        for seg in reversed(path):
            hits = [t for key, ts in PATH_TOPICS if seg == key for t in ts]
            if hits:
                found = list(dict.fromkeys(hits))
                break
    if not found and kind != KIND_ERRCODE:
        up = title.upper()
        found = list(dict.fromkeys(slug for pat, slug in TITLE_RULES if re.search(pat, up)))[:2]
    if not found and kind != KIND_ERRCODE:
        found = list(kbtopics.infer_topics(f"{title} {_clean(text)[:300]}", taxonomy, limit=2))
    if kind == KIND_GUC and "param" not in found:
        found.append("param")
    return tuple(t for t in found if t in known)[:limit]


# ---------------------------------------------------------------- 切分

def is_guc_page(path: Sequence[str]) -> bool:
    return all(p in path for p in _GUC_PARENT) and path[-1] not in _GUC_SKIP_PAGES


def is_errcode_page(path: Sequence[str]) -> bool:
    return bool(re.match(r"GAUSS-\d{5}", path[-1] if path else ""))


def _h4_sections(lines: Sequence[str]) -> List[Tuple[str, List[str]]]:
    """按 `### ` 切成 (小节名, 行);第一个 ### 之前的叫「正文」。"""
    out: List[Tuple[str, List[str]]] = [("正文", [])]
    for ln in lines:
        if ln.startswith("### "):
            out.append((ln[4:].strip(), []))
        else:
            out[-1][1].append(ln)
    return [(n, b) for n, b in out if b or n != "正文"]


def split_page(page: str, path: Sequence[str], lines: Sequence[str],
               taxonomy: Sequence[kbtopics.Topic]) -> List[DocItem]:
    """一页 → 0..n 条。导航页返回 []。"""
    path = tuple(path)
    body = [ln for ln in lines if not ln.startswith("# ")]
    if len(re.sub(r"\s", "", "".join(body))) < NAV_MIN_CHARS:
        return []
    title = path[-1]
    if is_guc_page(path):
        return _split_guc(page, path, body, taxonomy)
    if is_errcode_page(path):
        text = "\n".join(body)
        n = len(ERRCODE_RE.findall(text))
        summary = f"错误码 {title}:共 {n} 个(报错原文、SQLSTATE、CAUSE 原因、ACTION 处理);按错误码号或报错原文检索。"
        return [DocItem(page, page, path, title, KIND_ERRCODE, summary[:SUMMARY_MAX], (("正文", text),),
                        topics_for(path, title, text, KIND_ERRCODE, taxonomy))]
    secs = tuple((n, "\n".join(b).strip()) for n, b in _h4_sections(body))
    text = "\n".join(b for _n, b in secs)
    return [DocItem(page, page, path, title, KIND_PAGE, first_sentences(text), secs,
                    topics_for(path, title, text, KIND_PAGE, taxonomy))]


def _split_guc(page: str, path: Tuple[str, ...], body: Sequence[str],
               taxonomy: Sequence[kbtopics.Topic]) -> List[DocItem]:
    parts = _h4_sections(body)
    intro = "\n".join(parts[0][1]).strip() if parts and parts[0][0] == "正文" else ""
    params = [(n, b) for n, b in parts if n != "正文"]
    out: List[DocItem] = []
    for name, lines in params:
        text = "\n".join(lines).strip()
        fields = guc_fields(text)
        secs = tuple((k, v) for k, v in fields.items()) or (("正文", text),)
        out.append(DocItem(f"{page}#{name}", page, path, f"{name}(GUC 参数 · {path[-1]})", KIND_GUC,
                           guc_summary(name, fields, text), secs,
                           topics_for(path, name, text, KIND_GUC, taxonomy)))
    if len(re.sub(r"\s", "", intro)) >= 200 or not params:
        out.insert(0, DocItem(page, page, path, path[-1], KIND_PAGE, first_sentences(intro or "\n".join(body)),
                              (("正文", intro or "\n".join(body)),),
                              topics_for(path, path[-1], intro, KIND_PAGE, taxonomy)))
    return out


def errcode_blocks(text: str) -> List[Tuple[str, str]]:
    """错误码页正文 → [(GAUSS-xxxxx, 这一码的几行)]:检索块按码切。"""
    marks = [(m.start(), m.group(1)) for m in ERRCODE_RE.finditer(text)]
    out = []
    for i, (pos, code) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        out.append((code, text[pos:end].strip()))
    return out


def find_errcode(text: str, needle: str) -> List[str]:
    """--grep:错误码号精确匹配,否则按子串(不分大小写)找含它的码块。"""
    blocks = errcode_blocks(text)
    exact = [b for c, b in blocks if c.upper() == needle.upper()]
    return exact or [b for _c, b in blocks if needle.lower() in b.lower()]


def apply_override(items: Iterable[DocItem], overrides: Sequence[Tuple[Tuple[str, ...], Tuple[str, ...]]]
                   ) -> List[DocItem]:
    """计划里的人工覆盖:路径前缀匹配最长的那条说了算(覆盖为空元组 = 不归任何专题)。"""
    out = []
    ranked = sorted(overrides, key=lambda o: -len(o[0]))
    for it in items:
        hit = next((tops for prefix, tops in ranked if tuple(it.path[:len(prefix)]) == tuple(prefix)), None)
        out.append(it if hit is None else DocItem(**{**it.__dict__, "topics": tuple(hit)}))
    return out


def chapter_of(path: Sequence[str], depth: int = 3) -> Tuple[str, ...]:
    return tuple(path[:depth])


__all__ = ["DocItem", "KIND_GUC", "KIND_PAGE", "KIND_ERRCODE", "GUC_FIELDS", "SUMMARY_MAX", "split_page",
           "first_sentences", "guc_fields", "guc_summary", "topics_for", "errcode_blocks", "find_errcode",
           "apply_override", "is_guc_page", "is_errcode_page", "chapter_of"]

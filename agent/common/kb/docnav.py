"""官方文档集在分层里的样子:L0 一行、L1 专题页里的一节、L1 文档目录导航(`kb.py doc`)。

几千条官方文档不能像客户条目那样「每类截前 4 条」(按编号截出来的与问题无关),所以:
  · 专题页里只写「按类别计数 + 4 条代表条目 + 下钻命令」;代表条目按「标题里带专题关键词 → 类别优先级」挑;
  · 文档目录导航按原书目录逐级下钻:不给章节就列各章条目数;给到章节列该章每条的 ID + 标题 + 一句话,≤ 上限,超出写明怎么翻页。
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from . import catalog as kbcat
from . import docsplit
from . import topics as kbtopics

NAV_LIMIT = 6000
CATEGORY_ORDER = ("GUC 参数", "系统表 / 视图", "说明类页面", "SQL 语法与函数", "错误码")
_CATALOG_CHAPTERS = ("系统表和系统视图", "Schema")


def category(e: kbcat.Entry) -> str:
    kind = e.meta.get("kind")
    path = e.meta.get("doc_path") or []
    if kind == docsplit.KIND_GUC:
        return "GUC 参数"
    if kind == docsplit.KIND_ERRCODE:
        return "错误码"
    if any(c in path for c in _CATALOG_CHAPTERS):
        return "系统表 / 视图"
    if "SQL参考" in path:
        return "SQL 语法与函数"
    return "说明类页面"


def _keywords(slug: str, topic: Optional[kbtopics.Topic]) -> Tuple[str, ...]:
    words = [slug.upper()] + [s for s in (topic.synonyms if topic else ()) if len(s) >= 2]
    return tuple(dict.fromkeys(words))


def representatives(entries: Sequence[kbcat.Entry], slug: str, topic: Optional[kbtopics.Topic], n: int = 4
                    ) -> List[kbcat.Entry]:
    kws = _keywords(slug, topic)

    def key(e: kbcat.Entry):
        title = e.title.upper()
        hit = any(k.upper() in title for k in kws)
        return (not hit, CATEGORY_ORDER.index(category(e)), e.id)
    return sorted(entries, key=key)[:n]


def topic_block(entries: Sequence[kbcat.Entry], slug: str, topic: Optional[kbtopics.Topic]) -> List[str]:
    """L1 专题页末尾的「官方文档」一节(每套文档集一段)。entries 是该专题的全部条目;没有官方文档就返回空。"""
    by_set: Dict[str, List[kbcat.Entry]] = {}
    for e in entries:
        if e.meta.get("docset"):
            by_set.setdefault(str(e.meta.get("docset")), []).append(e)
    lines: List[str] = []
    for code, ents in sorted(by_set.items()):
        first = ents[0].meta
        counts: Dict[str, int] = {}
        for e in ents:
            counts[category(e)] = counts.get(category(e), 0) + 1
        lines += [f"## 官方文档({first.get('product', '')} {first.get('versions', '')} · 文档集 {code} · {len(ents)} 条)",
                  "按类别:" + " · ".join(f"{k} {counts[k]}" for k in CATEGORY_ORDER if counts.get(k)),
                  "代表条目:"]
        lines += [f"- {e.cite}《{e.title}》:{kbcat.first_sentence(e.summary, 90)}"
                  for e in representatives(ents, slug, topic)]
        lines += [f"全部:`kb.py doc {code} --topic {slug}`(按目录列出;引用前 `kb.py show <ID>`)", ""]
    return lines


def l0_rows(cat: kbcat.Catalog, docsets: Sequence) -> List[str]:
    """L0 的「官方文档集」表。docsets 是 docset.installed() 的结果(带 manifest)。"""
    if not docsets:
        return []
    out = ["## 官方文档集(标准知识库,随工具版本安装,只读)", "",
           "| 代号 | 文档 | 产品与版本 | 条目 | 索引 | 入口 |", "|---|---|---|---|---|---|"]
    for ds in docsets:
        m = ds.manifest
        idx = m.get("index") or {}
        state = "完整" if idx.get("complete") else ("未建" if not idx else f"建了一部分(还差 {idx.get('docs_left', '?')} 条)")
        ver = f"{m.get('product', '')} {m.get('versions', '')}" + (f"(内核 {m['kernel_version']})" if m.get("kernel_version") else "")
        out.append(f"| {ds.code} | {m.get('title', '')} | {ver} | {m.get('items', 0)} | {state} | `kb.py doc {ds.code}` |")
    out += ["", "官方文档只说明产品怎么工作;**贵行规范、案例与之不一致时以贵行为准**,并列呈现交用户裁决。", ""]
    return out


def _chapter_key(path: Sequence[str], depth: int) -> Tuple[str, ...]:
    return tuple(path[:depth])


def render_nav(cat: kbcat.Catalog, code: str, chapter: str = "", topic: str = "", show_all: bool = False,
               limit: int = NAV_LIMIT) -> str:
    """`kb.py doc <代号> [章节] [--topic] [--all]`。章节按名字匹配目录里任意一级(取第一处)。"""
    ents = cat.from_docset(code)
    if not ents:
        return f"(文档集 {code} 没有条目)\n"
    m = ents[0].meta
    head = [f"# {code} 文档目录导航(L1)", "",
            f"> 产品 {m.get('product', '')} · 版本 {m.get('versions', '')} · 条目 {len(ents)}"
            + (f" · 专题 {kbtopics.name_of(topic, cat.topics)}" if topic else ""), ""]
    if topic:
        ents = [e for e in ents if (topic == kbtopics.OTHER and not e.topics) or topic in e.topics]
    if chapter:
        ents = [e for e in ents if chapter in (e.meta.get("doc_path") or [])]
        if not ents:
            return "\n".join(head + [f"没有名为「{chapter}」的章节(或该章节在这个专题下没有条目)。不带章节先看目录。"]) + "\n"
    if not chapter and not topic:
        return "\n".join(head + _toc_table(ents, code)) + "\n"
    lines = head + [f"## {chapter or kbtopics.name_of(topic, cat.topics)}({len(ents)} 条)", ""]
    shown = 0
    for e in ents:
        line = f"- {e.id}《{e.title}》:{kbcat.first_sentence(e.summary, 80)}"
        if not show_all and len("\n".join(lines)) + len(line) > limit - 200:
            break
        lines.append(line)
        shown += 1
    if shown < len(ents):
        lines += ["", f"(已列 {shown} / {len(ents)} 条;往下细分:`kb.py doc {code} \"<下一级章节名>\"`,"
                      f"或加 `--all` 全列)"]
        lines += [""] + _toc_table(ents, code, depth=_depth_below(ents, chapter))
    return "\n".join(lines) + "\n"


def _depth_below(ents: Sequence[kbcat.Entry], chapter: str) -> int:
    for e in ents:
        path = list(e.meta.get("doc_path") or [])
        if chapter in path:
            return path.index(chapter) + 2
    return 3


def _toc_table(ents: Sequence[kbcat.Entry], code: str, depth: int = 3) -> List[str]:
    counts: Dict[Tuple[str, ...], int] = {}
    for e in ents:
        key = _chapter_key(e.meta.get("doc_path") or [], depth)
        counts[key] = counts.get(key, 0) + 1
    rows = ["| 目录 | 条目 | 下钻 |", "|---|---|---|"]
    for key, n in counts.items():
        rows.append(f"| {' › '.join(key[1:]) or key[0]} | {n} | `kb.py doc {code} \"{key[-1]}\"` |")
    return rows


__all__ = ["category", "representatives", "topic_block", "l0_rows", "render_nav", "CATEGORY_ORDER"]

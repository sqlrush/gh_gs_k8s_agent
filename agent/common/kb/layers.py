"""渐进式披露的上两层:L0 总索引、L1 专题导航 —— 都由条目元数据与图谱生成,不手写。

  L0  <kb>/L0-总索引.md     知识范围(八类各多少)、适用环境(画像)、专题入口、读法      ≤ L0_LIMIT 字
  L1  <kb>/topics/<专题>.md 症状与同义词、候选原因(本行确认过的因果链 + 区分证据)、
                           相关条目摘要(按类型分组,带 ID)                         ≤ L1_LIMIT 字
L2 是条目本身(kb.py show <ID>),L3 是原始证据(kb.py evidence <ID>)。

上限按字符算:条目从几十条长到几千条,进上下文的量基本不变;超出时按「已确认优先、近期优先」截断,
并写明「其余 N 条:kb.py topic <专题> --all」——截断要看得见,不能静默少。
"""
from __future__ import annotations

import datetime
import pathlib
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from . import catalog as kbcat
from . import docnav
from . import graphfiles as gf
from . import topics as kbtopics
from . import types as kbtypes
from .atomic import write_text_atomic

L0_FILE = "L0-总索引.md"
L0_LIMIT = 3000
L1_LIMIT = 6000
ALL_LIMIT = 60000
GENERATED_MARK = "> 由 `kb.py index` 生成,勿手工编辑"
_TYPE_CAPS = {kbtypes.CASE: 6, kbtypes.NORM: 5, kbtypes.ERRATA: 5}
_DEFAULT_CAP = 4
_CONF_ORDER = {"已确认": 0, "推测": 1, "待验证": 2}


@dataclass(frozen=True)
class Cause:
    symptom: str
    rootcause: str
    cases: Tuple[str, ...]
    discriminator: str = ""
    disc_from: str = ""            # 区分证据 / 复发标志 / ""


@dataclass(frozen=True)
class TopicView:
    topic: kbtopics.Topic
    entries: Tuple[kbcat.Entry, ...]
    causes: Tuple[Cause, ...]
    lookalikes: Tuple[str, ...]     # 同一现象、不止一种根因的现象名
    synonyms: Tuple[str, ...]


# ---------------------------------------------------------------- 候选原因(图谱)

def _confirmed_causes(triples: Sequence[gf.Triple]) -> Dict[Tuple[str, str], Dict[str, object]]:
    """(现象, 根因) → {cases, symptom_name, rootcause_name};只看本行确认过的 caused_by 边。"""
    out: Dict[Tuple[str, str], Dict[str, object]] = {}
    for t in triples:
        if t.rel != "caused_by" or t.src.kind != "symptom" or t.dst.kind != "rootcause" or not t.in_path:
            continue
        slot = out.setdefault((t.src.id, t.dst.id), {"cases": [], "s": t.src.name, "r": t.dst.name})
        if t.case_id and t.case_id not in slot["cases"]:
            slot["cases"].append(t.case_id)
    return out


def _discriminator(case_ids: Sequence[str], by_id: Dict[str, kbcat.Entry]) -> Tuple[str, str]:
    for cid in case_ids:
        e = by_id.get(cid)
        sec = (e.meta.get("sections") or {}) if e else {}
        if sec.get("区分证据"):
            return kbcat.first_sentence(sec["区分证据"], 70), "区分证据"
    for cid in case_ids:
        e = by_id.get(cid)
        sec = (e.meta.get("sections") or {}) if e else {}
        sig = sec.get("复发标志") or "、".join((e.meta.get("signals") or [])[:2]) if e else ""
        if sig:
            return kbcat.first_sentence(sig, 70), "复发标志"
    return "", ""


def topic_view(cat: kbcat.Catalog, triples: Sequence[gf.Triple], slug: str) -> TopicView:
    topic = kbtopics.by_slug(cat.topics).get(slug) or kbtopics.Topic(slug, kbtopics.name_of(slug, cat.topics))
    entries = tuple(cat.in_topic(slug))
    in_topic_cases = {e.id for e in entries if e.type == kbtypes.CASE}
    by_id = cat.by_id()
    causes: List[Cause] = []
    for (_sid, _rid), v in _confirmed_causes(triples).items():
        cases = [c for c in v["cases"] if c in in_topic_cases]
        text = f"{v['s']} {v['r']}"
        if not cases and slug not in kbtopics.infer_topics(text, cat.topics):
            continue
        disc, src = _discriminator(cases or v["cases"], by_id)
        causes.append(Cause(str(v["s"]), str(v["r"]), tuple(cases or v["cases"]), disc, src))
    per_symptom: Dict[str, set] = {}
    for c in causes:
        per_symptom.setdefault(c.symptom, set()).add(c.rootcause)
    lookalikes = tuple(sorted(s for s, rs in per_symptom.items() if len(rs) > 1))
    causes.sort(key=lambda c: (c.symptom not in lookalikes, -len(c.cases), c.symptom, c.rootcause))
    sigs: List[str] = list(topic.synonyms)
    for e in entries:
        if e.type == kbtypes.CASE:
            sigs += [s for s in (e.meta.get("signals") or []) if len(s) <= 20]
    return TopicView(topic=topic, entries=entries, causes=tuple(causes), lookalikes=lookalikes,
                     synonyms=tuple(dict.fromkeys(sigs))[:24])


# ---------------------------------------------------------------- 渲染

def entry_line(e: kbcat.Entry, applies_note: str = "") -> str:
    tag = e.verification
    if e.type == kbtypes.CASE:
        when = f",{e.as_of}" if e.as_of else ""
        head = f"- {e.cite}《{e.title}》(结论强度:{tag or '?'}{when})"
    elif e.type == kbtypes.NORM:
        head = f"- {e.cite}" + (f"({tag})" if tag else "")
    else:
        head = f"- {e.cite}《{e.title}》" + (f"({tag})" if tag else "")
    note = f"〔{applies_note}〕" if applies_note else ""
    return f"{head}{note}:{e.summary}" if e.summary else head + note


def _date_key(s: str) -> int:
    digits = re.sub(r"\D", "", s or "")[:8]
    return int(digits) if digits else 0


def _sorted_entries(entries: Sequence[kbcat.Entry]) -> List[kbcat.Entry]:
    """类型按展示顺序;案例里已确认的在前、近期的在前,其余按 ID。"""
    order = {t: i for i, t in enumerate(kbtypes.DISPLAY_ORDER)}

    def key(e: kbcat.Entry):
        if e.type == kbtypes.CASE:
            return (order.get(e.type, 99), _CONF_ORDER.get(e.verification, 9), -_date_key(e.as_of), e.id)
        return (order.get(e.type, 99), 0, 0, e.id)
    return sorted(entries, key=key)


def _entry_block(entries: Sequence[kbcat.Entry], caps: Optional[Dict[str, int]]) -> Tuple[List[str], int]:
    lines: List[str] = []
    hidden = 0
    shown: Dict[str, int] = {}
    for e in _sorted_entries(entries):
        cap = None if caps is None else caps.get(e.type, _DEFAULT_CAP)
        if cap is not None and shown.get(e.type, 0) >= cap:
            hidden += 1
            continue
        shown[e.type] = shown.get(e.type, 0) + 1
        lines.append(entry_line(e))
    return lines, hidden


def render_l1(view: TopicView, version: str, show_all: bool = False) -> str:
    t = view.topic
    counts: Dict[str, int] = {}
    own = [e for e in view.entries if not e.meta.get("docset")]          # 官方文档集另起一节(docnav.topic_block)
    for e in own:
        counts[e.short] = counts.get(e.short, 0) + 1
    official = len(view.entries) - len(own)
    if official:
        counts["官方文档"] = official
    head = [f"# {kbtopics.name_of(t.slug, [t])}(L1 专题导航 · `{t.slug}`)", "",
            f"{GENERATED_MARK}。版本 {version} · 条目 {len(view.entries)}"
            + ("(" + " · ".join(f"{k} {v}" for k, v in counts.items()) + ")" if counts else ""), ""]
    if view.synonyms:
        head += ["## 症状与同义词", "、".join(view.synonyms), ""]
    cause_lines: List[str] = []
    if view.causes:
        cause_lines += ["## 候选原因(本行确认过的因果链,按案例数)", "", "| 现象 | 候选根因 | 案例 | 区分证据 |", "|---|---|---|---|"]
        for c in view.causes[:8 if not show_all else None]:
            disc = c.discriminator if c.disc_from == "区分证据" else (f"(复发标志){c.discriminator}" if c.discriminator else "")
            if not disc and c.symptom in view.lookalikes:
                disc = "⚠ 未写区分证据"
            cause_lines.append(f"| {c.symptom} | {c.rootcause} | {len(c.cases)} | {disc} |")
        if view.lookalikes:
            cause_lines += ["", f"> ⚠ 同一现象在本行有过不止一种根因:{'、'.join(view.lookalikes)}。"
                                "先按区分证据取证,不要看到一单相似就照搬它的处置。"]
        cause_lines.append("")
    foot = docnav.topic_block(view.entries, t.slug, t) + [
        "## 读法", "要引用哪条先 `kb.py show <ID>`(处置步骤、审批、版本结论不许只凭这里的摘要写);"
                 "核对原文 `kb.py evidence <ID>`。"]
    def build(caps: Optional[Dict[str, int]], causes: List[str]) -> str:
        lines, hidden = _entry_block(own, caps)
        tail = [f"(其余 {hidden} 条:`kb.py topic {t.slug} --all`)"] if hidden else []
        return "\n".join(head + causes + ["## 相关条目(摘要)", ""] + (lines or ["(暂无)"]) + tail + [""] + foot) + "\n"

    if show_all:
        return build(None, cause_lines)[:ALL_LIMIT]
    # 超上限就每类少显示一条,直到放得下;每类至少留一条。仍放不下(摘要太长)再硬截。
    caps = {e.type: _TYPE_CAPS.get(e.type, _DEFAULT_CAP) for e in own}
    while True:
        text = build(caps, cause_lines)
        if len(text) <= L1_LIMIT or all(v <= 1 for v in caps.values()):
            break
        caps = {k: max(1, v - 1) for k, v in caps.items()}
    if len(text) > L1_LIMIT:
        text = build(caps, cause_lines[:12])
    return text if len(text) <= L1_LIMIT else text[:L1_LIMIT - 30].rsplit("\n", 1)[0] + "\n(超出 L1 上限已截断)\n"


def _raw_count(kb: pathlib.Path) -> int:
    root = kb / "inbox"
    return sum(1 for _ in root.glob("*/items/*.md")) if root.is_dir() else 0


def _section_first(e: kbcat.Entry, name: str, limit: int = 30) -> str:
    return kbcat.first_sentence((e.meta.get("sections") or {}).get(name, ""), limit)


def render_l0(kb: pathlib.Path, cat: kbcat.Catalog, version: str, today: Optional[str] = None) -> str:
    from . import docset as kbdocset
    counts = kbcat.type_counts(cat)
    official = len(cat.from_docset())
    cases = cat.of_type(kbtypes.CASE)
    confirmed = sum(1 for e in cases if e.verification == "已确认")
    rows = [
        ("公司内部规范", counts.get(kbtypes.NORM, 0) + counts.get(kbtypes.ERRATA, 0),
         f"条款 {counts.get(kbtypes.NORM, 0)}、修正 {counts.get(kbtypes.ERRATA, 0)}"),
        ("历史案例排查思路", len(cases), f"已确认 {confirmed}"),
        ("环境与业务画像", counts.get("profile", 0), "个系统"),
        ("指标口径与正常基线", counts.get("baseline", 0), ""),
        ("已知缺陷与版本例外", counts.get("defect", 0), ""),
        ("处置与恢复手册", counts.get("runbook", 0), ""),
        ("高斯产品文档", counts.get("product", 0),
         f"客户摘录 {counts.get('product', 0) - official}、官方文档集 {official}(见下)" if official else ""),
        ("存量 Wiki 经验", counts.get("wiki", 0), ""),
    ]
    out = ["# 知识库总索引(L0)", "",
           f"{GENERATED_MARK}。版本 {version} · 生成于 {today or datetime.date.today().isoformat()}", "",
           "## 知识范围", "", "| 类型 | 条目 | 说明 |", "|---|---|---|"]
    for name, n, note in rows:
        out.append(f"| {name} | {n if n else '0 —— 未登记'} | {note if n else '回答涉及这类知识时如实说「本行未登记」'} |")
    if counts.get(kbtypes.GUIDE):
        out.append(f"| 指南(类型待确认) | {counts[kbtypes.GUIDE]} | 旧版指南,待 `kb.py migrate` 归类 |")
    raw = _raw_count(kb)
    if raw:
        out.append(f"| 原始工单(未结构化,L3) | {raw} | 只能说「有一单类似,未结构化」 |")
    out += [""] + docnav.l0_rows(cat, kbdocset.installed(kb))
    out += ["## 适用环境", ""]
    profiles = cat.profiles()
    if profiles:
        out += ["| 系统 | 产品与版本 | 部署 | 连接别名 | 批量窗口 | 画像 |", "|---|---|---|---|---|---|"]
        for p in profiles:
            m = p.meta
            aliases = "、".join(kbtypes.as_list(m.get("aliases"))) or "—"
            out.append(f"| {m.get('system', '?')} | {m.get('product', '?')} {m.get('version', '')} | "
                       f"{m.get('deployment', '?')} | {aliases} | {_section_first(p, '批量窗口') or '—'} | {p.id} |")
    else:
        out.append("(未登记环境画像:条目的产品系列 / 版本适用性无法判断,回答里写「适用性未知」)")
    out += ["", "## 专题入口", "", "| 专题 | 常见说法 | 条目 | 入口 |", "|---|---|---|---|"]
    for t in cat.topics:
        ents = cat.in_topic(t.slug)
        if not ents:
            continue
        mix: Dict[str, int] = {}
        for e in ents:
            key = "官方" if e.meta.get("docset") else e.short
            mix[key] = mix.get(key, 0) + 1
        out.append(f"| {t.name} | {' '.join(t.synonyms[:3])} | {' '.join(f'{k}{v}' for k, v in mix.items())} | "
                   f"`kb.py topic {t.slug}` |")
    other = cat.in_topic(kbtopics.OTHER)
    if other:
        out.append(f"| {kbtopics.OTHER_NAME} | — | {len(other)} | `kb.py topic {kbtopics.OTHER}` |")
    out += ["", "## 读法", "",
            "专题 → `kb.py topic <专题>`;引用前 → `kb.py show <ID>`;核对原文 → `kb.py evidence <ID>`。"
            "条目与本环境产品 / 版本不符的写「不适用本环境」,没有画像的写「适用性未知」。"]
    text = "\n".join(out) + "\n"
    if len(text) > L0_LIMIT:
        text = text[:L0_LIMIT - 40].rsplit("\n", 1)[0] + "\n(超出 L0 上限已截断:专题过多,合并专题定义)\n"
    return text


# ---------------------------------------------------------------- 诊断时的专题提示

def hint_lines(cat: kbcat.Catalog, triples: Sequence[gf.Triple], slugs: Sequence[str], max_causes: int = 3) -> List[str]:
    """知识库一节里每条发现前的两行:专题入口 + 本行确认过的候选原因(有反例时提醒先取证)。"""
    if not slugs:
        return []
    names = [f"{kbtopics.name_of(s, cat.topics)}(`kb.py topic {s}`)" for s in slugs]
    lines = [f"- 专题:{'、'.join(names)}"]
    causes: List[Cause] = []
    look = False
    for s in slugs[:2]:
        v = topic_view(cat, triples, s)
        causes += list(v.causes)
        look = look or bool(v.lookalikes)
    seen = set()
    picked = []
    for c in sorted(causes, key=lambda c: -len(c.cases)):
        if c.rootcause in seen:
            continue
        seen.add(c.rootcause)
        picked.append(c)
    if picked:
        txt = " · ".join(f"{c.rootcause}({len(c.cases)} 案)" for c in picked[:max_causes])
        lines.append(f"- 候选原因(本行确认过):{txt}"
                     + (";同一现象有过多种根因,先按区分证据取证" if look else ""))
    return lines


# ---------------------------------------------------------------- 写盘(kb.py index)

def write_layers(kb: pathlib.Path, version: str) -> Tuple[List[str], List[str]]:
    """写 L0 与各专题 L1;已经没有条目的专题,删掉它**生成的**旧文件(_taxonomy.yaml 与手写文件不动)。"""
    cat = kbcat.load(kb)
    triples, _ = gf.load_triples(kb, case_ids=[e.id for e in cat.of_type(kbtypes.CASE)])
    written: List[str] = []
    write_text_atomic(kb / L0_FILE, render_l0(kb, cat, version))
    written.append(L0_FILE)
    tdir = kb / "topics"
    tdir.mkdir(parents=True, exist_ok=True)
    live = set()
    for slug in [t.slug for t in cat.topics] + [kbtopics.OTHER]:
        view = topic_view(cat, triples, slug)
        if not view.entries:
            continue
        live.add(f"{slug}.md")
        write_text_atomic(tdir / f"{slug}.md", render_l1(view, version))
        written.append(f"topics/{slug}.md")
    for old in tdir.glob("*.md"):
        if old.name in live:
            continue
        try:
            if GENERATED_MARK in old.read_text(encoding="utf-8")[:400]:
                old.unlink()
        except (OSError, UnicodeDecodeError):
            pass
    return written, list(cat.warnings)

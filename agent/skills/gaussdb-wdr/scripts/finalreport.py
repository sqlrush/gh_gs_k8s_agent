"""Render the final WDR report from evidence + interp — port of wdr/render.go.

Runs the mechanical anchoring recheck, builds a paint-agnostic model, then emits
markdown or ANSI. Both emitters consume the same model; differences are color +
table style only.

2026-10-26 回答模板化(R7):正文改成「① 负载画像 → ② 问题与元凶 → ③ 解决方案 → ④ 总结和结论」,
其余全景数据、SQL 附录、本报告没覆盖的、校验区进「附录」。markdown 发射器在 mdreport.py,附录在 appendix.py。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import render
from ansi import (
    ANSI_BOLD, ANSI_DIM, ANSI_ORANGE, box_table, colorize, severity_color,
)
import kbrefs
from interp import PRIORITIES, Interp, Suggestion
from model import (
    DIM_CACHE, DIM_CHECKPOINT, DIM_DBSTAT, DIM_FILEIO, DIM_LOADPROFILE,
    DIM_TOPSQL, DIM_WAITS, Evidence, Severity, dim_severity,
)
from recheck import Rechecked, recheck
from appendix import (  # noqa: F401  (测试与旧调用方仍按 finalreport.xxx 取)
    APPENDIX_PLAN_MAX, APPENDIX_SQL_MAX, _NOT_COVERED, _emit_next_steps, _emit_plan,
    _emit_quarantine_md, _emit_sql_appendix,
)
from mdreport import emit_markdown, split_tables
from util import trunc

# Top-down presentation order in 全景分析 (global load → waits → SQL → ...).
_REPORT_ORDER = [DIM_LOADPROFILE, DIM_DBSTAT, DIM_WAITS, DIM_TOPSQL,
                 DIM_CHECKPOINT, DIM_CACHE, DIM_FILEIO]


def _dim_rank(name: str) -> int:
    return _REPORT_ORDER.index(name) if name in _REPORT_ORDER else len(_REPORT_ORDER)


@dataclass
class _MatrixRow:
    dim: str
    sev: Severity
    headline: str


@dataclass
class _FindingView:
    sev: Severity
    code: str
    dim: str
    evidence_line: str
    root_cause: str
    suggestions: list = field(default_factory=list)
    sql_id: str = ""        # 这条发现指向哪条语句;SQL 附录按它列全文
    app_view: str = ""      # 应用视角(回答模板 R2)
    mechanism: str = ""     # 数据库机制
    priority: str = ""      # P0 / P1 / P2;interp 没给就按严重度排
    kb_lines: list = field(default_factory=list)   # 已核对、已上色的知识引用


@dataclass
class _RawTable:
    title: str
    headline: str
    headers: list
    rows: list


@dataclass
class _ReportModel:
    conn: str = ""
    target: str = ""
    window: object = None
    overall: Severity = Severity.OK
    driver: str = ""
    badge: str = ""
    matrix: list = field(default_factory=list)
    findings: list = field(default_factory=list)
    missing: list = field(default_factory=list)
    unanchored: list = field(default_factory=list)
    unverified: list = field(default_factory=list)
    raw_tables: list = field(default_factory=list)
    sql_texts: dict = field(default_factory=dict)   # sql_id → 全文,给 SQL 附录用
    plans: dict = field(default_factory=dict)       # sql_id → 执行计划正文(模型取回来的)
    hist_plans: dict = field(default_factory=dict)  # sql_id → 采集时取的窗口内真实计划(见 collectors.collect_plans)
    summary: str = ""
    load_app_view: str = ""
    load_mechanism: str = ""
    conclusion: str = ""
    followups: list = field(default_factory=list)
    kb_dropped: list = field(default_factory=list)  # 核对不过、没进正文的知识引用


def format_suggestion(s: Suggestion) -> str:
    out = s.text + "  风险:" + s.risk
    if s.manual:
        out += " [需人工执行]"
    if s.validation is not None and s.validation.status == "verified" and s.validation.evidence:
        out += "  ✅实证 " + s.validation.evidence
    return out


def default_priority(sev: Severity) -> str:
    """interp 没给优先级时按严重度排:严重 → P0,告警 → P1,其余 → P2。"""
    if sev >= Severity.CRITICAL:
        return PRIORITIES[0]
    return PRIORITIES[1] if sev >= Severity.WARN else PRIORITIES[2]


def build_model(ev: Evidence, r: Rechecked, in_: object = None, kb_dir=None) -> _ReportModel:
    m = _ReportModel(conn=ev.conn, target=ev.target, window=ev.window,
                     overall=r.overall, driver=r.driver, badge=r.badge)
    for d in ev.dims:
        m.matrix.append(_MatrixRow(d.dimension, dim_severity(d), d.headline))
    for a in r.anchored:
        inf = a.interp
        fv = _FindingView(
            sev=a.ev.severity, code=a.ev.code, dim=a.ev.dimension,
            evidence_line=f"{a.ev.metric} {a.ev.value} vs 阈值 {a.ev.threshold}（{a.ev.evidence}）",
            # 引发它的语句:证据里带的优先;证据没带(死锁、等待这类库级发现)就用模型在 interp 里点名的那条
            root_cause=a.root_cause, sql_id=getattr(a.ev, "sql_id", "") or getattr(inf, "sql_id", "") or "",
            app_view=getattr(inf, "app_view", ""), mechanism=getattr(inf, "mechanism", ""),
            priority=getattr(inf, "priority", "") or default_priority(a.ev.severity))
        for s in a.verified:
            fv.suggestions.append(format_suggestion(s))
        lines, dropped = kbrefs.resolve(getattr(inf, "kb_refs", []), kb_dir)
        fv.kb_lines = lines
        m.kb_dropped.extend("%s · %s" % (a.ev.code, x) for x in dropped)
        m.findings.append(fv)
    for f in r.missing:
        m.missing.append(f"{f.code}（{f.severity.label()}）")
    for inf in r.unanchored:
        m.unanchored.append(inf.code)
    for a in r.anchored:
        for s in a.unverified:
            method = s.validation.method if s.validation is not None else ""
            m.unverified.append(f"{s.text}（{method} 未通过/未验证）")
    # 全景分析：所有可用维度按自顶向下顺序铺开，每维带其 Headline。
    for d in ev.dims:
        if d.available and d.rows:
            m.raw_tables.append(_RawTable(d.dimension, d.headline, d.headers, d.rows))
    m.raw_tables.sort(key=lambda t: _dim_rank(t.title))
    m.sql_texts = dict(getattr(ev, "sql_texts", {}) or {})   # SQL 附录用
    m.plans = dict(getattr(in_, "plans", {}) or {})           # 附录里的执行计划
    m.hist_plans = dict(getattr(ev, "sql_plans", {}) or {})   # 优先:窗口内的真实执行计划
    m.summary = getattr(in_, "summary", "") or ""
    m.load_app_view = getattr(in_, "load_app_view", "") or ""
    m.load_mechanism = getattr(in_, "load_mechanism", "") or ""
    m.conclusion = getattr(in_, "conclusion", "") or ""
    m.followups = list(getattr(in_, "followups", []) or [])
    return m


def render_report(ev: Evidence, in_: Interp, fmt: str = "md", no_color: bool = False, kb_dir=None) -> str:
    """kb_dir:核对知识引用用的知识库目录;不给就按 $GSDB_KB_DIR / 安装根下的 kb/ 找。"""
    r = recheck(ev, in_)
    m = build_model(ev, r, in_, kb_dir if kb_dir is not None else kbrefs.default_kb_dir())
    if fmt in ("md", ""):
        return emit_markdown(m)
    if fmt == "ansi":
        return _emit_ansi(m, not no_color)
    raise ValueError(f"--format {fmt!r}: must be md or ansi")


# ---- markdown emitter ----

# ---- ansi emitter ----

_FONT_TAG = re.compile(r"</?font[^>]*>")


def _ansi_table(t, color: bool) -> str:
    out = colorize("【" + t.title + "】", ANSI_BOLD, color) + "\n"
    if t.headline:
        out += "  " + t.headline + "\n"
    return out + box_table(t.headers, t.rows) + "\n"


def _emit_ansi(m: _ReportModel, color: bool) -> str:
    """终端版:段落与 markdown 版相同(① 负载画像 → ② 问题与元凶 → ③ 解决方案 → ④ 总结 → 附录),只是配色与表格样式不同。"""
    w = m.window

    def sev_col(s, text):
        return colorize(text, severity_color(s), color)

    def h2(text):
        return colorize(text, ANSI_BOLD, color) + "\n"

    title = f"📊 WDR 报告 · {m.conn}"
    if m.target:
        title += f" ({m.target})"
    out = colorize(title, ANSI_BOLD, color) + "\n"
    out += (f"评估窗口 snap {w.begin_id} ({w.begin_ts}) → {w.end_id} ({w.end_ts})  "
            f"时长 {w.duration_min} 分钟  scope={w.scope} node={w.node}\n")
    out += f"总体状态 {sev_col(m.overall, m.overall.label())}  驱动：{m.driver}\n"
    out += f"判断校验 {m.badge}\n"
    if m.summary:
        out += f"短结论 {m.summary}\n"
    load, culprit, rest = split_tables(m.raw_tables)

    out += "\n" + h2("① 负载画像")
    out += box_table(["维度", "严重度", "关键指标"], [[r.dim, r.sev.label(), trunc(r.headline, 40)] for r in m.matrix]) + "\n"
    for label, text in (("应用视角", m.load_app_view), ("数据库机制", m.load_mechanism)):
        if text:
            out += f"  {label}  {text}\n"
    out += "".join(_ansi_table(t, color) for t in load)

    out += h2("② 问题与元凶") + "".join(_ansi_table(t, color) for t in culprit)
    for f in m.findings:
        out += f"{sev_col(f.sev, f.sev.label())} {f.code} · {f.dim}\n"
        out += f"  证据  {f.evidence_line}\n"
        for label, text in (("应用视角", f.app_view), ("数据库机制", f.mechanism), ("根因", f.root_cause)):
            if text:
                out += f"  {label}  {text}\n"
        if f.sql_id:
            out += f"  引发它的语句  sql_id {f.sql_id}\n"
        for line in f.kb_lines:
            out += "  知识  " + _FONT_TAG.sub("", line) + "\n"     # 终端里没有颜色标签,只留文字与出处

    out += "\n" + h2("③ 解决方案")
    for p in PRIORITIES:
        for f in (x for x in m.findings if x.priority == p):
            for s in f.suggestions:
                out += f"  {p}  {f.code}：{s}\n"

    out += "\n" + h2("④ 总结和结论") + "  " + (m.conclusion or "本次未给出总结。") + "\n"
    out += "".join(f"  · {q}\n" for q in m.followups)
    out += colorize("—— 脚本只读采集；结论已锚定校验，建议经实证。", ANSI_DIM, color) + "\n\n"

    if rest or m.missing or m.unanchored or m.unverified or m.kb_dropped:
        out += h2("附录") + "".join(_ansi_table(t, color) for t in rest)
        out += _emit_quarantine_ansi(m, color)
        for x in m.kb_dropped:
            out += colorize("⚠ 知识引用未展示", ANSI_ORANGE, color) + f"  {x}\n"
    return out


def _emit_quarantine_ansi(m: _ReportModel, color: bool) -> str:
    out = ""
    if m.missing:
        out += colorize("⚠ 模型遗漏", ANSI_ORANGE, color) + "\n"
        for s in m.missing:
            out += f"  {s}\n"
    if m.unanchored:
        out += colorize("⚠ 未锚定（已剔除）", ANSI_ORANGE, color) + "\n"
        for c in m.unanchored:
            out += f"  {c}\n"
    if m.unverified:
        out += colorize("⚠ 未验证建议（已剔除）", ANSI_ORANGE, color) + "\n"
        for s in m.unverified:
            out += f"  {s}\n"
    if m.missing or m.unanchored or m.unverified:
        out += "\n"
    return out

"""Render the final WDR report from evidence + interp — port of wdr/render.go.

Runs the mechanical anchoring recheck, builds a paint-agnostic model, then emits
markdown or ANSI. Both emitters consume the same model; differences are color +
table style only.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import render
from ansi import (
    ANSI_BOLD, ANSI_DIM, ANSI_ORANGE, box_table, colorize, severity_color,
)
from interp import Interp, Suggestion
from model import (
    DIM_CACHE, DIM_CHECKPOINT, DIM_DBSTAT, DIM_FILEIO, DIM_LOADPROFILE,
    DIM_TOPSQL, DIM_WAITS, Evidence, Severity, dim_severity,
)
from recheck import Rechecked, recheck
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


def format_suggestion(s: Suggestion) -> str:
    out = s.text + "  风险:" + s.risk
    if s.manual:
        out += " [需人工执行]"
    if s.validation is not None and s.validation.status == "verified" and s.validation.evidence:
        out += "  ✅实证 " + s.validation.evidence
    return out


def build_model(ev: Evidence, r: Rechecked, in_: object = None) -> _ReportModel:
    m = _ReportModel(conn=ev.conn, target=ev.target, window=ev.window,
                     overall=r.overall, driver=r.driver, badge=r.badge)
    for d in ev.dims:
        m.matrix.append(_MatrixRow(d.dimension, dim_severity(d), d.headline))
    for a in r.anchored:
        fv = _FindingView(
            sev=a.ev.severity, code=a.ev.code, dim=a.ev.dimension,
            evidence_line=f"{a.ev.metric} {a.ev.value} vs 阈值 {a.ev.threshold}（{a.ev.evidence}）",
            root_cause=a.root_cause, sql_id=getattr(a.ev, "sql_id", "") or "")
        for s in a.verified:
            fv.suggestions.append(format_suggestion(s))
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
    return m


def render_report(ev: Evidence, in_: Interp, fmt: str = "md", no_color: bool = False) -> str:
    r = recheck(ev, in_)
    m = build_model(ev, r, in_)
    if fmt in ("md", ""):
        return _emit_markdown(m)
    if fmt == "ansi":
        return _emit_ansi(m, not no_color)
    raise ValueError(f"--format {fmt!r}: must be md or ansi")


# ---- markdown emitter ----

def _emit_markdown(m: _ReportModel) -> str:
    w = m.window
    if m.target:
        out = f"# WDR 报告 — {m.conn} ({m.target})\n\n"
    else:
        out = f"# WDR 报告 — {m.conn}\n\n"
    out += f"总体状态 {m.overall.label()}  驱动：{m.driver}\n\n"
    out += f"判断校验 {m.badge}\n\n"
    out += (f"## 报告窗口\n\nsnap {w.begin_id} ({w.begin_ts}) → snap {w.end_id} "
            f"({w.end_ts})，时长 {w.duration_min} 分钟，scope={w.scope} node={w.node}\n\n")

    out += "## 维度概览\n\n"
    mrows = [[r.dim, r.sev.label(), r.headline] for r in m.matrix]
    out += render.table(["维度", "严重度", "关键指标"], mrows)
    out += "\n"

    out += _emit_charts(m)

    if m.raw_tables:
        out += "## 全景分析（自顶向下）\n\n"
        for t in m.raw_tables:
            out += f"### {t.title}\n\n"
            if t.headline:
                out += f"{t.headline}\n\n"
            if t.headers and t.rows:
                out += render.table(t.headers, t.rows)
                out += "\n"

    out += "## 高风险发现（根因 → 引发请求 → 优化）\n\n"
    if not m.findings:
        out += "无确定性发现被解读。\n\n"
    for f in m.findings:
        out += f"### {f.sev.label()} {f.code} · {f.dim}\n\n"
        out += f"- 证据：{f.evidence_line}\n"
        out += f"- 根因：{f.root_cause}\n"
        if f.suggestions:
            out += "- 建议：\n"
            for s in f.suggestions:
                out += f"  - {s}\n"
        out += "\n"

    out += _emit_quarantine_md(m)
    out += _emit_next_steps(m)
    out += _emit_sql_appendix(m)
    out += "—— 本报告经脚本只读采集；结论已对确定性发现锚定校验，建议经实证。\n"
    return out


# 图示里每类最多画几条 —— 画满十条就成了另一张表,失去「一眼看出比例」的意义。
CHART_ROWS = 5


def _col(t, *names):
    """按列名找下标;找不到返回 -1。**不按下标取值** —— 列顺序变过一次,
    按下标取会静默地把另一列的数当成这一列。"""
    for n in names:
        for i, h in enumerate(t.headers or []):
            if str(h).startswith(n):
                return i
    return -1


def _emit_charts(m: _ReportModel) -> str:
    """图示:等待类占比、Top SQL 占 DB time。

    **为什么要有它:** 纯数字表格看不出倾斜 —— 「LOCK_EVENT 86%、IO_EVENT 10%、LWLOCK 3%」
    要读三个数再在脑子里比;一条占了八成的横条一眼就看出来。
    对话窗口渲染不了 SVG/图片,所以用方块字符;真正的曲线图与柱状图在大盘上。
    取不到数据就整段不出现(宁可没有图,也不要画一张空图)。
    """
    blocks = []
    for t in m.raw_tables:
        if t.title == "Wait Events / Classes":
            i_name, i_pct = _col(t, "等待类"), _col(t, "占比")
            if i_name >= 0 and i_pct >= 0:
                items = [(r[i_name], _f(r[i_pct])) for r in t.rows[:CHART_ROWS]]
                body = render.share_lines(items)
                if body:
                    blocks.append(("等待类占比（窗口内总等待时长的分布）", body))
        elif t.title == "Top SQL":
            i_sid, i_pct = _col(t, "sql_id"), _col(t, "占DB time", "占本榜合计")
            if i_sid >= 0 and i_pct >= 0:
                items = [(r[i_sid], _f(r[i_pct])) for r in t.rows[:CHART_ROWS]]
                body = render.share_lines(items)
                if body:
                    label = (t.headers or [])[i_pct]
                    blocks.append(("Top SQL 的 %s" % label, body))
    if not blocks:
        return ""
    out = "## 图示\n\n"
    for title, body in blocks:
        out += "**%s**\n\n" % title
        out += render.code_block("", body)
        out += "\n"
    return out


def _f(x) -> float:
    try:
        return float(str(x).replace("%", "").strip())
    except (TypeError, ValueError):
        return 0.0


# 发现码 → 下一步该跑什么。**由脚本生成,不靠模型自己想。**
#
# 为什么要钉成确定性输出:SKILL.md 里早就写了「带 sql_id 的先 sqltune 实证」,
# 可 2026-10-17 实测那一轮,模型给的 8 条建议一条都没去实证,报告自己也写着
# 「0 条建议经 hypopg/cost 实证」。规则在提示词里,就只是「希望它做」;
# 落成报告里的一个段落,用户才看得见该跑什么、也看得出有没有跑。
# WDR 这份数据根本看不到的东西。**每次都要写出来**:不写等于让用户以为已经查全了。
# 这里只说「没看什么」,不说「你去跑哪条命令」—— 要跑的由 followup.py 交代给模型。
_NOT_COVERED = [
    "**膨胀与回收水位**：死元组、autovacuum 追不追得上、回收被谁卡住（长事务 / 两阶段事务 /"
    "复制槽），WDR 的快照里都没有这些数 —— 实测一张有 540 万死元组的表，快照里记的是 0。",
    "**一直没跑完的 SQL**：WDR 只统计**已完成**的执行，正卡着没返回的语句在这里一行都没有。",
    "**窗口之前的历史**：快照只记当下的累计值，窗口开始前的膨胀程度、长事务已经开了多久，"
    "事后都重建不出来。",
]


def _emit_next_steps(m: _ReportModel) -> str:
    """本报告没覆盖到的范围 —— **用人话说,不给命令**。

    **第一版在这里列的是 `python3 ../gaussdb-explain/scripts/explain.py --sql-id 123`
    这样的命令行,那是错的。** 报告是逐字呈现给用户的最终输出:
      · 用户要的是执行计划本身,不是取执行计划的命令 —— 给命令等于把活推回给他,
        而他在聊天窗口里也跑不了;
      · 报告既然是最终输出,模型渲染完就收工,那几条命令一条都不会被执行。
    具体要跑什么,由 `followup.py` 在 collect 阶段从**标准错误**交代给模型,
    模型跑完把结论写进 interp.json,在这份报告里体现为根因与建议 —— 而不是一行待办。

    这一段只保留一件事:**说清楚这份报告没看哪些东西**。不说等于让用户以为已经查全了。
    """
    out = "## 本报告没覆盖的\n\n"
    for why in _NOT_COVERED:
        out += "- %s\n" % why
    return out + "\n"


# 附录里每条 SQL 最多展示这么长。再长的留标记,让人去大盘或 sqlfetch 取 ——
# 报告是给人读的,一条几 KB 的 SQL 糊在正文里谁也读不下去。
APPENDIX_SQL_MAX = 1200


def _emit_sql_appendix(m: _ReportModel) -> str:
    """SQL 附录:根因里提到的 sql_id → 完整 SQL 文本。

    **为什么要有附录(照 AWR 的做法):** 根因那几行里只写 sql_id 和一小段片段,
    读的人认不出是哪条语句;可要是把全文塞进根因,一条几百字的 SQL 会把结论冲掉。
    所以正文给 sql_id + 片段,全文集中在附录,再给出大盘与执行计划的去处。

    只列**根因里真正提到过**的 sql_id —— 把 Top SQL 全表的文本都搬下来,
    附录会比报告本身还长。
    """
    texts = m.sql_texts or {}
    if not texts:
        return ""
    wanted, seen = [], set()
    for f in m.findings:
        sid = getattr(f, "sql_id", "") or ""
        if sid and sid in texts and sid not in seen:
            seen.add(sid)
            wanted.append(sid)
    if not wanted:
        return ""
    out = "## SQL 附录（根因里提到的语句）\n\n"
    for sid in wanted:
        body = texts[sid]
        if len(body) > APPENDIX_SQL_MAX:
            body = body[:APPENDIX_SQL_MAX] + "\n…（全文更长，到大盘或用 gaussdb-sqlfetch 取）"
        out += "### sql_id `%s`\n\n" % sid
        out += render.code_block("sql", body)
        out += "\n"
        out += _emit_plan(m, sid)
    return out


# 附录里每条执行计划最多展示这么长。超长的计划(几十层嵌套)贴全了没人读得完,
# 关键永远在开头那几层 —— 真要逐层看,到大盘或重跑 explain。
APPENDIX_PLAN_MAX = 2500


def _emit_plan(m: _ReportModel, sid: str) -> str:
    """这条语句的执行计划。

    **计划是模型取回来放进 interp.json 的,不是报告里给一条命令让用户自己去跑。**
    第一版这里写的是「执行计划：`python3 …/explain.py --sql-id 123`（计划要实时连库，
    报告里不预先生成）」—— 那句话把本该做的事当成了做不到的事:计划确实要连库,
    但连库的是模型,它在写 interp.json 之前就该把计划取回来。
    (要跑哪些命令由 followup.py 在 collect 阶段从标准错误交代。)

    取不到也要如实留一行,说明没取到 —— 空着会让人以为这条语句没有计划可看。
    """
    hist = (m.hist_plans or {}).get(sid) or {}
    plan = (m.plans or {}).get(sid) or ""
    if str(hist.get("plan") or "").strip():
        # 2026-10-21 起采集时就取窗口内的真实执行计划;它讲的正是这段时间,优先于模型事后取的
        head = ("- 执行计划（SQL 历史里记下的真实执行计划：%s 那次执行，耗时 %s ms，返回 %s 行）："
                % (hist.get("start_time") or "?", hist.get("db_time_ms") or "?", hist.get("rows") or "?"))
        plan = hist["plan"]
    elif str(plan).strip():
        head = "- 执行计划："
    else:
        why = hist.get("why") or "计划要实时连库；若语句已不在库里或权限不足，会取不到"
        return "- 执行计划：本次未取到（%s）。\n\n" % why
    body = str(plan).rstrip()
    if len(body) > APPENDIX_PLAN_MAX:
        body = body[:APPENDIX_PLAN_MAX] + "\n…（计划更长，到大盘或重跑 explain 看全）"
    return head + "\n\n" + render.code_block("text", body) + "\n"


def _emit_quarantine_md(m: _ReportModel) -> str:
    out = ""
    if m.missing:
        out += "## ⚠ 模型遗漏\n\n"
        for s in m.missing:
            out += f"- {s}：确定性发现未被解读\n"
        out += "\n"
    if m.unanchored:
        out += "## ⚠ 未锚定（已剔除，不作正式发现）\n\n"
        for c in m.unanchored:
            out += f"- {c}：interp 引用的 Code 不在确定性发现中\n"
        out += "\n"
    if m.unverified:
        out += "## ⚠ 未验证建议（已剔除）\n\n"
        for s in m.unverified:
            out += f"- {s}\n"
        out += "\n"
    return out


# ---- ansi emitter ----

def _emit_ansi(m: _ReportModel, color: bool) -> str:
    w = m.window

    def sev_col(s, text):
        return colorize(text, severity_color(s), color)

    title = f"📊 WDR 报告 · {m.conn}"
    if m.target:
        title += f" ({m.target})"
    out = colorize(title, ANSI_BOLD, color) + "\n"
    out += (f"窗口 snap {w.begin_id} ({w.begin_ts}) → {w.end_id} ({w.end_ts})  "
            f"时长 {w.duration_min} 分钟  scope={w.scope} node={w.node}\n")
    out += f"总体状态 {sev_col(m.overall, m.overall.label())}  驱动：{m.driver}\n"
    out += f"判断校验 {m.badge}\n\n"

    out += colorize("维度概览", ANSI_BOLD, color) + "\n"
    mrows = [[r.dim, r.sev.label(), trunc(r.headline, 40)] for r in m.matrix]
    out += box_table(["维度", "严重度", "关键指标"], mrows)
    out += "\n"

    if m.raw_tables:
        out += colorize("全景分析（自顶向下）", ANSI_BOLD, color) + "\n"
        for t in m.raw_tables:
            out += colorize("【" + t.title + "】", ANSI_BOLD, color) + "\n"
            if t.headline:
                out += "  " + t.headline + "\n"
            out += box_table(t.headers, t.rows)
            out += "\n"

    out += colorize("高风险发现（根因 → 引发请求 → 优化）", ANSI_BOLD, color) + "\n"
    for f in m.findings:
        out += f"{sev_col(f.sev, f.sev.label())} {f.code} · {f.dim}\n"
        out += f"  证据  {f.evidence_line}\n"
        out += f"  根因  {f.root_cause}\n"
        for s in f.suggestions:
            out += f"  建议  {s}\n"
    out += "\n"

    out += _emit_quarantine_ansi(m, color)
    out += colorize("—— 脚本只读采集；结论已锚定校验，建议经实证。", ANSI_DIM, color) + "\n"
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

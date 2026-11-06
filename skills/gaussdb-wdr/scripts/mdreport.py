"""WDR 报告的 markdown 版式(2026-10-26 回答模板化 R7,需求 docs/design/2026-10-26-answer-template-requirements.md)。

用户看 WDR 最想知道的是:这段时间库忙不忙、时间花在哪、是哪几条语句造成的。所以正文是:

    开头      评估窗口 → 总体状态带 → 判断校验 → 短结论
    ① 负载画像  维度概览、等待类占比图、Load Profile / 库级统计 / 等待事件,外加「应用视角 / 数据库机制」两小段
    ② 问题与元凶  Top SQL 多维表(含「各维度元凶」行,原样保留)+ 每条发现:证据、两种视角、根因、引发它的语句、知识引用
    ③ 解决方案  按 P0 / P1 / P2 分组的建议(只放验证过的)
    ④ 总结和结论  总结、收尾一句、展开提议
    附录      其余全景数据、本报告没覆盖的、SQL 附录、校验区

版式由脚本固定,模型只写 interp.json 的字段 —— 每次长得一样,不靠提示词。
"""
from __future__ import annotations

from typing import Dict, List, Tuple

import render
from appendix import _emit_next_steps, _emit_quarantine_md, _emit_sql_appendix
from interp import PRIORITIES
from model import DIM_DBSTAT, DIM_LOADPROFILE, DIM_TOPSQL, DIM_WAITS

LOAD_DIMS = (DIM_LOADPROFILE, DIM_DBSTAT, DIM_WAITS)     # ① 负载画像里的表
CULPRIT_DIMS = (DIM_TOPSQL,)                             # ② 问题与元凶里的表
_PRIORITY_LABEL = {"P0": "P0(最优先)", "P1": "P1(次优先)", "P2": "P2"}
CLOSING = "—— 本报告经脚本只读采集；结论已对确定性发现锚定校验，建议经实证。"

# 图示里每类最多画几条 —— 画满十条就成了另一张表,失去「一眼看出比例」的意义。
CHART_ROWS = 5


def split_tables(raw_tables: list) -> Tuple[list, list, list]:
    """全景表按落在哪一段分三组:① 负载画像、② 问题与元凶、附录。组内保持原来的自顶向下顺序。"""
    load = [t for t in raw_tables if t.title in LOAD_DIMS]
    culprit = [t for t in raw_tables if t.title in CULPRIT_DIMS]
    rest = [t for t in raw_tables if t.title not in LOAD_DIMS + CULPRIT_DIMS]
    return load, culprit, rest


def _col(t, *names):
    """按列名找下标;找不到返回 -1。**不按下标取值** —— 列顺序变过一次,
    按下标取会静默地把另一列的数当成这一列。"""
    for n in names:
        for i, h in enumerate(t.headers or []):
            if str(h).startswith(n):
                return i
    return -1


def _f(x) -> float:
    try:
        return float(str(x).replace("%", "").strip())
    except (TypeError, ValueError):
        return 0.0


def _chart(t) -> str:
    """一张方块字符图:等待类占比、Top SQL 占 DB time。

    **为什么要有它:** 纯数字表格看不出倾斜 —— 「LOCK_EVENT 86%、IO_EVENT 10%、LWLOCK 3%」
    要读三个数再在脑子里比;一条占了八成的横条一眼就看出来。
    对话窗口渲染不了 SVG/图片,所以用方块字符;真正的曲线图与柱状图在大盘上。
    取不到数据就不出现(宁可没有图,也不要画一张空图)。
    """
    if t.title == "Wait Events / Classes":
        i_name, i_pct, title = _col(t, "等待类"), _col(t, "占比"), "等待类占比（窗口内总等待时长的分布）"
    elif t.title == "Top SQL":
        i_name, i_pct = _col(t, "sql_id"), _col(t, "占DB time", "占本榜合计")
        title = "Top SQL 的 %s" % (t.headers or [])[i_pct] if i_pct >= 0 else ""
    else:
        return ""
    if i_name < 0 or i_pct < 0:
        return ""
    body = render.share_lines([(r[i_name], _f(r[i_pct])) for r in t.rows[:CHART_ROWS]])
    return "**%s**\n\n%s\n" % (title, render.code_block("", body)) if body else ""


def _table(t) -> str:
    out = "### %s\n\n" % t.title
    if t.headline:
        out += "%s\n\n" % t.headline
    if t.headers and t.rows:
        out += render.table(t.headers, t.rows) + "\n"
    return out


def _two_views(app: str, mech: str) -> str:
    out = ""
    if app:
        out += "应用视角：%s\n\n" % app
    if mech:
        out += "数据库机制：%s\n\n" % mech
    return out


def demote(text: str) -> str:
    """标题降一级(挂到「## 附录」下);代码块里的行不动。"""
    out, in_code = [], False
    for line in text.split("\n"):
        if line.startswith("```"):
            in_code = not in_code
        out.append("#" + line if (line.startswith("#") and not in_code) else line)
    return "\n".join(out)


def _head(m) -> str:
    w = m.window
    out = "# WDR 报告 — %s%s\n\n" % (m.conn, " (%s)" % m.target if m.target else "")
    out += ("评估窗口：snap %s (%s) → snap %s (%s)，时长 %s 分钟，scope=%s node=%s\n\n"
            % (w.begin_id, w.begin_ts, w.end_id, w.end_ts, w.duration_min, w.scope, w.node))
    out += "总体状态 %s  驱动：%s\n\n" % (m.overall.label(), m.driver)
    out += "判断校验 %s\n\n" % m.badge
    if m.summary:
        out += "**短结论**：%s\n\n" % m.summary
    return out


def _load_section(m, load: list) -> str:
    out = "## ① 负载画像\n\n"
    out += render.table(["维度", "严重度", "关键指标"], [[r.dim, r.sev.label(), r.headline] for r in m.matrix]) + "\n"
    out += _two_views(m.load_app_view, m.load_mechanism)
    for t in load:
        out += _chart(t) + _table(t)
    return out


def _finding(m, f) -> str:
    out = "### %s %s · %s\n\n- 证据：%s\n" % (f.sev.label(), f.code, f.dim, f.evidence_line)
    if f.app_view:
        out += "- 应用视角：%s\n" % f.app_view
    if f.mechanism:
        out += "- 数据库机制：%s\n" % f.mechanism
    out += "- 根因：%s\n" % f.root_cause
    if f.sql_id:
        where = "（全文与执行计划见附录）" if f.sql_id in (m.sql_texts or {}) else ""
        out += "- 引发它的语句：sql_id `%s`%s\n" % (f.sql_id, where)
    for line in f.kb_lines:
        out += "- %s\n" % line
    return out + "\n"


def _culprit_section(m, culprit: list) -> str:
    out = "## ② 问题与元凶\n\n"
    for t in culprit:
        out += _table(t) + _chart(t)
    if not m.findings:
        out += "无确定性发现被解读。\n\n"
    for f in m.findings:
        out += _finding(m, f)
    return out


def _solutions(m) -> str:
    out = "## ③ 解决方案\n\n"
    groups: Dict[str, List[str]] = {p: [] for p in PRIORITIES}
    for f in m.findings:
        groups.setdefault(f.priority, []).extend("%s：%s" % (f.code, s) for s in f.suggestions)
    if not any(groups.values()):
        return out + "本次没有经验证的优化建议（没通过验证的见附录的校验区）。\n\n"
    for p in PRIORITIES:
        if groups.get(p):
            out += "### %s\n\n%s\n" % (_PRIORITY_LABEL[p], "".join("- %s\n" % s for s in groups[p]))
    return out


def _conclusion(m) -> str:
    out = "## ④ 总结和结论\n\n%s\n\n%s\n\n" % (m.conclusion or "本次未给出总结。", CLOSING)
    if m.followups:
        out += "".join("- %s\n" % q for q in m.followups) + "\n"
    return out


def _kb_dropped(m) -> str:
    if not m.kb_dropped:
        return ""
    return "## ⚠ 知识引用未展示（编号核对不过）\n\n" + "".join("- %s\n" % x for x in m.kb_dropped) + "\n"


def emit_markdown(m) -> str:
    load, culprit, rest = split_tables(m.raw_tables)
    out = _head(m) + _load_section(m, load) + _culprit_section(m, culprit) + _solutions(m) + _conclusion(m)
    appendix = ""
    if rest:
        appendix += "## 其余全景数据\n\n" + "".join(_table(t) for t in rest)
    appendix += _emit_next_steps(m) + _emit_sql_appendix(m) + _emit_quarantine_md(m) + _kb_dropped(m)
    return out + "## 附录\n\n" + demote(appendix)

"""Render the health evidence pack — port of internal/probe/health/report.go.

Fixed ## sections so the skill can parse the output deterministically.

sub_results（aggregate.SubSkillResult 列表，已按 --include/--exclude 过滤过
scope）驱动报告顶部两段固定小节：「本次未采集到的维度」「未纳入汇总的能力」。
**两段都判断 `.ok`，绝不判断 `len(.findings)`** —— ok=False 时 findings 必然
是空列表（aggregate.py 的契约），跟"确实没查出风险"的 ok=True 长得一样，
唯一能分辨的字段是 ok。见 tests/test_health_handover_units.py 里专门盯这条的
test_a_clean_sub_skill_is_not_confused_with_a_failed_one_by_list_length。
"""
from __future__ import annotations

import json

import aggregate
import render
from model import HealthEvidence


def _uncovered_section() -> str:
    out = "## 未纳入汇总的能力\n\n"
    out += ("以下 skill 需要用户指定 SQL / sql_id / 存储过程才能运行，health "
           "没有这类输入，本次未纳入汇总 —— **不代表这些方面没有问题，只是没有"
           "被检查**：\n\n")
    for skill in aggregate.NEEDS_TARGET:
        out += f"- {skill}\n"
    out += "\n"
    return out


def _missing_section(sub_results, no_sub_note: str = "") -> str:
    out = "## 本次未采集到的维度\n\n"
    failed = [r for r in sub_results if not r.ok]   # 只看 ok，不看 len(findings)
    if failed:
        for r in failed:
            out += f"- **{r.skill}**：{r.error}\n"
        out += ("\n以上维度**不是没有风险，是没有查到** —— 请单独运行对应 skill "
               "复核，不要把这次报告当成这些维度已经确认健康。\n\n")
    elif sub_results:
        names = "、".join(r.skill for r in sub_results)
        out += f"无：本次纳入的子 skill（{names}）全部采集成功。\n\n"
    elif no_sub_note:
        out += no_sub_note + "\n\n"     # 调用方知道为什么没跑(如时段体检回看过去),说真实原因
    else:
        out += ("本次没有子 skill 纳入范围（--include/--exclude 排除了全部"
               "锁/等待/膨胀相关维度，或调用方未提供 sub_results）。\n\n")
    return out


def kb_section(findings) -> tuple:
    """「客户知识库参照」:按每条 finding 查客户知识库,固定小节 + JSON。

    脚本层接入而不是靠提示词——模型是在已经看到客户先例的前提下作答的,"有没有看到"
    可以断言。知识库不存在 / 没配 / 连不上,小节只剩「> 知识库未接入(原因)」,报告照常。
    没装 common/kb(旧安装)也一样降级,不许让健康检查因为知识库炸掉。
    """
    try:
        from common.kb import query as kbquery
    except ImportError as exc:
        text = f"## 客户知识库参照\n> 知识库未接入(common/kb 未安装:{exc})\n\n"
        return text, {"status": {"attached": False, "reason": f"common/kb 未安装:{exc}"}, "items": []}
    return kbquery.section_for(list(findings))


def health_dict(ev: HealthEvidence, sub_results=()) -> dict:
    """JSON 形态。--format json 打它,存档也存它 —— 大盘读的就是这个形状,一份定义。"""
    d = ev.to_dict()
    d["sub_skills"] = [{"skill": r.skill, "ok": r.ok, "error": r.error}
                       for r in sub_results]
    d["uncovered_capabilities"] = list(aggregate.NEEDS_TARGET)
    d["kb_refs"] = kb_section(ev.findings)[1]
    return d


def render_health_json(ev: HealthEvidence, sub_results=()) -> str:
    return json.dumps(health_dict(ev, sub_results=sub_results), ensure_ascii=False, indent=2)


def _evidence_with_source(f) -> str:
    """子 skill 产的 finding 带着 skill 来源——混进主表后要让人知道详情去哪查。
    本地 8 个维度产的 finding 没有 skill（空串），不安一个假来源。"""
    if f.skill:
        return f"{f.evidence}（详见 {f.skill}）"
    return f.evidence


def render_health(ev: HealthEvidence, sub_results=(), no_sub_note: str = "") -> str:
    if ev.target:
        out = f"# Health Evidence — {ev.conn} ({ev.target})\n\n"
    else:
        out = f"# Health Evidence — {ev.conn}\n\n"
    # 容器里每个 Pod 一个人(GSDB_USER_ID 由平台注入):报告抬头写明执行人,审计落到人。
    # 单机沙箱没有这个变量,这一行不出现,报告形状不变。
    from common.audit import actor_id
    if actor_id():
        out = out.rstrip("\n") + f"\n执行人：{actor_id()}\n\n"
    out += f"总体状态：{ev.overall.label()}\n\n"

    # 顶部两段固定小节：先说清楚这次没查到什么、结构性覆盖不到什么，
    # 再进正文——不能让读者先看到一份干净的报告，才在最后发现漏了什么。
    out += _missing_section(sub_results, no_sub_note)
    out += _uncovered_section()
    # 第三段固定小节:客户知识库对每条发现怎么说(条款 / 相似案例 / 本行历史路径)。
    # 放在维度正文之前——处置建议要以它为首选依据,不能让读者(模型)先看完通用分析再想起它。
    out += kb_section(ev.findings)[0]

    for d in ev.dims:
        out += f"## {d.dimension}\n\n"
        if not d.available:
            out += f"> 不可用：{d.note}\n\n"
            continue
        if d.headline:
            out += f"{d.headline}\n\n"
        if d.headers and d.rows:
            out += render.table(d.headers, d.rows)
            out += "\n"

    out += "## Deterministic Findings\n\n"
    if not ev.findings:
        out += "无（所有维度未越阈值）。\n\n"
    else:
        rows = [[f.severity.label(), f.dimension, f.code, f.metric, f.value,
                f.threshold, _evidence_with_source(f)]
                for f in ev.findings]
        out += render.table(["严重度", "维度", "Code", "指标", "值", "阈值", "证据"], rows)
        out += "\n"

    out += "## Collection Notes\n\n"
    any_degraded = False
    for d in ev.dims:
        if not d.available:
            out += f"- {d.dimension}：降级（{d.note}）\n"
            any_degraded = True
    for r in sub_results:
        if not r.ok:
            out += f"- {r.skill}：降级（{r.error}）\n"
            any_degraded = True
    if not any_degraded:
        out += "- 全部维度采集成功。\n"
    return out

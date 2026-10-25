"""「客户知识库参照」固定小节 —— 所有接入 skill 共用同一个渲染器。

纪律(设计稿 §5.1):
  · 每条发现一段;没命中就明写「无对应条款 / 无相似案例 / 路径:无」——省略和「没查」分不清;
  · 每个引用带 ID + 出处 + 结论强度,脚本能验;
  · 第一行状态行永远存在;未接入时整节只剩「> 知识库未接入(原因)」;
  · 不做顶部「违规汇总」——规范只跟在它解释的那条发现后面。

2026-10-24 渐进式披露:状态行下面两行是 L0 摘要(本实例画像、八类知识各多少);每条发现先给
L1 的入口(专题 + 本行确认过的候选原因),再给条目引用。条目引用只是摘要,要引用细节先 `kb.py show <ID>`。
"""
from __future__ import annotations

from typing import Sequence

from .query import FindingRefs, KbStatus, QueryResult, Ref

SECTION_TITLE = "## 客户知识库参照"
NOT_ATTACHED = "> 知识库未接入"


def status_line(status: KbStatus) -> str:
    if not status.attached:
        return f"{NOT_ATTACHED}({status.reason})" if status.reason else NOT_ATTACHED
    c = status.counts
    ver = f" v{status.version}" if status.version else ""
    mode = f" · 模式:{status.mode}" if status.mode else ""
    if status.mode == "文件" and status.reason:          # 为什么退到了文件:不藏,写在状态行里
        mode += f"({status.reason})"
    return (f"> 知识库{ver}{mode} · 条款 {c.get('docs.rule', 0)} · 案例 {c.get('docs.case', 0)} · "
            f"原始工单 {c.get('docs.raw', 0)} · 向量:{status.vector} · 图:{status.graph}")


def _clause_line(r: Ref) -> str:
    sev = r.meta.get("severity") or ""
    src = f" ——{r.source}" if r.source else ""
    return f"- **贵行规范** {r.short_id}《{r.title}》" + (f"({sev})" if sev else "") + src


def _case_line(r: Ref) -> str:
    meta = r.meta or {}
    conclusion = meta.get("conclusion") or "?"
    when = meta.get("occurred_at") or ""
    action = (r.sections.get("处置") or "").strip().replace("\n", " ")
    action = action[:120] + ("…" if len(action) > 120 else "")
    head = f"- **历史相似** {r.short_id}(结论强度:{conclusion}" + (f",{when}" if when else "") + ")"
    return head + (f":处置 = {action}" if action else f":{r.snippet[:100]}")


def _path_line(p) -> str:
    support = f"({p.support} 案例支持:{'、'.join(p.cases[:3])})" if p.cases else "(无案例佐证)"
    return f"- **本行历史路径** {p.symptom} → {p.rootcause} → {p.action} {support}"


def _raw_line(r: Ref) -> str:
    return f"- **原始工单** {r.short_id}(未结构化):{r.snippet[:100].replace(chr(10), ' ')}"


def _guide_line(r: Ref) -> str:
    return f"- **相关指南** {r.short_id}《{r.title}》:{r.snippet[:100].replace(chr(10), ' ')}"


# 六类类型化条目的引用行:前缀名 + 括号里放验证状态 / 厂商结论 / 系统
_TYPED_LABEL = {"runbook": "处置手册", "defect": "已知缺陷", "baseline": "贵行基线", "product": "产品文档",
                "wiki": "Wiki 经验", "profile": "环境画像"}


def _typed_line(r: Ref) -> str:
    m = r.meta or {}
    label = _TYPED_LABEL.get(r.kind, r.kind)
    tag = ""
    if r.kind == "baseline":
        tag = "、".join(s for s in (f"系统 {m.get('system')}" if m.get("system") else "",
                                   f"单位 {m.get('unit')}" if m.get("unit") else "",
                                   str(m.get("value_kind") or "")) if s)
    elif r.kind == "product":
        tag = " ".join(str(m.get(k) or "") for k in ("product", "versions")).strip()
    elif r.kind == "defect":
        tag = f"厂商{m.get('vendor_status')}" if m.get("vendor_status") else ""
    elif m.get("verification"):
        tag = str(m["verification"])
    head = f"- **{label}** {r.short_id}《{r.title}》" + (f"({tag})" if tag else "")
    applies = str(m.get("_applies") or "")
    if applies:
        head += f"〔{applies}〕"
    body = ""
    if r.kind == "baseline":
        body = str((m.get("sections") or {}).get("正常表现") or m.get("summary") or "")
    else:
        body = str(m.get("summary") or r.snippet or "")
    body = " ".join(body.split())
    return head + (f":{body[:120]}" + ("…" if len(body) > 120 else "") if body else "")


def render_item(item: FindingRefs) -> str:
    lines = [f"### 对 {item.label}".rstrip()]
    lines += list(item.hints)
    if item.empty:
        lines.append("- 贵行规范:无对应条款 · 历史相似:无相似案例 · 路径:无")
    else:
        lines += [_clause_line(r) for r in item.clauses] or ["- 贵行规范:无对应条款"]
        lines += [_case_line(r) for r in item.cases] or ["- 历史相似:无相似案例"]
        lines += [_path_line(p) for p in item.paths] or ["- 本行历史路径:无(没有已确认的 现象→根因→处置 链)"]
        lines += [_typed_line(r) for r in item.typed]
        lines += [_raw_line(r) for r in item.raws]
        lines += [_guide_line(r) for r in item.guides]
    for n in item.notes:
        lines.append(f"- _({n})_")
    return "\n".join(lines) + "\n"


def render_section(result: QueryResult) -> str:
    """整节:标题 + 状态行 + 每条发现一段。未接入时只有标题 + 状态行。"""
    out = [SECTION_TITLE, status_line(result.status)] + list(result.status.l0) + [""]
    if not result.status.attached:
        return "\n".join(out[:2]) + "\n\n"
    if not result.items:
        out.append("(本次没有发现需要对照)\n")
        return "\n".join(out)
    out += [render_item(it) for it in result.items]
    if any(it.typed or it.cases or it.clauses for it in result.items):
        out.append("_(以上是摘要:要引用某条的步骤、审批、版本结论,先 `kb.py show <ID>` 读全文;核对原文 `kb.py evidence <ID>`)_\n")
    return "\n".join(out)


def render_query(result: QueryResult) -> str:
    """`kb.py query --q` 的输出:同一格式,标题换成问题本身。"""
    return render_section(result)

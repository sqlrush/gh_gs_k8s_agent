"""Report rendering (pure functions — no DB, no rule evaluation).

Two blocks, and the boundary between them is the point: `Deterministic Findings`
are facts the script proved; `Advisory` items are rules the script cannot decide,
handed to the LLM together with the evidence it needs to decide them.

唯一读外部的是 markdown 末尾的「客户知识库参照」小节(读知识库文件 / 索引,不连业务库;
失败只降级成「未接入」,见 common/kbsection.py)。它是参考,不改判定。
"""
from __future__ import annotations

import json

import render
from common import kbsection
from model import Finding, ReviewResult, Severity, severity_icon, severity_name, sort_findings

_DET_TITLE = "## Deterministic Findings"
_ADV_TITLE = "## Advisory（需结合证据判断）"
_KB_WHERE_MAX = 3           # 一条规则命中多处时,检索文本里带前几处位置(对象名能对上客户条款与案例)


def _counts(findings) -> dict[str, int]:
    out = {"error": 0, "warn": 0, "info": 0, "advisory": 0}
    for f in findings:
        if f.advisory:
            out["advisory"] += 1
        else:
            out[severity_name(f.severity)] += 1
    return out


def _headline(res: ReviewResult, counts: dict[str, int]) -> str:
    scope = []
    if res.statements:
        scope.append(f"{res.statements} 条语句")
    if res.objects:
        scope.append(f"{res.objects} 个对象")
    verdict = "❌ 不通过" if counts["error"] else ("⚠️ 有告警" if counts["warn"] else "✅ 通过")

    return (f"# SQL 规范审查\n\n"
            f"- 来源：`{res.source}`\n"
            f"- 范围：{('、'.join(scope)) or '（空）'}\n"
            f"- 结论：**{verdict}** "
            f"（error {counts['error']} / warn {counts['warn']} / info {counts['info']}"
            f" / advisory {counts['advisory']}）\n")


def _finding_block(f: Finding) -> str:
    lines = [f"### {severity_icon(f.severity)} [{severity_name(f.severity)}] "
             f"{f.rule_id} {f.rule_name}",
             "",
             f"- 位置：`{f.location}`",
             f"- 说明：{f.message}"]
    if f.snippet:
        lines.append(f"- 语句：`{f.snippet}`")
    if f.rationale:
        lines.append(f"- 依据：{f.rationale}")
    if f.fix:
        lines.append(f"- 整改：{f.fix} [需人工执行]")
    if f.evidence:
        lines.append("- 证据：")
        lines += [f"  - {e}" for e in f.evidence]
    return "\n".join(lines) + "\n"


def render_markdown(res: ReviewResult) -> str:
    findings = sort_findings(res.findings)
    determ = [f for f in findings if not f.advisory]
    advisory = [f for f in findings if f.advisory]
    counts = _counts(findings)

    parts = [_headline(res, counts)]

    if res.notes:
        parts.append("\n".join(f"> 注：{n}" for n in res.notes) + "\n")

    parts.append(_DET_TITLE + "\n")
    if determ:
        parts.append(render.table(
            ["#", "级别", "规则", "位置", "说明"],
            [[str(i + 1), severity_name(f.severity), f"{f.rule_id} {f.rule_name}",
              f.location, render.truncate(f.message, 60)]
             for i, f in enumerate(determ)]))
        parts.append("")
        parts += [_finding_block(f) for f in determ]
    else:
        parts.append("未发现违规。\n")

    if advisory:
        parts.append(_ADV_TITLE + "\n")
        parts.append("以下规则脚本无法确定性判定，已附证据，请逐条判断：\n")
        parts += [_finding_block(f) for f in advisory]

    # 客户规范与先例对这些违规怎么说(参考,不改判定 —— 见 SKILL.md);JSON 输出不带(形状不变)
    parts.append(kbsection.markdown(kb_items(res)))
    return "\n".join(parts)


def kb_items(res: ReviewResult) -> list:
    """给知识库检索的「发现」:每条命中的规则一项(纯函数)。

    同一条规则常常命中几十处(--top 50 里每条 SELECT * 一处);逐处查出来是同一段参照重复几十遍,
    所以按规则合并,证据带规则名、第一处的说明与语句、前几处位置。advisory 规则同样要查 ——
    脚本判不了的,恰恰最需要客户自己的条款来参照。
    """
    groups: dict = {}
    for f in sort_findings(res.findings):
        groups.setdefault(f.rule_id, []).append(f)
    items = []
    for rule_id, fs in groups.items():
        f = fs[0]
        where = " ".join(list(dict.fromkeys(x.location for x in fs if x.location))[:_KB_WHERE_MAX])
        sev = "advisory" if f.advisory else severity_icon(f.severity) + severity_name(f.severity)
        items.append(kbsection.Item(code=rule_id, dimension="sqlreview", metric=f.rule_name,
                                    value=f"{len(fs)} 处",
                                    evidence=" ".join(p for p in (f.rule_name, f.message, f.snippet, where) if p),
                                    severity=sev))
    return items


def render_json(res: ReviewResult) -> str:
    findings = sort_findings(res.findings)
    payload = {
        "source": res.source,
        "statements": res.statements,
        "objects": res.objects,
        "notes": list(res.notes),
        "summary": _counts(findings),
        "findings": [
            {
                "rule_id": f.rule_id,
                "rule_name": f.rule_name,
                "severity": severity_name(f.severity),
                "advisory": f.advisory,
                "message": f.message,
                "location": f.location,
                "snippet": f.snippet,
                "rationale": f.rationale,
                "fix": f.fix,
                "evidence": list(f.evidence),
            }
            for f in findings
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)

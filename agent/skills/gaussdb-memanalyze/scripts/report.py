"""Report rendering (pure — no DB, no judgement).

The capability probe goes first, on purpose: before reading a single number the
user must know which layers had data and which were blind, and why.

唯一读外部的是 markdown 里的「客户知识库参照」小节(读知识库文件 / 索引,不连业务库;
失败只降级成「未接入」,见 common/kbsection.py)。
"""
from __future__ import annotations

import json

import render
from common import kbsection
from model import (
    DIM_CONFIG, DIM_CONTEXT, DIM_INSTANCE, DIM_OPERATOR, DIM_SESSION, DIM_SQL,
    MemEvidence, Severity,
)

_LAYER_ROWS = (
    ("L1 实例级", DIM_INSTANCE, "instance", None),
    ("L2 上下文", DIM_CONTEXT, "session_ctx", "L2"),
    ("L3 会话级", DIM_SESSION, "session_mem", None),
    ("L4 SQL 级", DIM_SQL, "wlm_session", "L4"),
    ("L5 算子级", DIM_OPERATOR, "wlm_operator", "L5"),
    ("L6 配置面", DIM_CONFIG, None, None),
)


def _capability_block(ev: MemEvidence) -> str:
    lines = ["## 能力与视图探测", ""]
    for label, _dim, slot, reason_key in _LAYER_ROWS:
        reason = ev.capability.reasons.get(reason_key) if reason_key else None
        if slot is None:
            lines.append(f"- {label}   ✓ （读自 pg_settings）")
            continue
        vi = ev.catalog.get(slot)
        if reason:
            lines.append(f"- {label}   ✗ 不可用：{reason}")
        elif vi.available:
            lines.append(f"- {label}   ✓ {vi.name}（{len(vi.columns)} 列）")
        else:
            lines.append(f"- {label}   ✗ 不可用：{vi.reason}")
    lines.append("")
    lines.append("> 标 ✗ 的层**没有数据**，不代表该层没有问题。")
    return "\n".join(lines) + "\n"


def _findings_block(ev: MemEvidence) -> str:
    findings = sorted(ev.findings, key=lambda f: -int(f.severity))
    if not findings:
        return "## Deterministic Findings\n\n未发现越过阈值的内存问题。\n"

    body = [[f.severity.label(), f.code, f.dimension, f.metric, f.value,
             f.threshold] for f in findings]
    out = ["## Deterministic Findings", "",
           render.table(["级别", "代码", "层", "指标", "实测值", "阈值"], body), ""]
    for f in findings:
        out.append(f"- **{f.code}**（{f.severity.label()}）：{f.evidence}")
    return "\n".join(out) + "\n"


def _dim_block(d) -> str:
    out = [f"## {d.dimension}", ""]
    if not d.available:
        out.append(f"不可用：{d.note}\n")
        return "\n".join(out)
    if d.headline:
        out.append(d.headline + "\n")
    if d.note:                      # a resolvable-but-empty layer must explain itself
        out.append(f"> {d.note}\n")
    if d.headers and d.rows:
        out.append(render.table(d.headers, [[str(c) for c in r] for r in d.rows]))
    elif not d.rows:
        out.append("（无数据）\n")
    return "\n".join(out)


def render_markdown(ev: MemEvidence) -> str:
    parts = [
        f"# 动态内存分析（{ev.mode}）\n",
        f"- 连接：`{ev.conn}`",
        f"- 范围：{ev.target}",
        f"- 总体：{ev.overall.label()}\n",
    ]
    if ev.notes:
        parts.append("\n".join(f"> 注：{n}" for n in ev.notes) + "\n")
    parts.append(_capability_block(ev))
    parts.append(_findings_block(ev))
    # 客户知识库对每条发现怎么说 —— 紧跟在确定性发现之后、各层明细之前(与 health 同一位置):
    # 处置建议以客户先例为首选依据,不能让读者看完通用分析再想起它。JSON 输出不带(形状不变)。
    parts.append(kbsection.markdown(ev.findings))
    parts += [_dim_block(d) for d in ev.dims]
    return "\n".join(parts)


def render_json(ev: MemEvidence) -> str:
    return json.dumps(ev.to_dict(), ensure_ascii=False, indent=2)

"""跨 skill 的风险表达 —— 一份，不是三份。

health / wdr / memanalyze 原先各存一份字段完全相同的 Finding。
汇总层出现之后这就不只是重复：health 要解析子 skill 的 json，
三份定义一旦哪份多加一个字段，汇总侧读到的东西就和产出侧对不上，
而 json 解析对不上的表现往往是**少一条风险**，不是报错。

severity 是脚本按阈值判定的确定性等级，**LLM 不得更改**。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from enum import IntEnum
from typing import Any


class Severity(IntEnum):
    OK = 0
    NOTICE = 1
    WARN = 2
    CRITICAL = 3

    def label(self) -> str:
        return {
            Severity.CRITICAL: "🔴严重",
            Severity.WARN: "🟠告警",
            Severity.NOTICE: "🟡关注",
        }.get(self, "🟢健康")


def worst(severities: list[Severity]) -> Severity:
    """取最坏的一档；空列表是 OK。"""
    w = Severity.OK
    for s in severities:
        if s > w:
            w = s
    return w


@dataclass(frozen=True)
class Finding:
    """一条越过阈值的确定性观察。

    code 是稳定标识：报告、SKILL.md 的验收闸、汇总层都靠它交叉引用，
    改 code 等于改对外接口。
    """
    dimension: str
    code: str
    severity: Severity
    metric: str
    value: str
    threshold: str
    evidence: str
    sql_id: str = ""
    skill: str = ""

    def to_dict(self) -> dict:
        return {
            "dimension": self.dimension,
            "code": self.code,
            "severity": int(self.severity),   # 跨进程要能比大小，别给字符串
            "metric": self.metric,
            "value": self.value,
            "threshold": self.threshold,
            "evidence": self.evidence,
            "sql_id": self.sql_id,
            "skill": self.skill,
        }


_REQUIRED = ("dimension", "code", "severity", "metric", "value",
             "threshold", "evidence")


def findings_to_json(findings: list[Finding], skill: str) -> str:
    """序列化成 health 认得的形状，并盖上来源 skill 名与执行人（GSDB_USER_ID，没有就是空串）。"""
    from .audit import actor_id
    stamped = [replace(f, skill=skill) for f in findings]
    return json.dumps(
        {"skill": skill, "user_id": actor_id(), "findings": [f.to_dict() for f in stamped]},
        ensure_ascii=False, indent=2)


def findings_from_json(text: str) -> list[Finding]:
    """解析子 skill 的输出。**形状不对当场抛，不返回空列表。**

    空列表会被汇总层读成「这个 skill 没查出风险」—— 那和「没解析出来」
    是两件事，而前者会让一份漏了整个维度的报告看起来一切正常。
    """
    try:
        payload: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("findings json 解析失败：%s" % exc) from exc
    if not isinstance(payload, dict) or "findings" not in payload:
        raise ValueError("findings json 缺 'findings' 键，拿到的是：%r"
                         % (list(payload)[:5] if isinstance(payload, dict) else type(payload).__name__))
    findings = payload["findings"]
    if not isinstance(findings, list):
        raise ValueError("findings json 的 'findings' 应为列表，拿到的是：%s"
                         % type(findings).__name__)
    out = []
    for i, raw in enumerate(findings):
        if not isinstance(raw, dict):
            raise ValueError("第 %d 条 finding 应为对象，拿到的是：%s"
                             % (i, type(raw).__name__))
        missing = [k for k in _REQUIRED if k not in raw]
        if missing:
            raise ValueError("第 %d 条 finding 缺字段 %s" % (i, missing))
        out.append(Finding(
            dimension=raw["dimension"], code=raw["code"],
            severity=Severity(int(raw["severity"])),
            metric=raw["metric"], value=raw["value"],
            threshold=raw["threshold"], evidence=raw["evidence"],
            sql_id=raw.get("sql_id", ""),
            skill=raw.get("skill", "") or payload.get("skill", ""),
        ))
    return out

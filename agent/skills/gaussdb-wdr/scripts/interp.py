"""LLM interp.json schema + loaders — port of internal/probe/wdr/interp.go.

The render step mechanically re-checks the interp against the evidence pack's
deterministic findings (see recheck.py) — it is never trusted blindly.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from model import Evidence, Severity


@dataclass(frozen=True)
class Validation:
    method: str = ""   # hypopg | cost-rewrite | none
    status: str = ""   # verified | failed | n/a
    evidence: str = ""

    @staticmethod
    def from_dict(d: Optional[dict]) -> "Optional[Validation]":
        if not d:
            return None
        return Validation(method=d.get("method", ""), status=d.get("status", ""),
                          evidence=d.get("evidence", ""))


@dataclass(frozen=True)
class Suggestion:
    text: str = ""
    risk: str = ""     # 低 | 中 | 高
    manual: bool = False
    validation: Optional[Validation] = None

    @staticmethod
    def from_dict(d: dict) -> "Suggestion":
        return Suggestion(text=d.get("text", ""), risk=d.get("risk", ""),
                          manual=bool(d.get("manual", False)),
                          validation=Validation.from_dict(d.get("validation")))


@dataclass(frozen=True)
class InterpFinding:
    code: str = ""
    root_cause: str = ""
    sql_id: str = ""
    suggestions: list = field(default_factory=list)

    @staticmethod
    def from_dict(d: dict) -> "InterpFinding":
        return InterpFinding(
            code=d.get("code", ""), root_cause=d.get("rootCause", ""),
            sql_id=d.get("sqlId", ""),
            suggestions=[Suggestion.from_dict(s) for s in d.get("suggestions", []) or []])


@dataclass(frozen=True)
class InterpOverall:
    severity: str = ""   # OK | NOTICE | WARN | CRITICAL
    driver: str = ""


@dataclass(frozen=True)
class Interp:
    overall: InterpOverall = field(default_factory=InterpOverall)
    verification_badge: str = ""
    findings: list = field(default_factory=list)
    # sql_id → 执行计划正文。模型在写 interp 之前跑 explain 取回来,附录里**直接展示计划**,
    # 而不是给用户一条「你去跑 explain」的命令 —— 他要的是计划,不是取计划的办法。
    plans: dict = field(default_factory=dict)

    @staticmethod
    def from_dict(d: dict) -> "Interp":
        ov = d.get("overall", {}) or {}
        return Interp(
            overall=InterpOverall(severity=ov.get("severity", ""), driver=ov.get("driver", "")),
            verification_badge=d.get("verificationBadge", ""),
            findings=[InterpFinding.from_dict(f) for f in d.get("findings", []) or []],
            plans=_plans_from_dict(d.get("plans")))


def _plans_from_dict(raw) -> dict:
    """interp 里的执行计划。两种写法都认:

      `{"plans": {"<sql_id>": "<计划正文>"}}`
      `{"plans": [{"sqlId": "...", "plan": "..."}]}`

    两种都认是因为「写成对象还是数组」这种事值不得让一次分析失败重跑 ——
    而写错了又恰好不报错、最后附录里静默地没有计划,是更坏的结果。
    空值/坏类型一律丢掉(附录会如实写「本次未取到」),绝不把 None 塞进正文。
    """
    out: dict = {}
    if isinstance(raw, dict):
        items = raw.items()
    elif isinstance(raw, list):
        items = [(x.get("sqlId") or x.get("sql_id"), x.get("plan"))
                 for x in raw if isinstance(x, dict)]
    else:
        return out
    for sid, plan in items:
        if not sid or not isinstance(plan, str) or not plan.strip():
            continue
        out[str(sid)] = plan
    return out


def parse_severity(s: str) -> Severity:
    """Map an interp.json severity string to a Severity band. Unknown/empty → OK
    (the render layer treats evidence as authoritative, so a bad interp severity
    never inflates the report)."""
    key = (s or "").strip().upper()
    return {"CRITICAL": Severity.CRITICAL, "WARN": Severity.WARN,
            "NOTICE": Severity.NOTICE}.get(key, Severity.OK)


def load_interp(path: str) -> Interp:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError as exc:
        raise ValueError(f"读取 interp 文件失败：{exc}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"解析 interp JSON 失败：{exc}")
    return Interp.from_dict(data)


def load_evidence(path: str) -> Evidence:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError as exc:
        raise ValueError(f"读取 evidence 文件失败：{exc}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"解析 evidence JSON 失败：{exc}")
    return Evidence.from_dict(data)

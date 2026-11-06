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
class KbRef:
    """引用的一条知识:编号 + 那句话(+ 可选标签,如「未验证」)。由脚本上色、加出处括号(common/kb/citestyle.py)。"""
    id: str = ""
    text: str = ""
    note: str = ""

    @staticmethod
    def from_dict(d) -> "Optional[KbRef]":
        if not isinstance(d, dict):
            return None
        ref = KbRef(id=str(d.get("id") or "").strip(), text=str(d.get("text") or "").strip(),
                    note=str(d.get("note") or "").strip())
        return ref if ref.id and ref.text else None


# 2026-10-26 回答模板化(R7):每条发现分「应用视角」与「数据库机制」两小段、带处置优先级与知识引用;
# 报告开头的短结论、负载画像的两小段、结尾的总结与展开提议都由 interp 给。全是可选字段 ——
# 旧格式的 interp.json 照样能渲染(缺的段落不出现或写「本次未给出」),旧会话里的报告重渲染不会失败。
PRIORITIES = ("P0", "P1", "P2")


def _priority(raw) -> str:
    s = str(raw or "").strip().upper()
    return s if s in PRIORITIES else ""


def _text(raw) -> str:
    return raw.strip() if isinstance(raw, str) else ""


@dataclass(frozen=True)
class InterpFinding:
    code: str = ""
    root_cause: str = ""
    sql_id: str = ""
    suggestions: list = field(default_factory=list)
    app_view: str = ""
    mechanism: str = ""
    priority: str = ""          # P0 | P1 | P2;空 = 按严重度排
    kb_refs: list = field(default_factory=list)

    @staticmethod
    def from_dict(d: dict) -> "InterpFinding":
        raw_refs = d.get("kbRefs")
        refs = [KbRef.from_dict(x) for x in raw_refs] if isinstance(raw_refs, list) else []
        return InterpFinding(
            code=d.get("code", ""), root_cause=d.get("rootCause", ""),
            sql_id=d.get("sqlId", ""),
            suggestions=[Suggestion.from_dict(s) for s in d.get("suggestions", []) or []],
            app_view=_text(d.get("appView")), mechanism=_text(d.get("mechanism")),
            priority=_priority(d.get("priority")), kb_refs=[r for r in refs if r is not None])


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
    summary: str = ""            # 开头的短结论(2–3 句)
    load_app_view: str = ""      # ① 负载画像的「应用视角」
    load_mechanism: str = ""     # ① 负载画像的「数据库机制」
    conclusion: str = ""         # ④ 总结和结论
    followups: list = field(default_factory=list)   # ④ 末尾的展开提议(1–3 条)

    @staticmethod
    def from_dict(d: dict) -> "Interp":
        ov = d.get("overall", {}) or {}
        lv = d.get("loadView") if isinstance(d.get("loadView"), dict) else {}
        ups = d.get("followups") if isinstance(d.get("followups"), list) else []
        return Interp(
            overall=InterpOverall(severity=ov.get("severity", ""), driver=ov.get("driver", "")),
            verification_badge=d.get("verificationBadge", ""),
            findings=[InterpFinding.from_dict(f) for f in d.get("findings", []) or []],
            plans=_plans_from_dict(d.get("plans")),
            summary=_text(d.get("summary")), load_app_view=_text(lv.get("appView")),
            load_mechanism=_text(lv.get("mechanism")), conclusion=_text(d.get("conclusion")),
            followups=[_text(x) for x in ups if _text(x)][:3])


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

"""检查点的统一表示:评分标准里每一条都变成一个 Item,规则层与裁判层往里填结果。"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Dict, List, Mapping, Optional

CATS = ("format", "tools", "points", "handling", "cite", "forbid")
PREFIX = {"format": "X", "points": "P", "handling": "H", "cite": "C", "forbid": "F"}
CAUSES = ("工具", "提示词", "模型", "环境")


@dataclass(frozen=True)
class Item:
    key: str                       # P1、H2、C1、F1、X1(版式的 extra)、R:段落(规则检查)、T:工具
    cat: str
    text: str
    w: float                       # 这一条的分值(版式的各项按份数均分后的分值)
    veto: bool = False
    truth: str = ""
    by: str = "judge"
    decided: bool = False          # 已有结论(规则层定了,或裁判给了)
    score: Optional[float] = None  # 得分;禁止项是「没犯 = 满分」
    hit: Optional[bool] = None     # 禁止项是否命中
    evidence: str = ""
    cause: str = ""                # 丢分归因
    source: str = ""               # rule | judge | manual

    def decide(self, score: Optional[float] = None, hit: Optional[bool] = None, evidence: str = "",
               cause: str = "", source: str = "rule") -> "Item":
        if self.cat == "forbid":
            hit = bool(hit)
            score = 0.0 if hit else self.w
        return replace(self, decided=True, score=score, hit=hit, evidence=evidence[:600], cause=cause, source=source)

    @property
    def lost(self) -> float:
        return 0.0 if not self.decided or self.score is None else max(0.0, self.w - self.score)

    def as_dict(self) -> Dict[str, Any]:
        return {k: getattr(self, k) for k in ("key", "cat", "text", "w", "veto", "truth", "by", "decided", "score",
                                              "hit", "evidence", "cause", "source")}


def from_rubric(rubric: Mapping[str, Any]) -> List[Item]:
    """评分标准 → 待判的检查点(版式与工具在规则层另算,这里只放 extra 与四个列表)。"""
    out: List[Item] = []
    for name in ("points", "handling", "cite", "forbid"):
        for i, it in enumerate(rubric.get(name) or [], 1):
            out.append(Item("%s%d" % (PREFIX[name], i), name, str(it.get("text") or ""), float(it.get("w") or 0),
                            bool(it.get("veto")), str(it.get("truth") or ""), str(it.get("by") or "judge")))
    return out


def load(rows: List[Mapping[str, Any]]) -> List[Item]:
    return [Item(**{k: r[k] for k in Item.__dataclass_fields__ if k in r}) for r in rows]

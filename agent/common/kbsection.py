"""诊断 skill 脚本接入「客户知识库参照」小节的公共入口:按每条发现检索客户知识库。

脚本层接入而不是靠提示词——模型是在已经看到客户先例的前提下作答的,「有没有看到」可以断言。
health / lockwait / sqltune 最早各自内联了同样几行(各自的 kb_section);之后接入的 skill 统一走这里。

纪律与那三个一致:
  · 永不抛、永不影响主报告:知识库不存在 / 没配 / 连不上,小节只剩「> 知识库未接入(原因)」,
    不许静默省略——省略和「没查」分不清;没装 common/kb(旧安装)也一样降级;
  · 只放进 markdown 输出。waitevent / vacuum / lockwait 被 health 以 `--format json` 当子进程拉起,
    health 会对汇总后的全部发现统一检索一次——子进程的 JSON 里再查,就是同一份发现查两遍。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Sequence, Tuple

SECTION_TITLE = "## 客户知识库参照"      # 与 common/kb/render.py 同名小节;那边装不上时这里自己写


@dataclass(frozen=True)
class Item:
    """给没有 common.finding.Finding 的 skill(proctune / sqlreview)组装检索项。

    字段名与 Finding 一致,query.py 按属性名取。与 common.kb.query.SimpleFinding 同形 —— 放在这里,
    是为了 common/kb 装不上时「组装检索项」这一步本身也不炸(那时整节照样降级成「未接入」)。
    """
    code: str
    dimension: str
    metric: str
    value: str
    evidence: str
    severity: str = ""


def _distinct(findings: Sequence[Any]) -> List[Any]:
    """完全相同的发现(code / 指标 / 值 / 证据都一样)只查一次。

    vacuum 的 R4(回收水位被卡)在每张命中的表上各报一条一模一样的;逐条查出来是同一段参照重复 N 遍。
    """
    seen = set()
    out = []
    for f in findings:
        key = tuple(str(getattr(f, k, "") or "") for k in ("code", "metric", "value", "evidence"))
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


def section(findings: Sequence[Any]) -> Tuple[str, Dict[str, Any]]:
    """(markdown 小节, JSON 字典)。findings 只要有 code / metric / value / evidence / severity 这几个属性。"""
    try:
        from common.kb import query as kbquery
    except ImportError as exc:                       # 旧安装没有 common/kb,或缺它的依赖
        reason = f"common/kb 未安装:{exc}"
        return (f"{SECTION_TITLE}\n> 知识库未接入({reason})\n\n",
                {"status": {"attached": False, "reason": reason}, "items": []})
    return kbquery.section_for(_distinct(findings))


def markdown(findings: Sequence[Any]) -> str:
    """只要 markdown 小节(JSON 输出不带知识库的脚本用,理由见模块说明)。"""
    return section(findings)[0]

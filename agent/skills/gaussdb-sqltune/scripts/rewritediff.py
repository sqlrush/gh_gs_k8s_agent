"""改写前后的对比:SQL 文本差异与两棵执行计划(2026-10-26 回答模板化 R4 第 ③ 段)。

verify.py 原来只给 cost 前后两个数,读的人看不到「改了哪几行」「计划从什么变成什么」。
这里出两节,SKILL.md 第 8 步要求照抄进「怎么优化」那一段:

    ### SQL 文本差异                 按行的统一差异(- 原来 / + 改写后),空白差异不算
    ### 执行计划(改写前 / 改写后)     两棵 EXPLAIN 计划原样;组合验证时「改写后」含假设索引

计划取不到(权限、语句报错)只说明取不到,不影响 cost 判定本身。
"""
from __future__ import annotations

import difflib

PLAN_MAX = 2500


def _lines(sql: str) -> list:
    return [" ".join(line.split()) for line in (sql or "").strip().rstrip(";").splitlines() if line.strip()]


def sql_diff(orig_sql: str, new_sql: str) -> str:
    """按行的统一差异,只留 -/+ 与上下文行;两边只差空白时返回空串。"""
    a, b = _lines(orig_sql), _lines(new_sql)
    if a == b:
        return ""
    out = [line for line in difflib.unified_diff(a, b, lineterm="", n=1) if not line.startswith(("---", "+++", "@@"))]
    return "\n".join(out)


def explain_text(db, sql_text: str) -> str:
    """EXPLAIN 的文本计划(不执行语句)。取不到返回空串。"""
    import common
    try:
        _, rows = db.query(f"EXPLAIN (FORMAT TEXT, COSTS TRUE) {sql_text}")
    except common.DBError:
        return ""
    return "\n".join(str(r[0]) for r in rows or [])


def _block(text: str) -> str:
    body = text.rstrip()
    if len(body) > PLAN_MAX:
        body = body[:PLAN_MAX] + "\n…(计划更长,重跑 explain 看全)"
    return "```\n%s\n```\n" % body


def comparison_md(diff: str, orig_plan: str, new_plan: str, after_label: str = "改写后") -> str:
    out = "### SQL 文本差异\n\n"
    out += ("```diff\n%s\n```\n\n" % diff) if diff else "两边只差空白或完全相同。\n\n"
    out += "### 执行计划(改写前 / %s)\n\n" % after_label
    if not orig_plan and not new_plan:
        return out + "本次没取到执行计划(只比较了 cost)。\n"
    out += "改写前:\n\n" + (_block(orig_plan) if orig_plan else "没取到。\n") + "\n"
    out += "%s:\n\n" % after_label + (_block(new_plan) if new_plan else "没取到。\n")
    return out

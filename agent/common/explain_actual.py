"""EXPLAIN ANALYZE 到底跑没跑,看计划文本,不看请求标志。

现场三条 *_analyze 注册脚本(explain / sqltune / proctune)按客户只读要求把 ANALYZE 固定成 false
(wengzhongyu 17dde8c,2026-08-14):用户 SQL 不会被执行,要了 --analyze 拿到的仍是估算计划。
直连原始会话那条路(sqltune / proctune 有 db 时)仍是真 ANALYZE。两条路口径必须一致,
所以统一按结果判:EXPLAIN ANALYZE 的每个节点都带 `(actual time=… rows=… loops=…)`,没有就是估算。
凭「我请求了 analyze」就写 Analyzed: true,是把估算当实测——静默失效,比报错危险。
"""
from __future__ import annotations

ACTUAL_MARK = "actual time="

FIELD_ANALYZE_OFF_NOTE = (
    "本次要了 ANALYZE,拿到的仍是估算计划:现场注册脚本按客户只读要求把 ANALYZE 固定关闭,"
    "用户 SQL 没有被执行,计划里没有 actual 行数与耗时——引用时不要说成实测,行数与耗时都是规划器的估算"
)


def has_actual(plan: str) -> bool:
    """计划文本里有没有 actual 行(EXPLAIN ANALYZE 真跑过的唯一可靠证据)。"""
    return ACTUAL_MARK in (plan or "")


def analyzed_for_real(plan: str, requested: bool) -> bool:
    """要了 analyze **并且** 计划里有 actual 行,才算真 analyze 过。"""
    return bool(requested) and has_actual(plan)


def analyze_note(plan: str, requested: bool) -> str:
    """要了 analyze 却拿到估算计划时的说明;其余情况空串。"""
    if not requested or has_actual(plan):
        return ""
    return FIELD_ANALYZE_OFF_NOTE

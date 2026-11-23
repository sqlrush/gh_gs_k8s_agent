"""目标库与取数库不同时,按 SQL 历史里的真实执行计划做调优分析。

2026-11-06 行方「部分 SQL 无法调优」:SQL 的表在业务库,中间件按实例 IP 固定连 postgres,现取执行计划找不到对象。
SQL 历史(statement_history)只在系统库里有数据、覆盖实例上全部库(og5 实测),取数库是 postgres 时正好取得到:
用当时真实执行的计划,加上计划规则(evidence.scan_plan、planrules.root_causes)出一份分析。
没有现场取计划、没有表与索引统计、索引建议没经过假设索引验证 —— 报告开头写明,不冒充完整调优。
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Optional

import evidence
import planrules
from sqlfetch import sql_fetch

from common import access

PLAN_SCRIPT = "explain.plan_from_history"     # 已在白名单(20261022 增量),explain 按 id 看历史计划用的同一条
_MARK = {"warn": "🟠", "info": "ℹ️"}


def recorded_plan(runner, sql_id: str) -> Optional[dict]:
    """SQL 历史里最近一次真实执行的计划;没有记下(或脚本没登记)时 None。"""
    try:
        rows = runner.run(PLAN_SCRIPT, {"sid": int(sql_id)})
    except (access.QueryError, ValueError):
        return None
    if not rows or not str(rows[0].get("query_plan") or "").strip():
        return None
    rec = dict(rows[0])
    plan = str(rec["query_plan"]).strip("\n")
    if plan.startswith("Datanode Name:"):           # 第一行是节点名,不是计划的一部分
        plan = plan.split("\n", 1)[1] if "\n" in plan else ""
    rec["query_plan"] = plan.rstrip()
    return rec


def findings(sql: str, plan: str, schema: str) -> list:
    """计划规则:根因规则(NOT IN、相关子查询、索引列套函数、LIKE)+ 通用的计划扫描。没有目录证据,索引类规则自然不命中。"""
    ev = SimpleNamespace(plan=plan, sql=sql, indexes=(), columns=(), schema=schema)
    return list(planrules.root_causes(ev, original_sql=sql)) + list(evidence.scan_plan(plan))


def render(sql_id: str, sql: str, rec: dict, conn, found: list) -> str:
    target, src = conn.database, conn.exec_database
    out = ["# SQL 调优(按执行历史)— sql_id %s" % sql_id, "",
           "> **要诊断的库「%s」与取数的库「%s」不同**:中间件按实例 IP 固定连「%s」,现取执行计划找不到「%s」里的表。"
           "本报告依据 SQL 历史里记下的**当时真实执行的计划**分析;没有现场取计划、没有读表与索引统计,"
           "索引建议没有经过假设索引验证 —— 给方向,落地前由 DBA 在「%s」上核对。" % (target, src, src, target, target),
           "", "## SQL", "", "```sql", sql.strip(), "```", "",
           "## 当时真实执行的计划", "",
           "%s 那次执行,耗时 %s ms,返回 %s 行。" % (rec.get("start_time") or "?", rec.get("db_time_ms") or "?",
                                                rec.get("n_returned_rows") or "?"),
           "", "```", rec["query_plan"], "```", "", "## 计划里看到的问题", ""]
    if found:
        for f in found:
            out.append("- %s **%s**:%s" % (_MARK.get(f.severity, "•"), f.kind, f.detail))
            if f.advice:
                out.append("  - 建议:%s" % f.advice)
    else:
        out.append("计划规则没有命中。结合上面的计划看:哪一步实际行数最多、耗时最长,那一步的访问方式(全表扫描、"
                   "嵌套循环里的重复扫描、排序或哈希落盘)就是要改的地方。")
    return "\n".join(out) + "\n"


def report(runner, sql_id: str, conn, fmt: str = "markdown") -> str:
    """能出就返回报告正文;SQL 文本或历史计划取不到时返回空串,由调用方照旧报原因。"""
    try:
        fr = sql_fetch(runner, sql_id)
    except (access.QueryError, ValueError):
        return ""
    rec = recorded_plan(runner, sql_id)
    if rec is None or not (fr.sql or "").strip():
        return ""
    found = findings(fr.sql, rec["query_plan"], fr.schema or "")
    if fmt == "json":
        return json.dumps({"mode": "history", "sql_id": sql_id, "sql": fr.sql, "plan": rec["query_plan"],
                           "target_database": conn.database, "exec_database": conn.exec_database,
                           "executed_at": rec.get("start_time"), "db_time_ms": rec.get("db_time_ms"),
                           "findings": [f.__dict__ for f in found]}, ensure_ascii=False, indent=2) + "\n"
    return render(sql_id, fr.sql, rec, conn, found)

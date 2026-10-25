"""sql_id 在统计视图里查不到时的说明:为什么查不到、文本去哪找。explain 与 sqltune 共用一份。

2026-10-19 测试:用户在大盘的时段报告上点「看执行计划」,那条语句来自 10-17 上午,而实例 10-17 21:13
重启过 —— dbe_perf.statement 早就没有它了。原来的报错只列「按可能性排序」的三种原因,再让用户自己
去 WDR 附录里找文本。可这两件事工具自己都知道:
  · 实例什么时候启动的(health.live_counters 给 postmaster_start)—— 原因能直接说出来;
  · 文本在不在我们自己的 WDR 存档里(sql_texts)—— 在就直接给出来,还能接着出计划。
取不到(没导那条脚本、不在容器里、存档没有)都退回原来的三条原因,不报错、不猜。
"""
from __future__ import annotations

import json
import re
from typing import Optional, Tuple

from common import reports, windata

TEXT_SHOWN = 2000
# 只剥开头的注释(与 topsql.top_sql / wdr 的显示同一规则):开头的注释可能写着别人的推断,
# 照抄进回答就成了「诊断结论」;中间的注释可能是 hint,不动。
_LEAD_COMMENTS = re.compile(r"^(\s*(--[^\n]*(\n|$)|/\*([^*]|\*+[^*/])*\*+/))+\s*")


def archived_text(sql_id: str) -> Optional[Tuple[str, str, str]]:
    """(全文, 时段起, 时段止):最近一份含这条语句的 WDR 存档。没有存档目录 / 没找到 → None。"""
    base = reports.reports_dir()
    if base is None:
        return None
    files = sorted((base / "wdr").glob("*/*.json"), key=lambda p: p.name, reverse=True)
    for path in files:
        if path.name in (reports.LATEST, reports.INDEX):
            continue
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        texts = d.get("sql_texts") if isinstance(d, dict) else None
        text = texts.get(str(sql_id)) if isinstance(texts, dict) else None
        if text:
            w = d.get("window") or {}
            return str(text), str(w.get("begin_ts") or ""), str(w.get("end_ts") or "")
    return None


def _strip_lead(text: str) -> str:
    out = _LEAD_COMMENTS.sub("", text, count=1).strip()
    return out or text


def message(runner, raw_id: str, degraded: str = "") -> str:
    """查不到时的完整说明(多行)。不抛异常:取不到的部分就不写。"""
    ck = None
    try:
        ck = windata.clock(runner, with_stats_reset=False)
    except Exception:          # noqa: BLE001 —— 说明文字的附加信息,取不到不影响主报错
        ck = None
    pm = ck.postmaster_start if ck else None
    found = archived_text(raw_id)
    head = "sql id %s 在 dbe_perf.statement_history 与 dbe_perf.statement 里都查不到。" % raw_id
    lines = [head]
    seen_before_restart = False
    if found and pm:
        end = windata.parse_ts(found[2] + (":00" if len(found[2]) == 16 else ""))
        seen_before_restart = end is not None and end < pm
    if pm and seen_before_restart:
        lines.append("**原因已确定**：这条语句出现在 %s – %s，而实例在 %s 重启过 —— dbe_perf.statement "
                     "是自实例启动以来的累计视图，重启之前的语句都已不在里面。这是正常的，不是统计没开。"
                     % (found[1], found[2], pm.strftime("%Y-%m-%d %H:%M:%S")))
    else:
        if pm:
            lines.append("实例在 %s 启动（统计从那时起算），dbe_perf.statement 只有这之后执行过的语句。"
                         % pm.strftime("%Y-%m-%d %H:%M:%S"))
        lines.append("常见原因按可能性排序:\n"
                     "  1. **实例重启过** —— 重启之前的 sql_id 一律查不到;\n"
                     "  2. 这条语句已从 statement_history 里**老化**出去(历史是滚动的);\n"
                     "  3. 统计没开:enable_stmt_track / track_stmt_parameter。")
    if found:
        text = _strip_lead(found[0])
        n = text.count("?")
        clip = text if len(text) <= TEXT_SHOWN else text[:TEXT_SHOWN] + " …(截断)"
        lines.append("这条语句的文本在 WDR 存档里找到了(时段 %s – %s，开头的注释已去掉):\n```sql\n%s\n```"
                     % (found[1], found[2], clip))
        lines.append(("它是参数化模板(%d 个 ?)。取计划:把它用 --sql-stdin 交给 gaussdb-sqltune"
                      "(会按列类型合成占位符值出计划);或填上真实值后用 gaussdb-explain --sql-stdin。" % n) if n else
                     "它不带占位符，可以直接用 gaussdb-explain --sql-stdin 取计划。")
    else:
        lines.append("已经有 SQL 文本时(WDR 报告的「SQL 附录」、大盘点 sql_id 的抽屉里都有),"
                     "改用 --sql-stdin 把文本传进来即可;参数化语句里的 ? 要先填上真实值才能出计划。")
    if degraded:
        lines.append("另:statement_history 本身不可用:%s" % degraded)
    return "\n".join(lines)

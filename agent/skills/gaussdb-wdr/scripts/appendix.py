"""WDR 报告的附录与校验区:本报告没覆盖的、SQL 附录(全文 + 执行计划)、模型遗漏 / 未锚定 / 未验证建议。

2026-10-26 回答模板化(R7)从 finalreport.py 拆出来:报告正文改成「负载画像 → 问题与元凶 → 解决方案 → 总结」,
这些放到最后的「附录」里。内容与写法没变,只是挪了位置(标题由 finalreport 降一级挂到「## 附录」下)。
"""
from __future__ import annotations

import render


# 发现码 → 下一步该跑什么。**由脚本生成,不靠模型自己想。**
#
# 为什么要钉成确定性输出:SKILL.md 里早就写了「带 sql_id 的先 sqltune 实证」,
# 可 2026-10-17 实测那一轮,模型给的 8 条建议一条都没去实证,报告自己也写着
# 「0 条建议经 hypopg/cost 实证」。规则在提示词里,就只是「希望它做」;
# 落成报告里的一个段落,用户才看得见该跑什么、也看得出有没有跑。
# WDR 这份数据根本看不到的东西。**每次都要写出来**:不写等于让用户以为已经查全了。
# 这里只说「没看什么」,不说「你去跑哪条命令」—— 要跑的由 followup.py 交代给模型。
_NOT_COVERED = [
    "**膨胀与回收水位**：死元组、autovacuum 追不追得上、回收被谁卡住（长事务 / 两阶段事务 /"
    "复制槽），WDR 的快照里都没有这些数 —— 实测一张有 540 万死元组的表，快照里记的是 0。",
    "**一直没跑完的 SQL**：WDR 只统计**已完成**的执行，正卡着没返回的语句在这里一行都没有。",
    "**窗口之前的历史**：快照只记当下的累计值，窗口开始前的膨胀程度、长事务已经开了多久，"
    "事后都重建不出来。",
]


def _emit_next_steps(m: _ReportModel) -> str:
    """本报告没覆盖到的范围 —— **用人话说,不给命令**。

    **第一版在这里列的是 `python3 ../gaussdb-explain/scripts/explain.py --sql-id 123`
    这样的命令行,那是错的。** 报告是逐字呈现给用户的最终输出:
      · 用户要的是执行计划本身,不是取执行计划的命令 —— 给命令等于把活推回给他,
        而他在聊天窗口里也跑不了;
      · 报告既然是最终输出,模型渲染完就收工,那几条命令一条都不会被执行。
    具体要跑什么,由 `followup.py` 在 collect 阶段从**标准错误**交代给模型,
    模型跑完把结论写进 interp.json,在这份报告里体现为根因与建议 —— 而不是一行待办。

    这一段只保留一件事:**说清楚这份报告没看哪些东西**。不说等于让用户以为已经查全了。
    """
    out = "## 本报告没覆盖的\n\n"
    for why in _NOT_COVERED:
        out += "- %s\n" % why
    return out + "\n"


# 附录里每条 SQL 最多展示这么长。再长的留标记,让人去大盘或 sqlfetch 取 ——
# 报告是给人读的,一条几 KB 的 SQL 糊在正文里谁也读不下去。
APPENDIX_SQL_MAX = 1200


def _emit_sql_appendix(m: _ReportModel) -> str:
    """SQL 附录:根因里提到的 sql_id → 完整 SQL 文本。

    **为什么要有附录(照 AWR 的做法):** 根因那几行里只写 sql_id 和一小段片段,
    读的人认不出是哪条语句;可要是把全文塞进根因,一条几百字的 SQL 会把结论冲掉。
    所以正文给 sql_id + 片段,全文集中在附录,再给出大盘与执行计划的去处。

    只列**根因里真正提到过**的 sql_id —— 把 Top SQL 全表的文本都搬下来,
    附录会比报告本身还长。
    """
    texts = m.sql_texts or {}
    if not texts:
        return ""
    wanted, seen = [], set()
    for f in m.findings:
        sid = getattr(f, "sql_id", "") or ""
        if sid and sid in texts and sid not in seen:
            seen.add(sid)
            wanted.append(sid)
    if not wanted:
        return ""
    out = "## SQL 附录（根因里提到的语句）\n\n"
    for sid in wanted:
        body = texts[sid]
        if len(body) > APPENDIX_SQL_MAX:
            body = body[:APPENDIX_SQL_MAX] + "\n…（全文更长，到大盘或用 gaussdb-sqlfetch 取）"
        out += "### sql_id `%s`\n\n" % sid
        out += render.code_block("sql", body)
        out += "\n"
        out += _emit_plan(m, sid)
    return out


# 附录里每条执行计划最多展示这么长。超长的计划(几十层嵌套)贴全了没人读得完,
# 关键永远在开头那几层 —— 真要逐层看,到大盘或重跑 explain。
APPENDIX_PLAN_MAX = 2500


def _emit_plan(m: _ReportModel, sid: str) -> str:
    """这条语句的执行计划。

    **计划是模型取回来放进 interp.json 的,不是报告里给一条命令让用户自己去跑。**
    第一版这里写的是「执行计划：`python3 …/explain.py --sql-id 123`（计划要实时连库，
    报告里不预先生成）」—— 那句话把本该做的事当成了做不到的事:计划确实要连库,
    但连库的是模型,它在写 interp.json 之前就该把计划取回来。
    (要跑哪些命令由 followup.py 在 collect 阶段从标准错误交代。)

    取不到也要如实留一行,说明没取到 —— 空着会让人以为这条语句没有计划可看。
    """
    hist = (m.hist_plans or {}).get(sid) or {}
    plan = (m.plans or {}).get(sid) or ""
    if str(hist.get("plan") or "").strip():
        # 2026-10-21 起采集时就取窗口内的真实执行计划;它讲的正是这段时间,优先于模型事后取的
        head = ("- 执行计划（SQL 历史里记下的真实执行计划：%s 那次执行，耗时 %s ms，返回 %s 行）："
                % (hist.get("start_time") or "?", hist.get("db_time_ms") or "?", hist.get("rows") or "?"))
        plan = hist["plan"]
    elif str(plan).strip():
        head = "- 执行计划："
    else:
        why = hist.get("why") or "计划要实时连库；若语句已不在库里或权限不足，会取不到"
        return "- 执行计划：本次未取到（%s）。\n\n" % why
    body = str(plan).rstrip()
    if len(body) > APPENDIX_PLAN_MAX:
        body = body[:APPENDIX_PLAN_MAX] + "\n…（计划更长，到大盘或重跑 explain 看全）"
    return head + "\n\n" + render.code_block("text", body) + "\n"


def _emit_quarantine_md(m: _ReportModel) -> str:
    out = ""
    if m.missing:
        out += "## ⚠ 模型遗漏\n\n"
        for s in m.missing:
            out += f"- {s}：确定性发现未被解读\n"
        out += "\n"
    if m.unanchored:
        out += "## ⚠ 未锚定（已剔除，不作正式发现）\n\n"
        for c in m.unanchored:
            out += f"- {c}：interp 引用的 Code 不在确定性发现中\n"
        out += "\n"
    if m.unverified:
        out += "## ⚠ 未验证建议（已剔除）\n\n"
        for s in m.unverified:
            out += f"- {s}\n"
        out += "\n"
    return out

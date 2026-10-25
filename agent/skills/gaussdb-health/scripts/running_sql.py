"""执行中、还没结束的长 SQL —— 慢 SQL 维度的另一半。

**为什么要有这一项(faultlab S1,2026-10-24):** 一条相关子查询在 og5 上跑了 1123 秒、
一次没跑完,health 一句没提。慢 SQL 维度读的 dbe_perf.statement **只记已完成的执行**,
还在跑的语句在那里一行都没有 —— 而「现在有条 SQL 跑了二十分钟还没完」恰恰是用户问
「库为什么卡」时最想知道的。

**取数复用 health.long_xact,不加白名单脚本。** 那条本来就列出所有 active / 空闲事务中的
客户端会话;active 行的 state_age_s(now − state_change)就是这条语句已经执行了多久 ——
openGauss 在语句开始时同时刷新 query_start 与 state_change(og5 上逐秒核对过,两者相同)。

**等锁的语句不算进来,只点数。** 它们的耗时花在等锁上(pg_stat_activity.waiting),
该看锁阻塞链(gaussdb-lockwait 会报 LOCK_WAIT_*),把它们报成「执行中的慢 SQL」会把人导去调 SQL。
faultlab 那轮 K1 链上有 15 个这样的会话。

**取数上限要说清。** long_xact 按事务开始时间取最老的 RUNNING_FETCH 个会话;语句时长 ≤ 事务时长,
所以取回的最后一行事务时长还没过阈值时,截掉的那些不可能过阈值;过了,就可能漏,要明说。
"""
from __future__ import annotations

import dataclasses
from typing import Sequence, Tuple

from model import DIM_SLOWSQL, DimResult, Finding, Severity
from thresholds import Thresholds, go_duration
from util import sev_by_duration, summarize_err, trunc

from common import access, syssql
from common.grmp.values import as_bool, is_null

SCRIPT = "health.long_xact"
CODE = "SLOWSQL_RUNNING"
# 一次取多少个会话。按事务开始时间取最老的 —— 见模块说明的「取数上限」。
RUNNING_FETCH = 200
QUERY_CHARS = 120


@dataclasses.dataclass(frozen=True)
class RunningCheck:
    findings: Tuple[Finding, ...] = ()
    brief: str = ""             # 给慢 SQL 维度标题追加的一句


def _secs(row, key: str) -> float:
    """NULL(协议下是空串)当 0 秒;认不出的值照常抛错 —— 不把取数出错伪装成「没跑多久」。"""
    value = row.get(key)
    return 0.0 if is_null(value) else float(value)


def _threshold(sev: Severity, th: Thresholds) -> str:
    if sev == Severity.CRITICAL:
        return ">" + go_duration(th.running_sql_crit)
    if sev == Severity.WARN:
        return ">" + go_duration(th.running_sql_warn)
    return ">" + go_duration(th.running_sql_notice)


def _finding(row, secs: float, sev: Severity, th: Thresholds) -> Finding:
    pid = row.get("pid")
    return Finding(
        DIM_SLOWSQL, CODE, sev, f"pid {pid} 执行中的 SQL", f"{secs:.0f}s", _threshold(sev, th),
        f"pg_stat_activity pid={pid}（{row.get('usename') or '?'}）state=active，已执行 {secs:.0f} 秒仍未结束；"
        f"dbe_perf.statement 只记已完成的执行，上面的慢 SQL 表里没有它。SQL：{trunc(row.get('query'), QUERY_CHARS)}。"
        f"看它此刻的计划用 /gaussdb-explain --pid {pid}，深调用 /gaussdb-sqltune（贴 SQL 原文）。"
        f"同一会话也可能出现在长事务维度：那边说的是它持着快照多久，这里说的是这条语句还没跑完")


def _may_have_missed(rows: Sequence, th: Thresholds, fetched_cap: int) -> bool:
    if len(rows) < fetched_cap or not rows:
        return False
    return _secs(rows[-1], "xact_age_s") >= th.running_sql_notice


def judge(rows: Sequence, th: Thresholds, top: int, fetched_cap: int) -> RunningCheck:
    """health.long_xact 的行 → 执行中长 SQL 的发现与一句标题。纯函数。"""
    floor = th.running_sql_notice
    long_rows = [r for r in rows if r.get("state") == "active" and _secs(r, "state_age_s") >= floor]
    # 老白名单(客户还没重导)没有 waiting 列:当作没在等锁,照常判
    waiting = [r for r in long_rows if as_bool(r.get("waiting", ""))]
    executing = [r for r in long_rows if not as_bool(r.get("waiting", ""))]
    screening = syssql.screen(executing, sql_id=lambda r: "pid %s" % r.get("pid"),
                              text=lambda r: r.get("query"))
    running = sorted(screening.kept, key=lambda r: _secs(r, "state_age_s"), reverse=True)
    findings = tuple(_finding(r, _secs(r, "state_age_s"),
                              sev_by_duration(_secs(r, "state_age_s"), floor, th.running_sql_warn,
                                              th.running_sql_crit), th)
                     for r in running[:top])
    parts = [_running_brief(running, top, floor)]
    if waiting:
        parts.append(f"另有 {len(waiting)} 条已执行超过 {go_duration(floor)}、在等锁（耗时花在锁上，见锁阻塞）")
    if _may_have_missed(rows, th, fetched_cap):
        parts.append(f"只取了事务最老的 {fetched_cap} 个会话，可能还有没列出的")
    if screening.brief():
        parts.append(screening.brief())
    return RunningCheck(findings=findings, brief="；".join(parts))


def _running_brief(running: Sequence, top: int, floor: int) -> str:
    if not running:
        return f"执行中超过 {go_duration(floor)} 的 SQL：无"
    head = running[0]
    shown = f"，列出最长的 {top} 条" if len(running) > top else ""
    return (f"执行中超过 {go_duration(floor)} 的 SQL {len(running)} 条"
            f"（最长 pid {head.get('pid')} 已执行 {_secs(head, 'state_age_s'):.0f}s{shown}）")


def check(runner, th: Thresholds, top: int) -> RunningCheck:
    try:
        rows = runner.run(SCRIPT, {"limit": RUNNING_FETCH})
    except access.QueryError as exc:
        return RunningCheck(brief="执行中的 SQL 取不到（%s）" % summarize_err(exc))
    return judge(rows or [], th, top, RUNNING_FETCH)


def attach(d: DimResult, chk: RunningCheck) -> DimResult:
    """并进慢 SQL 维度,返回新的 DimResult。维度不可用(dbe_perf.statement 取不到)时说明写进 note ——
    渲染层对不可用的维度只打 note。"""
    findings = [*d.findings, *chk.findings]
    if not chk.brief:
        return dataclasses.replace(d, findings=findings)
    if d.available:
        return dataclasses.replace(d, findings=findings, headline="；".join(p for p in (d.headline, chk.brief) if p))
    return dataclasses.replace(d, findings=findings, note="；".join(p for p in (d.note, chk.brief) if p))

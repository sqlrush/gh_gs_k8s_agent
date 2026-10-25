"""「等待」与「自身耗时」:把被锁挡住的语句和自己慢的语句分开。

**为什么要分。** 2026-10-24 faultlab 在 og5 上全量混跑:总耗时榜首是 K1 链上等锁的 UPDATE fl.acct
(6778.9 秒,CPU 0),平均耗时榜首是 K2 等锁的 FOR UPDATE(1081 秒,CPU 0)。真正「自己慢」的 S3 / S4
和高频的 S7 被挤到后面 —— 榜单答的是「谁等得最久」,用户问的是「谁最拖慢系统」。

**为什么用 CPU + 数据 IO,不用锁等待时间。** dbe_perf.statement 与 snapshot.snap_summary_statement 里
**没有**锁等待列(og5 逐列核对过),有的是 cpu_time 与 data_io_time。别的来源都缺一块:
  · statement_history.lock_wait_time 只记**跑完了**的执行 —— 那一轮被挡住、最后被取消的 FOR UPDATE
    (1081 秒)一条记录都没有;客户默认 track_stmt_stat_level 也只记慢 SQL;
  · gs_asp 的采样在「解析阶段等表锁」时不带 unique_query_id(og5 上 400 个样本是空的),
    被 ALTER 挡住的 DML 恰好就是这种。
所以只说能从同一组计数器里算准的:自身耗时 = CPU + 数据 IO;等待占比 = 1 − 自身耗时 / 总耗时。
等待里有等锁、等轻量锁、排队等 CPU、网络收发 —— 分不出是哪一种,报告里照实写明。
这与 health 的 SLOWSQL_LOW_CPU(CPU 只占耗时一小部分 → 非 CPU 消耗主导)是同一个判据。
"""
from __future__ import annotations

from typing import Iterable, Optional

# 等待占比到这个数,就说「这一条主要在等」—— 那一轮等锁的语句都是 95%~100%,
# 自己慢的 S3 / S7 在 og5 这台挤满的机器上也有 40% 左右是排队等 CPU,所以门槛不能低。
WAIT_DOMINANT = 0.8

SELF_KEYS = ("self", "self_avg")
SELF_DEF = "自身耗时 = CPU + 数据 IO 时间,不含等锁、等轻量锁、排队等 CPU 等等待"


def self_sec(cpu_sec: Optional[float], io_sec: Optional[float]) -> Optional[float]:
    """None = 取数脚本里没有这两列(白名单还没更新 / 升级前的基线),不是 0。"""
    if cpu_sec is None or io_sec is None:
        return None
    return max(0.0, float(cpu_sec) + float(io_sec))


def wait_ratio(total_sec: float, self_s: Optional[float]) -> Optional[float]:
    if self_s is None or not total_sec or total_sec <= 0:
        return None
    return min(1.0, max(0.0, 1.0 - self_s / float(total_sec)))


def share_cell(ratio: Optional[float]) -> str:
    return "—" if ratio is None else "%d%%" % int(round(ratio * 100))


def sec_cell(value: Optional[float]) -> str:
    return "—" if value is None else "%.2f" % value


def board_note(rows: Iterable, by: str) -> str:
    """表格下面那段说明。rows 的每项要有 total_sec 与 self_sec。"""
    rows = list(rows)
    ratios = [wait_ratio(r.total_sec, r.self_sec) for r in rows]
    if rows and all(x is None for x in ratios):
        return ("\n> 等待占比取不到:取数脚本里还没有 CPU / 数据 IO 列(topsql.top_sql / topsql.snap_statement "
                "白名单待更新,或这一窗口的起点是升级前留下的基线)。\n")
    lines = []
    if by in SELF_KEYS:
        lines.append("按自身耗时排:%s。" % SELF_DEF)
    heavy = ["#%d" % (i + 1) for i, x in enumerate(ratios) if x is not None and x >= WAIT_DOMINANT]
    if heavy:
        lines.append(
            "%s 的耗时有 %d%% 以上不是 CPU / 数据 IO,是在等(等锁、等轻量锁、排队等 CPU、睡眠都算在内)"
            "—— 它们是被挡住的,不是自己慢;要看是谁挡的用 gaussdb-lockwait。dbe_perf.statement 里没有"
            "单条语句的锁等待时间列,等待占比按「总耗时 − CPU − 数据 IO」算,分不出等的具体是哪一种。"
            % ("、".join(heavy), int(WAIT_DOMINANT * 100)))
        if by not in SELF_KEYS:
            lines.append("不含等待的排名用 `--by self`(自身耗时)/ `--by self_avg`(平均自身耗时)。")
    return "".join("\n> %s\n" % s for s in lines)

"""SQL 优化第 ① 段「现状与趋势」:取数、相减、判定都在这里,模型只转述(2026-10-26 回答模板化 R4)。

三条只读白名单脚本(走中间件要先登记;交付闸按脚本名全串检索,故写全):
  sqltune.stat_now      dbe_perf.statement:自统计清零以来的执行次数与平均 / 最大 / 最小耗时(实测)
  sqltune.stat_trend    WDR 快照里这条语句的累计值:相邻快照相减 → 按小时的次数与平均耗时
  sqltune.exec_history  statement_history 按小时:记下的执行里最慢多少、执行计划指纹(计划只在变化时给出)

**拐点与计划变化由脚本判定,不让模型拿数去推。** 某小时平均耗时 ≥ 此前 24 小时中位数的 JUMP_RATIO 倍、
且绝对值 ≥ JUMP_MIN_MS,算「突然变慢」(取最早的一次)。计划指纹只是粗筛(绑定值不同也会让指纹不同),
再按计划的节点结构核对一遍,结构真的变了才算「计划变了」。
任何一条脚本取不到(白名单未登记、视图没权限、保留期外)都只落到这一节的说明里,不影响调优主流程。
"""
from __future__ import annotations

import datetime as _dt
import re
import statistics
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

STAT_SCRIPT = "sqltune.stat_now"
TREND_SCRIPT = "sqltune.stat_trend"
HISTORY_SCRIPT = "sqltune.exec_history"
TREND_DAYS = 7
JUMP_RATIO = 3.0
JUMP_MIN_MS = 100.0
BASELINE_HOURS = 24
MIN_BASELINE_POINTS = 3
CORRELATE_HOURS = 2           # 突然变慢与计划变化相隔不超过这么多小时,就说两者吻合
PLAN_MAX = 2500
HOUR_FMT = "%Y-%m-%d %H:00"


@dataclass(frozen=True)
class Stats:
    calls: int
    avg_ms: float
    max_ms: float
    min_ms: float
    total_ms: float
    cpu_ms: float
    io_ms: float


@dataclass(frozen=True)
class HourPoint:
    hour: str            # YYYY-MM-DD HH:00(这一小时结束时刻所在的那一小时)
    calls: int
    total_ms: float

    @property
    def avg_ms(self) -> float:
        return self.total_ms / self.calls if self.calls else 0.0


@dataclass(frozen=True)
class Jump:
    hour: str
    avg_ms: float
    baseline_ms: float

    @property
    def ratio(self) -> float:
        return self.avg_ms / self.baseline_ms if self.baseline_ms else 0.0


@dataclass(frozen=True)
class HistHour:
    hour: str
    runs: int
    avg_ms: float
    max_ms: float
    plan_sig: str
    plan: str


@dataclass(frozen=True)
class PlanChange:
    hour: str
    before_hour: str
    before_plan: str
    after_plan: str


@dataclass(frozen=True)
class TrendReport:
    stats: Optional[Stats] = None
    stats_note: str = ""
    points: Tuple[HourPoint, ...] = ()
    resets: Tuple[str, ...] = ()
    jump: Optional[Jump] = None
    trend_note: str = ""
    history: Tuple[HistHour, ...] = ()
    changes: Tuple[PlanChange, ...] = ()
    plan_note: str = ""


# ---------------------------------------------------------------- 纯函数

def _num(row: dict, key: str) -> float:
    try:
        return float(str(row.get(key) or "").strip() or 0)
    except ValueError:
        return 0.0


def parse_stats(rows: Sequence[dict]) -> Optional[Stats]:
    """没有这条语句(统计清零后没再跑过)返回 None。"""
    if not rows or not int(_num(rows[0], "calls")):
        return None
    r = rows[0]
    return Stats(int(_num(r, "calls")), _num(r, "avg_ms"), _num(r, "max_ms"), _num(r, "min_ms"),
                 _num(r, "total_ms"), _num(r, "cpu_ms"), _num(r, "io_ms"))


def _hour_of(ts: str) -> str:
    return _dt.datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S").strftime(HOUR_FMT)


def hourly(rows: Sequence[dict]) -> Tuple[Tuple[HourPoint, ...], Tuple[str, ...]]:
    """快照累计值 → 按小时的增量。后一个比前一个小 = 计数器清零过,那一段丢弃并记下时刻。"""
    rows = sorted(rows, key=lambda r: int(_num(r, "snapshot_id")))
    acc: Dict[str, List[float]] = {}
    resets: List[str] = []
    for prev, cur in zip(rows, rows[1:]):
        d_calls = _num(cur, "calls") - _num(prev, "calls")
        d_us = _num(cur, "total_us") - _num(prev, "total_us")
        if d_calls < 0 or d_us < 0:
            resets.append(str(cur.get("snap_ts") or "")[:16])
            continue
        if d_calls == 0:
            continue
        slot = acc.setdefault(_hour_of(str(cur.get("snap_ts"))), [0.0, 0.0])
        slot[0] += d_calls
        slot[1] += d_us / 1000.0
    points = tuple(HourPoint(h, int(c), t) for h, (c, t) in sorted(acc.items()))
    return points, tuple(resets)


def find_jump(points: Sequence[HourPoint]) -> Optional[Jump]:
    """最早一个「平均耗时 ≥ 此前 24 小时中位数 JUMP_RATIO 倍、且 ≥ JUMP_MIN_MS」的小时。"""
    stamps = [_dt.datetime.strptime(p.hour, HOUR_FMT) for p in points]
    for i, p in enumerate(points):
        lo = stamps[i] - _dt.timedelta(hours=BASELINE_HOURS)
        base = [q.avg_ms for q, t in zip(points[:i], stamps[:i]) if t >= lo]
        if len(base) < MIN_BASELINE_POINTS:
            continue
        median = statistics.median(base)
        if median > 0 and p.avg_ms >= JUMP_RATIO * median and p.avg_ms >= JUMP_MIN_MS:
            return Jump(p.hour, p.avg_ms, median)
    return None


def plan_shape(plan: str) -> Tuple[str, ...]:
    """计划的节点结构:只留首行与 `->` 节点行,去掉括号里的估算 / 实际值与全部数字。绑定值不同不改变它。"""
    out = []
    for i, line in enumerate((plan or "").splitlines()):
        if not line.strip() or (i > 0 and "->" not in line):
            continue
        text = line
        while True:
            stripped = re.sub(r"\([^()]*\)", "", text)
            if stripped == text:
                break
            text = stripped
        out.append(" ".join(re.sub(r"\d+", "", text).split()))
    return tuple(out)


def parse_history(rows: Sequence[dict]) -> Tuple[HistHour, ...]:
    return tuple(HistHour(str(r.get("hour") or "")[:13] + ":00", int(_num(r, "runs")), _num(r, "avg_ms"),
                          _num(r, "max_ms"), str(r.get("plan_sig") or ""), str(r.get("query_plan") or ""))
                 for r in sorted(rows, key=lambda r: str(r.get("hour") or "")))


def plan_changes(hist: Sequence[HistHour]) -> Tuple[PlanChange, ...]:
    out: List[PlanChange] = []
    prev: Optional[HistHour] = None
    for h in hist:
        if not h.plan_sig:
            continue
        if prev is not None and h.plan_sig != prev.plan_sig and h.plan:
            if plan_shape(h.plan) != plan_shape(prev.plan):
                out.append(PlanChange(h.hour, prev.hour, prev.plan, h.plan))
        if h.plan:                                     # 指纹变了才带计划;没带的沿用上一份
            prev = h
        elif prev is None:
            prev = h
    return tuple(out)


# ---------------------------------------------------------------- 取数

def _try(runner, script: str, values: dict):
    """取不到(未登记、执行报错、中间件拒绝)→ (None, 原因)。参数错、列定义错这类缺陷照常抛出,不在这里吞掉。"""
    from common.grmp.errors import QueryError
    from common.grmp.registry import RegistryError
    try:
        return runner.run(script, values), ""
    except (QueryError, RegistryError) as exc:
        first = (str(exc).splitlines() or [type(exc).__name__])[0]
        return None, first[:200]


NO_ID_NOTE = "没有 sql_id(按 SQL 文本调优),现状与趋势暂不可得;用 sql_id 调优才有。"


def unavailable(note: str = NO_ID_NOTE) -> TrendReport:
    return TrendReport(stats_note=note)


def collect(runner, sql_id: str, days: int = TREND_DAYS) -> TrendReport:
    try:
        sid = int(str(sql_id).strip())
    except ValueError:
        return unavailable()
    rows, err = _try(runner, STAT_SCRIPT, {"sid": sid})
    stats = parse_stats(rows or [])
    stats_note = ("暂不可得(%s)" % err) if err else ("" if stats else "统计视图里没有这条语句(统计清零后还没再执行过)。")
    trows, terr = _try(runner, TREND_SCRIPT, {"sid": sid, "days": days})
    points, resets = hourly(trows or [])
    trend_note = ("暂不可得(%s)" % terr) if terr else ("" if points else "近 %d 天的 WDR 快照里没有这条语句的增量。" % days)
    hrows, herr = _try(runner, HISTORY_SCRIPT, {"sid": sid, "days": days})
    hist = parse_history(hrows or [])
    plan_note = ("暂不可得(%s)" % herr) if herr else ("" if hist else "近 %d 天的逐次执行记录里没有这条语句。" % days)
    return TrendReport(stats, stats_note, points, resets, find_jump(points), trend_note,
                       hist, plan_changes(hist), plan_note)

"""「现状与趋势」三节的 markdown:`## Current Stats` / `## Trend` / `## Plan Changes`(SKILL.md 第 8 步要求照抄)。

数据与判定在 sqltrend.py;这里只排版。口径写在每节开头:哪些是实测、取自哪里、看得到多远。
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
from typing import List

import render
from sqltrend import (CORRELATE_HOURS, JUMP_MIN_MS, JUMP_RATIO, PLAN_MAX, TREND_DAYS, HourPoint, TrendReport,
                   HOUR_FMT)

_AROUND = 3          # 突然变慢那一小时前后各列几小时


def _ms(x: float) -> str:
    return "%.1f ms" % x if x < 1000 else "%.2f s" % (x / 1000.0)


def _t(hour: str) -> _dt.datetime:
    return _dt.datetime.strptime(hour, HOUR_FMT)


def stats_section(r: TrendReport) -> str:
    out = "## Current Stats\n\n"
    s = r.stats
    if s is None:
        return out + "> 现状数据%s\n\n" % (r.stats_note or "暂不可得。")
    out += render.table(["执行次数", "平均耗时", "最大耗时", "最小耗时", "总耗时", "CPU 时间", "数据 IO 时间"],
                        [[str(s.calls), _ms(s.avg_ms), _ms(s.max_ms), _ms(s.min_ms), _ms(s.total_ms),
                          _ms(s.cpu_ms), _ms(s.io_ms)]])
    out += "\n> 口径:dbe_perf.statement 自上次统计清零(实例重启或统计重置)以来的累计,实测值。\n"
    if s.total_ms > 0:
        share = (s.cpu_ms + s.io_ms) / s.total_ms * 100
        out += "> CPU + 数据 IO 占总耗时 %.0f%%%s\n" % (
            share, ";其余时间在等待(锁、轻量锁、排队等),不全是语句自己慢" if share < 50 else "")
    return out + "\n"


def _avg(points: List[HourPoint]) -> float:
    calls = sum(p.calls for p in points)
    return sum(p.total_ms for p in points) / calls if calls else 0.0


def trend_section(r: TrendReport) -> str:
    out = "## Trend\n\n"
    if not r.points:
        return out + "> 趋势%s\n\n" % (r.trend_note or "暂不可得。")
    out += ("> 近 %d 天、按小时:相邻两个 WDR 快照相减得到的执行次数与平均耗时(实测)。"
            "快照多久一个由 wdr_snapshot_interval 决定,更早的超出了快照保留期。\n\n" % TREND_DAYS)
    last = _t(r.points[-1].hour)
    recent = [p for p in r.points if _t(p.hour) > last - _dt.timedelta(hours=24)]
    before = [p for p in r.points if _t(p.hour) <= last - _dt.timedelta(hours=24)]
    if recent and before and _avg(before):
        out += "- 最近 24 小时平均 %s(%d 次);之前平均 %s(%d 次),是之前的 %.1f 倍。\n" % (
            _ms(_avg(recent)), sum(p.calls for p in recent), _ms(_avg(before)), sum(p.calls for p in before),
            _avg(recent) / _avg(before))
    if r.jump:
        out += "- **突然变慢**:%s 这一小时平均 %s,是此前 24 小时中位数 %s 的 %.1f 倍。\n" % (
            r.jump.hour, _ms(r.jump.avg_ms), _ms(r.jump.baseline_ms), r.jump.ratio)
    else:
        out += "- 近 %d 天没有突然变慢的小时(判定:某小时平均 ≥ 此前 24 小时中位数的 %.0f 倍且 ≥ %.0f ms)。\n" % (
            TREND_DAYS, JUMP_RATIO, JUMP_MIN_MS)
    if r.resets:
        out += "- 计数器在 %s 前后被清零(实例重启或统计重置),跨清零的那一段不计。\n" % "、".join(r.resets)
    days: dict = {}
    for p in r.points:
        days.setdefault(p.hour[:10], []).append(p)
    rows = [[d, str(sum(p.calls for p in ps)), _ms(_avg(ps)), "%s(%s)" % (max(ps, key=lambda p: p.avg_ms).hour[11:],
                                                                         _ms(max(p.avg_ms for p in ps)))]
            for d, ps in sorted(days.items())]
    out += "\n" + render.table(["日期", "执行次数", "平均耗时", "平均最慢的一小时"], rows)
    if r.jump:
        j = _t(r.jump.hour)
        near = [p for p in r.points if abs((_t(p.hour) - j).total_seconds()) <= _AROUND * 3600]
        out += "\n突然变慢前后:\n\n" + render.table(["小时", "执行次数", "平均耗时"],
                                                 [[p.hour, str(p.calls), _ms(p.avg_ms)] for p in near])
    return out + "\n"


def _plan_block(plan: str) -> str:
    body = plan.rstrip()
    if len(body) > PLAN_MAX:
        body = body[:PLAN_MAX] + "\n…(计划更长,到大盘或重跑 explain 看全)"
    return render.code_block("", body)


def plan_section(r: TrendReport) -> str:
    out = "## Plan Changes\n\n"
    if not r.history:
        return out + "> 执行计划历史%s\n\n" % (r.plan_note or "暂不可得。")
    out += ("> 来源:statement_history 里记下的真实执行计划(只含被记下的执行:慢于阈值或开了全量跟踪;"
            "保留期由参数决定),每小时取最近一次。\n\n")
    slow = max(r.history, key=lambda h: h.max_ms)
    out += "- 记下的执行里最慢的一次在 %s 这一小时:%s。\n" % (slow.hour, _ms(slow.max_ms))
    if not r.changes:
        out += "- 近 %d 天记下的 %d 个小时里,执行计划没有变过(节点结构一致)。\n" % (TREND_DAYS, len(r.history))
    if r.jump:
        hit = [c for c in r.changes if abs((_t(c.hour) - _t(r.jump.hour)).total_seconds()) <= CORRELATE_HOURS * 3600]
        if hit:
            out += "- **突然变慢的时间(%s)与执行计划变化(%s)吻合**,计划变化是首要怀疑对象。\n" % (r.jump.hour, hit[0].hour)
        elif any(h.hour == r.jump.hour for h in r.history) and not r.changes:
            out += "- 突然变慢那一小时执行计划没有变,原因多半在计划之外(锁等待、数据量、资源争用)。\n"
    for c in r.changes:
        out += "\n### %s 起执行计划变了(之前一份记于 %s)\n\n之前:\n\n%s\n之后:\n\n%s" % (
            c.hour, c.before_hour, _plan_block(c.before_plan), _plan_block(c.after_plan))
    return out + "\n"


def report(r: TrendReport) -> str:
    return stats_section(r) + trend_section(r) + plan_section(r)


def to_dict(r: TrendReport) -> dict:
    d = dataclasses.asdict(r)
    if r.jump:
        d["jump"]["ratio"] = round(r.jump.ratio, 2)
    return d

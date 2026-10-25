"""Top SQL 的「某个时间段」与「最近 N 分钟」。

用户 2026-10-18 定的原则:**不追求精确,只要把用户说的时间段覆盖住;并告诉用户实际
评估的是哪一段、为什么。** 端点怎么选、说明怎么写,全在 common/timewin.py;
这里只负责「每类端点怎么取数」和「取回来怎么相减」。

三类端点各自的取数:
  snapshot  topsql.snap_statement(那一刻的语句累计值,按 sql_id 聚合)
  sample    我们自己存的采样历史(delta.load_history)
  live      topsql.top_sql(此刻的累计值)
三者口径一致(og5 上逐类核对过:实时值 >= 快照值,单位都是微秒),所以可以任意两两相减,
相减本身复用 delta.diff —— 新出现的语句整条计入、计数变小判为统计清零,规则不变。

**起点取宽、终点取窄。** 终点只取前 SAMPLE_WIDTH 条来排名,但每一条都要在起点里找到前值;
起点取得窄的话,终点里排第 300 的语句可能在起点里被截掉,于是被当成「新出现」整条计入、
虚高。所以起点按 START_WIDTH 取。

取不到快照(WDR 没开 / 白名单没导这条脚本)或取不到数据库时钟时,`plan()` 返回 None,
调用方退回原来的「自存基线 / 现场采一小段」做法 —— 那条路自己会说明它覆盖不到用户要的时间段。
"""
from __future__ import annotations

import datetime as _dt
from typing import List, NamedTuple, Optional

from common import timewin, windata

import delta as delta_mod

SNAP_STMT_SCRIPT = "topsql.snap_statement"
START_WIDTH = 5000              # 起点取宽,见模块说明


class Plan(NamedTuple):
    coverage: timewin.Coverage
    clock: windata.Clock


def clock(runner) -> Optional[windata.Clock]:
    """语句累计值是内存态的,只随实例重启清零 —— 不把 pg_stat_reset 算作清零。"""
    return windata.clock(runner, with_stats_reset=False)


def snapshot_points(runner) -> List[timewin.Point]:
    return windata.snapshot_points(runner)


def sample_points(history: List[delta_mod.Sample], offset: _dt.timedelta) -> List[timewin.Point]:
    """自存采样 → 端点(时间戳从容器 UTC 换到数据库本地时间)。"""
    out = []
    for s in history or []:
        at = timewin.to_local(s.at, offset)
        if at is not None:
            out.append(timewin.Point("sample", at, s))
    return out


def plan(runner, want_from, want_to, history, ck: windata.Clock) -> Optional[Plan]:
    """选窗口。快照与自存采样一个都没有时返回 None(调用方决定退回老做法还是报错)。"""
    pts = snapshot_points(runner) + sample_points(history, ck.offset)
    if not pts:
        return None
    cov = timewin.cover(want_from, want_to, pts, ck.db_now, ck.reset_at)
    return Plan(coverage=cov, clock=ck)


def fetch(runner, point: timewin.Point, width: int, live_sample) -> delta_mod.Sample:
    """取一个端点的语句累计值。live_sample 是调用方的「此刻采样」函数(复用 topsql 的取数)。"""
    if point.kind == "sample":
        return point.ref
    if point.kind == "live":
        return live_sample()
    rows = runner.run(SNAP_STMT_SCRIPT, {"snap": int(point.ref), "limit": int(width)})
    return delta_mod.from_rows(point.at.strftime("%Y%m%dT%H%M%S"), rows)


def title(cov: timewin.Coverage, by_label: str) -> str:
    return "Top SQL（%s → %s，按%s）" % (cov.start.label(), cov.end.label(), by_label)

"""时间段功能的取数:快照列表、数据库时钟。Top SQL 与健康检查共用。

端点怎么选、说明怎么写在 common/timewin.py(纯函数);这里只负责把库里的事实读出来。
**任何失败都返回「没有」而不是抛异常** —— WDR 可能没开、白名单里可能还没导这几条脚本,
调用方据此退回老做法或如实说明,而不是让整次分析失败。
"""
from __future__ import annotations

import datetime as _dt
from typing import List, NamedTuple, Optional

from common import access, timewin
from common.grmp.values import is_null

SNAPSHOTS_SCRIPT = "wdr.snapshots"
CLOCK_SCRIPT = "health.live_counters"
SNAPSHOT_LOOKBACK = 500     # 每小时一个快照时 500 个是 20 天,够覆盖保留期(og5 是 8 天)

_TS = "%Y-%m-%d %H:%M:%S"


class Clock(NamedTuple):
    db_now: _dt.datetime
    reset_at: Optional[_dt.datetime]    # 这之前的起点不能和之后的终点相减
    offset: _dt.timedelta               # 数据库本地时间 − UTC
    postmaster_start: Optional[_dt.datetime]
    stats_reset: Optional[_dt.datetime]


def parse_ts(text) -> Optional[_dt.datetime]:
    if is_null(text):
        return None
    try:
        return _dt.datetime.strptime(str(text).strip()[:19], _TS)
    except (TypeError, ValueError):
        return None


def utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)


def clock(runner, with_stats_reset: bool) -> Optional[Clock]:
    """数据库此刻的时间、实例启动与统计重置时刻。取不到返回 None(不猜)。

    `with_stats_reset`:要不要把 pg_stat_reset() 也算作「计数器清零」。
      · Top SQL 不算 —— 语句累计值是内存态的,只随实例重启清零,与 pg_stat_reset 无关;
      · 健康检查要算 —— 提交 / 回滚 / 死锁这族就在 pg_stat_database 里,会被单独清零。
    取两者中**较晚**的那个当界线:保守,但不会把清零前后的值拿来相减。
    """
    try:
        rows = runner.run(CLOCK_SCRIPT, {})
        kv = {r["item"]: r["value"] for r in rows or []}
    except (access.QueryError, KeyError, TypeError):
        return None
    now = parse_ts(kv.get("now"))
    if now is None:
        return None
    pm = parse_ts(kv.get("postmaster_start"))
    sr = parse_ts(kv.get("stats_reset"))
    marks = [t for t in (pm, sr if with_stats_reset else None) if t is not None]
    return Clock(db_now=now, reset_at=max(marks) if marks else None,
                 offset=timewin.db_offset(now, utc_now()), postmaster_start=pm, stats_reset=sr)


def snapshot_points(runner) -> List[timewin.Point]:
    """WDR 快照 → 端点。WDR 没开、脚本没导、行形状不对,都当成「没有快照」。"""
    try:
        rows = runner.run(SNAPSHOTS_SCRIPT, {"limit": SNAPSHOT_LOOKBACK})
    except access.QueryError:
        return []
    out = []
    for r in rows or []:
        try:
            sid = int(float(r["snapshot_id"]))
        except (KeyError, TypeError, ValueError):
            continue
        at = parse_ts(r.get("start_ts")) or _minute(r.get("start_ts"))
        if at is not None:
            out.append(timewin.Point("snapshot", at, sid))
    return out


def _minute(text) -> Optional[_dt.datetime]:
    """老版本的 wdr.snapshots 只给到分钟(现场可能还没重导白名单)。照样能用,只是粗一点。"""
    try:
        return _dt.datetime.strptime(str(text).strip()[:16], "%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return None

"""`wdr snaps` — list WDR snapshots + preflight enable_wdr_snapshot.

Port of internal/probe/wdr/snaps.go. Returns an ERROR (not a degraded report)
when WDR is off or fewer than 2 snapshots exist — the toolkit never creates
snapshots; it tells the user to enable WDR / create one themselves.
"""
from __future__ import annotations

import datetime as _dt

import render
from util import i64

import common  # resolved on sys.path by the entry script

import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve()

for parent in _HERE.parents:
    if (parent / "common" / "sql.py").exists():
        sys.path.insert(0, str(parent))
        break

# SQL 已迁到 scripts/registry/wdr/ —— 两条路径共用同一份定义
# 取数失败只认这一个类型。换一种数据库访问方式时，改的是访问模块，
# 不是这里 —— 详见 common/grmp/errors.py。
from common import access  # noqa: E402
# 结果值全是字符串：bool("f") 是 True、int("3704.0") 会抛异常。
# 类型还原一律走这里，不用裸 int()/float()/bool()。
from common.grmp.values import as_bool, as_float, as_int  # noqa: E402


# 按时间找窗口时多取一些快照:默认 20 个在每小时一次的库上只够一天,前天的故障就找不到了
WINDOW_LOOKBACK = 500
_TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M")
WIDE_FACTOR = 2           # 窗口比时间段长这么多倍以上就提醒


def _minute(text: str, what: str):
    """用户给的时间 → datetime(秒照用户给的,没给就是 0 秒)。

    快照时间只到分钟时(老版本的 wdr.snapshots,现场可能还没重导)由 _cover 把两边都截到分钟比较;
    快照时间到秒时按秒比较 —— 与时段体检 / 时段 Top SQL(common/timewin)同一条覆盖规则:
    用户要从 10:46 起,10:46:29 的快照晚于 10:46,不能当起点(2026-10-19 测试:WDR 选了 2981,
    健康检查选了 2980,同一个问题两处窗口不一样)。"""
    raw = (text or "").strip()
    for fmt in _TIME_FORMATS:
        try:
            return _dt.datetime.strptime(raw, fmt)
        except ValueError:
            continue
    raise common.DBError("%s「%s」看不懂:写成 YYYY-MM-DD HH:MM(数据库所在时区),例如 2026-10-16 15:16" % (what, raw))


def _snap_time(text: str):
    raw = str(text).strip()
    for fmt, n in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d %H:%M", 16)):
        try:
            return _dt.datetime.strptime(raw[:n], fmt)
        except ValueError:
            continue
    return None


def _has_seconds(snaps_list) -> bool:
    return all(len(str(st).strip()) >= 19 for _i, st, _e, _d in snaps_list)


def _cover(snaps_list, t_from: str, t_to: str) -> str:
    """围住 [t_from, t_to] 的最小窗口:起点 = 开始之前(含)最后一个快照,终点 = 结束之后(含)第一个快照。

    围不住就抛错,说清缺哪一头、怎么办,并给出能用的近似窗口 —— **由用户决定**,不悄悄只覆盖一部分。
    """
    asc = sorted(((i, _snap_time(st)) for i, st, _e, _d in snaps_list if _snap_time(st)), key=lambda x: x[0])
    if len(asc) < 2:
        raise common.DBError("能读到时间的快照不足 2 个,围不出窗口。")
    a = _minute(t_from, "开始时间") if t_from else None
    b = _minute(t_to, "结束时间") if t_to else None
    if not _has_seconds(snaps_list):      # 快照只到分钟:两边都按分钟比,同一分钟的快照算「不晚于」
        a = a.replace(second=0) if a else None
        b = b.replace(second=0) if b else None
    if a and b and a > b:
        raise common.DBError("开始时间 %s 晚于结束时间 %s。" % (t_from, t_to))
    oldest, newest = asc[0], asc[-1]
    begin = None
    if a:
        before = [x for x in asc if x[1] <= a]
        if not before:
            raise common.DBError(
                "时间段开始 %s 早于能查到的最早快照 %d(%s)—— 更早的快照可能已按保留期清理"
                "(wdr_snapshot_retention_days),围不住这一头。若接受从 %s 起算,用 `--begin %d --end <终点>`。"
                % (t_from, oldest[0], oldest[1].strftime("%Y-%m-%d %H:%M"), oldest[1].strftime("%H:%M"), oldest[0]))
        begin = before[-1]
    after = [x for x in asc if begin is None or x[0] > begin[0]]
    end = next((x for x in after if x[1] >= b), None) if b else (after[-1] if after else None)
    if end is None:
        hint = ("若只分析到 %s 为止,用 `--begin %d --end %d`(只覆盖到那时)。"
                % (newest[1].strftime("%H:%M"), begin[0], newest[0])) if begin and newest[0] > begin[0] else ""
        raise common.DBError(
            "时间段结束 %s 晚于最新的快照 %d(%s),还没有快照能围住它:等下一次自动快照"
            "(间隔 wdr_snapshot_interval),或请 DBA 自行 `SELECT create_wdr_snapshot();` —— 本工具不创建快照。%s"
            % (t_to or "(未给)", newest[0], newest[1].strftime("%Y-%m-%d %H:%M"), hint))
    if begin is None:                     # 只给了结束时间:起点 = 终点之前的那一个快照
        prev = [x for x in asc if x[0] < end[0]]
        if not prev:
            raise common.DBError("结束时间 %s 之前没有快照,围不出窗口。" % t_to)
        begin = prev[-1]
    win_min = (end[1] - begin[1]).total_seconds() / 60
    want_min = ((b or end[1]) - (a or begin[1])).total_seconds() / 60
    out = ("\n覆盖 %s – %s 的窗口:`--begin %d --end %d`(快照 %d 于 %s,快照 %d 于 %s)。\n"
           % (t_from or begin[1].strftime("%Y-%m-%d %H:%M"), t_to or "最新快照", begin[0], end[0],
              begin[0], begin[1].strftime("%m-%d %H:%M"), end[0], end[1].strftime("%m-%d %H:%M")))
    if want_min > 0 and win_min > WIDE_FACTOR * want_min:
        out += ("> 窗口(%d 分钟)比时间段宽(%d 分钟):WDR 只能按快照算,窗口里其他时间的负载也会算进去;"
                "要更准,得在故障前后各有一个快照。\n" % (win_min, want_min))
    return out


def snaps(runner, limit: int, t_from: str = "", t_to: str = "") -> str:
    if t_from or t_to:
        limit = max(limit, WINDOW_LOOKBACK)
    if limit <= 0:
        limit = 20
    try:
        rows = runner.run("wdr.wdr_enabled")
        enabled = rows[0]["enable_wdr_snapshot"] if rows else ""
    except access.QueryError as exc:
        raise common.DBError(f"读取 enable_wdr_snapshot 失败：{exc}")
    if str(enabled or "").strip().lower() != "on":
        raise common.DBError(
            "WDR 未开启（enable_wdr_snapshot=off）。请由 DBA 执行 "
            "`ALTER SYSTEM SET enable_wdr_snapshot=on`（需 reload/重启）并等待自动快照，"
            "或自行 `SELECT create_wdr_snapshot();`。本工具只读、不代为开启或创建。")

    try:
        rows = runner.run("wdr.snapshots", {"limit": int(limit)})
    except access.QueryError as exc:
        raise common.DBError(f"查询 snapshot.snapshot 失败：{exc}")

    snaps_list = [(as_int(r["snapshot_id"]), r["start_ts"], r["end_ts"],
                   as_int(r["dur_min"])) for r in rows]
    if len(snaps_list) < 2:
        raise common.DBError(
            f"可用快照不足（{len(snaps_list)} 个）：WDR 报告至少需要两个快照围出窗口。"
            "请等待下一个自动快照间隔，或由 DBA 自行 `SELECT create_wdr_snapshot();`。")

    if t_from or t_to:
        cover = _cover(snaps_list, t_from, t_to)
        shown = snaps_list[:20]            # 表格只列最近 20 个,窗口那一行才是要用的
    else:
        cover, shown = "", snaps_list
    tbl = [[i64(s[0]), s[1], s[2], i64(s[3])] for s in shown]
    out = f"# WDR Snapshots（enable_wdr_snapshot={enabled}）\n\n"
    out += render.table(["snapshot_id", "start_ts", "end_ts", "dur_min"], tbl)
    if cover:
        return out + cover
    # snaps_list[0] is newest (DESC); suggest the two most-recent consecutive.
    out += f"\n建议窗口：`--begin {snaps_list[1][0]} --end {snaps_list[0][0]}`（最近两个快照）。\n"
    return out

"""wdr snaps --from/--to:按时间段算出刚好围住它的快照窗口。

**为什么(2026-10-16):** 模型做 WDR 时会挑错窗口(说「分析了 2930→2931」,存档却是 2931→2932),
用户要「覆盖 15:16–15:26 那次故障」时只能自己数快照号。现在由脚本按快照时间算:
起点 = 时间段开始之前(含)最后一个快照,终点 = 时间段结束之后(含)第一个快照;按分钟比(快照时间只到分钟)。
**围不住就报错**(不确定当失败):时间段早于能查到的最早快照、或晚于最新快照时,说清缺哪一头和怎么办,
不悄悄给一个只覆盖一部分的窗口。窗口比时间段大很多时要提醒:WDR 只能按快照算,窗口里其他时间的负载也算进去。
用的还是 wdr.snapshots(多取一些行),不改白名单。
"""
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "skills" / "gaussdb-wdr" / "scripts"
for _m in ("model", "thresholds", "util", "collectors", "report", "native", "snaps", "interp", "recheck", "finalreport", "ansi", "render"):
    sys.modules.pop(_m, None)
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_ROOT))

import common  # noqa: E402
import snaps as S  # noqa: E402

# og5 上那次故障模拟前后各打了一个快照;再往前是每小时的自动快照
ROWS = [  # DESC,和 wdr.snapshots 一样
    {"snapshot_id": "2938", "start_ts": "2026-10-16 16:00", "end_ts": "2026-10-16 16:00", "dur_min": "0"},
    {"snapshot_id": "2937", "start_ts": "2026-10-16 15:26", "end_ts": "2026-10-16 15:26", "dur_min": "0"},
    {"snapshot_id": "2936", "start_ts": "2026-10-16 15:16", "end_ts": "2026-10-16 15:16", "dur_min": "0"},
    {"snapshot_id": "2935", "start_ts": "2026-10-16 15:00", "end_ts": "2026-10-16 15:00", "dur_min": "0"},
    {"snapshot_id": "2934", "start_ts": "2026-10-16 14:00", "end_ts": "2026-10-16 14:00", "dur_min": "0"},
]


class _Runner:
    def __init__(self, rows=ROWS):
        self.rows, self.limits = rows, []

    def run(self, name, params=None):
        if name == "wdr.wdr_enabled":
            return [{"enable_wdr_snapshot": "on"}]
        self.limits.append((params or {}).get("limit"))
        return self.rows[: (params or {}).get("limit") or 20]


def test_window_that_exactly_wraps_the_incident():
    out = S.snaps(_Runner(), 20, "2026-10-16 15:16:42", "2026-10-16 15:26:46")
    assert "`--begin 2936 --end 2937`" in out
    assert "15:16" in out and "15:26" in out


def test_window_inside_hourly_snapshots_is_wider_and_says_so():
    out = S.snaps(_Runner(), 20, "2026-10-16T14:20", "2026-10-16T14:40")
    assert "`--begin 2934 --end 2935`" in out
    assert "比时间段宽" in out, "窗口 60 分钟、时间段 20 分钟:要提醒窗口里其他时间的负载也算进去"


def test_only_from_means_up_to_the_latest_snapshot():
    assert "`--begin 2936 --end 2938`" in S.snaps(_Runner(), 20, "2026-10-16 15:20", "")


def test_end_after_the_latest_snapshot_is_an_error_with_a_way_out():
    with pytest.raises(common.DBError) as e:
        S.snaps(_Runner(), 20, "2026-10-16 15:30", "2026-10-16 16:30")
    msg = str(e.value)
    assert "最新的快照" in msg and "2938" in msg and "不创建快照" in msg
    assert "--begin 2937 --end 2938" in msg, "给出能用的近似窗口,但由用户决定"


def test_start_before_the_oldest_snapshot_is_an_error():
    with pytest.raises(common.DBError) as e:
        S.snaps(_Runner(), 20, "2026-10-16 13:00", "2026-10-16 14:30")
    assert "最早" in str(e.value) and "2934" in str(e.value)


def test_time_filter_looks_further_back_than_the_default_twenty():
    r = _Runner()
    S.snaps(r, 20, "2026-10-16 15:16", "2026-10-16 15:26")
    assert r.limits and r.limits[-1] >= 500, "按时间找窗口要多取一些快照,否则一天前的窗口根本看不到"


def test_bad_time_format_is_refused():
    with pytest.raises(common.DBError) as e:
        S.snaps(_Runner(), 20, "昨天下午", "")
    assert "YYYY-MM-DD HH:MM" in str(e.value)


def test_without_times_the_old_suggestion_is_unchanged():
    out = S.snaps(_Runner(), 20)
    assert "`--begin 2937 --end 2938`" in out and "最近两个快照" in out


def test_only_to_means_the_window_that_ends_there():
    assert "`--begin 2935 --end 2936`" in S.snaps(_Runner(), 20, "", "2026-10-16 15:10")


def test_with_second_precision_a_snapshot_inside_the_start_minute_is_not_the_start():
    """与时段体检同一条覆盖规则:要从 10:46 起,10:46:29 的快照晚于 10:46,起点取 10:45:29 那一个。
    2026-10-19 测试:同一个时间段,WDR 选了 2981、健康检查选了 2980。"""
    snaps = [(2980, "2026-10-17 10:45:29", "", ""), (2981, "2026-10-17 10:46:29", "", ""),
             (2986, "2026-10-17 11:06:55", "", ""), (2987, "2026-10-17 11:08:02", "", "")]
    out = S._cover(snaps, "2026-10-17 10:46", "2026-10-17 11:07")
    assert "--begin 2980 --end 2987" in out


def test_minute_precision_snapshots_keep_the_old_same_minute_rule():
    """老版本 wdr.snapshots 只给到分钟:同一分钟的快照仍算「不晚于」,不因为看不到秒而多退一个快照。"""
    snaps = [(2980, "2026-10-17 10:45", "", ""), (2981, "2026-10-17 10:46", "", ""),
             (2986, "2026-10-17 11:06", "", ""), (2987, "2026-10-17 11:08", "", "")]
    assert "--begin 2981 --end 2987" in S._cover(snaps, "2026-10-17 10:46", "2026-10-17 11:07")

"""WDR「写入来源」的退路(2026-11-12 行方:生产环境权限与参数固定,能调的只有发出去的 SQL)。

快照表里没有这段时间的表级写入 / redo 写入时,改读实时累计值(health.table_writes、wdr.redo_io_live),
一句话总结里写明口径「不是所选时间段」;实时值也取不到时,快照查询报错的写「这一项无法查询,不影响其余部分」。
"""
import pathlib
import sys
from types import SimpleNamespace

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "skills" / "gaussdb-wdr" / "scripts"
for _m in ("model", "thresholds", "util", "collectors", "report",
           "native", "snaps", "interp", "recheck", "finalreport", "kbrefs", "appendix", "mdreport", "render", "ansi"):
    sys.modules.pop(_m, None)                  # 健康检查也有同名的 collectors / model,别拿错
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_ROOT))

import collectors  # noqa: E402
from common import access  # noqa: E402

OPT = SimpleNamespace(begin=10, end=12, top=5)
LIVE = [{"schema": "app", "table": "orders", "is_toast": "f", "n_tup_ins": "100", "n_tup_upd": "50",
         "n_tup_del": "0", "toast_bytes": ""}]


class _R:
    def __init__(self, canned, fail=()):
        self.canned, self.fail, self.calls = canned, fail, []

    def run(self, script, values=None):
        self.calls.append(script)
        if script in self.fail:
            raise access.QueryError("permission denied for relation snap_global_stat_all_tables")
        return self.canned.get(script, [])


def test_snapshot_rows_are_used_as_before():
    r = _R({"wdr.table_writes": [{"schema": "app", "table": "t", "is_toast": "f", "ins": "5", "upd": "0", "del": "0"}],
            "wdr.redo_io": [{"writes": "3", "blocks": "40"}]})
    d = collectors.collect_writes(r, OPT)
    assert d.available and d.rows[0][0] == "app.t" and "实时累计" not in d.headline
    assert collectors.LIVE_TABLE_WRITES not in r.calls and collectors.LIVE_REDO not in r.calls


def test_empty_snapshots_fall_back_to_live_totals_and_say_so():
    r = _R({"wdr.redo_io": [{"writes": "0", "blocks": "0"}], collectors.LIVE_TABLE_WRITES: LIVE,
            collectors.LIVE_REDO: [{"writes": "7", "blocks": "1024"}]})
    d = collectors.collect_writes(r, OPT)
    assert d.available and d.rows[0][0] == "app.orders"
    assert "快照里没有这段时间的表级写入记录" in d.headline and "不是所选时间段" in d.headline
    assert "快照里没有这段时间的 redo 写入记录" in d.headline
    assert any(x in d.headline for x in ("1,024 块", "1024 块"))


def test_snapshot_error_with_live_data_still_reports():
    r = _R({collectors.LIVE_TABLE_WRITES: LIVE}, fail=("wdr.table_writes",))
    d = collectors.collect_writes(r, OPT)
    assert d.available and d.rows[0][0] == "app.orders" and "不是所选时间段" in d.headline


def test_nothing_anywhere_after_an_error_says_this_part_cannot_be_queried():
    r = _R({}, fail=("wdr.table_writes", collectors.LIVE_TABLE_WRITES))
    d = collectors.collect_writes(r, OPT)
    assert not d.available and "这一项无法查询" in d.note and "不影响其余部分" in d.note


def test_genuinely_quiet_window_without_live_data_keeps_the_old_wording():
    r = _R({"wdr.redo_io": [{"writes": "0", "blocks": "0"}]})
    d = collectors.collect_writes(r, OPT)
    assert d.available and d.headline.startswith("窗口内没有表级写入")

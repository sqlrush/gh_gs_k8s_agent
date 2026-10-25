"""WDR 的 Top SQL 排除内核系统 SQL(2026-10-22 客户要求):榜单与各维度元凶都不从系统 SQL 里挑,
标题写明排除了几条;落盘总量仍按全部语句算(系统 SQL 落的盘也是「能归到语句的」)。"""
from __future__ import annotations

import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "skills" / "gaussdb-wdr" / "scripts"
for _m in ("model", "thresholds", "util", "collectors", "report",
           "native", "snaps", "interp", "recheck", "finalreport", "ansi", "followup"):
    sys.modules.pop(_m, None)
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_ROOT))

import collectors as C                                   # noqa: E402
import thresholds                                        # noqa: E402
from model import Options                                # noqa: E402



@pytest.fixture(autouse=True)
def _state_dir(monkeypatch, tmp_path):
    """判定缓存落在状态目录:测试里指到临时目录,不碰开发机上真的 GSDB_HOME。"""
    monkeypatch.setenv("GSDB_HOME", str(tmp_path / "gdaa"))


class Runner:
    def __init__(self, rows):
        self.rows = rows

    def run(self, name, params=None):
        if name != "wdr.top_sql":
            return []
        return self.rows[: int(params["top"])]


def stmt(sid, elapsed_s, query, spill_mb=0.0, cpu_s=0.0, calls=1):
    return {"sid": sid, "query": query, "calls": str(calls), "elapsed_us": str(int(elapsed_s * 1e6)),
            "cpu_us": str(int(cpu_s * 1e6)), "spill_kb": str(int(spill_mb * 1024)), "phys_blocks": "0"}


def _opt():
    o = Options(begin=1, end=2, top=2, thresholds=thresholds.default_thresholds())
    o.db_time_us = 1_000_000_000
    return o


def test_system_sql_is_neither_listed_nor_picked_as_a_culprit():
    rows = [stmt("9", 500, "SELECT generate_wdr_report(?, ?, ?, ?, ?) AS report_line;", cpu_s=400, calls=900),
            stmt("8", 300, "select ?, ?, t.* from dbe_perf.summary_statio_all_indexes t", spill_mb=50),
            stmt("1", 200, "UPDATE fl.acct SET bal = bal + ? WHERE id = ?", cpu_s=1),
            stmt("2", 100, "SELECT * FROM fl.orders o JOIN fl.items i ON i.oid = o.id", spill_mb=10, cpu_s=90)]
    opt = _opt()
    d = C.collect_topsql(Runner(rows), opt)
    assert [r[0] for r in d.rows] == ["1", "2"]
    assert "CPU:2(" in d.headline and "溢出:2(" in d.headline and "调用:1(" in d.headline
    assert "已排除 2 条内核系统 SQL(9、8" in d.headline and "800.0 秒" in d.headline
    assert opt.stmt_spill_kb == (50 + 10) * 1024, "落盘总量仍按全部语句算"
    assert all(f.sql_id in ("1", "2") for f in d.findings if f.sql_id)


def test_a_window_with_only_system_sql_says_so():
    d = C.collect_topsql(Runner([stmt("9", 5, "select count(*) from pg_class")]), _opt())
    assert d.headline.startswith("窗口内只有内核系统 SQL：已排除 1 条")

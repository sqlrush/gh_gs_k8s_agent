"""统计新鲜度:采集器一侧(假 runner,不连库)。SQL 形态见 test_health_registry_units.py。

faultlab V7(2026-10-24):导入 50 万行没做 ANALYZE 的 fl.import_stage 根本不在候选里。
候选门槛加了「实时页数」(min_pages),采集器要把它传下去;旧白名单没有这个参数时
中间件会拒,以前那条路径静默当成「没有陈旧的表」。
"""
from __future__ import annotations

import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "skills" / "gaussdb-health" / "scripts"

for _m in ("collectors", "model", "thresholds", "util", "report", "aggregate", "counters",
           "running_sql"):
    sys.modules.pop(_m, None)
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_ROOT))

import collectors  # noqa: E402
import thresholds  # noqa: E402
from common import access  # noqa: E402


class _Runner:
    def __init__(self, stale):
        self.stale, self.calls = stale, []

    def run(self, script, params=None):
        self.calls.append((script, dict(params or {})))
        if script == "health.stale_stats":
            if isinstance(self.stale, Exception):
                raise self.stale
            return self.stale
        if script == "health.invalid_index":
            return [{"cnt": "0"}]
        return []


def _never_analyzed():
    return {"tbl_name": "fl.import_stage", "frozen_pages": "0", "cur_pages": "4202", "frozen_tuples": "0",
            "live_tuples": "0", "stat_columns": "0", "last_analyze": "never", "last_autoanalyze": "never"}


def test_collector_passes_the_page_gate():
    th = thresholds.default_thresholds()
    r = _Runner([])
    collectors.collect_schema(r, th, 10)
    params = dict(r.calls)["health.stale_stats"]
    assert params["min_pages"] == th.stale_min_pages == 128
    assert params["min_rows"] == th.stale_min_rows


def test_a_never_analyzed_table_is_flagged():
    """V7 形态:relpages=0、reltuples=0、pg_stats 0 列 —— 判定本来就会报,缺的只是它进不了候选。"""
    d = collectors.collect_schema(_Runner([_never_analyzed()]), thresholds.default_thresholds(), 10)
    assert [f.code for f in d.findings] == ["STALE_STATS"]
    assert "fl.import_stage" in d.findings[0].metric


def test_an_unreadable_stale_stats_script_is_said_not_silent():
    """客户还没重导白名单时,新代码传的 min_pages 会被中间件拒(脚本未声明的参数)。"""
    err = access.QueryError("传入了脚本未声明的参数：min_pages")
    d = collectors.collect_schema(_Runner(err), thresholds.default_thresholds(), 10)
    rows = [r for r in d.rows if r[0] == "统计新鲜度"]
    assert rows and rows[0][1] == "（取不到）" and "min_pages" in rows[0][2]
    assert d.findings == []
    assert "统计不足以支撑估算的表 取不到" in d.headline, "取不到就不能写成 0 张"

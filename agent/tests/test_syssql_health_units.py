"""健康检查里排除内核系统 SQL(2026-10-22 客户要求):慢 SQL 维度、时段 Top SQL 的标题、时段落盘冠军。"""
from __future__ import annotations

import datetime as _dt
import json
import pathlib
import subprocess
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "skills" / "gaussdb-health" / "scripts"
for _m in ("model", "thresholds", "util", "collectors", "report", "window",
           "native", "snaps", "interp", "recheck", "finalreport", "kbrefs", "appendix", "mdreport", "render", "ansi"):
    sys.modules.pop(_m, None)
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_ROOT))

import collectors  # noqa: E402
import thresholds  # noqa: E402
import window  # noqa: E402
from common import timewin  # noqa: E402



@pytest.fixture(autouse=True)
def _state_dir(monkeypatch, tmp_path):
    """判定缓存落在状态目录:测试里指到临时目录,不碰开发机上真的 GSDB_HOME。"""
    monkeypatch.setenv("GSDB_HOME", str(tmp_path / "gdaa"))


class Runner:
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def run(self, name, params=None):
        self.calls.append((name, dict(params or {})))
        a = self.answers.get(name, [])
        return a(params) if callable(a) else a


def _slow(sid, query, avg="3000", total="9.0"):
    return {"unique_sql_id": sid, "query": query, "calls": "3", "avg_ms": avg, "total_sec": total, "cpu_sec": "1.0"}


def test_slow_sql_dimension_skips_system_sql_and_says_so():
    rows = [_slow("9", "select ?, ?, t.* from dbe_perf.summary_stat_all_tables t", "9000", "30.0"),
            _slow("1", "UPDATE fl.a SET x = ? WHERE id = ?")]
    r = Runner({"health.slow_sql": lambda p: rows[: p["limit"]]})
    d = collectors.collect_slowsql(r, thresholds.default_thresholds(), 5)
    assert [row[0] for row in d.rows] == ["1"]
    assert d.headline.startswith("Top1 avg 3000ms") and "已排除 1 条内核系统 SQL(9,合计 30.0 秒)" in d.headline
    assert r.calls[0][1]["limit"] == 5 + collectors.syssql.SCREEN_MARGIN


def _cov():
    t0 = _dt.datetime(2026, 10, 22, 10, 0)
    return timewin.Coverage(want_from=t0, want_to=t0, start=timewin.Point("snapshot", t0, 1),
                            end=timewin.Point("snapshot", t0 + _dt.timedelta(hours=1), 2), complete=True, note="")


def test_spill_champion_is_not_a_system_statement_but_its_spill_still_counts():
    rows = [{"sid": "9", "query": "select ?, ?, t.* from dbe_perf.summary_statio_all_tables t", "spill_kb": "4000"},
            {"sid": "1", "query": "SELECT * FROM fl.orders ORDER BY created_at", "spill_kb": "1000"}]
    note, champ = window._spill_extra(Runner({window.WDR_TOP_SQL: rows}), _cov(), 8000 * 1024)
    assert champ == "1"


def test_window_top_sql_headline_carries_what_topsql_excluded(monkeypatch):
    payload = {"rows": [{"sql_id": "1", "calls": 1, "total_sec": 2.0, "avg_ms": 2.0, "query": "UPDATE fl.a"}],
               "system_sql": {"excluded": ["9"], "brief": "已排除 1 条内核系统 SQL(9,合计 3.0 秒)"}}
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, json.dumps(payload), ""))
    d = window.topsql_dim(_cov(), 10, "topsql.py", 30)
    assert d.headline.endswith("；已排除 1 条内核系统 SQL(9,合计 3.0 秒)")

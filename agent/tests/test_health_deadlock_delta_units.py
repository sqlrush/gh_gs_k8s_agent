"""死锁「距上次新增」接进报告(health.annotate_counters,假 runner + 临时存档目录,不连库)。

**faultlab 那轮(2026-10-24)的实测:** 死锁每 30 秒 +1,health 两次巡检只给累计值 80 → 96,
没有「距上次新增」,也**一句没说为什么** —— Mac 上没设存档目录,没有基线;脚本其实算出了原因,
但那句话只用来重算总体状态,从没进报告。SKILL.md 却写着「第一次巡检没有基线,脚本会说明」。

下面钉死:给不出增量时,原因出现在维度表与 DEADLOCKS 那条发现里;有上一份存档报告时用它算。
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "skills" / "gaussdb-health" / "scripts"

for _m in ("model", "thresholds", "util", "collectors", "report", "health", "aggregate",
           "render", "counters", "running_sql"):
    sys.modules.pop(_m, None)
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_ROOT))

import collectors  # noqa: E402
import counters  # noqa: E402
import health  # noqa: E402
from common import reports  # noqa: E402
from model import HealthEvidence  # noqa: E402
from thresholds import default_thresholds  # noqa: E402

CONN = "api/10-0-0-9-postgres"


class _Runner:
    def __init__(self, deadlocks):
        self.deadlocks = deadlocks

    def run(self, script, params=None):
        if script == "health.db_concurrency":
            return [{"deadlocks": str(self.deadlocks), "xact_commit": "4169057", "xact_rollback": "269"}]
        if script == "health.prepared_xacts":
            return [{"cnt": "0"}]
        return []


def _ev(deadlocks=96):
    d = collectors.collect_concurrency(_Runner(deadlocks), default_thresholds(), 10)
    return HealthEvidence(conn=CONN, dims=[d], findings=list(d.findings))


def _row(ev, label):
    return next((r[1] for r in ev.dims[0].rows if r[0] == label), None)


def _deadlock_finding(ev):
    found = [f for f in ev.findings if f.code == "DEADLOCKS"]
    assert len(found) == 1
    assert found[0] in ev.dims[0].findings, "维度里与汇总表里的那条要一致"
    return found[0]


@pytest.fixture
def no_archive(monkeypatch):
    monkeypatch.delenv(reports.ENV_DIR, raising=False)


@pytest.fixture
def archive_dir(monkeypatch, tmp_path):
    monkeypatch.setenv(reports.ENV_DIR, str(tmp_path))
    return tmp_path / "health" / reports.instance_key(CONN)


def test_without_an_archive_the_report_says_why_there_is_no_delta(no_archive):
    """faultlab 原样:Mac 上没有存档目录。累计值照报,但要写明给不出增量、为什么。"""
    ev = _ev(96)
    note = health.annotate_counters(ev, CONN, full_run=True)
    row = _row(ev, "deadlocks 距上次新增")
    assert row is not None and row.startswith("给不出") and "GSDB_REPORTS_DIR" in row
    f = _deadlock_finding(ev)
    assert f.value == "96", "给不出增量时值仍是累计值,不能编一个增量"
    assert "给不出" in f.evidence and "GSDB_REPORTS_DIR" in f.evidence
    assert "不会自己清零" in f.evidence, "要说明这是只增不减的累计值"
    assert "GSDB_REPORTS_DIR" in note
    assert "已留基线" not in note, "没有存档目录就留不下基线,不能这么说"


def test_previous_archived_report_gives_the_delta(archive_dir, tmp_path):
    """没有基线文件、但存档里有上一份完整巡检:用它算「距上次新增」。"""
    reports.archive("health", {"dims": [{"dimension": "Transactions / Concurrency", "available": True,
                                         "rows": [["deadlocks", "80"],
                                                  ["commit/rollback", "4160000/260 (0.0%回滚)"]]}]},
                    now="20261024T014700Z", instance=CONN)
    ev = _ev(96)
    note = health.annotate_counters(ev, CONN, full_run=True)
    assert _row(ev, "deadlocks 距上次新增") == "16（累计 96）"
    f = _deadlock_finding(ev)
    assert f.metric == "死锁（距上次新增）" and f.value == "16（累计 96）"
    assert "20261024T014700Z" in note
    assert (archive_dir / counters.BASELINE_NAME).exists(), "完整巡检之后留下基线"


def test_first_run_says_it_is_the_first_and_leaves_a_baseline(archive_dir):
    ev = _ev(96)
    note = health.annotate_counters(ev, CONN, full_run=True)
    row = _row(ev, "deadlocks 距上次新增")
    assert row.startswith("给不出") and "第一次" in row
    assert "已留基线" in note
    saved = json.loads((archive_dir / counters.BASELINE_NAME).read_text(encoding="utf-8"))
    assert saved["counters"]["deadlocks"] == 96


def test_a_partial_run_says_it_leaves_no_baseline(archive_dir):
    """--include/--exclude 的局部巡检不更新基线(否则下一次完整巡检的「距上次」被截断),要说出来。"""
    ev = _ev(96)
    note = health.annotate_counters(ev, CONN, full_run=False)
    assert "局部巡检" in note and not (archive_dir / counters.BASELINE_NAME).exists()


def test_a_reset_is_said_to_be_a_reset_not_zero_growth(archive_dir):
    counters.save(archive_dir / counters.BASELINE_NAME, "20261024T014700Z", {"deadlocks": 120})
    ev = _ev(96)
    health.annotate_counters(ev, CONN, full_run=True)
    row = _row(ev, "deadlocks 距上次新增")
    assert row.startswith("给不出") and "重置" in row
    assert "重置" in _deadlock_finding(ev).evidence


def test_zero_new_deadlocks_still_drops_the_finding(archive_dir):
    """原有行为不变:有基线且零新增 → 撤掉 DEADLOCKS,改成信息行。"""
    counters.save(archive_dir / counters.BASELINE_NAME, "20261024T014700Z",
                  {"deadlocks": 96, "xact_commit": 4169057, "xact_rollback": 269})
    ev = _ev(96)
    health.annotate_counters(ev, CONN, full_run=True)
    assert [f.code for f in ev.findings] == []
    assert "没有新增" in _row(ev, "deadlocks 判定")


def test_no_deadlocks_at_all_adds_no_noise(no_archive):
    """累计 0 次:没有什么要解释的,不加「给不出」那一行。"""
    ev = _ev(0)
    health.annotate_counters(ev, CONN, full_run=True)
    assert _row(ev, "deadlocks 距上次新增") is None

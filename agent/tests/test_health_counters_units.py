"""健康检查的累计计数器增量(纯函数,不连库)。

**要钉死的事:** 死锁数、回滚数是**自上次统计重置以来**的累计值。发生过一次死锁之后
`deadlocks` 永远 > 0,健康检查从此每次都报「死锁累计数 24」—— 报了三个月、其中两个月
零新增,看报告的人分不出「正在发生」和「历史上发生过」,久了当背景噪音,真的新增时也看不见。

下面每条测试都对着一种**错了看不出来**的情形:没有基线却假装有、统计重置后相减出负数
被当成「没有新增」、累计回滚率把一阵疯狂回滚摊平。
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "skills" / "gaussdb-health" / "scripts"

# health 与别的 skill 有同名模块(model / collectors / report / thresholds)
for _m in ("counters", "model", "collectors", "report", "thresholds", "aggregate", "util"):
    sys.modules.pop(_m, None)
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_ROOT))

import counters as C  # noqa: E402


def test_delta_is_the_increase_since_the_baseline():
    res = C.compute({"deadlocks": 27, "xact_commit": 1000, "xact_rollback": 10},
                    {"deadlocks": 26, "xact_commit": 900, "xact_rollback": 5}, "t1")
    assert res.reset is False
    assert res.items["deadlocks"].delta == 1
    assert res.items["deadlocks"].now == 27
    assert res.items["xact_commit"].delta == 100


def test_without_a_baseline_the_delta_is_none_not_zero():
    """没有基线时「增量」是**未知**,不是 0。当成 0 会变成一句「距上次没有新增」,
    而那是在没有任何依据的情况下下的结论。"""
    res = C.compute({"deadlocks": 24, "xact_commit": 1, "xact_rollback": 0}, {}, "")
    assert res.items["deadlocks"].delta is None
    assert res.items["deadlocks"].now == 24
    assert "没有基线" in res.note


def test_counters_going_backwards_is_a_reset_not_zero_growth():
    """`pg_stat_reset()` 或实例重启之后,计数器从头开始。相减得到负数毫无意义,
    必须明说「给不出增量」—— 这正是 WDR 那个 DB time bug 的同一类错误。"""
    res = C.compute({"deadlocks": 2, "xact_commit": 10, "xact_rollback": 0},
                    {"deadlocks": 24, "xact_commit": 1000, "xact_rollback": 5}, "t1")
    assert res.reset is True
    assert all(v.delta is None for v in res.items.values())
    assert "重置" in res.note and "不是" in res.note


def test_a_counter_missing_from_the_baseline_is_unknown_not_full_value():
    """基线里没有这一项(比如老版本基线)→ 这一项的增量未知。
    **不能**像 Top SQL 那样「基线没有就整条计入」—— 那边是「窗口里新出现的语句」,
    这边是「同一个计数器只是上次没记」,含义完全不同,当成全量会虚报一次巨大新增。"""
    res = C.compute({"deadlocks": 27, "xact_commit": 10, "xact_rollback": 1},
                    {"xact_commit": 5, "xact_rollback": 1}, "t1")
    assert res.items["deadlocks"].delta is None
    assert res.items["xact_commit"].delta == 5


def test_rollback_rate_on_the_window_is_not_the_cumulative_rate():
    """**这是「累计拉平」最直观的一例。**

    库累计 100 万笔、只回滚 10 笔 → 累计回滚率 0.001%,看着很健康。
    而最近这一窗口 100 笔里回滚了 90 笔 → 窗口回滚率 90%,库其实正在出问题。
    """
    res = C.compute({"deadlocks": 0, "xact_commit": 1_000_010, "xact_rollback": 100},
                    {"deadlocks": 0, "xact_commit": 1_000_000, "xact_rollback": 10}, "t1")
    assert C.rollback_pct(res, "delta") == pytest.approx(90.0)
    assert C.rollback_pct(res, "total") == pytest.approx(0.01, abs=0.01)


def test_rollback_rate_is_none_when_nothing_happened_in_the_window():
    """窗口里一笔事务都没有 → 回滚率无从计算,返回 None(不是 0%)。"""
    res = C.compute({"deadlocks": 0, "xact_commit": 100, "xact_rollback": 1},
                    {"deadlocks": 0, "xact_commit": 100, "xact_rollback": 1}, "t1")
    assert C.rollback_pct(res, "delta") is None


def test_rollback_rate_is_none_after_a_reset():
    res = C.compute({"deadlocks": 0, "xact_commit": 1, "xact_rollback": 0},
                    {"deadlocks": 0, "xact_commit": 1000, "xact_rollback": 5}, "t1")
    assert C.rollback_pct(res, "delta") is None


# ---------------------------------------------------------------- 基线存取

def test_baseline_round_trip(tmp_path):
    p = tmp_path / "health" / "inst" / C.BASELINE_NAME
    assert C.save(p, "20261017T120000Z", {"deadlocks": 27}) is True
    vals, at, why = C.load(p)
    assert why == "" and at == "20261017T120000Z" and vals["deadlocks"] == 27


def test_no_reports_dir_means_no_baseline_and_says_so():
    assert C.baseline_path(None, "inst") is None
    vals, at, why = C.load(None)
    assert vals == {} and why


def test_first_run_is_reported_as_such(tmp_path):
    vals, at, why = C.load(tmp_path / "nope.json")
    assert vals == {} and "第一次" in why


def test_corrupt_baseline_is_reported_not_crashed(tmp_path):
    p = tmp_path / C.BASELINE_NAME
    p.write_text("{坏的", encoding="utf-8")
    vals, at, why = C.load(p)
    assert vals == {} and why


def test_baseline_with_unparsable_values_is_treated_as_missing(tmp_path):
    p = tmp_path / C.BASELINE_NAME
    p.write_text(json.dumps({"at": "t", "counters": {"deadlocks": "很多"}}), encoding="utf-8")
    vals, at, why = C.load(p)
    assert vals == {} and why


def test_baseline_path_lives_next_to_the_archived_reports():
    p = C.baseline_path(pathlib.Path("/nas/me/reports"), "api_10-0-0-9-postgres")
    assert p == pathlib.Path("/nas/me/reports/health/api_10-0-0-9-postgres/_counters.json")


# ---------------------------------------------------------------- 退回上一份存档报告

def _archived(tmp_path, monkeypatch, *reports_):
    """用真的 reports.archive 写存档(与 health.py 同一个函数),返回该实例的目录。"""
    from common import reports
    monkeypatch.setenv(reports.ENV_DIR, str(tmp_path))
    for stamp, name, rows in reports_:
        payload = {"dims": [{"dimension": "Transactions / Concurrency", "available": True, "rows": rows}]}
        reports.archive("health", payload, name=name, now=stamp, instance="api/10-0-0-9-postgres",
                        latest=not (name or "").endswith(".span"))
    return tmp_path / "health" / reports.instance_key("api/10-0-0-9-postgres")


def test_previous_archived_report_gives_the_counters(tmp_path, monkeypatch):
    """基线文件没有(比如从没有基线文件的老版本升级上来)时,上一份完整巡检报告里就有
    这几个累计值 —— 拿它算增量,不必白白再等一次巡检。"""
    d = _archived(tmp_path, monkeypatch,
                  ("20261024T014700Z", None, [["deadlocks", "80"], ["commit/rollback", "4000/200 (4.8%回滚)"],
                                              ["prepared 2PC", "2"]]))
    vals, at, why = C.load_from_reports(d)
    assert why == "" and at == "20261024T014700Z"
    assert vals == {"deadlocks": 80, "xact_commit": 4000, "xact_rollback": 200}


def test_span_reports_are_not_a_baseline(tmp_path, monkeypatch):
    """时段报告的「死锁」是窗口内增量(Window Transactions),维度也不同 —— 只认「此刻」巡检。"""
    d = _archived(tmp_path, monkeypatch,
                  ("20261024T014700Z", None, [["deadlocks", "80"]]),
                  ("20261024T015000Z", "20261024T015000Z.span", [["deadlocks", "3"]]))
    vals, at, _ = C.load_from_reports(d)
    assert vals == {"deadlocks": 80} and at == "20261024T014700Z"


def test_no_archived_report_says_why(tmp_path):
    vals, at, why = C.load_from_reports(tmp_path / "nothing-here")
    assert vals == {} and "第一次" in why
    assert C.load_from_reports(None)[2]


def test_load_baseline_prefers_the_baseline_file_then_the_archive(tmp_path, monkeypatch):
    d = _archived(tmp_path, monkeypatch, ("20261024T014700Z", None, [["deadlocks", "80"]]))
    vals, at, _ = C.load_baseline(d / C.BASELINE_NAME)
    assert vals == {"deadlocks": 80}, "基线文件不存在时退回上一份报告"
    C.save(d / C.BASELINE_NAME, "20261024T015500Z", {"deadlocks": 90})
    vals, at, _ = C.load_baseline(d / C.BASELINE_NAME)
    assert vals == {"deadlocks": 90} and at == "20261024T015500Z"


def test_no_reports_dir_names_the_setting():
    """Mac / 非容器线没有存档目录 —— 原因要说到人能照着办的程度。"""
    assert "GSDB_REPORTS_DIR" in C.load_baseline(None)[2]


def test_tracked_counters_are_the_cumulative_ones():
    """跟踪的必须是**累计型**计数器。瞬时量(连接数、锁、死元组)不能放进来 ——
    对瞬时量做差没有意义,「现在 10 个连接、上次 8 个」不代表「新增 2 个连接」。"""
    assert set(C.TRACKED) == {"deadlocks", "xact_commit", "xact_rollback"}
